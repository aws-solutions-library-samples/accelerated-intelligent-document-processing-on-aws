# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Module for saving document data to reporting storage.
"""

import datetime
import io
import json
import logging
import re
from typing import Any, Dict, List, Optional

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from idp_common.config.models import IDPConfig
from idp_common.models import Document
from idp_common.s3 import get_json_content
from idp_common.utils import parse_s3_uri

# Configure logging
logger = logging.getLogger(__name__)


class SaveReportingData:
    """
    Class for saving document data to reporting storage.

    This class provides methods to save different types of document data
    to a reporting bucket in Parquet format for analytics.
    """

    def __init__(
        self,
        reporting_bucket: str,
        database_name: Optional[str] = None,
        config: Optional[IDPConfig] = None,
    ):
        """
        Initialize the SaveReportingData class.

        Args:
            reporting_bucket: S3 bucket name for reporting data
            database_name: Glue database name for creating tables (optional)
            config: Configuration dictionary containing pricing and other settings (optional)
        """
        self.reporting_bucket = reporting_bucket
        self.database_name = database_name
        self.config = config or IDPConfig()
        self.s3_client = boto3.client("s3")
        self.glue_client = boto3.client("glue") if database_name else None

        # Cache for pricing data to avoid repeated processing
        self._pricing_cache = None

    def _serialize_value(self, value: Any) -> Optional[str]:
        """
        Serialize complex values for Parquet storage as strings.

        Args:
            value: The value to serialize

        Returns:
            Serialized value as string, or None if input is None
        """
        if value is None:
            return None
        elif isinstance(value, str):
            return value
        elif isinstance(value, (int, float, bool)):
            # Convert numeric/boolean values to strings
            return str(value)
        elif isinstance(value, (list, dict)):
            # Convert complex types to JSON strings
            return json.dumps(value)
        else:
            # Convert other types to string
            return str(value)

    def _save_records_as_parquet(
        self, records: List[Dict], s3_key: str, schema: pa.Schema
    ) -> None:
        """
        Save a list of records as a Parquet file to S3 with explicit schema.

        Args:
            records: List of dictionaries to save
            s3_key: S3 key path
            schema: PyArrow schema for the table
        """
        if not records:
            logger.warning("No records to save")
            return

        # Create PyArrow table from records with explicit schema
        table = pa.Table.from_pylist(records, schema=schema)

        # Create in-memory buffer
        buffer = io.BytesIO()

        # Write parquet data to buffer
        pq.write_table(table, buffer, compression="snappy")

        # Upload to S3
        buffer.seek(0)
        self.s3_client.put_object(
            Bucket=self.reporting_bucket,
            Key=s3_key,
            Body=buffer.getvalue(),
            ContentType="application/octet-stream",
        )
        logger.info(
            f"Saved {len(records)} records as Parquet to s3://{self.reporting_bucket}/{s3_key}"
        )

    def _parse_s3_uri(self, uri: str) -> tuple:
        """
        Parse an S3 URI into bucket and key.

        Args:
            uri: S3 URI in the format s3://bucket/key

        Returns:
            Tuple of (bucket, key)
        """
        bucket, key = parse_s3_uri(uri)
        return bucket, key

    def _infer_pyarrow_type(self, value: Any) -> pa.DataType:
        """
        Infer PyArrow data type from a Python value.

        Args:
            value: The value to infer type from

        Returns:
            PyArrow data type
        """
        if value is None:
            return pa.string()  # Default to string for null values
        elif isinstance(value, bool):
            return pa.bool_()
        elif isinstance(value, int):
            return pa.int64()
        elif isinstance(value, float):
            return pa.float64()
        elif isinstance(value, str):
            return pa.string()
        elif isinstance(value, (list, dict)):
            return pa.string()  # Store complex types as JSON strings
        else:
            return pa.string()  # Default to string for unknown types

    def _convert_value_to_string(self, value: Any) -> Optional[str]:
        """
        Convert any value to string, handling special cases for robust type compatibility.

        Args:
            value: The value to convert

        Returns:
            String representation of the value, or None if input is None
        """
        if value is None:
            return None
        elif isinstance(value, bytes):
            # Handle binary data
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError:
                # If can't decode, convert to hex string
                return value.hex()
        elif isinstance(value, (list, dict)):
            return json.dumps(value)
        elif isinstance(value, datetime.datetime):
            return value.isoformat()
        elif isinstance(value, (int, float, bool)):
            return str(value)
        else:
            return str(value)

    def _flatten_json_data(
        self, data: Dict[str, Any], prefix: str = ""
    ) -> Dict[str, Any]:
        """
        Flatten nested JSON data with dot notation and convert all values to strings
        for robust type compatibility.

        Args:
            data: The JSON data to flatten
            prefix: Prefix for nested keys

        Returns:
            Flattened dictionary with all values converted to strings
        """
        flattened = {}

        for key, value in data.items():
            new_key = f"{prefix}.{key}" if prefix else key

            if isinstance(value, dict) and value:
                # Recursively flatten nested dictionaries
                flattened.update(self._flatten_json_data(value, new_key))
            elif isinstance(value, list):
                # Convert lists to JSON strings
                flattened[new_key] = json.dumps(value) if value else None
            else:
                # Convert all values to strings for type consistency
                flattened[new_key] = self._convert_value_to_string(value)

        return flattened

    def _create_dynamic_schema(self, records: List[Dict[str, Any]]) -> pa.Schema:
        """
        Create a PyArrow schema dynamically from a list of records.
        Uses conservative typing - all fields default to string unless whitelisted.
        This prevents Athena type compatibility issues.

        Args:
            records: List of dictionaries to analyze

        Returns:
            PyArrow schema with conservative string typing
        """
        # Define fields that should maintain specific types
        TIMESTAMP_FIELDS = {
            "timestamp",
            "evaluation_date",
        }

        if not records:
            # Return a minimal schema with just section_id
            return pa.schema([("section_id", pa.string())])

        # Collect all unique field names
        all_fields = set()
        for record in records:
            all_fields.update(record.keys())

        # Create schema with conservative typing
        schema_fields = []
        for field_name in sorted(all_fields):  # Sort for consistent ordering
            if field_name in TIMESTAMP_FIELDS:
                # Keep timestamps as timestamps for proper time-based queries
                pa_type = pa.timestamp("ms")
            else:
                # Default everything else to string to prevent type conflicts
                pa_type = pa.string()

            schema_fields.append((field_name, pa_type))

        return pa.schema(schema_fields)

    def _sanitize_records_for_schema(
        self, records: List[Dict[str, Any]], schema: pa.Schema
    ) -> List[Dict[str, Any]]:
        """
        Sanitize records to ensure they conform to the schema and handle type compatibility issues.

        Args:
            records: List of record dictionaries
            schema: PyArrow schema to conform to

        Returns:
            List of sanitized records
        """
        sanitized_records = []

        for record in records:
            sanitized_record = {}

            # Process each field in the schema
            for field in schema:
                field_name = field.name
                value = record.get(field_name)

                if value is None:
                    sanitized_record[field_name] = None
                elif field.type == pa.string():
                    # Convert all values to strings for string fields
                    sanitized_record[field_name] = self._convert_value_to_string(value)
                elif field.type == pa.timestamp("ms"):
                    # Handle timestamp fields
                    if isinstance(value, datetime.datetime):
                        sanitized_record[field_name] = value
                    else:
                        # Try to parse string timestamps
                        try:
                            if isinstance(value, str):
                                sanitized_record[field_name] = (
                                    datetime.datetime.fromisoformat(
                                        value.replace("Z", "+00:00")
                                    )
                                )
                            else:
                                sanitized_record[field_name] = None
                        except (ValueError, TypeError):
                            sanitized_record[field_name] = None
                else:
                    # For any other types, convert to string as fallback
                    sanitized_record[field_name] = self._convert_value_to_string(value)

            # Add any fields from the record that aren't in the schema (shouldn't happen with dynamic schema)
            for field_name, value in record.items():
                if field_name not in sanitized_record:
                    sanitized_record[field_name] = self._convert_value_to_string(value)

            sanitized_records.append(sanitized_record)

        return sanitized_records

    def _convert_schema_to_glue_columns(
        self, schema: pa.Schema
    ) -> List[Dict[str, str]]:
        """
        Convert PyArrow schema to Glue table columns format.

        Args:
            schema: PyArrow schema

        Returns:
            List of column definitions for Glue
        """
        columns = []
        for field in schema:
            # Map PyArrow types to Glue/Hive types
            if field.type == pa.string():
                glue_type = "string"
            elif field.type == pa.bool_():
                glue_type = "boolean"
            elif field.type == pa.int64():
                glue_type = "bigint"
            elif field.type == pa.int32():
                glue_type = "int"
            elif field.type == pa.float64():
                glue_type = "double"
            elif field.type == pa.float32():
                glue_type = "float"
            elif field.type == pa.timestamp("ms"):
                glue_type = "timestamp"
            else:
                # Default to string for unknown types
                glue_type = "string"

            columns.append({"Name": field.name, "Type": glue_type})

        return columns

    def _create_or_update_glue_table(
        self, section_type: str, schema: pa.Schema, new_section_created: bool = False
    ) -> bool:
        """
        Create or update a Glue table for a document section type.

        Args:
            section_type: The document section type (e.g., 'invoice', 'receipt')
            schema: PyArrow schema for the table
            new_section_created: Whether this is a new section type

        Returns:
            True if table was created or updated, False otherwise
        """
        if not self.glue_client or not self.database_name:
            logger.debug(
                "Glue client or database name not configured, skipping table creation"
            )
            return False

        # Escape section_type to make it table-name-safe and s3 prefix-safe
        # Note: we escape '-' in tablename but not in s3 prefix, only to provide backward compatability for data already stored.
        section_type_tablename = re.sub(r"[/\\:*?\"<>|-]", "_", section_type.lower())
        section_type_prefix = re.sub(r"[/\\:*?\"<>|]", "_", section_type.lower())
        table_name = f"document_sections_{section_type_tablename}"

        # Convert schema to Glue columns
        columns = self._convert_schema_to_glue_columns(schema)

        # Table input for create/update
        table_input = {
            "Name": table_name,
            "Description": f"Document sections table for type: {section_type}",
            "StorageDescriptor": {
                "Columns": columns,
                "Location": f"s3://{self.reporting_bucket}/document_sections/{section_type_prefix}/",
                "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                "Compressed": True,
                "SerdeInfo": {
                    "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                },
            },
            "PartitionKeys": [{"Name": "date", "Type": "string"}],
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {
                "classification": "parquet",
                "typeOfData": "file",
                "projection.enabled": "true",
                "projection.date.type": "date",
                "projection.date.format": "yyyy-MM-dd",
                "projection.date.range": "2024-01-01,2030-12-31",
                "projection.date.interval": "1",
                "projection.date.interval.unit": "DAYS",
                "storage.location.template": f"s3://{self.reporting_bucket}/document_sections/{section_type_prefix}/date=${{date}}/",
            },
        }

        try:
            # Try to get the existing table
            existing_table = self.glue_client.get_table(
                DatabaseName=self.database_name, Name=table_name
            )

            # Check if schema has changed significantly
            existing_columns = (
                existing_table.get("Table", {})
                .get("StorageDescriptor", {})
                .get("Columns", [])
            )
            existing_column_names = {col["Name"] for col in existing_columns}
            new_column_names = {col["Name"] for col in columns}

            # Check if location has changed
            existing_location = (
                existing_table.get("Table", {})
                .get("StorageDescriptor", {})
                .get("Location", "")
            )
            new_location = table_input["StorageDescriptor"]["Location"]

            # Check if columns or location have changed
            columns_changed = bool(new_column_names - existing_column_names)
            location_changed = existing_location != new_location

            # If there are new columns or location has changed, update the table
            if columns_changed or location_changed:
                if columns_changed:
                    logger.info(f"Updating Glue table {table_name} with new columns")
                if location_changed:
                    logger.info(
                        f"Updating Glue table {table_name} with new location: {existing_location} -> {new_location}"
                    )

                self.glue_client.update_table(
                    DatabaseName=self.database_name, TableInput=table_input
                )
                return True
            else:
                logger.debug(
                    f"Glue table {table_name} already exists with current schema and location"
                )
                return False

        except Exception as get_table_error:
            # Check if it's an EntityNotFoundException or similar (table doesn't exist)
            error_str = str(get_table_error)
            if (
                "EntityNotFoundException" in error_str
                or "not found" in error_str.lower()
            ):
                # Table doesn't exist, create it
                logger.info(
                    f"Creating new Glue table {table_name} for section type: {section_type}"
                )
                try:
                    self.glue_client.create_table(
                        DatabaseName=self.database_name, TableInput=table_input
                    )
                    logger.info(f"Successfully created Glue table {table_name}")
                    return True
                except Exception as create_error:
                    # Check if it's an AlreadyExistsException
                    if "AlreadyExistsException" in str(create_error):
                        logger.debug(
                            f"Glue table {table_name} already exists (race condition)"
                        )
                        return False
                    logger.error(
                        f"Error creating Glue table {table_name}: {str(create_error)}"
                    )
                    return False
            else:
                # Some other error occurred
                logger.error(
                    f"Error checking Glue table {table_name}: {str(get_table_error)}"
                )
                return False

    def save(self, document: Document, data_to_save: List[str]) -> List[Dict[str, Any]]:
        """
        Save document data based on the data_to_save list.

        Args:
            document: Document object containing data to save
            data_to_save: List of data types to save

        Returns:
            List of results from each save operation
        """
        results = []

        # Process each data type based on data_to_save
        if "evaluation_results" in data_to_save:
            logger.info("Processing evaluation results")
            result = self.save_evaluation_results(document)
            if result:
                results.append(result)

        if "metering" in data_to_save:
            logger.info("Processing metering data")
            result = self.save_metering_data(document)
            if result:
                results.append(result)

        if "sections" in data_to_save:
            logger.info("Processing document sections")
            result = self.save_document_sections(document)
            if result:
                results.append(result)

        if "rule_validation_results" in data_to_save:
            logger.info("Processing rule validation results")
            result = self.save_rule_validation_results(document)
            if result:
                results.append(result)

        # Add more data types here as needed
        # if 'document_metadata' in data_to_save:
        #     logger.info("Processing document metadata")
        #     result = self.save_document_metadata(document)
        #     if result:
        #         results.append(result)

        return results

    def save_evaluation_results(self, document: Document) -> Optional[Dict[str, Any]]:
        """
        Save evaluation results for a document to the reporting bucket.

        Args:
            document: Document object containing evaluation results URI

        Returns:
            Dict with status and message, or None if no evaluation results
        """
        if not document.evaluation_results_uri:
            warning_msg = (
                f"No evaluation_results_uri available for document {document.id}"
            )
            logger.warning(warning_msg)
            return None

        try:
            # Load evaluation results from S3
            logger.info(
                f"Loading evaluation results from {document.evaluation_results_uri}"
            )
            eval_result = get_json_content(document.evaluation_results_uri)

            if not eval_result:
                warning_msg = f"Empty evaluation results for document {document.id}"
                logger.warning(warning_msg)
                return None

        except Exception as e:
            error_msg = f"Error loading evaluation results from {document.evaluation_results_uri}: {str(e)}"
            logger.error(error_msg)
            return {"statusCode": 500, "body": error_msg}

        # Define schemas specific to evaluation results (including doc split metrics)
        document_schema = pa.schema(
            [
                ("document_id", pa.string()),
                ("input_key", pa.string()),
                ("evaluation_date", pa.timestamp("ms")),
                ("accuracy", pa.float64()),
                ("precision", pa.float64()),
                ("recall", pa.float64()),
                ("f1_score", pa.float64()),
                ("false_alarm_rate", pa.float64()),
                ("false_discovery_rate", pa.float64()),
                ("weighted_overall_score", pa.float64()),
                ("execution_time", pa.float64()),
                # Doc split classification metrics
                ("page_level_accuracy", pa.float64()),
                ("split_accuracy_without_order", pa.float64()),
                ("split_accuracy_with_order", pa.float64()),
                ("total_pages", pa.int32()),
                ("total_splits", pa.int32()),
                ("correctly_classified_pages", pa.int32()),
                ("correctly_split_without_order", pa.int32()),
                ("correctly_split_with_order", pa.int32()),
                ("config_version", pa.string()),
            ]
        )

        section_schema = pa.schema(
            [
                ("document_id", pa.string()),
                ("section_id", pa.string()),
                ("section_type", pa.string()),
                ("accuracy", pa.float64()),
                ("precision", pa.float64()),
                ("recall", pa.float64()),
                ("f1_score", pa.float64()),
                ("false_alarm_rate", pa.float64()),
                ("false_discovery_rate", pa.float64()),
                ("weighted_overall_score", pa.float64()),
                ("evaluation_date", pa.timestamp("ms")),
                ("config_version", pa.string()),
            ]
        )

        attribute_schema = pa.schema(
            [
                ("document_id", pa.string()),
                ("section_id", pa.string()),
                ("section_type", pa.string()),
                ("attribute_name", pa.string()),
                ("expected", pa.string()),
                ("actual", pa.string()),
                ("matched", pa.bool_()),
                ("score", pa.float64()),
                ("reason", pa.string()),
                ("evaluation_method", pa.string()),
                ("confidence", pa.string()),
                ("confidence_threshold", pa.string()),
                ("weight", pa.float64()),
                ("evaluation_date", pa.timestamp("ms")),
                ("config_version", pa.string()),
            ]
        )

        # Use document.initial_event_time if available, otherwise use current time
        if document.initial_event_time:
            try:
                # Try to parse the initial_event_time string into a datetime object
                doc_time = datetime.datetime.fromisoformat(
                    document.initial_event_time.replace("Z", "+00:00")
                )
                evaluation_date = doc_time
                date_partition = doc_time.strftime("%Y-%m-%d")
                logger.info(
                    f"Using document initial_event_time: {document.initial_event_time} for partitioning"
                )
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"Could not parse document.initial_event_time: {document.initial_event_time}, using current time instead. Error: {str(e)}"
                )
                evaluation_date = datetime.datetime.now()
                date_partition = evaluation_date.strftime("%Y-%m-%d")
        else:
            logger.warning(
                "Document initial_event_time not available, using current time instead"
            )
            evaluation_date = datetime.datetime.now()
            date_partition = evaluation_date.strftime("%Y-%m-%d")

        # Escape document ID by replacing slashes with underscores
        document_id = document.id or document.input_key or "unknown"
        escaped_doc_id = re.sub(r"[/\\]", "_", document_id)

        # Create timestamp string for unique filenames (to avoid overwrites if same doc processed multiple times)
        timestamp_str = evaluation_date.strftime("%Y%m%d_%H%M%S_%f")[
            :-3
        ]  # Include milliseconds

        # 1. Document level metrics (including doc split metrics)
        # Extract doc split metrics if available
        doc_split_metrics = eval_result.get("doc_split_metrics", {})

        document_record = {
            "document_id": document_id,
            "input_key": document.input_key,
            "evaluation_date": evaluation_date,  # Use document's initial_event_time
            "accuracy": eval_result.get("overall_metrics", {}).get("accuracy", 0.0),
            "precision": eval_result.get("overall_metrics", {}).get("precision", 0.0),
            "recall": eval_result.get("overall_metrics", {}).get("recall", 0.0),
            "f1_score": eval_result.get("overall_metrics", {}).get("f1_score", 0.0),
            "false_alarm_rate": eval_result.get("overall_metrics", {}).get(
                "false_alarm_rate", 0.0
            ),
            "false_discovery_rate": eval_result.get("overall_metrics", {}).get(
                "false_discovery_rate", 0.0
            ),
            # Preserve None for documents whose sections were all no-ops
            # (no extractable schema). Athena / parquet float64 columns treat
            # this as NULL, so downstream queries can filter with IS NOT NULL
            # instead of accidentally averaging a 0.0 for excluded docs.
            "weighted_overall_score": eval_result.get("overall_metrics", {}).get(
                "weighted_overall_score"
            ),
            "execution_time": eval_result.get("execution_time", 0.0),
            # Doc split classification metrics (None if not available for backward compatibility)
            "page_level_accuracy": (
                doc_split_metrics.get("page_level_accuracy")
                if doc_split_metrics
                else None
            ),
            "split_accuracy_without_order": (
                doc_split_metrics.get("split_accuracy_without_order")
                if doc_split_metrics
                else None
            ),
            "split_accuracy_with_order": (
                doc_split_metrics.get("split_accuracy_with_order")
                if doc_split_metrics
                else None
            ),
            "total_pages": (
                doc_split_metrics.get("total_pages") if doc_split_metrics else None
            ),
            "total_splits": (
                doc_split_metrics.get("total_splits") if doc_split_metrics else None
            ),
            "correctly_classified_pages": (
                doc_split_metrics.get("correctly_classified_pages")
                if doc_split_metrics
                else None
            ),
            "correctly_split_without_order": (
                doc_split_metrics.get("correctly_split_without_order")
                if doc_split_metrics
                else None
            ),
            "correctly_split_with_order": (
                doc_split_metrics.get("correctly_split_with_order")
                if doc_split_metrics
                else None
            ),
            "config_version": document.config_version or "default",
        }

        # Save document metrics in Parquet format
        doc_key = f"evaluation_metrics/document_metrics/date={date_partition}/{escaped_doc_id}_{timestamp_str}_results.parquet"
        self._save_records_as_parquet([document_record], doc_key, document_schema)

        # 2. Section level metrics
        section_records = []
        # 3. Attribute level records
        attribute_records = []

        # Log section results count
        section_results = eval_result.get("section_results", [])
        logger.info(f"Processing {len(section_results)} section results")

        for section_result in section_results:
            section_id = section_result.get("section_id")
            section_type = section_result.get("document_class", "")

            # Section record
            section_record = {
                "document_id": document_id,
                "section_id": section_id,
                "section_type": section_type,
                "accuracy": section_result.get("metrics", {}).get("accuracy", 0.0),
                "precision": section_result.get("metrics", {}).get("precision", 0.0),
                "recall": section_result.get("metrics", {}).get("recall", 0.0),
                "f1_score": section_result.get("metrics", {}).get("f1_score", 0.0),
                "false_alarm_rate": section_result.get("metrics", {}).get(
                    "false_alarm_rate", 0.0
                ),
                "false_discovery_rate": section_result.get("metrics", {}).get(
                    "false_discovery_rate", 0.0
                ),
                # Sections excluded from scoring (no extractable schema) carry
                # ``None`` — keep it as SQL NULL so per-section rollups don't
                # average in a fake 0.0.
                "weighted_overall_score": section_result.get("metrics", {}).get(
                    "weighted_overall_score"
                ),
                "evaluation_date": evaluation_date,  # Use document's initial_event_time
                "config_version": document.config_version or "default",
            }
            section_records.append(section_record)

            # Log section metrics
            logger.debug(
                f"Added section record for section_id={section_id}, section_type={section_type}"
            )

            # Attribute records
            attributes = section_result.get("attributes", [])
            logger.debug(f"Section {section_id} has {len(attributes)} attributes")

            for attr in attributes:
                # Handle weight field - default to 1.0 if None or missing
                weight_value = attr.get("weight")
                weight = weight_value if weight_value is not None else 1.0

                attribute_record = {
                    "document_id": document_id,
                    "section_id": section_id,
                    "section_type": section_type,
                    "attribute_name": self._serialize_value(attr.get("name", "")),
                    "expected": self._serialize_value(attr.get("expected", "")),
                    "actual": self._serialize_value(attr.get("actual", "")),
                    "matched": attr.get("matched", False),
                    "score": attr.get("score", 0.0),
                    "reason": self._serialize_value(attr.get("reason", "")),
                    "evaluation_method": self._serialize_value(
                        attr.get("evaluation_method", "")
                    ),
                    "confidence": self._serialize_value(attr.get("confidence")),
                    "confidence_threshold": self._serialize_value(
                        attr.get("confidence_threshold")
                    ),
                    "weight": weight,  # Explicitly handle None values
                    "evaluation_date": evaluation_date,  # Use document's initial_event_time
                    "config_version": document.config_version or "default",
                }
                attribute_records.append(attribute_record)
                logger.debug(
                    f"Added attribute record for attribute_name={attr.get('name', '')}"
                )

        # Log counts
        logger.info(
            f"Collected {len(section_records)} section records and {len(attribute_records)} attribute records"
        )

        # Save section metrics in Parquet format
        if section_records:
            section_key = f"evaluation_metrics/section_metrics/date={date_partition}/{escaped_doc_id}_{timestamp_str}_results.parquet"
            self._save_records_as_parquet(section_records, section_key, section_schema)
        else:
            logger.warning("No section records to save")

        # Save attribute metrics in Parquet format
        if attribute_records:
            attr_key = f"evaluation_metrics/attribute_metrics/date={date_partition}/{escaped_doc_id}_{timestamp_str}_results.parquet"
            self._save_records_as_parquet(attribute_records, attr_key, attribute_schema)
        else:
            logger.warning("No attribute records to save")

        logger.info(
            f"Completed saving evaluation results to s3://{self.reporting_bucket}"
        )

        return {
            "statusCode": 200,
            "body": "Successfully saved evaluation results to reporting bucket",
        }

    def _get_pricing_from_config(self) -> Dict[str, Dict[str, float]]:
        """
        Get pricing information from the configuration dictionary.

        This method loads pricing from the configuration dictionary passed to the constructor,
        with caching to avoid repeated processing.

        Returns:
            Dictionary mapping service/unit combinations to prices
        """
        # Return cached pricing if available
        if self._pricing_cache is not None:
            return self._pricing_cache

        # Initialize empty pricing map
        pricing_map = {}

        # Load pricing from configuration
        try:
            if self.config.pricing:
                logger.info(
                    f"Found {len(self.config.pricing)} pricing entries in configuration"
                )

                config_loaded_count = 0
                # Convert configuration pricing to lookup dictionary (convert strings to floats)
                for service in self.config.pricing:
                    service_name = service.name
                    for unit_info in service.units:
                        unit_name = unit_info.name
                        try:
                            # Convert price string to float for calculations
                            price = float(unit_info.price)
                            if service_name not in pricing_map:
                                pricing_map[service_name] = {}
                            pricing_map[service_name][unit_name] = price
                            config_loaded_count += 1
                        except (ValueError, TypeError) as e:
                            logger.warning(
                                f"Invalid price value for {service_name}/{unit_name}: {unit_info.price}, error: {e}. Skipping entry."
                            )

                if config_loaded_count > 0:
                    logger.info(
                        f"Successfully loaded {config_loaded_count} pricing entries from configuration"
                    )
                else:
                    logger.warning("No valid pricing data found in configuration")
            else:
                logger.warning("No pricing section found in configuration")

        except Exception as e:
            logger.error(f"Error processing pricing from configuration: {str(e)}")

        # Cache the pricing from configuration
        self._pricing_cache = pricing_map
        return pricing_map

    def _create_or_update_metering_glue_table(self, schema: pa.Schema) -> bool:
        """
        Create or update a Glue table specifically for metering data.

        Args:
            schema: PyArrow schema for the metering table

        Returns:
            True if table was created or updated, False otherwise
        """
        if not self.glue_client or not self.database_name:
            logger.debug(
                "Glue client or database name not configured, skipping table creation"
            )
            return False

        table_name = "metering"

        # Convert schema to Glue columns
        columns = self._convert_schema_to_glue_columns(schema)

        # Table input for create/update
        table_input = {
            "Name": table_name,
            "Description": "Metering data table for document processing costs and usage",
            "StorageDescriptor": {
                "Columns": columns,
                "Location": f"s3://{self.reporting_bucket}/metering/",
                "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                "Compressed": True,
                "SerdeInfo": {
                    "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                },
            },
            "PartitionKeys": [{"Name": "date", "Type": "string"}],
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {
                "projection.enabled": "true",
                "projection.date.type": "date",
                "projection.date.format": "yyyy-MM-dd",
                "projection.date.range": "2020-01-01,NOW",
                "projection.date.interval": "1",
                "projection.date.interval.unit": "DAYS",
                "storage.location.template": f"s3://{self.reporting_bucket}/metering/date=${{date}}/",
            },
        }

        try:
            # Check if table exists
            existing_table_response = self.glue_client.get_table(
                DatabaseName=self.database_name, Name=table_name
            )

            # Table exists, check if we need to update it
            existing_table = existing_table_response["Table"]
            existing_columns = existing_table["StorageDescriptor"]["Columns"]

            # Check if new columns need to be added
            existing_column_names = {col["Name"] for col in existing_columns}
            new_column_names = {col["Name"] for col in columns}

            # Check if location has changed
            existing_location = existing_table["StorageDescriptor"].get("Location", "")
            new_location = table_input["StorageDescriptor"]["Location"]

            # Check if columns or location have changed
            columns_changed = not new_column_names.issubset(existing_column_names)
            location_changed = existing_location != new_location

            if columns_changed or location_changed:
                if columns_changed:
                    logger.info(f"Updating Glue table {table_name} with new columns")
                if location_changed:
                    logger.info(
                        f"Updating Glue table {table_name} with new location: {existing_location} -> {new_location}"
                    )

                self.glue_client.update_table(
                    DatabaseName=self.database_name, TableInput=table_input
                )
                logger.info(f"Successfully updated Glue table {table_name}")
                return True
            else:
                logger.debug(f"Glue table {table_name} already up to date")
                return True

        except Exception as e:
            if "EntityNotFoundException" in str(e):
                # Table doesn't exist, create it
                logger.info(f"Creating new Glue table {table_name} for metering data")
                try:
                    self.glue_client.create_table(
                        DatabaseName=self.database_name, TableInput=table_input
                    )
                    logger.info(f"Successfully created Glue table {table_name}")
                    return True
                except Exception as create_error:
                    if "AlreadyExistsException" in str(create_error):
                        # Race condition - table was created by another process
                        logger.info(
                            f"Glue table {table_name} already exists (created by another process)"
                        )
                        return True
                    else:
                        logger.error(
                            f"Error creating Glue table {table_name}: {str(create_error)}"
                        )
                        return False
            else:
                logger.error(
                    f"Error checking/updating Glue table {table_name}: {str(e)}"
                )
                return False

    def _get_unit_cost(self, service_api: str, unit: str) -> float:
        """
        Get the unit cost for a specific service API and unit using the configuration dictionary
        (same source as the UI).

        Args:
            service_api: The AWS service API (e.g., 'bedrock/model-id', 'textract/operation')
            unit: The unit of measurement (e.g., 'inputTokens', 'pages')

        Returns:
            Unit cost in USD, or 0.0 if not found
        """
        # Get pricing from configuration dictionary
        pricing_map = self._get_pricing_from_config()

        # Try exact match first
        if service_api in pricing_map and unit in pricing_map[service_api]:
            return pricing_map[service_api][unit]

        # Try partial matches for common patterns
        service_api_lower = service_api.lower()
        unit_lower = unit.lower()

        for service_key, service_costs in pricing_map.items():
            service_key_lower = service_key.lower()
            if (
                service_key_lower in service_api_lower
                or service_api_lower in service_key_lower
            ):
                for unit_key, cost in service_costs.items():
                    unit_key_lower = unit_key.lower()
                    if (
                        unit_key_lower == unit_lower
                        or unit_key_lower in unit_lower
                        or unit_lower in unit_key_lower
                    ):
                        logger.info(
                            f"Using partial match for {service_api}/{unit}: {service_key}/{unit_key} = ${cost}"
                        )
                        return cost

        # Log when no cost mapping is found
        logger.warning(
            f"No unit cost mapping found for service_api='{service_api}', unit='{unit}'. Using $0.0"
        )
        return 0.0

    def clear_pricing_cache(self):
        """
        Clear the cached pricing data to force reload from configuration on next access.
        Useful for testing or when configuration has been updated.
        """
        self._pricing_cache = None
        logger.info("Pricing cache cleared")

    def save_metering_data(self, document: Document) -> Optional[Dict[str, Any]]:
        """
        Save metering data for a document to the reporting bucket.

        Args:
            document: Document object containing metering data

        Returns:
            Dict with status and message, or None if no metering data
        """
        if not document.metering:
            warning_msg = f"No metering data to save for document {document.id}"
            logger.warning(warning_msg)
            return None

        # Define schema for metering data with new cost fields
        metering_schema = pa.schema(
            [
                ("document_id", pa.string()),
                ("context", pa.string()),
                ("service_api", pa.string()),
                ("unit", pa.string()),
                ("value", pa.float64()),
                ("number_of_pages", pa.int32()),
                ("unit_cost", pa.float64()),
                ("estimated_cost", pa.float64()),
                ("timestamp", pa.timestamp("ms")),
                ("config_version", pa.string()),
            ]
        )

        # Use document.initial_event_time if available, otherwise use current time
        if document.initial_event_time:
            try:
                # Try to parse the initial_event_time string into a datetime object
                doc_time = datetime.datetime.fromisoformat(
                    document.initial_event_time.replace("Z", "+00:00")
                )
                timestamp = doc_time
                date_partition = doc_time.strftime("%Y-%m-%d")
                logger.info(
                    f"Using document initial_event_time: {document.initial_event_time} for partitioning"
                )
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"Could not parse document.initial_event_time: {document.initial_event_time}, using current time instead. Error: {str(e)}"
                )
                timestamp = datetime.datetime.now()
                date_partition = timestamp.strftime("%Y-%m-%d")
        else:
            logger.warning(
                "Document initial_event_time not available, using current time instead"
            )
            timestamp = datetime.datetime.now()
            date_partition = timestamp.strftime("%Y-%m-%d")

        # Escape document ID by replacing slashes with underscores
        document_id = document.id or document.input_key or "unknown"
        escaped_doc_id = re.sub(r"[/\\]", "_", document_id)

        # Create timestamp string for unique filenames (to avoid overwrites if same doc processed multiple times)
        timestamp_str = timestamp.strftime("%Y%m%d_%H%M%S_%f")[
            :-3
        ]  # Include milliseconds

        # Process metering data
        metering_records = []

        for key, metrics in document.metering.items():
            # Split the key into context and service_api
            parts = key.split("/", 1)
            if len(parts) == 2:
                context, service_api = parts
            else:
                context = ""
                service_api = key

            # Process each unit and value
            for unit, value in metrics.items():
                # Convert value to float if possible
                try:
                    float_value = float(value)
                except (ValueError, TypeError):
                    # If conversion fails, use 1.0 as default
                    float_value = 1.0
                    logger.warning(
                        f"Could not convert metering value to float: {value}, using 1.0 instead"
                    )

                # Get the number of pages from the document
                num_pages = document.num_pages if document.num_pages is not None else 0

                # Calculate unit cost and estimated cost using pricing from configuration
                unit_cost = self._get_unit_cost(service_api, unit)
                estimated_cost = float_value * unit_cost

                metering_record = {
                    "document_id": document_id,
                    "context": context,
                    "service_api": service_api,
                    "unit": unit,
                    "value": float_value,
                    "number_of_pages": num_pages,
                    "unit_cost": unit_cost,
                    "estimated_cost": estimated_cost,
                    "timestamp": timestamp,
                    "config_version": document.config_version or "default",
                }
                metering_records.append(metering_record)

        # Save metering data in Parquet format
        if metering_records:
            metering_key = f"metering/date={date_partition}/{escaped_doc_id}_{timestamp_str}_results.parquet"
            self._save_records_as_parquet(
                metering_records, metering_key, metering_schema
            )
            logger.info(f"Saved {len(metering_records)} metering records")
        else:
            logger.warning("No metering records to save")

        return {
            "statusCode": 200,
            "body": "Successfully saved metering data to reporting bucket",
        }

    def save_document_sections(self, document: Document) -> Optional[Dict[str, Any]]:
        """
        Save document sections data to the reporting bucket.

        This method processes each section in the document, loads the extraction
        results from S3, and saves them as Parquet files with dynamic schema
        inference and the specified partition structure.

        Args:
            document: Document object containing sections with extraction results

        Returns:
            Dict with status and message, or None if no sections to process
        """
        if not document.sections:
            warning_msg = f"No sections to save for document {document.id}"
            logger.warning(warning_msg)
            return None

        # Use document.initial_event_time if available, otherwise use current time
        if document.initial_event_time:
            try:
                # Try to parse the initial_event_time string into a datetime object
                doc_time = datetime.datetime.fromisoformat(
                    document.initial_event_time.replace("Z", "+00:00")
                )
                timestamp = doc_time
                date_partition = doc_time.strftime("%Y-%m-%d")
                logger.info(
                    f"Using document initial_event_time: {document.initial_event_time} for partitioning"
                )
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"Could not parse document.initial_event_time: {document.initial_event_time}, using current time instead. Error: {str(e)}"
                )
                current_time = datetime.datetime.now()
                timestamp = current_time
                date_partition = current_time.strftime("%Y-%m-%d")
        else:
            logger.warning(
                "Document initial_event_time not available, using current time instead"
            )
            current_time = datetime.datetime.now()
            timestamp = current_time
            date_partition = current_time.strftime("%Y-%m-%d")

        # Escape document ID by replacing slashes with underscores
        document_id = document.id or document.input_key or "unknown"
        escaped_doc_id = re.sub(r"[/\\]", "_", document_id)

        sections_processed = 0
        sections_with_errors = 0
        total_records_saved = 0
        section_types_processed = set()  # Track unique section types

        logger.info(
            f"Processing {len(document.sections)} sections for document {document_id}"
        )

        for section in document.sections:
            try:
                # Skip sections without extraction results
                if not section.extraction_result_uri:
                    logger.warning(
                        f"Section {section.section_id} has no extraction_result_uri, skipping"
                    )
                    continue

                # Skip sections whose class is marked as excluded from
                # processing. Their result.json is a small "skipped" stub and
                # has no extractable attributes to report on — including it
                # would pollute the section parquet schema.
                from idp_common.section_exclusion import is_section_excluded

                if is_section_excluded(section):
                    logger.info(
                        "Reporting skipped for excluded section %s "
                        "(class=%s, reason=%s)",
                        section.section_id,
                        section.classification,
                        section.exclusion_reason or "excluded",
                    )
                    continue

                logger.info(
                    f"Processing section {section.section_id} with classification '{section.classification}'"
                )

                # Load extraction results from S3
                try:
                    extraction_data = get_json_content(section.extraction_result_uri)
                    if not extraction_data:
                        logger.warning(
                            f"Empty extraction results for section {section.section_id}, skipping"
                        )
                        continue
                except Exception as e:
                    logger.error(
                        f"Error loading extraction results from {section.extraction_result_uri}: {str(e)}"
                    )
                    sections_with_errors += 1
                    continue

                # Prepare records for this section
                section_records = []

                # Handle different data structures
                if isinstance(extraction_data, dict):
                    # Flatten the JSON data
                    flattened_data = self._flatten_json_data(extraction_data)

                    # Add section metadata
                    flattened_data["section_id"] = section.section_id
                    flattened_data["document_id"] = document_id
                    flattened_data["section_classification"] = section.classification
                    flattened_data["section_confidence"] = section.confidence
                    flattened_data["timestamp"] = timestamp
                    flattened_data["config_version"] = (
                        document.config_version or "default"
                    )

                    section_records.append(flattened_data)

                elif isinstance(extraction_data, list):
                    # Handle list of records
                    for i, item in enumerate(extraction_data):
                        if isinstance(item, dict):
                            flattened_item = self._flatten_json_data(item)
                        else:
                            flattened_item = {"value": str(item)}

                        # Add section metadata and record index
                        flattened_item["section_id"] = section.section_id
                        flattened_item["document_id"] = document_id
                        flattened_item["section_classification"] = (
                            section.classification
                        )
                        flattened_item["section_confidence"] = section.confidence
                        flattened_item["record_index"] = i
                        flattened_item["config_version"] = (
                            document.config_version or "default"
                        )

                        section_records.append(flattened_item)
                else:
                    # Handle primitive types
                    record = {
                        "section_id": section.section_id,
                        "document_id": document_id,
                        "section_classification": section.classification,
                        "section_confidence": section.confidence,
                        "value": str(extraction_data),
                        "config_version": document.config_version or "default",
                    }
                    section_records.append(record)

                if not section_records:
                    logger.warning(
                        f"No records to save for section {section.section_id}"
                    )
                    continue

                # Create dynamic schema for this section's data
                schema = self._create_dynamic_schema(section_records)

                # Sanitize all records to ensure robust type compatibility
                section_records = self._sanitize_records_for_schema(
                    section_records, schema
                )

                # Create S3 key with separate tables for each section type
                # document_sections/{section_type}/date={date}/{escaped_doc_id}_section_{section_id}.parquet
                section_type = (
                    section.classification if section.classification else "unknown"
                )
                # Escape section_type to make it filesystem-safe and lowercase for consistency
                section_type_prefix = re.sub(
                    r"[/\\:*?\"<>|]", "_", section_type.lower()
                )

                s3_key = (
                    f"document_sections/"
                    f"{section_type_prefix}/"
                    f"date={date_partition}/"
                    f"{escaped_doc_id}_section_{section.section_id}.parquet"
                )

                # Save the section data as Parquet
                self._save_records_as_parquet(section_records, s3_key, schema)

                sections_processed += 1
                total_records_saved += len(section_records)

                logger.info(
                    f"Saved {len(section_records)} records for section {section.section_id} "
                    f"to s3://{self.reporting_bucket}/{s3_key}"
                )

                # Track this section type and create/update Glue table if needed
                if section_type not in section_types_processed:
                    section_types_processed.add(section_type)
                    # Try to create or update the Glue table for this section type
                    table_created = self._create_or_update_glue_table(
                        section_type, schema
                    )
                    if table_created:
                        logger.info(
                            f"Created/updated Glue table for section type: {section_type}"
                        )

            except Exception as e:
                logger.error(f"Error processing section {section.section_id}: {str(e)}")
                sections_with_errors += 1
                continue

        # Log summary
        logger.info(
            f"Document sections processing complete for {document_id}: "
            f"{sections_processed} sections processed successfully, "
            f"{sections_with_errors} sections had errors, "
            f"{total_records_saved} total records saved"
        )

        if sections_processed == 0:
            return {
                "statusCode": 200,
                "body": f"No sections with extraction results found for document {document_id}",
            }

        return {
            "statusCode": 200,
            "body": f"Successfully saved {sections_processed} document sections "
            f"with {total_records_saved} total records to reporting bucket",
        }

    def _create_or_update_rule_validation_glue_table(
        self, table_name: str, schema: pa.Schema
    ) -> bool:
        """
        Create or update a Glue table for rule validation data.

        Args:
            table_name: Table name (e.g., 'rule_validation_summary')
            schema: PyArrow schema for the table

        Returns:
            True if table was created or updated, False otherwise
        """
        if not self.glue_client or not self.database_name:
            logger.warning(
                f"Glue client or database name not configured, skipping table creation for {table_name}"
            )
            return False

        columns = self._convert_schema_to_glue_columns(schema)

        table_input = {
            "Name": table_name,
            "Description": f"Rule validation data: {table_name}",
            "StorageDescriptor": {
                "Columns": columns,
                "Location": f"s3://{self.reporting_bucket}/{table_name}/",
                "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                "Compressed": True,
                "SerdeInfo": {
                    "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                },
            },
            "PartitionKeys": [{"Name": "date", "Type": "string"}],
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {
                "projection.enabled": "true",
                "projection.date.type": "date",
                "projection.date.format": "yyyy-MM-dd",
                "projection.date.range": "2020-01-01,NOW",
                "projection.date.interval": "1",
                "projection.date.interval.unit": "DAYS",
                "storage.location.template": f"s3://{self.reporting_bucket}/{table_name}/date=${{date}}/",
            },
        }

        try:
            existing_table_response = self.glue_client.get_table(
                DatabaseName=self.database_name, Name=table_name
            )
            existing_columns = existing_table_response["Table"]["StorageDescriptor"][
                "Columns"
            ]
            existing_column_names = {col["Name"] for col in existing_columns}
            new_column_names = {col["Name"] for col in columns}

            if not new_column_names.issubset(existing_column_names):
                self.glue_client.update_table(
                    DatabaseName=self.database_name, TableInput=table_input
                )
                logger.info(f"Updated Glue table {table_name}")
                return True
            return True

        except Exception as e:
            if "EntityNotFoundException" in str(e):
                try:
                    self.glue_client.create_table(
                        DatabaseName=self.database_name, TableInput=table_input
                    )
                    logger.info(f"Created Glue table {table_name}")
                    return True
                except Exception as create_error:
                    if "AlreadyExistsException" not in str(create_error):
                        logger.error(
                            f"Error creating table {table_name}: {str(create_error)}"
                        )
                    return False
            else:
                logger.error(
                    f"Unexpected error checking/updating table {table_name}: {str(e)}"
                )
            return False

    def save_rule_validation_results(
        self, document: Document
    ) -> Optional[Dict[str, Any]]:
        """
        Save rule validation results for a document to the reporting bucket.

        Args:
            document: Document object containing rule validation result URI

        Returns:
            Dict with status and message, or None if no rule validation results
        """
        if (
            not hasattr(document, "rule_validation_result")
            or not document.rule_validation_result
        ):
            logger.warning(
                f"No rule_validation_result available for document {document.id}"
            )
            return None

        # Get the JSON summary file (not the markdown file)
        if hasattr(document.rule_validation_result, "summary") and hasattr(
            document.rule_validation_result.summary, "consolidated_summary_uri"
        ):
            # Replace .md with .json to get the JSON summary
            json_uri = document.rule_validation_result.summary.consolidated_summary_uri.replace(
                ".md", ".json"
            )
        elif document.rule_validation_result.output_uri:
            # Fallback to output_uri, replace .md with .json if needed
            json_uri = document.rule_validation_result.output_uri.replace(
                ".md", ".json"
            )
        else:
            logger.warning(
                f"No output_uri in rule_validation_result for document {document.id}"
            )
            return None

        try:
            logger.info(f"Loading rule validation results from {json_uri}")
            rule_validation_data = get_json_content(json_uri)

            if not rule_validation_data:
                logger.warning(
                    f"Empty rule validation results for document {document.id}"
                )
                return None

        except Exception as e:
            error_msg = (
                f"Error loading rule validation results from {json_uri}: {str(e)}"
            )
            logger.error(error_msg)
            return {"statusCode": 500, "body": error_msg}

        # Define schemas
        document_summary_schema = pa.schema(
            [
                ("document_id", pa.string()),
                ("input_key", pa.string()),
                ("validation_date", pa.timestamp("ms")),
                ("overall_status", pa.string()),
                ("total_policy_types", pa.int32()),
                ("total_rules", pa.int32()),
                ("pass_count", pa.int32()),
                ("fail_count", pa.int32()),
                ("information_not_found_count", pa.int32()),
            ]
        )

        rule_details_schema = pa.schema(
            [
                ("document_id", pa.string()),
                ("policy_type", pa.string()),
                ("rule", pa.string()),
                ("recommendation", pa.string()),
                ("reasoning", pa.string()),
                ("supporting_pages", pa.string()),
                ("validation_date", pa.timestamp("ms")),
            ]
        )

        # Get timestamp
        if document.initial_event_time:
            try:
                doc_time = datetime.datetime.fromisoformat(
                    document.initial_event_time.replace("Z", "+00:00")
                )
                validation_date = doc_time
                date_partition = doc_time.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                validation_date = datetime.datetime.now()
                date_partition = validation_date.strftime("%Y-%m-%d")
        else:
            validation_date = datetime.datetime.now()
            date_partition = validation_date.strftime("%Y-%m-%d")

        document_id = rule_validation_data.get("document_id", document.id)
        escaped_doc_id = re.sub(r"[/\\]", "_", document_id)

        # Prepare document summary
        overall_stats = rule_validation_data.get("overall_statistics", {})
        recommendation_counts = overall_stats.get("recommendation_counts", {})

        document_record = {
            "document_id": document_id,
            "input_key": document.input_key,
            "validation_date": validation_date,
            "overall_status": rule_validation_data.get("overall_status", "UNKNOWN"),
            "total_policy_types": rule_validation_data.get("total_policy_types", 0),
            "total_rules": overall_stats.get("total_rules", 0),
            "pass_count": recommendation_counts.get("Pass", 0),
            "fail_count": recommendation_counts.get("Fail", 0),
            "information_not_found_count": recommendation_counts.get(
                "Information Not Found", 0
            ),
        }

        # Save document summary
        doc_summary_key = f"rule_validation_summary/date={date_partition}/{escaped_doc_id}_summary.parquet"
        self._save_records_as_parquet(
            [document_record], doc_summary_key, document_summary_schema
        )
        logger.info(
            f"Saved document summary to s3://{self.reporting_bucket}/{doc_summary_key}"
        )
        self._create_or_update_rule_validation_glue_table(
            "rule_validation_summary", document_summary_schema
        )

        # Prepare rule details
        rule_records = []
        rule_details = rule_validation_data.get("rule_details", {})

        for policy_type, rule_stats in rule_details.items():
            rules_list = rule_stats.get("rules", [])
            for rule_detail in rules_list:
                rule_records.append(
                    {
                        "document_id": document_id,
                        "policy_type": policy_type,
                        "rule": rule_detail.get("rule", "Unknown"),
                        "recommendation": rule_detail.get("recommendation", "Unknown"),
                        "reasoning": rule_detail.get("reasoning", ""),
                        "supporting_pages": json.dumps(
                            rule_detail.get("supporting_pages", [])
                        ),
                        "validation_date": validation_date,
                    }
                )

        if rule_records:
            rule_details_key = f"rule_validation_details/date={date_partition}/{escaped_doc_id}_details.parquet"
            self._save_records_as_parquet(
                rule_records, rule_details_key, rule_details_schema
            )
            logger.info(
                f"Saved {len(rule_records)} rule details to s3://{self.reporting_bucket}/{rule_details_key}"
            )
            self._create_or_update_rule_validation_glue_table(
                "rule_validation_details", rule_details_schema
            )

        return {
            "statusCode": 200,
            "body": f"Successfully saved rule validation results: 1 summary + {len(rule_records)} rule details",
        }
