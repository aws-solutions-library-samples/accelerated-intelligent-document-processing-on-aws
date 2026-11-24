#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Live check of review routing and the pipeline run plan (``make stacktest-review-routing``).

Review routing decides, after policy rule validation, whether a document is held
for review and in which review queue (``routing`` in the configuration). The run
plan decides at which step a run starts and which steps it leaves out. Both are
decided on real tables, queues and executions, so this check reads the
**execution history** of each run it causes and asserts which states the run
entered and which tasks it scheduled, not only how the document ended.

It is one script with several subcommands, because a stack upgrade has to be
checked from both sides of the upgrade. Every subcommand takes ``--stack-name``,
``--region`` and ``--state-file``; the state file is how one subcommand tells the
next what it created.

SUBCOMMANDS
-----------
  pre-upgrade      Against the release before review routing. Creates a temporary
                   profile with review on and ``hitl.confidence_threshold: 0.99``, a
                   temporary Annotator and Admin, lists the Annotator on the Default
                   queue, uploads three documents and waits until each is in review.
  start-in-flight  Uploads three more documents under the temporary profile and
                   returns without waiting, so that they are being processed while
                   the stack is updated. Run it again once the update has begun:
                   every batch is kept.
  post-upgrade     After the update. The three held documents are completed,
                   rejected and removed from review, and each run is read for where
                   it started (run again, it reads those runs and ends no review a
                   second time). An in-flight run that called an updated function
                   must have held its document when the document needed review;
                   a run that called none is skipped. A new upload under the
                   unchanged profile is held by the built-in routing rule.
  capture-switch   Before a stack update. Records Review Routing (``hitl.enabled``)
                   of the ``default`` profile and of ``review-routing-showcase``,
                   and the revision bodies each profile has, in the state file.
  check-switch     After the update. Review Routing has the value that was
                   captured in both profiles, and no revision body that was
                   captured has been written again.
  scenarios        Creates the queues ``approvals``, ``compliance`` and
                   ``specialist`` (when they do not exist) and uploads the profile
                   ``review-routing-showcase``, then checks where the four showcase
                   samples land, that a Test Studio document is not routed,
                   completion, rejection, send-back, reprocess from a later step,
                   who may save routing rules, the fallback to the Default queue,
                   the routing test, the DMN conversion, and the latency of the
                   routing and apply-review steps.
  reasons-and-restart
                   The reason a reviewer reads and the failed policy rules, on
                   the review item, the status object, the tracking row, the
                   review history and ``idp-cli status``; what finishing a review
                   will run (``getReviewCompletionPlan``), who may read it and how
                   long it takes; a send-back, which starts at classification; a
                   review completed after a regroup, which extracts the regrouped
                   sections again and then applies the review; and where Process
                   Changes would start for a completed document.
  scenario-rounds  The three scenario invoices of ``samples/review-routing``,
                   round by round: the routing rule that places each, the reason
                   its reviewer reads, the correction a reviewer makes, and the
                   round in which no routing rule matches and it completes.
  seed-demo        Leaves the three queues, the showcase profile, the four showcase
                   samples, one waiting in each queue, and the three scenario
                   invoices waiting in their first round, on the stack.
  cleanup          Removes everything the state file names, except what
                   ``seed-demo`` left: the temporary users and profile, the
                   documents of the checks with their review and tracking rows, and
                   the queues and profile the checks created.

The hook payload (``reviewed``, ``runKind``) is not checked here: no test hook is
deployed. It is covered offline by ``patterns/unified/tests`` and by the sample
hook's own tests.

THE STATE FILE
--------------
JSON, written after every change so that ``cleanup`` can remove what a failed run
left. It holds names only: the profile, the temporary users' addresses (all
``@example.invalid``), document keys, review cycle ids, execution names, queue
ids, and for ``capture-switch`` whether Review Routing was on and the file name
and entity tag of each revision body. It never holds a password, a token, an
account id or an execution ARN; a write that would is refused. Each subcommand
sets a new password for the temporary users it signs in as, in memory only.

OUTCOMES AND EXIT CODES
-----------------------
Every check is PASS, FAIL, ERROR (could not be run) or SKIP (precondition absent).
A SKIP is never a PASS. Exit 0 only when every check passed; 1 when any check
failed; 2 when nothing failed but a check errored or was skipped.

USAGE
-----
  AWS_PROFILE=default python3 scripts/review_routing_live_check.py scenarios \\
      --stack-name IDP1 --region us-west-2 --state-file scratch/routing-state.json
  AWS_PROFILE=default python3 scripts/review_routing_live_check.py cleanup \\
      --stack-name IDP1 --region us-west-2 --state-file scratch/routing-state.json

Around a stack update, with the same three options each time: ``capture-switch``,
the update, then ``check-switch``, ``reasons-and-restart``, ``scenario-rounds``,
``scenarios``, ``seed-demo`` and ``cleanup``.

Requires: awscli v2 on PATH and this checkout's packages importable (``make``
pins them); ``idp-cli`` on PATH or beside the interpreter (``reasons-and-restart``
reads ``idp-cli status``); credentials with Cognito admin, CloudFormation read,
S3 read/write on the input and output buckets and list on the configuration
bucket, DynamoDB read/write on the review queue and tracking tables, Step
Functions read, and CloudWatch Logs Insights. ``scenarios`` processes the lending
package twice and takes most of an hour; ``reasons-and-restart`` processes it
once.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_queues_live_check as queues_check  # noqa: E402
from review_queues_live_check import (  # noqa: E402
    ERROR,
    EXIT_FAIL,
    EXIT_INCOMPLETE,
    EXIT_PASS,
    FAIL,
    PASS,
    POLL_SECONDS,
    QUEUE_CACHE_SECONDS,
    QUEUE_INPUT_FIELDS,
    SKIP,
    STATUS_SEGMENT,
    CheckAborted,
    CheckTimedOut,
    Deps,
    Report,
    change_one_field,
)

REPO = Path(__file__).resolve().parents[1]

STATE_VERSION = 1
KEY_PREFIX = "review-routing-live"
DEMO_PREFIX = "review-routing-demo"

SHOWCASE_PROFILE = "review-routing-showcase"
SHOWCASE_CONFIG = REPO / "config_library" / "unified" / SHOWCASE_PROFILE / "config.yaml"
#: The queues the showcase's rules name, by id, with the name each is created with.
SHOWCASE_QUEUES = {
    "approvals": "Approvals",
    "compliance": "Compliance",
    "specialist": "Specialist",
}
DEFAULT_QUEUE = "default"
DEFAULT_PROFILE = "default"
BUILTIN_CONFIDENCE_RULE = "builtin-confidence-alerts"
#: The showcase rule that sends a W-2 to the Default queue.
SHOWCASE_W2_RULE = "default-w2-forms"

#: Cognito groups of the temporary users, by the role name used in this script.
#: ``bystander`` is a second Annotator: one who is on a queue and holds no item.
ROLE_GROUPS = {
    "annotator": "Annotator",
    "bystander": "Annotator",
    "reviewer": "Reviewer",
    "author": "Author",
    "admin": "Admin",
}
#: The temporary users ``scenarios`` signs in as.
SCENARIO_ROLES = ("annotator", "reviewer", "author", "admin")

OUTPUT_KEYS = {
    **queues_check.OUTPUT_KEYS,
    "state_machine": "StateMachineArn",
    "config_bucket": "S3ConfigurationBucketName",
}

HELD = "HITL_IN_PROGRESS"
COMPLETED = "COMPLETED"
REJECTED = "REJECTED"
FAILED = "FAILED"

ROUTE_TIMEOUT_SECONDS = 20 * 60
#: A package of many sections: OCR, classification and one extraction per section.
LONG_ROUTE_TIMEOUT_SECONDS = 45 * 60
FINALIZE_TIMEOUT_SECONDS = 20 * 60
#: An ID token lasts an hour; one older than this is minted again.
TOKEN_MAX_AGE_SECONDS = 25 * 60
QUERY_TIMEOUT_SECONDS = 120
#: The status object is written after the tracking row; how long to wait for it.
STATUS_OBJECT_WAIT_SECONDS = 180
STATUS_OBJECT_POLL_SECONDS = 5
ROUTING_P95_TARGET_MS = 1500.0
NOTE = "review routing live check"

# ------------------------------------------------- reasons, plans and restarts
#: How a reason is told from one whose placeholders were not filled in.
BRACE = re.compile(r"[{}]")
REASON_SOURCE_ADMIN = "admin"
#: How much of a reason or of a failed policy rule's explanation a detail shows.
SHOWN_TEXT = 300
#: The steps a run plan names, as ``getReviewCompletionPlan`` and
#: ``getProcessChangesPlan`` return them.
STEP_CLASSIFICATION = "classification"
STEP_EXTRACTION = "extraction"
STEP_APPLY_REVIEW = "apply_review_changes"
STEP_POLICY_RULE_VALIDATION = "policy_rule_validation"
PLANNED_STATES = frozenset({"run", "skipped", "not_enabled"})
PLANNED_SKIPPED = "skipped"
#: Why a completed review extracts a section again.
CAUSE_ADDED = "added"
CAUSE_PAGES_CHANGED = "pages_changed"
#: The key of a section result that holds the record of its review
#: (``idp_common.review_queues.models.REVIEW_KEY``).
REVIEW_RECORD_KEY = "review"
#: The suffix the simulated reviewer gives a value they correct.
CORRECTED_SUFFIX = " (reviewed)"

#: The warm p95 ``getReviewCompletionPlan`` is to stay under.
PLAN_P95_TARGET_MS = 1000.0
#: How many times the operation is called to be measured.
PLAN_LATENCY_CALLS = 20
#: This check makes no call to the review queues function for this long before
#: and after the measured calls, and reads the function's log for the period of
#: the calls widened by the margin on both sides. The period then holds the
#: measured calls and no other call of this check while this machine's clock and
#: the log's differ by less than the margin.
PLAN_QUIET_SECONDS = 6.0
PLAN_CLOCK_MARGIN_SECONDS = 3
#: How long the log events of the measured calls are waited for.
PLAN_LOG_WAIT_SECONDS = 180.0
PLAN_LOG_POLL_SECONDS = 15.0
#: The nested stack that owns the API functions, and the log group of the function
#: that serves the review queue operations, by their logical ids.
API_STACK = "APIRESOLVERSTACK"
PLAN_RESOLVER_LOG_GROUP = "ReviewQueuesResolverFunctionLogGroup"

# ------------------------------------------------------- the Review Routing switch
#: The profiles whose switch a stack update must not change.
SWITCH_PROFILES = (DEFAULT_PROFILE, SHOWCASE_PROFILE)
#: Where the configuration bucket keeps the body of each revision of a profile
#: (``idp_common.config.revisions.REVISION_S3_PREFIX``).
REVISION_PREFIX = "config_revisions"

# ---------------------------------------------------------------- state machine
#: A state only the definition before review routing has.
OLD_DEFINITION_STATE = "CheckHITLRequired"
#: The assembly states under the names that definition gives them. Their task
#: decides there whether the document is held.
OLD_ASSEMBLY_STATES = frozenset(
    {"Pipeline_ProcessResultsStep", "BDA_ProcessResultsStep", "BDA_ProcessResultsSkip"}
)
#: The nested stack that owns the workflow, and the functions the assembly states
#: call, by their logical ids.
PATTERN_STACK = "PATTERNSTACK"
ASSEMBLY_FUNCTIONS = ("ProcessResultsFunction", "BDAProcessResultsFunction")
RUN_PLAN_STATE = "CheckRunPlan"
ROUTING_STATE = "RoutingStep"
APPLY_REVIEW_STATE = "ApplyReviewChangesStep"
REJECTED_END_STATE = "SetRejectedStatus"
START_AT_POLICY_STATE = "StartAtPolicyRuleValidation"
#: Where a sent-back review starts: OCR is kept and the split is done again.
START_AT_CLASSIFICATION_STATE = "StartAtClassification"
#: Where a completed review starts when the reviewer regrouped sections, the hook
#: state each extracted section passes, and the two states that take the run from
#: the assembly to applying the review.
START_AT_EXTRACTION_SECTIONS_STATE = "StartAtExtractionForSections"
POST_EXTRACTION_HOOK_STATE = "PostExtractionHook"
CHECK_REVIEW_STATE = "CheckReviewToApply"
SELECT_ASSEMBLED_STATE = "SelectAssembledDocument"
OCR_STATE = "OCRStep"
#: The task states of the steps before extraction (Pipeline mode).
BEFORE_EXTRACTION_STATES = frozenset({OCR_STATE, "ClassificationStep"})
SKIP_POLICY_STATE = "SkipPolicyRuleValidation"
POLICY_FIRST_STATE = "PolicyClassificationStep"
EXTRACTION_STATES = frozenset(
    {
        "ExtractionPlanStep",
        "ShardExtractionStep",
        "ExtractionMergeStep",
        "ExtractionStep",
    }
)
#: The task states of the steps before policy rule validation, in both modes.
EARLY_STEP_STATES = (
    EXTRACTION_STATES
    | BEFORE_EXTRACTION_STATES
    | frozenset(
        {
            "AssessmentStep",
            "InvokeDataAutomationStep",
            "BDA_AssembleDocumentStep",
            "AssembleExistingDocumentStep",
        }
    )
)
#: Every state of the current definition this check names. An offline test compares
#: it with the definition, so a renamed state fails there and not as a vacuous pass.
NAMED_STATES = EARLY_STEP_STATES | frozenset(
    {
        RUN_PLAN_STATE,
        ROUTING_STATE,
        APPLY_REVIEW_STATE,
        REJECTED_END_STATE,
        START_AT_POLICY_STATE,
        START_AT_CLASSIFICATION_STATE,
        START_AT_EXTRACTION_SECTIONS_STATE,
        POST_EXTRACTION_HOOK_STATE,
        CHECK_REVIEW_STATE,
        SELECT_ASSEMBLED_STATE,
        SKIP_POLICY_STATE,
        POLICY_FIRST_STATE,
    }
)
#: The history events that mean a task was handed to a Lambda function.
SCHEDULED_EVENTS = frozenset({"TaskScheduled", "LambdaFunctionScheduled"})

LATENCY_STATS = (
    "| stats count(*) as n, pct(@duration, 50) as p50, pct(@duration, 95) as p95"
)
#: An invocation that did not initialise a new execution environment.
WARM_FILTER = 'filter @type = "REPORT" and not ispresent(@initDuration) '
ALL_FILTER = 'filter @type = "REPORT" '
LATENCY_FUNCTIONS = ("RoutingFunction", "ApplyReviewChangesFunction")


@dataclass(frozen=True)
class Sample:
    """A showcase sample: the file, and where the preset's README says it lands."""

    name: str
    path: Path
    queue: str
    rule: str
    timeout: float


SHOWCASE_SAMPLES = (
    Sample(
        "lending",
        REPO / "samples" / "lending_package.pdf",
        "specialist",
        "specialist-bank-checks",
        LONG_ROUTE_TIMEOUT_SECONDS,
    ),
    Sample(
        "statement",
        REPO / "samples" / "bank-statement-multipage.pdf",
        "compliance",
        "compliance-policy-failure",
        LONG_ROUTE_TIMEOUT_SECONDS,
    ),
    Sample(
        "invoice",
        REPO / "samples" / "fcc-invoices-7429ba503d96718740c2f714598b40a4.pdf",
        "approvals",
        "approvals-high-value",
        ROUTE_TIMEOUT_SECONDS,
    ),
    Sample(
        "w2",
        queues_check.SAMPLE,
        DEFAULT_QUEUE,
        SHOWCASE_W2_RULE,
        ROUTE_TIMEOUT_SECONDS,
    ),
)
SAMPLES_BY_NAME = {s.name: s for s in SHOWCASE_SAMPLES}
#: The sample a reviewer regroups: a package with sections of several pages.
REGROUP_SAMPLE = SAMPLES_BY_NAME["lending"]

