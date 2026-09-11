# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Batch operations for IDP SDK."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from idp_sdk._core.document_processor import _section_instances
from idp_sdk._core.naming import resolve_config_profile
from idp_sdk.exceptions import (
    IDPConfigurationError,
    IDPProcessingError,
    IDPResourceNotFoundError,
)
from idp_sdk.models import (
    BatchDeletionResult,
    BatchDownloadResult,
    BatchInfo,
    BatchListResult,
    BatchProcessResult,
    BatchReprocessResult,
    BatchStatus,
    DocumentDeletionResult,
    DocumentsAbortedResult,
    DocumentStatus,
    ExecutionsStoppedResult,
    RerunStep,
    StopWorkflowsResult,
)

logger = logging.getLogger(__name__)


class BatchOperation:
    """Batch document processing operations."""

    def __init__(self, client):
        self._client = client

    def process(
        self,
        source: Optional[str] = None,
        manifest: Optional[str] = None,
        directory: Optional[str] = None,
        s3_uri: Optional[str] = None,
        test_set: Optional[str] = None,
        stack_name: Optional[str] = None,
        batch_id: Optional[str] = None,
        batch_prefix: str = "sdk-batch",
        file_pattern: str = "*.pdf",
        recursive: bool = True,
        number_of_files: Optional[int] = None,
        config_path: Optional[str] = None,
        config_version: Optional[str] = None,
        config_revision: Optional[int] = None,
        context: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> BatchProcessResult:
        """Process multiple documents through the IDP pipeline.

        Args:
            source: Source path (auto-detects type: directory, manifest, or S3 URI)
            manifest: Path to manifest CSV file
            directory: Local directory containing documents
            s3_uri: S3 URI (s3://bucket/prefix/)
            test_set: Test set identifier
            stack_name: Optional stack name override
            batch_id: Optional custom batch ID
            batch_prefix: Prefix for batch output
            file_pattern: File pattern for directory/S3 scanning
            recursive: Recursively scan directories
            number_of_files: Limit number of files to process
            config_path: Path to custom configuration file
            config_version: Configuration Profile to use for processing
            config_profile: Configuration profile (the current name for
                config_version; either may be given, not both with different values).
            config_revision: Optional revision of that profile. Omit to process
                under the profile's current configuration.
            context: Context for test set processing
            **kwargs: Additional parameters

        Returns:
            BatchProcessResult with batch processing information
        """
        config_version = resolve_config_profile(config_profile, config_version)
        from idp_sdk._core.batch_processor import BatchProcessor

        name = self._client._require_stack(stack_name)

        if source:
            import os

            if source.startswith("s3://"):
                s3_uri = source
            elif os.path.isdir(source):
                directory = source
            elif os.path.isfile(source):
                manifest = source
            else:
                raise IDPConfigurationError(
                    f"Source '{source}' not found or unrecognized format"
                )

        sources = [manifest, directory, s3_uri, test_set]
        if sum(1 for s in sources if s) != 1:
            raise IDPConfigurationError(
                "Specify exactly one source: manifest, directory, s3_uri, or test_set"
            )

        try:
            # BatchProcessor.__init__ signature: (stack_name, config_path=None, region=None)
            # config_version is NOT a constructor arg — pass it to the processing methods instead
            processor = BatchProcessor(
                stack_name=name,
                config_path=config_path,
                region=self._client._region,
            )

            if test_set:
                result = self._process_test_set(
                    processor,
                    test_set,
                    context,
                    number_of_files,
                    config_version,
                    config_revision,
                )
            elif manifest:
                # Pass config_version to process_batch if the method supports it
                import inspect

                manifest_sig = inspect.signature(processor.process_batch)
                manifest_kwargs = dict(
                    manifest_path=manifest,
                    output_prefix=batch_prefix,
                    batch_id=batch_id,
                    number_of_files=number_of_files,
                )
                if "config_version" in manifest_sig.parameters and config_version:
                    manifest_kwargs["config_version"] = config_version
                if (
                    "config_revision" in manifest_sig.parameters
                    and config_revision is not None
                ):
                    manifest_kwargs["config_revision"] = config_revision
                result = processor.process_batch(**manifest_kwargs)
            elif directory:
                import inspect

                dir_sig = inspect.signature(processor.process_batch_from_directory)
                dir_kwargs = dict(
                    dir_path=directory,
                    file_pattern=file_pattern,
                    recursive=recursive,
                    output_prefix=batch_prefix,
                    batch_id=batch_id,
                    number_of_files=number_of_files,
                )
                if "config_version" in dir_sig.parameters and config_version:
                    dir_kwargs["config_version"] = config_version
                if (
                    "config_revision" in dir_sig.parameters
                    and config_revision is not None
                ):
                    dir_kwargs["config_revision"] = config_revision
                result = processor.process_batch_from_directory(**dir_kwargs)
            else:
                import inspect

                s3_kwargs = dict(
                    s3_uri=s3_uri,
                    file_pattern=file_pattern,
                    recursive=recursive,
                    output_prefix=batch_prefix,
                    batch_id=batch_id,
                )
                sig = inspect.signature(processor.process_batch_from_s3_uri)
                if "config_version" in sig.parameters and config_version:
                    s3_kwargs["config_version"] = config_version
                if "config_revision" in sig.parameters and config_revision is not None:
                    s3_kwargs["config_revision"] = config_revision
                result = processor.process_batch_from_s3_uri(**s3_kwargs)

            return BatchProcessResult(
                batch_id=result["batch_id"],
                document_ids=result["document_ids"],
                queued=result.get("queued", 0),
                uploaded=result.get("uploaded", 0),
                failed=result.get("failed", 0),
                baselines_uploaded=result.get("baselines_uploaded", 0),
                source=result.get("source", ""),
                output_prefix=result.get("output_prefix", batch_prefix),
                timestamp=datetime.fromisoformat(
                    result.get("timestamp", datetime.now(timezone.utc).isoformat())
                ),
            )
        except Exception as e:
            raise IDPProcessingError(f"Batch processing failed: {e}") from e

    def run(
        self,
        source: Optional[str] = None,
        manifest: Optional[str] = None,
        directory: Optional[str] = None,
        s3_uri: Optional[str] = None,
        test_set: Optional[str] = None,
        stack_name: Optional[str] = None,
        batch_id: Optional[str] = None,
        batch_prefix: str = "sdk-batch",
        file_pattern: str = "*.pdf",
        recursive: bool = True,
        number_of_files: Optional[int] = None,
        config_path: Optional[str] = None,
        config_version: Optional[str] = None,
        config_revision: Optional[int] = None,
        context: Optional[str] = None,
        *,
        config_profile: Optional[str] = None,
        **kwargs,
    ) -> BatchProcessResult:
        """Deprecated: Use process() instead."""
        config_version = resolve_config_profile(config_profile, config_version)
        import warnings

        warnings.warn(
            "run() is deprecated, use process() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.process(
            source=source,
            manifest=manifest,
            directory=directory,
            s3_uri=s3_uri,
            test_set=test_set,
            stack_name=stack_name,
            batch_id=batch_id,
            batch_prefix=batch_prefix,
            file_pattern=file_pattern,
            recursive=recursive,
            number_of_files=number_of_files,
            config_path=config_path,
            config_version=config_version,
            config_revision=config_revision,
            context=context,
            **kwargs,
        )

    def _process_test_set(
        self,
        processor,
        test_set: str,
        context: Optional[str],
        number_of_files: Optional[int],
        config_version: Optional[str] = None,
        config_revision: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Process a test set (internal helper).

        Enhancement 1:
        - Step 1: Invoke TestSetResolverFunction (non-fatal if missing)
        - Step 2: Invoke TestRunnerFunction with configVersion (and configRevision,
          when the caller pinned one) in payload
        """
        import json

        import boto3

        lambda_client = boto3.client("lambda", region_name=self._client._region)
        all_functions = []
        paginator = lambda_client.get_paginator("list_functions")
        for page in paginator.paginate():
            all_functions.extend(page["Functions"])

        stack_name = self._client._require_stack()

        # Enhancement 1 — Step 1: Invoke TestSetResolverFunction for auto-detection.
        # This is non-fatal: a missing resolver is logged as a warning and execution continues.
        test_set_resolver_function = next(
            (
                f["FunctionName"]
                for f in all_functions
                if stack_name in f["FunctionName"]
                and "TestSetResolverFunction" in f["FunctionName"]
            ),
            None,
        )

        if test_set_resolver_function:
            try:
                resolver_payload = {
                    "info": {"fieldName": "getTestSets"},
                    "arguments": {},
                }
                lambda_client.invoke(
                    FunctionName=test_set_resolver_function,
                    Payload=json.dumps(resolver_payload),
                )
                logger.debug(
                    "TestSetResolverFunction invoked successfully: %s",
                    test_set_resolver_function,
                )
            except Exception as exc:
                logger.warning(
                    "TestSetResolverFunction invocation failed (non-fatal): %s", exc
                )
        else:
            logger.warning(
                "TestSetResolverFunction not found for stack %s — skipping resolver step",
                stack_name,
            )

        # Locate TestRunnerFunction (required)
        test_runner_function = next(
            (
                f["FunctionName"]
                for f in all_functions
                if stack_name in f["FunctionName"]
                and "TestRunnerFunction" in f["FunctionName"]
            ),
            None,
        )

        if not test_runner_function:
            raise IDPResourceNotFoundError(
                f"TestRunnerFunction not found for stack {stack_name}"
            )

        # Enhancement 1 — Step 2: Include configVersion in the TestRunnerFunction payload.
        payload = {"arguments": {"input": {"testSetId": test_set}}}
        if context:
            payload["arguments"]["input"]["context"] = context
        if number_of_files:
            payload["arguments"]["input"]["numberOfFiles"] = number_of_files
        if config_version:
            payload["arguments"]["input"]["configVersion"] = config_version
        # Pinning must reach the run, or the caller believes they scored r7 while
        # the run actually used whatever the profile currently holds — a silently
        # wrong answer that then goes into a comparison.
        if config_revision is not None:
            payload["arguments"]["input"]["configRevision"] = int(config_revision)

        response = lambda_client.invoke(
            FunctionName=test_runner_function, Payload=json.dumps(payload)
        )
        result = json.loads(response["Payload"].read())

        # Check for Lambda function errors (AWS returns StatusCode=200 for invocation success,
        # but function errors are indicated by errorMessage in the payload)
        if "errorMessage" in result:
            error_type = result.get("errorType", "Unknown")
            error_msg = result["errorMessage"]
            raise IDPProcessingError(
                f"Test runner execution failed ({error_type}): {error_msg}"
            )

        resources = processor.resources
        test_set_bucket = resources.get("TestSetBucket")
        s3_client = boto3.client("s3", region_name=self._client._region)

        document_ids = []
        response = s3_client.list_objects_v2(
            Bucket=test_set_bucket, Prefix=f"{test_set}/input/"
        )

        if "Contents" in response:
            batch_id = result["testRunId"]
            for obj in response["Contents"]:
                key = obj["Key"]
                if not key.endswith("/"):
                    filename = key.split("/")[-1]
                    document_ids.append(f"{batch_id}/{filename}")

        return {
            "batch_id": result["testRunId"],
            "document_ids": document_ids,
            "queued": result.get("filesCount", len(document_ids)),
            "uploaded": 0,
            "failed": 0,
            "source": f"test-set:{test_set}",
            "output_prefix": test_set,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def reprocess(
        self,
        step: Union[str, RerunStep],
        document_ids: Optional[List[str]] = None,
        batch_id: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> BatchReprocessResult:
        """Reprocess existing documents from a specific step.

        Args:
            step: Pipeline step to reprocess from
            document_ids: List of document IDs to reprocess
            batch_id: Batch ID (will reprocess all documents in batch)
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            BatchReprocessResult with reprocess statistics
        """
        from idp_sdk._core.rerun_processor import RerunProcessor

        name = self._client._require_stack(stack_name)
        step_str = step.value if isinstance(step, RerunStep) else step

        if not document_ids and not batch_id:
            raise IDPConfigurationError("Must specify either document_ids or batch_id")

        try:
            processor = RerunProcessor(stack_name=name, region=self._client._region)

            if batch_id and not document_ids:
                document_ids = processor.get_batch_document_ids(batch_id)

            result = processor.rerun_documents(
                document_ids=document_ids, step=step_str, monitor=False
            )

            return BatchReprocessResult(
                documents_queued=result.get("documents_queued", 0),
                documents_failed=result.get("documents_failed", 0),
                failed_documents=result.get("failed_documents", []),
                step=RerunStep(step_str),
            )
        except Exception as e:
            raise IDPProcessingError(f"Reprocess failed: {e}") from e

    def rerun(
        self,
        step: Union[str, RerunStep],
        document_ids: Optional[List[str]] = None,
        batch_id: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> BatchReprocessResult:
        """Deprecated: Use reprocess() instead."""
        import warnings

        warnings.warn(
            "rerun() is deprecated, use reprocess() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.reprocess(
            step=step,
            document_ids=document_ids,
            batch_id=batch_id,
            stack_name=stack_name,
            **kwargs,
        )

    def get_document_ids(
        self,
        batch_id: str,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> List[str]:
        """Get all document IDs belonging to a batch.

        Useful for pre-fetching a document count before a confirmation prompt
        without triggering the full reprocess pipeline.

        Args:
            batch_id: Batch identifier
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            List of document object keys (S3 keys) in the batch

        Raises:
            IDPResourceNotFoundError: If the batch does not exist
            IDPProcessingError: On unexpected errors
        """
        from idp_sdk._core.batch_processor import BatchProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = BatchProcessor(stack_name=name, region=self._client._region)
            batch_info = processor.get_batch_info(batch_id)

            if not batch_info:
                raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")

            return batch_info.get("document_ids", [])
        except IDPResourceNotFoundError:
            raise
        except Exception as e:
            raise IDPProcessingError(
                f"Failed to get document IDs for batch {batch_id}: {e}"
            ) from e

    def get_status(
        self,
        batch_id: str,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> BatchStatus:
        """Get status of a batch.

        Args:
            batch_id: Batch identifier
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            BatchStatus with batch processing information
        """
        from idp_sdk._core.batch_processor import BatchProcessor
        from idp_sdk._core.progress_monitor import ProgressMonitor

        name = self._client._require_stack(stack_name)
        processor = BatchProcessor(stack_name=name, region=self._client._region)

        batch_info = processor.get_batch_info(batch_id)
        if not batch_info:
            raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")

        document_ids = batch_info["document_ids"]

        monitor = ProgressMonitor(
            stack_name=name, resources=processor.resources, region=self._client._region
        )
        status_data = monitor.get_batch_status(document_ids)
        stats = monitor.calculate_statistics(status_data)

        documents = []
        for category in ["completed", "running", "queued", "failed"]:
            for doc in status_data.get(category, []):
                start_time = doc.get("start_time") or None
                end_time = doc.get("end_time") or None
                if start_time == "":
                    start_time = None
                if end_time == "":
                    end_time = None
                documents.append(
                    DocumentStatus(
                        document_id=doc.get("document_id", ""),
                        status=doc.get("status", "UNKNOWN"),
                        start_time=start_time,
                        end_time=end_time,
                        duration_seconds=doc.get("duration"),
                        num_pages=doc.get("num_pages"),
                        num_sections=doc.get("num_sections"),
                        error=doc.get("error"),
                    )
                )

        return BatchStatus(
            batch_id=batch_id,
            documents=documents,
            total=stats.get("total", len(documents)),
            completed=stats.get("completed", 0),
            failed=stats.get("failed", 0),
            in_progress=stats.get("running", 0),
            queued=stats.get("queued", 0),
            success_rate=stats.get("success_rate", 0.0) / 100.0,
            all_complete=stats.get("all_complete", False),
        )

    def list(
        self,
        limit: int = 10,
        next_token: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> BatchListResult:
        """List recent batch processing jobs.

        Args:
            limit: Maximum number of batches to return
            next_token: Pagination token from previous request
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            BatchListResult with batches and optional next_token
        """
        from idp_sdk._core.batch_processor import BatchProcessor

        name = self._client._require_stack(stack_name)
        processor = BatchProcessor(stack_name=name, region=self._client._region)
        result = processor.list_batches(limit=limit, next_token=next_token)

        batches = [
            BatchInfo(
                batch_id=b["batch_id"],
                document_ids=b["document_ids"],
                queued=b.get("queued", 0),
                failed=b.get("failed", 0),
                timestamp=b.get("timestamp", ""),
            )
            for b in result["batches"]
        ]

        return BatchListResult(
            batches=batches,
            count=result["count"],
            next_token=result.get("next_token"),
        )

    def download_results(
        self,
        batch_id: str,
        output_dir: str,
        file_types: Optional[List[str]] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> BatchDownloadResult:
        """Download processing results from OutputBucket.

        Args:
            batch_id: Batch identifier
            output_dir: Local directory to save results
            file_types: List of file types to download
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            BatchDownloadResult with download statistics
        """
        from idp_sdk._core.batch_processor import BatchProcessor

        name = self._client._require_stack(stack_name)
        processor = BatchProcessor(stack_name=name, region=self._client._region)

        types_list = file_types or ["all"]
        if "all" in types_list:
            types_list = ["pages", "sections", "summary", "evaluation"]

        result = processor.download_batch_results(
            batch_id=batch_id, output_dir=output_dir, file_types=types_list
        )

        return BatchDownloadResult(
            files_downloaded=result.get("files_downloaded", 0),
            documents_downloaded=result.get("documents_downloaded", 0),
            output_dir=result.get("output_dir", output_dir),
        )

    def list_versions(
        self,
        document_id: str,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """List processing-run versions for a document, newest first.

        Args:
            document_id: The document's S3 object key (its tracking id)
            stack_name: Optional stack name override

        Returns:
            List of run records (dicts with RunId, CompletionTime,
            ConfigVersion, FileCount, ManifestUri, ...).
        """
        from idp_sdk._core.batch_processor import BatchProcessor

        name = self._client._require_stack(stack_name)
        processor = BatchProcessor(stack_name=name, region=self._client._region)
        return processor.list_document_versions(document_id)

    def download_version(
        self,
        document_id: str,
        run_id: str,
        output_dir: str,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> BatchDownloadResult:
        """Download the exact output bytes of a specific document version.

        Uses the run's manifest to fetch each output object by its pinned S3
        VersionId, so the download reflects that run even if later runs have
        overwritten the objects.

        Args:
            document_id: The document's S3 object key
            run_id: Run identifier (from list_versions)
            output_dir: Local directory to save results
            stack_name: Optional stack name override

        Returns:
            BatchDownloadResult with download statistics
        """
        from idp_sdk._core.batch_processor import BatchProcessor

        name = self._client._require_stack(stack_name)
        processor = BatchProcessor(stack_name=name, region=self._client._region)
        result = processor.download_version_results(
            document_id=document_id, run_id=run_id, output_dir=output_dir
        )
        return BatchDownloadResult(
            files_downloaded=result.get("files_downloaded", 0),
            documents_downloaded=1,
            output_dir=result.get("output_dir", output_dir),
        )

    def download_sources(
        self,
        batch_id: str,
        output_dir: str,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> BatchDownloadResult:
        """Download original source files from InputBucket for all documents in a batch.

        Args:
            batch_id: Batch identifier
            output_dir: Local directory to save source files
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            BatchDownloadResult with download statistics
        """
        import os

        import boto3

        from idp_sdk._core.batch_processor import BatchProcessor

        name = self._client._require_stack(stack_name)
        resources = self._client._get_stack_resources(name)
        input_bucket = resources["InputBucket"]

        processor = BatchProcessor(stack_name=name, region=self._client._region)
        batch_info = processor.get_batch_info(batch_id)
        if not batch_info:
            raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")

        document_ids = batch_info["document_ids"]
        s3_client = boto3.client("s3", region_name=self._client._region)

        os.makedirs(output_dir, exist_ok=True)
        files_downloaded = 0

        try:
            for doc_id in document_ids:
                local_path = os.path.join(output_dir, doc_id)
                local_dir = os.path.dirname(local_path)
                os.makedirs(local_dir, exist_ok=True)

                s3_client.download_file(
                    Bucket=input_bucket, Key=doc_id, Filename=local_path
                )
                files_downloaded += 1

            return BatchDownloadResult(
                files_downloaded=files_downloaded,
                documents_downloaded=files_downloaded,
                output_dir=output_dir,
            )
        except Exception as e:
            raise IDPProcessingError(f"Failed to download sources: {e}") from e

    def delete_documents(
        self,
        batch_id: Optional[str] = None,
        pattern: Optional[str] = None,
        status_filter: Optional[str] = None,
        stack_name: Optional[str] = None,
        dry_run: bool = False,
        continue_on_error: bool = True,
        **kwargs,
    ) -> BatchDeletionResult:
        """Permanently delete documents and their associated data.

        Specify either ``batch_id`` or ``pattern`` to select documents.

        Args:
            batch_id: Batch identifier (selects all docs containing this string)
            pattern: Wildcard pattern to match document keys (e.g. ``"batch-123/*.pdf"``)
            status_filter: Optional status filter (e.g., 'FAILED', 'COMPLETED')
            stack_name: Optional stack name override
            dry_run: If True, only simulate deletion without actually deleting
            continue_on_error: If True, continue deleting other documents if one fails
            **kwargs: Additional parameters

        Returns:
            BatchDeletionResult with deletion statistics
        """
        import boto3

        from idp_common.delete_documents import (
            delete_documents,
            get_documents_by_batch,
            get_documents_by_pattern,
        )

        if not batch_id and not pattern:
            raise IDPConfigurationError("Must specify either batch_id or pattern")
        if batch_id and pattern:
            raise IDPConfigurationError("Cannot specify both batch_id and pattern")

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

        try:
            if pattern:
                document_ids = get_documents_by_pattern(
                    tracking_table=tracking_table,
                    pattern=pattern,
                    status_filter=status_filter,
                )
            else:
                document_ids = get_documents_by_batch(
                    tracking_table=tracking_table,
                    batch_id=batch_id,
                    status_filter=status_filter,
                )

            if not document_ids:
                return BatchDeletionResult(
                    success=True,
                    deleted_count=0,
                    failed_count=0,
                    total_count=0,
                    dry_run=dry_run,
                    results=[],
                )

            result = delete_documents(
                object_keys=document_ids,
                tracking_table=tracking_table,
                s3_client=s3_client,
                input_bucket=input_bucket,
                output_bucket=output_bucket,
                dry_run=dry_run,
                continue_on_error=continue_on_error,
            )

            single_results = [
                DocumentDeletionResult(
                    success=r.get("success", False),
                    object_key=r.get("object_key", ""),
                    deleted=r.get("deleted", {}),
                    errors=r.get("errors", []),
                )
                for r in result.get("results", [])
            ]

            return BatchDeletionResult(
                success=result.get("success", False),
                deleted_count=result.get("deleted_count", 0),
                failed_count=result.get("failed_count", 0),
                total_count=result.get("total_count", 0),
                dry_run=result.get("dry_run", dry_run),
                results=single_results,
            )
        except Exception as e:
            raise IDPProcessingError(f"Batch deletion failed: {e}") from e

    def get_results(
        self,
        batch_id: str,
        section_id: int = 1,
        limit: int = 10,
        next_token: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Get extracted metadata and fields for all documents in a batch.

        Args:
            batch_id: Batch identifier
            section_id: Section number within documents (default: 1)
            limit: Maximum documents to return per page (default: 10)
            next_token: Pagination token from previous request
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            Dictionary with batch metadata and paginated documents
        """
        import base64

        from idp_sdk._core.batch_processor import BatchProcessor
        from idp_sdk._core.progress_monitor import ProgressMonitor

        name = self._client._require_stack(stack_name)
        batch_processor = BatchProcessor(stack_name=name, region=self._client._region)
        monitor = ProgressMonitor(
            stack_name=name,
            resources=batch_processor.resources,
            region=self._client._region,
        )

        batch_info = batch_processor.get_batch_info(batch_id)
        if not batch_info:
            raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")

        document_ids = batch_info.get("document_ids", [])
        total_in_batch = len(document_ids)

        # Parse pagination token
        offset = 0
        if next_token:
            try:
                decoded = base64.b64decode(next_token).decode("utf-8")
                offset = int(decoded)
            except Exception:
                offset = 0

        # Get paginated document slice
        page_docs = document_ids[offset : offset + limit]
        has_more = offset + limit < total_in_batch

        # Retrieve metadata for each document
        import boto3

        s3_client = boto3.client("s3", region_name=self._client._region)
        output_bucket = batch_processor.resources.get("OutputBucket")

        documents = [
            self._get_document_metadata(
                monitor=monitor,
                s3_client=s3_client,
                output_bucket=output_bucket,
                doc_id=doc_id,
                section_id=section_id,
            )
            for doc_id in page_docs
        ]

        result = {
            "batch_id": batch_id,
            "section_id": section_id,
            "count": len(documents),
            "total_in_batch": total_in_batch,
            "documents": documents,
        }

        # Add pagination token if more results exist
        if has_more:
            next_offset = offset + limit
            result["next_token"] = base64.b64encode(
                str(next_offset).encode("utf-8")
            ).decode("utf-8")

        return result

    def get_document_results(
        self,
        document_id: str,
        section_id: int = 1,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Get extracted metadata and fields for a single document by its id.

        Unlike :meth:`get_results`, no batch id is needed: the document id (the
        S3 object key of the source document) is also the prefix of its results
        in the output bucket, so the document is addressed directly. An
        ``s3://`` URI pointing at the document's output prefix (e.g. the
        ``idp_raw_ref`` emitted to post-processing hook consumers) is also
        accepted — its key part is used as the document id.

        Args:
            document_id: Document identifier (S3 object key of the source
                document), or an ``s3://`` URI whose key is the document id
            section_id: Section number within the document (default: 1)
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            Dictionary with the document's metadata: document_id,
            document_class, fields, confidence, page_count, status
        """
        import boto3

        from idp_sdk._core.batch_processor import BatchProcessor
        from idp_sdk._core.progress_monitor import ProgressMonitor

        name = self._client._require_stack(stack_name)
        batch_processor = BatchProcessor(stack_name=name, region=self._client._region)
        monitor = ProgressMonitor(
            stack_name=name,
            resources=batch_processor.resources,
            region=self._client._region,
        )
        s3_client = boto3.client("s3", region_name=self._client._region)
        output_bucket = batch_processor.resources.get("OutputBucket")

        doc_id = self._document_id_from_ref(document_id=document_id)
        return self._get_document_metadata(
            monitor=monitor,
            s3_client=s3_client,
            output_bucket=output_bucket,
            doc_id=doc_id,
            section_id=section_id,
        )

    @staticmethod
    def _document_id_from_ref(document_id: str) -> str:
        """Normalize a document reference to a bare document id.

        Accepts either a bare document id (S3 object key) or an ``s3://``
        URI pointing at the document's output prefix. The URI is parsed with a
        plain string split — never ``urllib.parse.urlparse``, which treats
        ``#`` in keys (e.g. ``Report_#2.pdf``) as a fragment delimiter and
        silently truncates them.

        Args:
            document_id: Bare document id or ``s3://bucket/<document_id>[/]``

        Returns:
            The bare document id, without any trailing slash.
        """
        if document_id.startswith("s3://"):
            parts = document_id[len("s3://") :].split("/", 1)
            if len(parts) < 2 or not parts[1]:
                raise ValueError(
                    f"Invalid document reference: {document_id}. "
                    "Expected s3://bucket/<document_id>"
                )
            document_id = parts[1]
        return document_id.rstrip("/")

    def _get_document_metadata(
        self,
        monitor: Any,
        s3_client: Any,
        output_bucket: Optional[str],
        doc_id: str,
        section_id: int,
    ) -> Dict[str, Any]:
        """Retrieve status and extracted metadata for one document.

        Reads ``<doc_id>/sections/<section_id>/result.json`` from the output
        bucket when the document has completed processing. Shared by
        :meth:`get_results` (per batch document) and
        :meth:`get_document_results` (single document).

        Args:
            monitor: ProgressMonitor for the stack (document status lookup)
            s3_client: boto3 S3 client
            output_bucket: Name of the stack's output bucket
            doc_id: Document identifier (S3 object key)
            section_id: Section number within the document

        Returns:
            Dictionary with document_id, document_class, fields, confidence,
            page_count, and status (ERROR status if retrieval failed).
        """
        import json

        try:
            # Get status
            status_data = monitor.get_batch_status([doc_id])
            status = "UNKNOWN"
            if status_data.get("completed"):
                status = "COMPLETED"
            elif status_data.get("running"):
                status = "RUNNING"
            elif status_data.get("queued"):
                status = "QUEUED"
            elif status_data.get("failed"):
                status = "FAILED"

            # Try to get results.json from S3
            document_class = None
            fields = None
            confidence = None
            page_count = None

            if status == "COMPLETED":
                try:
                    s3_key = f"{doc_id}/sections/{section_id}/result.json"
                    response = s3_client.get_object(Bucket=output_bucket, Key=s3_key)
                    result_data = json.loads(response["Body"].read())

                    document_class = result_data.get("document_class", {}).get("type")
                    inference = result_data.get("inference_result", {})
                    fields = {
                        k: v
                        for k, v in inference.items()
                        if k not in ["metadata", "explainability_info"]
                    }

                    # Extract confidence from explainability_info - nested structure
                    explainability = result_data.get("explainability_info", [])
                    if explainability:
                        confidence = {}

                        def confidence_of(val):
                            """Mirror one explainability node's shape.

                            Lists are mirrored as lists so a list attribute's
                            per-row confidence survives instead of being dropped
                            — including a multi-instance class (#715), whose
                            entire result sits under one `instances` list and
                            which would otherwise report NO confidence at all.
                            """
                            if isinstance(val, dict):
                                if "confidence" in val:
                                    return val["confidence"]
                                return {k: confidence_of(v) for k, v in val.items()}
                            if isinstance(val, list):
                                return [confidence_of(v) for v in val]
                            return None

                        def extract_confidences(obj, target_dict):
                            for key, val in obj.items():
                                if isinstance(val, (dict, list)):
                                    target_dict[key] = confidence_of(val)

                        for item in explainability:
                            extract_confidences(item, confidence)

                    page_count = len(
                        result_data.get("split_document", {}).get("page_indices", [])
                    )
                except Exception as e:
                    logger.warning(f"Could not read results.json for {doc_id}: {e}")

            return {
                "document_id": doc_id,
                "document_class": document_class,
                "fields": fields,
                # Multi-instance classes (#715) return one entry per document
                # found in the section; None for a single-record class. Additive
                # — `fields` still carries the raw shape.
                "instances": _section_instances(fields),
                "confidence": confidence,
                "page_count": page_count,
                "status": status,
            }
        except Exception as e:
            logger.warning(f"Error retrieving metadata for {doc_id}: {e}")
            return {
                "document_id": doc_id,
                "document_class": None,
                "fields": None,
                "instances": None,
                "confidence": None,
                "page_count": None,
                "status": "ERROR",
            }

    def get_confidence(
        self,
        batch_id: str,
        section_id: int = 1,
        limit: int = 10,
        next_token: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Get confidence scores and quality metrics for all documents in a batch.

        Args:
            batch_id: Batch identifier
            section_id: Section number (default: 1)
            limit: Maximum documents to return (default: 10)
            next_token: Pagination token from previous request
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            Dictionary with batch confidence data and paginated documents
        """
        import base64

        from idp_sdk._core.assessment_analyzer import AssessmentAnalyzer
        from idp_sdk._core.batch_processor import BatchProcessor
        from idp_sdk._core.progress_monitor import ProgressMonitor

        name = self._client._require_stack(stack_name)
        batch_processor = BatchProcessor(stack_name=name, region=self._client._region)
        analyzer = AssessmentAnalyzer(stack_name=name, region=self._client._region)
        monitor = ProgressMonitor(
            stack_name=name,
            resources=batch_processor.resources,
            region=self._client._region,
        )

        batch_info = batch_processor.get_batch_info(batch_id)
        if not batch_info:
            raise IDPResourceNotFoundError(f"Batch not found: {batch_id}")

        document_ids = batch_info.get("document_ids", [])
        total_in_batch = len(document_ids)

        # Parse pagination token
        offset = 0
        if next_token:
            try:
                decoded = base64.b64decode(next_token).decode("utf-8")
                offset = int(decoded)
            except Exception:
                offset = 0

        # Get paginated document slice
        page_docs = document_ids[offset : offset + limit]
        has_more = offset + limit < total_in_batch

        # Retrieve confidence scores for each document
        documents = []

        for doc_id in page_docs:
            try:
                confidence_data = analyzer.get_confidence(doc_id, section_id)
                status_data = monitor.get_batch_status([doc_id])
                status = "UNKNOWN"
                if status_data.get("completed"):
                    status = "COMPLETED"
                elif status_data.get("running"):
                    status = "RUNNING"
                elif status_data.get("queued"):
                    status = "QUEUED"
                elif status_data.get("failed"):
                    status = "FAILED"

                attributes = confidence_data.get("attributes", {})
                documents.append(
                    {
                        "document_id": doc_id,
                        "attributes": attributes,
                        "status": status,
                    }
                )
            except FileNotFoundError:
                documents.append(
                    {
                        "document_id": doc_id,
                        "attributes": {},
                        "status": "PROCESSING",
                    }
                )
            except Exception as e:
                logger.warning(f"Error retrieving confidence for {doc_id}: {e}")
                documents.append(
                    {
                        "document_id": doc_id,
                        "attributes": {},
                        "status": "ERROR",
                    }
                )

        result = {
            "batch_id": batch_id,
            "section_id": section_id,
            "count": len(documents),
            "total_in_batch": total_in_batch,
            "documents": documents,
        }

        # Add pagination token if more results exist
        if has_more:
            next_offset = offset + limit
            result["next_token"] = base64.b64encode(
                str(next_offset).encode("utf-8")
            ).decode("utf-8")

        return result

    def stop_workflows(
        self,
        stack_name: Optional[str] = None,
        skip_purge: bool = False,
        skip_stop: bool = False,
        **kwargs,
    ) -> StopWorkflowsResult:
        """Stop all running workflows for a stack.

        Args:
            stack_name: Optional stack name override
            skip_purge: Skip purging the queue
            skip_stop: Skip stopping executions
            **kwargs: Additional parameters

        Returns:
            StopWorkflowsResult with stop statistics
        """
        from idp_sdk._core.stop_workflows import WorkflowStopper

        name = self._client._require_stack(stack_name)
        stopper = WorkflowStopper(stack_name=name, region=self._client._region)
        results = stopper.stop_all(skip_purge=skip_purge, skip_stop=skip_stop)

        # Enhancement 6: Map raw stopper dict into typed nested Pydantic models
        exec_raw = results.get("executions_stopped") or {}
        abort_raw = results.get("documents_aborted") or {}

        return StopWorkflowsResult(
            executions_stopped=ExecutionsStoppedResult(
                total_stopped=exec_raw.get("total_stopped", 0),
                total_failed=exec_raw.get("total_failed", 0),
                remaining=exec_raw.get("remaining", 0),
                error=exec_raw.get("error"),
            )
            if exec_raw
            else None,
            documents_aborted=DocumentsAbortedResult(
                documents_aborted=abort_raw.get("documents_aborted", 0),
                error=abort_raw.get("error"),
            )
            if abort_raw
            else None,
            queue_purged=not skip_purge,
        )
