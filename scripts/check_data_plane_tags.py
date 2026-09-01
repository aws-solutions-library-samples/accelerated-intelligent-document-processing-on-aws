#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Enforce ``idp:plane=data`` on the small allowlist of data-plane Lambdas.

See docs/reporting-sql-layer.md §10.3 for the tagging rationale.
Short version:

- Only per-document processors carry ``idp:plane=data``. Everything else
  is implicitly control plane (invoked by users, admin actions,
  schedules, or system observers, not by document arrival).
- The allowlist below names each data-plane Lambda by logical ID plus
  the template that owns it. The linter verifies each one exists AND
  carries the tag — a rename or a missing tag fails the build.
- Adding a new pipeline stage means adding it to this allowlist AND
  tagging it. The linter's failure message points reviewers here.

Exit code:
    0 — every allowlisted Lambda has ``idp:plane=data``
    1 — one or more Lambdas are missing the tag or don't exist
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. `pip install pyyaml`", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parents[1]
UNIFIED_TEMPLATE = REPO_ROOT / "patterns" / "unified" / "template.yaml"
MAIN_TEMPLATE = REPO_ROOT / "template.yaml"

# Data-plane allowlist: Lambda logical IDs classified as "invoked
# per document". See docs/reporting-sql-layer.md §10.4 for the
# classification rule ("what triggered the invocation").
#
# Keep this list narrow — every entry is a source of cost that the
# Monitor dashboard's Data Plane KPI accounts for. Adding a new entry
# is a deliberate act, not a maintenance chore.
#
# Explicitly NOT on this list (in the same templates, but control plane):
# TestExecutionAggregationFunction (post-run orchestration),
# MLflowLoggerFunction (per-run write-up), CodeBuildTrigger (one-shot
# bootstrap), BDAOCRProjectFunction (CFN custom resource),
# TestFileCopierFunction (test-run seeding, scales with test volume not
# prod docs), CircuitBreakerManagerFunction (alarm/health-check driven),
# BackfillWorkerFunction (admin one-shot), FinetuningProcessDocumentFunction
# (training-set processing, not prod doc arrival), DataMartRollupFunction
# itself, and all API resolvers / chat / auth / admin functions.
DATA_PLANE_ALLOWLIST: dict[str, Path] = {
    # ── Pipeline mode (Textract + Bedrock) ─────────────────────────────
    "OCRFunction": UNIFIED_TEMPLATE,
    "ClassificationFunction": UNIFIED_TEMPLATE,
    "ExtractionFunction": UNIFIED_TEMPLATE,
    "AssessmentFunction": UNIFIED_TEMPLATE,
    "SummarizationFunction": UNIFIED_TEMPLATE,
    "EvaluationFunction": UNIFIED_TEMPLATE,
    # Per-doc pipeline result stitching (post-extraction).
    "ProcessResultsFunction": UNIFIED_TEMPLATE,
    # Sync-invoked from the pipeline per doc for pre/post-processing hooks
    # (PII redaction etc.). Timeout up to 900s per doc.
    "PipelineHooksDispatcherFunction": UNIFIED_TEMPLATE,
    # Per-doc (or per-shard) Bedrock batch shard runtime.
    "ShardRuntimeFunction": UNIFIED_TEMPLATE,
    # ── BDA mode (Bedrock Data Automation) ─────────────────────────────
    # Per-doc BDA path. Only invoked when use_bda config flag is set,
    # but when it runs, it's per doc.
    "InvokeBDAFunction": UNIFIED_TEMPLATE,
    "BDAProcessResultsFunction": UNIFIED_TEMPLATE,
    "BDACompletionFunction": UNIFIED_TEMPLATE,
    # ── Rule validation (per-doc quality gate) ─────────────────────────
    # Only active when rule_validation is enabled in config, but per doc
    # when it runs.
    "RuleValidationFunction": UNIFIED_TEMPLATE,
    "RuleValidationOrchestrationFunction": UNIFIED_TEMPLATE,
    "RuleValidationPolicyClassificationFunction": UNIFIED_TEMPLATE,
    # ── Ingest / tracking (main stack) ─────────────────────────────────
    # S3 upload event → one invocation per doc arrival.
    "QueueSender": MAIN_TEMPLATE,
    # SQS batch trigger from the doc queue.
    "QueueProcessor": MAIN_TEMPLATE,
    # Jobs API batch ingest — extracts zip and feeds files to input bucket.
    # Cost scales linearly with doc volume through the batch API path.
    "BatchPreProcessorFunction": MAIN_TEMPLATE,
    # Invoked per Step Functions state change per document.
    "WorkflowTracker": MAIN_TEMPLATE,
    # SQS per-doc status-change events from the Jobs API path.
    "JobTracker": MAIN_TEMPLATE,
    # Invoked async by EvaluationFunction / RuleValidationOrchestration /
    # RuleValidationPolicyClassification per doc. Cost scales with doc
    # volume, not with dashboard views.
    "SaveReportingDataFunctionV2": MAIN_TEMPLATE,
    # SQS per-doc dispatcher for user-supplied custom post-processor.
    "PostProcessingDecompressor": MAIN_TEMPLATE,
    # HITL section review completion — per-doc pipeline callback. The
    # pipeline pauses on low-confidence sections (task-token pattern) and
    # this Lambda fires exactly once per doc that needs review, resuming
    # the Step Function. Trigger is a human click, but the CAUSE is a
    # specific doc's low-confidence extraction — cost scales with docs,
    # not with UI activity or infra baseline.
    "CompleteSectionReviewFunction": MAIN_TEMPLATE,
}