SCENARIO_DIR = REPO / "samples" / "review-routing"
#: The class of the scenario invoices, the policy that checks them and its rule.
SCENARIO_CLASS = "Scenario-Invoice"
SCENARIO_POLICY = "scenario_invoice_checks"
NET_AMOUNT_POLICY_RULE = "net_amount_matches"
REGISTERED_VENDOR = "AnyCompany Office Supplies"


@dataclass(frozen=True)
class Round:
    """One review round of a scenario document.

    ``rule`` is the routing rule that places the document in ``queue``; it is
    empty for the round in which no routing rule matches and the document
    completes. ``reason_has`` is a value of the document the reviewer's reason
    names, ``policy_failure`` the failed policy rule listed first, and
    ``correction`` what the simulated reviewer sets, by field, before completing
    the review.
    """

    rule: str = ""
    queue: str = ""
    reason_has: str = ""
    policy_failure: str = ""
    correction: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Scenario:
    """A scenario invoice, and the rounds the showcase's README says it goes through."""

    name: str
    path: Path
    rounds: tuple[Round, ...]

    @property
    def first(self) -> Round:
        return self.rounds[0]

    def sample(self, expected: Round) -> Sample:
        """Where a round places the document, as ``check_lands`` reads it."""
        return Sample(
            f"scenario-{self.name}",
            self.path,
            expected.queue,
            expected.rule,
            ROUTE_TIMEOUT_SECONDS,
        )


_VENDOR_ROUND = Round(
    "scenario-vendor-not-approved",
    "approvals",
    "AnyCo Office",
    correction=(("Vendor", REGISTERED_VENDOR),),
)


def _terms_round(invoice_number: str) -> Round:
    return Round(
        "scenario-payment-terms-missing",
        DEFAULT_QUEUE,
        invoice_number,
        correction=(("PaymentTerms", "Net 30"),),
    )


SCENARIOS = (
    Scenario(
        "one",
        SCENARIO_DIR / "routing-scenario-one-problem.pdf",
        (_VENDOR_ROUND, Round()),
    ),
    Scenario(
        "two",
        SCENARIO_DIR / "routing-scenario-two-problems.pdf",
        (
            Round(
                "scenario-net-amount-check",
                "compliance",
                "1250",
                policy_failure=NET_AMOUNT_POLICY_RULE,
                correction=(("NetAmountDue", 1100),),
            ),
            _terms_round("INV-2002"),
            Round(),
        ),
    ),
    Scenario(
        "three",
        SCENARIO_DIR / "routing-scenario-three-problems.pdf",
        (
            # A reviewer cannot add a purchase order: the review is completed as it is.
            Round("scenario-po-without-purchase-order", "approvals", "PO-backed"),
            _VENDOR_ROUND,
            _terms_round("INV-3003"),
            Round(),
        ),
    ),
)
SCENARIOS_BY_NAME = {s.name: s for s in SCENARIOS}


# ------------------------------------------------------------------ the history
@dataclass(frozen=True)
class History:
    """What one execution did: the states it entered and the tasks it scheduled."""

    #: State names, in the order they were entered (a state of a Map is listed once
    #: per iteration).
    entered: tuple[str, ...] = ()
    #: The state of every task handed to a Lambda function, in order.
    scheduled: tuple[str, ...] = ()
    #: When each state of ``entered`` was entered.
    entered_at: tuple[datetime | None, ...] = ()

    def has(self, state: str) -> bool:
        return state in self.entered

    def first_entered_at(self, states: Iterable[str]) -> datetime | None:
        """When the run first entered one of ``states``; ``None`` when it did not."""
        wanted = set(states)
        for name, at in zip(self.entered, self.entered_at):
            if name in wanted:
                return at
        return None

    def before(self, first: str, second: str) -> bool:
        """True when both were entered, ``first`` before ``second``."""
        if first not in self.entered or second not in self.entered:
            return False
        return self.entered.index(first) < self.entered.index(second)

    def touched(self, states: Iterable[str]) -> list[str]:
        """Which of ``states`` the run entered or scheduled a task for."""
        seen = set(self.entered) | set(self.scheduled)
        return sorted(s for s in states if s in seen)

    def summary(self, limit: int = 10) -> str:
        shown = ", ".join(self.entered[:limit])
        more = (
            f", … {len(self.entered) - limit} more" if len(self.entered) > limit else ""
        )
        return f"entered [{shown}{more}]; {len(self.scheduled)} task(s) scheduled"


def _state_of(event: Mapping[str, Any], by_id: Mapping[int, Mapping[str, Any]]) -> str:
    """The state an event belongs to: the nearest ``…StateEntered`` before it."""
    seen: set[int] = set()
    current: Mapping[str, Any] | None = event
    while current is not None and current.get("id") not in seen:
        seen.add(current.get("id", -1))
        if str(current.get("type", "")).endswith("StateEntered"):
            return str((current.get("stateEnteredEventDetails") or {}).get("name", ""))
        current = by_id.get(current.get("previousEventId", 0))
    return ""


def analyse_history(events: Iterable[Mapping[str, Any]]) -> History:
    """Read ``GetExecutionHistory`` events into the states entered and tasks scheduled."""
    ordered = sorted(events, key=lambda e: e.get("id", 0))
    by_id = {e.get("id", 0): e for e in ordered}
    entered: list[str] = []
    entered_at: list[datetime | None] = []
    scheduled: list[str] = []
    for event in ordered:
        kind = str(event.get("type", ""))
        if kind.endswith("StateEntered"):
            entered.append(
                str((event.get("stateEnteredEventDetails") or {}).get("name", ""))
            )
            entered_at.append(parse_time(event.get("timestamp")))
        elif kind in SCHEDULED_EVENTS:
            scheduled.append(_state_of(event, by_id))
    return History(tuple(entered), tuple(scheduled), tuple(entered_at))


def parse_time(value: Any) -> datetime | None:
    """A timestamp the AWS CLI printed, as an aware datetime; ``None`` when it is not one."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _utc(moment: datetime) -> str:
    """A time as text in UTC, so that two of them can be compared by eye."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def in_flight_verdict(
    history: History,
    status: str | None,
    review_reasons: int | None,
    functions_updated: tuple[datetime, datetime] | None,
) -> tuple[str, str]:
    """What a document started before the update shows (FR-69).

    The requirement is about a run that calls the updated functions: a run on the
    new definition, or a run on the old definition whose assembly step, which
    decides the hold there, ran after the assembly functions were updated. Such a
    run must be held when the document needs review. A run that called none of the
    updated functions, and a document that needed no review, show nothing about
    the update: SKIP, never PASS.

    ``review_reasons`` is the number of confidence alerts and missing required
    fields the tracking row counts; ``functions_updated`` is when the update of the
    assembly functions began and when it was complete.
    """
    updated_code = True
    if history.has(ROUTING_STATE):
        ran = f"ran on the new definition ({ROUTING_STATE} entered)"
    elif history.has(OLD_DEFINITION_STATE):
        decided = history.first_entered_at(OLD_ASSEMBLY_STATES)
        if decided is None or functions_updated is None:
            return (
                SKIP,
                "ran on the old definition; when its assembly step ran, or when "
                "the assembly functions were updated, is unknown",
            )
        began, complete = functions_updated
        if decided < began:
            return (
                SKIP,
                f"ran on the old definition and its assembly step ran "
                f"{_utc(decided)}, before the assembly functions were "
                f"updated ({_utc(began)}): it called no updated function",
            )
        updated_code = decided >= complete
        ran = (
            f"ran on the old definition and its assembly step ran "
            f"{_utc(decided)}, after the update of the assembly functions "
            f"{'was complete' if updated_code else 'began'} "
            f"({_utc(complete if updated_code else began)})"
        )
    else:
        return (
            ERROR,
            f"neither definition's review state was entered; {history.summary()}",
        )
    if status == HELD:
        if updated_code:
            return PASS, f"{ran}; status={status}"
        return (
            SKIP,
            f"{ran} and before it was complete; status={status}, and which code "
            "held the document is not known",
        )
    if status == COMPLETED and review_reasons == 0:
        return (
            SKIP,
            f"{ran}; status={status} with no confidence alert and no missing "
            "required field: the document needed no review",
        )
    return FAIL, f"{ran}; status={status}, review reasons counted: {review_reasons}"


# -------------------------------------------------------------------- the state
_FORBIDDEN_KEY = re.compile(r"(^|_)(password|token|secret|arn)($|_)", re.IGNORECASE)
#: Twelve digits standing alone, as an account id does in an ARN. An execution name
#: ends in a message id, whose last group can be twelve digits after a hyphen.
_ACCOUNT_ID = re.compile(r"(?<![\w-])\d{12}(?![\w-])")


def assert_publishable(value: Any, where: str = "state") -> None:
    """Refuse a state that holds a credential, an account id or a real address."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _FORBIDDEN_KEY.search(str(key)):
                raise ValueError(
                    f"{where}.{key}: a state file key may not name a secret"
                )
            assert_publishable(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_publishable(item, f"{where}[{index}]")
    elif isinstance(value, str):
        if _ACCOUNT_ID.search(value) or value.startswith("arn:"):
            raise ValueError(f"{where}: holds an account id or an ARN")
        if value.startswith("eyJ"):
            raise ValueError(f"{where}: looks like a token")
        if "@" in value and not value.endswith("@example.invalid"):
            raise ValueError(
                f"{where}: holds an address that is not a temporary user's"
            )


def new_state(stack: str, region: str, tag: str) -> dict:
    return {
        "version": STATE_VERSION,
        "stack": stack,
        "region": region,
        "tag": tag,
        "profile": "",
        "users": {},
        "listed_on": [],
        "held": [],
        "in_flight": [],
        "in_flight_started_at": "",
        "documents": [],
        "queues_created": [],
        "showcase_profile_created": False,
        "scenarios_started_at": "",
        "seeded": {},
        "switch": {},
    }


def load_state(path: Path, stack: str, region: str, tag: str) -> dict:
    """The state at ``path``, or a new one. A state of another stack is refused."""
    if not path.is_file():
        return new_state(stack, region, tag)
    state = json.loads(path.read_text())
    if state.get("version") != STATE_VERSION:
        raise CheckAborted(f"{path} is not a version {STATE_VERSION} state file")
    if (state.get("stack"), state.get("region")) != (stack, region):
        raise CheckAborted(
            f"{path} belongs to stack {state.get('stack')} ({state.get('region')}), "
            f"not {stack} ({region})"
        )
    return {**new_state(stack, region, state.get("tag") or tag), **state}


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    assert_publishable(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    partial.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    partial.replace(path)


def execution_name(arn: str | None) -> str:
    """The name of an execution, which is the last part of its ARN."""
    return (arn or "").rsplit(":", 1)[-1]


def execution_arn(state_machine_arn: str, name: str) -> str:
    return f"{state_machine_arn.replace(':stateMachine:', ':execution:', 1)}:{name}"


class RedactingReport(Report):
    """A report whose details never show an account id."""

    def add(self, name: str, outcome: str, detail: str) -> None:
        super().add(name, outcome, _ACCOUNT_ID.sub("<account>", detail))


def default_upload_config_file(stack: str, region: str, profile: str, path: Path):
    """Upload a configuration file as a profile through the SDK, validated."""
    from idp_sdk import IDPClient

    result = IDPClient(stack_name=stack, region=region).config.upload(
        str(path), config_profile=profile
    )
    if not result.success:
        raise CheckAborted(f"upload of profile {profile} failed: {result.error}")


def default_document_status(stack: str, region: str, key: str) -> dict:
    """What ``idp-cli status --format json`` prints for a document on its own.

    The command looks a document up through the CLI batch that submitted it
    (``--document-id`` too), and the documents of this check are uploaded without
    one. So the document is read with the SDK, as the command reads each document
    of a batch, and printed by the command's own code: the conversion and the
    formatter of ``idp_cli``. What is not run is the command's batch lookup.
    """
    from idp_cli import display
    from idp_cli.cli import _batch_status_to_display_dicts
    from idp_sdk import IDPClient
    from idp_sdk.models import BatchStatus

    try:
        document = IDPClient(stack_name=stack, region=region).document.get_status(key)
    except Exception as e:  # noqa: BLE001 - any SDK fault means the check cannot run
        raise CheckAborted(
            f"the SDK could not read the document's status: {e}"
        ) from None
    status_data, stats = _batch_status_to_display_dicts(
        BatchStatus(
            batch_id=key,
            documents=[document],
            total=1,
            completed=0,
            failed=0,
            in_progress=0,
            queued=0,
            success_rate=0.0,
            all_complete=False,
        )
    )
    try:
        printed = json.loads(display.format_status_json(status_data, stats))
    except ValueError:
        printed = None
    if not isinstance(printed, dict):
        raise CheckAborted("the status formatter of idp-cli printed no JSON object")
    return printed


@dataclass
class RoutingDeps(Deps):
    """Everything that touches AWS or the network, injectable for tests."""

    upload_config_file: Callable[..., None] = default_upload_config_file
    document_status: Callable[[str, str, str], dict] = default_document_status
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _as_json(value: Any) -> Any:
    """An AWSJSON field, which the API returns as text or as the object itself."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _plain(value: Any) -> Any:
    """A DynamoDB attribute value, as the AWS CLI prints it, as plain data."""
    if not isinstance(value, Mapping) or len(value) != 1:
        return value
    ((kind, held),) = value.items()
    if kind == "M":
        return {name: _plain(item) for name, item in held.items()}
    if kind == "L":
        return [_plain(item) for item in held]
    if kind == "N":
        number = float(held)
        return int(number) if number.is_integer() else number
    if kind == "NULL":
        return None
    # S, BOOL and the set types are held as what they are.
    return held


def _mapping(value: Any) -> dict:
    """``value`` when it is a JSON object, else an empty one."""
    return dict(value) if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[dict]:
    """The JSON objects of a list; anything else is left out."""
    return [dict(row) for row in value or [] if isinstance(row, Mapping)]


