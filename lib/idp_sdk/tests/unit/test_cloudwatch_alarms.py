# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Static regression coverage for CloudWatch alarm definitions.

Background
----------
``WorkflowErrorsAlarm`` -- the solution's primary "workflow failures" alert,
wired to the ``Workflow Alerts`` SNS topic -- was defined against the
``AWS/States`` metric ``ExecutionsFailedCount``. That metric does not exist;
Step Functions publishes ``ExecutionsFailed``. CloudWatch accepts an alarm on a
metric name that is never published, so the alarm was created successfully and
then sat in ``INSUFFICIENT_DATA`` forever, through real failed executions,
notifying nobody (GitHub issue #746).

Two properties of that bug make it worth a dedicated test rather than a one-line
fix:

1. **Nothing could have caught it.** ``cfn-lint`` validates that an alarm is
   *well-formed*, not that its metric name exists in its namespace, so the
   template linted clean for as long as the bug existed. There is no deploy-time
   error either -- a wrong metric name is indistinguishable to CloudWatch from a
   metric that has not been published yet.
2. **The failure mode is silence.** A broken alarm looks exactly like a healthy
   one right up until the moment you needed it. The dashboard widget next to it
   used the correct ``ExecutionsFailed`` the whole time, so the two disagreed
   about the same signal with no visible symptom.

``TreatMissingData`` is the second half of the same story. Neither ``AWS/States``
alarm set it, so both defaulted to ``missing`` and idled in
``INSUFFICIENT_DATA`` -- which is precisely what a wrong metric name also looks
like. The absent property is what let the wrong metric name hide.

The checks here are deliberately static (no AWS, no deploy) so they run in the
fast unit gate and fail at author time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from idp_sdk._core.cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit


# --- Metric allowlists --------------------------------------------------------

# Metric names published by the AWS-owned namespaces this repo alarms on. An
# alarm whose metric name is not in its namespace's set can never receive a
# datapoint, which is the #746 bug.
#
# These are intentionally scoped to the metric families the templates actually
# use, NOT a full transcription of every metric each service emits: the point is
# to make a typo fail, and a list nobody can verify would not do that. Adding a
# legitimate new metric means adding it here, which is a deliberate speed bump on
# a property that is otherwise unverifiable until an incident.
#
# Sources:
#   AWS/States -- Step Functions *execution* metrics (dimension: StateMachineArn)
#     https://docs.aws.amazon.com/step-functions/latest/dg/procedure-cw-metrics.html
#   AWS/SQS    https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html
#   AWS/Lambda https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics.html
AWS_METRIC_NAMES: dict[str, frozenset[str]] = {
    "AWS/States": frozenset(
        {
            "ExecutionThrottled",
            "ExecutionTime",
            "ExecutionsAborted",
            "ExecutionsFailed",
            "ExecutionsRedriven",
            "ExecutionsStarted",
            "ExecutionsSucceeded",
            "ExecutionsTimedOut",
        }
    ),
    "AWS/SQS": frozenset(
        {
            "ApproximateAgeOfOldestMessage",
            "ApproximateNumberOfMessagesDelayed",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesVisible",
            "NumberOfEmptyReceives",
            "NumberOfMessagesDeleted",
            "NumberOfMessagesReceived",
            "NumberOfMessagesSent",
            "SentMessageSize",
        }
    ),
    "AWS/Lambda": frozenset(
        {
            "ClaimedAccountConcurrency",
            "ConcurrentExecutions",
            "DeadLetterErrors",
            "DestinationDeliveryFailures",
            "Duration",
            "Errors",
            "Invocations",
            "IteratorAge",
            "PostRuntimeExtensionsDuration",
            "ProvisionedConcurrencyInvocations",
            "ProvisionedConcurrencySpilloverInvocations",
            "ProvisionedConcurrencyUtilization",
            "ProvisionedConcurrentExecutions",
            "Throttles",
            "UnreservedConcurrentExecutions",
        }
    ),
}

# Metric names that do not exist and have appeared in this repo before. Checked
# as raw text across every template so the guard also covers places the
# structured walk below cannot reach -- most importantly embedded CloudWatch
# *dashboard* JSON, where metric names live inside a !Sub string and a typo is
# just as invisible.
KNOWN_BOGUS_METRIC_NAMES = {
    # GitHub #746. The real metric is `ExecutionsFailed`.
    "ExecutionsFailedCount",
}


# --- Template discovery -------------------------------------------------------


def _repo_root() -> Path:
    """Walk up until we find the repo root (contains the main template.yaml)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


_EXCLUDED_PARTS = {".git", "node_modules", ".aws-sam", "build", "dist", ".venv"}


def _discover_templates() -> list[str]:
    """Every CloudFormation template in the repo, as repo-relative paths.

    Discovered rather than listed. A hardcoded list is right for the permissions
    boundary suite, which has to assert about templates that *should* contain a
    role; here the assertion is about alarms that already exist, so globbing
    means an alarm added to any template -- including one created after this
    test -- is covered with no list to update. The alternative fails silently,
    which is the exact failure mode this file exists to prevent.
    """
    root = _repo_root()
    found = []
    for path in sorted(root.rglob("*.yaml")):
        if _EXCLUDED_PARTS & set(path.relative_to(root).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "AWSTemplateFormatVersion" not in text and "AWS::Serverless" not in text:
            continue
        found.append(str(path.relative_to(root)))
    return found


TEMPLATES = _discover_templates()


def _load(rel_path: str) -> dict:
    # Intrinsics come back as {"!Tag": value} so the assertions below can tell a
    # !Ref from a literal; see idp_sdk._core.cfn_yaml for the safety rationale.
    return load_cfn_template(_repo_root() / rel_path)


def _alarms(template: dict) -> dict[str, dict]:
    return {
        name: body
        for name, body in (template.get("Resources") or {}).items()
        if isinstance(body, dict) and body.get("Type") == "AWS::CloudWatch::Alarm"
    }


def _metric_refs(props: dict) -> Iterator[tuple[Any, Any]]:
    """Yield every ``(namespace, metric_name)`` pair an alarm evaluates.

    Covers both alarm shapes: the simple top-level ``Namespace``/``MetricName``
    form, and the metric-math ``Metrics`` list where each entry may carry a
    ``MetricStat.Metric``. A metric-math alarm can hold a typo just as easily as
    a simple one.
    """
    if "MetricName" in props:
        yield props.get("Namespace"), props["MetricName"]
    for entry in props.get("Metrics") or []:
        if not isinstance(entry, dict):
            continue
        metric = (entry.get("MetricStat") or {}).get("Metric") or {}
        if isinstance(metric, dict) and "MetricName" in metric:
            yield metric.get("Namespace"), metric["MetricName"]


def _alarms_with_metrics() -> list[tuple[str, str, Any, Any]]:
    """Flatten every alarm in every template into checkable metric references."""
    rows = []
    for rel_path in TEMPLATES:
        for name, body in _alarms(_load(rel_path)).items():
            for namespace, metric_name in _metric_refs(body.get("Properties") or {}):
                rows.append((rel_path, name, namespace, metric_name))
    return rows


ALARM_METRIC_REFS = _alarms_with_metrics()


# --- Tests --------------------------------------------------------------------


def test_templates_are_discoverable():
    """Guard: discovery actually found the templates, so the suite isn't vacuous.

    Without this, a broken glob or a moved file turns every parametrized test
    below into zero test cases and the suite passes by finding nothing.
    """
    assert "template.yaml" in TEMPLATES, (
        f"Main template not discovered; found {TEMPLATES}"
    )
    assert len(TEMPLATES) >= 10, f"Suspiciously few templates discovered: {TEMPLATES}"


def test_alarms_were_found():
    """Guard: the main template's alarms are being checked."""
    main_alarms = {
        name for path, name, _, _ in ALARM_METRIC_REFS if path == "template.yaml"
    }
    assert "WorkflowErrorsAlarm" in main_alarms, (
        "WorkflowErrorsAlarm not found in template.yaml. If it was renamed, "
        "update this guard; if it was removed, #746's alert has no replacement."
    )
    assert "SlowExecutionsAlarm" in main_alarms


@pytest.mark.parametrize(
    "rel_path,alarm_name,namespace,metric_name",
    ALARM_METRIC_REFS,
    ids=[f"{p}:{n}:{m}" for p, n, _, m in ALARM_METRIC_REFS],
)
def test_alarm_metric_name_exists_in_its_namespace(
    rel_path, alarm_name, namespace, metric_name
):
    """An alarm on a metric name the namespace never publishes can never fire.

    Custom namespaces (``{"!Ref": "AWS::StackName"}``) are skipped -- the metric
    names there are emitted by our own code, so there is no authoritative list to
    check against and the emitting call site is the only source of truth.
    """
    if not isinstance(namespace, str) or not namespace.startswith("AWS/"):
        return  # custom or intrinsic namespace; nothing to validate against

    assert isinstance(metric_name, str), (
        f"{rel_path}:{alarm_name} uses a non-literal MetricName "
        f"({metric_name!r}); this check cannot validate it."
    )

    known = AWS_METRIC_NAMES.get(namespace)
    assert known is not None, (
        f"{rel_path}:{alarm_name} alarms on AWS namespace {namespace!r}, which "
        f"this test has no metric list for. Add {namespace!r} to "
        f"AWS_METRIC_NAMES with the metric names that namespace publishes -- an "
        f"unchecked AWS namespace is how #746 happened."
    )
    assert metric_name in known, (
        f"{rel_path}:{alarm_name} alarms on {namespace}/{metric_name}, which is "
        f"not a metric that namespace publishes. An alarm on a nonexistent "
        f"metric is created successfully and then never fires (GitHub #746). "
        f"Known metrics: {sorted(known)}"
    )


@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_every_alarm_sets_treat_missing_data(rel_path):
    """Every alarm states what "no datapoints" means instead of defaulting.

    The CloudFormation default is ``missing``, which parks the alarm in
    ``INSUFFICIENT_DATA`` whenever there is no traffic. For this solution's
    alarms that state is both wrong (no documents processed means no failures,
    which is ``OK``) and actively harmful: it is visually identical to an alarm
    that is broken, which is how #746 stayed hidden. Setting it explicitly forces
    the author to decide.
    """
    offenders = [
        name
        for name, body in _alarms(_load(rel_path)).items()
        if "TreatMissingData" not in (body.get("Properties") or {})
    ]
    assert not offenders, (
        f"{rel_path}: alarms without an explicit TreatMissingData: {offenders}. "
        f"Set it (usually `notBreaching`: no traffic means no failures, not "
        f"unknown). Defaulting to `missing` leaves the alarm in "
        f"INSUFFICIENT_DATA, which is indistinguishable from a broken alarm."
    )


@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_no_known_bogus_metric_names_anywhere_in_template(rel_path):
    """Text-level guard covering dashboard JSON as well as alarm resources.

    The structured walk above cannot see metric names inside embedded CloudWatch
    dashboard bodies, which are !Sub strings of JSON. A wrong metric name in a
    dashboard widget is the same defect with a quieter symptom -- an empty graph
    rather than a silent alarm -- so the names known to be wrong are also
    forbidden as raw text.

    Whole-line YAML comments are stripped first: the templates are allowed to
    *name* a wrong metric in order to explain why it is wrong, which is more
    useful to the next reader than a comment that has to talk around it.
    """
    text = (_repo_root() / rel_path).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for bogus in KNOWN_BOGUS_METRIC_NAMES:
        assert bogus not in body, (
            f"{rel_path} references {bogus!r}, which is not a real CloudWatch "
            f"metric name. See KNOWN_BOGUS_METRIC_NAMES in this file."
        )
