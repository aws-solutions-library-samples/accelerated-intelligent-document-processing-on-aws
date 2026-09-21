# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Evaluation operations for IDP SDK."""

from typing import Dict, Optional

from idp_sdk.exceptions import IDPProcessingError, IDPResourceNotFoundError
from idp_sdk.models import (
    BaselineInfo,
    EvaluationBaselineListResult,
    EvaluationMetrics,
    EvaluationReport,
    FieldComparison,
    UseAsBaselineResult,
)


class EvaluationOperation:
    """Evaluation and baseline management operations."""

    def __init__(self, client):
        self._client = client

    def create_baseline(
        self,
        document_id: str,
        baseline_data: Dict,
        metadata: Optional[Dict] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> Dict:
        """Create evaluation baseline for a document.

        Args:
            document_id: Document identifier (S3 key)
            baseline_data: Baseline data structure (sections with expected fields)
            metadata: Optional metadata
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            Dictionary with baseline creation result
        """
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = EvaluationProcessor(
                stack_name=name, region=self._client._region
            )
            return processor.create_baseline(
                document_id=document_id, baseline_data=baseline_data, metadata=metadata
            )
        except Exception as e:
            raise IDPProcessingError(f"Failed to create baseline: {e}") from e

    def use_as_baseline(
        self,
        document_id: str,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> UseAsBaselineResult:
        """Promote a processed document's output to the evaluation baseline.

        Programmatic equivalent of the UI "Use as Evaluation Baseline" button:
        copies every object under the document's output prefix into the
        evaluation baseline bucket and updates the document's ``EvaluationStatus``
        to ``BASELINE_AVAILABLE``. Runs synchronously (returns when the copy is
        complete), so it can be scripted and its result checked directly.

        Args:
            document_id: Document identifier (S3 key / object key)
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            UseAsBaselineResult with the number of files copied and final status

        Raises:
            IDPResourceNotFoundError: If no output exists for the document
            IDPProcessingError: If the copy fails
        """
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = EvaluationProcessor(
                stack_name=name, region=self._client._region
            )
            result = processor.use_as_baseline(document_id=document_id)

            return UseAsBaselineResult(
                document_id=result["document_id"],
                files_copied=result["files_copied"],
                evaluation_status=result["evaluation_status"],
                timestamp=result.get("timestamp"),
            )
        except FileNotFoundError as e:
            raise IDPResourceNotFoundError(str(e)) from e
        except Exception as e:
            raise IDPProcessingError(f"Failed to use document as baseline: {e}") from e

    def get_report(
        self,
        document_id: str,
        section_id: int = 1,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> EvaluationReport:
        """Get evaluation report for a document section.

        Args:
            document_id: Document identifier (S3 key)
            section_id: Section number (default: 1)
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            EvaluationReport with the section's scores and its per-attribute
            comparisons

        Raises:
            IDPResourceNotFoundError: If the document has not been evaluated, or
                its results contain no such section
            IDPProcessingError: If the report cannot be read
        """
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = EvaluationProcessor(
                stack_name=name, region=self._client._region
            )
            result = processor.get_report(
                document_id=document_id, section_id=section_id
            )

            return EvaluationReport(
                document_id=result["document_id"],
                section_id=result["section_id"],
                field_comparisons=[
                    FieldComparison(
                        attribute=comparison["attribute"],
                        expected=comparison.get("expected"),
                        actual=comparison.get("actual"),
                        matched=bool(comparison.get("matched")),
                        score=comparison.get("score"),
                        method=comparison.get("method"),
                        reason=comparison.get("reason"),
                    )
                    for comparison in result.get("field_comparisons", [])
                ],
                document_class=result.get("document_class"),
                accuracy=result.get("accuracy"),
                precision=result.get("precision"),
                recall=result.get("recall"),
                f1_score=result.get("f1_score"),
                overall_metrics=result.get("overall_metrics", {}),
            )
        except FileNotFoundError as e:
            raise IDPResourceNotFoundError(str(e)) from e
        except Exception as e:
            raise IDPProcessingError(f"Failed to get evaluation report: {e}") from e

    def get_metrics(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        document_class: Optional[str] = None,
        batch_id: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> EvaluationMetrics:
        """Get aggregated evaluation metrics.

        Args:
            start_date: Start date filter (ISO format)
            end_date: End date filter (ISO format)
            document_class: Document class filter
            batch_id: Batch ID filter
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            EvaluationMetrics with aggregated statistics.

            Passing ``document_class`` leaves the four top-level averages ``None``
            and puts the class-scoped answer in
            ``by_document_class[document_class]``: the top-level figures come from
            each document's whole-document metrics, which cannot answer a question
            about one class of section.
        """
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = EvaluationProcessor(
                stack_name=name, region=self._client._region
            )
            result = processor.get_metrics(
                start_date=start_date,
                end_date=end_date,
                document_class=document_class,
                batch_id=batch_id,
            )

            return EvaluationMetrics(
                total_documents=result["total_documents"],
                avg_accuracy=result["avg_accuracy"],
                avg_precision=result["avg_precision"],
                avg_recall=result["avg_recall"],
                avg_f1_score=result["avg_f1_score"],
                by_document_class=result["by_document_class"],
                start_date=start_date,
                end_date=end_date,
                document_class=document_class,
            )
        except Exception as e:
            raise IDPProcessingError(f"Failed to get evaluation metrics: {e}") from e

    def list_baselines(
        self,
        limit: int = 100,
        next_token: Optional[str] = None,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> EvaluationBaselineListResult:
        """List evaluation baselines with pagination.

        Args:
            limit: Maximum number of baselines to return (default: 100)
            next_token: Pagination token from previous request
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            EvaluationBaselineListResult with baselines and optional next_token
        """
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = EvaluationProcessor(
                stack_name=name, region=self._client._region
            )
            result = processor.list_baselines(limit=limit, next_token=next_token)

            return EvaluationBaselineListResult(
                baselines=[
                    BaselineInfo(
                        document_id=baseline["document_id"],
                        s3_location=baseline["s3_location"],
                    )
                    for baseline in result["baselines"]
                ],
                count=result["count"],
                next_token=result.get("next_token"),
            )
        except Exception as e:
            raise IDPProcessingError(f"Failed to list baselines: {e}") from e

    def delete_baseline(
        self,
        document_id: str,
        stack_name: Optional[str] = None,
        **kwargs,
    ) -> Dict:
        """Delete evaluation baseline for a document.

        Args:
            document_id: Document identifier (S3 key)
            stack_name: Optional stack name override
            **kwargs: Additional parameters

        Returns:
            Dictionary with deletion result
        """
        from idp_sdk._core.evaluation_processor import EvaluationProcessor

        name = self._client._require_stack(stack_name)

        try:
            processor = EvaluationProcessor(
                stack_name=name, region=self._client._region
            )
            return processor.delete_baseline(document_id=document_id)
        except Exception as e:
            raise IDPProcessingError(f"Failed to delete baseline: {e}") from e
