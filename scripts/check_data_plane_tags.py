#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Enforce ``idp:plane=data`` on the small whitelist of data-plane Lambdas.

See docs/reporting-sql-layer.md §10.3 for the tagging rationale.
Short version:

- Only per-document processors carry ``idp:plane=data``. Everything else
  is implicitly control plane (invoked by users, admin actions,
  schedules, or system observers, not by document arrival).
- The whitelist below names each data-plane Lambda by logical ID plus
  the template that owns it. The linter verifies each one exists AND
  carries the tag — a rename or a missing tag fails the build.
- Adding a new pipeline stage means adding it to this whitelist AND
  tagging it. The linter's failure message points reviewers here.

Exit code:
    0 — every whitelisted Lambda has ``idp:plane=data``
    1 — one or more Lambdas are missing the tag or don't exist
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. `pip install pyyaml`", file=sys.stderr)
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parents[1]
UNIFIED_TEMPLATE = REPO_ROOT / "patterns" / "unified" / "template.yaml"
MAIN_TEMPLATE = REPO_ROOT / "template.yaml"

# Data-plane whitelist: Lambda logical IDs classified as "invoked
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
# prod docs), CompleteSectionReviewFunction (HITL user click),
# CircuitBreakerManagerFunction (alarm/health-check driven),
# BackfillWorkerFunction (admin one-shot), FinetuningProcessDocumentFunction
# (training-set processing, not prod doc arrival), DataMartRollupFunction
# itself, and all API resolvers / chat / auth / admin functions.
DATA_PLANE_WHITELIST: dict[str, Path] = {
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


def _check_whitelisted_lambda(path: Path, logical_id: str) -> List[str]:
    """Ensure a whitelisted data-plane Lambda exists AND carries the tag."""
    if not path.exists():
        return [f"{_display_path(path)}: template file not found"]
    template = yaml.load(path.read_text(), Loader=_cfn_tag_loader())
    resources = (template or {}).get("Resources", {}) or {}
    resource = resources.get(logical_id)
    if resource is None:
        return [
            f"{_display_path(path)}: {logical_id} not found in template "
            f"— DATA_PLANE_WHITELIST is out of date (either add the resource "
            f"back, or remove it from the whitelist)"
        ]
    if _tag_value(resource, "idp:plane") != "data":
        return [f"{_display_path(path)}: {logical_id} is missing idp:plane=data tag"]
    return []


def main() -> int:
    missing: List[str] = []
    for logical_id, path in DATA_PLANE_WHITELIST.items():
        missing.extend(_check_whitelisted_lambda(path, logical_id))

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
            "not by document arrival), remove it from DATA_PLANE_WHITELIST in\n"
            "scripts/check_data_plane_tags.py instead.\n\n"
            "See docs/reporting-sql-layer.md §10.3–§10.4.",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK — all {len(DATA_PLANE_WHITELIST)} whitelisted data-plane Lambdas "
        f"carry idp:plane=data."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
