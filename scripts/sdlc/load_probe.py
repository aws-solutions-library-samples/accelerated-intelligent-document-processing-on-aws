#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Load probe: a few dozen packets at once against an EXISTING stack
(``make stacktest-load``; GitLab ``load_probe`` on its own weekly schedule).

Why it exists. The failures that occur in one call out of a few hundred, only
under throttling, or only with one model's reply habits are invisible to the unit
suites (fakes) and to the integration suite (a handful of documents at low
concurrency). This probe is the tier that can see them. It is not a merge gate.

WHAT IT DOES
------------
  1. Plans the run: one ARM per feature under load, each arm a preset from
     ``config_library/unified`` plus an overlay, and N copies of that preset's
     sample packet. Refuses (exit 2, nothing created) when the plan exceeds the
     document cap or its estimated cost exceeds the dollar cap.
  2. Builds each arm's configuration and validates it OFFLINE with
     ``idp_common.config.models.IDPConfig``.
  3. Uploads each arm as a NEW configuration profile
     ``loadprobe-<run id>-<arm>``. It never writes ``default`` and never writes a
     profile that already exists: the name is checked against the stack's
     profiles first, and the SDK's own ``version_created`` is checked after.
  4. Submits every arm's documents back to back, so the whole set reaches the
     queue together and the stack processes it at its own configured
     concurrency (``MaxConcurrentWorkflows``, read and reported).
  5. Polls until every document settles or the deadline passes.
  6. Reads each document's Step Functions execution history and output sections.

ASSERTIONS (exit 1 when any fails)
----------------------------------
  * No document failed for a transient cause. A failure whose cause cannot be
    read, or is a Map-run tolerance failure whose child cause is not visible
    here, counts as UNCLASSIFIED and fails the run too: it cannot be shown to be
    non-transient.
  * Every document settled before the deadline.
  * In every arm whose configuration asks for confidence, every section that
    extracted at least one value carries at least one confidence score.

Reported, not asserted: step-level retries (per state and per error name),
documents failed for a deterministic cause, the stack's concurrency. The retry
counts are a TREND to read across runs; one run's number is not a verdict.

What it does not see: retries inside one Lambda invocation (the Bedrock client's
own backoff), and failures inside a distributed Map run's child executions.

COST AND CLEANUP
----------------
The dollar cap is enforced on an ESTIMATE made before anything is created
(pages x ``--cost-per-page-usd``). It is not a meter: nothing stops the run once
submitted, which is why the document cap and the per-page figure are
deliberately conservative. ``--cleanup`` decides whether the profiles and
documents the run created are deleted afterwards: ``never`` (the default, for
inspection), ``on-pass`` (what the schedule uses, so a red run keeps its
evidence) or ``always``.

EXIT CODES: 0 PASS, 1 FAIL, 2 could not run (refused, setup error, no stack).

USAGE
-----
  make stacktest-load STACK_NAME=<stack> [REGION=us-east-1] [DRY_RUN=1]
  python3 scripts/sdlc/load_probe.py --stack-name <stack> --dry-run
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

REPO = Path(__file__).resolve().parents[2]
PRESETS = REPO / "config_library" / "unified"
SAMPLES = REPO / "samples"

PASS, FAIL, CANNOT_RUN = 0, 1, 2

#: The extraction model every arm runs: fast, and prone to throttling under load,
#: which is the condition the probe exists to create.
DEFAULT_FAST_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

#: Conservative defaults; every one is a parameter. Unmeasured: the per-page
#: figure is a deliberately pessimistic guess, not a reading of any run's bill.
DEFAULT_DOCS_PER_ARM = 8
DEFAULT_MAX_DOCUMENTS = 40
DEFAULT_MAX_COST_USD = 25.0
DEFAULT_COST_PER_PAGE_USD = 0.10
DEFAULT_DEADLINE_MINUTES = 45
DEFAULT_POLL_SECONDS = 30

PROFILE_PREFIX = "loadprobe"

#: States/Lambda error names that are transient although they are not in
#: idp_common's exception vocabulary (that vocabulary judges Python exceptions
#: and service codes; these are what Step Functions itself reports).
SFN_TRANSIENT_ERRORS = frozenset(
    {
        "transienterror",  # idp_common.utils.transient_errors.TransientError
        "states.timeout",
        "states.heartbeattimeout",
        "sandbox.timedout",
        "lambda.serviceexception",
        "lambda.awslambdaexception",
        "lambda.sdkclientexception",
        "lambda.toomanyrequestsexception",
        "lambda.clientexecutiontimeoutexception",
    }
)