def _shown(text: Any) -> str:
    """A reason or an explanation as a detail shows it: one line, not too long."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= SHOWN_TEXT else line[: SHOWN_TEXT - 1] + "…"


@dataclass(frozen=True)
class Shown:
    """The reason and the failed policy rules of a placement, as one surface has them."""

    reason: str = ""
    #: The ids of the failed policy rules listed, in order.
    failed: tuple[str, ...] = ()
    count: int = 0

    @classmethod
    def read(cls, routed: Any, *, snake: bool = False) -> "Shown":
        """From a routed-by record: camelCase as the API and the tracking row
        write it, or snake_case as the status object and the CLI do."""
        routed = _mapping(routed)
        failures, count, rule_id = (
            ("policy_failures", "policy_failure_count", "rule_id")
            if snake
            else ("policyFailures", "policyFailureCount", "ruleId")
        )
        return cls(
            str(routed.get("reason") or ""),
            tuple(str(f.get(rule_id) or "") for f in _rows(routed.get(failures))),
            int(routed.get(count) or 0),
        )

    def text(self) -> str:
        return (
            f"reason: {_shown(self.reason) or 'none'}; failed policy rules: "
            f"{list(self.failed) or 'none'} of {self.count}"
        )


def plan_problems(plan: Any) -> list[str]:
    """What is wrong with a planned run as the API returned it; empty when nothing is.

    A planned run names its mode and its start step, and lists the steps of that
    mode in order, each with an id, a label and a state. The start step is among
    them and is not skipped.
    """
    if not isinstance(plan, Mapping):
        return ["not an object"]
    problems = [f"no {name}" for name in ("mode", "startStep") if not plan.get(name)]
    steps = _rows(plan.get("steps"))
    if not steps:
        problems.append("no steps")
    for step in steps:
        if not step.get("id") or not step.get("label"):
            problems.append("a step without an id or a label")
        if step.get("state") not in PLANNED_STATES:
            problems.append(f"step {step.get('id')} has state {step.get('state')!r}")
    start = [step for step in steps if step.get("id") == plan.get("startStep")]
    if steps and (not start or start[0].get("state") == PLANNED_SKIPPED):
        problems.append(
            f"the start step {plan.get('startStep')!r} is not a step it runs"
        )
    if not isinstance(plan.get("sections"), list):
        problems.append("no sections list")
    return problems


def plan_text(plan: Any) -> str:
    """A planned run in one line: its mode, its start, each step's state, its sections."""
    plan = _mapping(plan)
    steps = ", ".join(
        f"{step.get('id')}={step.get('state')}" for step in _rows(plan.get("steps"))
    )
    sections = ", ".join(
        f"{section.get('sectionId')} ({section.get('cause')})"
        for section in _rows(plan.get("sections"))
    )
    return (
        f"mode={plan.get('mode')} start={plan.get('startStep')} steps [{steps}] "
        f"sections [{sections}]"
    )


def string_field(result: Any) -> str:
    """A top-level field of a section result that holds text; "" when none does.

    Its name is a plain word, so the name is also the field's path.
    """
    inference = _mapping(_mapping(result).get("inference_result"))
    for name, value in inference.items():
        if isinstance(value, str) and value.strip() and str(name).isidentifier():
            return str(name)
    return ""


def with_fields(result: Mapping[str, Any], changes: Mapping[str, Any]) -> dict:
    """A copy of a section result with these top-level fields set."""
    data = copy.deepcopy(dict(result))
    if not isinstance(data.get("inference_result"), dict):
        raise CheckAborted("the section result has no inference_result object")
    data["inference_result"].update(changes)
    return data


def next_section_id(section_ids: Iterable[str]) -> str:
    """The id of a section a reviewer adds: one past every numeric id, as the
    page grouping editor of the web UI numbers it."""
    numeric = [int(section_id) for section_id in section_ids if section_id.isdigit()]
    return str(max(numeric, default=0) + 1)


# ---------------------------------------------------------------------- the run
@dataclass
class Tracking:
    status: str | None = None
    execution: str = ""
    #: The confidence alerts and missing required fields the row counts; ``None``
    #: when it counts neither.
    review_reasons: int | None = None
    #: The row's copy of the routed-by record; empty when the row has none.
    routed_by: dict = field(default_factory=dict)
    #: The document's sections as the row lists them: ``Id``, ``PageIds``,
    #: ``Class`` and ``OutputJSONUri``.
    sections: tuple[dict, ...] = ()


@dataclass
class SentBack:
    """A review that was sent back, and the run that followed."""

    #: The step the service queued the run from.
    start_step: str
    after: Tracking
    history: History
    #: The document's review item after the run; empty when it was not held again.
    item: dict
    #: The history entries of the cycle that was sent back.
    entries: list[tuple[str, dict]]
    cycle_id: str


