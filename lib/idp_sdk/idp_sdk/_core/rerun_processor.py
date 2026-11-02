# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Rerun Processor Module

Handles document reprocessing for specific pipeline steps.
"""

import json
import logging
import os
from typing import Dict, List

import boto3

logger = logging.getLogger(__name__)

#: Statuses the malware-scanning ingest gate puts a document in, and the reason
#: reprocessing each one is refused.
#:
#: Kept as a mapping of status VALUE to message, keyed on the string rather than
#: on the ``Status`` enum, because this module imports ``idp_common`` lazily
#: through a path shim and must not fail at import time on a machine where that
#: package is absent. ``scripts/tests/test_reprocess_paths_agree.py`` checks this
#: set against the resolver's ``_REPROCESS_REFUSED_STATUSES`` so the two doors
#: cannot drift apart.
_MALWARE_GATE_REFUSALS = {
    "MALWARE_SCANNING": (
        "the malware scan is still in progress, so nothing has decided whether "
        "this file is safe. Reprocessing now would start the workflow "
        "immediately. Retry once the verdict arrives."
    ),
    "QUARANTINED": (
        "Amazon GuardDuty found malware in this document and it was refused at "
        "ingest. Reprocessing it would push known malware into OCR and Bedrock. "
        "Delete it instead."
    ),
    "MALWARE_SCAN_FAILED": (
        "this document could not be malware-scanned and was refused at ingest. "
        "Reprocessing it would process a file nothing has cleared. If it is a "
        "legitimate document GuardDuty could not read, such as a "
        "password-protected PDF, re-upload an unprotected copy."
    ),
}


def _malware_gate_refusal(document):
    """The reason this document must not be reprocessed, or None.

    Reads the status defensively — ``document.status`` may be an enum member or
    the bare string depending on how the row was loaded — because getting this
    wrong in the permissive direction reopens the gate.
    """
    status = getattr(document, "status", None)
    status = getattr(status, "value", status)
    if status is None:
        return None
    return _MALWARE_GATE_REFUSALS.get(str(status).upper())


def _get_idp_common_path() -> str:
    """
    Get path to idp_common package dynamically

    Returns:
        Absolute path to idp_common_pkg directory

    Raises:
        RuntimeError: If idp_common_pkg cannot be found
    """
    # Get the directory containing this file (idp_cli/idp_cli/)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate up to project root (../../)
    project_root = os.path.dirname(os.path.dirname(current_dir))
    # Construct path to idp_common_pkg
    idp_common_path = os.path.join(project_root, "lib", "idp_common_pkg")

    if not os.path.exists(idp_common_path):
        raise RuntimeError(
            f"idp_common_pkg not found at expected location: {idp_common_path}"
        )

    return idp_common_path


class RerunProcessor:
    """Handles reprocessing of documents for specific pipeline steps"""

    def __init__(self, stack_name: str, region: str = None):
        """
        Initialize rerun processor

        Args:
            stack_name: CloudFormation stack name
            region: AWS region (optional)
        """
        self.stack_name = stack_name

        # Auto-detect region if not provided
        if not region:
            session = boto3.session.Session()
            region = session.region_name
            if not region:
                raise ValueError(
                    "Region could not be determined. Please specify --region or configure AWS_DEFAULT_REGION"
                )

        self.region = region
        self.lambda_client = boto3.client("lambda", region_name=region)
        self.sqs_client = boto3.client("sqs", region_name=region)

        # Import here to avoid circular dependency
        from idp_sdk._core.stack_info import StackInfo

        # Get stack resources
        stack_info = StackInfo(stack_name, region)
        if not stack_info.validate_stack():
            raise ValueError(
                f"Stack '{stack_name}' is not in a valid state for operations"
            )

        self.resources = stack_info.get_resources()

        # Also get TrackingTable and AppSync API from CloudFormation (not in outputs)
        self._add_tracking_table()
        self._add_appsync_api()

        logger.info(f"Initialized rerun processor for stack: {stack_name}")

    def _add_tracking_table(self):
        """Add TrackingTable to resources by querying CloudFormation"""
        try:
            cfn = boto3.client("cloudformation", region_name=self.region)
            response = cfn.describe_stack_resource(
                StackName=self.stack_name, LogicalResourceId="TrackingTable"
            )
            physical_id = response["StackResourceDetail"]["PhysicalResourceId"]
            self.resources["TrackingTable"] = physical_id
            logger.info(f"Added TrackingTable to resources: {physical_id}")
        except Exception as e:
            logger.warning(f"Could not retrieve TrackingTable: {e}")

    def _add_appsync_api(self):
        """Add AppSync API URL to resources by parsing CloudFormation outputs"""
        try:
            cfn = boto3.client("cloudformation", region_name=self.region)
            response = cfn.describe_stacks(StackName=self.stack_name)
            stack = response["Stacks"][0]

            # AppSync URL is in WebUITestEnvFile output
            for output in stack.get("Outputs", []):
                if output.get("OutputKey") == "WebUITestEnvFile":
                    env_file = output.get("OutputValue", "")
                    # Parse the env file for REACT_APP_APPSYNC_GRAPHQL_URL
                    for line in env_file.split("\n"):
                        if line.startswith("REACT_APP_APPSYNC_GRAPHQL_URL="):
                            appsync_url = line.split("=", 1)[1].strip()
                            self.resources["AppSyncApiUrl"] = appsync_url
                            logger.info(f"Added AppSync API URL: {appsync_url}")
                            return

            logger.warning("AppSync API URL not found in WebUITestEnvFile output")

        except Exception as e:
            logger.warning(f"Could not retrieve AppSync API: {e}")

    def rerun_documents(
        self, document_ids: List[str], step: str, monitor: bool = False
    ) -> Dict:
        """
        Rerun processing for specific documents from a specific step

        Args:
            document_ids: List of document object keys to reprocess
            step: Pipeline step to rerun from ('classification' or 'extraction')
            monitor: Whether to monitor progress

        Returns:
            Dictionary with rerun results
        """
        logger.info(f"Rerunning {len(document_ids)} documents from step: {step}")

        results = {
            "documents_queued": 0,
            "documents_failed": 0,
            "failed_documents": [],
            "step": step,
        }

        for object_key in document_ids:
            try:
                # Get full document
                document = self._get_document(object_key)

                if not document:
                    logger.error(f"Document not found: {object_key}")
                    results["documents_failed"] += 1
                    results["failed_documents"].append(
                        {"object_key": object_key, "error": "Document not found"}
                    )
                    continue

                # ⚠️ The malware gate's second door. This function is the single
                # chokepoint for `idp-cli reprocess`, `documents.reprocess()` and
                # `batches.reprocess()`, and it does NOT go through the reprocess
                # API resolver where the equivalent guard lives — it reads the row
                # and sends to SQS itself, client-side. Without this check the
                # whole gate is bypassable from the CLI, and `--batch-id` makes it
                # worse by re-queueing every document in a batch unattended, so a
                # quarantined file rides along with its clean siblings.
                #
                # Skipped-and-reported rather than raised: this is a loop over
                # many documents and one refused file must not abandon the rest.
                refusal = _malware_gate_refusal(document)
                if refusal:
                    logger.error(f"Refusing to reprocess {object_key}: {refusal}")
                    results["documents_failed"] += 1
                    results["failed_documents"].append(
                        {"object_key": object_key, "error": refusal}
                    )
                    continue

                # Prepare document based on step
                if step == "classification":
                    document = self._prepare_for_classification_rerun(document)
                elif step == "extraction":
                    document = self._prepare_for_extraction_rerun(document)
                else:
                    raise ValueError(f"Invalid step: {step}")

                # Update document status in database before sending to queue
                # This ensures the monitor can see the QUEUED status immediately
                self._update_document_status(document)

                # Send to queue
                self._send_to_queue(document)

                results["documents_queued"] += 1
                logger.info(f"Queued {object_key} for {step} reprocessing")

            except Exception as e:
                logger.error(f"Failed to rerun {object_key}: {e}")
                results["documents_failed"] += 1
                results["failed_documents"].append(
                    {"object_key": object_key, "error": str(e)}
                )

        logger.info(
            f"Rerun complete: {results['documents_queued']} queued, {results['documents_failed']} failed"
        )
        return results

    def _get_document(self, object_key: str):
        """
        Get full document from DynamoDB as Document object

        Args:
            object_key: Document object key

        Returns:
            Document object or None if not found
        """
        # Import idp_common here to get document
        try:
            # Use DynamoDB service directly to get document
            import sys

            sys.path.insert(0, _get_idp_common_path())
            from idp_common.docs_service import create_document_service

            # Set environment variables for DynamoDB service
            tracking_table = self.resources.get("TrackingTable")
            if not tracking_table:
                raise ValueError("TrackingTable not found in stack resources")

            # Temporarily set environment variables
            original_tracking = os.environ.get("TRACKING_TABLE")
            original_region = os.environ.get("AWS_REGION")

            os.environ["TRACKING_TABLE"] = tracking_table
            if self.region:
                os.environ["AWS_REGION"] = self.region

            try:
                doc_service = create_document_service(mode="dynamodb")
                document = doc_service.get_document(object_key)
            finally:
                # Restore original environment variables
                if original_tracking:
                    os.environ["TRACKING_TABLE"] = original_tracking
                else:
                    os.environ.pop("TRACKING_TABLE", None)

                if original_region:
                    os.environ["AWS_REGION"] = original_region
                elif self.region:
                    os.environ.pop("AWS_REGION", None)

            if not document:
                return None

            # Ensure bucket names are set
            document.input_bucket = self.resources.get("InputBucket")
            document.output_bucket = self.resources.get("OutputBucket")

            logger.info(
                f"Retrieved document {object_key}: {len(document.pages)} pages, {len(document.sections)} sections"
            )

            return document

        except Exception as e:
            logger.error(f"Error retrieving document {object_key}: {e}")
            return None

    def _prepare_for_classification_rerun(self, document):
        """
        Prepare document for classification rerun

        - Clears all page classifications
        - Deletes all sections (including S3 data)
        - Keeps OCR data intact

        Args:
            document: Document object

        Returns:
            Modified Document object
        """
        logger.info(
            f"Preparing {document.id} for classification rerun: clearing {len(document.pages)} page classifications and {len(document.sections)} sections"
        )

        # Delete section extraction data from S3 before clearing sections
        s3_client = boto3.client("s3", region_name=self.region)
        for section in document.sections:
            if (
                section.extraction_result_uri
                and section.extraction_result_uri.startswith("s3://")
            ):
                try:
                    # Parse S3 URI and delete object
                    parts = section.extraction_result_uri.replace("s3://", "").split(
                        "/", 1
                    )
                    if len(parts) == 2:
                        bucket, key = parts
                        s3_client.delete_object(Bucket=bucket, Key=key)
                        logger.debug(
                            f"Deleted section extraction data: {section.extraction_result_uri}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to delete {section.extraction_result_uri}: {e}"
                    )

        # Clear page classifications
        for page in document.pages.values():
            page.classification = ""
            logger.debug(f"Cleared classification for page {page.page_id}")

        # Create placeholder section (AppSync doesn't update empty arrays)
        # Using empty section_id so UI can filter/ignore it
        from idp_common.models import Section

        document.sections = [
            Section(
                section_id="1",
                classification="-",
                confidence=0.0,
                page_ids=[],
                extraction_result_uri=None,
                attributes=None,
                confidence_threshold_alerts=[],
            )
        ]
        logger.info("Set placeholder section (section_id='') for AppSync update")

        # Update status
        from idp_common.models import Status

        document.status = Status.QUEUED
        document.start_time = None
        document.completion_time = None
        document.workflow_execution_arn = None

        # Clear any previous errors
        document.errors = []

        return document

    def _prepare_for_extraction_rerun(self, document):
        """
        Prepare document for extraction rerun

        - Keeps page classifications and sections intact
        - Clears extraction results from sections
        - Keeps OCR and classification data intact

        Args:
            document: Document object

        Returns:
            Modified Document object
        """
        logger.info(
            f"Preparing {document.id} for extraction rerun: clearing extraction data from {len(document.sections)} sections"
        )

        # Clear extraction data from sections
        for section in document.sections:
            section.extraction_result_uri = None
            section.attributes = None
            section.confidence_threshold_alerts = []
            section.missing_required_fields = []
            logger.debug(f"Cleared extraction data for section {section.section_id}")

        # Update status
        from idp_common.models import Status

        document.status = Status.QUEUED
        document.start_time = None
        document.completion_time = None
        document.workflow_execution_arn = None

        # Clear any previous errors
        document.errors = []

        return document

    def _update_document_status(self, document) -> None:
        """
        Update document via AppSync (auto-syncs to DynamoDB)

        Args:
            document: Document object with status=QUEUED and cleared sections
        """
        try:
            import sys

            sys.path.insert(0, _get_idp_common_path())
            from idp_common.docs_service import create_document_service

            # Set environment variables temporarily
            original_tracking = os.environ.get("TRACKING_TABLE")
            original_region = os.environ.get("AWS_REGION")
            original_appsync = os.environ.get("APPSYNC_API_URL")

            tracking_table = self.resources.get("TrackingTable")
            appsync_url = self.resources.get("AppSyncApiUrl")

            os.environ["TRACKING_TABLE"] = tracking_table
            os.environ["AWS_REGION"] = self.region
            if appsync_url:
                os.environ["APPSYNC_API_URL"] = appsync_url

            try:
                # Update via AppSync (resolver automatically updates DynamoDB)
                if appsync_url:
                    appsync_service = create_document_service(mode="appsync")
                    appsync_service.update_document(document)
                    logger.info(
                        f"Updated document {document.id} via AppSync (cleared {len(document.sections)} sections)"
                    )
                else:
                    # Fallback to direct DynamoDB update if AppSync not available
                    dynamodb_service = create_document_service(mode="dynamodb")
                    dynamodb_service.update_document(document)
                    logger.info(
                        f"Updated document {document.id} in DynamoDB directly (cleared {len(document.sections)} sections)"
                    )
            finally:
                # Restore environment
                if original_tracking:
                    os.environ["TRACKING_TABLE"] = original_tracking
                else:
                    os.environ.pop("TRACKING_TABLE", None)

                if original_region:
                    os.environ["AWS_REGION"] = original_region
                else:
                    os.environ.pop("AWS_REGION", None)

                if original_appsync:
                    os.environ["APPSYNC_API_URL"] = original_appsync
                else:
                    os.environ.pop("APPSYNC_API_URL", None)

        except Exception as e:
            # Don't fail the whole operation if status update fails
            logger.warning(f"Failed to update document status: {str(e)}")

    def _send_to_queue(self, document) -> None:
        """
        Send document to SQS queue for processing

        Args:
            document: Document object to send
        """
        # Note: stack outputs use DocumentQueueUrl, not DocumentQueue
        queue_url = self.resources.get("DocumentQueueUrl")

        if not queue_url:
            # Debug: show what resources we have
            logger.error(f"Available resources: {list(self.resources.keys())}")
            raise ValueError(
                f"DocumentQueueUrl not found in stack resources. Available: {list(self.resources.keys())}"
            )

        # Convert Document object to dict for SQS
        message_body = json.dumps(document.to_dict(), default=str)

        # Send to SQS
        response = self.sqs_client.send_message(
            QueueUrl=queue_url, MessageBody=message_body
        )

        logger.info(
            f"Sent document {document.id} to queue. MessageId: {response.get('MessageId')}"
        )

    def get_batch_document_ids(self, batch_id: str) -> List[str]:
        """
        Get all document IDs from a batch

        Args:
            batch_id: Batch identifier

        Returns:
            List of document object keys
        """
        from idp_sdk._core.batch_processor import BatchProcessor

        processor = BatchProcessor(stack_name=self.stack_name, region=self.region)
        batch_info = processor.get_batch_info(batch_id)

        if not batch_info:
            raise ValueError(f"Batch not found: {batch_id}")

        return batch_info.get("document_ids", [])
