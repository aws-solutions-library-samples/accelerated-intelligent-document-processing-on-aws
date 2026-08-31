# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Test Execution Aggregation Lambda Function.

Aggregates evaluation metrics for test runs using Stickler's bulk evaluator.
This function is invoked by the TestResultsResolver to offload heavy Stickler processing.
"""

import json
import logging
import os
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

# R14 graded packet metrics forwarded from each doc's ``doc_split_metrics``.
# Order matches ``idp_common.evaluation.models.DocSplitMetrics`` so any
# additions there are noticed here (mismatch drops the new field from
# aggregation rather than crashing). All values are in [0.0, 1.0].
_GRADED_PACKET_KEYS = (
    "final_score",
    "clustering_score",
    "v_measure",
    "rand_index",
    "avg_ordering_score",
)

# Ceiling on the per-section classification errors carried onto the run record.
# The whole aggregation lands in ONE DynamoDB attribute (testRunResult), so an
# unbounded list is a 400KB item limit waiting to be hit: a 500-document run
# where the config is wrong for a whole class produces thousands of entries.
# Truncation is reported rather than silent — see ``_collect_classification_errors``.
MAX_CLASSIFICATION_ERRORS = 200


def _classification_errors_for_doc(
    doc_key: str, doc_split_metrics: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Per-section classification mismatches for one document.

    ``section_details_with_order`` is already computed for every evaluated
    document and serialized into its results.json, but nothing forwarded it to
    the run level — so a misclassified document was invisible in Test Studio
    except as a dip in an aggregate percentage. Extraction runs against the
    wrong schema can be *confidently* wrong, so such a document can also rank
    low-priority by alert count in the annotation queue and never be opened.

    Three kinds, kept apart because they mean different things and imply
    different fixes:

    * ``class`` — the wrong document class. Extraction ran the wrong schema.
    * ``unmatched`` — a ground-truth section with no predicted counterpart at
      all, which is a splitting failure rather than a labelling one.
    * ``order`` — right class and right pages, wrong page order. Cosmetic for
      extraction, but it is what ``split_accuracy_with_order`` penalises, so
      conflating it with ``class`` would misdirect whoever is debugging.
    """
    errors: List[Dict[str, Any]] = []
    for section in doc_split_metrics.get("section_details_with_order") or []:
        if not isinstance(section, dict):
            continue
        expected = section.get("ground_truth_class")
        predicted = section.get("predicted_class")

        if predicted in (None, ""):
            kind = "unmatched"
        elif expected != predicted:
            kind = "class"
        elif section.get("order_matched") is False:
            kind = "order"
        else:
            continue

        errors.append(
            {
                "doc_key": doc_key,
                "section_id": section.get("section_id"),
                "kind": kind,
                "expected_class": expected,
                "predicted_class": predicted,
                "expected_pages": section.get("ground_truth_pages") or [],
                "predicted_pages": section.get("predicted_pages") or [],
            }
        )
    return errors