@dataclass
class Run:
    """One subcommand's work against the stack: its fixtures, calls and waits."""

    args: argparse.Namespace
    deps: RoutingDeps
    report: Report
    state: dict = field(default_factory=dict)
    ctx: dict = field(default_factory=dict)
    out: dict = field(default_factory=dict)
    auth_enabled: bool = False
    passwords: dict = field(default_factory=dict)
    tokens: dict = field(default_factory=dict)
    #: The ids of the nested stacks, by logical id, once read. Never written to
    #: the state file.
    nested: dict = field(default_factory=dict)
    #: The execution each document had before this run uploaded it again, by key.
    #: What that execution left is not what the upload produced.
    replaced: dict = field(default_factory=dict)

    # ------------------------------------------------------------ lifecycle
    def open(self) -> None:
        d = self.deps
        self.state = load_state(
            Path(self.args.state_file), self.args.stack_name, self.args.region, d.rand()
        )
        self.ctx = d.resolve_stack(self.args.stack_name, self.args.region)
        rows = d.aws(
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            self.ctx["stack"],
            "--query",
            "Stacks[0].Outputs",
            "--output",
            "json",
            region=self.ctx["region"],
        )
        values = {r.get("OutputKey"): r.get("OutputValue") for r in rows or []}
        self.out = {k: (values.get(v) or "").strip() for k, v in OUTPUT_KEYS.items()}

    def require_outputs(self) -> bool:
        """False, with a SKIP, on a stack without review queues; raises when one is missing."""
        if not self.out["review_table"]:
            self.report.add(
                "stack-has-review-queues",
                SKIP,
                "no HITLTableName output — an older or headless stack",
            )
            return False
        missing = [k for k, v in self.out.items() if not v]
        if missing:
            raise CheckAborted(f"stack outputs missing: {', '.join(missing)}")
        return True

    def save(self) -> None:
        save_state(Path(self.args.state_file), self.state)

    def close(self) -> None:
        try:
            if self.auth_enabled:
                self.deps.restore_auth_flows(self.ctx)
        finally:
            self.save()

    def step(self, name: str, work: Callable[[], None]) -> None:
        """Run one check. A wait that ran out is a FAIL; a step that could not run, an ERROR."""
        try:
            work()
        except CheckTimedOut as e:
            self.report.add(name, FAIL, str(e))
        except CheckAborted as e:
            self.report.add(name, ERROR, str(e))
        except Exception as e:  # noqa: BLE001 - report, never mask as a pass
            self.report.add(name, ERROR, f"unexpected {type(e).__name__}: {e}")

    # ---------------------------------------------------------------- users
    @property
    def tag(self) -> str:
        return self.state["tag"]

    def ensure_user(self, role: str) -> str:
        """The temporary user of a role, created if need be, with a new password."""
        if not self.auth_enabled:
            self.deps.enable_admin_auth(self.ctx)
            self.auth_enabled = True
        email = self.state["users"].get(role) or (
            f"{KEY_PREFIX}-{role}-{self.tag}@example.invalid"
        )
        if role not in self.passwords:
            self.state["users"][role] = email
            self.save()
            password = "Aa1!" + secrets.token_urlsafe(24)
            self.deps.create_cognito_user(self.ctx, email, ROLE_GROUPS[role], password)
            self.passwords[role] = password
        return email

    def token(self, role: str) -> str:
        email = self.ensure_user(role)
        cached = self.tokens.get(role)
        if cached and self.deps.monotonic() - cached[1] < TOKEN_MAX_AGE_SECONDS:
            return cached[0]
        token = self.deps.get_id_token(self.ctx, email, self.passwords[role])
        if not token or len(token) < 100:
            raise CheckAborted(f"could not mint an ID token for the temporary {role}")
        self.tokens[role] = (token, self.deps.monotonic())
        return token

    # ------------------------------------------------------------------ API
    def api(self, role: str, field_name: str, args: dict) -> tuple[int, Any]:
        status, body = self.deps.call_body(
            self.ctx["api_base"], field_name, args, self.token(role)
        )
        if status in (0, None) or (isinstance(status, int) and status >= 500):
            raise CheckAborted(f"{field_name} did not complete: {status} {body!r:.200}")
        try:
            return status, json.loads(body or "")
        except (ValueError, TypeError):
            return status, None

    def must(self, role: str, field_name: str, args: dict) -> Any:
        code, body = self.api(role, field_name, args)
        if code != 200 or body is None:
            raise CheckAborted(f"{field_name} as {role} answered {code} {body!r:.200}")
        return body

    # --------------------------------------------------------------- queues
    def queue_definitions(self) -> list[dict]:
        """Every queue definition. The listing is paged; before review routing it was a plain list."""
        found: list[dict] = []
        args: dict = {}
        while True:
            page = self.must("admin", "listReviewQueues", args)
            paged = isinstance(page, dict)
            rows = page.get("items") if paged else page
            found.extend(q for q in rows or [] if isinstance(q, dict))
            next_token = page.get("nextToken") if paged else None
            if not next_token:
                return found
            args = {"nextToken": next_token}

    def queue_definition(self, queue_id: str) -> dict | None:
        for queue in self.queue_definitions():
            if queue.get("id") == queue_id or (
                queue_id == DEFAULT_QUEUE and queue.get("isDefaultQueue")
            ):
                return queue
        return None

    def _put_annotators(self, queue: dict, annotators: list[str]) -> None:
        update = {k: queue[k] for k in QUEUE_INPUT_FIELDS if queue.get(k) is not None}
        update["eligibleAnnotators"] = annotators
        self.must("admin", "updateReviewQueue", {"input": update})

    def list_annotator(
        self, queue_id: str, roles: tuple[str, ...] = ("annotator",)
    ) -> None:
        """List temporary Annotators on a queue, keeping who is listed already.

        Every role is listed in one write: a definition read again straight after
        a write can be the one from before it.
        """
        annotators = [self.ensure_user(role) for role in roles]
        queue = self.queue_definition(queue_id)
        if queue is None:
            raise CheckAborted(f"there is no queue {queue_id}")
        if queue_id == DEFAULT_QUEUE and not queue.get("enabled"):
            raise CheckAborted(
                "the Default queue is disabled, so no document is held for review; "
                "enable it in Review Studio › Review Management"
            )
        listed = [str(e).lower() for e in queue.get("eligibleAnnotators") or []]
        if queue_id not in self.state["listed_on"]:
            self.state["listed_on"].append(queue_id)
            self.save()
        missing = [annotator for annotator in annotators if annotator not in listed]
        if missing:
            self._put_annotators(queue, [*listed, *missing])

    def ensure_queue(
        self, queue_id: str, name: str, roles: tuple[str, ...] = ("annotator",)
    ) -> bool:
        """Create a queue that does not exist, and list the Annotators on it. True when created."""
        created = self.queue_definition(queue_id) is None
        if created:
            self.must(
                "admin",
                "createReviewQueue",
                {
                    "input": {
                        "id": queue_id,
                        "name": name,
                        "eligibleAnnotators": [
                            self.ensure_user(role) for role in roles
                        ],
                    }
                },
            )
            if queue_id not in self.state["queues_created"]:
                self.state["queues_created"].append(queue_id)
            if queue_id not in self.state["listed_on"]:
                self.state["listed_on"].append(queue_id)
            self.save()
        else:
            self.list_annotator(queue_id, roles)
        return created

    def wait_out_queue_cache(self) -> None:
        """Each function caches the queue definitions briefly; a change shows after that."""
        self.deps.sleep(QUEUE_CACHE_SECONDS)

    # ------------------------------------------------------------- profiles
    def profile_exists(self, profile: str) -> bool:
        code, body = self.api("admin", "getConfigVersion", {"versionName": profile})
        return (
            code == 200
            and isinstance(body, dict)
            and bool(body.get("success"))
            and bool(_as_json(body.get("Custom")))
        )

    def ensure_showcase(self, *, bystander_queue: str = "") -> None:
        """The three queues, created when they do not exist, and the showcase
        profile, uploaded again. ``bystander_queue`` is a queue the second
        temporary Annotator is listed on as well."""

        def roles(queue_id: str) -> tuple[str, ...]:
            both = queue_id == bystander_queue
            return ("annotator", "bystander") if both else ("annotator",)

        created = [
            queue_id
            for queue_id, name in SHOWCASE_QUEUES.items()
            if self.ensure_queue(queue_id, name, roles(queue_id))
        ]
        self.list_annotator(DEFAULT_QUEUE, roles(DEFAULT_QUEUE))
        if not self.profile_exists(SHOWCASE_PROFILE):
            # Recorded before the upload, so a cleanup removes a half-made profile.
            self.state["showcase_profile_created"] = True
            self.save()
        # The profile is the preset as it is now, on every run. A profile stored
        # with one outcome per routing rule is in a shape the routing step does
        # not read, and a run after the preset changed must not test the one
        # before. A profile that was there before this check is replaced too, and
        # is not removed by the cleanup.
        self.deps.upload_config_file(
            self.ctx["stack"], self.ctx["region"], SHOWCASE_PROFILE, SHOWCASE_CONFIG
        )
        print(
            f"  queues {', '.join(SHOWCASE_QUEUES)} ready "
            f"({len(created)} created); profile {SHOWCASE_PROFILE} uploaded"
        )
        self.wait_out_queue_cache()

    def revision_bodies(self, profile: str) -> dict[str, str]:
        """The revision bodies of a profile: each object's file name and entity tag.

        A body is written once, under its revision number, so an entity tag that
        differs later means the body was written again.
        """
        rows = self.deps.aws(
            "s3api",
            "list-objects-v2",
            "--bucket",
            self.out["config_bucket"],
            "--prefix",
            f"{REVISION_PREFIX}/{profile}/",
            "--query",
            "Contents[].[Key, ETag]",
            region=self.ctx["region"],
        )
        return {
            str(key).rsplit("/", 1)[-1]: str(tag).strip('"') for key, tag in rows or []
        }

    def switch(self, profile: str) -> dict:
        """Review Routing of a profile as it is stored, and its revision bodies.

        The default profile is read whole. Any other profile stores only what
        differs from it, so its value is ``None`` when it does not set the switch.
        """
        answer = self.must("admin", "getConfigVersion", {"versionName": profile})
        stored = _mapping(
            _as_json(answer.get("Default" if profile == DEFAULT_PROFILE else "Custom"))
        )
        exists = bool(answer.get("success")) and bool(stored)
        enabled = _mapping(stored.get("hitl")).get("enabled")
        return {
            "exists": exists,
            "enabled": enabled if isinstance(enabled, bool) else None,
            "revisions": self.revision_bodies(profile) if exists else {},
        }

    # ------------------------------------------------------------ documents
    def upload(
        self, key: str, path: Path, profile: str, *, extra: str = "", track: bool = True
    ) -> str:
        if not Path(path).is_file():
            raise CheckAborted(f"sample not found: {path}")
        if track and key not in self.state["documents"]:
            self.state["documents"].append(key)
            self.save()
        # A key that was uploaded before still shows what that upload's run left,
        # until the run of this upload starts.
        self.replaced[key] = self.tracking(key).execution
        self.deps.aws(
            "s3api",
            "put-object",
            "--bucket",
            self.out["input_bucket"],
            "--key",
            key,
            "--body",
            str(path),
            "--metadata",
            f"config-version={profile}{extra}",
            region=self.ctx["region"],
        )
        print(f"  uploaded {key} under profile {profile}")
        return key

    def key_for(self, label: str, path: Path) -> str:
        return f"{KEY_PREFIX}-{self.tag}/{label}-{Path(path).name}"

    def fresh_key(self, label: str, path: Path) -> str:
        """A key no earlier run of this check used: the document is new each time
        the subcommand is run. The file keeps its name, which a class can be
        recognised by."""
        earlier = sum(f"/{label}-" in key for key in self.state["documents"])
        return self.key_for(f"{label}-{earlier + 1}", path)

    def tracking(self, key: str) -> Tracking:
        row = self.deps.aws(
            "dynamodb",
            "get-item",
            "--table-name",
            self.out["tracking_table"],
            "--key",
            json.dumps({"PK": {"S": f"doc#{key}"}, "SK": {"S": "none"}}),
            "--consistent-read",
            region=self.ctx["region"],
        )
        item = (row or {}).get("Item") or {}
        counts = [
            int(item[name]["N"])
            for name in ("ConfidenceAlertCount", "MissingRequiredFieldCount")
            if "N" in (item.get(name) or {})
        ]
        return Tracking(
            status=(item.get("ObjectStatus") or {}).get("S"),
            execution=execution_name((item.get("WorkflowExecutionArn") or {}).get("S")),
            review_reasons=sum(counts) if counts else None,
            routed_by=_mapping(_plain(item.get("ReviewRoutedBy"))),
            sections=tuple(_rows(_plain(item.get("Sections")))),
        )

    def wait_tracking_routed_by(self, key: str) -> dict:
        """The tracking row's routed-by record. The row gets it after the status
        this check waited for, so it is read for a short while."""
        deadline = self.deps.monotonic() + STATUS_OBJECT_WAIT_SECONDS
        while True:
            routed = self.tracking(key).routed_by
            if routed or self.deps.monotonic() >= deadline:
                return routed
            self.deps.sleep(STATUS_OBJECT_POLL_SECONDS)

    def item_row(self, key: str) -> dict:
        """The review item of a document as the review table stores it; empty when
        it has none. It holds what the API does not return, such as how many
        times in a row the document was placed (``PlacementCount``)."""
        row = self.deps.aws(
            "dynamodb",
            "get-item",
            "--table-name",
            self.out["review_table"],
            "--key",
            json.dumps({"PK": {"S": f"DOC#{key}"}, "SK": {"S": "ITEM"}}),
            "--consistent-read",
            region=self.ctx["region"],
        )
        return _mapping(_plain({"M": (row or {}).get("Item") or {}}))

    def read_uri(self, uri: str) -> Any:
        try:
            return self.deps.aws("s3", "cp", uri, "-", region=self.ctx["region"])
        except RuntimeError:
            return None

    def status_object(self, key: str) -> dict:
        body = self.read_uri(f"s3://{self.out['output_bucket']}/{key}/{STATUS_SEGMENT}")
        return body if isinstance(body, dict) else {}

    def wait_status_object(self, key: str, want: str) -> dict:
        """The document's review status object once it says ``want``.

        The object is written last, after the tracking row, so it can lag the status
        this check waited for. It is read for a short while, and returned as last
        seen: the caller's check decides.
        """
        deadline = self.deps.monotonic() + STATUS_OBJECT_WAIT_SECONDS
        while True:
            status = self.status_object(key)
            if status.get("status") == want or self.deps.monotonic() >= deadline:
                return status
            self.deps.sleep(STATUS_OBJECT_POLL_SECONDS)

    def review_rows(self, key: str) -> list[dict]:
        rows = self.deps.aws(
            "dynamodb",
            "query",
            "--table-name",
            self.out["review_table"],
            "--key-condition-expression",
            "PK = :pk",
            "--expression-attribute-values",
            json.dumps({":pk": {"S": f"DOC#{key}"}}),
            "--projection-expression",
            "PK, SK",
            region=self.ctx["region"],
        )
        return list((rows or {}).get("Items") or [])

    def history_entries(self, key: str, cycle_id: str) -> list[tuple[str, dict]]:
        """The entries recorded for one review cycle of a document, oldest first:
        each event with its details.

        Read from the table: the API returns the history of the document's current
        cycle only, and a send-back or a completion ends the cycle it is recorded on.
        """
        rows = self.deps.aws(
            "dynamodb",
            "query",
            "--table-name",
            self.out["review_table"],
            "--key-condition-expression",
            "PK = :pk AND begins_with(SK, :cycle)",
            "--expression-attribute-values",
            json.dumps({":pk": {"S": f"DOC#{key}"}, ":cycle": {"S": f"H#{cycle_id}#"}}),
            "--projection-expression",
            "#event, #details",
            "--expression-attribute-names",
            json.dumps({"#event": "Event", "#details": "Details"}),
            region=self.ctx["region"],
        )
        return [
            (
                (row.get("Event") or {}).get("S", ""),
                _mapping(_plain(row.get("Details"))),
            )
            for row in (rows or {}).get("Items") or []
        ]

    # ---------------------------------------------------------------- waits
    def wait_documents(
        self, wanted: Mapping[str, tuple[frozenset[str], float]]
    ) -> dict[str, Tracking]:
        """Wait for several documents at once; return what each was last seen as.

        ``wanted`` maps a key to the statuses that end its wait and its timeout. A
        document that failed, or whose time ran out, is returned as last seen; the
        caller's check decides, so a timeout is never read as a pass. A document
        that still shows the run of an earlier upload of its key has not been
        seen: it is returned with no status.
        """
        started = self.deps.monotonic()
        seen: dict[str, Tracking] = {}
        waiting = set(wanted)
        while True:
            for key in sorted(waiting):
                found = self.tracking(key)
                statuses, timeout = wanted[key]
                earlier = bool(found.execution) and (
                    found.execution == self.replaced.get(key)
                )
                seen[key] = Tracking() if earlier else found
                if (
                    seen[key].status in statuses
                    or seen[key].status == FAILED
                    or self.deps.monotonic() - started >= timeout
                ):
                    waiting.discard(key)
            if not waiting:
                return seen
            self.deps.sleep(POLL_SECONDS)

    def wait_next_run(
        self, key: str, previous: str, statuses: frozenset[str], timeout: float
    ) -> Tracking:
        """Wait until a run other than ``previous`` leaves the document in one of ``statuses``."""
        deadline = self.deps.monotonic() + timeout
        while True:
            seen = self.tracking(key)
            moved = bool(seen.execution) and seen.execution != previous
            if moved and seen.status in statuses:
                return seen
            if moved and seen.status == FAILED:
                raise CheckAborted(f"the run {seen.execution} of {key} failed")
            if self.deps.monotonic() >= deadline:
                raise CheckTimedOut(
                    f"timed out after {int(timeout)}s waiting for a new run of {key} to "
                    f"reach {'/'.join(sorted(statuses))} (last seen: status={seen.status}, "
                    f"run={'new' if moved else 'unchanged'})"
                )
            self.deps.sleep(POLL_SECONDS)

    # ----------------------------------------------------------- executions
    def history(self, name: str) -> History:
        if not name:
            raise CheckAborted("the document has no execution recorded")
        result = self.deps.aws(
            "stepfunctions",
            "get-execution-history",
            "--execution-arn",
            execution_arn(self.out["state_machine"], name),
            "--no-include-execution-data",
            region=self.ctx["region"],
        )
        return analyse_history((result or {}).get("events") or [])

    def resource_id(self, stack: str, logical_id: str) -> str:
        """The physical id of a stack's resource; "" when the stack has none."""
        return str(
            self.deps.aws(
                "cloudformation",
                "describe-stack-resources",
                "--stack-name",
                stack,
                "--logical-resource-id",
                logical_id,
                "--query",
                "StackResources[0].PhysicalResourceId",
                region=self.ctx["region"],
            )
            or ""
        )

    def nested_stack(self, logical_id: str) -> str:
        """The id of one of the stack's nested stacks; "" when there is none."""
        if logical_id not in self.nested:
            self.nested[logical_id] = self.resource_id(self.ctx["stack"], logical_id)
        return self.nested[logical_id]

    def pattern_stack(self) -> str:
        """The id of the nested stack that owns the workflow; "" when there is none."""
        return self.nested_stack(PATTERN_STACK)

    def pattern_log_group(self, function: str) -> str:
        """A workflow function's log group. The pattern template names it after its
        own stack, the nested one: ``/<nested stack name>/lambda/<logical id>``."""
        nested = self.pattern_stack()
        if not nested:
            raise CheckAborted(f"the stack has no {PATTERN_STACK} resource")
        # A stack id is ``arn:…:stack/<name>/<guid>``.
        return f"/{nested.split('/')[1]}/lambda/{function}"

    def plan_resolver_log_group(self) -> str:
        """The log group of the function that serves ``getReviewCompletionPlan``.
        The API template gives it no name, so the name is the one the stack
        generated: the physical id of the log group resource."""
        nested = self.nested_stack(API_STACK)
        if not nested:
            raise CheckAborted(f"the stack has no {API_STACK} resource")
        group = self.resource_id(nested, PLAN_RESOLVER_LOG_GROUP)
        if not group:
            raise CheckAborted(
                f"the {API_STACK} stack has no {PLAN_RESOLVER_LOG_GROUP} resource"
            )
        return group

    def assembly_functions_updated(
        self, after: datetime | None
    ) -> tuple[datetime, datetime] | None:
        """When the first update of the assembly functions after ``after`` began and
        was complete, from the events of the nested stack that owns them."""
        nested = self.pattern_stack()
        if not nested:
            return None
        named = " || ".join(
            f"LogicalResourceId=='{name}'" for name in ASSEMBLY_FUNCTIONS
        )
        result = self.deps.aws(
            "cloudformation",
            "describe-stack-events",
            "--stack-name",
            nested,
            "--query",
            f"StackEvents[?ResourceType=='AWS::Lambda::Function' && ({named})]"
            ".[LogicalResourceId, ResourceStatus, Timestamp]",
            region=self.ctx["region"],
        )
        events = sorted(
            (at, name, status)
            for name, status, at in ((n, s, parse_time(t)) for n, s, t in result or [])
            if at is not None and (after is None or at >= after)
        )
        began = next((at for at, _, s in events if s == "UPDATE_IN_PROGRESS"), None)
        if began is None:
            return None
        # Each function's first completion after the update began; the last of them.
        completed: dict[str, datetime] = {}
        for at, name, status in events:
            if status == "UPDATE_COMPLETE" and at >= began:
                completed.setdefault(name, at)
        return (began, max(completed.values())) if completed else None

    # -------------------------------------------------------------- reviews
    def review_item(self, role: str, key: str) -> dict:
        detail = self.must(role, "getReviewItem", {"objectKey": key})
        if not isinstance(detail.get("item"), dict):
            raise CheckAborted(f"getReviewItem returned no item for {key}")
        return detail

    def claim(self, role: str, key: str) -> dict:
        """Claim the document's open review; return ``{objectKey, cycleId}``.

        The cycle is the item's own: the status object is written last, and after
        a second placement it can still be the one of the cycle before. An item
        is activated when the run that placed it has ended, a moment after the
        document's status says it is held, so the claim waits for that.
        """
        deadline = self.deps.monotonic() + STATUS_OBJECT_WAIT_SECONDS
        while True:
            item = self.review_item("admin", key)["item"]
            if item.get("state") != "HOLDING" or self.deps.monotonic() >= deadline:
                break
            self.deps.sleep(STATUS_OBJECT_POLL_SECONDS)
        item_args = {"objectKey": key, "cycleId": item.get("cycleId")}
        code, claimed = self.api(role, "claimReviewItem", item_args)
        if code != 200 or (claimed or {}).get("state") != "CLAIMED":
            raise CheckAborted(
                f"claimReviewItem as {role} answered {code} "
                f"state={(claimed or {}).get('state')}"
            )
        return item_args

    def section_result(self, section: Mapping[str, Any]) -> dict:
        """The stored result of a section of a review item."""
        result = self.read_uri(section.get("resultUri") or "")
        if not isinstance(result, dict):
            raise CheckAborted("could not read the section result")
        return result

    def save_draft(
        self, role: str, item_args: dict, section: Mapping[str, Any], data: dict
    ) -> None:
        self.must(
            role,
            "saveReviewDraft",
            {
                **item_args,
                "sectionId": section["sectionId"],
                "data": json.dumps(data),
                "baseRevision": section.get("draftRevision") or 0,
            },
        )

    def finish(self, role: str, item_args: dict, detail: Mapping[str, Any]) -> None:
        """Complete a claimed review, ticking the queue's checklist."""
        args = dict(item_args)
        if detail.get("checklist"):
            args["checklist"] = [True] * len(detail["checklist"])
        done = self.must(role, "completeReviewItem", args)
        if done.get("state") != "FINALIZING":
            raise CheckAborted(f"completeReviewItem left state {done.get('state')}")

    def complete(
        self, role: str, key: str, correction: Mapping[str, Any] | None = None
    ) -> dict:
        """Claim, save a draft and complete, as ``role``; return ``{objectKey, cycleId}``.

        With no ``correction``, one field of the first section is changed. With
        one, those fields of the scenario invoice's section are set, and an empty
        one completes the review as it is, with no draft.
        """
        item_args = self.claim(role, key)
        detail = self.review_item(role, key)
        if correction is None or correction:
            sections = _rows(detail.get("sections"))
            if not sections or not sections[0].get("resultUri"):
                raise CheckAborted("getReviewItem returned no section result")
            section = sections[0]
            if correction:
                invoices = [
                    s for s in sections if s.get("documentClass") == SCENARIO_CLASS
                ]
                section = invoices[0] if invoices else section
            result = self.section_result(section)
            data = (
                with_fields(result, correction)
                if correction
                else change_one_field(result)[0]
            )
            self.save_draft(role, item_args, section, data)
        self.finish(role, item_args, detail)
        return item_args

    def reject(self, role: str, key: str) -> None:
        item_args = self.claim(role, key)
        reasons = self.review_item(role, key).get("rejectReasons") or []
        if not reasons:
            raise CheckAborted("the item's queue lists no reject reason")
        self.must(
            role,
            "rejectReviewItem",
            {**item_args, "reason": reasons[0], "note": NOTE},
        )

    def send_back(
        self, role: str, key: str, item_args: dict, previous: str
    ) -> SentBack:
        """Send a claimed review back and wait for the run that follows.

        The call names no step: where the run starts is the service's decision.
        """
        answer = self.must(role, "sendBackReviewItem", {**item_args, "note": NOTE})
        if answer.get("status") != "QUEUED":
            raise CheckAborted(
                f"sendBackReviewItem answered {answer.get('status')} "
                f"{answer.get('reason')}: {answer.get('message')}"
            )
        after = self.wait_next_run(
            key, previous, frozenset({HELD, COMPLETED}), ROUTE_TIMEOUT_SECONDS
        )
        return SentBack(
            start_step=str(answer.get("startStep") or ""),
            after=after,
            history=self.history(after.execution),
            # A document that was not held again has no review to read.
            item=self.review_item("admin", key)["item"] if after.status == HELD else {},
            entries=self.history_entries(key, item_args["cycleId"]),
            cycle_id=item_args["cycleId"],
        )

    def check_send_back(self, sent: SentBack) -> None:
        """A sent-back review keeps its OCR and starts at classification, and the
        cycle that was sent back records that."""
        history = sent.history
        ocr = history.touched({OCR_STATE})
        self.report.check(
            "send-back-starts-at-classification",
            sent.start_step == STEP_CLASSIFICATION
            and history.has(START_AT_CLASSIFICATION_STATE)
            and not ocr,
            f"queued from {sent.start_step or 'no step'}; run {sent.after.execution}: "
            f"{history.summary()}; OCR touched: {ocr or 'none'}",
        )
        events = [event for event, _ in sent.entries]
        recorded = [details for event, details in sent.entries if event == "sent_back"]
        steps = [details.get("start_step") for details in recorded]
        new_cycle = sent.item.get("cycleId") not in (None, sent.cycle_id)
        self.report.check(
            "send-back-is-recorded-and-the-document-returns-to-a-queue",
            steps == [STEP_CLASSIFICATION] and new_cycle,
            f"status={sent.after.status} queue={sent.item.get('queue')} "
            f"new cycle={new_cycle} "
            f"events of the cycle sent back: {sorted(set(filter(None, events)))}; "
            f"start_step recorded: {steps or 'none'}",
        )

    # --------------------------------------------------------------- checks
    def check_lands(self, name: str, key: str, sample: Sample, seen: Tracking) -> bool:
        """A document is held in the queue its rule names, and the item says which rule."""
        if seen.status != HELD:
            return self.report.check(
                name,
                False,
                f"status={seen.status}, expected {HELD} in queue {sample.queue}",
            )
        item = self.review_item("admin", key)["item"]
        routed = item.get("routedBy") or {}
        history = self.history(seen.execution)
        ok = (
            item.get("queue") == sample.queue
            and routed.get("ruleId") == sample.rule
            and history.has(ROUTING_STATE)
        )
        return self.report.check(
            name,
            ok,
            f"queue={item.get('queue')} (expected {sample.queue}) "
            f"rule={routed.get('ruleId')} (expected {sample.rule}) "
            f"fallback={routed.get('fallbackReason') or 'none'}; "
            f"{ROUTING_STATE} {'entered' if history.has(ROUTING_STATE) else 'not entered'}",
        )

    def routed_by(self, key: str) -> dict:
        """The routed-by record of the document's review item, as the API returns it."""
        return _mapping(self.review_item("admin", key)["item"].get("routedBy"))

    def check_reason(self, name: str, key: str, expected: Round) -> bool:
        """The reason the reviewer reads is the routing rule's own text with the
        document's values in it: it names the problem and no placeholder is left.

        Each failed policy rule is printed with the explanation derived for it.
        """
        routed = self.routed_by(key)
        reason = str(routed.get("reason") or "")
        failures = _rows(routed.get("policyFailures"))
        for failure in failures:
            print(
                f"  failed policy rule {failure.get('ruleId')} "
                f"(pages {failure.get('pages')}): {_shown(failure.get('reason'))}"
            )
        named = expected.reason_has in reason
        unfilled = bool(BRACE.search(reason))
        ok = (
            named and not unfilled and routed.get("reasonSource") == REASON_SOURCE_ADMIN
        )
        policy = ""
        if expected.policy_failure:
            first = failures[0].get("name") if failures else None
            ok = ok and first == expected.policy_failure
            policy = (
                f"; first failed policy rule: {first or 'none'} "
                f"(expected {expected.policy_failure})"
            )
        return self.report.check(
            name,
            ok,
            f"source={routed.get('reasonSource')}; names {expected.reason_has!r}: "
            f"{'yes' if named else 'no'}; placeholder left unfilled: "
            f"{'yes' if unfilled else 'no'}{policy}; reason: {_shown(reason) or 'none'}",
        )


