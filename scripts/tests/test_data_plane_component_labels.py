# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression test — every Lambda on ``DATA_PLANE_ALLOWLIST`` must map to a
real ``component`` label in the rollup Lambda's ``_component_for_function``.

Two invariants already existed around the allowlist:

- allowlist ↔ CFN tag: ``scripts/check_data_plane_tags.py`` (both directions)
- allowlist ↔ docs: the table in ``docs/reporting-sql-layer.md`` §10.4

This adds the third: **allowlist ↔ component label**. Without it, five of the
23 data-plane Lambdas were emitting ``data_plane_lambda_hourly`` rows with
``component='other-control'`` — a label whose name says "control", in the
data-plane table — and two more were claimed by the wrong stage rule
(``BDAProcessResultsFunction`` → ``process-results``,
``RuleValidationPolicyClassificationFunction`` → ``classification``). The cost
math was right in every case; only the grouping was wrong, which is exactly
the kind of bug that survives a review of the numbers.

Note the two name shapes below. At runtime ``_component_for_function`` receives
the **CFN physical function name** (``_rollup_*`` derives it from the ARN via
``arn.rsplit(":", 1)[-1]``), i.e. ``<stack>-<LogicalId>-<random>``, not the bare
logical ID. The pre-existing tests in
``lib/idp_common_pkg/tests/unit/lambdas/test_data_mart_rollup.py`` only pass
bare logical IDs, so a rule anchored with ``^`` would pass there and fail in
production. Every assertion here runs against both shapes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LINTER = REPO_ROOT / "scripts" / "check_data_plane_tags.py"
ROLLUP = REPO_ROOT / "src" / "lambda" / "data_mart_rollup" / "index.py"

# Expected component label per allowlisted data-plane Lambda. Kept explicit
# (rather than "anything but other-control") so a rule reorder that silently
# moves a Lambda between two real buckets also fails — that was the
# BDAProcessResults / RuleValidationPolicyClassification failure mode.
#
# Mirrors the Component column of the §10.4 table in
# docs/reporting-sql-layer.md. Update all three together.
EXPECTED_COMPONENT: Dict[str, str] = {
    # Pipeline mode (Textract + Bedrock)
    "OCRFunction": "ocr",
    "ClassificationFunction": "classification",
    "ExtractionFunction": "extraction",
    "AssessmentFunction": "assessment",
    "SummarizationFunction": "summarization",
    "EvaluationFunction": "evaluation",
    "ProcessResultsFunction": "process-results",
    "PipelineHooksDispatcherFunction": "pipeline-hooks",
    "ShardRuntimeFunction": "shard-runtime",
    # BDA mode
    "InvokeBDAFunction": "bda",
    "BDAProcessResultsFunction": "bda",
    "BDACompletionFunction": "bda",
    # Rule validation
    "RuleValidationFunction": "rule-validation",
    "RuleValidationOrchestrationFunction": "rule-validation",
    "RuleValidationPolicyClassificationFunction": "rule-validation",
    # Ingest / tracking (main stack)
    "QueueSender": "queue-sender",
    "QueueProcessor": "queue-processor",
    "BatchPreProcessorFunction": "batch-ingest",
    "WorkflowTracker": "workflow-tracker",
    "JobTracker": "job-tracker",
    "SaveReportingDataFunctionV2": "save-reporting",
    "PostProcessingDecompressor": "post-processing",
    "CompleteSectionReviewFunction": "hitl-review",
}