def _cfn_tag_loader() -> type[yaml.SafeLoader]:
    """Return a SafeLoader that tolerates CloudFormation shorthand tags
    (``!Ref``, ``!Sub``, ``!If``, ``!GetAtt``, …). We don't care about
    resolving them — we only inspect ``Properties.Tags`` which is plain
    scalar / mapping data.
    """

    class Loader(yaml.SafeLoader):
        pass

    def _stub_constructor(loader, node):  # noqa: ARG001
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    for tag in (
        "!Ref",
        "!Sub",
        "!GetAtt",
        "!GetAZs",
        "!Join",
        "!Select",
        "!Split",
        "!ImportValue",
        "!If",
        "!Equals",
        "!And",
        "!Or",
        "!Not",
        "!Base64",
        "!Cidr",
        "!FindInMap",
        "!Transform",
        "!Condition",
    ):
        Loader.add_constructor(tag, _stub_constructor)  # type: ignore[arg-type]
    return Loader


def _tag_value(resource: dict, key: str) -> str | None:
    """Return the value of the given tag key on the resource, or None."""
    tags = (resource.get("Properties") or {}).get("Tags")
    if tags is None:
        return None
    # Tags can be either a list of {Key, Value} dicts (native CFN) or a
    # mapping (SAM-specific shorthand). Handle both.
    if isinstance(tags, dict):
        return tags.get(key)
    if isinstance(tags, list):
        for entry in tags:
            if isinstance(entry, dict) and entry.get("Key") == key:
                return entry.get("Value")
    return None


def _display_path(path: Path) -> str:
    """Show path relative to the repo when possible; otherwise as-is.
    Called from error messages — the try/except keeps unit tests using
    a tmp_path outside the repo from crashing on ``relative_to``.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@lru_cache(maxsize=None)
def _load_template_resources(path: Path) -> Dict[str, Any]:
    """Parse a CloudFormation template once and cache the Resources map.

    Both `_check_allowlisted_lambda` (called ~23× per invocation, one
    per DATA_PLANE_ALLOWLIST entry) and `_check_inverse_drift` used to
    call `yaml.load(path.read_text())` on the same 12k-line
    `template.yaml` — parsing it dozens of times. The linter runs on
    every push in CI, so the caching cuts fastlint runtime measurably
    while keeping the exact same behavior.
    """
    if not path.exists():
        return {}
    template = yaml.load(path.read_text(), Loader=_cfn_tag_loader())
    return (template or {}).get("Resources", {}) or {}


def _check_allowlisted_lambda(path: Path, logical_id: str) -> List[str]:
    """Ensure an allowlisted data-plane Lambda exists AND carries the tag."""
    if not path.exists():
        return [f"{_display_path(path)}: template file not found"]
    resources = _load_template_resources(path)
    resource = resources.get(logical_id)
    if resource is None:
        return [
            f"{_display_path(path)}: {logical_id} not found in template "
            f"— DATA_PLANE_ALLOWLIST is out of date (either add the resource "
            f"back, or remove it from the allowlist)"
        ]
    if _tag_value(resource, "idp:plane") != "data":
        return [f"{_display_path(path)}: {logical_id} is missing idp:plane=data tag"]
    return []


def _check_inverse_drift() -> List[str]:
    """Return errors for any Lambda in the two source templates that
    carries ``idp:plane=data`` but is NOT on the allowlist.

    The main check covers allowlist → tag; this is the inverse (tag →
    allowlist). Without it, someone renaming a data-plane Lambda without
    updating the allowlist gets a passing lint AND the Lambda silently
    falls into ``other-control`` at rollup time. Round-5 review fix.
    """
    errors: List[str] = []
    allowlisted_by_template: Dict[Path, set[str]] = {}
    for logical_id, path in DATA_PLANE_ALLOWLIST.items():
        allowlisted_by_template.setdefault(path, set()).add(logical_id)

    for template_path in {MAIN_TEMPLATE, UNIFIED_TEMPLATE}:
        if not template_path.exists():
            continue
        resources = _load_template_resources(template_path)
        expected = allowlisted_by_template.get(template_path, set())
        for logical_id, resource in resources.items():
            if not isinstance(resource, dict):
                continue
            if resource.get("Type") not in (
                "AWS::Serverless::Function",
                "AWS::Lambda::Function",
            ):
                continue
            if (
                _tag_value(resource, "idp:plane") == "data"
                and logical_id not in expected
            ):
                errors.append(
                    f"{_display_path(template_path)}: {logical_id} carries "
                    f"idp:plane=data but is NOT in DATA_PLANE_ALLOWLIST "
                    f"— either add it to the allowlist or drop the tag."
                )
    return errors


def main() -> int:
    missing: List[str] = []
    for logical_id, path in DATA_PLANE_ALLOWLIST.items():
        missing.extend(_check_allowlisted_lambda(path, logical_id))
    missing.extend(_check_inverse_drift())

    if missing:
        print(
            "ERROR: data-plane Lambda tag check failed:",
            file=sys.stderr,
        )
        for entry in missing:
            print(f"  - {entry}", file=sys.stderr)
        print(
            "\nAdd the tag under Properties.Tags:\n"
            "    Tags:\n"
            "      idp:plane: data\n\n"
            "If this Lambda is control plane (invoked by user / schedule / admin,\n"
            "not by document arrival), remove it from DATA_PLANE_ALLOWLIST in\n"
            "scripts/check_data_plane_tags.py instead.\n\n"
            "See docs/reporting-sql-layer.md §10.3–§10.4.",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK — all {len(DATA_PLANE_ALLOWLIST)} allowlisted data-plane Lambdas "
        f"carry idp:plane=data (both directions verified)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