# --------------------------------------------------------------- pre-upgrade
def pre_upgrade(run: Run) -> None:
    """Hold three documents on the stack as it is before the update."""
    if not run.require_outputs():
        return
    if not run.state["profile"]:
        run.state["profile"] = f"{KEY_PREFIX}-{run.tag}"
        run.save()
        run.deps.upload_profile(
            run.ctx["stack"],
            run.ctx["region"],
            run.state["profile"],
            {"hitl": {"enabled": True, "confidence_threshold": 0.99}},
        )
    run.list_annotator(DEFAULT_QUEUE)
    run.wait_out_queue_cache()
    keys = [
        run.upload(
            run.key_for(f"held-{n}", queues_check.SAMPLE),
            queues_check.SAMPLE,
            run.state["profile"],
        )
        for n in (1, 2, 3)
    ]
    seen = run.wait_documents(
        {key: (frozenset({HELD}), ROUTE_TIMEOUT_SECONDS) for key in keys}
    )
    held = []
    for n, key in enumerate(keys, start=1):
        status = (
            run.wait_status_object(key, "in_review") if seen[key].status == HELD else {}
        )
        ok = seen[key].status == HELD and status.get("status") == "in_review"
        run.report.check(
            f"pre-upgrade-held-{n}",
            ok,
            f"tracking={seen[key].status} review status={status.get('status')} "
            f"reasons={status.get('reason_count')}",
        )
        if ok:
            held.append(
                {
                    "key": key,
                    "cycle_id": status.get("cycle_id") or "",
                    "execution": seen[key].execution,
                }
            )
    run.state["held"] = held
    run.save()


def start_in_flight(run: Run) -> None:
    """Start three documents and return, so they are processed during the update.

    It can be run more than once: before the update is started, and again while the
    stack is updating, when a document is most likely to be mid-run as the state
    machine changes. Every batch is kept, and the time of the first one is what the
    update is looked for after.
    """
    if not run.require_outputs():
        return
    if not run.state["profile"]:
        raise CheckAborted("the state file names no profile; run pre-upgrade first")
    if not run.state["in_flight_started_at"]:
        run.state["in_flight_started_at"] = run.deps.now().isoformat()
        run.save()
    batch = len(run.state["in_flight"]) // 3 + 1
    keys = [
        run.upload(
            run.key_for(f"in-flight-{batch}-{n}", queues_check.SAMPLE),
            queues_check.SAMPLE,
            run.state["profile"],
        )
        for n in (1, 2, 3)
    ]
    run.state["in_flight"] = [*run.state["in_flight"], *keys]
    run.save()
    run.report.check(
        "in-flight-started",
        len(keys) == 3,
        f"{len(keys)} documents uploaded, not waited for",
    )


# -------------------------------------------------------------- post-upgrade
def post_upgrade(run: Run) -> None:
    """What the update did to documents held, and documents started, before it."""
    if not run.require_outputs():
        return
    held = run.state["held"]
    if len(held) < 3:
        raise CheckAborted(
            f"the state file names {len(held)} held document(s); pre-upgrade must hold three"
        )

    def review_ended(doc: Mapping[str, str]) -> bool:
        """True when a run follows the one that held the document: this subcommand
        was run before and ended the review. The run that followed is read again."""
        seen = run.tracking(doc["key"])
        ended = bool(seen.execution) and seen.execution != doc["execution"]
        if ended:
            print(f"  {doc['key']}: its review was ended before; reading that run")
        return ended

    def complete_held() -> None:
        doc = held[0]
        if not review_ended(doc):
            run.complete("annotator", doc["key"])
        after = run.wait_next_run(
            doc["key"],
            doc["execution"],
            frozenset({COMPLETED}),
            FINALIZE_TIMEOUT_SECONDS,
        )
        history = run.history(after.execution)
        early = history.touched(EARLY_STEP_STATES)
        run.report.check(
            "held-document-completes-from-apply-review-changes",
            history.before(RUN_PLAN_STATE, APPLY_REVIEW_STATE) and not early,
            f"run {after.execution}: {history.summary()}; "
            f"earlier steps touched: {early or 'none'}",
        )
        status = run.wait_status_object(doc["key"], "complete")
        run.report.check(
            "held-document-status-object",
            status.get("schema_version") == "1.1"
            and status.get("status") == "complete",
            f"schema_version={status.get('schema_version')} status={status.get('status')}",
        )

    def reject_held() -> None:
        doc = held[1]
        if not review_ended(doc):
            run.reject("annotator", doc["key"])
        after = run.wait_next_run(
            doc["key"],
            doc["execution"],
            frozenset({REJECTED}),
            FINALIZE_TIMEOUT_SECONDS,
        )
        history = run.history(after.execution)
        run.report.check(
            "held-document-reject-schedules-no-task",
            not history.scheduled and history.has(REJECTED_END_STATE),
            f"run {after.execution}: {history.summary()}; scheduled: "
            f"{list(history.scheduled) or 'none'}",
        )

    def remove_held() -> None:
        doc = held[2]
        if not review_ended(doc):
            cycle = run.status_object(doc["key"]).get("cycle_id") or doc["cycle_id"]
            run.must(
                "admin",
                "removeReviewItem",
                {
                    "objectKey": doc["key"],
                    "cycleId": cycle,
                    "note": "review routing live check",
                },
            )
        after = run.wait_next_run(
            doc["key"],
            doc["execution"],
            frozenset({COMPLETED}),
            FINALIZE_TIMEOUT_SECONDS,
        )
        history = run.history(after.execution)
        run.report.check(
            "held-document-removed-starts-at-policy-rule-validation",
            history.has(START_AT_POLICY_STATE),
            f"run {after.execution}: {history.summary()}",
        )

    def in_flight() -> None:
        keys = run.state["in_flight"]
        if not keys:
            run.report.add(
                "in-flight-documents",
                SKIP,
                "start-in-flight was not run before the update",
            )
            return
        seen = run.wait_documents(
            {key: (frozenset({HELD, COMPLETED}), ROUTE_TIMEOUT_SECONDS) for key in keys}
        )
        updated = run.assembly_functions_updated(
            parse_time(run.state["in_flight_started_at"])
        )
        outcomes = []
        for n, key in enumerate(keys, start=1):
            outcome, detail = in_flight_verdict(
                run.history(seen[key].execution),
                seen[key].status,
                seen[key].review_reasons,
                updated,
            )
            outcomes.append(outcome)
            run.report.add(f"in-flight-document-{n}", outcome, detail)
        if PASS not in outcomes and FAIL not in outcomes:
            print("  no in-flight run that needed review called an updated function")

    def new_upload() -> None:
        # A new document each time this subcommand is run.
        earlier = sum("/after-upgrade-" in key for key in run.state["documents"])
        key = run.upload(
            run.key_for(f"after-upgrade-{earlier + 1}", queues_check.SAMPLE),
            queues_check.SAMPLE,
            run.state["profile"],
        )
        seen = run.wait_documents({key: (frozenset({HELD}), ROUTE_TIMEOUT_SECONDS)})[
            key
        ]
        run.check_lands(
            "unchanged-profile-held-by-built-in-rule",
            key,
            Sample(
                "w2", queues_check.SAMPLE, DEFAULT_QUEUE, BUILTIN_CONFIDENCE_RULE, 0
            ),
            seen,
        )

    run.step("held-document-completes-from-apply-review-changes", complete_held)
    run.step("held-document-reject-schedules-no-task", reject_held)
    run.step("held-document-removed-starts-at-policy-rule-validation", remove_held)
    run.step("in-flight-documents", in_flight)
    run.step("unchanged-profile-held-by-built-in-rule", new_upload)


# ---------------------------------------------------- the Review Routing switch
def _switch_text(enabled: Any) -> str:
    return {True: "on", False: "off"}.get(enabled, "not set")


def capture_switch(run: Run) -> None:
    """Record Review Routing and the revision bodies of the two profiles, before
    the stack is updated."""
    if not run.require_outputs():
        return
    profiles = {profile: run.switch(profile) for profile in SWITCH_PROFILES}
    run.state["switch"] = {
        "captured_at": run.deps.now().isoformat(),
        "profiles": profiles,
    }
    run.save()
    run.report.check(
        "switch-captured",
        profiles[DEFAULT_PROFILE]["exists"],
        "; ".join(
            f"{profile}: Review Routing {_switch_text(found['enabled'])}, "
            f"{len(found['revisions'])} revision bodies"
            if found["exists"]
            else f"{profile}: no such profile"
            for profile, found in profiles.items()
        ),
    )


def check_switch(run: Run) -> None:
    """A stack update changes neither the switch nor a revision that was saved
    before it."""
    if not run.require_outputs():
        return
    captured = _mapping(_mapping(run.state.get("switch")).get("profiles"))
    if not captured:
        raise CheckAborted(
            "the state file holds no captured switch; run capture-switch before "
            "the stack is updated"
        )
    # A capture taken after the update would compare the stack with itself.
    print(
        f"  comparing with what was captured {run.state['switch'].get('captured_at')}"
    )
    for profile in SWITCH_PROFILES:
        before = _mapping(captured.get(profile))
        names = (
            f"switch-unchanged-in-{profile}",
            f"earlier-revisions-unchanged-in-{profile}",
        )
        if not before.get("exists"):
            for name in names:
                run.report.add(
                    name,
                    SKIP,
                    f"profile {profile} did not exist when the switch was captured",
                )
            continue

        def unchanged(
            profile: str = profile, before: dict = before, names: tuple = names
        ) -> None:
            now = run.switch(profile)
            run.report.check(
                names[0],
                now["exists"] and now["enabled"] == before.get("enabled"),
                f"Review Routing was {_switch_text(before.get('enabled'))} before "
                f"the update and is {_switch_text(now['enabled'])} after it"
                + ("" if now["exists"] else "; the profile no longer exists"),
            )
            was, is_now = _mapping(before.get("revisions")), now["revisions"]
            kept = sorted(set(was) & set(is_now))
            changed = [name for name in kept if was[name] != is_now[name]]
            counts = (
                f"{len(was)} captured, {len(set(was) - set(is_now))} no longer "
                f"retained, {len(set(is_now) - set(was))} new"
            )
            if not kept:
                # Nothing was compared, which is not the same as nothing changed.
                run.report.add(
                    names[1],
                    SKIP,
                    f"no revision body captured is there to compare ({counts})",
                )
                return
            run.report.check(
                names[1],
                not changed,
                f"{len(kept)} revision bodies compared, {len(changed)} written "
                f"again{': ' + ', '.join(changed) if changed else ''} ({counts})",
            )

        run.step(names[0], unchanged)