def _load_by_path(name: str, path: Path) -> Any:
    """Load a module by file path — neither ``scripts/`` nor
    ``src/lambda/data_mart_rollup/`` is on ``sys.path``. Same pattern as
    ``scripts/tests/test_check_data_plane_tags.py``."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"Could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def allowlist() -> Dict[str, Any]:
    return _load_by_path("linter", LINTER).DATA_PLANE_ALLOWLIST


@pytest.fixture(scope="module")
def component_for():
    """``_component_for_function`` from the rollup Lambda.

    The module creates boto3 clients at import time; that needs no credentials
    and no region for client construction to succeed here, but we never call
    anything that touches the network.
    """
    return _load_by_path("data_mart_rollup", ROLLUP)._component_for_function


def _physical_name(logical_id: str) -> str:
    """A realistic CFN-generated physical function name."""
    return f"IDPStack-PATTERNSTACK-2UBGW8A18HIT-{logical_id}-K7Fq2LmN9pQx"


@pytest.mark.unit
class TestAllowlistCoversEveryComponentLabel:
    def test_expected_map_matches_allowlist_exactly(self, allowlist) -> None:
        """Adding a Lambda to DATA_PLANE_ALLOWLIST without giving it a
        component label (or removing one and leaving this map stale) fails
        here, naming the drift."""
        missing = sorted(set(allowlist) - set(EXPECTED_COMPONENT))
        extra = sorted(set(EXPECTED_COMPONENT) - set(allowlist))
        assert not missing and not extra, (
            "EXPECTED_COMPONENT has drifted from DATA_PLANE_ALLOWLIST in "
            "scripts/check_data_plane_tags.py.\n"
            f"  on the allowlist but unlabelled here: {missing}\n"
            f"  labelled here but not on the allowlist: {extra}\n"
            "Add a rule to _COMPONENT_RULES in "
            "src/lambda/data_mart_rollup/index.py, an entry here, and a row "
            "in the §10.4 table of docs/reporting-sql-layer.md."
        )


@pytest.mark.unit
@pytest.mark.parametrize("logical_id", sorted(EXPECTED_COMPONENT))
class TestDataPlaneComponentLabels:
    def test_maps_to_expected_component(self, logical_id, component_for) -> None:
        expected = EXPECTED_COMPONENT[logical_id]
        for shape, name in (
            ("logical id", logical_id),
            ("CFN physical name", _physical_name(logical_id)),
        ):
            assert component_for(name) == expected, (
                f"{logical_id} as {shape} ({name!r}) mapped to "
                f"{component_for(name)!r}, expected {expected!r}. Check rule "
                f"ORDER in _COMPONENT_RULES — first match wins, and a broader "
                f"rule placed above a narrower one silently steals the label."
            )

    def test_never_falls_through_to_other_control(
        self, logical_id, component_for
    ) -> None:
        """``other-control`` in ``data_plane_lambda_hourly`` is always wrong:
        the row is by definition data plane (the Lambda carries
        ``idp:plane=data``), so the label contradicts the table it sits in."""
        for name in (logical_id, _physical_name(logical_id)):
            assert component_for(name) != "other-control", (
                f"{logical_id} ({name!r}) has no _COMPONENT_RULES match, so its "
                f"data_plane_lambda_hourly rows would be labelled "
                f"'other-control' in the DATA-plane table."
            )


@pytest.mark.unit
class TestBdaRuleDoesNotMatchTheWordLambda:
    """The BDA rule used to be ``(^|[^a-z])bda`` — a guard against ``lambda``
    containing ``bda`` at chars 3-5. That guard also broke
    ``InvokeBDAFunction``. The replacement names each BDA Lambda explicitly,
    so both properties must hold: no ``lambda``-named function is labelled
    ``bda``, and the BDA Lambdas are."""

    @pytest.mark.parametrize(
        "name",
        [
            "GetDomainLambda",
            "IDPStack-GetDomainLambda-K7Fq2LmN9pQx",
            "SomeFutureHelperLambdaFunction",
        ],
    )
    def test_lambda_in_the_name_is_not_bda(self, name, component_for) -> None:
        assert component_for(name) != "bda"

    @pytest.mark.parametrize(
        "logical_id",
        [
            "InvokeBDAFunction",
            "BDAProcessResultsFunction",
            "BDACompletionFunction",
            # Control plane (a CFN custom resource), but still the bda bucket.
            "BDAOCRProjectFunction",
        ],
    )
    def test_bda_lambdas_are_bda(self, logical_id, component_for) -> None:
        assert component_for(logical_id) == "bda"
        assert component_for(_physical_name(logical_id)) == "bda"
