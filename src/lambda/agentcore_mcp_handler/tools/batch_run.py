# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Batch run tool for processing documents"""

import logging
import base64
import boto3
import os
from typing import Any, Dict, Optional
from datetime import datetime

from idp_common import mcp_access

from .base import IDPTool

logger = logging.getLogger(__name__)

# Version marker
TOOL_VERSION = "SDK-v3"
TOOL_UPDATED = "2026-03-13T00:00:00Z"


class BatchRunTool(IDPTool):
    """Process documents through IDP pipeline"""

    def __init__(self):
        """Initialize with stack name from environment"""
        # Try AWS_STACK_NAME first, then extract from Lambda function name
        self.stack_name = os.environ.get('AWS_STACK_NAME')
        if not self.stack_name:
            lambda_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME', '')
            # Extract stack name by removing -agentcore-analytics suffix
            if lambda_name.endswith('-agentcore-analytics'):
                self.stack_name = lambda_name.replace('-agentcore-analytics', '')
            else:
                self.stack_name = lambda_name
        logger.info(f"BatchRunTool initialized with stack_name: {self.stack_name}")

    def execute(
        self,
        location: Optional[str] = None,
        content: Optional[str] = None,
        name: Optional[str] = None,
        prefix: Optional[str] = None,
        profile: Optional[str] = None,
        caller: Any = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Execute batch processing with intelligent parameter validation.

        ``profile`` names the Configuration Profile to process under; without one
        the destination decides it (a prefix mapping, else the active profile), as
        it does for a web upload.

        For a caller with a configuration scope two further rules apply. Processing
        from an S3 ``location`` is refused: the copy is made with this function's
        role, which can read every profile's documents, so it could re-home a
        document the caller may not see into a profile they can. And the profile
        the uploaded document will process under is resolved before anything is
        written and refused if it is outside the scope.
        """
        logger.info(f"=== Batch Run Tool ===")
        logger.info(f"Version: {TOOL_VERSION}")
        logger.info(f"Parameters: location={location}, content={'<base64>' if content else None}, name={name}, prefix={prefix}")

        # Validate parameters
        validation = self._validate_parameters(location, content, name)
        if not validation['valid']:
            return validation['error_response']

        scoped = caller is not None and caller.scope is not None
        if scoped and location and not content:
            return {
                'success': False,
                'error': 'location_not_permitted',
                'message': (
                    "Processing from an S3 location is not available to a caller "
                    "with a configuration scope. Send the document as base64 "
                    "content instead."
                ),
            }

        # Route to appropriate handler
        if content:
            return self._process_base64_content(content, name, prefix, profile, caller)
        else:
            return self._process_s3_location(location, prefix, profile)

    def _validate_parameters(self, location: Optional[str], content: Optional[str], name: Optional[str]) -> Dict[str, Any]:
        """Validate parameters and return validation result"""
        # Check if at least one source provided
        if not location and not content:
            return {
                'valid': False,
                'error_response': {
                    'success': False,
                    'error': 'missing_document_source',
                    'message': "Please provide either: (1) S3 location (e.g., 's3://bucket/documents/') or (2) base64-encoded document content"
                }
            }

        # Check if name provided when content provided
        if content and not name:
            return {
                'valid': False,
                'error_response': {
                    'success': False,
                    'error': 'missing_document_name',
                    'message': "When providing document content, please specify the filename with extension (e.g., 'invoice.pdf')"
                }
            }

        # Validate filename to prevent path traversal
        if name and ('..' in name or name.startswith('/')):
            return {
                'valid': False,
                'error_response': {
                    'success': False,
                    'error': 'invalid_filename',
                    'message': "Filename cannot contain '..' or start with '/'"
                }
            }

        return {'valid': True}

    def _process_s3_location(
        self, location: str, prefix: Optional[str], profile: Optional[str] = None
    ) -> Dict[str, Any]:
        """Process S3 location using IDPClient public API"""
        try:
            from idp_sdk.client import IDPClient

            client = IDPClient(stack_name=self.stack_name)
            batch_prefix = prefix or 'mcp-batch'
            result = client.batch.process(
                s3_uri=location,
                file_pattern="*.pdf",
                recursive=True,
                batch_prefix=batch_prefix,
                config_profile=profile,
            )

            return {
                'success': True,
                'batch_id': result.batch_id,
                'documents_queued': result.documents_queued,
                'summary': {
                    'queued': result.documents_queued,
                    'failed': result.documents_failed,
                },
                'message': f"Successfully queued {result.documents_queued} documents for processing"
            }
        except Exception as e:
            logger.error(f"S3 processing failed: {e}", exc_info=True)
            return {
                'success': False,
                'error': 'processing_failed',
                'message': f'Failed to queue documents for processing: {str(e)}'
            }

    def _process_base64_content(
        self,
        content: str,
        name: str,
        prefix: Optional[str],
        profile: Optional[str] = None,
        caller: Any = None,
    ) -> Dict[str, Any]:
        """Process base64 content by uploading to MCP temp S3 first, then processing via IDPClient"""
        try:
            from idp_sdk.client import IDPClient

            logger.info(f"Base64 processing for: {name}")

            # Step 1: Decode base64
            try:
                document_binary = base64.b64decode(content)
                logger.info(f"Decoded base64 content: {len(document_binary)} bytes")
            except Exception as e:
                logger.error(f"Base64 decoding failed: {e}")
                return {
                    'success': False,
                    'error': 'invalid_base64',
                    'message': 'The provided content is not valid base64'
                }

            # Step 2: Generate batch ID and temp S3 key
            batch_id = self._generate_batch_id('mcp-content')
            temp_s3_key = f"temp/{batch_id}/{name}"
            logger.info(f"Generated batch_id: {batch_id}, temp_key: {temp_s3_key}")

            # Step 2b: the profile this document will process under, checked against
            # the caller's scope before anything is written. The key is the one
            # the SDK copies the document to in the Input bucket.
            if caller is not None and caller.scope is not None:
                refusal = self._refuse_destination(
                    f"{batch_id}/{os.path.basename(name)}", profile, caller
                )
                if refusal:
                    return refusal

            # Step 3: Get MCP content bucket
            mcp_bucket = os.environ.get('MCP_SERVER_BUCKET')
            if not mcp_bucket:
                logger.error("MCP_SERVER_BUCKET environment variable not set")
                return {
                    'success': False,
                    'error': 'missing_bucket_config',
                    'message': 'MCP server bucket not configured'
                }

            logger.info(f"Uploading to MCP bucket: {mcp_bucket}")

            # Step 4: Upload to MCP temp S3
            s3_client = boto3.client('s3')
            s3_client.put_object(
                Bucket=mcp_bucket,
                Key=temp_s3_key,
                Body=document_binary
            )
            logger.info(f"Uploaded to S3: s3://{mcp_bucket}/{temp_s3_key}")

            # Step 5: Process via IDPClient public API using temp S3 URI
            client = IDPClient(stack_name=self.stack_name)
            temp_s3_uri = f"s3://{mcp_bucket}/temp/{batch_id}/"
            result = client.batch.process(
                s3_uri=temp_s3_uri,
                file_pattern="*.pdf",
                batch_prefix=batch_id,
                batch_id=batch_id,
                config_profile=profile,
            )
            logger.info(f"Batch processing completed: batch_id={result.batch_id}, queued={result.documents_queued}")

            # Step 6: Return success
            return {
                'success': True,
                'batch_id': result.batch_id,
                'documents_queued': 1,
                'document_name': os.path.basename(name),
                'message': 'Document queued for processing'
            }

        except Exception as e:
            logger.error(f"Base64 processing failed: {e}", exc_info=True)
            return {
                'success': False,
                'error': 'processing_failed',
                'message': f'Failed to queue document for processing: {str(e)}'
            }

    def _refuse_destination(
        self, object_key: str, profile: Optional[str], caller: Any
    ) -> Optional[Dict[str, Any]]:
        """Why an upload to ``object_key`` is refused for ``caller``, or ``None``.

        The same resolution ``uploadDocument`` and ingest apply, with every seam
        ``resolve_config_assignment`` takes, so the profile checked here is the
        profile ``queue_sender`` will assign. Unlike the web upload this fails
        closed when the prefix mappings cannot be read: the web upload trades that
        for availability of the UI's primary action, and an MCP caller with a scope
        can retry.
        """
        from idp_common.config.configuration_manager import ConfigurationManager
        from idp_common.config.prefix_mappings import (
            PrefixMappingStore,
            resolve_config_assignment,
        )
        from idp_common.config.tenants import (
            TenantStore,
            ingest_rejection_reason,
            is_strict_isolation,
        )

        table_name = os.environ.get('CONFIGURATION_TABLE_NAME', '')
        if not table_name:
            raise RuntimeError("CONFIGURATION_TABLE_NAME is not set")
        manager = ConfigurationManager(table_name=table_name)
        table = boto3.resource('dynamodb').Table(table_name)

        def profile_exists(name: str) -> bool:
            item = table.get_item(
                Key={"Configuration": f"Config#{name}"},
                ProjectionExpression="Configuration",
            ).get("Item")
            return bool(item)

        assignment = resolve_config_assignment(
            object_key,
            metadata_profile=profile,
            mappings=PrefixMappingStore(table).list(),
            active_profile=manager.resolve_active_version,
            published_revision=manager.resolve_published_revision,
            profile_exists=profile_exists,
            allowed_profiles=list(caller.scope),
        )
        refused = {
            'success': False,
            'error': 'profile_not_permitted',
            'message': (
                "This document would process under a Configuration Profile outside "
                "your configuration scope -- either the one you named, or the one "
                "that governs this destination. Name a profile you have access to "
                "with the 'profile' argument."
            ),
        }
        if assignment.scope_denied:
            return refused
        try:
            mcp_access.require_profile_in_scope(caller, assignment.profile)
        except PermissionError:
            return refused
        if assignment.rejected:
            return {
                'success': False,
                'error': 'destination_rejected',
                'message': assignment.reason,
            }
        if is_strict_isolation():
            reason = ingest_rejection_reason(
                object_key, assignment.profile, TenantStore(table).list()
            )
            if reason:
                return {
                    'success': False,
                    'error': 'destination_rejected',
                    'message': reason,
                }
        return None

    def _generate_batch_id(self, prefix: Optional[str]) -> str:
        """Generate batch ID with timestamp only"""
        prefix = prefix or 'mcp-batch'
        timestamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
        return f"{prefix}-{timestamp}"