# ----------------------------------------------------------------- scenarios
def scenarios(run: Run) -> None:
    """The routing and run plan scenarios, on the showcase profile."""
    if not run.require_outputs():
        return
    run.state["scenarios_started_at"] = run.deps.now().isoformat()
    run.save()
    for role in SCENARIO_ROLES:
        run.ensure_user(role)
    run.ensure_showcase()

    keys = {
        s.name: run.upload(run.key_for(s.name, s.path), s.path, SHOWCASE_PROFILE)
        for s in SHOWCASE_SAMPLES
    }
    studio = run.upload(
        run.key_for("test-studio", queues_check.SAMPLE),
        queues_check.SAMPLE,
        SHOWCASE_PROFILE,
        extra=",submission-source=test-studio",
    )
    seen = run.wait_documents(
        {
            **{keys[s.name]: (frozenset({HELD}), s.timeout) for s in SHOWCASE_SAMPLES},
            studio: (frozenset({COMPLETED, HELD}), ROUTE_TIMEOUT_SECONDS),
        }
    )
    landed: dict[str, bool] = {}

    def need(name: str) -> str:
        """The key of a sample a later check works on; it must have landed."""
        if not landed.get(name):
            raise CheckAborted(
                f"the {name} sample is not waiting where the check needs it"
            )
        return keys[name]

    for s in SHOWCASE_SAMPLES:

        def lands(s: Sample = s) -> None:
            landed[s.name] = run.check_lands(
                f"{s.name}-lands-in-{s.queue}", keys[s.name], s, seen[keys[s.name]]
            )

        run.step(f"{s.name}-lands-in-{s.queue}", lands)

    def invoice_reasons() -> None:
        reasons = run.review_item("admin", need("invoice"))["item"].get("reasons") or []
        kinds = sorted({r.get("kind") for r in reasons})
        run.report.check(
            "invoice-has-no-confidence-reason",
            "low_confidence" not in kinds,
            f"reason kinds: {kinds or 'none'}",
        )

    def test_studio() -> None:
        history = run.history(seen[studio].execution)
        run.report.check(
            "test-studio-document-is-not-routed",
            seen[studio].status == COMPLETED and history.has(ROUTING_STATE),
            f"status={seen[studio].status}; {ROUTING_STATE} "
            f"{'entered' if history.has(ROUTING_STATE) else 'not entered'}",
        )

    def routing_test() -> None:
        key = need("invoice")
        before = (run.review_item("admin", key)["item"], run.status_object(key))
        answer = run.must("admin", "testRoutingTable", {"objectKey": key})
        after = (run.review_item("admin", key)["item"], run.status_object(key))
        unchanged = (
            before[0].get("version") == after[0].get("version")
            and before[1].get("sequence") == after[1].get("sequence")
            and run.tracking(key).status == HELD
        )
        run.report.check(
            "routing-test-names-the-rule-and-changes-nothing",
            answer.get("matchedRuleId") == SAMPLES_BY_NAME["invoice"].rule
            and answer.get("decision") == "hold"
            and unchanged,
            f"matched={answer.get('matchedRuleId')} decision={answer.get('decision')} "
            f"queue={answer.get('outcomeQueue')}; item version "
            f"{before[0].get('version')} -> {after[0].get('version')}, status sequence "
            f"{before[1].get('sequence')} -> {after[1].get('sequence')}",
        )

    def complete_invoice() -> None:
        key = need("invoice")
        run.complete("annotator", key)
        after = run.wait_next_run(
            key,
            seen[key].execution,
            frozenset({COMPLETED, HELD}),
            FINALIZE_TIMEOUT_SECONDS,
        )
        history = run.history(after.execution)
        early = history.touched(EARLY_STEP_STATES)
        run.report.check(
            "completed-review-starts-at-apply-review-changes",
            history.before(RUN_PLAN_STATE, APPLY_REVIEW_STATE)
            and history.before(APPLY_REVIEW_STATE, POLICY_FIRST_STATE)
            and history.has(ROUTING_STATE)
            and not early,
            f"run {after.execution}: {history.summary()}; "
            f"earlier steps touched: {early or 'none'}",
        )
        run.report.check(
            "completed-review-is-not-held-again",
            after.status == COMPLETED,
            f"status={after.status}",
        )
        landed["invoice-completed"] = after.status == COMPLETED

    def reject_statement() -> None:
        key = need("statement")
        run.reject("reviewer", key)
        after = run.wait_next_run(
            key, seen[key].execution, frozenset({REJECTED}), FINALIZE_TIMEOUT_SECONDS
        )
        history = run.history(after.execution)
        run.report.check(
            "rejected-review-schedules-no-task",
            not history.scheduled and history.has(REJECTED_END_STATE),
            f"run {after.execution}: {history.summary()}; scheduled: "
            f"{list(history.scheduled) or 'none'}",
        )

    def send_back_w2() -> None:
        key = need("w2")
        item_args = run.claim("annotator", key)
        run.check_send_back(
            run.send_back("annotator", key, item_args, seen[key].execution)
        )

    def reprocess_invoice() -> None:
        key = need("invoice")
        if not landed.get("invoice-completed"):
            raise CheckAborted(
                "the invoice's review was not completed, so it cannot be reprocessed"
            )
        previous = run.tracking(key).execution
        answer = run.must(
            "admin",
            "reprocessDocument",
            {"objectKeys": [key], "startStep": "summarization"},
        )
        results = answer.get("results") or [{}]
        if results[0].get("status") != "QUEUED":
            raise CheckAborted(
                f"reprocessDocument answered {results[0].get('status')} "
                f"{results[0].get('reason')}: {results[0].get('message')}"
            )
        after = run.wait_next_run(
            key, previous, frozenset({COMPLETED, HELD}), FINALIZE_TIMEOUT_SECONDS
        )
        history = run.history(after.execution)
        extraction = history.touched(EXTRACTION_STATES)
        run.report.check(
            "reprocess-from-summarization-skips-to-routing",
            history.has(SKIP_POLICY_STATE)
            and history.has(ROUTING_STATE)
            and not extraction,
            f"run {after.execution} ended {after.status}: {history.summary()}; "
            f"extraction touched: {extraction or 'none'}",
        )

    def author_saves() -> None:
        code, body = run.api(
            "author",
            "updateConfiguration",
            {
                "versionName": SHOWCASE_PROFILE,
                "customConfig": json.dumps(
                    {"routing": {"max_consecutive_placements": 2}}
                ),
            },
        )
        run.report.check(
            "author-cannot-change-routing-rules",
            code == 403,
            f"status={code} {json.dumps(body)[:160]}",
        )
        before = run.must(
            "admin", "getConfigVersion", {"versionName": SHOWCASE_PROFILE}
        )
        notes = (_as_json(before.get("Custom")) or {}).get("notes") or ""
        code, body = run.api(
            "author",
            "updateConfiguration",
            {
                "versionName": SHOWCASE_PROFILE,
                "customConfig": json.dumps(
                    {"notes": f"Review routing showcase (live check {run.tag})"}
                ),
            },
        )
        saved = code == 200 and bool((body or {}).get("success"))
        run.report.check(
            "author-can-save-another-section",
            saved,
            f"status={code} success={(body or {}).get('success')}",
        )
        if saved:
            # The profile is left as it was found.
            run.must(
                "author",
                "updateConfiguration",
                {
                    "versionName": SHOWCASE_PROFILE,
                    "customConfig": json.dumps({"notes": notes}),
                },
            )

    def missing_queue() -> None:
        sample = SAMPLES_BY_NAME["lending"]
        if sample.queue not in run.state["queues_created"]:
            # Deleting a queue moves its documents to Default and loses its settings.
            run.report.add(
                "missing-queue-falls-back-to-default",
                SKIP,
                f"queue {sample.queue} existed before this check, so it is not deleted",
            )
            return
        run.must("admin", "deleteReviewQueue", {"id": sample.queue})
        try:
            run.wait_out_queue_cache()
            key = run.upload(
                run.key_for("lending-fallback", sample.path),
                sample.path,
                SHOWCASE_PROFILE,
            )
            after = run.wait_documents({key: (frozenset({HELD}), sample.timeout)})[key]
            if after.status != HELD:
                run.report.check(
                    "missing-queue-falls-back-to-default",
                    False,
                    f"status={after.status}",
                )
                return
            item = run.review_item("admin", key)["item"]
            routed = item.get("routedBy") or {}
            run.report.check(
                "missing-queue-falls-back-to-default",
                item.get("queue") == DEFAULT_QUEUE
                and routed.get("ruleId") == sample.rule
                and routed.get("fallbackReason") == "queue_missing",
                f"queue={item.get('queue')} rule={routed.get('ruleId')} "
                f"fallback={routed.get('fallbackReason')}",
            )
        finally:
            # The queue is the showcase's: put it back whatever the check found.
            run.must(
                "admin",
                "createReviewQueue",
                {
                    "input": {
                        "id": sample.queue,
                        "name": SHOWCASE_QUEUES[sample.queue],
                        "eligibleAnnotators": [run.ensure_user("annotator")],
                    }
                },
            )

    def dmn_round_trip() -> None:
        from idp_common.config.routing_models import RoutingConfig

        answer = run.must(
            "admin", "getConfigVersion", {"versionName": SHOWCASE_PROFILE}
        )
        saved = (_as_json(answer.get("Custom")) or {}).get("routing")
        if not isinstance(saved, dict):
            raise CheckAborted("the showcase profile has no routing section")
        tables = RoutingConfig.model_validate(saved).model_dump()["tables"]
        for version in ("1.5", "1.3"):
            exported = run.must(
                "admin",
                "convertRoutingDmn",
                {
                    "direction": "to_dmn",
                    "content": json.dumps(saved),
                    "dmnVersion": version,
                },
            )
            imported = run.must(
                "admin",
                "convertRoutingDmn",
                {"direction": "from_dmn", "content": exported.get("xml") or ""},
            )
            back = RoutingConfig.model_validate(
                _as_json(imported.get("routing")) or {}
            ).model_dump()["tables"]
            run.report.check(
                f"dmn-{version}-round-trip-keeps-the-table",
                back == tables,
                f"{len((tables or [{}])[0].get('rules') or [])} rule(s) exported as DMN "
                f"{version} ({len(exported.get('xml') or '')} characters) and imported",
            )

    def latency() -> None:
        began = parse_time(run.state["scenarios_started_at"]) or run.deps.now()
        figures = []
        for function in LATENCY_FUNCTIONS:
            group = run.pattern_log_group(function)
            warm = run_latency_query(run, function, group, began, WARM_FILTER)
            every = warm or run_latency_query(run, function, group, began, ALL_FILTER)
            if every is None:
                raise CheckAborted(f"{function} logged no invocation in the period")
            n, p50, p95 = every
            figures.append(
                f"{function} ({'warm' if warm else 'none warm; all'} invocations): "
                f"n={n} p50={p50:.0f} ms p95={p95:.0f} ms"
            )
            if function == "RoutingFunction" and warm and p95 > ROUTING_P95_TARGET_MS:
                print(
                    f"  finding: {function} warm p95 {p95:.0f} ms is over the "
                    f"{ROUTING_P95_TARGET_MS:.0f} ms target"
                )
        run.report.check("latency-recorded", True, "; ".join(figures))

    run.step("invoice-has-no-confidence-reason", invoice_reasons)
    run.step("test-studio-document-is-not-routed", test_studio)
    run.step("routing-test-names-the-rule-and-changes-nothing", routing_test)
    run.step("completed-review-starts-at-apply-review-changes", complete_invoice)
    run.step("rejected-review-schedules-no-task", reject_statement)
    run.step("send-back-starts-at-classification", send_back_w2)
    run.step("reprocess-from-summarization-skips-to-routing", reprocess_invoice)
    run.step("author-cannot-change-routing-rules", author_saves)
    run.step("missing-queue-falls-back-to-default", missing_queue)
    run.step("dmn-round-trip-keeps-the-table", dmn_round_trip)
    run.step("latency-recorded", latency)


def run_latency_query(
    run: Run,
    function: str,
    group: str,
    began: datetime,
    invocations: str,
    ended: datetime | None = None,
) -> tuple[int, float, float] | None:
    """Count, p50 and p95 of a function's invocations since ``began`` (Logs Insights).

    ``group`` is the function's log group and ``invocations`` the filter that
    selects them. The period ends at ``ended``, or now. ``None`` when none matched.
    """
    d, region = run.deps, run.ctx["region"]
    end = int(d.now().timestamp()) + 60 if ended is None else int(ended.timestamp())
    started = d.aws(
        "logs",
        "start-query",
        "--log-group-name",
        group,
        "--start-time",
        str(int(began.timestamp())),
        "--end-time",
        str(end),
        "--query-string",
        invocations + LATENCY_STATS,
        region=region,
    )
    deadline = d.monotonic() + QUERY_TIMEOUT_SECONDS
    while True:
        answer = d.aws(
            "logs",
            "get-query-results",
            "--query-id",
            (started or {}).get("queryId", ""),
            region=region,
        )
        status = (answer or {}).get("status")
        if status == "Complete":
            break
        if status in {"Failed", "Cancelled", "Timeout"} or d.monotonic() >= deadline:
            raise CheckAborted(f"the latency query for {function} ended {status}")
        d.sleep(2)
    rows = (answer or {}).get("results") or []
    if not rows:
        return None
    row = {cell.get("field"): cell.get("value") for cell in rows[0]}
    count = int(float(row.get("n") or 0))
    if count == 0:
        return None
    return count, float(row.get("p50") or 0), float(row.get("p95") or 0)


def completion_plan_latency(run: Run, item_args: dict) -> str:
    """How long ``getReviewCompletionPlan`` takes, from the log of the function
    that serves it.

    The function serves every review queue operation and its log does not say
    which one an invocation was, so the operation is called a number of times
    with nothing else of this check before or after it, and the invocations of
    that period are read. Returns the figures as one line.
    """
    d = run.deps
    operation = "getReviewCompletionPlan"
    group = run.plan_resolver_log_group()
    d.sleep(PLAN_QUIET_SECONDS)
    began = d.now()
    for _ in range(PLAN_LATENCY_CALLS):
        run.must("annotator", operation, item_args)
    ended = d.now()
    d.sleep(PLAN_QUIET_SECONDS)
    margin = timedelta(seconds=PLAN_CLOCK_MARGIN_SECONDS)
    # Logs Insights takes whole seconds: the start rounds down, so the end is
    # rounded up.
    period = (began - margin, ended + margin + timedelta(seconds=1))
    deadline = d.monotonic() + PLAN_LOG_WAIT_SECONDS
    while True:
        warm = run_latency_query(
            run, operation, group, period[0], WARM_FILTER, period[1]
        )
        # The log events of an invocation are readable some time after it ended.
        if (warm and warm[0] >= PLAN_LATENCY_CALLS) or d.monotonic() >= deadline:
            break
        d.sleep(PLAN_LOG_POLL_SECONDS)
    every = warm or run_latency_query(
        run, operation, group, period[0], ALL_FILTER, period[1]
    )
    if every is None:
        raise CheckAborted(
            f"the review queues function logged no invocation for the "
            f"{PLAN_LATENCY_CALLS} calls of {operation}"
        )
    n, p50, p95 = every
    if warm and p95 > PLAN_P95_TARGET_MS:
        print(
            f"  finding: {operation} warm p95 {p95:.0f} ms is over the "
            f"{PLAN_P95_TARGET_MS:.0f} ms target"
        )
    others = (
        f"; {n - PLAN_LATENCY_CALLS} more than this check made, so another caller "
        "used the stack in the period"
        if n > PLAN_LATENCY_CALLS
        else ""
    )
    return (
        f"{operation} ({'warm' if warm else 'none warm; all'} invocations): n={n} of "
        f"{PLAN_LATENCY_CALLS} calls p50={p50:.0f} ms p95={p95:.0f} ms "
        f"(target p95 {PLAN_P95_TARGET_MS:.0f} ms){others}"
    )


# ------------------------------------------------------- reasons-and-restart
def _section_to_regroup(
    run: Run, detail: Mapping[str, Any], layout: Mapping[str, list[str]]
) -> tuple[dict, dict, str]:
    """A section in review with a top-level text field to correct: the section,
    its result and the field's name. One of two or more pages is preferred,
    because it can give a page to a new section."""
    found: list[tuple[dict, dict, str]] = []
    for section in _rows(detail.get("sections")):
        pages = layout.get(str(section.get("sectionId"))) or []
        if not pages or not section.get("resultUri"):
            continue
        result = run.section_result(section)
        name = string_field(result)
        if not name:
            continue
        if len(pages) >= 2:
            return section, result, name
        found.append((section, result, name))
    if found:
        return found[0]
    raise CheckAborted(
        "the document has no section in review with a top-level text field to correct"
    )


