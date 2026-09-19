# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Lambda function for saving document evaluation data to the reporting bucket in Parquet format.
"""

import json
import logging
import os
import traceback
from typing import Dict, Any, List

from idp_common.config import get_config
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.models import Document
from idp_common.reporting import SaveReportingData
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Lambda handler for saving document evaluation data to the reporting bucket.

    Args:
        event: Lambda event containing document data, reporting bucket name, and data_to_save
        context: Lambda context

    Returns:
        Dict with status and message
    """
    logger.info(
        "Starting save_reporting_data process with event: "
        f"{json.dumps(sanitize_event_for_logging(event), indent=2)}"
    )

    try:
        # Extract parameters from the event
        document_dict = event.get("document")
        reporting_bucket = event.get("reporting_bucket")
        data_to_save = event.get("data_to_save", [])

        if not document_dict:
            error_msg = "No document data provided in the event"
            logger.error(error_msg)
            return {"statusCode": 400, "body": error_msg}

        if not reporting_bucket:
            error_msg = "No reporting bucket specified in the event"
            logger.error(error_msg)
            return {"statusCode": 400, "body": error_msg}

        if not data_to_save:
            warning_msg = "No data_to_save specified in the event, nothing to do"
            logger.warning(warning_msg)
            return {"statusCode": 200, "body": warning_msg}

        # Convert document dict to Document object
        document = Document.from_dict(document_dict)

        # Get the database name from event or environment variable
        # The database name is typically in the format: {stackname}-reporting-db
        database_name = event.get("database_name")
        if not database_name:
            # Try to get from environment variable if not in event
            stack_name = os.environ.get("STACK_NAME", "").lower()
            if stack_name:
                database_name = f"{stack_name}-reporting-db"
                logger.info(f"Using database name from stack name: {database_name}")
            else:
                logger.warning(
                    "No STACK_NAME environment variable found, Glue tables will not be created"
                )
        else:
            logger.info(f"Using database_name from event: {database_name}")

        # Get the configuration table name from environment variable and load config with pricing
        config_table_name = os.environ.get("CONFIGURATION_TABLE_NAME")
        config = None
        if config_table_name:
            try:
                logger.info(f"Loading configuration from table: {config_table_name}")
                
                # Extract config_version from document if available
                config_version = document_dict.get('config_version') if document_dict else None
                if config_version:
                    logger.info(f"Using document config_version: {config_version}")
                
                config = get_config(table_name=config_table_name, as_model=True, version=config_version)

                # Also load pricing separately and attach it to the config
                config_manager = ConfigurationManager(table_name=config_table_name)
                pricing_config = config_manager.get_merged_pricing()
                if pricing_config and hasattr(pricing_config, "pricing"):
                    config.pricing = pricing_config.pricing
                    logger.info(f"Loaded {len(pricing_config.pricing)} pricing entries")
                else:
                    logger.warning("No pricing configuration found")

                logger.info("Configuration and pricing loaded successfully")
            except Exception as e:
                logger.warning(
                    f"Failed to load configuration from {config_table_name}: {str(e)}"
                )
                config = None
        else:
            logger.warning("No configuration table name provided")

        # Use the SaveReportingData class to save the data
        # Pass database_name to enable automatic Glue table creation
        # Pass config dictionary to enable dynamic pricing from configuration
        reporter = SaveReportingData(reporting_bucket, database_name, config)
        results = reporter.save(document, data_to_save)

        # If no data was processed, return a warning
        if not results:
            return {
                "statusCode": 200,
                "body": "No data was processed - check data_to_save parameter",
            }

        # Return success if all operations completed
        return {
            "statusCode": 200,
            "body": "Successfully saved data to reporting bucket",
        }

    except Exception as e:
        error_msg = f"Error saving data to reporting bucket: {str(e)}"
        logger.error(error_msg)
        # Log the full stack trace for better debugging
        logger.error(f"Stack trace: {traceback.format_exc()}")

        return {"statusCode": 500, "body": error_msg}