#: Errors that say only that a child failed, so the cause is not visible here.
OPAQUE_ERRORS = frozenset(
    {
        "states.exceedtoleratedfailurethreshold",
        "states.itemreaderfailed",
        "extractionshardmapfailed",
    }
)

TASK_SCHEDULED = ("TaskScheduled", "LambdaFunctionScheduled")
TASK_FAILED = (
    "TaskFailed",
    "TaskTimedOut",
    "LambdaFunctionFailed",
    "LambdaFunctionTimedOut",
    "LambdaFunctionStartFailed",
    "TaskStartFailed",
)
SETTLED = {"COMPLETED", "FAILED", "ABORTED"}


# --------------------------------------------------------------------------- arms


@dataclass(frozen=True)
class Arm:
    name: str
    preset: str
    sample: str
    pages: int
    overlay: dict[str, Any]
    #: Set when the arm cannot run without an operator-supplied value.
    needs: Optional[str] = None


#: Settings every arm carries, so the probe measures the features under load and
#: not whatever the stack's default profile happens to enable.
COMMON_OVERLAY: dict[str, Any] = {
    "use_bda": False,
    "hitl.enabled": False,
    "summarization.enabled": False,
    "extraction.confidence.mode": "separate",
}


def build_arms(decision_model_endpoint: Optional[str]) -> list[Arm]:
    lending = ("lending-package-sample", "lending_package.pdf", 6)
    rvl = ("rvl-cdip-package-sample", "rvl_cdip_package.pdf", 10)
    return [
        Arm(
            "split-then-classify",
            *rvl,
            {"classification.strategy": "split_then_classify"},
        ),
        Arm(
            "holistic",
            *lending,
            {
                "classification.strategy": "combined",
                "classification.combined.granularity": "packet",
            },
        ),
        Arm("agentic", *lending, {"extraction.mode": "advanced"}),
        Arm(
            "decision-model",
            *lending,
            {
                "extraction.confidence.method": "decision_model",
                "extraction.confidence.decision_model.endpoint": decision_model_endpoint
                or "",
                "extraction.geometry.mode": "ocr_only",
            },
            needs=None
            if decision_model_endpoint
            else "--decision-model-endpoint (a GENAIIDP- Lambda ARN)",
        ),
    ]


def set_path(config: dict, dotted: str, value: Any) -> None:
    node = config
    *parents, leaf = dotted.split(".")
    for key in parents:
        child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    node[leaf] = value