def regroup_grouping(
    layout: Mapping[str, list[str]], classes: Mapping[str, str], section_id: str
) -> tuple[list[dict], str, str]:
    """The page grouping a reviewer saves so that ``section_id`` has other pages
    and one section is new: the grouping, the new section's id, and what was done.

    A section of two or more pages gives its last page to a new section of its
    class. A section of one page takes the pages of the section after it, and the
    section after that one is put under a new id: on a document whose sections
    have one page each, that is the regroup that changes the pages of a section
    and adds a section.
    """
    added = next_section_id(layout)
    pages = sorted(layout[section_id], key=int)
    if len(pages) >= 2:
        grouping = [
            {
                "sectionId": other,
                "classification": classes[other],
                "pageIds": pages[:-1] if other == section_id else page_ids,
            }
            for other, page_ids in layout.items()
        ]
        grouping.append(
            {
                "sectionId": added,
                "classification": classes[section_id],
                "pageIds": pages[-1:],
            }
        )
        return (
            grouping,
            added,
            f"section {section_id} gave its last page to the new section {added}",
        )
    # In page order, starting after the section: the one whose pages it takes,
    # then the one that is put under a new id.
    ordered = sorted(
        (other for other in layout if layout[other]),
        key=lambda other: min(int(page) for page in layout[other]),
    )
    at = ordered.index(section_id)
    others = ordered[at + 1 :] + ordered[:at]
    if len(others) < 2:
        raise CheckAborted(
            "the document has too few sections to regroup: a section of one page "
            "needs two other sections"
        )
    taken, renewed = others[0], others[1]
    grouping = [
        {
            "sectionId": other,
            "classification": classes[other],
            "pageIds": sorted(pages + list(layout[taken]), key=int)
            if other == section_id
            else page_ids,
        }
        for other, page_ids in layout.items()
        if other not in (taken, renewed)
    ]
    grouping.append(
        {
            "sectionId": added,
            "classification": classes[renewed],
            "pageIds": list(layout[renewed]),
        }
    )
    return (
        grouping,
        added,
        f"section {section_id} took the pages of section {taken}, and section "
        f"{renewed} was put under the new id {added}",
    )


def reasons_and_restart(run: Run) -> None:
    """The reason a reviewer reads, what finishing a review will run, and where
    the run after a send-back and after a regrouped review starts."""
    if not run.require_outputs():
        return
    for role in ("annotator", "bystander", "admin"):
        run.ensure_user(role)
    one, two = SCENARIOS_BY_NAME["one"], SCENARIOS_BY_NAME["two"]
    # The second Annotator is on the queue of the document whose plan they ask for.
    run.ensure_showcase(bystander_queue=one.first.queue)
    samples = {
        "one": one.sample(one.first),
        "two": two.sample(two.first),
        "regroup": REGROUP_SAMPLE,
    }
    keys = {
        "one": run.fresh_key("reasons-one", one.path),
        "two": run.fresh_key("reasons-two", two.path),
        "regroup": run.fresh_key("regroup", REGROUP_SAMPLE.path),
    }
    for name, key in keys.items():
        run.upload(key, samples[name].path, SHOWCASE_PROFILE)
    seen = run.wait_documents(
        {
            keys[name]: (frozenset({HELD, COMPLETED}), sample.timeout)
            for name, sample in samples.items()
        }
    )
    landed: dict[str, bool] = {}
    # The ``{objectKey, cycleId}`` of each review the Annotator claimed.
    held: dict[str, dict] = {}
    # What the regroup did, for the check that completes the review after it.
    regroup: dict = {}

    def need(name: str) -> str:
        """The key of a document a later check works on; it must have landed."""
        if not landed.get(name):
            raise CheckAborted(
                f"the {name} document is not waiting where the check needs it"
            )
        return keys[name]

    for name, sample in samples.items():
        label = f"{sample.name}-lands-in-{sample.queue}"

        def lands(name: str = name, sample: Sample = sample, label: str = label):
            landed[name] = run.check_lands(label, keys[name], sample, seen[keys[name]])

        run.step(label, lands)

    # (a) The reason and the failed policy rules, on every surface that shows them.
    def two_reason() -> None:
        run.check_reason(
            "scenario-two-reason-is-rendered",
            need("two"),
            replace(two.first, policy_failure=""),
        )

    def two_policy_failure() -> None:
        routed = run.routed_by(need("two"))
        failures = _rows(routed.get("policyFailures"))
        wanted = f"{SCENARIO_POLICY}.{NET_AMOUNT_POLICY_RULE}"
        listed = [
            failure
            for failure in failures
            if failure.get("ruleId") == wanted
            and failure.get("policy") == SCENARIO_POLICY
            and failure.get("name") == NET_AMOUNT_POLICY_RULE
        ]
        count = routed.get("policyFailureCount") or 0
        run.report.check(
            "scenario-two-lists-the-failed-policy-rule",
            bool(listed) and count >= len(failures),
            f"{count} failed policy rule(s), listed: "
            f"{[failure.get('ruleId') for failure in failures] or 'none'}; {wanted}: "
            f"{_shown(listed[0].get('reason')) or 'no explanation' if listed else 'not listed'}",
        )

    def surface(name: str, read: Callable[[str], Shown]) -> Callable[[], None]:
        """A check that one surface shows what the review item does."""

        def work() -> None:
            key = need("two")
            item = Shown.read(run.routed_by(key))
            found = read(key)
            run.report.check(
                name,
                bool(item.reason) and found == item,
                found.text()
                if found == item
                else f"{found.text()}; the review item has {item.text()}",
            )

        return work

    def in_status_object(key: str) -> Shown:
        status = run.wait_status_object(key, "in_review")
        return Shown.read(status.get("routed_by"), snake=True)

    def in_cli_status(key: str) -> Shown:
        printed = run.deps.document_status(run.ctx["stack"], run.ctx["region"], key)
        return Shown.read(
            {
                "reason": printed.get("routing_reason"),
                "policy_failures": printed.get("policy_failures"),
                "policy_failure_count": printed.get("policy_failure_count"),
            },
            snake=True,
        )

    def two_history() -> None:
        key = need("two")
        item = run.review_item("admin", key)["item"]
        reason = _mapping(item.get("routedBy")).get("reason")
        routed = [
            details
            for event, details in run.history_entries(key, item.get("cycleId") or "")
            if event == "routed"
        ]
        recorded = routed[-1] if routed else {}
        run.report.check(
            "scenario-two-history-has-the-reason",
            bool(reason) and recorded.get("routing_reason") == reason,
            f"routed entries: {len(routed)}; routing_reason: "
            f"{_shown(recorded.get('routing_reason')) or 'none'}",
        )

    # (d) What finishing the review will run: for the holder, and nobody else.
    def plan_for_the_holder() -> None:
        key = need("one")
        held["one"] = run.claim("annotator", key)
        plan = run.must("annotator", "getReviewCompletionPlan", held["one"])
        complete, back = _mapping(plan.get("complete")), _mapping(plan.get("sendBack"))
        problems = [
            *(f"complete: {problem}" for problem in plan_problems(complete)),
            *(f"send back: {problem}" for problem in plan_problems(back)),
        ]
        run.report.check(
            "completion-plan-gives-the-holder-both-plans",
            not problems
            and complete.get("startStep") == STEP_APPLY_REVIEW
            and back.get("startStep") == STEP_CLASSIFICATION,
            f"complete: {plan_text(complete)}; send back: {plan_text(back)}"
            + (f"; problems: {'; '.join(problems)}" if problems else ""),
        )

    def claimed(name: str) -> dict:
        if name not in held:
            raise CheckAborted(f"the review of the {name} document was not claimed")
        return held[name]

    def plan_refused() -> None:
        key, item_args = need("one"), claimed("one")
        code, _ = run.api("bystander", "getReviewItem", {"objectKey": key})
        if code != 200:
            raise CheckAborted(
                f"the second Annotator cannot read the item ({code}), so a refusal "
                "of its plan would not show that only the holder may read it"
            )
        code, body = run.api("bystander", "getReviewCompletionPlan", item_args)
        run.report.check(
            "completion-plan-is-refused-to-an-annotator-who-does-not-hold-the-item",
            code == 403,
            f"status={code} {json.dumps(body)[:160]}; the same Annotator reads "
            "the item (200)",
        )

    # (f) How long the plan takes.
    def plan_latency() -> None:
        need("one")
        run.report.check(
            "completion-plan-latency-recorded",
            True,
            completion_plan_latency(run, claimed("one")),
        )

    # (c) A send-back names no step and starts at classification.
    def send_back_one() -> None:
        key = need("one")
        item_args = held.get("one") or run.claim("annotator", key)
        run.check_send_back(
            run.send_back("annotator", key, item_args, seen[key].execution)
        )

    # (e) Where Process Changes would start for a completed document.
    def process_changes_plan() -> None:
        key = need("two")
        cycle = run.review_item("admin", key)["item"].get("cycleId")
        # Removed from review, the document completes with what the run produced.
        run.must(
            "admin",
            "removeReviewItem",
            {"objectKey": key, "cycleId": cycle, "note": NOTE},
        )
        run.wait_next_run(
            key, seen[key].execution, frozenset({COMPLETED}), FINALIZE_TIMEOUT_SECONDS
        )
        plan = run.must("admin", "getProcessChangesPlan", {"objectKey": key})
        problems = plan_problems(plan)
        run.report.check(
            "process-changes-plan-starts-at-policy-rule-validation",
            not problems and plan.get("startStep") == STEP_POLICY_RULE_VALIDATION,
            plan_text(plan)
            + (f"; problems: {'; '.join(problems)}" if problems else ""),
        )

    # (b) A review completed after a regroup extracts the regrouped sections again.
    def regroup_and_plan() -> None:
        key = need("regroup")
        item_args = run.claim("annotator", key)
        detail = run.review_item("annotator", key)
        stored = run.tracking(key).sections
        layout = {
            str(s.get("Id")): [str(page) for page in s.get("PageIds") or []]
            for s in stored
        }
        classes = {str(s.get("Id")): str(s.get("Class") or "") for s in stored}
        section, result, name = _section_to_regroup(run, detail, layout)
        section_id = str(section["sectionId"])
        corrected = f"{result['inference_result'][name]}{CORRECTED_SUFFIX}"
        run.save_draft(
            "annotator", item_args, section, with_fields(result, {name: corrected})
        )
        # The section's pages change and one section is new, in one regroup.
        grouping, added, how = regroup_grouping(layout, classes, section_id)
        answer = run.must(
            "annotator",
            "updateDocumentSections",
            {"objectKey": key, "sections": grouping},
        )
        if not answer.get("success"):
            raise CheckAborted(
                f"updateDocumentSections refused the regroup: {answer.get('message')}"
            )
        regroup.update(
            item_args=item_args,
            detail=detail,
            section=section_id,
            added=added,
            extracted=[section_id, added],
            field=name,
            value=corrected,
        )
        plan = run.must("annotator", "getReviewCompletionPlan", item_args)
        complete = _mapping(plan.get("complete"))
        causes = {
            str(planned.get("sectionId")): planned.get("cause")
            for planned in _rows(complete.get("sections"))
        }
        regroup["planned"] = sorted(causes)
        problems = plan_problems(complete)
        run.report.check(
            "regrouped-review-plan-extracts-both-sections",
            not problems
            and complete.get("startStep") == STEP_EXTRACTION
            and causes.get(section_id) == CAUSE_PAGES_CHANGED
            and causes.get(added) == CAUSE_ADDED,
            f"{how}; plan: {plan_text(complete)}"
            + (f"; problems: {'; '.join(problems)}" if problems else ""),
        )

    def complete_regrouped() -> None:
        key = need("regroup")
        if "added" not in regroup:
            raise CheckAborted("the review was not regrouped, so it is not completed")
        run.finish("annotator", regroup["item_args"], regroup["detail"])
        after = regroup["after"] = run.wait_next_run(
            key,
            seen[key].execution,
            frozenset({HELD, COMPLETED}),
            REGROUP_SAMPLE.timeout,
        )
        history = run.history(after.execution)
        path = (
            START_AT_EXTRACTION_SECTIONS_STATE,
            CHECK_REVIEW_STATE,
            SELECT_ASSEMBLED_STATE,
            APPLY_REVIEW_STATE,
        )
        hooks = history.entered.count(POST_EXTRACTION_HOOK_STATE)
        # One section at a time passes the hook state: as many as the plan named.
        planned = len(regroup.get("planned") or regroup["extracted"])
        early = history.touched(BEFORE_EXTRACTION_STATES)
        run.report.check(
            "regrouped-review-extracts-then-applies-the-review",
            all(history.before(a, b) for a, b in zip(path, path[1:]))
            and hooks == planned
            and not early,
            f"run {after.execution}: entered {[s for s in path if history.has(s)]} "
            f"of {list(path)}; {POST_EXTRACTION_HOOK_STATE} entered {hooks} time(s) "
            f"for {planned} section(s); earlier steps touched: {early or 'none'}",
        )

    def completed(name: str) -> Any:
        """What the regroup or the run after the completed review left."""
        if "after" not in regroup:
            raise CheckAborted(
                "the regrouped review was not completed, so its run left nothing "
                "to read"
            )
        return regroup[name]

    def extracted_results() -> tuple[dict[str, dict], dict[str, dict]]:
        """The document's sections as the tracking row lists them, by id, and the
        results of the sections that were extracted again."""
        key = need("regroup")
        sections = {str(s.get("Id")): s for s in run.tracking(key).sections}
        return sections, {
            section_id: _mapping(
                run.read_uri(
                    str(sections.get(section_id, {}).get("OutputJSONUri") or "")
                )
            )
            for section_id in completed("extracted")
        }

    def restart_recorded() -> None:
        key, extracted = need("regroup"), completed("extracted")
        completions = [
            details
            for event, details in run.history_entries(
                key, regroup["item_args"]["cycleId"]
            )
            if event == "completed"
        ]
        recorded = completions[-1] if completions else {}
        restarted = sorted(str(s) for s in recorded.get("restart_sections") or [])
        run.report.check(
            "regrouped-review-records-where-the-run-starts",
            recorded.get("restart_step") == STEP_EXTRACTION
            and set(extracted) <= set(restarted),
            f"completed entry: restart_step={recorded.get('restart_step')} "
            f"restart_sections={restarted}; regrouped: {sorted(extracted)}",
        )

    def every_section_has_a_result() -> None:
        extracted = completed("extracted")
        sections, _ = extracted_results()
        # A section the configuration excludes is not processed and has no result.
        missing = sorted(
            section_id
            for section_id, section in sections.items()
            if not section.get("OutputJSONUri") and not section.get("Excluded")
        )
        run.report.check(
            "regrouped-review-every-section-has-a-result",
            set(extracted) <= set(sections) and not missing,
            f"{len(sections)} section(s), without a result: {missing or 'none'}; "
            f"regrouped sections listed: {sorted(set(extracted) & set(sections))}",
        )

    def extracted_sections_not_confirmed() -> None:
        _, results = extracted_results()
        records = {
            section_id: _mapping(result.get(REVIEW_RECORD_KEY))
            for section_id, result in results.items()
        }
        run.report.check(
            "regrouped-review-extracted-sections-are-not-confirmed",
            all(
                record.get("confirmed") is False
                and record.get("extracted_again") is True
                for record in records.values()
            ),
            "; ".join(
                f"section {section_id}: confirmed={record.get('confirmed')} "
                f"extracted_again={record.get('extracted_again')}"
                for section_id, record in records.items()
            ),
        )

    def correction_kept() -> None:
        section_id, name = completed("section"), completed("field")
        _, results = extracted_results()
        record = _mapping(results[section_id].get(REVIEW_RECORD_KEY))
        applied = name in (record.get("changed_paths") or [])
        unapplied = name in (record.get("unapplied_paths") or [])
        inference = _mapping(results[section_id].get("inference_result"))
        holds = inference.get(name) == regroup["value"]
        # Set on the new result, or recorded as a correction that could not be.
        run.report.check(
            "regrouped-review-keeps-the-correction",
            (applied and holds) or (unapplied and not applied),
            f"field {name} of section {section_id}: in changed_paths: {applied}, "
            f"in unapplied_paths: {unapplied}; the new result "
            f"{'holds' if holds else 'does not hold'} the corrected value",
        )

    def held_again_or_completed() -> None:
        key, after = need("regroup"), completed("after")
        placement = (
            run.item_row(key).get("PlacementCount") if after.status == HELD else None
        )
        run.report.check(
            "regrouped-review-is-held-again-or-completes",
            after.status == COMPLETED or (after.status == HELD and placement == 2),
            f"status={after.status}"
            + (f" placement={placement}" if after.status == HELD else ""),
        )

    stem = "scenario-two"
    run.step(f"{stem}-reason-is-rendered", two_reason)
    run.step(f"{stem}-lists-the-failed-policy-rule", two_policy_failure)
    for name, read in (
        (f"{stem}-status-object-has-the-reason", in_status_object),
        (
            f"{stem}-tracking-row-has-the-reason",
            lambda key: Shown.read(run.wait_tracking_routed_by(key)),
        ),
        (f"{stem}-cli-status-has-the-reason", in_cli_status),
    ):
        run.step(name, surface(name, read))
    run.step(f"{stem}-history-has-the-reason", two_history)
    run.step("completion-plan-gives-the-holder-both-plans", plan_for_the_holder)
    run.step(
        "completion-plan-is-refused-to-an-annotator-who-does-not-hold-the-item",
        plan_refused,
    )
    run.step("completion-plan-latency-recorded", plan_latency)
    run.step("send-back-starts-at-classification", send_back_one)
    run.step(
        "process-changes-plan-starts-at-policy-rule-validation", process_changes_plan
    )
    run.step("regrouped-review-plan-extracts-both-sections", regroup_and_plan)
    run.step("regrouped-review-extracts-then-applies-the-review", complete_regrouped)
    run.step("regrouped-review-records-where-the-run-starts", restart_recorded)
    run.step("regrouped-review-every-section-has-a-result", every_section_has_a_result)
    run.step(
        "regrouped-review-extracted-sections-are-not-confirmed",
        extracted_sections_not_confirmed,
    )
    run.step("regrouped-review-keeps-the-correction", correction_kept)
    run.step("regrouped-review-is-held-again-or-completes", held_again_or_completed)


