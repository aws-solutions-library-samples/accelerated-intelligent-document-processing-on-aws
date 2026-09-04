# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""BDA Blueprint Optimizer for optimizing blueprints using ground truth data.

Provides data models for optimization results and a service class that
orchestrates the BDA blueprint optimization lifecycle. Delegates to existing
services for blueprint CRUD, schema transformation, and config management.
"""

import json
import logging
import os
import time
import uuid
from copy import deepcopy
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Optional

import boto3
from pydantic import BaseModel

from idp_common.bda.bda_ocr import build_profile_arn
from idp_common.config.class_names import sanitize_class_name
from idp_common.config.class_settings import carry_forward_authored_settings

if TYPE_CHECKING:
    from idp_common.bda.bda_blueprint_creator import BDABlueprintCreator
    from idp_common.bda.bda_blueprint_service import BdaBlueprintService
    from idp_common.config.configuration_manager import ConfigurationManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Polling configuration constants
# ---------------------------------------------------------------------------
OPTIMIZATION_INITIAL_INTERVAL_SECONDS = 5
OPTIMIZATION_MAX_INTERVAL_SECONDS = 30
OPTIMIZATION_MAX_DURATION_SECONDS = 900  # 15 minutes
OPTIMIZATION_BACKOFF_MULTIPLIER = 2.0


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
class OptimizationStatus(str, Enum):
    """Terminal states for an optimization job."""

    IMPROVED = "improved"
    NO_IMPROVEMENT = "no_improvement"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


class OptimizationMetrics(BaseModel):
    """Accuracy metrics from BDA optimization evaluation.

    Attributes:
        exact_match: Exact-match accuracy score (0.0–1.0).
        f1: F1 accuracy score (0.0–1.0).
        confidence: Confidence score (0.0–1.0).
    """

    exact_match: float = 0.0
    f1: float = 0.0
    confidence: float = 0.0


class OptimizationResult(BaseModel):
    """Result of a blueprint optimization attempt.

    Attributes:
        status: Terminal status of the optimization job.
        improved: Whether the optimization improved extraction accuracy.
        before_metrics: Metrics before optimization (when available).
        after_metrics: Metrics after optimization (when available).
        error_message: Human-readable error description (on failure).
        blueprint_arn: ARN of the created/optimized blueprint.
        optimized_schema: The optimized IDP class schema (when improved).
    """

    status: OptimizationStatus
    improved: bool = False
    before_metrics: Optional[OptimizationMetrics] = None
    after_metrics: Optional[OptimizationMetrics] = None
    error_message: Optional[str] = None
    blueprint_arn: Optional[str] = None
    optimized_schema: Optional[dict] = None


# ---------------------------------------------------------------------------
# Service class (methods added in subsequent tasks 1.2–1.7)
# ---------------------------------------------------------------------------


class BlueprintOptimizer:
    """Orchestrates BDA blueprint optimization using ground truth data.

    Delegates to existing services for all blueprint and config operations:
    - BdaBlueprintService: schema transforms, project management,
      property sanitization.
    - BDABlueprintCreator: blueprint CRUD (create, update, version).
    - ConfigurationManager: class definition updates.
    """

    def __init__(
        self,
        blueprint_service: "BdaBlueprintService",
        blueprint_creator: "BDABlueprintCreator",
        config_manager: "ConfigurationManager",
        s3_client: Any = None,
        bedrock_client: Any = None,
    ) -> None:
        """Initialize BlueprintOptimizer.

        Args:
            blueprint_service: For schema transforms, project management,
                property sanitization.
            blueprint_creator: For blueprint CRUD. Also provides the
                bedrock-data-automation client
                (blueprint_creator.bedrock_client) which is reused for
                optimization API calls.
            config_manager: For class definition updates
                (get_raw_configuration / save_raw_configuration).
            s3_client: Optional S3 client override (for testing).
                Defaults to boto3.client('s3').
            bedrock_client: Optional bedrock-data-automation client
                override (for testing). Defaults to
                blueprint_creator.bedrock_client.
        """
        self.blueprint_service = blueprint_service
        self.blueprint_creator = blueprint_creator
        self.config_manager = config_manager
        self.s3_client = s3_client or boto3.client("s3")
        self.bedrock_client = bedrock_client or blueprint_creator.bedrock_client

        # Lazily resolved on first optimization invocation.
        self._region = os.environ.get("AWS_REGION", "us-east-1")
        self._account_id: Optional[str] = None
        self._profile_arn: Optional[str] = None

    def _get_profile_arn(self) -> str:
        """Return the cached data automation profile ARN.

        Lazily resolves the AWS account ID via STS on first call and
        caches the result for subsequent invocations.

        Returns:
            The ``dataAutomationProfileArn`` string.

        Raises:
            RuntimeError: If the account ID cannot be resolved.
        """
        if self._profile_arn:
            return self._profile_arn

        try:
            identity = boto3.client("sts").get_caller_identity()
        except Exception as e:
            raise RuntimeError(
                f"Cannot resolve AWS account ID for profile ARN: {e}"
            ) from e
        self._account_id = identity.get("Account")

        # Same fix as bda_service: build via the shared helper so the partition
        # is honoured (a hardcoded `arn:aws:` never resolves in GovCloud/China)
        # and ap-* regions get the `apac` geo prefix rather than `ap`. Partition
        # comes from the caller ARN on the SAME identity call above — no extra
        # STS round-trip.
        partition = (identity.get("Arn") or "arn:aws:").split(":")[
            1
        ] or "aws"  # arn-partition-ok: fallback used only to PARSE the partition out
        self._profile_arn = build_profile_arn(
            self._region, self._account_id or "", partition
        )
        return self._profile_arn

    def optimize(
        self,
        class_schema: dict,
        document_key: str,
        ground_truth_key: str,
        bucket: str,
        version: str,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> OptimizationResult:
        """Orchestrate the full BDA blueprint optimization lifecycle.

        Creates a BDA blueprint for the discovered class, triggers the
        BDA optimization API with ground truth data, polls for
        completion, evaluates improvement, and conditionally updates
        the blueprint and IDP class definitions.

        Args:
            class_schema: IDP class JSON Schema for the discovered
                class.
            document_key: S3 key for the discovery PDF document.
            ground_truth_key: S3 key for the ground truth JSON file.
            bucket: S3 bucket containing both objects.
            version: Configuration version name (e.g. 'default').
            status_callback: Optional callback invoked with progress
                messages. Ignored when ``None``.

        Returns:
            An ``OptimizationResult`` describing the outcome.
        """
        _notify = status_callback or (lambda _msg: None)

        # ----------------------------------------------------------
        # 1. Create a BDA blueprint for the discovered class
        # ----------------------------------------------------------
        _notify("Creating BDA blueprint for optimization...")
        try:
            blueprint_arn = self._create_blueprint_for_class(class_schema, version)
        except Exception as e:
            logger.error(f"Blueprint creation failed: {e}", exc_info=True)
            return OptimizationResult(
                status=OptimizationStatus.FAILED,
                error_message=f"Blueprint creation failed: {e}",
            )

        try:
            # ------------------------------------------------------
            # 2. Prepare optimization assets
            # ------------------------------------------------------
            document_s3_uri, ground_truth_s3_uri = self._upload_optimization_assets(
                document_key, ground_truth_key, bucket
            )

            output_s3_uri = f"s3://{bucket}/optimization/output/{uuid.uuid4().hex}"

            # ------------------------------------------------------
            # 3. Invoke the BDA optimization API
            # ------------------------------------------------------
            _notify("Optimizing blueprint with ground truth data...")
            invocation_arn = self._invoke_optimization(
                blueprint_arn,
                document_s3_uri,
                ground_truth_s3_uri,
                output_s3_uri,
            )

            # ------------------------------------------------------
            # 4. Poll until terminal state
            # ------------------------------------------------------
            try:
                poll_response = self._poll_optimization_status(invocation_arn)
            except TimeoutError as te:
                logger.warning(str(te))
                return OptimizationResult(
                    status=OptimizationStatus.TIMED_OUT,
                    error_message=str(te),
                    blueprint_arn=blueprint_arn,
                )

            poll_status = poll_response.get("status", "")

            # ------------------------------------------------------
            # 5. Handle terminal states
            # ------------------------------------------------------
            if poll_status in ("ServiceError", "ClientError"):
                error_msg = poll_response.get(
                    "errorMessage",
                    poll_response.get("errorType", poll_status),
                )
                logger.error(f"Optimization ended with {poll_status}: {error_msg}")
                return OptimizationResult(
                    status=OptimizationStatus.FAILED,
                    error_message=error_msg,
                    blueprint_arn=blueprint_arn,
                )

            # poll_status == "Success"
            result_s3_uri = poll_response["outputConfiguration"]["s3Object"]["s3Uri"]
            optimization_results = self._fetch_optimization_results(result_s3_uri)

            improved, before_metrics, after_metrics = self._evaluate_improvement(
                optimization_results
            )

            if improved:
                optimized_bda_schema = json.loads(
                    optimization_results["evaluationComparison"]["after"][
                        "blueprintSchema"
                    ]
                )
                project_arn = self.blueprint_service.get_or_create_project_for_version(
                    version
                )
                optimized_idp_schema = self._apply_optimized_schema(
                    blueprint_arn,
                    project_arn,
                    optimized_bda_schema,
                    class_schema,
                    version,
                )
                return OptimizationResult(
                    status=OptimizationStatus.IMPROVED,
                    improved=True,
                    before_metrics=before_metrics,
                    after_metrics=after_metrics,
                    blueprint_arn=blueprint_arn,
                    optimized_schema=optimized_idp_schema,
                )

            # No improvement detected
            return OptimizationResult(
                status=OptimizationStatus.NO_IMPROVEMENT,
                improved=False,
                before_metrics=before_metrics,
                after_metrics=after_metrics,
                blueprint_arn=blueprint_arn,
            )

        except Exception as e:
            logger.error(f"Optimization failed: {e}", exc_info=True)
            return OptimizationResult(
                status=OptimizationStatus.FAILED,
                error_message=str(e),
                blueprint_arn=blueprint_arn,
            )

    def _create_blueprint_for_class(self, class_schema: dict, version: str) -> str:
        """Get or create a BDA blueprint for the discovered class.

        First checks if a blueprint already exists for this class in
        the BDA project.  If found, reuses it (updating the schema if
        needed).  Otherwise creates a new one following the standard
        naming convention: ``{STACK_NAME}-{class_name}-{uuid_hex}``.

        Args:
            class_schema: IDP class JSON Schema for the discovered class.
            version: Configuration version name (e.g. 'default').

        Returns:
            The ARN of the existing or newly created BDA blueprint.

        Raises:
            Exception: If project creation, schema transformation, or
                blueprint creation fails.
        """
        project_arn = self.blueprint_service.get_or_create_project_for_version(version)

        class_name = class_schema.get(
            "$id",
            class_schema.get("x-aws-idp-document-type", "Document"),
        )

        # --- Check for an existing blueprint for this class ---
        existing_blueprints = self.blueprint_service._retrieve_all_blueprints(
            project_arn
        )
        existing = self.blueprint_service._blueprint_lookup(
            existing_blueprints, class_name
        )

        if existing:
            blueprint_arn = existing["blueprintArn"]
            logger.info(
                f"Reusing existing blueprint {blueprint_arn} "
                f"('{existing.get('blueprintName')}') for class '{class_name}'"
            )
            return blueprint_arn

        # --- No existing blueprint — create a new one ---
        schema_copy = deepcopy(class_schema)
        self.blueprint_service._sanitize_property_names(schema_copy)
        bda_schema = self.blueprint_service._transform_json_schema_to_bedrock_blueprint(
            schema_copy
        )

        # Sanitized identically to the sync path (and to _blueprint_lookup
        # above), so a class id with a space produces a name BDA accepts and
        # the next lookup still finds it.
        blueprint_name = (
            f"{self.blueprint_service.blueprint_name_prefix}-"
            f"{sanitize_class_name(class_name)}"
            f"-{uuid.uuid4().hex[:8]}"
        )

        result = self.blueprint_creator.create_blueprint(
            document_type="DOCUMENT",
            blueprint_name=blueprint_name,
            schema=json.dumps(bda_schema),
        )

        blueprint_arn = result["blueprint"]["blueprintArn"]

        # Create a blueprint version and associate with the project
        self.blueprint_creator.create_blueprint_version(
            blueprint_arn=blueprint_arn,
            project_arn=project_arn,
        )

        logger.info(
            f"Created optimization blueprint {blueprint_arn} "
            f"for class '{class_name}' in project {project_arn}"
        )
        return blueprint_arn

    def _upload_optimization_assets(
        self,
        document_key: str,
        ground_truth_key: str,
        bucket: str,
    ) -> tuple[str, str]:
        """Prepare S3 URIs for the optimization API.

        Both the discovery document and ground truth JSON are already
        stored in S3.  This method constructs the ``s3://`` URIs that
        the BDA optimization API expects.

        Args:
            document_key: S3 key for the discovery PDF document.
            ground_truth_key: S3 key for the ground truth JSON file.
            bucket: S3 bucket containing both objects.

        Returns:
            A tuple of (document_s3_uri, ground_truth_s3_uri).
        """
        document_s3_uri = f"s3://{bucket}/{document_key}"
        ground_truth_s3_uri = f"s3://{bucket}/{ground_truth_key}"
        logger.info(
            "Optimization assets prepared — "
            f"document: {document_s3_uri}, "
            f"ground_truth: {ground_truth_s3_uri}"
        )
        return document_s3_uri, ground_truth_s3_uri

    def _invoke_optimization(
        self,
        blueprint_arn: str,
        document_s3_uri: str,
        ground_truth_s3_uri: str,
        output_s3_uri: str,
    ) -> str:
        """Invoke the BDA blueprint optimization API.

        Constructs the optimization payload and calls
        ``invoke_blueprint_optimization_async`` on the
        ``bedrock-data-automation`` client.

        Args:
            blueprint_arn: ARN of the BDA blueprint to optimize.
            document_s3_uri: S3 URI of the sample document (PDF).
            ground_truth_s3_uri: S3 URI of the ground truth JSON.
            output_s3_uri: S3 URI prefix for optimization output.

        Returns:
            The ``invocationArn`` for polling the optimization status.

        Raises:
            ClientError: If the BDA optimization API call fails.
        """
        # Use cached profile ARN (lazily resolved on first call)
        profile_arn = self._get_profile_arn()

        payload = {
            "blueprint": {
                "blueprintArn": blueprint_arn,
                "stage": "LIVE",
            },
            "samples": [
                {
                    "assetS3Object": {"s3Uri": document_s3_uri},
                    "groundTruthS3Object": {"s3Uri": ground_truth_s3_uri},
                },
            ],
            "outputConfiguration": {
                "s3Object": {"s3Uri": output_s3_uri},
            },
            "dataAutomationProfileArn": profile_arn,
        }

        logger.info(f"Invoking blueprint optimization for {blueprint_arn}")
        response = self.bedrock_client.invoke_blueprint_optimization_async(**payload)
        invocation_arn = response["invocationArn"]
        logger.info(f"Blueprint optimization started — invocationArn: {invocation_arn}")
        return invocation_arn

    def _poll_optimization_status(self, invocation_arn: str) -> dict:
        """Poll get_blueprint_optimization_status until terminal state.

        Uses exponential backoff starting at 5 s, doubling each
        iteration up to a 30 s cap.  Raises ``TimeoutError`` if the
        optimization does not reach a terminal state within 15 minutes.

        Terminal states: Success, ServiceError, ClientError.
        Non-terminal states: Created, InProgress.

        Args:
            invocation_arn: The invocation ARN returned by
                ``invoke_blueprint_optimization_async``.

        Returns:
            The terminal status response dict from
            ``get_blueprint_optimization_status``.

        Raises:
            TimeoutError: If polling exceeds the maximum duration.
        """
        terminal_states = {"Success", "ServiceError", "ClientError"}
        start_time = time.time()
        iteration = 0

        while True:
            response = self.bedrock_client.get_blueprint_optimization_status(
                invocationArn=invocation_arn,
            )
            status = response.get("status", "")
            logger.info(f"Optimization status for {invocation_arn}: {status}")

            if status in terminal_states:
                return response

            elapsed = time.time() - start_time
            if elapsed >= OPTIMIZATION_MAX_DURATION_SECONDS:
                raise TimeoutError(
                    f"Blueprint optimization timed out after "
                    f"{OPTIMIZATION_MAX_DURATION_SECONDS} seconds "
                    f"(invocationArn={invocation_arn})"
                )

            interval = min(
                OPTIMIZATION_INITIAL_INTERVAL_SECONDS
                * (OPTIMIZATION_BACKOFF_MULTIPLIER**iteration),
                OPTIMIZATION_MAX_INTERVAL_SECONDS,
            )
            time.sleep(interval)
            iteration += 1

    def _fetch_optimization_results(self, output_s3_uri: str) -> dict:
        """Read optimization results from S3 output location.

        After ``get_blueprint_optimization_status`` returns *Success*,
        the evaluation comparison results are stored at the S3 URI from
        ``outputConfiguration.s3Object.s3Uri``.  The URI is a prefix;
        the actual file is ``optimization_results.json`` under it.

        Retries up to 5 times with a 2-second delay between attempts
        to handle S3 eventual consistency delays.

        Args:
            output_s3_uri: The ``s3://bucket/prefix`` URI where the BDA
                optimization API wrote its results.

        Returns:
            The parsed optimization results dict containing
            ``evaluationComparison`` with before/after schemas and
            metrics.

        Raises:
            ClientError: If the S3 read fails after all retries.
            json.JSONDecodeError: If the response is not valid JSON.
        """
        # Parse s3://bucket/prefix from the URI
        uri_without_scheme = output_s3_uri.replace("s3://", "", 1)
        bucket, _, prefix = uri_without_scheme.partition("/")

        # The poll response returns a prefix — the actual results file
        # is optimization_results.json under that prefix.
        key = prefix.rstrip("/") + "/optimization_results.json"

        logger.info(f"Fetching optimization results from s3://{bucket}/{key}")

        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                response = self.s3_client.get_object(Bucket=bucket, Key=key)
                body = response["Body"].read().decode("utf-8")
                return json.loads(body)
            except Exception:
                if attempt < max_retries:
                    logger.warning(
                        f"S3 key not yet available (attempt {attempt}/{max_retries}), "
                        f"retrying in 2s..."
                    )
                    time.sleep(2)
                else:
                    logger.error(
                        f"S3 key still not available after {max_retries} attempts: "
                        f"s3://{bucket}/{key}"
                    )
                    raise

    def _evaluate_improvement(
        self, optimization_result: dict
    ) -> tuple[bool, OptimizationMetrics, OptimizationMetrics]:
        """Evaluate whether optimization improved extraction accuracy.

        Extracts ``aggregateMetrics`` from the before and after
        evaluation results and compares ``exactMatch`` and ``f1``
        scores.

        Args:
            optimization_result: The full optimization results dict
                containing ``evaluationComparison`` with ``before``
                and ``after`` entries.

        Returns:
            A tuple of ``(improved, before_metrics, after_metrics)``
            where *improved* is ``True`` iff the after ``exact_match``
            or ``f1`` score is strictly greater than the corresponding
            before score.
        """
        comparison = optimization_result["evaluationComparison"]

        before_agg = comparison["before"]["aggregateMetrics"]
        after_agg = comparison["after"]["aggregateMetrics"]

        before_metrics = OptimizationMetrics(
            exact_match=before_agg.get("exactMatch", 0.0),
            f1=before_agg.get("f1", 0.0),
            confidence=before_agg.get("confidence", 0.0),
        )
        after_metrics = OptimizationMetrics(
            exact_match=after_agg.get("exactMatch", 0.0),
            f1=after_agg.get("f1", 0.0),
            confidence=after_agg.get("confidence", 0.0),
        )

        improved = (
            after_metrics.exact_match > before_metrics.exact_match
            or after_metrics.f1 > before_metrics.f1
        )

        logger.info(
            f"Optimization evaluation — "
            f"before: exactMatch={before_metrics.exact_match}, "
            f"f1={before_metrics.f1} | "
            f"after: exactMatch={after_metrics.exact_match}, "
            f"f1={after_metrics.f1} | "
            f"improved={improved}"
        )
        return improved, before_metrics, after_metrics

    def _apply_optimized_schema(
        self,
        blueprint_arn: str,
        project_arn: str,
        optimized_bda_schema: dict,
        original_class_schema: dict,
        version: str,
    ) -> dict:
        """Apply the optimized schema to blueprint and class definitions.

        Updates the BDA blueprint with the optimized schema, creates a
        new blueprint version associated with the project, transforms
        the optimized BDA schema back to IDP class format, preserves
        the original class identity fields, and updates the class
        definitions in the configuration.

        Reuses:
        - BDABlueprintCreator.update_blueprint() for blueprint update
        - BDABlueprintCreator.create_blueprint_version() for version
          creation + project association
        - BdaBlueprintService.transform_bda_blueprint_to_idp_class_schema()
          for BDA→IDP transform
        - ConfigurationManager.get_raw_configuration() +
          save_raw_configuration() for class definition update
          (follows the same pattern as
          ClassesDiscovery._merge_and_save_class)

        Args:
            blueprint_arn: ARN of the BDA blueprint to update.
            project_arn: ARN of the BDA project to associate the
                new version with.
            optimized_bda_schema: The optimized BDA blueprint schema
                dict from the optimization results.
            original_class_schema: The original IDP class schema,
                used to preserve ``$id`` and
                ``x-aws-idp-document-type``.
            version: Configuration version name (e.g. 'default').

        Returns:
            The updated IDP class schema with preserved identity
            fields.

        Raises:
            Exception: If blueprint update, version creation, or
                config save fails.
        """
        # 1. Update the blueprint with the optimized schema
        self.blueprint_creator.update_blueprint(
            blueprint_arn, "DEVELOPMENT", json.dumps(optimized_bda_schema)
        )
        logger.info(f"Updated blueprint {blueprint_arn} with optimized schema")

        # 2. Create a new blueprint version and associate with project
        self.blueprint_creator.create_blueprint_version(blueprint_arn, project_arn)
        logger.info(
            f"Created blueprint version for {blueprint_arn} in project {project_arn}"
        )

        # 3. Transform optimized BDA schema back to IDP class format
        idp_schema = self.blueprint_service.transform_bda_blueprint_to_idp_class_schema(
            optimized_bda_schema
        )

        # 4. Preserve original $id and x-aws-idp-document-type
        original_id = original_class_schema.get("$id")
        original_doc_type = original_class_schema.get("x-aws-idp-document-type")
        if original_id is not None:
            idp_schema["$id"] = original_id
        if original_doc_type is not None:
            idp_schema["x-aws-idp-document-type"] = original_doc_type

        # 5. Update class definitions following _merge_and_save_class
        class_id = idp_schema.get("$id") or idp_schema.get("x-aws-idp-document-type")
        logger.info(f"Updating class definition '{class_id}' in version '{version}'")

        existing_config = (
            self.config_manager.get_raw_configuration("Config", version=version) or {}
        )
        existing_classes = list(existing_config.get("classes", []))

        classes_by_id: dict[str, dict] = {}
        for cls in existing_classes:
            cls_id = cls.get("$id") or cls.get("x-aws-idp-document-type")
            if cls_id:
                classes_by_id[cls_id] = cls

        if class_id:
            # Carry forward the class-level settings the BDA transform does not
            # produce (extraction/escalation models, confidence thresholds,
            # classification regexes, multi-instance, few-shot examples).
            # Assigning the transformed schema straight over the existing class
            # erased all of them, and the loss only showed up in the next
            # document processed.
            existing_class = classes_by_id.get(class_id)
            if existing_class is not None:
                carry_forward_authored_settings(existing_class, idp_schema)
            classes_by_id[class_id] = idp_schema

        existing_config["classes"] = list(classes_by_id.values())
        self.config_manager.save_raw_configuration(
            "Config", existing_config, version=version
        )
        logger.info(
            f"Saved updated class definition '{class_id}' to version '{version}'"
        )

        return idp_schema
