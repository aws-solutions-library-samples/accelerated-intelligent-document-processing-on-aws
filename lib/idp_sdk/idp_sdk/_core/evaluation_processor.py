# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Evaluation Processor Module

Handles evaluation baseline management and report operations.
"""

import json
import logging
from datetime import datetime
from typing import Dict, Optional

import boto3
from botocore.exceptions import ClientError

from idp_sdk._core.stack_info import StackInfo

logger = logging.getLogger(__name__)


class EvaluationProcessor:
    """Processes evaluation baselines and reports"""

    def __init__(self, stack_name: str, region: Optional[str] = None):
        """
        Initialize evaluation processor

        Args:
            stack_name: Name of the CloudFormation stack
            region: AWS region (optional)
        """
        self.stack_name = stack_name
        self.region = region

        # Initialize AWS clients
        self.s3 = boto3.client("s3", region_name=region)
        self.dynamodb = boto3.resource("dynamodb", region_name=region)

        # Get stack resources
        stack_info = StackInfo(stack_name, region)
        if not stack_info.validate_stack():
            raise ValueError(
                f"Stack '{stack_name}' is not in a valid state for operations"
            )

        self.resources = stack_info.get_resources()
        logger.info(f"Initialized evaluation processor for stack: {stack_name}")

    def create_baseline(
        self, document_id: str, baseline_data: Dict, metadata: Optional[Dict] = None
    ) -> Dict:
        """
        Create evaluation baseline for a document

        Args:
            document_id: Document identifier (S3 key)
            baseline_data: Baseline data structure (sections with expected fields)
            metadata: Optional metadata

        Returns:
            Dictionary with baseline creation result
        """
        baseline_bucket = self.resources.get("EvaluationBaselineBucket")
        if not baseline_bucket:
            raise ValueError("EvaluationBaselineBucket not found in stack resources")

        try:
            # Store baseline structure matching expected format
            for section_id, section_data in baseline_data.items():
                section_key = f"{document_id}/sections/{section_id}/result.json"
                self.s3.put_object(
                    Bucket=baseline_bucket,
                    Key=section_key,
                    Body=json.dumps(section_data),
                    ContentType="application/json",
                )

            # Store metadata if provided
            if metadata:
                metadata_key = f"{document_id}/metadata.json"
                self.s3.put_object(
                    Bucket=baseline_bucket,
                    Key=metadata_key,
                    Body=json.dumps(metadata),
                    ContentType="application/json",
                )

            return {
                "document_id": document_id,
                "sections_created": len(baseline_data),
                "timestamp": datetime.utcnow().isoformat(),
            }

        except Exception as e:
            logger.error(f"Error creating baseline: {e}")
            raise

    def use_as_baseline(self, document_id: str) -> Dict:
        """Promote a processed document's output to the evaluation baseline.

        This is the programmatic equivalent of the UI "Use as Evaluation
        Baseline" button: it copies every object under ``{document_id}/`` from
        the output bucket into the evaluation baseline bucket, then records the
        outcome on the document's tracking record via ``EvaluationStatus``
        (``BASELINE_AVAILABLE`` on success, ``BASELINE_ERROR`` on failure).

        Unlike the UI mutation (which returns immediately and copies in a
        background Lambda), this runs synchronously and only returns once the
        copy is complete — better suited to scripting and monitoring.

        Args:
            document_id: Document identifier (S3 key / object key)

        Returns:
            Dictionary with copy result: document_id, files_copied,
            evaluation_status, and timestamp.
        """
        output_bucket = self.resources.get("OutputBucket")
        if not output_bucket:
            raise ValueError("OutputBucket not found in stack resources")

        baseline_bucket = self.resources.get("EvaluationBaselineBucket")
        if not baseline_bucket:
            raise ValueError("EvaluationBaselineBucket not found in stack resources")

        # Collect all objects under the document prefix. The prefix is the
        # document id followed by "/" so we don't accidentally match sibling
        # documents whose id shares a prefix (e.g. "doc1" vs "doc10").
        prefix = f"{document_id}/"
        paginator = self.s3.get_paginator("list_objects_v2")
        keys = [
            obj["Key"]
            for page in paginator.paginate(Bucket=output_bucket, Prefix=prefix)
            for obj in page.get("Contents", [])
        ]

        if not keys:
            raise FileNotFoundError(
                f"No output objects found under prefix '{prefix}' in bucket "
                f"'{output_bucket}'. Has the document finished processing?"
            )

        # Mark copy in progress so concurrent readers (UI/CLI) see the state.
        self._set_evaluation_status(document_id, "BASELINE_COPYING")

        copied = 0
        try:
            for key in keys:
                self.s3.copy_object(
                    CopySource={"Bucket": output_bucket, "Key": key},
                    Bucket=baseline_bucket,
                    Key=key,
                )
                copied += 1
        except Exception as e:
            logger.error(f"Error copying baseline objects for {document_id}: {e}")
            self._set_evaluation_status(document_id, "BASELINE_ERROR")
            raise

        self._set_evaluation_status(document_id, "BASELINE_AVAILABLE")

        return {
            "document_id": document_id,
            "files_copied": copied,
            "evaluation_status": "BASELINE_AVAILABLE",
            "timestamp": datetime.utcnow().isoformat(),
        }

    def _set_evaluation_status(self, document_id: str, status: str) -> None:
        """Set EvaluationStatus on the document's tracking record.

        Mirrors the DynamoDB layout used by idp_common's document service
        (PK="doc#<key>", SK="none"). Best-effort: a status-write failure is
        logged but not raised so it can't mask the copy result — except from
        ``use_as_baseline`` which decides its own error handling.
        """
        table_name = self.resources.get("DocumentsTable")
        if not table_name:
            logger.warning(
                "DocumentsTable not found in stack resources; skipping "
                "EvaluationStatus update"
            )
            return

        try:
            table = self.dynamodb.Table(table_name)
            table.update_item(
                Key={"PK": f"doc#{document_id}", "SK": "none"},
                UpdateExpression="SET #es = :es",
                ExpressionAttributeNames={"#es": "EvaluationStatus"},
                ExpressionAttributeValues={":es": status},
            )
        except Exception as e:
            logger.warning(
                f"Failed to set EvaluationStatus={status} for {document_id}: {e}"
            )

    #: The scores ``idp_common.evaluation``'s derived-metrics helper writes into
    #: every ``metrics`` and ``overall_metrics`` block. It also writes
    #: ``false_alarm_rate`` and ``false_discovery_rate``, which are passed through
    #: in ``overall_metrics`` rather than averaged.
    METRIC_NAMES = ("accuracy", "precision", "recall", "f1_score")

    @staticmethod
    def _evaluation_results_key(document_id: str) -> str:
        """Where the pipeline writes a document's evaluation results.

        Delegates to ``idp_common`` rather than restating the template. The
        producer (``idp_common.evaluation.service``) and the aggregation Lambda
        both import this helper, so a copy here would let the key be changed in
        one place and leave this reader silently looking for an object nothing
        writes — which is the defect this method exists to fix. Imported inside
        the function, the pattern the rest of the SDK uses for ``idp_common``, so
        an operation that never touches evaluation never pays for the import.
        """
        from idp_common.evaluation.contract import evaluation_results_key

        return evaluation_results_key(document_id)

    @classmethod
    def _evaluation_results_suffix(cls) -> str:
        """The trailing part of that key, for filtering a bucket listing.

        ``get_metrics`` has no document id to work from — it sifts every object
        under a prefix — so it needs the template's tail rather than a concrete
        key, and derives it from the same helper by passing an empty id.

        Two properties this relies on and ``tests/unit/test_evaluation_operations
        .py`` asserts, because both fail *silently* rather than loudly: the
        document id must sit at the **front** of the template (otherwise no real
        key ends with this string and the scan reports zero evaluations), and the
        result must be non-empty (otherwise it matches every object in the
        bucket).
        """
        return cls._evaluation_results_key("")

    @classmethod
    def _new_score_accumulator(cls) -> Dict[str, list]:
        """A running ``[total, n]`` per metric.

        ``n`` is per metric rather than shared, because a section the pipeline
        excluded or failed to evaluate carries a ``metrics`` block with none of
        the four scores in it. Counting those in one shared denominator would
        pull every average toward zero without anything saying so.
        """
        return {name: [0.0, 0] for name in cls.METRIC_NAMES}

    @classmethod
    def _accumulate_scores(cls, accumulator: Dict[str, list], metrics: Dict) -> None:
        for name in cls.METRIC_NAMES:
            value = metrics.get(name)
            # `bool` is an `int`; a `True` here would mean the artifact put a flag
            # where a score belongs, so it is not silently averaged as 1.0.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                accumulator[name][0] += float(value)
                accumulator[name][1] += 1

    @classmethod
    def _averages(cls, accumulator: Dict[str, list]) -> Dict[str, Optional[float]]:
        """``{"avg_<metric>": mean or None}``, ``None`` when nothing reported it."""
        return {
            f"avg_{name}": (total / n if n else None)
            for name, (total, n) in accumulator.items()
        }

    @staticmethod
    def _section_metrics(section: Dict) -> Dict:
        metrics = section.get("metrics")
        return metrics if isinstance(metrics, dict) else {}

    @staticmethod
    def _field_comparisons(section: Dict) -> list:
        """Flatten a section's ``attributes`` into comparison dicts.

        The attribute keys are the evaluation service's own
        (``name``/``evaluation_method``); they are renamed here so the SDK's
        ``FieldComparison`` vocabulary does not have to track them.
        """
        return [
            {
                "attribute": attr.get("name", ""),
                "expected": attr.get("expected"),
                "actual": attr.get("actual"),
                "matched": bool(attr.get("matched")),
                "score": attr.get("score"),
                "method": attr.get("evaluation_method"),
                "reason": attr.get("reason"),
            }
            for attr in section.get("attributes", [])
            if isinstance(attr, dict)
        ]

    def get_report(self, document_id: str, section_id: int = 1) -> Dict:
        """
        Get evaluation report for a document section

        Args:
            document_id: Document identifier (S3 key)
            section_id: Section number (default: 1)

        Returns:
            Dictionary with the section's metrics, its per-attribute comparisons,
            and the document-level ``overall_metrics`` for context.

        Raises:
            FileNotFoundError: If the document has no evaluation results, or has
                results that do not include the requested section.
        """
        output_bucket = self.resources["OutputBucket"]
        eval_key = self._evaluation_results_key(document_id)

        try:
            response = self.s3.get_object(Bucket=output_bucket, Key=eval_key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise FileNotFoundError(
                    f"No evaluation results at s3://{output_bucket}/{eval_key}. "
                    "Has the document been evaluated against a baseline?"
                ) from e
            raise

        eval_data = json.loads(response["Body"].read())

        # section_id is a string in the artifact and an int on this API.
        wanted = str(section_id)
        section = next(
            (
                s
                for s in eval_data.get("section_results", [])
                if isinstance(s, dict) and str(s.get("section_id")) == wanted
            ),
            None,
        )
        if section is None:
            available = [
                str(s.get("section_id"))
                for s in eval_data.get("section_results", [])
                if isinstance(s, dict)
            ]
            raise FileNotFoundError(
                f"Evaluation results for '{document_id}' contain no section "
                f"{section_id}. Sections present: {available or 'none'}."
            )

        metrics = self._section_metrics(section)
        overall = eval_data.get("overall_metrics")

        return {
            "document_id": eval_data.get("document_id") or document_id,
            "section_id": section_id,
            "document_class": section.get("document_class"),
            "accuracy": metrics.get("accuracy"),
            "precision": metrics.get("precision"),
            "recall": metrics.get("recall"),
            "f1_score": metrics.get("f1_score"),
            "field_comparisons": self._field_comparisons(section),
            "overall_metrics": overall if isinstance(overall, dict) else {},
        }

    def get_metrics(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        document_class: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> Dict:
        """
        Get aggregated evaluation metrics

        Args:
            start_date: Start date filter (ISO format)
            end_date: End date filter (ISO format)
            document_class: Document class filter
            batch_id: Batch ID filter

        Returns:
            Dictionary with ``total_documents``, the four ``avg_*`` scores
            averaged over documents, and ``by_document_class`` giving the same
            four averaged over that class's *sections* plus a section ``count``.

            A document class is a property of a **section**, and the document
            level has only whole-document ``overall_metrics`` — so when
            ``document_class`` is set the four top-level averages are ``None``
            rather than a whole-document figure that reads as a class-scoped one.
            The class-scoped answer is in ``by_document_class[document_class]``.

            Any average is ``None`` when nothing in scope reported that metric: a
            section the pipeline excluded or failed to evaluate carries no scores,
            and counting it in the denominator would drag the average toward zero.
        """
        output_bucket = self.resources["OutputBucket"]

        try:
            prefix = f"{batch_id}/" if batch_id else ""
            results_suffix = self._evaluation_results_suffix()
            paginator = self.s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=output_bucket, Prefix=prefix)

            document_scores = self._new_score_accumulator()
            by_class: Dict[str, Dict] = {}
            count = 0

            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith(results_suffix):
                        continue

                    # Apply date filter
                    if start_date or end_date:
                        obj_date = obj["LastModified"].isoformat()
                        if start_date and obj_date < start_date:
                            continue
                        if end_date and obj_date > end_date:
                            continue

                    response = self.s3.get_object(Bucket=output_bucket, Key=key)
                    eval_data = json.loads(response["Body"].read())

                    sections = [
                        s
                        for s in eval_data.get("section_results", [])
                        if isinstance(s, dict)
                    ]

                    # A class filter keeps documents that contain a section of
                    # that class, and narrows the breakdown to those sections.
                    if document_class:
                        sections = [
                            s
                            for s in sections
                            if s.get("document_class") == document_class
                        ]
                        if not sections:
                            continue

                    overall = eval_data.get("overall_metrics")
                    self._accumulate_scores(
                        document_scores, overall if isinstance(overall, dict) else {}
                    )
                    count += 1

                    for section in sections:
                        doc_class = section.get("document_class") or "unknown"
                        bucket_for_class = by_class.setdefault(
                            doc_class,
                            {"count": 0, "_scores": self._new_score_accumulator()},
                        )
                        bucket_for_class["count"] += 1
                        self._accumulate_scores(
                            bucket_for_class["_scores"], self._section_metrics(section)
                        )

            for class_data in by_class.values():
                class_data.update(self._averages(class_data.pop("_scores")))

            # A class-scoped question cannot be answered from whole-document
            # metrics, so it is not answered with them.
            averages = (
                dict.fromkeys(f"avg_{name}" for name in self.METRIC_NAMES)
                if document_class
                else self._averages(document_scores)
            )

            return {
                "total_documents": count,
                **averages,
                "by_document_class": by_class,
            }

        except Exception as e:
            logger.error(f"Error getting metrics: {e}")
            raise

    def list_baselines(
        self, limit: int = 100, next_token: Optional[str] = None
    ) -> Dict:
        """
        List evaluation baselines with pagination

        Args:
            limit: Maximum number of baselines to return
            next_token: Pagination token from previous request

        Returns:
            Dictionary with baselines list and optional next_token
        """
        baseline_bucket = self.resources.get("EvaluationBaselineBucket")
        if not baseline_bucket:
            raise ValueError("EvaluationBaselineBucket not found in stack resources")

        try:
            # Build list parameters
            list_params = {
                "Bucket": baseline_bucket,
                "Delimiter": "/",
                "MaxKeys": limit,
            }

            if next_token:
                import base64

                decoded = base64.b64decode(next_token).decode("utf-8")
                list_params["ContinuationToken"] = decoded

            # List top-level prefixes (document IDs)
            response = self.s3.list_objects_v2(**list_params)

            baselines = []
            for prefix in response.get("CommonPrefixes", []):
                doc_id = prefix["Prefix"].rstrip("/")
                baselines.append(
                    {
                        "document_id": doc_id,
                        "s3_location": f"s3://{baseline_bucket}/{prefix['Prefix']}",
                    }
                )

            result = {"baselines": baselines, "count": len(baselines)}

            # Add next_token if more results available
            if response.get("IsTruncated"):
                import base64

                encoded = base64.b64encode(
                    response["NextContinuationToken"].encode("utf-8")
                ).decode("utf-8")
                result["next_token"] = encoded

            return result

        except Exception as e:
            logger.error(f"Error listing baselines: {e}")
            raise

    def delete_baseline(self, document_id: str) -> Dict:
        """
        Delete evaluation baseline for a document

        Args:
            document_id: Document identifier (S3 key)

        Returns:
            Dictionary with deletion result
        """
        baseline_bucket = self.resources.get("EvaluationBaselineBucket")
        if not baseline_bucket:
            raise ValueError("EvaluationBaselineBucket not found in stack resources")

        try:
            # List all objects for this document
            prefix = f"{document_id}/"
            paginator = self.s3.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=baseline_bucket, Prefix=prefix)

            deleted_count = 0
            for page in pages:
                objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if objects:
                    self.s3.delete_objects(
                        Bucket=baseline_bucket, Delete={"Objects": objects}
                    )
                    deleted_count += len(objects)

            return {"document_id": document_id, "deleted_count": deleted_count}

        except Exception as e:
            logger.error(f"Error deleting baseline: {e}")
            raise