def _collect_classification_errors(
    per_doc_errors: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Flatten per-document mismatches into the run-level payload.

    Class errors sort first: a wrong schema is what makes a document's whole
    extraction meaningless, so it must not be pushed past the cap by a run
    full of page-order nits. ``total`` counts everything found, so the UI can
    say "showing 200 of 340" rather than implying 200 is all there was.
    """
    ordering = {"class": 0, "unmatched": 1, "order": 2}
    flat = [e for errors in per_doc_errors.values() for e in errors]
    flat.sort(key=lambda e: (ordering.get(e["kind"], 9), e["doc_key"] or ""))

    kept = flat[:MAX_CLASSIFICATION_ERRORS]
    if len(flat) > len(kept):
        logger.warning(
            "Truncating classification errors for this run: keeping %d of %d "
            "(class errors first). The UI reports the total.",
            len(kept),
            len(flat),
        )
    return {
        "errors": kept,
        "total": len(flat),
        "documents_affected": len(per_doc_errors),
        "truncated": len(flat) > len(kept),
    }


# Process-wide LRU dedupe for the parent-bucket-collision warning. The
# aggregation Lambda runs once per test run; a stable scalar-vs-structured
# schema collision would otherwise fire the warning on every run in a
# warm container. LRU (not a hard cap) so a container that sees many
# DIFFERENT collision sets over its lifetime still logs each fresh one.
_SEEN_COLLISION_MAX = 128
_seen_collision_names: "OrderedDict[tuple, None]" = OrderedDict()
_seen_collision_lock = threading.Lock()


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lambda handler for test execution aggregation.

    Args:
        event: Lambda event containing test_run_id
        context: Lambda context

    Returns:
        Dictionary with aggregated metrics
    """
    try:
        test_run_id = event.get("test_run_id")
        tracking_table_name = os.environ.get("TRACKING_TABLE")

        if not test_run_id:
            raise ValueError("Missing required parameter: test_run_id")

        if not tracking_table_name:
            raise ValueError("TRACKING_TABLE environment variable not set")

        logger.info(f"Aggregating test run: {test_run_id}")

        result = aggregate_test_run_with_stickler(test_run_id, tracking_table_name)

        # Calculate average weighted score from document-level scores
        weighted_scores = result.get("weighted_overall_scores", {})
        avg_weighted_score = None
        if weighted_scores:
            scores = [score for score in weighted_scores.values() if score is not None]
            if scores:
                avg_weighted_score = sum(scores) / len(scores)

        # Format avg_weighted_score
        avg_weighted_score_str = (
            f"{avg_weighted_score:.4f}" if avg_weighted_score is not None else "N/A"
        )

        logger.info(
            f"Aggregation completed for test run: {test_run_id}, "
            f"document_count={result.get('document_count', 0)}, "
            f"overall_accuracy={result.get('overall_accuracy')}, "
            f"avg_weighted_score={avg_weighted_score_str}"
        )

        _record_confidence_curve(test_run_id, tracking_table_name, result)

        return {"statusCode": 200, "body": json.dumps(result)}

    except Exception as e:
        logger.error(f"Error in test execution aggregation: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e), "metrics": _empty_metrics()}),
        }


def _drafting_config_versions(table, test_set_id: str, exclude_run_id: str) -> set:
    """Config versions that produced any of this set's draft labels.

    Labeling-job items are per-run (SK ``labeljob#{runId}``) and the job id *is* a
    test run id, so each one's run record carries the version the runner resolved —
    no extra field to keep in sync. Returns an empty set if the jobs cannot be read,
    which makes the caller's guard fail *open*: a curve that misses a refusal is
    recoverable (reset and rebuild), whereas dropping every observation on a
    transient read error would silently stop the estimator from ever measuring.
    """
    versions = set()
    try:
        start_key = None
        for _ in range(20):
            kwargs = {
                "KeyConditionExpression": Key("PK").eq(f"testset#{test_set_id}")
                & Key("SK").begins_with("labeljob#"),
            }
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            page = table.query(**kwargs)
            for job in page.get("Items") or []:
                run_id = job.get("jobId")
                if not run_id or run_id == exclude_run_id:
                    continue
                version = (
                    table.get_item(
                        Key={"PK": f"testrun#{run_id}", "SK": "metadata"}
                    ).get("Item")
                    or {}
                ).get("ConfigVersion")
                if version:
                    versions.add(version)
            start_key = page.get("LastEvaluatedKey")
            if not start_key:
                break
    except Exception as e:  # noqa: BLE001 — see docstring: guard fails open
        logger.warning(f"Could not resolve drafting configs for {test_set_id}: {e}")
    return versions


def _record_confidence_curve(
    test_run_id: str, tracking_table_name: str, result: Dict[str, Any]
) -> None:
    """Fold this scoring run's calibration into the test set's confidence curve.

    A scoring run is the highest-fidelity source the review-effort estimator has:
    it measures correctness across the *whole* confidence range, including the
    high-confidence zone that worst-first human review never reaches. Only after
    such a run can the estimate honestly report itself as "measured".

    The ECE bins computed above already are the reliability table the curve
    stores, so this reuses them rather than recomputing anything.

    Best-effort: the curve is an optimization for a different feature, and
    failing to update it must not fail the aggregation the dashboard depends on.
    """
    try:
        bins = (
            ((result or {}).get("confidence_metrics") or {})
            .get("overall", {})
            .get("ece", {})
            .get("bins")
        )
        if not bins:
            logger.info(
                "No ECE bins in aggregation result; skipping confidence-curve update"
            )
            return

        table = dynamodb.Table(tracking_table_name)
        run = (
            table.get_item(Key={"PK": f"testrun#{test_run_id}", "SK": "metadata"}).get(
                "Item"
            )
            or {}
        )
        test_set_id = run.get("TestSetId")
        if not test_set_id:
            return  # Not a test-set run — nothing to attribute the curve to.

        # A run scored against labels the same config drafted measures the config
        # against itself: extraction reproduces the drafted baseline almost exactly,
        # the bins fold in as near-perfect accuracy, and the set can report "99%
        # accuracy, measured on this test set" for labels no human ever checked.
        # That is precisely the false confidence the quality tiers exist to prevent,
        # so these observations are refused rather than recorded.
        run_config = run.get("ConfigVersion")
        test_set = (
            table.get_item(Key={"PK": f"testset#{test_set_id}", "SK": "metadata"}).get(
                "Item"
            )
            or {}
        )
        # Every labeling job, not just the set's current pointer: a one-document
        # re-extract repoints it, so reading only the newest job would let a run
        # score the config that drafted the *other* 199 documents.
        drafted_by = _drafting_config_versions(table, test_set_id, test_run_id)
        if test_set.get("labelState") == "draft" and run_config in drafted_by:
            logger.info(
                f"Skipping confidence-curve update for {test_set_id}: run scored "
                f"config '{run_config}' against labels that config drafted, which "
                "would measure the config against itself"
            )
            return

        from idp_common.evaluation.curve_store import CurveStore

        accepted = CurveStore(table).add_ece_bins(
            test_set_id, bins, config_version=run_config
        )
        logger.info(
            f"Recorded {accepted} confidence-curve observation(s) for test set "
            f"{test_set_id} from scoring run {test_run_id}"
        )
    except Exception as e:  # noqa: BLE001 — must not fail aggregation
        logger.warning(
            f"Could not update confidence curve for test run {test_run_id}: {e}",
            exc_info=True,
        )


def aggregate_test_run_with_stickler(
    test_run_id: str, tracking_table_name: str
) -> Dict[str, Any]:
    """
    Aggregate evaluation metrics for a test run using Stickler's bulk evaluator.

    Args:
        test_run_id: Test run identifier (batch ID prefix)
        tracking_table_name: DynamoDB tracking table name

    Returns:
        Dictionary with aggregated metrics matching the existing format
    """
    # Load Stickler comparison results from S3
    (
        comparison_results,
        doc_weighted_scores,
        doc_graded_packet_scores,
        excluded_doc_keys,
        doc_classification_errors,
    ) = _load_comparison_results(test_run_id, tracking_table_name)

    if not comparison_results:
        logger.warning(f"No comparison results found for test run: {test_run_id}")
        empty = _empty_metrics()
        # Even with no per-section extraction comparisons we may still have
        # graded packet metrics (they come from doc_split, which runs before
        # extraction evaluation and survives an all-empty section pass). Fold
        # them in so the UI can still render classification-only runs.
        if doc_graded_packet_scores:
            empty["graded_packet_metrics"] = _aggregate_graded_packet_metrics(
                doc_graded_packet_scores
            )
        empty["excluded_documents"] = excluded_doc_keys
        empty["excluded_document_count"] = len(excluded_doc_keys)
        # Same reasoning as graded_packet_metrics above, and this is the path
        # that matters most for classification errors: a run whose classes are
        # all wrong can produce no usable section comparisons at all, so the
        # only thing left to report IS the misclassification.
        empty["classification_errors"] = _collect_classification_errors(
            doc_classification_errors
        )
        return empty

    # Use Stickler's bulk aggregator
    try:
        from stickler.structured_object_evaluator.bulk_structured_model_evaluator import (
            BulkStructuredModelEvaluator,
            aggregate_from_comparisons,
        )
        from stickler.structured_object_evaluator.models.confidence import (
            AUROCMetric,
            BrierScoreMetric,
            ECEMetric,
            ErrorCaptureAtBudgetMetric,
        )

        # Stickler consumers should not see our ``_idp_source`` sentinel —
        # build an untagged view for every Stickler call (finding from
        # #625 high review — passing the tagged dicts directly to
        # ``aggregate_from_comparisons`` and the two
        # ``BulkStructuredModelEvaluator`` instances would surface the
        # sentinel if any future Stickler version iterates SCR top-level
        # keys). ``_run_level_row_aggregates`` (our code) reads
        # ``_idp_source`` from the ORIGINAL tagged list below.
        untagged_comparisons = [
            {k: v for k, v in scr.items() if k != "_idp_source"}
            for scr in comparison_results
        ]

        process_eval = aggregate_from_comparisons(untagged_comparisons)

        logger.info(
            f"Stickler aggregation complete: document_count={process_eval.document_count}, comparison_results={len(comparison_results)}, weighted_scores={len(doc_weighted_scores)}"
        )

        # Replace the sklearn confidence post-pass with a Stickler accumulator
        # subclass that collapses list-index paths (LineItems[0].Rate →
        # LineItems.Rate) BEFORE Stickler's ConfidenceCalculator sees them.
        # ECARB uses the SAME subclass in a second evaluator so its per-field
        # keys collapse identically — otherwise the downstream merge (keyed
        # on field_name) would land ECARB values under indexed buckets the UI
        # never looks up. Two evaluators (not one with two accumulators)
        # because both accumulators emit under the same ``confidence_metrics``
        # name and would collide inside a single BulkStructuredModelEvaluator.
        # Both passes are cheap — one iteration over
        # ``update_from_comparison_result`` each.
        confidence_metrics = None
        try:
            evaluator = BulkStructuredModelEvaluator(
                accumulators=[
                    _IndexCollapsingConfidenceAccumulator(
                        metrics=[AUROCMetric(), ECEMetric(), BrierScoreMetric()]
                    )
                ]
            )
            for comp_result in untagged_comparisons:
                evaluator.update_from_comparison_result(comp_result)
            confidence_metrics = evaluator.compute().confidence_metrics
            # Stickler's BrierScoreMetric emits under key ``brier_score``; the
            # deleted sklearn post-pass wrote under ``brier`` and the Test Studio
            # UI (TestResults.tsx, TestComparison.tsx) + awsjson-types.ts still
            # read ``brier``. Rename in-place at the aggregation boundary so
            # the UI keeps rendering Brier Score without touching 8 UI sites.
            confidence_metrics = _rename_brier_score_key(confidence_metrics)
        except Exception as e:
            logger.warning(
                f"Failed to compute pattern-collapsed confidence metrics: {e}",
                exc_info=True,
            )

        ecab_metrics = None
        try:
            # Use the SAME index-collapsing accumulator subclass so ECARB's
            # per-field keys (LineItems[N].Rate → LineItems.Rate) match the
            # primary confidence-metrics keys. Otherwise the ECARB merge in
            # _transform_stickler_metrics — which keys on ``field_name`` —
            # would land indexed keys under buckets the UI never looks up
            # (UI reads pattern keys like ``LineItems.Rate``).
            ecab_evaluator = BulkStructuredModelEvaluator(
                accumulators=[
                    _IndexCollapsingConfidenceAccumulator(
                        metrics=[ErrorCaptureAtBudgetMetric(budgets=[0.30])]
                    )
                ]
            )
            for comp_result in untagged_comparisons:
                ecab_evaluator.update_from_comparison_result(comp_result)
            ecab_metrics = ecab_evaluator.compute().confidence_metrics

            if ecab_metrics and "overall" in ecab_metrics:
                ecab_30 = (
                    ecab_metrics.get("overall", {})
                    .get("error_capture_at_budget", {})
                    .get("budgets", {})
                    .get("0.30", {})
                )
                if ecab_30:
                    logger.info(
                        f"ECARB@30: catch {ecab_30.get('pct_errors_caught', 0) * 100:.0f}% "
                        f"of errors with {ecab_30.get('gain', 0):.1f}x gain vs random"
                    )
        except Exception as e:
            logger.warning(f"Failed to compute ECAB metrics: {e}", exc_info=True)

        # Replace process_eval.confidence_metrics with the pattern-collapsed
        # version — process_eval only has per-doc metrics for scalar
        # top-level fields.
        if confidence_metrics is not None:
            process_eval.confidence_metrics = confidence_metrics

        # Transform to IDP format (split metrics will be added by caller from Athena)
        transformed = _transform_stickler_metrics(
            process_eval, doc_weighted_scores, comparison_results, ecab_metrics
        )
        # Fold in run-level graded packet metrics (R14). Same idiom as
        # weighted_overall_scores: simple mean across documents plus the
        # per-document map. Emitted here (not in _transform_stickler_metrics)
        # so the graded-packet plumbing stays independent of Stickler's
        # confusion-matrix path — a Stickler shape change won't collide with
        # this field, and the field won't appear at all when the aggregation
        # itself returned nothing.
        transformed["graded_packet_metrics"] = _aggregate_graded_packet_metrics(
            doc_graded_packet_scores
        )
        transformed["excluded_documents"] = excluded_doc_keys
        transformed["excluded_document_count"] = len(excluded_doc_keys)
        transformed["classification_errors"] = _collect_classification_errors(
            doc_classification_errors
        )
        return transformed

    except Exception as e:
        logger.error(
            f"Stickler aggregation failed for {test_run_id}: {e}", exc_info=True
        )
        # Fold in ``excluded_doc_keys`` and ``graded_packet_metrics``
        # even on the Stickler-aggregation-failure path — the sibling
        # ``if not comparison_results:`` branch above already does
        # this, and dropping them here silently loses the doc-key info
        # operators need to investigate the failure (finding from
        # #625 self-review — the failure path was inconsistent with
        # the empty-input path on the same fields).
        empty = _empty_metrics()
        if doc_graded_packet_scores:
            empty["graded_packet_metrics"] = _aggregate_graded_packet_metrics(
                doc_graded_packet_scores
            )
        empty["excluded_documents"] = excluded_doc_keys
        empty["excluded_document_count"] = len(excluded_doc_keys)
        return empty


def _load_comparison_results(
    test_run_id: str, tracking_table_name: str
) -> tuple[
    List[Dict[str, Any]],
    Dict[str, float],
    Dict[str, Dict[str, float]],
    List[str],
    Dict[str, List[Dict[str, Any]]],
]:
    """
    Load all Stickler comparison results for documents in a test run.

    Args:
        test_run_id: Test run identifier (batch ID prefix)
        tracking_table_name: DynamoDB tracking table name

    Returns:
        Tuple of ``(comparison_results, doc_weighted_scores,
        doc_graded_packet_scores, excluded_doc_keys, doc_classification_errors)``.
        ``doc_graded_packet_scores`` maps doc_key → the graded packet metrics
        dict (``{final_score, clustering_score, v_measure, rand_index,
        avg_ordering_score}``) computed per document by
        ``compute_graded_packet_metrics``. Docs written before R14 landed —
        and docs whose gt/pred pages never overlap so ``evaluate_packet``
        can't produce scores — are absent from the map.
        ``excluded_doc_keys`` lists documents whose every section was a
        scoring no-op (class has no extractable schema); they contribute no
        weighted score and are surfaced to the UI as an "excluded" count.
        ``doc_classification_errors`` maps doc_key → the sections whose predicted
        class disagreed with ground truth, which the run-level Classification errors
        panel reads.
    """
    table = dynamodb.Table(tracking_table_name)
    output_bucket = os.environ.get("OUTPUT_BUCKET")

    if not output_bucket:
        logger.error("OUTPUT_BUCKET environment variable not set")
        # Five elements, matching the annotation and the success path below. This
        # returned four for a while after ``doc_classification_errors`` was added: the
        # happy path and the caller's unpack were both updated and this was not, so the
        # only route through here — a misconfigured function — raised ValueError on the
        # unpack instead of logging and degrading. Nothing caught it because no test
        # exercised the path.
        return [], {}, {}, [], {}

    # Scan for all documents matching the test run prefix
    comparison_results = []
    doc_weighted_scores = {}
    doc_graded_packet_scores: Dict[str, Dict[str, float]] = {}
    doc_classification_errors: Dict[str, List[Dict[str, Any]]] = {}

    # Use scan with filter on PK to select only document records for this test run
    response = table.scan(
        FilterExpression="begins_with(PK, :pk_prefix)",
        ExpressionAttributeValues={":pk_prefix": f"doc#{test_run_id}"},
    )

    items = response.get("Items", [])

    # Handle pagination
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression="begins_with(PK, :pk_prefix)",
            ExpressionAttributeValues={":pk_prefix": f"doc#{test_run_id}"},
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    logger.info(f"Found {len(items)} documents for test run {test_run_id}")

    # Filter for completed documents
    docs_to_load = []
    for item in items:
        doc_key = item.get("ObjectKey")
        if not doc_key:
            continue

        eval_status = item.get("EvaluationStatus")
        if eval_status != "COMPLETED":
            logger.debug(f"Skipping document {doc_key} with status {eval_status}")
            continue

        docs_to_load.append(doc_key)

    logger.info(f"Loading {len(docs_to_load)} completed documents in parallel")

    # Load S3 results in parallel using ThreadPoolExecutor
    # Use max 20 workers to balance parallelism with Lambda memory/network limits
    max_workers = min(20, len(docs_to_load)) if docs_to_load else 1

    # Rate-limit the version-drift warning: log ONCE per unique payload
    # version per run rather than once per document. On a rolling deploy over
    # a large historical test set, per-payload logging floods CloudWatch and
    # drowns any legitimate future drift signal (finding 10 from #625
    # adversarial review).
    _seen_payload_versions: set[str] = set()
    _version_warning_lock = threading.Lock()

    def load_document_results(doc_key):
        """Load and parse a single document's evaluation results.

        Uses ``idp_common.evaluation.contract`` for both the S3 key template
        and the ``STICKLER_RESULT_VERSION`` shape stamp — the writer
        (EvaluationService) stamps the same constant into each payload, so a
        future shape change fails loudly here rather than as wrong dashboard
        numbers.
        """
        from idp_common.evaluation.contract import (
            STICKLER_RESULT_VERSION,
            evaluation_results_key,
        )

        eval_results_uri = f"s3://{output_bucket}/{evaluation_results_key(doc_key)}"
        try:
            eval_data = _load_s3_json(eval_results_uri)

            # Version-stamp check: the writer stamps STICKLER_RESULT_VERSION
            # onto each results.json; if the payload carries a different
            # version, log a warning (once per unique payload version per
            # run) so a shape change surfaces before it corrupts aggregation
            # output. Missing stamp (old payload) is tolerated — soft gate.
            # Coerce to string for the comparison — a payload deserialized
            # from JSON where the writer omitted quotes around the version
            # (e.g. ``"stickler_result_version": 2.0``) would land as a
            # float here; ``float(2.0) != str("2.0")`` always fires and
            # spams the very warning this gate exists to prevent.
            raw_version = eval_data.get("stickler_result_version")
            payload_version = (
                str(raw_version) if raw_version is not None else None
            )
            if (
                payload_version is not None
                and payload_version != STICKLER_RESULT_VERSION
            ):
                should_log = False
                with _version_warning_lock:
                    if payload_version not in _seen_payload_versions:
                        _seen_payload_versions.add(payload_version)
                        should_log = True
                if should_log:
                    logger.warning(
                        "stickler_result_version mismatch: payload=%r "
                        "expected=%r. Blob shape may have drifted. First "
                        "example: %s. Further payloads with this version are "
                        "not logged individually.",
                        payload_version,
                        STICKLER_RESULT_VERSION,
                        eval_results_uri,
                    )

            section_results = eval_data.get("section_results", [])

            # Extract comparison results from sections. Tag each SCR with the
            # parent doc_key + section_id so run-level aggregation can locate
            # warnings back to a specific document (finding 6 from round-4
            # review — the previous ``idx=N`` fallback labeled the section
            # index, not the document). Shallow-copy first so the ``eval_data``
            # dict loaded from S3 (transient — discarded on function return)
            # is not mutated with our sentinel key.
            #
            # NOTE: only ``_run_level_row_aggregates`` (our own code) reads
            # the tagged list — the aggregator builds an ``untagged_comparisons``
            # view for every Stickler call site (``aggregate_from_comparisons``
            # and both ``BulkStructuredModelEvaluator.update_from_comparison_result``
            # loops) so the ``_idp_source`` sentinel never reaches Stickler.
            # The namespace prefix is still there so if the untagged view is
            # ever dropped by mistake, the key can't collide with a real
            # Stickler-owned field name.
            doc_comparisons = []
            for section in section_results:
                stickler_result = section.get("stickler_comparison_result")
                if stickler_result:
                    tagged = dict(stickler_result)
                    tagged["_idp_source"] = {
                        "doc_key": doc_key,
                        "section_id": section.get("section_id"),
                    }
                    doc_comparisons.append(tagged)

            # Extract weighted score. Docs whose sections were all no-ops
            # (no extractable schema for the class) emit ``None`` for the
            # document-level weighted score; the aggregator surfaces those as
            # ``excluded`` so the UI can show a count tile instead of a
            # spurious 0.0 bar in the histogram.
            weighted_score = None
            excluded_from_scoring = False
            if section_results:
                overall_metrics = eval_data.get("overall_metrics", {}) or {}
                weighted_score = overall_metrics.get("weighted_overall_score")
                excluded_from_scoring = bool(
                    overall_metrics.get("evaluation_excluded") or weighted_score is None
                )
            else:
                # Empty section_results — a payload that made it to disk
                # but produced no evaluable sections at all. Treat as an
                # excluded doc so it's visible in the UI's "excluded"
                # count tile rather than silently vanishing from both the
                # weighted-scores map AND the excluded list (a bug the
                # earlier code had: this doc looked identical to a load
                # failure, so operators couldn't tell what had happened).
                excluded_from_scoring = True

            # R14 graded packet metrics (V-measure / Rand / ordering) computed
            # per-doc by ``compute_graded_packet_metrics`` and serialized into
            # ``doc_split_metrics``. Only forward the five graded fields — the
            # rest of ``doc_split_metrics`` is the exact-match plumbing that
            # Athena already aggregates via ``split_classification_metrics``.
            # ``None`` values (older payloads, or payloads where
            # ``evaluate_packet`` returned no rows) are dropped so downstream
            # averaging never sees mixed-type entries.
            doc_split_metrics = eval_data.get("doc_split_metrics") or {}
            # ``bool`` is a subclass of ``int`` in Python, so plain
            # ``isinstance(x, (int, float))`` accepts True/False and would
            # average them into the graded score as 1.0 / 0.0. Explicitly
            # reject bool so a payload accidentally carrying a boolean at
            # one of the graded keys doesn't poison the mean.
            graded_scores = {
                key: doc_split_metrics[key]
                for key in _GRADED_PACKET_KEYS
                if isinstance(doc_split_metrics.get(key), (int, float))
                and not isinstance(doc_split_metrics.get(key), bool)
            }

            return {
                "doc_key": doc_key,
                "comparisons": doc_comparisons,
                "weighted_score": weighted_score,
                "excluded_from_scoring": excluded_from_scoring,
                "graded_scores": graded_scores,
                # The per-section detail behind the split-accuracy percentages.
                # Same payload the graded scores come from; previously dropped.
                "classification_errors": _classification_errors_for_doc(
                    doc_key, doc_split_metrics
                ),
                "success": True,
            }
        except Exception as e:
            logger.warning(
                f"Failed to load evaluation results from {eval_results_uri}: {e}"
            )
            return {"doc_key": doc_key, "success": False}

    excluded_doc_keys: List[str] = []

    # Execute parallel S3 loads
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(load_document_results, doc_key): doc_key
            for doc_key in docs_to_load
        }

        for future in as_completed(futures):
            result = future.result()
            if result["success"]:
                comparison_results.extend(result["comparisons"])
                if result["weighted_score"] is not None:
                    doc_weighted_scores[result["doc_key"]] = result["weighted_score"]
                elif result.get("excluded_from_scoring"):
                    # Doc had section results but every one was a no-op — keep
                    # it visible as "excluded" so the UI can distinguish this
                    # from a load failure.
                    excluded_doc_keys.append(result["doc_key"])
                if result.get("graded_scores"):
                    doc_graded_packet_scores[result["doc_key"]] = result[
                        "graded_scores"
                    ]
                if result.get("classification_errors"):
                    doc_classification_errors[result["doc_key"]] = result[
                        "classification_errors"
                    ]

    logger.info(
        f"Loaded {len(comparison_results)} comparison results for test run {test_run_id}"
    )
    logger.info(
        f"Loaded {len(doc_weighted_scores)} weighted scores for test run {test_run_id}"
    )
    logger.info(
        f"Loaded graded packet metrics for {len(doc_graded_packet_scores)} documents "
        f"for test run {test_run_id}"
    )
    if excluded_doc_keys:
        logger.info(
            f"{len(excluded_doc_keys)} document(s) excluded from scoring for test "
            f"run {test_run_id} (no extractable schema for any section)"
        )
    if doc_classification_errors:
        logger.info(
            f"{len(doc_classification_errors)} document(s) have classification "
            f"mismatches for test run {test_run_id}"
        )
    return (
        comparison_results,
        doc_weighted_scores,
        doc_graded_packet_scores,
        excluded_doc_keys,
        doc_classification_errors,
    )


