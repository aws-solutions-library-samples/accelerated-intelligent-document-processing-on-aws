# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document operations for IDP SDK."""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

import boto3
from botocore.exceptions import ClientError

from idp_sdk._core.ids import generate_timestamped_id
from idp_sdk.exceptions import (
    IDPConfigurationError,
    IDPError,
    IDPProcessingError,
    IDPResourceNotFoundError,
)
from idp_sdk.models import (
    DocumentBucket,
    DocumentDeletionResult,
    DocumentDownloadResult,
    DocumentInfo,
    DocumentListResult,
    DocumentMetadata,
    DocumentReprocessResult,
    DocumentStatus,
    DocumentUploadResult,
    ReprocessOption,
    RerunStep,
    ReviewInfo,
)


def _review_store(resources, region):
    """The review queue store for deletes, or None when the stack has none."""
    table = resources.get("HITLTable")
    if not table:
        return None
    from idp_common.review_queues.store import ReviewStore

    return ReviewStore(table, region=region)


class DocumentOperation:
    """Single document operations."""

    def __init__(self, client):
        self._client = client

    def process(
        self,
        file_path: str,
        document_id: Optional[str] = None,
        stack_name: Optional[str] = None,
        *,
        attributes: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> DocumentUploadResult:
        """Process a single document (upload and queue for processing).

        Args:
            file_path: Path to local file to upload
            document_id: Optional custom document ID (defaults to filename without extension)
            stack_name: Optional stack name override
            attributes: Caller-supplied document attributes, stored with the
                document and returned with its results (see
                ``docs/document-attributes.md``). Invalid attributes raise
                ``IDPConfigurationError`` before anything is uploaded.
            **kwargs: Additional parameters

        Returns:
            DocumentUploadResult with document_id and status
        """
        name = self._client._require_stack(stack_name)
        resources = self._client._get_stack_resources(name)

        if not os.path.isfile(file_path):
            raise IDPProcessingError(f"File not found: {file_path}")

        if attributes:
            from idp_sdk._core.document_attributes import validate

            try:
                attributes = validate(attributes)
            except ValueError as e:
                raise IDPConfigurationError(str(e)) from e

        # Generate document ID if not provided
        filename = os.path.basename(file_path)
        if not document_id:
            document_id = os.path.splitext(filename)[0]

        # Generate unique batch prefix for single document
        batch_id = generate_timestamped_id("single-doc")
        s3_key = f"{batch_id}/{filename}"

        try:
            # Upload to InputBucket
            s3_client = boto3.client("s3", region_name=self._client._region)
            input_bucket = resources["InputBucket"]
            if attributes:
                from idp_sdk._core.document_attributes import prepare_upload

                metadata = prepare_upload(s3_client, input_bucket, s3_key, attributes)
                s3_client.upload_file(
                    Filename=file_path,
                    Bucket=input_bucket,
                    Key=s3_key,
                    ExtraArgs={"Metadata": metadata},
                )
            else:
                s3_client.upload_file(
                    Filename=file_path, Bucket=input_bucket, Key=s3_key
                )

            return DocumentUploadResult(
                document_id=s3_key,
                status="queued",
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as e:
            raise IDPProcessingError(f"Failed to upload document: {e}") from e

    def get_status(
        self, document_id: str, stack_name: Optional[str] = None, **kwargs
    ) -> DocumentStatus:
        """Get single document status.

        Args:
            document_id: Document identifier (S3 key)
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            DocumentStatus with processing information
        """
        from idp_sdk._core.batch_processor import BatchProcessor
        from idp_sdk._core.progress_monitor import ProgressMonitor

        name = self._client._require_stack(stack_name)
        processor = BatchProcessor(stack_name=name, region=self._client._region)
        monitor = ProgressMonitor(
            stack_name=name, resources=processor.resources, region=self._client._region
        )

        status_data = monitor.get_batch_status([document_id])

        # Find document in status data. Every bucket the monitor fills, derived
        # from the enum rather than typed out, so a bucket added to
        # `DocumentBucket` cannot make an existing document read as not found --
        # which is what a four-name list would do to `awaiting_review` (#2022).
        for category in [bucket.value for bucket in DocumentBucket]:
            for doc in status_data.get(category, []):
                if doc.get("document_id") == document_id:
                    start_time = doc.get("start_time")
                    end_time = doc.get("end_time")
                    # Convert empty strings to None for datetime fields
                    if start_time == "":
                        start_time = None
                    if end_time == "":
                        end_time = None
                    return DocumentStatus(
                        document_id=doc.get("document_id", ""),
                        status=doc.get("status", "UNKNOWN"),
                        start_time=start_time,
                        end_time=end_time,
                        duration_seconds=doc.get("duration"),
                        num_pages=doc.get("num_pages"),
                        num_sections=doc.get("num_sections"),
                        error=doc.get("error"),
                        review=ReviewInfo.from_lookup(doc.get("review")),
                        review_status=(doc.get("review") or {}).get("status"),
                        bucket=DocumentBucket(category),
                    )

        raise IDPResourceNotFoundError(f"Document not found: {document_id}")

    def download_results(
        self,
        document_id: str,
        output_dir: str,
        file_types: Optional[List[str]] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> DocumentDownloadResult:
        """Download processing results for a single document.

        Args:
            document_id: Document identifier (S3 key)
            output_dir: Local directory to download results to
            file_types: List of file types to download (pages, sections, summary, evaluation)
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            DocumentDownloadResult with download statistics
        """
        name = self._client._require_stack(stack_name)
        resources = self._client._get_stack_resources(name)
        output_bucket = resources["OutputBucket"]

        types_list = file_types or ["pages", "sections", "summary", "evaluation"]

        # Create output directory
        os.makedirs(output_dir, exist_ok=True)

        s3_client = boto3.client("s3", region_name=self._client._region)
        files_downloaded = 0

        try:
            # List all files for this document
            paginator = s3_client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=output_bucket, Prefix=f"{document_id}/")

            for page in pages:
                for obj in page.get("Contents", []):
                    s3_key = obj["Key"]

                    # Filter by file type if specified
                    if file_types and "all" not in file_types:
                        if not any(
                            f"/{file_type}/" in s3_key
                            or s3_key.endswith(f"/{file_type}.json")
                            for file_type in types_list
                        ):
                            continue

                    # Download file
                    local_path = os.path.join(output_dir, s3_key)
                    local_dir = os.path.dirname(local_path)
                    os.makedirs(local_dir, exist_ok=True)

                    s3_client.download_file(
                        Bucket=output_bucket, Key=s3_key, Filename=local_path
                    )
                    files_downloaded += 1

            return DocumentDownloadResult(
                document_id=document_id,
                files_downloaded=files_downloaded,
                output_dir=output_dir,
            )
        except Exception as e:
            raise IDPProcessingError(f"Failed to download results: {e}") from e

    def download_source(
        self,
        document_id: str,
        output_path: str,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Download original document source file.

        Args:
            document_id: Document identifier (S3 key)
            output_path: Local file path to save document
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            Local file path where document was saved
        """
        name = self._client._require_stack(stack_name)
        resources = self._client._get_stack_resources(name)
        input_bucket = resources["InputBucket"]

        try:
            s3_client = boto3.client("s3", region_name=self._client._region)

            # Create output directory if needed
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)

            # Download file
            s3_client.download_file(
                Bucket=input_bucket, Key=document_id, Filename=output_path
            )

            return output_path
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                raise IDPResourceNotFoundError(f"Document not found: {document_id}")
            raise IDPProcessingError(f"Failed to download source: {e}") from e
        except Exception as e:
            raise IDPProcessingError(f"Failed to download source: {e}") from e

    def reprocess(
        self,
        document_id: str,
        step: Union[str, RerunStep] = RerunStep.BEGINNING,
        *,
        skip: Sequence[Union[str, RerunStep]] = (),
        version: Optional[str] = None,
        revision: Optional[int] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> DocumentReprocessResult:
        """Process a document again, starting at a step.

        The stack's reprocess resolver does the work: it checks that the
        document can start at the step, clears the outputs the run will
        produce again and queues the document.

        Args:
            document_id: Document identifier (S3 key)
            step: The step to start at: a ``RerunStep`` or its value
                (``beginning``, ``ocr``, ``classification``, ``extraction``,
                ``assessment``, ``policy_rule_validation``, ``summarization``,
                ``evaluation``, or ``resume`` for the first step with no output)
            skip: Optional steps to leave out of the run (``assessment``,
                ``policy_rule_validation``, ``summarization``, ``evaluation``)
            version: Configuration profile to process the document under
            revision: Revision of that profile
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            DocumentReprocessResult. ``queued`` is false when the document was
            refused or failed; ``status``, ``reason`` and ``message`` say why.

        Raises:
            IDPValidationError: ``step`` or ``skip`` names no such step
            ReprocessRefusedError: the server refused the request as a whole
            IDPResourceNotFoundError: the stack has no reprocess resolver output
            IDPProcessingError: the request failed
        """
        from idp_sdk._core import reprocess as server

        name = self._client._require_stack(stack_name)
        start = server.step_value(step)
        skipped = server.skip_values(skip)
        try:
            results = server.reprocess(
                name,
                self._client._region,
                [document_id],
                start,
                skipped,
                version=version,
                revision=revision,
            )
        except IDPError:
            raise
        except Exception as e:
            raise IDPProcessingError(f"Reprocess failed: {e}") from e
        if not results:
            raise IDPProcessingError(
                f"Reprocess returned no result for document {document_id}"
            )
        return results[0]

    def reprocess_options(
        self,
        document_ids: Union[str, Sequence[str]],
        *,
        version: Optional[str] = None,
        revision: Optional[int] = None,
        stack_name: Optional[str] = None,
    ) -> List[ReprocessOption]:
        """For each document, the steps a reprocess can start at.

        Reads only. The answer depends on the document's processing mode and on
        what it already has: a step whose input is missing is not available,
        and ``reason`` carries the code that says why.

        Args:
            document_ids: One document identifier or several
            version: Configuration profile the reprocess would name
            revision: Revision of that profile
            stack_name: Optional stack name override

        Returns:
            One ReprocessOption per document, in the order given

        Raises:
            ReprocessRefusedError: the server refused the request
            IDPResourceNotFoundError: the stack has no reprocess resolver output
            IDPProcessingError: the request failed
        """
        from idp_sdk._core import reprocess as server

        name = self._client._require_stack(stack_name)
        if isinstance(document_ids, str):
            document_ids = [document_ids]
        try:
            return server.reprocess_options(
                name,
                self._client._region,
                list(document_ids),
                version=version,
                revision=revision,
            )
        except IDPError:
            raise
        except Exception as e:
            raise IDPProcessingError(f"Reprocess options failed: {e}") from e

    def rerun(
        self,
        document_id: str,
        step: Union[str, RerunStep] = RerunStep.BEGINNING,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> DocumentReprocessResult:
        """Deprecated: Use reprocess() instead."""
        import warnings

        warnings.warn(
            "rerun() is deprecated, use reprocess() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.reprocess(
            document_id=document_id,
            step=step,
            stack_name=stack_name,
            **kwargs,
        )

    def delete(
        self,
        document_id: str,
        stack_name: Optional[str] = None,
        dry_run: bool = False,
        **kwargs,
    ) -> DocumentDeletionResult:
        """Delete a single document and all associated data.

        Args:
            document_id: Document identifier (S3 key)
            stack_name: Optional stack name override
            dry_run: If True, only simulate deletion without actually deleting
            **kwargs: Additional parameters

        Returns:
            DocumentDeletionResult with deletion details
        """
        import boto3

        from idp_common.delete_documents import delete_documents

        name = self._client._require_stack(stack_name)
        resources = self._client._get_stack_resources(name)

        input_bucket = resources.get("InputBucket")
        output_bucket = resources.get("OutputBucket")
        documents_table_name = resources.get("DocumentsTable")

        if not input_bucket or not output_bucket or not documents_table_name:
            raise IDPResourceNotFoundError(
                "Required resources not found: InputBucket, OutputBucket, or DocumentsTable"
            )

        dynamodb = boto3.resource("dynamodb", region_name=self._client._region)
        tracking_table = dynamodb.Table(documents_table_name)
        s3_client = boto3.client("s3", region_name=self._client._region)
        review_store = _review_store(resources, self._client._region)

        try:
            result = delete_documents(
                object_keys=[document_id],
                tracking_table=tracking_table,
                s3_client=s3_client,
                input_bucket=input_bucket,
                output_bucket=output_bucket,
                dry_run=dry_run,
                continue_on_error=False,
                review_store=review_store,
            )

            if result.get("results"):
                single_result = result["results"][0]
                return DocumentDeletionResult(
                    success=single_result.get("success", False),
                    object_key=single_result.get("object_key", document_id),
                    deleted=single_result.get("deleted", {}),
                    errors=single_result.get("errors", []),
                )

            return DocumentDeletionResult(
                success=False,
                object_key=document_id,
                deleted={},
                errors=["No deletion result returned"],
            )
        except Exception as e:
            raise IDPProcessingError(f"Document deletion failed: {e}") from e

    def list(
        self,
        limit: int = 100,
        next_token: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> DocumentListResult:
        """List documents with pagination support.

        Args:
            limit: Maximum number of documents to return (default: 100)
            next_token: Pagination token from previous request
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            DocumentListResult with documents and optional next_token
        """
        from idp_sdk._core.document_processor import DocumentProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = DocumentProcessor(stack_name=name, region=self._client._region)
            result = processor.list_documents(limit=limit, next_token=next_token)

            documents = [
                DocumentInfo(
                    document_id=doc["document_id"],
                    status=doc["status"],
                    timestamp=doc["timestamp"] or None,
                    batch_id=doc.get("batch_id"),
                )
                for doc in result["documents"]
            ]

            return DocumentListResult(
                documents=documents,
                count=result["count"],
                next_token=result.get("next_token"),
            )
        except Exception as e:
            raise IDPProcessingError(f"Failed to list documents: {e}") from e

    def get_metadata(
        self,
        document_id: str,
        section_id: int = 1,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> DocumentMetadata:
        """Get extracted metadata and fields for a document section.

        Args:
            document_id: Document identifier (S3 key)
            section_id: Section number (default: 1)
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            DocumentMetadata with extracted fields and metadata
        """
        from idp_sdk._core.document_processor import DocumentProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = DocumentProcessor(stack_name=name, region=self._client._region)
            result = processor.get_metadata(
                document_id=document_id, section_id=section_id
            )

            return DocumentMetadata(
                document_id=result["document_id"],
                section_id=result["section_id"],
                document_class=result["document_class"],
                fields=result["fields"],
                confidence=result["confidence"],
                page_count=result["page_count"],
                metadata=result["metadata"],
            )
        except FileNotFoundError as e:
            raise IDPResourceNotFoundError(str(e)) from e
        except Exception as e:
            raise IDPProcessingError(f"Failed to get metadata: {e}") from e