def arm_config(arm: Arm, fast_model: str, presets: Path = PRESETS) -> dict:
    """The preset with the common and arm overlays applied, validated offline."""
    import yaml

    from idp_common.config.models import IDPConfig

    with open(presets / arm.preset / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config = copy.deepcopy(config)
    overlay = {**COMMON_OVERLAY, "extraction.model": fast_model, **arm.overlay}
    for dotted, value in overlay.items():
        set_path(config, dotted, value)
    config.pop("managed", None)
    IDPConfig.model_validate(copy.deepcopy(config))
    return config


def confidence_expected(config: dict) -> bool:
    mode = ((config.get("extraction") or {}).get("confidence") or {}).get("mode")
    return str(mode or "separate").lower() != "off"


# --------------------------------------------------------------------------- plan


@dataclass
class Plan:
    arms: list[Arm]
    skipped: dict[str, str]
    docs_per_arm: int
    cost_per_page: float

    @property
    def documents(self) -> int:
        return self.docs_per_arm * len(self.arms)

    @property
    def pages(self) -> int:
        return sum(arm.pages * self.docs_per_arm for arm in self.arms)

    @property
    def estimated_cost(self) -> float:
        return round(self.pages * self.cost_per_page, 2)


def make_plan(args: argparse.Namespace) -> Plan:
    wanted = set(args.arms) if args.arms else None
    arms, skipped = [], {}
    for arm in build_arms(args.decision_model_endpoint):
        if wanted is not None and arm.name not in wanted:
            continue
        if arm.needs:
            skipped[arm.name] = f"needs {arm.needs}"
            continue
        arms.append(arm)
    return Plan(arms, skipped, args.docs_per_arm, args.cost_per_page_usd)


def refusals(plan: Plan, args: argparse.Namespace) -> list[str]:
    """Why this plan must not run; empty when it may."""
    problems = []
    if not plan.arms:
        problems.append("no arm left to run")
    if plan.documents > args.max_documents:
        problems.append(
            f"{plan.documents} documents planned, over the cap of "
            f"{args.max_documents} (--max-documents)"
        )
    if plan.estimated_cost > args.max_cost_usd:
        problems.append(
            f"estimated ${plan.estimated_cost:.2f} ({plan.pages} pages at "
            f"${plan.cost_per_page:.2f}) is over the cap of ${args.max_cost_usd:.2f} "
            "(--max-cost-usd)"
        )
    return problems


# ----------------------------------------------------------- history and verdicts


def count_retries(events: list[dict]) -> tuple[Counter, Counter]:
    """``(retries per state name, task failures per error name)`` for one execution.

    A retry is a second (or later) task scheduled within ONE entry into a state.
    Each scheduled event is attributed to its state entry by walking
    ``previousEventId`` back to the nearest ``*StateEntered`` event, which is what
    keeps concurrent Map iterations of the same state apart.
    """
    by_id = {e["id"]: e for e in events}
    per_entry: Counter = Counter()
    entry_name: dict[int, str] = {}
    for event in events:
        if event["type"] not in TASK_SCHEDULED:
            continue
        cursor = by_id.get(event.get("previousEventId", 0))
        while cursor is not None and not cursor["type"].endswith("StateEntered"):
            cursor = by_id.get(cursor.get("previousEventId", 0))
        if cursor is None:
            continue
        per_entry[cursor["id"]] += 1
        entry_name[cursor["id"]] = cursor.get("stateEnteredEventDetails", {}).get(
            "name", "?"
        )
    retries: Counter = Counter()
    for entry, scheduled in per_entry.items():
        if scheduled > 1:
            retries[entry_name[entry]] += scheduled - 1
    failures: Counter = Counter()
    for event in events:
        if event["type"] in TASK_FAILED:
            failures[_details(event).get("error") or event["type"]] += 1
    return retries, failures


def _details(event: dict) -> dict:
    for key, value in event.items():
        if key.endswith("EventDetails") and isinstance(value, dict):
            return value
    return {}


def terminal_failure(events: list[dict]) -> tuple[str, str]:
    """``(error, cause)`` of the last task-level failure, else the execution's own.

    The pipeline's catch-all Fail state replaces the error on ExecutionFailed with
    a generic one, so the task failure that preceded it is the real cause.
    """
    for event in reversed(events):
        if event["type"] in TASK_FAILED or event["type"] == "MapRunFailed":
            d = _details(event)
            return d.get("error") or event["type"], d.get("cause") or ""
    for event in reversed(events):
        if event["type"] in (
            "ExecutionFailed",
            "ExecutionTimedOut",
            "ExecutionAborted",
        ):
            d = _details(event)
            return d.get("error") or event["type"], d.get("cause") or ""
    return "", ""


def classify_failure(error: str, cause: str) -> str:
    """``transient``, ``deterministic`` or ``unclassified``."""
    from idp_common.utils.transient_errors import (
        TRANSIENT_ERROR_NAMES,
        TRANSIENT_MESSAGE_MARKERS,
    )

    names = {error.strip().lower()} if error else set()
    message = cause or ""
    try:  # a Lambda failure's cause is the function's JSON error payload
        payload = json.loads(cause)
        if isinstance(payload, dict):
            names.add(str(payload.get("errorType", "")).strip().lower())
            message = str(payload.get("errorMessage", "")) or message
    except (TypeError, ValueError):
        pass
    names.discard("")
    if names & (TRANSIENT_ERROR_NAMES | SFN_TRANSIENT_ERRORS):
        return "transient"
    lowered = message.lower()
    if any(marker in lowered for marker in TRANSIENT_MESSAGE_MARKERS):
        return "transient"
    if not names or names <= OPAQUE_ERRORS | {"states.runtime"}:
        return "unclassified"
    return "deterministic"


def _has_value(node: Any) -> bool:
    if isinstance(node, dict):
        return any(_has_value(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_value(v) for v in node)
    return node not in (None, "")


def confidence_gaps(sections: Iterable[dict]) -> list[str]:
    """Section ids that extracted a value and carry no confidence score."""
    from idp_common.evaluation import flatten_confidences

    gaps = []
    for section in sections:
        if not _has_value(section.get("inference_result")):
            continue
        if not flatten_confidences(section.get("explainability_info") or []):
            gaps.append(str(section.get("section_id", "?")))
    return gaps


# --------------------------------------------------------------------------- AWS


class AwsBackend:
    """Every call that reaches AWS. Replaced by a fake in the unit tests."""

    def __init__(self, stack: str, region: str):
        import boto3

        from idp_sdk import IDPClient

        self.stack, self.region = stack, region
        self.client = IDPClient(stack_name=stack, region=region)
        self.cfn = boto3.client("cloudformation", region_name=region)
        self.sfn = boto3.client("stepfunctions", region_name=region)
        self.s3 = boto3.client("s3", region_name=region)
        self.ddb = boto3.resource("dynamodb", region_name=region)
        identity = boto3.client("sts", region_name=region).get_caller_identity()
        print(f"Credentials: {identity['Arn']} ({region})")
        self._resources: Any = None

    def _stack_resources(self) -> Any:
        if self._resources is None:
            self._resources = self.client.stack.get_resources()
        return self._resources

    def max_concurrency(self) -> Optional[int]:
        stack = self.cfn.describe_stacks(StackName=self.stack)["Stacks"][0]
        for p in stack.get("Parameters", []):
            if p["ParameterKey"] == "MaxConcurrentWorkflows":
                return int(p["ParameterValue"])
        return None

    def profiles(self) -> set[str]:
        return {v.version_name for v in self.client.config.list().versions}

    def upload_profile(self, name: str, config: dict) -> bool:
        import yaml

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(config, f)
            path = f.name
        try:
            result = self.client.config.upload(
                path,
                config_profile=name,
                validate=False,
                description="load probe (scripts/sdlc/load_probe.py)",
                created_by="load-probe",
            )
        finally:
            os.unlink(path)
        if not result.success:
            raise RuntimeError(f"profile {name}: {result.error}")
        return bool(result.version_created)

    def submit(self, directory: str, profile: str, batch_id: str) -> tuple:
        result = self.client.batch.process(
            directory=directory, config_profile=profile, batch_id=batch_id
        )
        return result.batch_id, list(result.document_ids)

    def statuses(self, batch_id: str) -> dict[str, str]:
        status = self.client.batch.get_status(batch_id)
        return {
            d.document_id: str(getattr(d.status, "value", d.status)).upper()
            for d in status.documents
        }

    def history(self, document_id: str) -> list[dict]:
        table = self.ddb.Table(self._stack_resources().documents_table)
        item = table.get_item(Key={"PK": f"doc#{document_id}", "SK": "none"}).get(
            "Item", {}
        )
        arn = item.get("WorkflowExecutionArn") or item.get("ExecutionArn")
        if not arn:
            return []
        events: list[dict] = []
        pages = self.sfn.get_paginator("get_execution_history").paginate(
            executionArn=arn, includeExecutionData=True
        )
        for page in pages:
            events.extend(page["events"])
        return events

    def sections(self, document_id: str) -> list[dict]:
        bucket = self._stack_resources().output_bucket
        found = []
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"{document_id}/sections/"
        )
        for page in pages:
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/result.json"):
                    body = self.s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"]
                    section = json.loads(body.read())
                    section.setdefault("section_id", obj["Key"].split("/")[-2])
                    found.append(section)
        return found

    def delete_profile(self, name: str) -> None:
        self.client.config.delete(config_profile=name)

    def delete_batch(self, batch_id: str) -> None:
        self.client.batch.delete_documents(batch_id=batch_id)


# ---------------------------------------------------------------------- the run


@dataclass
class ArmRun:
    arm: Arm
    profile: str
    config: dict
    batch_id: str = ""
    documents: list[str] = field(default_factory=list)


@dataclass
class Clock:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def stage_documents(arm: Arm, copies: int, into: Path) -> str:
    """``copies`` uniquely named copies of the arm's sample, one directory per arm."""
    directory = into / arm.name
    directory.mkdir(parents=True)
    stem = Path(arm.sample).stem
    for i in range(copies):
        shutil.copyfile(SAMPLES / arm.sample, directory / f"{stem}-{i:03d}.pdf")
    return str(directory)


def wait_until_settled(
    backend: Any, runs: list[ArmRun], deadline: float, poll: float, clock: Clock
) -> dict[str, str]:
    latest: dict[str, str] = {}
    while True:
        for run in runs:
            latest.update(backend.statuses(run.batch_id))
        expected = [d for run in runs for d in run.documents]
        if all(latest.get(d) in SETTLED for d in expected):
            return latest
        if clock.now() >= deadline:
            return latest
        clock.sleep(min(poll, max(0.0, deadline - clock.now())))


def evaluate(backend: Any, runs: list[ArmRun], statuses: dict[str, str]) -> dict:
    report: dict[str, Any] = {"arms": {}, "findings": []}
    total_retries: Counter = Counter()
    total_failures: Counter = Counter()
    for run in runs:
        arm_report: dict[str, Any] = {
            "profile": run.profile,
            "batch_id": run.batch_id,
            "documents": len(run.documents),
            "statuses": dict(
                Counter(statuses.get(d, "UNSETTLED") for d in run.documents)
            ),
            "failures": [],
            "confidence_gaps": {},
        }
        retries: Counter = Counter()
        for doc in run.documents:
            status = statuses.get(doc, "UNSETTLED")
            events = backend.history(doc) if status in SETTLED else []
            doc_retries, doc_failures = count_retries(events)
            retries.update(doc_retries)
            total_failures.update(doc_failures)
            if status not in SETTLED:
                report["findings"].append(f"{doc}: did not settle ({status})")
            elif status != "COMPLETED":
                error, cause = terminal_failure(events)
                kind = classify_failure(error, cause)
                arm_report["failures"].append(
                    {
                        "document": doc,
                        "status": status,
                        "kind": kind,
                        "error": error,
                        "cause": cause[:500],
                    }
                )
                if kind != "deterministic":
                    report["findings"].append(
                        f"{doc}: {status}, {kind} cause {error or '(none recorded)'}"
                    )
            elif confidence_expected(run.config):
                gaps = confidence_gaps(backend.sections(doc))
                if gaps:
                    arm_report["confidence_gaps"][doc] = gaps
                    report["findings"].append(
                        f"{doc}: sections {', '.join(gaps)} extracted values "
                        "with no confidence score"
                    )
        arm_report["retries_by_state"] = dict(retries)
        arm_report["retries"] = sum(retries.values())
        total_retries.update(retries)
        report["arms"][run.arm.name] = arm_report
    report["retries_by_state"] = dict(total_retries)
    report["retries"] = sum(total_retries.values())
    report["task_failures_by_error"] = dict(total_failures)
    report["verdict"] = "FAIL" if report["findings"] else "PASS"
    return report


def run_probe(
    args: argparse.Namespace,
    backend: Any,
    clock: Optional[Clock] = None,
    out: Callable[[str], None] = print,
) -> tuple[int, dict]:
    clock = clock or Clock()
    started = clock.now()
    deadline = started + args.deadline_minutes * 60
    plan = make_plan(args)
    run_id = args.run_id or datetime.now(timezone.utc).strftime(
        "%Y%m%d-%H%M%S-"
    ) + secrets.token_hex(2)
    report: dict[str, Any] = {
        "run_id": run_id,
        "stack": args.stack_name,
        "plan": {
            "arms": [a.name for a in plan.arms],
            "skipped_arms": plan.skipped,
            "documents": plan.documents,
            "pages": plan.pages,
            "estimated_cost_usd": plan.estimated_cost,
            "max_cost_usd": args.max_cost_usd,
            "max_documents": args.max_documents,
            "deadline_minutes": args.deadline_minutes,
            "fast_model": args.fast_model,
        },
    }
    for name, why in plan.skipped.items():
        out(f"SKIP arm {name}: {why}")
    problems = refusals(plan, args)
    if problems:
        report["verdict"] = "REFUSED"
        report["refusals"] = problems
        for p in problems:
            out(f"REFUSED: {p}")
        return CANNOT_RUN, report

    runs = []
    for arm in plan.arms:
        profile = f"{PROFILE_PREFIX}-{run_id}-{arm.name}"
        runs.append(ArmRun(arm, profile, arm_config(arm, args.fast_model)))
    out(
        f"Plan: {plan.documents} documents, {plan.pages} pages, estimated "
        f"${plan.estimated_cost:.2f} (cap ${args.max_cost_usd:.2f}); arms "
        + ", ".join(f"{r.arm.name} -> {r.profile}" for r in runs)
    )
    if args.dry_run:
        report["verdict"] = "DRY_RUN"
        out("Dry run: configurations validated offline; nothing was created.")
        return PASS, report

    if backend is None:
        raise ValueError("a backend is required unless --dry-run is given")
    report["max_concurrent_workflows"] = backend.max_concurrency()
    existing = backend.profiles()
    created: list[str] = []
    batches: list[str] = []
    code = CANNOT_RUN
    try:
        for run in runs:
            if run.profile == "default" or run.profile in existing:
                raise RuntimeError(f"refusing to write existing profile {run.profile}")
            if not backend.upload_profile(run.profile, run.config):
                raise RuntimeError(
                    f"profile {run.profile} already existed; the upload modified it"
                )
            created.append(run.profile)
        with tempfile.TemporaryDirectory() as staging:
            for run in runs:
                directory = stage_documents(run.arm, plan.docs_per_arm, Path(staging))
                # An explicit batch id per arm: the SDK's generated one is the
                # prefix plus a one-second timestamp, so two arms submitted in the
                # same second would share a batch and overwrite each other's
                # identically named copies.
                run.batch_id, run.documents = backend.submit(
                    directory, run.profile, run.profile
                )
                batches.append(run.batch_id)
        out(f"Submitted {sum(len(r.documents) for r in runs)} documents; waiting.")
        statuses = wait_until_settled(backend, runs, deadline, args.poll_seconds, clock)
        report.update(evaluate(backend, runs, statuses))
        report["elapsed_minutes"] = round((clock.now() - started) / 60, 1)
        code = PASS if report["verdict"] == "PASS" else FAIL
    except Exception as exc:  # noqa: BLE001 - reported, and the run cannot pass
        report["verdict"] = "ERROR"
        report["error"] = f"{type(exc).__name__}: {exc}"
        out(f"ERROR: {report['error']}")
    finally:
        if args.cleanup == "always" or (args.cleanup == "on-pass" and code == PASS):
            report["cleanup_errors"] = cleanup(backend, batches, created)
    return code, report


def cleanup(backend: Any, batches: list[str], profiles: list[str]) -> list[str]:
    """Delete what this run created; return what could not be deleted."""
    errors = []
    for delete, items in (
        (backend.delete_batch, batches),
        (backend.delete_profile, profiles),
    ):
        for item in items:
            try:
                delete(item)
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                errors.append(f"{item}: {type(exc).__name__}: {exc}")
    return errors


def summarize(report: dict, out: Callable[[str], None] = print) -> None:
    out(f"Load probe {report.get('run_id')}: {report.get('verdict')}")
    if report.get("max_concurrent_workflows") is not None:
        out(f"  stack MaxConcurrentWorkflows: {report['max_concurrent_workflows']}")
    for name, arm in report.get("arms", {}).items():
        out(
            f"  {name}: {arm['statuses']} retries={arm['retries']} "
            f"failures={len(arm['failures'])} confidence_gaps={len(arm['confidence_gaps'])}"
        )
    if "retries" in report:
        # The trend line: read across runs, never asserted.
        out(
            f"  TREND retries={report['retries']} by_state={report['retries_by_state']} "
            f"task_failures={report['task_failures_by_error']}"
        )
    for finding in report.get("findings", []):
        out(f"  FINDING {finding}")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stack-name", required=True)
    ap.add_argument(
        "--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )
    ap.add_argument("--arms", nargs="*", help="Subset of arms to run (default: all)")
    ap.add_argument("--docs-per-arm", type=int, default=DEFAULT_DOCS_PER_ARM)
    ap.add_argument("--max-documents", type=int, default=DEFAULT_MAX_DOCUMENTS)
    ap.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    ap.add_argument(
        "--cost-per-page-usd", type=float, default=DEFAULT_COST_PER_PAGE_USD
    )
    ap.add_argument("--deadline-minutes", type=float, default=DEFAULT_DEADLINE_MINUTES)
    ap.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    ap.add_argument("--fast-model", default=DEFAULT_FAST_MODEL)
    ap.add_argument(
        "--decision-model-endpoint",
        default=os.environ.get("IDP_LOAD_PROBE_DECIDER_ARN"),
    )
    ap.add_argument(
        "--cleanup", choices=("never", "on-pass", "always"), default="never"
    )
    ap.add_argument("--report-dir", default=str(REPO / "scratch" / "load-probe"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.docs_per_arm < 1:
        ap.error("--docs-per-arm must be at least 1")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        backend = None if args.dry_run else AwsBackend(args.stack_name, args.region)
        code, report = run_probe(args, backend)
    except Exception as exc:  # noqa: BLE001 - setup failure: the run did not happen
        print(f"ERROR: could not run the load probe: {type(exc).__name__}: {exc}")
        return CANNOT_RUN
    summarize(report)
    out_dir = Path(args.report_dir) / report["run_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"Report: {out_dir / 'report.json'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