# ----------------------------------------------------------- scenario-rounds
def round_checks(scenario: Scenario, number: int) -> tuple[str, ...]:
    """The names of the checks of one round of a scenario document."""
    expected = scenario.rounds[number - 1]
    stem = f"scenario-{scenario.name}-round-{number}"
    if not expected.rule:
        return (f"{stem}-completes",)
    return (
        f"{stem}-lands-in-{expected.queue}",
        f"{stem}-reason-names-the-problem",
        f"{stem}-review-is-completed",
    )


def play_rounds(run: Run, scenario: Scenario, key: str, first: Tracking) -> None:
    """One scenario document through its rounds.

    In each round but the last, the document is held by the routing rule of one
    problem; the simulated reviewer corrects that problem and completes the
    review, and the run that follows decides the next round. In the last round no
    routing rule matches and the document completes. A round is played only when
    the one before it ended as expected: a correction answers one routing rule.
    """
    seen = first
    placements: Any = None
    lost = ""
    for number, expected in enumerate(scenario.rounds, start=1):
        names = round_checks(scenario, number)
        if lost:
            for name in names[:2]:
                run.report.add(name, ERROR, lost)
            continue
        if not expected.rule:

            def completes(
                name: str = names[0],
                seen: Tracking = seen,
                placements: Any = placements,
                number: int = number,
            ) -> None:
                rule = run.routed_by(key).get("ruleId") if seen.status == HELD else ""
                run.report.check(
                    name,
                    seen.status == COMPLETED and placements == number - 1,
                    f"status={seen.status} after {number - 1} review(s); placements "
                    f"counted on the last review item: {placements}"
                    + (f"; held by routing rule {rule}" if rule else ""),
                )

            run.step(names[0], completes)
            continue
        played: dict = {}

        def lands(
            name: str = names[0], expected: Round = expected, seen: Tracking = seen
        ) -> None:
            played["landed"] = run.check_lands(
                name, key, scenario.sample(expected), seen
            )

        def reason(name: str = names[1], expected: Round = expected) -> None:
            played["placements"] = run.item_row(key).get("PlacementCount")
            run.check_reason(name, key, expected)

        def review(expected: Round = expected, seen: Tracking = seen) -> None:
            run.complete("annotator", key, dict(expected.correction))
            played["seen"] = run.wait_next_run(
                key,
                seen.execution,
                frozenset({HELD, COMPLETED}),
                FINALIZE_TIMEOUT_SECONDS,
            )

        run.step(names[0], lands)
        if played.get("landed"):
            run.step(names[1], reason)
            # Reported only when the review could not be completed or no run followed.
            run.step(names[2], review)
        if "seen" in played:
            seen, placements = played["seen"], played.get("placements")
        else:
            lost = (
                f"round {number} did not end as the showcase says, so round "
                f"{number + 1} and later were not played"
            )


def scenario_rounds(run: Run) -> None:
    """The scenario invoices, round by round, with a simulated reviewer."""
    if not run.require_outputs():
        return
    for role in ("annotator", "admin"):
        run.ensure_user(role)
    run.ensure_showcase()
    keys = {
        scenario.name: run.upload(
            run.fresh_key(f"rounds-{scenario.name}", scenario.path),
            scenario.path,
            SHOWCASE_PROFILE,
        )
        for scenario in SCENARIOS
    }
    seen = run.wait_documents(
        {
            key: (frozenset({HELD, COMPLETED}), ROUTE_TIMEOUT_SECONDS)
            for key in keys.values()
        }
    )
    for scenario in SCENARIOS:
        play_rounds(run, scenario, keys[scenario.name], seen[keys[scenario.name]])


# ----------------------------------------------------------------- seed-demo
def seed_demo(run: Run) -> None:
    """Leave the queues, the showcase profile, the showcase samples, one waiting
    in each queue, and the scenario invoices waiting in their first round."""
    if not run.require_outputs():
        return
    run.ensure_showcase()
    seeds = (*SHOWCASE_SAMPLES, *(s.sample(s.first) for s in SCENARIOS))
    keys = {
        s.name: run.upload(
            f"{DEMO_PREFIX}/{s.path.name}", s.path, SHOWCASE_PROFILE, track=False
        )
        for s in seeds
    }
    # Recorded before the wait: from here on these are the demo's, and cleanup leaves them.
    run.state["seeded"] = {
        "queues": sorted(SHOWCASE_QUEUES),
        "profile": SHOWCASE_PROFILE,
        "documents": sorted(keys.values()),
        "waiting": dict(sorted(Counter(s.queue for s in seeds).items())),
    }
    run.save()
    seen = run.wait_documents(
        {keys[s.name]: (frozenset({HELD}), s.timeout) for s in seeds}
    )
    for s in seeds:

        def seeded(s: Sample = s) -> None:
            run.check_lands(
                f"demo-{s.name}-waits-in-{s.queue}", keys[s.name], s, seen[keys[s.name]]
            )

        run.step(f"demo-{s.name}-waits-in-{s.queue}", seeded)


# ------------------------------------------------------------------- cleanup
def cleanup(run: Run) -> None:
    """Remove what the checks created. Every step runs even when an earlier one fails."""
    state = run.state
    seeded = state.get("seeded") or {}
    kept_queues = set(seeded.get("queues") or [])
    failures: list[str] = []

    def attempt(label: str, work: Callable[[], None]) -> None:
        try:
            work()
        except Exception as e:  # noqa: BLE001 - keep cleaning up
            failures.append(label)
            run.report.add(f"cleanup-{label}", ERROR, f"{type(e).__name__}: {e}")

    def remove_document(key: str) -> None:
        region = run.ctx["region"]
        for row in run.review_rows(key):
            run.deps.aws(
                "dynamodb",
                "delete-item",
                "--table-name",
                run.out["review_table"],
                "--key",
                json.dumps({"PK": row["PK"], "SK": row["SK"]}),
                region=region,
            )
        run.deps.aws(
            "dynamodb",
            "delete-item",
            "--table-name",
            run.out["tracking_table"],
            "--key",
            json.dumps({"PK": {"S": f"doc#{key}"}, "SK": {"S": "none"}}),
            region=region,
        )
        run.deps.aws("s3", "rm", f"s3://{run.out['input_bucket']}/{key}", region=region)
        run.deps.aws(
            "s3",
            "rm",
            f"s3://{run.out['output_bucket']}/{key}/",
            "--recursive",
            region=region,
        )
        state["documents"].remove(key)
        run.save()

    removed = len(state["documents"])
    for key in list(state["documents"]):
        attempt(
            f"document-{key.rsplit('/', 1)[-1]}", lambda key=key: remove_document(key)
        )
    state["held"], state["in_flight"] = [], []

    def release_queue(queue_id: str) -> None:
        temporary = set(state["users"].values())
        queue = run.queue_definition(queue_id)
        if (
            queue is not None
            and queue_id in state["queues_created"]
            and queue_id not in kept_queues
        ):
            run.must("admin", "deleteReviewQueue", {"id": queue_id})
        elif queue is not None:
            listed = [str(e).lower() for e in queue.get("eligibleAnnotators") or []]
            if temporary.intersection(listed):
                run._put_annotators(queue, [e for e in listed if e not in temporary])
        state["listed_on"].remove(queue_id)
        if queue_id in state["queues_created"] and queue_id not in kept_queues:
            state["queues_created"].remove(queue_id)
        run.save()

    if state["users"].get("admin"):
        for queue_id in list(state["listed_on"]):
            try:
                release_queue(queue_id)
            except Exception as e:  # noqa: BLE001 - a lasting change to a shared queue
                failures.append(f"queue-{queue_id}")
                run.report.add(
                    f"cleanup-queue-{queue_id}",
                    FAIL,
                    f"a temporary user ({', '.join(sorted(state['users'].values()))}) "
                    f"may still be listed on queue {queue_id} "
                    f"({type(e).__name__}: {e}); remove it in Review Studio › "
                    "Review Management",
                )

    def remove_profiles() -> None:
        if state["profile"]:
            run.deps.delete_profile(
                run.ctx["stack"], run.ctx["region"], state["profile"]
            )
            state["profile"] = ""
        if state["showcase_profile_created"] and not seeded.get("profile"):
            run.deps.delete_profile(
                run.ctx["stack"], run.ctx["region"], SHOWCASE_PROFILE
            )
            state["showcase_profile_created"] = False
        run.save()

    attempt("profiles", remove_profiles)

    def demo_is_seeded() -> None:
        run.deps.sleep(POLL_SECONDS)
        overview = run.must("admin", "getReviewQueuesOverview", {})
        depth = {
            (q.get("summary") or {}).get("name"): (q.get("summary") or {}).get("depth")
            for q in overview.get("queues") or []
        }
        # A state file written before the scenario invoices were seeded names no
        # counts: one document then waits in each queue.
        wanted = seeded.get("waiting") or {
            queue: 1 for queue in (DEFAULT_QUEUE, *sorted(seeded.get("queues") or []))
        }
        run.report.check(
            "demo-documents-wait-in-their-queues",
            all(depth.get(queue) == count for queue, count in wanted.items()),
            ", ".join(
                f"{queue}={depth.get(queue)} (expected {count})"
                for queue, count in wanted.items()
            ),
        )

    if seeded.get("documents"):
        run.step("demo-documents-wait-in-their-queues", demo_is_seeded)

    def remove_users() -> None:
        for role, email in list(state["users"].items()):
            run.deps.delete_cognito_user(run.ctx, email)
            del state["users"][role]
            run.save()

    attempt("users", remove_users)
    if not failures:
        run.report.add(
            "cleanup",
            PASS,
            f"{removed} document(s), the temporary users and profile removed"
            + (
                f"; left for the demo: queues {', '.join(seeded['queues'])}, profile "
                f"{seeded['profile']}, {len(seeded['documents'])} documents"
                if seeded.get("documents")
                else ""
            ),
        )


SUBCOMMANDS: dict[str, tuple[Callable[[Run], None], str]] = {
    "pre-upgrade": (pre_upgrade, "hold three documents before the stack is updated"),
    "start-in-flight": (
        start_in_flight,
        "start three documents and return without waiting",
    ),
    "post-upgrade": (
        post_upgrade,
        "check the held and in-flight documents after the update",
    ),
    "capture-switch": (
        capture_switch,
        "record Review Routing and the revisions of two profiles before an update",
    ),
    "check-switch": (
        check_switch,
        "check that the update changed neither the switch nor an earlier revision",
    ),
    "scenarios": (
        scenarios,
        "check routing and run plan scenarios on the showcase profile",
    ),
    "reasons-and-restart": (
        reasons_and_restart,
        "check the reason a reviewer reads, the plan of a finished review and "
        "where the run after a send-back and a regrouped review starts",
    ),
    "scenario-rounds": (
        scenario_rounds,
        "take the scenario invoices through their review rounds",
    ),
    "seed-demo": (
        seed_demo,
        "leave the queues, the profile, the showcase samples and the scenario "
        "invoices waiting",
    ),
    "cleanup": (cleanup, "remove what the state file names, except the demo"),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Live check of review routing and the pipeline run plan."
    )
    sub = ap.add_subparsers(dest="command", required=True, metavar="subcommand")
    for name, (_work, summary) in SUBCOMMANDS.items():
        p = sub.add_parser(name, help=summary, description=summary)
        p.add_argument("--stack-name", required=True)
        p.add_argument(
            "--region",
            default=os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION"),
        )
        p.add_argument(
            "--state-file",
            required=True,
            help="JSON file the subcommands share; names only, never a secret",
        )
    args = ap.parse_args(argv)
    if not args.region:
        ap.error("--region is required (or set AWS_REGION / AWS_DEFAULT_REGION)")
    return args


def execute(args: argparse.Namespace, deps: RoutingDeps) -> Report:
    """Run one subcommand and return its report. The state is saved whatever happens."""
    report = RedactingReport()
    run = Run(args, deps, report)
    work = SUBCOMMANDS[args.command][0]
    try:
        run.open()
        run.step(args.command, lambda: work(run))
    except CheckAborted as e:
        report.add(args.command, ERROR, str(e))
    except Exception as e:  # noqa: BLE001 - report, never mask as a pass
        report.add(args.command, ERROR, f"unexpected {type(e).__name__}: {e}")
    finally:
        if run.ctx:
            try:
                run.close()
            except Exception as e:  # noqa: BLE001 - the verdict must still be printed
                report.add(
                    "state-saved-and-auth-restored", ERROR, f"{type(e).__name__}: {e}"
                )
    return report


def main(argv: list[str] | None = None, deps: RoutingDeps | None = None) -> int:
    args = parse_args(argv)
    print(
        f"Review routing live check ({args.command}): stack {args.stack_name} ({args.region})"
    )
    report = execute(args, deps or RoutingDeps())
    code = report.exit_code()
    counts = {
        o: sum(c.outcome == o for c in report.checks) for o in (PASS, FAIL, ERROR, SKIP)
    }
    print(
        f"\n{len(report.checks)} checks: {counts[PASS]} passed, {counts[FAIL]} failed, "
        f"{counts[ERROR]} could not be run, {counts[SKIP]} skipped"
    )
    verdict = {EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_INCOMPLETE: "INCOMPLETE"}[
        code
    ]
    print(f"RESULT: {verdict} (exit {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