def _load_s3_json(s3_uri: str) -> Dict[str, Any]:
    """Load JSON content from S3 URI."""
    if not s3_uri.startswith("s3://"):
        raise ValueError(f"Invalid S3 URI: {s3_uri}")

    parts = s3_uri[5:].split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""

    response = s3_client.get_object(Bucket=bucket, Key=key)
    content = response["Body"].read().decode("utf-8")
    return json.loads(content)


def _run_level_counts_from_rows(
    comparison_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute run-level top-level metrics by classifying every ``field_comparisons``
    row across every document. Thin wrapper around
    ``_run_level_row_aggregates`` — that helper is the single source of
    truth so the two views cannot drift (finding from #625 high review —
    three near-identical implementations of the same aggregation would
    diverge silently on any future update).

    ``aggregate_from_comparisons(...).metrics`` returns an item-level rollup
    for list fields (Stickler's ``cm.overall`` semantics), which hides
    leaf-level failures inside Hungarian-paired items — a document with 80%
    of its list values wrong reports 100% precision at the run level (issue
    #625). ``field_comparisons`` is the only Stickler view that stays
    honest about every leaf.

    Returns a dict with the same keys the caller reads from
    ``process_eval.metrics``: ``tp``, ``fa``, ``fd``, ``fp``, ``tn``, ``fn``,
    ``cm_precision``, ``cm_recall``, ``cm_f1``, ``cm_accuracy``.
    """
    top, _ = _run_level_row_aggregates(comparison_results)
    return top


# Regex shared with ``_IndexCollapsingConfidenceAccumulator._INDEX_RE`` below
# so per-field metrics and confidence metrics bucket on IDENTICAL keys. Matches
# both digit-indexed ``[3]`` and bare-bracket ``[]`` (Stickler emits the
# latter for extras / hallucinated items). A previous digits-only pattern
# left ``Items[]`` as a stray bucket alongside ``Items.name`` (round-2
# adversarial review) and diverged from the confidence accumulator's pattern
# despite comments claiming they agreed (round-4 review).
_INDEX_TOKEN_RE = re.compile(r"\[[^\]]*\]")


def _collapse_indices(path: str) -> str:
    """Strip ``[…]`` list-index tokens from a Stickler field path.

    ``Items[3].name`` → ``Items.name``. See ``_INDEX_TOKEN_RE`` above.
    """
    return _INDEX_TOKEN_RE.sub("", path)


def _run_level_field_metrics_from_rows(
    comparison_results: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Per-field breakdown derived from rows, bucketed by index-collapsed
    path. Thin wrapper around ``_run_level_row_aggregates`` — that helper
    is the single source of truth for both top-level and per-field views
    so they cannot drift.

    ``process_eval.field_metrics`` (Stickler's ``cm.fields.X.overall``) uses
    the same item-level rollup that hides leaves inside kept items — so on
    the ``partial_list_failure`` fixture, the top-level says precision 0.20
    while ``Items.name`` under per-field says 1.00 (finding 3 from #625
    adversarial review — run-level dashboard self-contradicts).

    Item-level rows (rejected/missing/extra items) are attributed one count
    per leaf of the item's structure so a dropped 2-leaf item contributes
    2 fn to Items.name + Items.amount (matching top-level's leaf-normalized
    counts), not a single fn to Items.
    """
    _, per_field = _run_level_row_aggregates(comparison_results)
    return per_field


def _run_level_row_aggregates(
    comparison_results: List[Dict[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Single-pass variant of the two ``_run_level_*_from_rows`` helpers.

    ``_transform_stickler_metrics`` needs BOTH the top-level counts and the
    per-field breakdown — computing them via separate calls walks
    ``comparison_results`` (and re-runs ``iter_countable_rows``) twice.
    On a large test run (200 docs × 50 rows/section × N sections) that's
    twice the classification work and doubles the anonymous-root warning
    bookkeeping. This helper fuses both into a single row iteration
    (finding 9 from #625 round-4 review). Returns ``(top_metrics,
    per_field_metrics)`` — same shape the individual helpers produce, so
    the caller's downstream code is unaffected.

    The two individual helpers ``_run_level_counts_from_rows`` and
    ``_run_level_field_metrics_from_rows`` are retained as thin wrappers
    (each returns one half of this helper's tuple) so external unit tests
    that hit them directly still work; only the production
    ``_transform_stickler_metrics`` call site reaches this fused helper.
    Wrappers, not duplicates, so future edits touch one implementation and
    the two views can't drift (finding from #625 high review — an earlier
    docstring claimed they were independent implementations while the
    bodies had become one-line delegates).
    """
    from idp_common.evaluation.contract import (
        POSITIONAL_LEAF_NAME,
        _row_leaves,
        _row_weight,
        classify_field_comparison,
        iter_countable_rows,
        safe_div,
    )

    # Top-level (section-normalized) counts are accumulated inline in the
    # same row loop as per-field counts, using the classification we already
    # computed for the per-field bucketing (finding from #625 high review:
    # a previous variant walked rows twice — once to collect them, once
    # inside ``aggregate_row_counts`` — re-classifying every row).
    top_counts = {"tp": 0, "fa": 0, "fd": 0, "tn": 0, "fn": 0}
    field_counts: Dict[str, Dict[str, int]] = {}

    def _add(field: str, bucket: str, weight: int = 1) -> None:
        entry = field_counts.setdefault(
            field, {"tp": 0, "fa": 0, "fd": 0, "tn": 0, "fn": 0}
        )
        entry[bucket] += weight

    for scr in comparison_results:
        if not scr:
            continue
        src = scr.get("_idp_source") or {}
        doc_key = src.get("doc_key") or "unknown"
        section_id = src.get("section_id") or "unknown"
        rows = iter_countable_rows(
            scr.get("field_comparisons") or [],
            context=f"run-level aggregation doc={doc_key} section={section_id}",
        )

        for fc in rows:
            bucket = classify_field_comparison(fc)
            # Compute ``_row_leaves`` ONCE per row and derive weight from
            # its length — the previous code called ``_row_weight(fc)``
            # (which internally calls ``_row_leaves``) and then called
            # ``_row_leaves`` AGAIN for the per-field spread, doubling
            # the recursive leaf walk (model_dump / vars) per row per
            # section.
            leaves = _row_leaves(fc)
            if leaves:
                weight = len(leaves)
            else:
                weight = _row_weight(fc)
            top_counts[bucket] += weight

            path = (
                fc.get("expected_key")
                or fc.get("actual_key")
                or fc.get("field_path")
                or ""
            )
            collapsed = _collapse_indices(path)
            if not collapsed:
                continue

            if leaves:
                for leaf in leaves:
                    _add(f"{collapsed}.{leaf}", bucket)
            else:
                _add(collapsed, bucket, weight)

    counts = {**top_counts, "fp": top_counts["fa"] + top_counts["fd"]}
    tp = counts["tp"]
    fp = counts["fp"]
    fn = counts["fn"]
    tn = counts["tn"]
    total = tp + fp + fn + tn
    top_metrics = {
        **counts,
        "cm_precision": safe_div(tp, tp + fp),
        "cm_recall": safe_div(tp, tp + fn),
        "cm_f1": safe_div(2 * tp, 2 * tp + fp + fn),
        "cm_accuracy": safe_div(tp + tn, total),
    }

    _synthesize_parent_buckets(field_counts)

    # Drop the synthetic ``__positional__`` sub-bucket from the per-field
    # response before it reaches the API — it's an internal accounting
    # slot (positional attribution for mixed dotted+positional rows)
    # with no schema counterpart, and would render as a phantom child
    # in Test Studio's per-field table. Counts are already rolled up
    # into the parent bucket by ``_synthesize_parent_buckets``.
    _pos_suffix = f".{POSITIONAL_LEAF_NAME}"
    field_counts = {
        k: v
        for k, v in field_counts.items()
        if not k.endswith(_pos_suffix) and k != POSITIONAL_LEAF_NAME
    }

    field_metrics: Dict[str, Dict[str, Any]] = {}
    for fname, c in field_counts.items():
        fp_f = c["fa"] + c["fd"]
        total_f = c["tp"] + fp_f + c["fn"] + c["tn"]
        field_metrics[fname] = {
            **c,
            "fp": fp_f,
            "cm_precision": safe_div(c["tp"], c["tp"] + fp_f),
            "cm_recall": safe_div(c["tp"], c["tp"] + c["fn"]),
            "cm_f1": safe_div(2 * c["tp"], 2 * c["tp"] + fp_f + c["fn"]),
            "cm_accuracy": safe_div(c["tp"] + c["tn"], total_f),
        }
    return top_metrics, field_metrics


def _synthesize_parent_buckets(field_counts: Dict[str, Dict[str, int]]) -> None:
    """For every dotted leaf path in ``field_counts``, add parent-prefix
    buckets summing all descendants.

    ``Items.name`` and ``Items.amount`` → ``Items`` bucket holds the sum
    of both, so Test Studio's hierarchical table can expand from the
    parent row and the parent's counts equal the sum of its children.

    Cross-schema name collision (issue #625 review):
    In a test run that mixes two schemas where the SAME parent name is
    a *scalar* in schema A and a *structured list* in schema B, we
    ACCUMULATE structured descendants INTO the pre-existing scalar
    bucket. Reason: preserving only the scalar counts recreates the
    exact parent-vs-drilldown contradiction issue #625 exists to fix —
    the parent bucket would show ✓ (scalar's clean counts) while
    expanding its drilldown revealed red children from the structured
    schema. Always-accumulating means:

    * The parent bucket's counts equal the sum of every row that ended
      up under it, regardless of which schema emitted the row.
    * A drilldown that reveals a red child necessarily reflects in the
      parent's counts too — no green ✓ hiding red children.
    * On a collision, a WARN log is still emitted so operators can
      disambiguate the attribute name (via ``_seen_collision_names``).

    Mutates ``field_counts`` in place; no return value.
    """
    # Snapshot the ORIGINAL leaf buckets (before any synthesis) so we
    # don't count synthesized parents as descendants of grandparents
    # (would double-count in a 3-level tree). Also captures which paths
    # were pre-existing so we can WARN on cross-schema collisions
    # without changing counting behavior.
    original_paths = set(field_counts.keys())
    collisions: List[str] = []
    for path, counts in list(field_counts.items()):
        parts = path.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent in original_paths:
                # A leaf bucket already carries counts under this exact
                # path — a scalar attribute from a different schema whose
                # name happens to prefix this leaf. Note the collision so
                # operators can rename to disambiguate, but STILL
                # accumulate descendants into it: preserving-only
                # recreates the parent-vs-drilldown contradiction #625
                # was opened to eliminate.
                collisions.append(parent)
            entry = field_counts.setdefault(
                parent, {"tp": 0, "fa": 0, "fd": 0, "tn": 0, "fn": 0}
            )
            for k in ("tp", "fa", "fd", "tn", "fn"):
                entry[k] += counts[k]

    if collisions:
        # De-dup within a single call, AND across calls in a warm Lambda
        # container: a persistent scalar-vs-structured schema collision
        # would otherwise fire this warning on every aggregation
        # (finding from #625 high review — the aggregator runs once per
        # test run in production, so a stable collision floods CloudWatch
        # on every run). The LRU is bounded so a container that sees many
        # DIFFERENT collision names over its lifetime still gets fresh
        # warnings after eviction.
        unique = tuple(sorted(set(collisions)))
        should_log = False
        with _seen_collision_lock:
            if unique not in _seen_collision_names:
                if len(_seen_collision_names) >= _SEEN_COLLISION_MAX:
                    _seen_collision_names.popitem(last=False)
                _seen_collision_names[unique] = None
                should_log = True
        if should_log:
            logger.warning(
                "Parent-bucket name collision: %d attribute name(s) appear "
                "as a scalar in one schema and a structured list in another "
                "in this run: %s. The parent bucket now accumulates counts "
                "from BOTH interpretations so the parent row equals the sum "
                "of its children (no parent-vs-drilldown contradiction). "
                "Rename the attribute in one schema to disambiguate if the "
                "mixed counts are undesirable.",
                len(unique),
                list(unique[:10]),
            )


def _transform_stickler_metrics(
    process_eval,
    doc_weighted_scores: Dict[str, float],
    comparison_results: List[Dict[str, Any]],
    ecab_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Transform Stickler ProcessEvaluation to IDP metrics format.

    Args:
        process_eval: ProcessEvaluation from Stickler
        doc_weighted_scores: Per-document weighted scores
        comparison_results: List of comparison results for confidence calculation
        ecab_metrics: ECARB confidence metrics from BulkStructuredModelEvaluator (optional)

    Returns:
        Dictionary matching existing IDP metrics format (without split metrics)
    """
    # Top-level metrics come from the row-level ``field_comparisons`` sweep so
    # list-heavy documents don't silently under-report failures (#625).
    # Per-field metrics also come from rows (bucketed by index-collapsed path)
    # so the run-level dashboard's top-level and per-field views agree on the
    # same document — Stickler's ``process_eval.field_metrics`` uses the item-
    # level ``cm.fields.X.overall`` rollup that hides leaves inside kept items,
    # so it would say ``Items.name`` precision = 1.00 while top-level says 0.20
    # (finding 3 from #625 adversarial review).
    metrics, row_field_metrics = _run_level_row_aggregates(comparison_results)

    # Use Stickler's bulk confidence metrics (computed by aggregate_from_comparisons)
    # Stickler automatically aggregates prediction_confidences from comparison results
    confidence_metrics = process_eval.confidence_metrics
    average_confidence = None

    try:
        from idp_common.evaluation.confidence_integration import (
            get_average_confidence_from_metrics,
        )

        # R7: pattern-collapsed confidence metrics already computed by
        # ``_IndexCollapsingConfidenceAccumulator`` upstream — the caller
        # replaced ``process_eval.confidence_metrics`` before invoking us.
        # No post-pass enhancement required.

        # Merge ECARB (Error Capture at Review Budget) metrics from separate evaluation.
        # ECARB requires custom confidence_metrics in BulkStructuredModelEvaluator.
        # If ECAB succeeded but ``process_eval.confidence_metrics`` is None
        # (Stickler produced no aggregate confidence surface for this run),
        # initialize an empty dict so the ECAB values still land in the
        # output rather than being silently dropped (finding from #625
        # high review — a run with ECAB metrics but no other confidence
        # signal previously lost the ECAB rows entirely).
        if ecab_metrics and confidence_metrics is None:
            confidence_metrics = {}
        if ecab_metrics and confidence_metrics is not None:
            # Merge ECAB into overall metrics
            if (
                "overall" in ecab_metrics
                and "error_capture_at_budget" in ecab_metrics["overall"]
            ):
                if "overall" not in confidence_metrics:
                    confidence_metrics["overall"] = {}
                confidence_metrics["overall"]["error_capture_at_budget"] = ecab_metrics[
                    "overall"
                ]["error_capture_at_budget"]

            # Merge ECAB into per-field metrics — only initialize the
            # ``fields`` sub-dict when we actually have per-field ECAB
            # data to add. Creating an empty ``fields`` dict here would
            # trip the "no metrics" tail branch and drop the merged
            # confidence_metrics entirely (finding from #625 high review).
            ecab_fields = ecab_metrics.get("fields") or {}
            fields_to_merge = {
                field_name: field_ecab
                for field_name, field_ecab in ecab_fields.items()
                if "error_capture_at_budget" in field_ecab
            }
            if fields_to_merge:
                if "fields" not in confidence_metrics:
                    confidence_metrics["fields"] = {}
                for field_name, field_ecab in fields_to_merge.items():
                    if field_name not in confidence_metrics["fields"]:
                        confidence_metrics["fields"][field_name] = {}
                    confidence_metrics["fields"][field_name][
                        "error_capture_at_budget"
                    ] = field_ecab["error_capture_at_budget"]

        if confidence_metrics and confidence_metrics.get("fields"):
            # Extract average confidence for backward compatibility
            average_confidence = get_average_confidence_from_metrics(confidence_metrics)

            # Log confidence metrics for debugging
            logger.info(
                f"Enhanced confidence metrics: "
                f"AUROC={confidence_metrics.get('overall', {}).get('auroc', {}).get('value')}, "
                f"ECE={confidence_metrics.get('overall', {}).get('ece', {}).get('value')}, "
                f"Brier={confidence_metrics.get('overall', {}).get('brier', {}).get('value')}, "
                f"avg_confidence={average_confidence}, "
                f"field_count={len(confidence_metrics.get('fields', {}))}"
            )

            # Log sample field names to verify structure
            sample_fields = list(confidence_metrics.get("fields", {}).keys())[:5]
            logger.info(f"Sample confidence field patterns: {sample_fields}")
        elif confidence_metrics and confidence_metrics.get("overall"):
            # No per-field metrics but there IS an ``overall`` block (e.g.
            # ECAB overall merged into a fresh dict when Stickler emitted
            # nothing) — keep the dict so ECAB rows still reach the
            # output rather than being nullified by the tail branch
            # (finding from #625 high review — the tail branch below
            # previously reset confidence_metrics to None on any run with
            # no fields, dropping ECAB overall in the process).
            logger.info(
                "No per-field confidence metrics; preserving overall block "
                f"(ECAB={confidence_metrics.get('overall', {}).get('error_capture_at_budget')})"
            )
        else:
            logger.warning("No confidence metrics returned by Stickler bulk aggregator")
            confidence_metrics = None

    except Exception as e:
        logger.warning(f"Error processing confidence metrics: {e}")
        confidence_metrics = None

    return {
        # ``overall_accuracy`` matches the Optional[float] shape of the
        # sibling accuracy_breakdown fields — None on zero-denominator
        # ("unmeasurable"), float otherwise. ``metrics["cm_accuracy"]``
        # comes from ``safe_div`` (0.0 on zero-denom); route it through
        # the same optional helper so Athena IS NULL predicates see all
        # top-level accuracy fields consistently.
        "overall_accuracy": _optional_accuracy(metrics),
        "weighted_overall_scores": doc_weighted_scores,
        "average_confidence": average_confidence,  # Now computed from Stickler if available
        "confidence_metrics": confidence_metrics,  # NEW: Full calibration metrics (v0.4.0+)
        "accuracy_breakdown": {
            # All five fields use the same Optional[float] shape: None on
            # zero-denominator ("unmeasurable"), a float otherwise. Mixed
            # 0.0-vs-None across the block would let Athena IS NULL see
            # FAR/FDR as null but precision/recall/f1 as zero on the same
            # empty run (finding C1 from #625 broad-scope self-review).
            # ``metrics`` comes from ``_run_level_row_aggregates`` which
            # uses ``safe_div`` (0.0 on zero-denom); convert to None here
            # when the counts were genuinely zero.
            "precision": _optional_precision(metrics),
            "recall": _optional_recall(metrics),
            "f1_score": _optional_f1(metrics),
            "false_alarm_rate": _calculate_false_alarm_rate(metrics),
            "false_discovery_rate": _calculate_false_discovery_rate(metrics),
        },
        "confusion_matrix": {
            "tp": metrics.get("tp", 0),
            "fp": metrics.get("fp", 0),
            "tn": metrics.get("tn", 0),
            "fn": metrics.get("fn", 0),
            "fa": metrics.get("fa", 0),
            "fd": metrics.get("fd", 0),
        },
        "field_metrics": _with_accuracy_intervals(row_field_metrics),
        "document_count": process_eval.document_count,
        "total_time": process_eval.total_time,
    }


def _with_accuracy_intervals(field_metrics: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Attach sampling uncertainty to each field's accuracy.

    A per-field accuracy is a proportion measured on however many observations that
    field happened to get, and the point estimate alone cannot distinguish 100% on 3
    from 100% on 300. Overall run accuracy firms up within roughly a hundred documents
    because every document feeds it; a field appearing once per document gains one
    observation per document, so a broken field can sit inside a healthy overall score.

    Derived from the counts already present, so nothing new is measured or stored.
    Fields with no observations get no interval rather than a zero-filled one — 0%
    would read as "always wrong" for a field no document contained.
    """
    if not isinstance(field_metrics, dict):
        return field_metrics or {}

    try:
        from idp_common.evaluation.intervals import (
            accuracy_interval_from_confusion_matrix,
        )
    except ImportError as e:  # pragma: no cover — the extra is present in this Lambda
        logger.warning(f"Accuracy intervals unavailable: {e}")
        return field_metrics

    for metrics in field_metrics.values():
        if not isinstance(metrics, dict):
            continue
        interval = accuracy_interval_from_confusion_matrix(metrics)
        if interval is None:
            continue
        metrics["accuracy_observations"] = interval.observations
        metrics["accuracy_margin"] = interval.margin
        metrics["accuracy_low"] = interval.low
        metrics["accuracy_high"] = interval.high
    return field_metrics


def _aggregate_graded_packet_metrics(
    doc_graded_packet_scores: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    """Roll per-doc graded packet metrics up into a run-level bundle.

    Same idiom as ``weighted_overall_scores``: emits ``{"per_document": {...},
    "mean": {...}}`` where each ``mean`` entry is a simple unweighted average
    across the documents that reported that key. Docs are already
    page-count-aware within themselves (V-measure / Rand / ordering are
    computed over every page of the doc), so averaging per-doc gives each
    document equal weight in the run summary — matching how
    ``weighted_overall_scores`` is surfaced today.

    Returns an empty dict when no document reported graded metrics so the
    UI can skip the panel entirely rather than render an all-null table.
    """
    if not doc_graded_packet_scores:
        return {}

    means: Dict[str, float] = {}
    for key in _GRADED_PACKET_KEYS:
        values = [
            doc_scores[key]
            for doc_scores in doc_graded_packet_scores.values()
            if isinstance(doc_scores.get(key), (int, float))
        ]
        if values:
            means[key] = sum(values) / len(values)

    if not means:
        return {}

    return {
        "mean": means,
        "per_document": doc_graded_packet_scores,
        "document_count": len(doc_graded_packet_scores),
    }


def _optional_accuracy(metrics: Dict[str, Any]) -> Optional[float]:
    """Accuracy (``(tp + tn) / total``) or ``None`` on zero-denominator.

    Matches the Optional[float] shape of the sibling accuracy_breakdown
    fields so ``overall_accuracy`` isn't the odd one out — a downstream
    ``IS NULL`` check reads all top-level accuracy fields uniformly.
    """
    tp = metrics.get("tp", 0)
    tn = metrics.get("tn", 0)
    fp = metrics.get("fa", 0) + metrics.get("fd", 0)
    fn = metrics.get("fn", 0)
    total = tp + tn + fp + fn
    return (tp + tn) / total if total > 0 else None


def _optional_precision(metrics: Dict[str, Any]) -> Optional[float]:
    """Precision (``tp / (tp + fa + fd)``) or ``None`` on zero-denominator.

    Matches the FAR/FDR Optional[float] shape so the accuracy_breakdown
    block has a uniform "unmeasurable → None, measured → float" contract
    for external Athena / BI ``IS NULL`` predicates.
    """
    tp = metrics.get("tp", 0)
    fp = metrics.get("fa", 0) + metrics.get("fd", 0)
    denom = tp + fp
    return tp / denom if denom > 0 else None


def _optional_recall(metrics: Dict[str, Any]) -> Optional[float]:
    """Recall (``tp / (tp + fn)``) or ``None`` on zero-denominator."""
    tp = metrics.get("tp", 0)
    fn = metrics.get("fn", 0)
    denom = tp + fn
    return tp / denom if denom > 0 else None


def _optional_f1(metrics: Dict[str, Any]) -> Optional[float]:
    """F1 (``2·tp / (2·tp + fp + fn)``) or ``None`` on zero-denominator."""
    tp = metrics.get("tp", 0)
    fp = metrics.get("fa", 0) + metrics.get("fd", 0)
    fn = metrics.get("fn", 0)
    denom = 2 * tp + fp + fn
    return 2 * tp / denom if denom > 0 else None


def _calculate_false_alarm_rate(metrics: Dict[str, Any]) -> Optional[float]:
    """Calculate false alarm rate (FA / (FA + TN)).

    Uses Stickler's ``fa`` (false alarm — predicted when the value should be
    absent) rather than the combined ``fp``. Stickler's invariant is
    ``fp == fa + fd``, so the combined count double-counts false *discoveries*
    (predicted-but-wrong) as false *alarms* and inflates this rate whenever
    both error classes are present. Zero denominator returns ``None`` (not
    ``0.0``) so external Athena / BI queries can distinguish "unmeasurable"
    (no fa+tn signal in the run) from "measured 0.0" via ``IS NULL``
    (finding from #625 review — flipping to ``0.0`` broke that SQL
    predicate; the earlier "match per-doc's ``safe_div``" argument
    doesn't apply because per-doc is a different query surface).
    """
    fa = metrics.get("fa", 0)
    tn = metrics.get("tn", 0)
    return fa / (fa + tn) if (fa + tn) > 0 else None


def _calculate_false_discovery_rate(metrics: Dict[str, Any]) -> Optional[float]:
    """Calculate false discovery rate (FD / (FD + TP)).

    Uses Stickler's ``fd`` (false discovery — predicted a wrong value) rather
    than the combined ``fp``, for the same reason as
    ``_calculate_false_alarm_rate``: ``fp == fa + fd``, so the combined count
    would fold false alarms into this rate. Zero denominator returns
    ``None`` for the same reason (see ``_calculate_false_alarm_rate``).
    """
    fd = metrics.get("fd", 0)
    tp = metrics.get("tp", 0)
    return fd / (fd + tp) if (fd + tp) > 0 else None


class _IndexCollapsingConfidenceAccumulator:
    """R7: ``ConfidenceAccumulator`` subclass that collapses list-index paths
    before Stickler's ``ConfidenceCalculator`` sees them.

    Stickler's ``ConfidenceAccumulator`` keys pairs by the raw field path —
    which means for a Hungarian-matched array, ``LineItems[0].Rate``,
    ``LineItems[1].Rate``, ``LineItems[2].Rate`` are three separate entries
    with sample-sizes of 1 each. Downstream metrics (AUROC, ECE, Brier) can't
    be computed on N=1 series and the report either omits the field or fills
    it with nulls.

    This subclass rewrites every occurrence of ``[digits]`` to nothing before
    feeding the extractor, aggregating all indices at a single pattern-based
    key (``LineItems.Rate``). One pass over ``comparison_results``, all
    Stickler-native math. Replaces the previous scikit-learn-based post-pass
    (``_enhance_confidence_metrics_with_patterns``) that ran outside the
    accumulator pipeline entirely.
    """

    # Shared with ``_collapse_indices`` at module scope — see
    # ``_INDEX_TOKEN_RE`` for why the pattern matches ``[]`` as well as
    # ``[N]``. Kept as a class attribute for backward-compat with existing
    # tests / callers that reach in via ``cls._INDEX_RE``.
    _INDEX_RE = _INDEX_TOKEN_RE
    name = "confidence_metrics"

    def __init__(self, metrics=None):
        # Lazy-import Stickler so the module still parses when stickler-eval
        # isn't installed (defensive — Lambda always has it).
        from stickler.structured_object_evaluator.models.confidence.calculator import (
            ConfidenceCalculator,
        )

        self._calculator = ConfidenceCalculator(metrics=metrics)
        self.reset()

    def reset(self):
        self._keyed_pairs: Dict[str, list] = {}
        self._fields_with = 0
        self._fields_total = 0

    @classmethod
    def _collapse(cls, key: str) -> str:
        """LineItems[0].Rate -> LineItems.Rate — strip every ``[digits]``."""
        return cls._INDEX_RE.sub("", key)

    def accumulate(self, comparison_result, prediction_raw):
        """Rewrite indexed paths to pattern keys, then delegate to Stickler.

        Mirrors the built-in ``ConfidenceAccumulator.accumulate`` layout so any
        upstream refactor of the base class is a merge-conflict signal rather
        than silent drift.
        """
        field_comparisons = comparison_result.get("field_comparisons", []) or []
        if not field_comparisons:
            return

        # Feed raw indexed keys through extract_from_dicts (which joins each
        # comparison's actual_key against the confidences dict — 1:1 match).
        # Then collapse the resulting ``keyed_pairs`` into pattern buckets
        # afterwards so all indices at ``LineItems[N].Rate`` land under a
        # single ``LineItems.Rate`` pattern for AUROC/ECE/Brier computation
        # over the full sample.
        confidences = comparison_result.get("prediction_confidences") or {}
        extraction = self._calculator.extract_from_dicts(field_comparisons, confidences)

        if confidences:
            for field_path, pairs in extraction.keyed_pairs.items():
                pattern_key = (
                    self._collapse(field_path)
                    if isinstance(field_path, str)
                    else field_path
                )
                self._keyed_pairs.setdefault(pattern_key, []).extend(pairs)

        self._fields_with += extraction.fields_with_confidence
        self._fields_total += extraction.fields_total

    def compute(self):
        if self._fields_total == 0:
            return None
        return self._calculator.compute_metrics(
            self._keyed_pairs,
            fields_with_confidence=self._fields_with,
            fields_total=self._fields_total,
        )

    def get_state(self) -> Dict[str, Any]:
        return {
            "keyed_confidence_pairs": {
                field_path: [p.model_dump() for p in pairs]
                for field_path, pairs in self._keyed_pairs.items()
            },
            "confidence_fields_with": self._fields_with,
            "confidence_fields_total": self._fields_total,
        }

    def load_state(self, state: Dict[str, Any]) -> None:
        from stickler.structured_object_evaluator.models.confidence.metrics import (
            ConfidencePair,
        )

        self._keyed_pairs = {
            field_path: [ConfidencePair(**p) for p in pairs]
            for field_path, pairs in state.get("keyed_confidence_pairs", {}).items()
        }
        self._fields_with = state.get("confidence_fields_with", 0)
        self._fields_total = state.get("confidence_fields_total", 0)

    def merge_state(self, other_state: Dict[str, Any]) -> None:
        from stickler.structured_object_evaluator.models.confidence.metrics import (
            ConfidencePair,
        )

        for field_path, pairs in other_state.get("keyed_confidence_pairs", {}).items():
            self._keyed_pairs.setdefault(field_path, []).extend(
                [ConfidencePair(**p) for p in pairs]
            )
        self._fields_with += other_state.get("confidence_fields_with", 0)
        self._fields_total += other_state.get("confidence_fields_total", 0)


def _rename_brier_score_key(
    confidence_metrics: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Rename Stickler's ``brier_score`` key to ``brier`` in-place.

    Stickler's ``BrierScoreMetric.name`` is ``brier_score``; the deleted
    sklearn post-pass wrote ``brier``; the Test Studio UI + awsjson-types
    still read ``brier``. Rename here at the aggregation boundary so the UI
    keeps rendering Brier Score without a cross-repo change. Recurses through
    ``overall`` + ``fields.<name>`` to catch both scopes.
    """
    if not confidence_metrics:
        return confidence_metrics
    overall = confidence_metrics.get("overall")
    if (
        isinstance(overall, dict)
        and "brier_score" in overall
        and "brier" not in overall
    ):
        overall["brier"] = overall.pop("brier_score")
    fields = confidence_metrics.get("fields") or {}
    for field_metrics in fields.values():
        if (
            isinstance(field_metrics, dict)
            and "brier_score" in field_metrics
            and "brier" not in field_metrics
        ):
            field_metrics["brier"] = field_metrics.pop("brier_score")
    return confidence_metrics


def _empty_metrics() -> Dict[str, Any]:
    """Return empty metrics structure.

    ``excluded_documents`` / ``excluded_document_count`` are seeded here (same
    idiom as ``graded_packet_metrics``) so every return path emits the same
    shape — the UI can distinguish "field absent" from "0 excluded" without a
    presence check.
    """
    return {
        "overall_accuracy": None,
        "weighted_overall_scores": {},
        "average_confidence": None,
        "accuracy_breakdown": {
            # All ``None`` on the error path so external Athena / BI
            # queries can use ``IS NULL`` to distinguish "unmeasurable"
            # (the aggregation errored, no signal to compute anything)
            # from "measured zero" (a real run whose counts happened to
            # give a zero-denominator ratio). Aligns with the normal
            # path's ``_calculate_false_alarm_rate`` returning ``None``
            # on zero-denominator.
            "precision": None,
            "recall": None,
            "f1_score": None,
            "false_alarm_rate": None,
            "false_discovery_rate": None,
        },
        "split_classification_metrics": {},
        "graded_packet_metrics": {},
        "classification_errors": {
            "errors": [],
            "total": 0,
            "documents_affected": 0,
            "truncated": False,
        },
        "excluded_documents": [],
        "excluded_document_count": 0,
        "document_count": 0,
        "total_time": 0,
    }
