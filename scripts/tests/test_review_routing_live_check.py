# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The review-routing live check reads what a run did, and can fail.

``scripts/review_routing_live_check.py`` asserts on execution histories: which
states a run entered and which tasks it scheduled. These tests drive every
subcommand against an in-memory stack that answers the AWS CLI calls and the API
operations the script makes. The stack behaves as the product should, and each
defect it can be given (a completion that runs OCR again, a rejection that
schedules a task, a document in the wrong queue, a reason with a placeholder left
in it, …) must turn the check that exists for it red. A check that stays green on
a defective stack proves nothing on a real one.

They also pin what the script may never do: write a password, a token, an account
id or an execution ARN to its state file, read a wait that ran out as a pass, or
delete a queue that was there before it ran.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from idp_common.config import revisions, routing_reason
from idp_common.config.routing_models import RoutingConfig, review_queue_of
from idp_common.review_queues import models as review_models
from idp_common.review_queues import routing_dmn
from idp_common.run_plan import planned, restart, steps
from idp_common.run_plan.models import RunKind

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent


def _load():
    sys.path.insert(0, str(SCRIPTS))
    try:
        path = SCRIPTS / "review_routing_live_check.py"
        spec = importlib.util.spec_from_file_location("review_routing_live_check", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["review_routing_live_check"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


live = _load()

ACCOUNT = "123456789012"
STATE_MACHINE = f"arn:aws:states:us-west-2:{ACCOUNT}:stateMachine:workflow"
PATTERN_STACK_NAME = "S-PATTERNSTACK-1ABC"
PATTERN_STACK_ID = (
    f"arn:aws:cloudformation:us-west-2:{ACCOUNT}:stack/{PATTERN_STACK_NAME}/0a1b"
)
API_STACK_NAME = "S-APIRESOLVERSTACK-1XYZ"
API_STACK_ID = f"arn:aws:cloudformation:us-west-2:{ACCOUNT}:stack/{API_STACK_NAME}/9z8y"
#: The API template gives the group no name, so the stack generated this one.
RESOLVER_LOG_GROUP = f"{API_STACK_NAME}-ReviewQueuesResolverFunctionLogGroup-AbCd12"
OUTPUTS = {
    "HITLTableName": "hitl",
    "S3InputBucketName": "in",
    "S3OutputBucketName": "out",
    "TrackingTableName": "tracking",
    "StateMachineArn": STATE_MACHINE,
    "S3ConfigurationBucketName": "config",
}
EXISTING = "existing.annotator@example.com"
TAG = "abcd1234"
ANNOTATOR = f"review-routing-live-annotator-{TAG}@example.invalid"
BYSTANDER = f"review-routing-live-bystander-{TAG}@example.invalid"

#: The states of each kind of run on the current definition.
FIRST_RUN = [
    "CheckRunPlan",
    "PreprocessingHook",
    "OCRStep",
    "ClassificationStep",
    "ExtractionStep",
    "AssessmentStep",
    "Pipeline_AssembleDocumentStep",
    "CheckPolicyRuleValidationEnabled",
    "PolicyClassificationStep",
    "RoutingStep",
    "CheckRoutingDecision",
]
HOLD_END = ["MarkReviewHold", "SetReviewHoldStatus", "WorkflowComplete"]
CONTINUE_END = ["SummarizationStep", "WorkflowComplete"]
POLICY_AND_ROUTING = [
    "CheckPolicyRuleValidationEnabled",
    "PolicyClassificationStep",
    "RoutingStep",
    "CheckRoutingDecision",
]
#: What one section does in a run that starts at extraction for some sections.
SECTION_RUN = ["ExtractionStep", "AssessmentStep", "PostExtractionHook"]
#: The definition before review routing: no run plan, no routing step, and the
#: assembly step under its name there.
OLD_RUN = [
    "OCRStep",
    "ClassificationStep",
    "ExtractionStep",
    "AssessmentStep",
    "Pipeline_ProcessResultsStep",
    "CheckHITLRequired",
]
OLD_HOLD_END = ["MarkReviewHold", "SetReviewHoldStatus", "WorkflowComplete"]
OLD_CONTINUE_END = [
    "CheckRuleValidationEnabled",
    "SummarizationStep",
    "WorkflowComplete",
]
#: A run enters a state every this many seconds, so the old definition's assembly
#: step is entered a minute after the upload.
STATE_SECONDS = 15
ASSEMBLY_AFTER_SECONDS = STATE_SECONDS * OLD_RUN.index("Pipeline_ProcessResultsStep")

#: The showcase preset: its routing rules by id, and the value of its limit.
PRESET = yaml.safe_load(live.SHOWCASE_CONFIG.read_text())
PRESET_RULES = {rule["id"]: rule for rule in PRESET["routing"]["tables"][0]["rules"]}
#: The review queue each routing rule of the preset names.
PRESET_QUEUES = {
    rule.id: review_queue_of(rule.actions)
    for rule in RoutingConfig.model_validate(PRESET["routing"]).tables[0].rules
}
MAX_PLACEMENTS = PRESET["routing"]["max_consecutive_placements"]
#: What each scenario invoice prints, by file name.
TRUTH = {
    document["file"]: document["fields"]
    for document in json.loads(
        (live.SCENARIO_DIR / "scenarios.truth.json").read_text()
    )["documents"]
}
#: The vendors the showcase's vendor routing rule accepts.
APPROVED_VENDORS = re.findall(
    r'"([^"]*)"', PRESET_RULES["scenario-vendor-not-approved"]["conditions"][0]["test"]
)
NET_AMOUNT_RULE_ID = f"{live.SCENARIO_POLICY}.{live.NET_AMOUNT_POLICY_RULE}"


def events_for(states: list[str], started: datetime | None = None) -> list[dict]:
    """A history: each state entered, and a task scheduled for each ``…Step`` and hook."""
    out: list[dict] = []
    previous = 0
    for index, state in enumerate(states):
        task = state.endswith(("Step", "Hook"))
        out.append(
            {
                "id": len(out) + 1,
                "previousEventId": previous,
                "type": "TaskStateEntered" if task else "PassStateEntered",
                "stateEnteredEventDetails": {"name": state},
                **(
                    {}
                    if started is None
                    else {
                        "timestamp": (
                            started + timedelta(seconds=STATE_SECONDS * index)
                        ).isoformat()
                    }
                ),
            }
        )
        previous = len(out)
        if task:
            kind = (
                "TaskScheduled" if state.endswith("Hook") else "LambdaFunctionScheduled"
            )
            out.append({"id": len(out) + 1, "previousEventId": previous, "type": kind})
            previous = len(out)
    return out


def _options(args: tuple) -> dict:
    """The ``--flag value`` pairs of a CLI call; a flag with no value maps to True."""
    found: dict = {}
    rest = list(args)
    while rest:
        token = rest.pop(0)
        if isinstance(token, str) and token.startswith("--"):
            takes = bool(rest) and not str(rest[0]).startswith("--")
            found[token] = rest.pop(0) if takes else True
    return found


def ddb(value: Any) -> dict:
    """Plain data as a DynamoDB attribute value, the way the AWS CLI prints one."""
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if value is None:
        return {"NULL": True}
    if isinstance(value, list):
        return {"L": [ddb(item) for item in value]}
    if isinstance(value, dict):
        return {"M": {name: ddb(item) for name, item in value.items()}}
    raise TypeError(f"no DynamoDB form for {type(value).__name__}")


def _snake_failure(failure: dict) -> dict:
    """A failed policy rule as the status object and the CLI spell it."""
    return {
        "rule_id": failure["ruleId"],
        **{k: failure[k] for k in failure if k != "ruleId"},
    }


class FakeStack:
    """A stack with review routing: queues, profiles, users, documents and their runs."""

    def __init__(self) -> None:
        self.clock = 0.0
        self.wall = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.queues: dict[str, dict] = {
            "default": self._queue("default", "Default", [EXISTING], default=True)
        }
        self.paged = True
        #: The default profile as it is stored: the switch is off, as on a stack
        #: that was created before Review Routing was switched on at creation.
        self.default_config: dict = {"hitl": {"enabled": False}, "notes": "Default"}
        self.profiles: dict[str, dict] = {}
        self.profile_uploads: list[str] = []
        #: The revision bodies of each profile: file name to entity tag.
        self.revisions: dict[str, dict[str, str]] = {"default": {}}
        self._cut_revision("default", {"notes": "as created"})
        self._cut_revision("default", self.default_config)
        self.users: dict[str, str] = {}
        self.passwords: dict[str, str] = {}
        self.secrets: list[str] = []
        self.docs: dict[str, dict] = {}
        #: How many runs each key has had: an execution name is never used twice.
        self.run_numbers: dict[str, int] = {}
        #: The history entries of each review cycle: ``(event, details)``.
        self.history_rows: dict[tuple[str, str], list[tuple[str, dict]]] = {}
        self.removed_objects: list[str] = []
        #: The nested pattern stack's events for the assembly functions, as
        #: ``[logical id, status, time]``.
        self.function_events: list[list[str]] = []
        #: Documents whose assessment raises no confidence alert, by a part of
        #: their key: they need no review.
        self.no_alerts: set[str] = set()
        #: Documents with a required field missing, by a part of their key.
        self.missing_required: set[str] = set()
        self.auth_enabled = 0
        self.auth_restored = 0
        #: Runs on the definition before review routing.
        self.old_definition = False
        #: What is wrong with this stack; empty for one that behaves.
        self.defects: set[str] = set()
        #: Documents that never leave RUNNING, by a part of their key.
        self.stuck: set[str] = set()
        #: Documents whose next upload is not picked up yet, by a part of their
        #: key: the row still shows what the upload before it left.
        self.not_picked_up: set[str] = set()
        #: How many reads of a status object still show the status before the last
        #: change: the object is written after the tracking row.
        self.status_lag = 0
        #: How long after a document's status says it is held its item is active
        #: and its tracking row names what placed it.
        self.activation_seconds = 0.0
        #: When each call reached the review queues function, and for which field.
        self.resolver_calls: list[tuple[datetime, str]] = []
        #: How many reads of that function's log still find no event.
        self.log_lag = 0
        self.cycles = 0
        #: The address of whoever makes the API call being answered.
        self.caller = ""
        #: Placements of a scenario invoice that another routing rule makes, into
        #: the same queue, and placements whose reason is left as it was written:
        #: each as ``(scenario, placement number)``.
        self.misrouted_rounds: set[tuple[str, int]] = set()
        self.unrendered_rounds: set[tuple[str, int]] = set()
        #: Scenario invoices that are held again after their last review.
        self.never_complete: set[str] = set()

    @staticmethod
    def _queue(queue_id: str, name: str, annotators: list[str], default=False) -> dict:
        return {
            "id": queue_id,
            "name": name,
            "description": "",
            "priority": 5,
            "checklist": [],
            "rejectReasons": ["Illegible"],
            "skipReasons": ["Out of time"],
            "timeLimitMinutes": 60,
            "eligibleAnnotators": list(annotators),
            "isDefaultQueue": default,
            "enabled": True,
            "version": 1,
        }

    # ------------------------------------------------------------------ deps
    def deps(self):
        return live.RoutingDeps(
            resolve_stack=lambda stack, region: {
                "stack": stack,
                "region": region,
                "user_pool": "pool",
                "client": "client",
                "api_base": "https://api.invalid/api",
            },
            aws=self.aws,
            enable_admin_auth=self._enable_auth,
            restore_auth_flows=self._restore_auth,
            create_cognito_user=self._create_user,
            delete_cognito_user=lambda ctx, email: self.users.pop(email),
            get_id_token=self._token,
            call_body=self.call_body,
            upload_profile=self._upload_profile,
            delete_profile=lambda stack, region, profile: self.profiles.pop(profile),
            sleep=self.sleep,
            monotonic=lambda: self.clock,
            rand=lambda: TAG,
            upload_config_file=lambda stack, region, profile, path: (
                self._upload_profile(
                    stack, region, profile, yaml.safe_load(Path(path).read_text())
                )
            ),
            document_status=lambda stack, region, key: self._cli_status(key),
            now=lambda: self.wall,
        )

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        self.wall += timedelta(seconds=seconds)

    def _enable_auth(self, ctx) -> None:
        self.auth_enabled += 1

    def _restore_auth(self, ctx) -> None:
        self.auth_restored += 1

    def _create_user(self, ctx, email, group, password) -> None:
        self.users[email] = group
        self.passwords[email] = password
        self.secrets.append(password)

    def _token(self, ctx, email, password) -> str:
        assert self.passwords[email] == password, "signed in with a stale password"
        token = f"eyJ|{email}|" + "x" * 120
        self.secrets.append(token)
        return token

    def _cut_revision(self, profile: str, config: dict) -> str:
        """A save cuts a revision: a body written once, under the next number."""
        bodies = self.revisions.setdefault(profile, {})
        number = max((int(name.split(".")[0]) for name in bodies), default=0) + 1
        name = f"{number:06d}.json.gz"
        text = json.dumps([profile, number, config], sort_keys=True, default=str)
        bodies[name] = hashlib.sha256(text.encode()).hexdigest()[:32]
        return name

    def _upload_profile(self, stack, region, profile, config) -> None:
        self.profile_uploads.append(profile)
        self.profiles[profile] = dict(config)
        self._cut_revision(profile, config)

    # ------------------------------------------------------------- documents
    def _sample(self, key: str):
        return next(s for s in live.SHOWCASE_SAMPLES if key.endswith(s.path.name))

    @staticmethod
    def _uri(key: str, section_id: str) -> str:
        return f"s3://out/{key}/sections/{section_id}/result.json"

    def _contents(self, key: str) -> tuple[list[dict], dict[str, dict]]:
        """The sections a document is split into, and the result of each."""
        name = key.rsplit("/", 1)[-1]
        printed = next((v for file, v in TRUTH.items() if name.endswith(file)), None)
        if printed is not None:
            # A field the page does not print has no value.
            fields = {k: v for k, v in printed.items() if v is not None}
            parts = [("1", [1], live.SCENARIO_CLASS, fields)]
        elif name.endswith(live.REGROUP_SAMPLE.path.name):
            person = {"CurrentGrossPay": 1200.5, "EmployeeName": "Ada Example"}
            parts = [
                ("1", [1, 2], "Payslip", person),
                ("2", [3], "Bank-checks", {"Amount": 250, "Payee": "Ada Example"}),
                ("3", [4, 5, 6], "W2", {"EmployeeName": "Ada Example"}),
            ]
        else:
            parts = [("1", [1], "W2", {"Name": "Ada"})]
        sections = [
            {
                "Id": section_id,
                "PageIds": pages,
                "Class": document_class,
                "OutputJSONUri": self._uri(key, section_id),
            }
            for section_id, pages, document_class, _ in parts
        ]
        results = {
            section_id: {"inference_result": dict(fields)}
            for section_id, _, _, fields in parts
        }
        if name.endswith(live.REGROUP_SAMPLE.path.name):
            # A page of a class the configuration excludes: it is not processed,
            # so it has no result and is not in review.
            sections.append(
                {
                    "Id": "4",
                    "PageIds": [7],
                    "Class": "Blank",
                    "OutputJSONUri": "",
                    "Excluded": True,
                }
            )
        return sections, results

    def _scenario_placement(self, doc: dict) -> dict | None:
        """What the showcase's four scenario routing rules decide for an invoice."""
        printed, values = doc["printed"], doc["results"]["1"]["inference_result"]
        # Policy rule validation reads the page, so a correction does not clear it.
        fails = printed["NetAmountDue"] != printed["GrossTotal"] - printed["Discount"]
        unreviewed = not doc["reviewed"]
        rules = [
            (
                "scenario-po-without-purchase-order",
                unreviewed and values.get("InvoiceType") == "PO-backed",
            ),
            (
                "scenario-net-amount-check",
                unreviewed
                and fails
                and "net_amount_rule_never_matches" not in self.defects,
            ),
            (
                "scenario-vendor-not-approved",
                values.get("Vendor") not in APPROVED_VENDORS,
            ),
            ("scenario-payment-terms-missing", not values.get("PaymentTerms")),
        ]
        if "vendor_rule_is_tried_first" in self.defects:
            rules.insert(0, rules.pop(2))
        if "reviewed_documents_are_not_placed_again" in self.defects and not unreviewed:
            return None
        for rule, matches in rules:
            if matches:
                return {"queue": PRESET_QUEUES[rule], "rule": rule}
        return None

    def _placement(self, key: str, doc: dict) -> dict | None:
        """Where routing places a document; ``None`` when it is not held."""
        if doc["source"] == "test-studio" and "studio_held" not in self.defects:
            return None
        if doc.get("printed") and doc["profile"] == live.SHOWCASE_PROFILE:
            return self._scenario_placement(doc)
        if doc["reviewed"]:
            # Every other routing rule applies only before the first review.
            return None
        if doc["profile"] != live.SHOWCASE_PROFILE:
            if "unchanged_profile_not_held" in self.defects and not self.old_definition:
                return None
            return {"queue": "default", "rule": live.BUILTIN_CONFIDENCE_RULE}
        sample = self._sample(key)
        queue, rule = sample.queue, sample.rule
        if sample.name == "invoice" and "invoice_misrouted" in self.defects:
            queue, rule = "default", live.BUILTIN_CONFIDENCE_RULE
        if sample.name == "lending" and "lending_rule_unnamed" in self.defects:
            rule = ""
        placed = {"queue": queue, "rule": rule}
        if sample.name == "lending" and "lending_falls_back" in self.defects:
            return {**placed, "queue": "default", "fallback": "queue_disabled"}
        if queue not in self.queues:
            if "fallback_keeps_the_missing_queue" not in self.defects:
                placed["queue"] = "default"
            if "fallback_reason_lost" not in self.defects:
                placed["fallback"] = "queue_missing"
            if "fallback_forgets_the_rule" in self.defects:
                placed["rule"] = ""
        return placed

    def _start_run(self, key: str, states: list[str], status: str) -> None:
        doc = self.docs[key]
        self.run_numbers[key] = self.run_numbers.get(key, 0) + 1
        name = f"{key.rsplit('/', 1)[-1]}-{self.run_numbers[key]}"
        doc["runs"][name] = {"states": states, "started": self.wall}
        doc["execution"], doc["status"] = name, status

    def _policy_failures(self, key: str, doc: dict) -> list[dict]:
        """The policy rules a document failed, as the reviewer is shown them."""
        if "no_failed_policy_rule" in self.defects:
            return []
        printed = doc.get("printed")
        if printed and (
            printed["NetAmountDue"] != printed["GrossTotal"] - printed["Discount"]
        ):
            return [
                {
                    "ruleId": NET_AMOUNT_RULE_ID,
                    "policy": live.SCENARIO_POLICY,
                    "name": live.NET_AMOUNT_POLICY_RULE,
                    "reason": "The net amount due is not the gross total minus "
                    "the discount.",
                    "pages": [1],
                }
            ]
        if key.endswith(live.SAMPLES_BY_NAME["statement"].path.name):
            return [
                {
                    "ruleId": "statement_period.transactions_within_period",
                    "policy": "statement_period",
                    "name": "transactions_within_period",
                    "reason": "Transactions are dated outside the statement period.",
                    "pages": [2, 3],
                }
            ]
        return []

    def _reason(self, rule_id: str, key: str, doc: dict) -> tuple[str, str]:
        """What the reviewer reads, and where it comes from: the routing rule's
        reason in the preset, with the document's values in it."""
        rule = PRESET_RULES.get(rule_id)
        if rule is None:
            return "No routing rule matched.", routing_reason.SOURCE_FALLBACK
        values = doc["results"]["1"]["inference_result"]

        def resolve(placeholder):
            if placeholder.kind == routing_reason.KIND_CONDITION_VALUE:
                test = rule["conditions"][placeholder.index - 1]["test"]
                return re.findall(r'"([^"]*)"', test) or test.lstrip("<>= ")
            if placeholder.input == "field_value":
                return values.get(placeholder.path.rsplit(":", 1)[-1], 1500)
            if placeholder.input == "section_classes":
                return [section["Class"] for section in doc["sections"]]
            if placeholder.input == "failed_policy_rule_count":
                return len(self._policy_failures(key, doc))
            return 1

        text = routing_reason.render_reason(rule["reason"], resolve)
        if "reason_keeps_a_placeholder" in self.defects:
            text += " See {page_count}."
        if "reason_is_generated" in self.defects:
            return text, routing_reason.SOURCE_FALLBACK
        return text, routing_reason.SOURCE_ADMIN

    @staticmethod
    def _scenario(key: str) -> str:
        """Which scenario invoice a document is: one, two or three; "" for none."""
        found = re.search(r"routing-scenario-(\w+)-problems?\.pdf$", key)
        return found.group(1) if found else ""

    def _hold(self, key: str, placed: dict, placements: int = 1) -> None:
        doc = self.docs[key]
        self.cycles += 1
        cycle = f"cycle-{self.cycles}"
        this_round = (self._scenario(key), placements)
        if this_round in self.misrouted_rounds:
            placed = {**placed, "rule": live.BUILTIN_CONFIDENCE_RULE}
        reasons = [{"kind": "low_confidence", "sectionId": "1"}]
        if key.endswith(live.SAMPLES_BY_NAME["invoice"].path.name):
            reasons = reasons if "invoice_confidence_reason" in self.defects else []
        if doc.get("printed"):
            # The fields of a scenario invoice carry a confidence threshold of 0.
            reasons = []
        routed = None
        if not self.old_definition:
            reason, source = self._reason(placed["rule"], key, doc)
            if this_round in self.unrendered_rounds:
                reason = PRESET_RULES[placed["rule"]]["reason"]
            failures = self._policy_failures(key, doc)
            routed = {
                "tableId": "review-routing",
                "ruleId": placed["rule"],
                "placedQueue": placed["queue"],
                "fallbackReason": placed.get("fallback", ""),
                "reason": reason,
                "reasonSource": source,
                "policyFailures": failures,
                "policyFailureCount": len(failures),
            }
            entries = self.history_rows.setdefault((key, cycle), [])
            if "history_lacks_the_reason" not in self.defects:
                entries.append(
                    (
                        "routed",
                        {
                            "queue": placed["queue"],
                            "rule_id": placed["rule"],
                            "routing_reason": reason,
                            "reason_source": source,
                        },
                    )
                )
        doc["item"] = {
            "objectKey": key,
            "cycleId": cycle,
            "queue": placed["queue"],
            "state": "OPEN",
            "version": 1,
            "reasons": reasons,
            "routedBy": routed,
        }
        doc["owner"] = None
        doc["drafts"] = {}
        counted = "placements_are_not_counted" not in self.defects
        doc["placements"] = placements if counted else 1
        #: The sections in review, and each section's pages, when it was placed.
        doc["in_review"] = [s["Id"] for s in doc["sections"] if not s.get("Excluded")]
        doc["layout"] = {s["Id"]: list(s["PageIds"]) for s in doc["sections"]}
        doc["active_at"] = self.clock + self.activation_seconds
        doc["review_status"] = "in_review"
        doc["sequence"] = doc.get("sequence", 0) + 1

    def _upload(self, key: str, metadata: str) -> None:
        if key in self.docs and any(part in key for part in self.not_picked_up):
            return
        fields = dict(part.split("=", 1) for part in metadata.split(","))
        sections, results = self._contents(key)
        name = key.rsplit("/", 1)[-1]
        self.docs[key] = doc = {
            "profile": fields["config-version"],
            "source": fields.get("submission-source", ""),
            "runs": {},
            "item": None,
            "owner": None,
            "status": "QUEUED",
            "execution": "",
            "alerts": 0 if any(part in key for part in self.no_alerts) else 1,
            "missing": 1 if any(part in key for part in self.missing_required) else 0,
            "sections": sections,
            "results": results,
            "drafts": {},
            #: What a scenario invoice prints; ``None`` for any other document.
            "printed": next((v for f, v in TRUTH.items() if name.endswith(f)), None),
            "reviewed": False,
            "placements": 0,
        }
        if any(part in key for part in self.stuck):
            self._start_run(key, FIRST_RUN[:3], "RUNNING")
            return
        placed = self._placement(key, doc)
        reasons = doc["alerts"] + doc["missing"]
        if not reasons and doc["profile"] != live.SHOWCASE_PROFILE:
            placed = None
        first = list(FIRST_RUN)
        bypass = (
            "studio_bypasses_routing" if placed is None else "placed_outside_routing"
        )
        if bypass in self.defects:
            first.remove("RoutingStep")
        if self.old_definition:
            lost = "in_flight_run_not_held" in self.defects and "in-flight" in key
            if placed is None or lost:
                self._start_run(key, OLD_RUN + OLD_CONTINUE_END, live.COMPLETED)
            else:
                self._start_run(key, OLD_RUN + OLD_HOLD_END, live.HELD)
                self._hold(key, placed)
        elif placed is None:
            self._start_run(key, first + CONTINUE_END, live.COMPLETED)
        else:
            self._start_run(key, first + HOLD_END, live.HELD)
            self._hold(key, placed)

    def _regrouped(self, doc: dict) -> list[str]:
        """The sections whose pages are not the ones they had when the document
        was placed: a completed review extracts them again."""
        placed = doc["layout"]
        return [
            section["Id"]
            for section in doc["sections"]
            if not section.get("Excluded")
            and placed.get(section["Id"]) != section["PageIds"]
        ]

    def _apply_review(self, key: str, doc: dict, extracted: list[str]) -> None:
        """What the run after a completed review leaves in the section results."""
        for section in doc["sections"]:
            section_id = section["Id"]
            draft = (doc["drafts"].get(section_id) or {}).get("inference_result") or {}
            if section_id in extracted:
                added = not section["OutputJSONUri"]
                if added and "regrouped_section_has_no_result" in self.defects:
                    continue
                stored = doc["results"].get(section_id) or {
                    "inference_result": {"EmployeeName": "Ada Example"}
                }
                # Extracted again: the values of the page, then the corrections.
                fresh, changed = dict(stored["inference_result"]), []
                if "correction_lost_after_extraction" not in self.defects:
                    for name, value in draft.items():
                        if stored["inference_result"].get(name) != value:
                            fresh[name] = value
                            changed.append(name)
                section["OutputJSONUri"] = self._uri(key, section_id)
                doc["results"][section_id] = {
                    "inference_result": fresh,
                    live.REVIEW_RECORD_KEY: {
                        "v": 1,
                        "confirmed": "extracted_sections_are_confirmed" in self.defects,
                        "extracted_again": True,
                        "changed_paths": sorted(changed),
                        "changed_paths_truncated": False,
                        "unapplied_paths": [],
                    },
                }
            elif section["OutputJSONUri"]:
                result, changed = doc["results"][section_id], []
                if "correction_is_not_applied" not in self.defects:
                    for name, value in draft.items():
                        if result["inference_result"].get(name) != value:
                            result["inference_result"][name] = value
                            changed.append(name)
                result[live.REVIEW_RECORD_KEY] = {
                    "v": 1,
                    "confirmed": True,
                    "changed_paths": sorted(changed),
                    "changed_paths_truncated": False,
                }
        doc["reviewed"] = all(
            (doc["results"].get(section["Id"]) or {})
            .get(live.REVIEW_RECORD_KEY, {})
            .get("confirmed")
            is True
            for section in doc["sections"]
            if not section.get("Excluded")
        )

    def _cli_status(self, key: str) -> dict:
        """What ``idp-cli status --format json`` prints for a document."""
        doc = self.docs[key]
        routed = (doc["item"] or {}).get("routedBy") or {}
        printed = {
            "document_id": key,
            "status": doc["status"],
            "review_status": doc.get("review_status"),
            "exit_code": 2,
        }
        if routed and "cli_status_lacks_the_reason" not in self.defects:
            printed["routing_reason"] = routed["reason"]
            if routed["policyFailureCount"]:
                printed["policy_failures"] = [
                    _snake_failure(failure) for failure in routed["policyFailures"]
                ]
                printed["policy_failure_count"] = routed["policyFailureCount"]
        return printed

    # ------------------------------------------------------------------- aws
    def aws(self, *args, region=None):
        service, operation = args[0], args[1]
        opts = _options(args[2:])
        if (service, operation) == ("cloudformation", "describe-stacks"):
            return [{"OutputKey": k, "OutputValue": v} for k, v in OUTPUTS.items()]
        if (service, operation) == ("cloudformation", "describe-stack-resources"):
            logical = opts["--logical-resource-id"]
            if logical == live.PLAN_RESOLVER_LOG_GROUP:
                # The log group belongs to the nested API stack.
                assert opts["--stack-name"] == API_STACK_ID
                return RESOLVER_LOG_GROUP
            assert opts["--stack-name"] == "S"
            nested = {
                live.PATTERN_STACK: PATTERN_STACK_ID,
                live.API_STACK: API_STACK_ID,
            }
            absent = {
                live.PATTERN_STACK: "no_pattern_stack",
                live.API_STACK: "no_api_stack",
            }
            return None if absent[logical] in self.defects else nested[logical]
        if (service, operation) == ("cloudformation", "describe-stack-events"):
            # The functions belong to the nested stack, not to the one deployed.
            assert opts["--stack-name"] == PATTERN_STACK_ID
            assert all(name in opts["--query"] for name in live.ASSEMBLY_FUNCTIONS)
            return [list(event) for event in self.function_events]
        if (service, operation) == ("s3api", "put-object"):
            assert Path(opts["--body"]).is_file()
            return self._upload(opts["--key"], opts["--metadata"])
        if (service, operation) == ("s3api", "list-objects-v2"):
            assert opts["--bucket"] == OUTPUTS["S3ConfigurationBucketName"]
            prefix = opts["--prefix"]
            assert prefix.startswith(f"{revisions.REVISION_S3_PREFIX}/")
            bodies = self.revisions.get(prefix.split("/")[1], {})
            # The CLI prints null for a prefix that holds nothing.
            return [
                [f"{prefix}{name}", f'"{tag}"'] for name, tag in sorted(bodies.items())
            ] or None
        if (service, operation) == ("s3", "cp"):
            return self._read(args[2])
        if (service, operation) == ("s3", "rm"):
            return self.removed_objects.append(args[2])
        if service == "dynamodb":
            return self._dynamodb(operation, opts)
        if service == "stepfunctions":
            name = opts["--execution-arn"].rsplit(":", 1)[-1]
            assert opts["--execution-arn"].startswith(
                STATE_MACHINE.replace(":stateMachine:", ":execution:")
            )
            run = next(d["runs"][name] for d in self.docs.values() if name in d["runs"])
            assert operation == "get-execution-history"
            return {"events": events_for(run["states"], run["started"])}
        if (service, operation) == ("logs", "start-query"):
            # The workflow's log groups are named after the nested stack; the
            # review queues function's is the one its stack generated.
            group = opts["--log-group-name"]
            if group != RESOLVER_LOG_GROUP and not group.startswith(
                f"/{PATTERN_STACK_NAME}/lambda/"
            ):
                raise RuntimeError(f"Log group '{group}' does not exist")
            warm = "initDuration" in opts["--query-string"]
            return {
                "queryId": "|".join(
                    [
                        group,
                        "warm" if warm else "all",
                        opts["--start-time"],
                        opts["--end-time"],
                    ]
                )
            }
        if (service, operation) == ("logs", "get-query-results"):
            group, kind, start, end = opts["--query-id"].split("|")
            if group == RESOLVER_LOG_GROUP:
                return self._resolver_latency(int(start), int(end))
            # The apply function was only ever started cold in this period.
            if group.endswith("ApplyReviewChangesFunction") and kind == "warm":
                return {"status": "Complete", "results": []}
            p95 = "2400.0" if "routing_is_slow" in self.defects else "480.5"
            row = [("n", "7"), ("p50", "120.0"), ("p95", p95)]
            return {
                "status": "Complete",
                "results": [[{"field": f, "value": v} for f, v in row]],
            }
        raise AssertionError(f"unexpected aws call: {args}")

    def _resolver_latency(self, start: int, end: int) -> dict:
        """The review queues function's invocations in a period, every one warm."""
        if self.log_lag > 0:
            # The events of an invocation are readable some time after it ended.
            self.log_lag -= 1
            return {"status": "Complete", "results": []}
        count = sum(start <= at.timestamp() <= end for at, _ in self.resolver_calls)
        if not count:
            return {"status": "Complete", "results": []}
        p95 = "1800.0" if "completion_plan_is_slow" in self.defects else "310.0"
        row = [("n", str(count)), ("p50", "95.0"), ("p95", p95)]
        return {
            "status": "Complete",
            "results": [[{"field": f, "value": v} for f, v in row]],
        }

    def _read(self, uri: str):
        key = uri.removeprefix("s3://out/")
        if key.endswith("/review/status.json"):
            doc = self.docs.get(key.removesuffix("/review/status.json"))
            if not doc or not doc.get("review_status"):
                raise RuntimeError("404")
            old = "status_schema_old" in self.defects
            shown = doc["review_status"]
            if doc.get("status_lag", 0) > 0:
                doc["status_lag"] -= 1
                shown = doc.get("review_status_before", shown)
            routed = (doc["item"] or {}).get("routedBy")
            routed_by = None
            if routed:
                routed_by = {
                    "table_id": routed["tableId"],
                    "rule_id": routed["ruleId"],
                    "placed_queue": routed["placedQueue"],
                    "fallback_reason": routed["fallbackReason"],
                }
                if "status_object_lacks_the_reason" not in self.defects:
                    routed_by.update(
                        reason=routed["reason"],
                        reason_source=routed["reasonSource"],
                        policy_failures=[
                            _snake_failure(f) for f in routed["policyFailures"]
                        ],
                        policy_failure_count=routed["policyFailureCount"],
                    )
            return {
                "schema_version": "1.0" if old else "1.1",
                "status": shown,
                "cycle_id": doc["cycle_id"]
                if doc["item"] is None
                else doc["item"]["cycleId"],
                "sequence": doc["sequence"],
                "reason_count": 2,
                "routed_by": routed_by,
            }
        found = re.fullmatch(r"(.+)/sections/([^/]+)/result\.json", key)
        doc = self.docs.get(found.group(1)) if found else None
        if doc is None or found.group(2) not in doc["results"]:
            raise RuntimeError("404")
        return copy.deepcopy(doc["results"][found.group(2)])

    def _tracking_row(self, doc: dict) -> dict:
        arn = STATE_MACHINE.replace(":stateMachine:", ":execution:")
        counts = {
            "ConfidenceAlertCount": {"N": str(doc["alerts"])},
            "MissingRequiredFieldCount": {"N": str(doc["missing"])},
        }
        if "tracking_row_counts_nothing" in self.defects:
            counts = {}
        review = {}
        routed = (doc["item"] or {}).get("routedBy")
        # The row names what placed the document once the run has ended.
        if routed and self.clock >= doc["active_at"]:
            copied = {"tableId": routed["tableId"], "ruleId": routed["ruleId"]}
            if "tracking_row_lacks_the_reason" not in self.defects:
                copied.update(
                    reason=routed["reason"],
                    policyFailures=routed["policyFailures"],
                    policyFailureCount=routed["policyFailureCount"],
                )
            review = {"ReviewRoutedBy": ddb(copied)}
        return {
            "ObjectStatus": {"S": doc["status"]},
            "WorkflowExecutionArn": {"S": f"{arn}:{doc['execution']}"},
            "Sections": ddb(doc["sections"]),
            **counts,
            **review,
        }

    def _dynamodb(self, operation: str, opts: dict):
        table = opts["--table-name"]
        if operation == "get-item":
            wanted = json.loads(opts["--key"])
            if table == "hitl":
                # The review item of a document, as the review table stores it.
                assert wanted["SK"] == {"S": "ITEM"}
                doc = self.docs.get(wanted["PK"]["S"].removeprefix("DOC#"))
                if doc is None or doc["item"] is None:
                    return {}
                return {
                    "Item": {
                        **wanted,
                        "CycleId": {"S": doc["item"]["cycleId"]},
                        "Queue": {"S": doc["item"]["queue"]},
                        "PlacementCount": {"N": str(doc["placements"])},
                    }
                }
            doc = self.docs.get(wanted["PK"]["S"].removeprefix("doc#"))
            return {} if doc is None else {"Item": self._tracking_row(doc)}
        if operation == "query":
            values = json.loads(opts["--expression-attribute-values"])
            key = values[":pk"]["S"].removeprefix("DOC#")
            if ":cycle" in values:
                cycle = values[":cycle"]["S"].removeprefix("H#").removesuffix("#")
                return {
                    "Items": [
                        {"Event": {"S": event}, "Details": ddb(details)}
                        for event, details in self.history_rows.get((key, cycle), [])
                    ]
                }
            doc = self.docs.get(key)
            rows = (
                [{"PK": values[":pk"], "SK": {"S": "ITEM"}}]
                if doc and doc["item"]
                else []
            )
            return {"Items": rows}
        if operation == "delete-item":
            key = json.loads(opts["--key"])["PK"]["S"]
            if table == "hitl":
                self.docs[key.removeprefix("DOC#")]["item"] = None
            else:
                self.docs.pop(key.removeprefix("doc#"))
            return None
        raise AssertionError(f"unexpected dynamodb call: {operation}")

    # ------------------------------------------------------------------- API
    def call_body(self, api_base, field_name, args, token):
        self.caller = token.split("|")[1]
        role = self.users[self.caller].lower()
        if "Review" in field_name or field_name == "testRoutingTable":
            # One function serves the review queue operations.
            self.resolver_calls.append((self.wall, field_name))
        try:
            answer = getattr(self, f"op_{field_name}")(role, args)
        except KeyError as e:
            return 404, json.dumps({"message": f"not found: {e}"})
        except PermissionError as e:
            return 403, json.dumps({"message": str(e)})
        except ValueError as e:
            return 400, json.dumps({"message": str(e)})
        return 200, json.dumps(answer)

    def _item(self, args: dict) -> dict:
        item = self.docs[args["objectKey"]]["item"]
        if item is None or item["cycleId"] != args["cycleId"]:
            raise KeyError(args["objectKey"])
        return item

    def _holds(self, doc: dict) -> bool:
        """Whether the caller holds the document's review item."""
        claimed = doc["item"] is not None and doc["item"]["state"] == "CLAIMED"
        return claimed and doc["owner"] == self.caller

    def op_listReviewQueues(self, role, args):
        rows = [dict(q) for q in self.queues.values()]
        return {"items": rows, "nextToken": None} if self.paged else rows

    def op_updateReviewQueue(self, role, args):
        queue = self.queues[args["input"]["id"]]
        queue.update(args["input"])
        return queue

    def op_createReviewQueue(self, role, args):
        given = args["input"]
        assert given["id"] not in self.queues, "the queue exists"
        self.queues[given["id"]] = self._queue(
            given["id"], given["name"], given.get("eligibleAnnotators") or []
        )
        return self.queues[given["id"]]

    def op_deleteReviewQueue(self, role, args):
        del self.queues[args["id"]]
        for doc in self.docs.values():
            if doc["item"] and doc["item"]["queue"] == args["id"]:
                doc["item"]["queue"] = "default"
        return True

    def op_getReviewQueuesOverview(self, role, args):
        def depth(queue_id: str) -> int:
            return sum(
                1
                for doc in self.docs.values()
                if doc["item"]
                and doc["item"]["queue"] == queue_id
                and doc["status"] == live.HELD
            )

        return {
            "queues": [{"summary": {"name": q, "depth": depth(q)}} for q in self.queues]
        }

    def op_getConfigVersion(self, role, args):
        # The default profile is returned whole; any other as what it stores, and
        # a profile that does not exist as an empty object.
        profile = self.profiles.get(args["versionName"])
        unreadable = "default_profile_is_unreadable" in self.defects
        return {
            "success": True,
            "Default": {} if unreadable else json.dumps(self.default_config),
            "Custom": json.dumps(profile) if profile is not None else {},
        }

    def op_updateConfiguration(self, role, args):
        delta = json.loads(args["customConfig"])
        if "routing" in delta and role != "admin":
            if "author_saves_routing" not in self.defects:
                raise PermissionError("ROLE: only Admins can change routing rules")
        if "notes" in delta and "author_cannot_save" in self.defects:
            raise PermissionError("ROLE")
        self.profiles[args["versionName"]].update(delta)
        return {"success": True}

    def op_getReviewItem(self, role, args):
        key = args["objectKey"]
        doc = self.docs[key]
        if doc["item"] is None:
            raise KeyError(key)
        queue = self.queues.get(doc["item"]["queue"], {})
        listed = queue.get("eligibleAnnotators") or []
        if role == "annotator" and not self._holds(doc) and self.caller not in listed:
            raise PermissionError("NOT_A_MEMBER: you are not a member of this queue")
        sections = {section["Id"]: section for section in doc["sections"]}
        item = dict(doc["item"])
        if self.clock < doc["active_at"]:
            item["state"] = "HOLDING"
        return {
            "item": item,
            "sections": [
                {
                    "sectionId": section_id,
                    "documentClass": sections[section_id]["Class"],
                    "resultUri": sections[section_id]["OutputJSONUri"] or None,
                    "draftRevision": 1 if section_id in doc["drafts"] else 0,
                    "pageIds": [str(page) for page in sections[section_id]["PageIds"]],
                }
                for section_id in doc["in_review"]
                if section_id in sections
            ],
            "checklist": [],
            "rejectReasons": list(queue.get("rejectReasons") or []),
            "history": [],
        }

    def op_claimReviewItem(self, role, args):
        item = self._item(args)
        doc = self.docs[args["objectKey"]]
        if self.clock < doc["active_at"]:
            raise ValueError("INVALID_STATE: the item is HOLDING")
        listed = self.queues[item["queue"]]["eligibleAnnotators"]
        if role == "annotator" and self.caller not in listed:
            raise PermissionError("not listed on the queue")
        item["state"] = "CLAIMED"
        item["version"] += 1
        doc["owner"] = self.caller
        return item

    def op_saveReviewDraft(self, role, args):
        assert self._item(args)["state"] == "CLAIMED"
        doc = self.docs[args["objectKey"]]
        assert args["sectionId"] in doc["in_review"], "the section isn't in review"
        doc["drafts"][args["sectionId"]] = json.loads(args["data"])
        return {"revision": 1, "changedFieldCount": 1}

    def op_updateDocumentSections(self, role, args):
        doc = self.docs[args["objectKey"]]
        refused = {"success": False, "processingJobId": None}
        if "regroup_is_refused" in self.defects:
            return {**refused, "message": "REVIEW_UNAVAILABLE: no review queues"}
        if not self._holds(doc):
            return {**refused, "message": "NOT_LEASE_HOLDER: assigned to someone else"}
        incoming = args["sections"]
        before = {section["Id"]: section for section in doc["sections"]}
        pages = sorted(int(page) for section in incoming for page in section["pageIds"])
        if pages != sorted(page for s in doc["sections"] for page in s["PageIds"]):
            return {**refused, "message": "every page belongs to exactly one section"}
        rebuilt = []
        for section in incoming:
            # The content of a section that is kept follows it: its result, its
            # class unless one is given, and whether it is excluded. A section
            # the reviewer added has no result yet.
            kept = before.get(section["sectionId"], {"OutputJSONUri": ""})
            rebuilt.append(
                {
                    **kept,
                    "Id": section["sectionId"],
                    "PageIds": [int(page) for page in section["pageIds"]],
                    "Class": section.get("classification") or kept["Class"],
                }
            )
        doc["sections"] = sorted(rebuilt, key=lambda section: min(section["PageIds"]))
        return {
            "success": True,
            "message": "Page grouping saved",
            "processingJobId": None,
        }

    def op_getReviewCompletionPlan(self, role, args):
        self._item(args)
        doc = self.docs[args["objectKey"]]
        open_to_members = "plan_is_open_to_queue_members" in self.defects
        if role != "admin" and not self._holds(doc) and not open_to_members:
            raise PermissionError("NOT_LEASE_HOLDER: you don't hold this item's claim")
        extracted = self._regrouped(doc)
        if "plan_ignores_the_regroup" in self.defects:
            extracted = []
        complete = planned.planned_run(
            steps.EXTRACTION if extracted else steps.APPLY_REVIEW_CHANGES,
            steps.PIPELINE,
            kind=RunKind.REVIEW_COMPLETED,
            sections=extracted,
            causes={
                section_id: restart.CAUSE_PAGES_CHANGED
                if section_id in doc["layout"]
                else restart.CAUSE_ADDED
                for section_id in extracted
            },
        )
        send_back = planned.planned_run(
            steps.EXTRACTION
            if "send_back_starts_at_extraction" in self.defects
            else restart.SEND_BACK_START,
            steps.PIPELINE,
            kind=RunKind.SENT_BACK,
        )
        answer = {
            "complete": complete.model_dump(by_alias=True),
            "sendBack": send_back.model_dump(by_alias=True),
        }
        if "plan_has_one_outcome" in self.defects:
            del answer["sendBack"]
        if "plan_starts_at_applying_the_review" in self.defects:
            # It names the sections to extract again, and does not start there.
            answer["complete"]["startStep"] = steps.APPLY_REVIEW_CHANGES
        if "plan_leaves_out_the_new_section" in self.defects:
            answer["complete"]["sections"] = [
                section
                for section in answer["complete"]["sections"]
                if section["cause"] != restart.CAUSE_ADDED
            ]
        return answer

    def op_getProcessChangesPlan(self, role, args):
        if role not in ("admin", "reviewer"):
            raise PermissionError("Unauthorized")
        if self.docs[args["objectKey"]]["item"] is not None:
            raise ValueError("this document is in a review queue")
        start = steps.POLICY_RULE_VALIDATION
        if "process_changes_plan_extracts" in self.defects:
            start = steps.EXTRACTION
        return planned.planned_run(
            start, steps.PIPELINE, kind=RunKind.SAVED_EDITS
        ).model_dump(by_alias=True)

    def _end_review(self, key: str, status: str) -> dict:
        doc = self.docs[key]
        doc["cycle_id"] = doc["item"]["cycleId"]
        doc["item"] = None
        doc["owner"] = None
        doc["drafts"] = {}
        doc["review_status_before"] = doc["review_status"]
        doc["status_lag"] = self.status_lag
        doc["review_status"] = status
        doc["sequence"] += 1
        return doc

    def _completion_states(self, extracted: list[str]) -> list[str]:
        """The run after a completed review: from applying the review, or, when
        sections were regrouped, from extracting them again."""
        if not extracted or "extraction_not_entered" in self.defects:
            return ["CheckRunPlan", "ApplyReviewChangesStep", *POLICY_AND_ROUTING]
        sections = 1 if "hook_runs_once" in self.defects else len(extracted)
        states = [
            "CheckRunPlan",
            "StartAtExtractionForSections",
            *(SECTION_RUN * sections),
            "Pipeline_AssembleDocumentStep",
            "CheckReviewToApply",
        ]
        if "review_is_not_applied_after_extraction" not in self.defects:
            states += ["SelectAssembledDocument", "ApplyReviewChangesStep"]
        return [*states, *POLICY_AND_ROUTING]

    def op_completeReviewItem(self, role, args):
        item = self._item(args)
        assert item["state"] == "CLAIMED"
        key = args["objectKey"]
        doc = self.docs[key]
        extracted = self._regrouped(doc)
        states = self._completion_states(extracted)
        if "completion_reruns_ocr" in self.defects:
            states.insert(1, "OCRStep")
        if "completion_skips_apply_review" in self.defects:
            states[states.index("ApplyReviewChangesStep")] = (
                "StartAtPolicyRuleValidation"
            )
        if "completion_skips_routing" in self.defects:
            states.remove("RoutingStep")
        if "completion_skips_policy" in self.defects:
            states.remove("PolicyClassificationStep")
        if "completion_held_again" in self.defects:
            self._start_run(key, states + HOLD_END, live.HELD)
            item["state"] = "OPEN"
            return {"state": "FINALIZING"}
        if "restart_is_not_recorded" not in self.defects:
            self.history_rows.setdefault((key, item["cycleId"]), []).append(
                (
                    "completed",
                    {
                        "restart_step": steps.EXTRACTION
                        if extracted
                        else steps.APPLY_REVIEW_CHANGES,
                        "restart_sections": list(extracted),
                    },
                )
            )
        ran = [] if "extraction_not_entered" in self.defects else extracted
        self._apply_review(key, doc, ran)
        placements = doc["placements"] + 1
        placed = self._placement(key, doc)
        if placements > MAX_PLACEMENTS:
            placed = None
        if placed is None and self._scenario(key) in self.never_complete:
            placed = {"queue": "default", "rule": live.BUILTIN_CONFIDENCE_RULE}
        self._end_review(key, "complete")
        if placed is None:
            self._start_run(key, states + CONTINUE_END, live.COMPLETED)
        else:
            self._start_run(key, states + HOLD_END, live.HELD)
            self._hold(key, placed, placements)
        return {"state": "FINALIZING"}

    def op_rejectReviewItem(self, role, args):
        assert self._item(args)["state"] == "CLAIMED"
        key = args["objectKey"]
        assert args["reason"] == "Illegible"
        states = ["CheckRunPlan", "SetRejectedStatus", "WorkflowComplete"]
        if "rejection_ends_elsewhere" in self.defects:
            states = ["CheckRunPlan", "WorkflowComplete"]
        if "rejection_schedules_a_task" in self.defects:
            states = ["CheckRunPlan", "RoutingStep", "MarkReviewRejected", *states[1:]]
        self._end_review(key, "rejected")
        self._start_run(key, states, live.REJECTED)
        return {"state": "REJECTED"}

    def op_removeReviewItem(self, role, args):
        if role != "admin":
            raise PermissionError("Admins only")
        self._item(args)
        key = args["objectKey"]
        states = ["CheckRunPlan", "StartAtPolicyRuleValidation", *POLICY_AND_ROUTING]
        if "removal_runs_from_the_beginning" in self.defects:
            states = list(FIRST_RUN)
        if "removal_applies_review" in self.defects:
            states[1] = "ApplyReviewChangesStep"
        self._end_review(key, "complete")
        self._start_run(key, states + CONTINUE_END, live.COMPLETED)
        return {"state": "FINALIZING"}

    def op_sendBackReviewItem(self, role, args):
        item = self._item(args)
        assert item["state"] == "CLAIMED"
        if "startStep" in args:
            # The call names no step: one that does is a bad request.
            raise ValueError("BAD_ARGUMENT: startStep: Extra inputs are not permitted")
        key = args["objectKey"]
        if "send_back_refused" in self.defects:
            return {"objectKey": key, "status": "REFUSED", "reason": "CYCLE_CHANGED"}
        start = restart.SEND_BACK_START
        states = ["CheckRunPlan", "StartAtClassification", "ClassificationStep"]
        if "send_back_starts_at_extraction" in self.defects:
            start, states = steps.EXTRACTION, ["CheckRunPlan", "StartAtExtraction"]
        if "send_back_runs_ocr" in self.defects:
            states.insert(2, "OCRStep")
        states += [
            "ExtractionStep",
            "AssessmentStep",
            "Pipeline_AssembleDocumentStep",
            *POLICY_AND_ROUTING,
        ]
        if "send_back_not_recorded" not in self.defects:
            recorded = start
            if "send_back_records_another_step" in self.defects:
                recorded = steps.ASSESSMENT
            self.history_rows.setdefault((key, item["cycleId"]), []).extend(
                [("claimed", {}), ("sent_back", {"start_step": recorded})]
            )
        doc = self._end_review(key, "withdrawn")
        placed = self._placement(key, doc)
        if placed is None or "send_back_not_held_again" in self.defects:
            self._start_run(key, states + CONTINUE_END, live.COMPLETED)
        else:
            self._start_run(key, states + HOLD_END, live.HELD)
            self._hold(key, placed)
        return {"objectKey": key, "status": "QUEUED", "startStep": start}

    def op_reprocessDocument(self, role, args):
        (key,) = args["objectKeys"]
        assert args["startStep"] == "summarization"
        states = [
            "CheckRunPlan",
            "StartAtPolicyRuleValidation",
            "CheckPolicyRuleValidationEnabled",
            "SkipPolicyRuleValidation",
            "RoutingStep",
            "CheckRoutingDecision",
        ]
        if "reprocess_extracts_again" in self.defects:
            states.insert(1, "ExtractionStep")
        if "reprocess_runs_policy" in self.defects:
            states[3] = "PolicyClassificationStep"
        if "reprocess_skips_routing" in self.defects:
            states.remove("RoutingStep")
        self._start_run(key, states + CONTINUE_END, live.COMPLETED)
        return {
            "queued": 1,
            "results": [
                {"objectKey": key, "status": "QUEUED", "startStep": "summarization"}
            ],
        }

    def op_testRoutingTable(self, role, args):
        doc = self.docs[args["objectKey"]]
        if "routing_test_writes" in self.defects:
            doc["item"]["version"] += 1
        rule = doc["item"]["routedBy"]["ruleId"]
        if "routing_test_wrong_rule" in self.defects:
            rule = "builtin-missing-required"
        return {
            "decision": "continue"
            if "routing_test_says_continue" in self.defects
            else "hold",
            "matchedRuleId": rule,
            "outcomeKind": "queue",
            "outcomeQueue": doc["item"]["queue"],
        }

    def op_convertRoutingDmn(self, role, args):
        if args["direction"] == "to_dmn":
            routing = RoutingConfig.model_validate(json.loads(args["content"]))
            xml = routing_dmn.export_xml(routing, dmn_version=args["dmnVersion"])
            return {"xml": xml, "routing": None}
        routing = routing_dmn.import_xml(args["content"])
        if "dmn_import_drops_a_rule" in self.defects:
            routing.tables[0].rules.pop()
        return {"routing": json.dumps(routing.model_dump()), "xml": None}


# ----------------------------------------------------------------- helpers
def run(stack: FakeStack, state_file: Path, command: str, deps: Any = None):
    args = live.parse_args(
        [
            command,
            "--stack-name",
            "S",
            "--region",
            "us-west-2",
            "--state-file",
            str(state_file),
        ]
    )
    return live.execute(args, deps or stack.deps())


def outcomes(report: Any) -> dict[str, str]:
    return {c.name: c.outcome for c in report.checks}


def failed(report: Any) -> list[str]:
    return sorted(c.name for c in report.checks if c.outcome != live.PASS)


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "scratch" / "state.json"


def function_update(began: datetime, seconds: int = 15) -> list[list[str]]:
    """The nested stack's events for one update of both assembly functions."""
    complete = began + timedelta(seconds=seconds)
    return [
        [name, status, at.isoformat()]
        for name in live.ASSEMBLY_FUNCTIONS
        for status, at in (("UPDATE_IN_PROGRESS", began), ("UPDATE_COMPLETE", complete))
    ]


def upgraded(
    state_file: Path, *, straddle: bool = True, stack: FakeStack | None = None
) -> FakeStack:
    """A stack taken through pre-upgrade, start-in-flight and the update."""
    stack = stack or FakeStack()
    stack.old_definition = True
    stack.paged = False
    assert run(stack, state_file, "pre-upgrade").exit_code() == 0
    assert run(stack, state_file, "start-in-flight").exit_code() == 0
    # The in-flight runs reach their assembly step a minute after their upload: the
    # functions are updated before that, or long after.
    stack.function_events = function_update(
        stack.wall + timedelta(seconds=30 if straddle else 600)
    )
    stack.sleep(900)
    stack.old_definition = False
    stack.paged = True
    return stack


def detail_of(report: Any, name: str) -> str:
    return next(c.detail for c in report.checks if c.name == name)


#: Every check ``reasons-and-restart`` reports on a stack that behaves.
REASONS_AND_RESTART_CHECKS = [
    "scenario-one-lands-in-approvals",
    "scenario-two-lands-in-compliance",
    "lending-lands-in-specialist",
    "scenario-two-reason-is-rendered",
    "scenario-two-lists-the-failed-policy-rule",
    "scenario-two-status-object-has-the-reason",
    "scenario-two-tracking-row-has-the-reason",
    "scenario-two-cli-status-has-the-reason",
    "scenario-two-history-has-the-reason",
    "completion-plan-gives-the-holder-both-plans",
    "completion-plan-is-refused-to-an-annotator-who-does-not-hold-the-item",
    "completion-plan-latency-recorded",
    "send-back-starts-at-classification",
    "send-back-is-recorded-and-the-document-returns-to-a-queue",
    "process-changes-plan-starts-at-policy-rule-validation",
    "regrouped-review-plan-extracts-both-sections",
    "regrouped-review-extracts-then-applies-the-review",
    "regrouped-review-records-where-the-run-starts",
    "regrouped-review-every-section-has-a-result",
    "regrouped-review-extracted-sections-are-not-confirmed",
    "regrouped-review-keeps-the-correction",
    "regrouped-review-is-held-again-or-completes",
]
#: Every check ``scenario-rounds`` reports: the rounds of the showcase's README.
SCENARIO_ROUNDS_CHECKS = [
    "scenario-one-round-1-lands-in-approvals",
    "scenario-one-round-1-reason-names-the-problem",
    "scenario-one-round-2-completes",
    "scenario-two-round-1-lands-in-compliance",
    "scenario-two-round-1-reason-names-the-problem",
    "scenario-two-round-2-lands-in-default",
    "scenario-two-round-2-reason-names-the-problem",
    "scenario-two-round-3-completes",
    "scenario-three-round-1-lands-in-approvals",
    "scenario-three-round-1-reason-names-the-problem",
    "scenario-three-round-2-lands-in-approvals",
    "scenario-three-round-2-reason-names-the-problem",
    "scenario-three-round-3-lands-in-default",
    "scenario-three-round-3-reason-names-the-problem",
    "scenario-three-round-4-completes",
]
#: Every check ``seed-demo`` reports: where each document it leaves is waiting.
SEED_DEMO_CHECKS = [
    "demo-lending-waits-in-specialist",
    "demo-statement-waits-in-compliance",
    "demo-invoice-waits-in-approvals",
    "demo-w2-waits-in-default",
    "demo-scenario-one-waits-in-approvals",
    "demo-scenario-two-waits-in-compliance",
    "demo-scenario-three-waits-in-approvals",
]


# ------------------------------------------------------- the whole sequence
def test_the_whole_sequence_passes_on_a_stack_that_behaves(state_file):
    stack = upgraded(state_file)

    post = run(stack, state_file, "post-upgrade")
    assert failed(post) == []
    assert set(outcomes(post)) >= {
        "held-document-completes-from-apply-review-changes",
        "held-document-status-object",
        "held-document-reject-schedules-no-task",
        "held-document-removed-starts-at-policy-rule-validation",
        "in-flight-document-1",
        "in-flight-document-2",
        "in-flight-document-3",
        "unchanged-profile-held-by-built-in-rule",
    }

    scenarios = run(stack, state_file, "scenarios")
    assert failed(scenarios) == []
    assert set(outcomes(scenarios)) >= {
        "lending-lands-in-specialist",
        "statement-lands-in-compliance",
        "invoice-lands-in-approvals",
        "w2-lands-in-default",
        "invoice-has-no-confidence-reason",
        "test-studio-document-is-not-routed",
        "routing-test-names-the-rule-and-changes-nothing",
        "completed-review-starts-at-apply-review-changes",
        "completed-review-is-not-held-again",
        "rejected-review-schedules-no-task",
        "send-back-starts-at-classification",
        "send-back-is-recorded-and-the-document-returns-to-a-queue",
        "reprocess-from-summarization-skips-to-routing",
        "author-cannot-change-routing-rules",
        "author-can-save-another-section",
        "missing-queue-falls-back-to-default",
        "dmn-1.5-round-trip-keeps-the-table",
        "dmn-1.3-round-trip-keeps-the-table",
        "latency-recorded",
    }

    reasons = run(stack, state_file, "reasons-and-restart")
    assert failed(reasons) == []
    assert sorted(outcomes(reasons)) == sorted(REASONS_AND_RESTART_CHECKS)

    rounds = run(stack, state_file, "scenario-rounds")
    assert failed(rounds) == []
    assert sorted(outcomes(rounds)) == sorted(SCENARIO_ROUNDS_CHECKS)

    seeded = run(stack, state_file, "seed-demo")
    assert failed(seeded) == []
    assert sorted(outcomes(seeded)) == sorted(SEED_DEMO_CHECKS)

    cleaned = run(stack, state_file, "cleanup")
    assert failed(cleaned) == []
    assert outcomes(cleaned)["demo-documents-wait-in-their-queues"] == live.PASS

    # What is left is the demo: three queues, the profile, the four showcase
    # samples and the three scenario invoices, each waiting.
    assert sorted(stack.queues) == ["approvals", "compliance", "default", "specialist"]
    assert sorted(stack.profiles) == [live.SHOWCASE_PROFILE]
    assert sorted(stack.docs) == sorted(
        f"{live.DEMO_PREFIX}/{path.name}"
        for path in (
            *(s.path for s in live.SHOWCASE_SAMPLES),
            *(s.path for s in live.SCENARIOS),
        )
    )
    assert {d["status"] for d in stack.docs.values()} == {live.HELD}
    assert {d["placements"] for d in stack.docs.values()} == {1}
    assert stack.users == {}
    assert stack.queues["default"]["eligibleAnnotators"] == [EXISTING]
    for temporary in (ANNOTATOR, BYSTANDER):
        assert all(
            temporary not in q["eligibleAnnotators"] for q in stack.queues.values()
        )
    assert stack.auth_enabled == stack.auth_restored > 0


def test_the_state_file_never_holds_a_secret_an_account_id_or_an_arn(state_file):
    stack = upgraded(state_file)
    for command in (
        "capture-switch",
        "post-upgrade",
        "check-switch",
        "scenarios",
        "reasons-and-restart",
        "scenario-rounds",
        "seed-demo",
    ):
        run(stack, state_file, command)
        text = state_file.read_text()
        assert stack.secrets, "the run signed nobody in"
        for secret in stack.secrets:
            assert secret not in text
        assert ACCOUNT not in text and "arn:" not in text
        # The addresses it does hold are the temporary users', which are not real.
        assert set(re.findall(r"@[\w.]+", text)) == {"@example.invalid"}
    state = json.loads(state_file.read_text())
    assert state["held"][0]["execution"].endswith("-1")


def test_a_run_on_its_own_removes_the_queues_and_the_profile_it_created(state_file):
    stack = FakeStack()

    assert failed(run(stack, state_file, "scenarios")) == []
    assert sorted(stack.queues) == ["approvals", "compliance", "default", "specialist"]

    assert failed(run(stack, state_file, "cleanup")) == []
    assert sorted(stack.queues) == ["default"]
    assert stack.queues["default"]["eligibleAnnotators"] == [EXISTING]
    assert stack.profiles == {} and stack.docs == {} and stack.users == {}
    state = json.loads(state_file.read_text())
    assert (state["documents"], state["users"], state["queues_created"]) == ([], {}, [])


def test_a_queue_that_was_there_before_is_neither_deleted_nor_left_changed(state_file):
    stack = FakeStack()
    stack.queues["specialist"] = stack._queue("specialist", "Theirs", [EXISTING])
    stack.queues["specialist"]["checklist"] = ["Signed"]

    report = run(stack, state_file, "scenarios")
    assert outcomes(report)["missing-queue-falls-back-to-default"] == live.SKIP
    assert report.exit_code() == live.EXIT_INCOMPLETE

    run(stack, state_file, "cleanup")
    assert stack.queues["specialist"]["name"] == "Theirs"
    assert stack.queues["specialist"]["checklist"] == ["Signed"]
    assert stack.queues["specialist"]["eligibleAnnotators"] == [EXISTING]
    assert sorted(stack.queues) == ["default", "specialist"]


def test_a_profile_that_was_there_before_is_uploaded_again_and_not_removed(
    state_file,
):
    stack = FakeStack()
    # Stored with one outcome per routing rule: a shape the routing step does not
    # read, and one a save refuses.
    retired = copy.deepcopy(PRESET)
    for rule in retired["routing"]["tables"][0]["rules"]:
        rule["outcome"] = {"kind": "queue", "queue": PRESET_QUEUES[rule["id"]]}
        del rule["actions"], rule["reason"]
    stack.profiles[live.SHOWCASE_PROFILE] = retired

    assert failed(run(stack, state_file, "scenarios")) == []

    # Replaced by the preset as it is now.
    assert stack.profile_uploads == [live.SHOWCASE_PROFILE]
    assert stack.profiles[live.SHOWCASE_PROFILE] == PRESET

    run(stack, state_file, "cleanup")

    # It was not this check's to remove.
    assert sorted(stack.profiles) == [live.SHOWCASE_PROFILE]


def test_the_checks_own_profile_is_the_preset_as_it_is_now_on_every_run(state_file):
    stack = FakeStack()
    assert failed(run(stack, state_file, "scenarios")) == []
    # The profile the first run made, as an older preset left it.
    stack.profiles[live.SHOWCASE_PROFILE]["routing"] = {"tables": []}

    assert failed(run(stack, state_file, "scenarios")) == []

    assert stack.profile_uploads.count(live.SHOWCASE_PROFILE) == 2
    assert stack.profiles[live.SHOWCASE_PROFILE] == yaml.safe_load(
        live.SHOWCASE_CONFIG.read_text()
    )


# -------------------------------------------------- a defect turns a check red
@pytest.mark.parametrize(
    "defect, red",
    [
        ("completion_reruns_ocr", "held-document-completes-from-apply-review-changes"),
        (
            "completion_skips_apply_review",
            "held-document-completes-from-apply-review-changes",
        ),
        ("status_schema_old", "held-document-status-object"),
        ("rejection_schedules_a_task", "held-document-reject-schedules-no-task"),
        ("rejection_ends_elsewhere", "held-document-reject-schedules-no-task"),
        (
            "removal_applies_review",
            "held-document-removed-starts-at-policy-rule-validation",
        ),
        ("placed_outside_routing", "unchanged-profile-held-by-built-in-rule"),
        (
            "removal_runs_from_the_beginning",
            "held-document-removed-starts-at-policy-rule-validation",
        ),
        ("unchanged_profile_not_held", "unchanged-profile-held-by-built-in-rule"),
    ],
)
def test_a_defect_after_the_upgrade_fails_its_check(state_file, defect, red):
    stack = upgraded(state_file)
    stack.defects.add(defect)

    report = run(stack, state_file, "post-upgrade")

    assert outcomes(report)[red] == live.FAIL
    assert report.exit_code() == live.EXIT_FAIL


@pytest.mark.parametrize(
    "defect, red",
    [
        ("invoice_misrouted", "invoice-lands-in-approvals"),
        ("placed_outside_routing", "w2-lands-in-default"),
        ("studio_bypasses_routing", "test-studio-document-is-not-routed"),
        (
            "completion_skips_apply_review",
            "completed-review-starts-at-apply-review-changes",
        ),
        ("completion_skips_routing", "completed-review-starts-at-apply-review-changes"),
        ("rejection_ends_elsewhere", "rejected-review-schedules-no-task"),
        ("send_back_starts_at_extraction", "send-back-starts-at-classification"),
        ("reprocess_skips_routing", "reprocess-from-summarization-skips-to-routing"),
        ("lending_rule_unnamed", "lending-lands-in-specialist"),
        ("lending_falls_back", "lending-lands-in-specialist"),
        (
            "routing_test_says_continue",
            "routing-test-names-the-rule-and-changes-nothing",
        ),
        ("fallback_keeps_the_missing_queue", "missing-queue-falls-back-to-default"),
        ("fallback_forgets_the_rule", "missing-queue-falls-back-to-default"),
        ("invoice_confidence_reason", "invoice-has-no-confidence-reason"),
        ("studio_held", "test-studio-document-is-not-routed"),
        ("routing_test_writes", "routing-test-names-the-rule-and-changes-nothing"),
        ("routing_test_wrong_rule", "routing-test-names-the-rule-and-changes-nothing"),
        ("completion_reruns_ocr", "completed-review-starts-at-apply-review-changes"),
        ("completion_skips_policy", "completed-review-starts-at-apply-review-changes"),
        ("completion_held_again", "completed-review-is-not-held-again"),
        ("rejection_schedules_a_task", "rejected-review-schedules-no-task"),
        ("send_back_runs_ocr", "send-back-starts-at-classification"),
        (
            "send_back_not_recorded",
            "send-back-is-recorded-and-the-document-returns-to-a-queue",
        ),
        (
            "send_back_records_another_step",
            "send-back-is-recorded-and-the-document-returns-to-a-queue",
        ),
        (
            "send_back_not_held_again",
            "send-back-is-recorded-and-the-document-returns-to-a-queue",
        ),
        ("reprocess_extracts_again", "reprocess-from-summarization-skips-to-routing"),
        ("reprocess_runs_policy", "reprocess-from-summarization-skips-to-routing"),
        ("author_saves_routing", "author-cannot-change-routing-rules"),
        ("author_cannot_save", "author-can-save-another-section"),
        ("fallback_reason_lost", "missing-queue-falls-back-to-default"),
        ("dmn_import_drops_a_rule", "dmn-1.5-round-trip-keeps-the-table"),
    ],
)
def test_a_defect_in_a_scenario_fails_its_check(state_file, defect, red):
    stack = FakeStack()
    stack.defects.add(defect)

    report = run(stack, state_file, "scenarios")

    assert outcomes(report)[red] == live.FAIL
    assert report.exit_code() == live.EXIT_FAIL


def test_a_refused_send_back_is_an_error_and_not_a_pass(state_file):
    stack = FakeStack()
    stack.defects.add("send_back_refused")

    report = run(stack, state_file, "scenarios")

    assert outcomes(report)["send-back-starts-at-classification"] == live.ERROR
    assert "CYCLE_CHANGED" in detail_of(report, "send-back-starts-at-classification")


def test_a_document_that_never_arrives_fails_and_stops_the_checks_that_need_it(
    state_file,
):
    stack = FakeStack()
    stack.stuck.add("invoice-")

    report = run(stack, state_file, "scenarios")

    seen = outcomes(report)
    assert seen["invoice-lands-in-approvals"] == live.FAIL
    # What works on the invoice could not be run: an ERROR, never a PASS.
    assert seen["completed-review-starts-at-apply-review-changes"] == live.ERROR
    assert seen["reprocess-from-summarization-skips-to-routing"] == live.ERROR
    assert seen["w2-lands-in-default"] == live.PASS
    assert report.exit_code() == live.EXIT_FAIL


def test_a_sample_in_the_wrong_queue_stops_the_checks_that_work_on_it(state_file):
    stack = FakeStack()
    stack.defects.add("invoice_misrouted")

    report = run(stack, state_file, "scenarios")

    checks = {c.name: c for c in report.checks}
    for name in (
        "invoice-has-no-confidence-reason",
        "routing-test-names-the-rule-and-changes-nothing",
        "completed-review-starts-at-apply-review-changes",
        "reprocess-from-summarization-skips-to-routing",
    ):
        assert checks[name].outcome == live.ERROR
        assert "not waiting where the check needs it" in checks[name].detail


def test_the_latency_cannot_be_read_without_the_pattern_stack(state_file):
    stack = FakeStack()
    stack.defects.add("no_pattern_stack")

    report = run(stack, state_file, "scenarios")

    checks = {c.name: c for c in report.checks}
    assert checks["latency-recorded"].outcome == live.ERROR
    assert "the stack has no PATTERNSTACK resource" in checks["latency-recorded"].detail


def test_the_nested_stack_id_is_not_written_to_the_state_file(state_file):
    stack = FakeStack()

    assert failed(run(stack, state_file, "scenarios")) == []

    assert "PATTERNSTACK" not in state_file.read_text()


def test_a_slow_routing_step_is_a_finding_and_not_a_failed_check(state_file, capsys):
    stack = FakeStack()
    stack.defects.add("routing_is_slow")

    report = run(stack, state_file, "scenarios")

    assert outcomes(report)["latency-recorded"] == live.PASS
    out = capsys.readouterr().out
    assert "finding: RoutingFunction warm p95 2400 ms is over the 1500 ms target" in out
    # The function with no warm invocation is reported over all of its invocations.
    assert "ApplyReviewChangesFunction (none warm; all invocations): n=7" in out


def test_a_seeded_document_in_the_wrong_queue_fails(state_file):
    stack = FakeStack()
    stack.defects.add("invoice_misrouted")

    report = run(stack, state_file, "seed-demo")

    assert outcomes(report)["demo-invoice-waits-in-approvals"] == live.FAIL
    cleaned = run(stack, state_file, "cleanup")
    assert outcomes(cleaned)["demo-documents-wait-in-their-queues"] == live.FAIL
    # Counted per queue: the invoice is one too many in Default, one short in Approvals.
    assert "default=2 (expected 1)" in detail_of(
        cleaned, "demo-documents-wait-in-their-queues"
    )


def test_post_upgrade_can_be_run_again(state_file):
    stack = upgraded(state_file)
    first = run(stack, state_file, "post-upgrade")
    held = [doc["key"] for doc in json.loads(state_file.read_text())["held"]]

    again = run(stack, state_file, "post-upgrade")

    assert failed(first) == [] and failed(again) == []
    assert outcomes(again) == outcomes(first)
    # No review was ended twice: the run that held each document, and the one after.
    assert [len(stack.docs[key]["runs"]) for key in held] == [2, 2, 2]
    # The new upload is a new document each time.
    uploads = sorted(key for key in stack.docs if "/after-upgrade-" in key)
    assert [key.rsplit("/", 1)[1].split("-W2")[0] for key in uploads] == [
        "after-upgrade-1",
        "after-upgrade-2",
    ]


# --------------------------------------------- the Review Routing switch
def with_showcase(stack: FakeStack) -> FakeStack:
    """A stack that already has the showcase profile, as a seeded stack does."""
    stack._upload_profile(
        "S", "us-west-2", live.SHOWCASE_PROFILE, copy.deepcopy(PRESET)
    )
    stack.profile_uploads.clear()
    return stack


def update_stack(stack: FakeStack) -> None:
    """What a stack update does to the configuration: the default profile is saved
    again, which cuts a revision, and its oldest revision is pruned."""
    default = stack.revisions["default"]
    del default[min(default)]
    if "update_switches_review_routing_on" in stack.defects:
        stack.default_config["hitl"]["enabled"] = True
    if "update_changes_the_showcase_switch" in stack.defects:
        stack.profiles[live.SHOWCASE_PROFILE]["hitl"]["enabled"] = False
    if "update_writes_a_revision_again" in stack.defects:
        default[min(default)] = "0" * 32
    if "update_writes_a_showcase_revision_again" in stack.defects:
        showcase = stack.revisions[live.SHOWCASE_PROFILE]
        showcase[min(showcase)] = "0" * 32
    stack._cut_revision("default", stack.default_config)


SWITCH_CHECKS = [
    "switch-unchanged-in-default",
    "earlier-revisions-unchanged-in-default",
    "switch-unchanged-in-review-routing-showcase",
    "earlier-revisions-unchanged-in-review-routing-showcase",
]


def test_an_update_that_leaves_the_switch_and_the_revisions_alone_passes(state_file):
    stack = with_showcase(FakeStack())

    captured = run(stack, state_file, "capture-switch")
    assert outcomes(captured) == {"switch-captured": live.PASS}
    assert "default: Review Routing off, 2 revision bodies" in captured.checks[0].detail
    assert (
        "review-routing-showcase: Review Routing on, 1 revision bodies"
        in captured.checks[0].detail
    )
    update_stack(stack)
    report = run(stack, state_file, "check-switch")

    assert failed(report) == [] and report.exit_code() == live.EXIT_PASS
    assert [c.name for c in report.checks] == SWITCH_CHECKS
    assert detail_of(report, "switch-unchanged-in-default") == (
        "Review Routing was off before the update and is off after it"
    )
    # A revision the update pruned, and the one it cut, are not a change.
    assert detail_of(report, "earlier-revisions-unchanged-in-default") == (
        "1 revision bodies compared, 0 written again "
        "(2 captured, 1 no longer retained, 1 new)"
    )
    # Nothing was uploaded: the two subcommands only read.
    assert stack.profile_uploads == [] and stack.docs == {}


def test_what_is_captured_is_whether_the_switch_was_on_and_names(state_file):
    stack = with_showcase(FakeStack())

    run(stack, state_file, "capture-switch")

    switch = json.loads(state_file.read_text())["switch"]
    assert switch["captured_at"] == "2026-01-01T12:00:00+00:00"
    assert switch["profiles"] == {
        "default": {
            "exists": True,
            "enabled": False,
            "revisions": stack.revisions["default"],
        },
        live.SHOWCASE_PROFILE: {
            "exists": True,
            "enabled": True,
            "revisions": stack.revisions[live.SHOWCASE_PROFILE],
        },
    }
    assert sorted(switch["profiles"]["default"]["revisions"]) == [
        "000001.json.gz",
        "000002.json.gz",
    ]


@pytest.mark.parametrize(
    "defect, red",
    [
        ("update_switches_review_routing_on", "switch-unchanged-in-default"),
        (
            "update_changes_the_showcase_switch",
            "switch-unchanged-in-review-routing-showcase",
        ),
        ("update_writes_a_revision_again", "earlier-revisions-unchanged-in-default"),
        (
            "update_writes_a_showcase_revision_again",
            "earlier-revisions-unchanged-in-review-routing-showcase",
        ),
    ],
)
def test_an_update_that_changes_the_switch_or_a_revision_fails(state_file, defect, red):
    stack = with_showcase(FakeStack())
    stack.defects.add(defect)
    run(stack, state_file, "capture-switch")
    update_stack(stack)

    report = run(stack, state_file, "check-switch")

    assert failed(report) == [red]
    assert outcomes(report)[red] == live.FAIL
    assert report.exit_code() == live.EXIT_FAIL


def test_a_default_profile_that_cannot_be_read_is_not_captured(state_file):
    stack = with_showcase(FakeStack())
    stack.defects.add("default_profile_is_unreadable")

    report = run(stack, state_file, "capture-switch")

    assert outcomes(report) == {"switch-captured": live.FAIL}
    assert "default: no such profile" in report.checks[0].detail


def test_the_switch_that_changed_is_named_in_the_detail(state_file, capsys):
    stack = with_showcase(FakeStack())
    stack.defects.add("update_switches_review_routing_on")
    run(stack, state_file, "capture-switch")
    update_stack(stack)

    report = run(stack, state_file, "check-switch")

    assert detail_of(report, "switch-unchanged-in-default") == (
        "Review Routing was off before the update and is on after it"
    )
    # When the capture was taken is printed: one taken after the update would
    # compare the stack with itself.
    assert (
        "comparing with what was captured 2026-01-01T12:00:00+00:00"
        in capsys.readouterr().out
    )


def test_a_profile_that_was_not_there_is_skipped_and_never_passed(state_file):
    stack = FakeStack()
    run(stack, state_file, "capture-switch")
    update_stack(stack)

    report = run(stack, state_file, "check-switch")

    seen = outcomes(report)
    assert [seen[name] for name in SWITCH_CHECKS] == [
        live.PASS,
        live.PASS,
        live.SKIP,
        live.SKIP,
    ]
    assert report.exit_code() == live.EXIT_INCOMPLETE
    assert "did not exist when the switch was captured" in detail_of(
        report, "switch-unchanged-in-review-routing-showcase"
    )


def test_revisions_that_are_all_gone_are_skipped_and_never_passed(state_file):
    stack = with_showcase(FakeStack())
    run(stack, state_file, "capture-switch")
    # Every revision that was captured has been pruned since.
    stack.revisions["default"] = {"000009.json.gz": "f" * 32}

    report = run(stack, state_file, "check-switch")

    assert outcomes(report)["earlier-revisions-unchanged-in-default"] == live.SKIP
    assert outcomes(report)["switch-unchanged-in-default"] == live.PASS
    assert "no revision body captured is there to compare" in detail_of(
        report, "earlier-revisions-unchanged-in-default"
    )


def test_check_switch_without_a_capture_cannot_run(state_file):
    stack = with_showcase(FakeStack())

    report = run(stack, state_file, "check-switch")

    assert outcomes(report) == {"check-switch": live.ERROR}
    assert "run capture-switch before the stack is updated" in report.checks[0].detail


# ------------------------------------------- reasons, plans and restarts
#: A defect of the stack, and the check of ``reasons-and-restart`` it turns red.
REASONS_AND_RESTART_DEFECTS = [
    # Not placed by the routing step at all.
    ("placed_outside_routing", "scenario-one-lands-in-approvals"),
    ("lending_falls_back", "lending-lands-in-specialist"),
    # A wrong routing rule: the invoice is held for its missing payment terms.
    ("net_amount_rule_never_matches", "scenario-two-lands-in-compliance"),
    # A reason that still holds a brace, and one that is not the Admin's text.
    ("reason_keeps_a_placeholder", "scenario-two-reason-is-rendered"),
    ("reason_is_generated", "scenario-two-reason-is-rendered"),
    ("no_failed_policy_rule", "scenario-two-lists-the-failed-policy-rule"),
    (
        "status_object_lacks_the_reason",
        "scenario-two-status-object-has-the-reason",
    ),
    ("tracking_row_lacks_the_reason", "scenario-two-tracking-row-has-the-reason"),
    ("cli_status_lacks_the_reason", "scenario-two-cli-status-has-the-reason"),
    ("history_lacks_the_reason", "scenario-two-history-has-the-reason"),
    ("plan_has_one_outcome", "completion-plan-gives-the-holder-both-plans"),
    (
        "send_back_starts_at_extraction",
        "completion-plan-gives-the-holder-both-plans",
    ),
    (
        "plan_is_open_to_queue_members",
        "completion-plan-is-refused-to-an-annotator-who-does-not-hold-the-item",
    ),
    ("send_back_starts_at_extraction", "send-back-starts-at-classification"),
    ("send_back_runs_ocr", "send-back-starts-at-classification"),
    (
        "send_back_records_another_step",
        "send-back-is-recorded-and-the-document-returns-to-a-queue",
    ),
    (
        "send_back_not_recorded",
        "send-back-is-recorded-and-the-document-returns-to-a-queue",
    ),
    (
        "process_changes_plan_extracts",
        "process-changes-plan-starts-at-policy-rule-validation",
    ),
    ("plan_ignores_the_regroup", "regrouped-review-plan-extracts-both-sections"),
    (
        "plan_starts_at_applying_the_review",
        "regrouped-review-plan-extracts-both-sections",
    ),
    (
        "plan_leaves_out_the_new_section",
        "regrouped-review-plan-extracts-both-sections",
    ),
    # The run extracts a section the plan did not name.
    (
        "plan_leaves_out_the_new_section",
        "regrouped-review-extracts-then-applies-the-review",
    ),
    # Extraction not entered, and the other ways the run can leave its path.
    (
        "extraction_not_entered",
        "regrouped-review-extracts-then-applies-the-review",
    ),
    (
        "review_is_not_applied_after_extraction",
        "regrouped-review-extracts-then-applies-the-review",
    ),
    ("completion_reruns_ocr", "regrouped-review-extracts-then-applies-the-review"),
    ("hook_runs_once", "regrouped-review-extracts-then-applies-the-review"),
    ("restart_is_not_recorded", "regrouped-review-records-where-the-run-starts"),
    (
        "regrouped_section_has_no_result",
        "regrouped-review-every-section-has-a-result",
    ),
    (
        "extracted_sections_are_confirmed",
        "regrouped-review-extracted-sections-are-not-confirmed",
    ),
    ("correction_lost_after_extraction", "regrouped-review-keeps-the-correction"),
    ("placements_are_not_counted", "regrouped-review-is-held-again-or-completes"),
]


@pytest.mark.parametrize("defect, red", REASONS_AND_RESTART_DEFECTS)
def test_a_defect_in_a_reason_a_plan_or_a_restart_fails_its_check(
    state_file, defect, red
):
    stack = FakeStack()
    stack.defects.add(defect)

    report = run(stack, state_file, "reasons-and-restart")

    assert outcomes(report)[red] == live.FAIL
    assert report.exit_code() == live.EXIT_FAIL


def test_every_check_of_reasons_and_restart_has_a_defect_that_turns_it_red():
    red = {name for _, name in REASONS_AND_RESTART_DEFECTS}
    # The latency is a figure that is recorded, with a target it is compared to
    # as a finding: it can be an ERROR, and is never a FAIL.
    recorded = {"completion-plan-latency-recorded"}

    assert red | recorded == set(REASONS_AND_RESTART_CHECKS)
    assert not red & recorded


def test_the_reason_and_the_failed_policy_rule_are_shown_in_the_details(
    state_file, capsys
):
    stack = FakeStack()

    report = run(stack, state_file, "reasons-and-restart")

    reason = detail_of(report, "scenario-two-reason-is-rendered")
    assert "source=admin; names '1250': yes; placeholder left unfilled: no" in reason
    assert "The net amount due 1250 is not the gross total 1200 minus" in reason
    assert NET_AMOUNT_RULE_ID in detail_of(
        report, "scenario-two-lists-the-failed-policy-rule"
    )
    # The explanation derived for the failed policy rule is printed, to be read.
    assert (
        f"failed policy rule {NET_AMOUNT_RULE_ID} (pages [1]): The net amount due "
        "is not the gross total minus the discount."
    ) in capsys.readouterr().out


def test_the_regroup_gives_the_last_page_of_a_section_to_a_new_one(state_file):
    stack = FakeStack()

    report = run(stack, state_file, "reasons-and-restart")

    (doc,) = [d for key, d in stack.docs.items() if "/regroup-1-" in key]
    assert [(s["Id"], s["PageIds"]) for s in doc["sections"]] == [
        ("1", [1]),
        ("5", [2]),
        ("2", [3]),
        ("3", [4, 5, 6]),
        ("4", [7]),
    ]
    # The new section has the class of the one it was taken from, and a result.
    assert doc["sections"][1]["Class"] == doc["sections"][0]["Class"] == "Payslip"
    # The correction was to the first text field, and it is on the new result.
    assert doc["results"]["1"]["inference_result"] == {
        "CurrentGrossPay": 1200.5,
        "EmployeeName": f"Ada Example{live.CORRECTED_SUFFIX}",
    }
    assert (
        "section 1 gave its last page to the new section 5; plan: mode=pipeline "
        "start=extraction"
    ) in detail_of(report, "regrouped-review-plan-extracts-both-sections")
    assert "sections [1 (pages_changed), 5 (added)]" in detail_of(
        report, "regrouped-review-plan-extracts-both-sections"
    )
    # The excluded section has no result, and is not one that lacks it.
    assert doc["sections"][-1]["OutputJSONUri"] == ""
    assert detail_of(report, "regrouped-review-every-section-has-a-result") == (
        "5 section(s), without a result: none; regrouped sections listed: ['1', '5']"
    )
    assert detail_of(report, "regrouped-review-is-held-again-or-completes") == (
        "status=HITL_IN_PROGRESS placement=2"
    )


def test_a_regrouped_review_that_completes_is_not_a_failure(state_file):
    stack = FakeStack()
    # Confirmed although extracted again: the document reads as reviewed and no
    # routing rule holds it. That it completes is not what is wrong with it.
    stack.defects.add("extracted_sections_are_confirmed")

    report = run(stack, state_file, "reasons-and-restart")

    assert failed(report) == ["regrouped-review-extracted-sections-are-not-confirmed"]
    assert detail_of(report, "regrouped-review-is-held-again-or-completes") == (
        "status=COMPLETED"
    )


class TestTheGroupingOfARegroup:
    """``regroup_grouping``: one regroup that changes a section's pages and adds
    a section, whatever the page counts of the document's sections."""

    CLASSES = {"1": "Payslip", "2": "US-drivers-licenses", "3": "Bank-checks"}

    def test_a_section_of_several_pages_gives_its_last_page_to_a_new_one(self):
        layout = {"1": ["1", "2"], "2": ["3"], "3": ["4"]}

        grouping, added, how = live.regroup_grouping(layout, self.CLASSES, "1")

        assert added == "4"
        assert grouping == [
            {"sectionId": "1", "classification": "Payslip", "pageIds": ["1"]},
            {
                "sectionId": "2",
                "classification": "US-drivers-licenses",
                "pageIds": ["3"],
            },
            {"sectionId": "3", "classification": "Bank-checks", "pageIds": ["4"]},
            {"sectionId": "4", "classification": "Payslip", "pageIds": ["2"]},
        ]
        assert how == "section 1 gave its last page to the new section 4"

    def test_a_section_of_one_page_takes_the_next_and_the_one_after_is_renewed(self):
        """The lending package's sections have one page each: section 1 takes
        the page of section 2, and section 3 comes back under a new id."""
        layout = {"1": ["1"], "2": ["2"], "3": ["3"], "4": ["4"]}
        classes = {**self.CLASSES, "4": "W2"}

        grouping, added, how = live.regroup_grouping(layout, classes, "1")

        assert added == "5"
        assert grouping == [
            {"sectionId": "1", "classification": "Payslip", "pageIds": ["1", "2"]},
            {"sectionId": "4", "classification": "W2", "pageIds": ["4"]},
            {"sectionId": "5", "classification": "Bank-checks", "pageIds": ["3"]},
        ]
        # Every page is in exactly one section, as before.
        assert sorted(page for row in grouping for page in row["pageIds"]) == [
            "1",
            "2",
            "3",
            "4",
        ]
        assert how == (
            "section 1 took the pages of section 2, and section 3 was put under "
            "the new id 5"
        )

    def test_the_last_section_takes_from_the_first(self):
        layout = {"1": ["1"], "2": ["2"], "3": ["3"]}

        grouping, added, _ = live.regroup_grouping(layout, self.CLASSES, "3")

        assert added == "4"
        assert grouping == [
            {"sectionId": "3", "classification": "Bank-checks", "pageIds": ["1", "3"]},
            {
                "sectionId": "4",
                "classification": "US-drivers-licenses",
                "pageIds": ["2"],
            },
        ]

    def test_two_sections_of_one_page_cannot_be_regrouped(self):
        layout = {"1": ["1"], "2": ["2"]}

        with pytest.raises(live.CheckAborted, match="too few sections to regroup"):
            live.regroup_grouping(layout, self.CLASSES, "1")


class TestTheStatusTheCommandPrints:
    """``default_document_status``: the SDK's status of one document, printed by
    the conversion and the formatter of ``idp-cli status --format json``."""

    @staticmethod
    def _document(review):
        from idp_sdk.models import DocumentStatus
        from idp_sdk.models.document import ReviewInfo

        return DocumentStatus(
            document_id="in/routing-scenario-two-problems.pdf",
            status="HITL_IN_PROGRESS",
            review=ReviewInfo.from_lookup(review),
            review_status=(review or {}).get("status"),
        )

    def _printed(self, monkeypatch, review):
        import idp_sdk

        asked = []
        document = self._document(review)

        class Client:
            def __init__(self, stack_name, region):
                asked.append((stack_name, region))
                self.document = self

            def get_status(self, key):
                asked.append(key)
                return document

        monkeypatch.setattr(idp_sdk, "IDPClient", Client)
        printed = live.default_document_status(
            "stack", "us-east-1", "in/routing-scenario-two-problems.pdf"
        )
        assert asked == [
            ("stack", "us-east-1"),
            "in/routing-scenario-two-problems.pdf",
        ]
        return printed

    def test_it_holds_the_reason_and_the_failed_policy_rules(self, monkeypatch):
        failure = {
            "ruleId": "scenario_invoice_checks.net_amount_matches",
            "policy": "scenario_invoice_checks",
            "name": "net_amount_matches",
            "reason": "The net amount due is not the gross total minus the discount.",
            "pages": [1],
        }

        printed = self._printed(
            monkeypatch,
            {
                "status": "in_review",
                "queue": "compliance",
                "routedBy": {
                    "ruleId": "scenario-net-amount-check",
                    "reason": "The net amount due 1250 is wrong.",
                    "policyFailures": [failure],
                    "policyFailureCount": 1,
                },
            },
        )

        assert printed["document_id"] == "in/routing-scenario-two-problems.pdf"
        assert printed["review_status"] == "in_review"
        assert printed["routing_reason"] == "The net amount due 1250 is wrong."
        assert printed["policy_failure_count"] == 1
        assert [f["rule_id"] for f in printed["policy_failures"]] == [
            "scenario_invoice_checks.net_amount_matches"
        ]

    def test_a_document_that_was_never_routed_has_no_reason(self, monkeypatch):
        printed = self._printed(monkeypatch, None)

        assert "routing_reason" not in printed
        assert "policy_failures" not in printed

    def test_a_fault_of_the_sdk_means_the_check_could_not_be_run(self, monkeypatch):
        import idp_sdk

        class Client:
            def __init__(self, stack_name, region):
                self.document = self

            def get_status(self, key):
                raise RuntimeError("no such stack")

        monkeypatch.setattr(idp_sdk, "IDPClient", Client)

        with pytest.raises(live.CheckAborted, match="no such stack"):
            live.default_document_status("stack", "us-east-1", "in/a.pdf")


def test_a_regroup_that_is_refused_stops_every_check_that_follows_it(state_file):
    stack = FakeStack()
    stack.defects.add("regroup_is_refused")

    report = run(stack, state_file, "reasons-and-restart")

    checks = {c.name: c for c in report.checks}
    regrouped = [n for n in REASONS_AND_RESTART_CHECKS if n.startswith("regrouped-")]
    assert len(regrouped) == 7
    # Each is reported as a check that could not be run: none is left out, and
    # none passes.
    assert [checks[name].outcome for name in regrouped] == [live.ERROR] * 7
    assert "updateDocumentSections refused the regroup: REVIEW_UNAVAILABLE" in (
        checks["regrouped-review-plan-extracts-both-sections"].detail
    )
    assert "the regrouped review was not completed" in (
        checks["regrouped-review-keeps-the-correction"].detail
    )
    assert sorted(checks) == sorted(REASONS_AND_RESTART_CHECKS)
    # The review was left as it was: claimed, with its draft, and not completed.
    (doc,) = [d for key, d in stack.docs.items() if "/regroup-1-" in key]
    assert doc["item"]["state"] == "CLAIMED" and len(doc["runs"]) == 1


def test_a_send_back_that_names_a_start_step_is_refused(state_file):
    """A call that still sends ``startStep`` gets a bad request, never a pass."""
    stack = FakeStack()
    deps = stack.deps()
    answer = deps.call_body

    def naming_a_step(api_base, field_name, args, token):
        if field_name == "sendBackReviewItem":
            args = {**args, "startStep": "extraction"}
        return answer(api_base, field_name, args, token)

    deps.call_body = naming_a_step

    report = run(stack, state_file, "reasons-and-restart", deps)

    assert outcomes(report)["send-back-starts-at-classification"] == live.ERROR
    assert "sendBackReviewItem as annotator answered 400" in detail_of(
        report, "send-back-starts-at-classification"
    )
    assert "send-back-is-recorded-and-the-document-returns-to-a-queue" not in (
        outcomes(report)
    )


def test_the_send_back_names_no_step(state_file):
    stack = FakeStack()
    deps = stack.deps()
    answer, sent = deps.call_body, []

    def recording(api_base, field_name, args, token):
        if field_name == "sendBackReviewItem":
            sent.append(dict(args))
        return answer(api_base, field_name, args, token)

    deps.call_body = recording

    assert failed(run(stack, state_file, "reasons-and-restart", deps)) == []
    assert failed(run(stack, state_file, "scenarios", deps)) == []

    assert [sorted(args) for args in sent] == [["cycleId", "note", "objectKey"]] * 2


def test_a_second_annotator_who_is_not_on_the_queue_proves_nothing(state_file):
    stack = FakeStack()
    deps = stack.deps()
    answer = deps.call_body

    def not_listing_the_bystander(api_base, field_name, args, token):
        if field_name in ("createReviewQueue", "updateReviewQueue"):
            listed = args["input"].get("eligibleAnnotators") or []
            kept = [email for email in listed if email != BYSTANDER]
            args = {"input": {**args["input"], "eligibleAnnotators": kept}}
        return answer(api_base, field_name, args, token)

    deps.call_body = not_listing_the_bystander

    report = run(stack, state_file, "reasons-and-restart", deps)

    name = "completion-plan-is-refused-to-an-annotator-who-does-not-hold-the-item"
    # Refused for not being on the queue, which anybody is: an ERROR, not a PASS.
    assert outcomes(report)[name] == live.ERROR
    assert "the second Annotator cannot read the item (403)" in detail_of(report, name)


def test_a_document_that_does_not_land_stops_the_checks_that_work_on_it(state_file):
    stack = FakeStack()
    stack.defects.add("net_amount_rule_never_matches")

    report = run(stack, state_file, "reasons-and-restart")

    checks = {c.name: c for c in report.checks}
    for name in REASONS_AND_RESTART_CHECKS:
        if name.startswith("scenario-two-") and "lands" not in name:
            assert checks[name].outcome == live.ERROR, name
            assert "not waiting where the check needs it" in checks[name].detail
    name = "process-changes-plan-starts-at-policy-rule-validation"
    assert checks[name].outcome == live.ERROR
    # What works on the other documents is still checked.
    assert checks["send-back-starts-at-classification"].outcome == live.PASS
    assert checks["regrouped-review-keeps-the-correction"].outcome == live.PASS


def test_an_item_that_is_not_active_yet_is_waited_for(state_file):
    stack = FakeStack()
    # The item is activated, and the tracking row names what placed the document,
    # a moment after the document's status says it is held.
    stack.activation_seconds = 12

    assert failed(run(stack, state_file, "reasons-and-restart")) == []
    assert failed(run(stack, state_file, "scenario-rounds")) == []


# ------------------------------------------------- the plan's latency
def test_the_plan_latency_is_read_for_the_measured_calls_and_no_others(state_file):
    stack = FakeStack()

    report = run(stack, state_file, "reasons-and-restart")

    calls = live.PLAN_LATENCY_CALLS
    assert detail_of(report, "completion-plan-latency-recorded") == (
        f"getReviewCompletionPlan (warm invocations): n={calls} of {calls} calls "
        "p50=95 ms p95=310 ms (target p95 1000 ms)"
    )
    # The function served other calls, and other calls for a plan, before and
    # after: the period that was read holds none of them.
    plans = [field for _, field in stack.resolver_calls].count(
        "getReviewCompletionPlan"
    )
    assert len(stack.resolver_calls) > plans > calls


def test_the_plan_latency_waits_for_the_log_events(state_file):
    stack = FakeStack()
    stack.log_lag = 2

    report = run(stack, state_file, "reasons-and-restart")

    assert outcomes(report)["completion-plan-latency-recorded"] == live.PASS
    assert f"n={live.PLAN_LATENCY_CALLS} of" in detail_of(
        report, "completion-plan-latency-recorded"
    )


def test_a_log_that_never_shows_the_calls_is_an_error_and_not_a_pass(state_file):
    stack = FakeStack()
    stack.log_lag = 10**6

    report = run(stack, state_file, "reasons-and-restart")

    assert outcomes(report)["completion-plan-latency-recorded"] == live.ERROR
    assert "logged no invocation" in detail_of(
        report, "completion-plan-latency-recorded"
    )


def test_a_slow_completion_plan_is_a_finding_and_not_a_failed_check(state_file, capsys):
    stack = FakeStack()
    stack.defects.add("completion_plan_is_slow")

    report = run(stack, state_file, "reasons-and-restart")

    assert outcomes(report)["completion-plan-latency-recorded"] == live.PASS
    assert (
        "finding: getReviewCompletionPlan warm p95 1800 ms is over the 1000 ms target"
        in capsys.readouterr().out
    )


def test_the_plan_latency_cannot_be_read_without_the_api_stack(state_file):
    stack = FakeStack()
    stack.defects.add("no_api_stack")

    report = run(stack, state_file, "reasons-and-restart")

    assert outcomes(report)["completion-plan-latency-recorded"] == live.ERROR
    assert "the stack has no APIRESOLVERSTACK resource" in detail_of(
        report, "completion-plan-latency-recorded"
    )
    assert "APIRESOLVERSTACK" not in state_file.read_text()


def test_the_review_queues_log_group_is_the_one_the_api_template_declares():
    api = (REPO / "nested/api-resolvers/template.yaml").read_text()
    resources = dict(re.findall(r"^  (\w+):\n((?:(?:    .*)?\n)+)", api, re.M))
    group = resources[live.PLAN_RESOLVER_LOG_GROUP]
    assert group.startswith("    Type: AWS::Logs::LogGroup\n")
    # It has no name of its own, so its name is its physical id.
    assert "LogGroupName" not in group
    # The function that logs to it is the one that serves the review queues.
    function = resources["ReviewQueuesResolverFunction"]
    assert f"LogGroup: !Ref {live.PLAN_RESOLVER_LOG_GROUP}" in function
    assert "CodeUri: ./src/lambda/review_queues_resolver" in function
    root = (REPO / "template.yaml").read_text()
    assert re.search(
        rf"^  {live.API_STACK}:\n(?:    .*\n)*?    Type: AWS::CloudFormation::Stack",
        root,
        re.M,
    )


# ------------------------------------------------------ scenario rounds
@pytest.mark.parametrize(
    "defect, red",
    [
        # A wrong routing rule that names the right queue.
        ("vendor_rule_is_tried_first", "scenario-three-round-1-lands-in-approvals"),
        ("net_amount_rule_never_matches", "scenario-two-round-1-lands-in-compliance"),
        ("reason_keeps_a_placeholder", "scenario-one-round-1-reason-names-the-problem"),
        ("reason_is_generated", "scenario-three-round-2-reason-names-the-problem"),
        ("no_failed_policy_rule", "scenario-two-round-1-reason-names-the-problem"),
        ("correction_is_not_applied", "scenario-one-round-2-completes"),
        (
            "reviewed_documents_are_not_placed_again",
            "scenario-two-round-2-lands-in-default",
        ),
        ("placements_are_not_counted", "scenario-three-round-4-completes"),
    ],
)
def test_a_defect_in_a_round_fails_its_check(state_file, defect, red):
    stack = FakeStack()
    stack.defects.add(defect)

    report = run(stack, state_file, "scenario-rounds")

    assert outcomes(report)[red] == live.FAIL
    assert report.exit_code() == live.EXIT_FAIL


@pytest.mark.parametrize("red", SCENARIO_ROUNDS_CHECKS)
def test_every_check_of_a_round_has_a_defect_that_turns_it_red(state_file, red):
    scenario, number, kind = re.fullmatch(
        r"scenario-(\w+)-round-(\d)-(lands|reason|completes).*", red
    ).groups()
    stack = FakeStack()
    if kind == "lands":
        # Placed by another routing rule, into the queue the README names.
        stack.misrouted_rounds.add((scenario, int(number)))
    elif kind == "reason":
        stack.unrendered_rounds.add((scenario, int(number)))
    else:
        stack.never_complete.add(scenario)

    report = run(stack, state_file, "scenario-rounds")

    assert outcomes(report)[red] == live.FAIL
    # The rounds before it were played and passed.
    before = SCENARIO_ROUNDS_CHECKS[: SCENARIO_ROUNDS_CHECKS.index(red)]
    earlier = [name for name in before if name.startswith(f"scenario-{scenario}-")]
    assert [outcomes(report)[name] for name in earlier] == [live.PASS] * len(earlier)


@pytest.mark.parametrize("scenario", ["one", "two", "three"])
def test_a_seeded_scenario_invoice_placed_by_another_routing_rule_fails(
    state_file, scenario
):
    stack = FakeStack()
    stack.misrouted_rounds.add((scenario, 1))

    report = run(stack, state_file, "seed-demo")

    (red,) = [name for name in SEED_DEMO_CHECKS if f"-scenario-{scenario}-" in name]
    assert failed(report) == [red]
    assert outcomes(report)[red] == live.FAIL


def test_the_reviewer_makes_the_corrections_of_the_readme(state_file):
    stack = FakeStack()

    report = run(stack, state_file, "scenario-rounds")

    assert failed(report) == []
    values = {
        key.rsplit("-problem", 1)[0].rsplit("-", 1)[-1]: doc["results"]["1"][
            "inference_result"
        ]
        for key, doc in stack.docs.items()
    }
    assert values["one"]["Vendor"] == live.REGISTERED_VENDOR
    assert (values["two"]["NetAmountDue"], values["two"]["PaymentTerms"]) == (
        1100,
        "Net 30",
    )
    assert (values["three"]["Vendor"], values["three"]["PaymentTerms"]) == (
        live.REGISTERED_VENDOR,
        "Net 30",
    )
    # One run to place each document, and one after each review.
    runs = {
        key.rsplit("-problem", 1)[0].rsplit("-", 1)[-1]: len(doc["runs"])
        for key, doc in stack.docs.items()
    }
    assert runs == {"one": 2, "two": 3, "three": 4}
    assert detail_of(report, "scenario-three-round-4-completes") == (
        "status=COMPLETED after 3 review(s); placements counted on the last review "
        "item: 3"
    )


def test_the_rounds_after_one_that_went_wrong_are_not_played_and_never_pass(
    state_file,
):
    stack = FakeStack()
    stack.defects.add("vendor_rule_is_tried_first")

    report = run(stack, state_file, "scenario-rounds")

    seen = outcomes(report)
    assert seen["scenario-three-round-1-lands-in-approvals"] == live.FAIL
    later = [
        name
        for name in SCENARIO_ROUNDS_CHECKS
        if name.startswith("scenario-three-") and "-round-1-" not in name
    ]
    assert len(later) == 5
    assert [seen[name] for name in later] == [live.ERROR] * 5
    assert "round 1 did not end as the showcase says" in detail_of(report, later[0])
    # No correction was made for a problem the document was not held for.
    (doc,) = [d for key, d in stack.docs.items() if "three-problems" in key]
    assert len(doc["runs"]) == 1 and doc["item"]["state"] == "OPEN"
    # The other documents are played to the end.
    assert seen["scenario-one-round-2-completes"] == live.PASS
    assert seen["scenario-two-round-3-completes"] == live.PASS


def test_a_review_that_cannot_be_completed_is_reported_and_stops_the_rounds(
    state_file,
):
    stack = FakeStack()
    # The item is never activated, so it cannot be claimed.
    stack.activation_seconds = 10**9

    report = run(stack, state_file, "scenario-rounds")

    seen = outcomes(report)
    assert seen["scenario-one-round-1-lands-in-approvals"] == live.PASS
    assert seen["scenario-one-round-1-review-is-completed"] == live.ERROR
    assert "claimReviewItem as annotator answered 400" in detail_of(
        report, "scenario-one-round-1-review-is-completed"
    )
    assert seen["scenario-one-round-2-completes"] == live.ERROR
    assert report.exit_code() == live.EXIT_INCOMPLETE


def test_a_rerun_uses_new_documents(state_file):
    stack = FakeStack()

    assert failed(run(stack, state_file, "scenario-rounds")) == []
    assert failed(run(stack, state_file, "scenario-rounds")) == []

    names = sorted(key.rsplit("/", 1)[1] for key in stack.docs)
    assert names == [
        "rounds-one-1-routing-scenario-one-problem.pdf",
        "rounds-one-2-routing-scenario-one-problem.pdf",
        "rounds-three-1-routing-scenario-three-problems.pdf",
        "rounds-three-2-routing-scenario-three-problems.pdf",
        "rounds-two-1-routing-scenario-two-problems.pdf",
        "rounds-two-2-routing-scenario-two-problems.pdf",
    ]


def test_a_document_uploaded_again_is_not_read_from_the_run_before(state_file):
    stack = FakeStack()
    assert failed(run(stack, state_file, "seed-demo")) == []
    # The upload is not picked up: the row still shows the document waiting where
    # the run before left it.
    stack.not_picked_up.add(live.REGROUP_SAMPLE.path.name)

    again = run(stack, state_file, "seed-demo")

    seen = outcomes(again)
    assert seen["demo-lending-waits-in-specialist"] == live.FAIL
    assert "status=None" in detail_of(again, "demo-lending-waits-in-specialist")
    # A document whose upload started a run is read from that run.
    assert seen["demo-w2-waits-in-default"] == live.PASS


def test_the_scenarios_are_the_ones_the_preset_and_the_samples_describe():
    readme = (live.SHOWCASE_CONFIG.parent / "README.md").read_text()
    (invoice,) = [
        schema
        for schema in PRESET["classes"]
        if schema["x-aws-idp-document-type"] == live.SCENARIO_CLASS
    ]
    (policy,) = [
        schema
        for schema in PRESET["policy_classes"]
        if schema["x-aws-idp-policy-type"] == live.SCENARIO_POLICY
    ]
    assert live.NET_AMOUNT_POLICY_RULE in policy["rule_properties"]
    assert live.REGISTERED_VENDOR in APPROVED_VENDORS

    for scenario in live.SCENARIOS:
        assert scenario.path.is_file(), scenario.path
        assert scenario.path.name in TRUTH
        # A scenario invoice gets its class from its file name.
        assert re.fullmatch(
            invoice["x-aws-idp-document-name-regex"], scenario.path.name
        )
        assert f"`samples/{scenario.path.relative_to(REPO / 'samples')}`" in readme
        *held, last = scenario.rounds
        # The last round is the one in which no routing rule matches.
        assert last == live.Round()
        assert 1 <= len(held) <= MAX_PLACEMENTS
        for number, expected in enumerate(held, start=1):
            assert PRESET_QUEUES[expected.rule] == expected.queue
            assert expected.reason_has
            for name, _ in expected.correction:
                assert name in invoice["properties"], name
            # The row of the README's table of rounds.
            assert (
                f"| `{scenario.path.name}` | {number} | `{expected.rule}` | "
                f"`{expected.queue}` | `{expected.reason_has}` |"
            ) in readme
    # One, two and three problems.
    assert [len(scenario.rounds) - 1 for scenario in live.SCENARIOS] == [1, 2, 3]
    assert live.REGROUP_SAMPLE.path.is_file()


def test_the_names_the_script_copies_are_the_librarys():
    assert live.REVISION_PREFIX == revisions.REVISION_S3_PREFIX
    assert live.REVIEW_RECORD_KEY == review_models.REVIEW_KEY
    assert live.REASON_SOURCE_ADMIN == routing_reason.SOURCE_ADMIN
    assert (
        live.STEP_CLASSIFICATION,
        live.STEP_EXTRACTION,
        live.STEP_APPLY_REVIEW,
        live.STEP_POLICY_RULE_VALIDATION,
    ) == (
        steps.CLASSIFICATION,
        steps.EXTRACTION,
        steps.APPLY_REVIEW_CHANGES,
        steps.POLICY_RULE_VALIDATION,
    )
    # A send-back starts at classification.
    assert restart.SEND_BACK_START == live.STEP_CLASSIFICATION
    assert live.PLANNED_STATES == {
        planned.PLANNED_RUN,
        planned.PLANNED_SKIPPED,
        planned.PLANNED_NOT_ENABLED,
    }
    assert live.PLANNED_SKIPPED == planned.PLANNED_SKIPPED
    assert (live.CAUSE_ADDED, live.CAUSE_PAGES_CHANGED) == (
        restart.CAUSE_ADDED,
        restart.CAUSE_PAGES_CHANGED,
    )
    # The stack output the revision bodies are listed from.
    root = (REPO / "template.yaml").read_text()
    assert f"\n  {live.OUTPUT_KEYS['config_bucket']}:\n" in root


def _sent_back_plan() -> dict:
    return planned.planned_run(
        steps.CLASSIFICATION, steps.PIPELINE, kind=RunKind.SENT_BACK
    ).model_dump(by_alias=True)


def test_a_planned_run_as_the_library_writes_it_has_no_problem():
    plan = _sent_back_plan()

    assert live.plan_problems(plan) == []
    assert live.plan_text(plan).startswith(
        "mode=pipeline start=classification steps [beginning=skipped, "
        "classification=run, "
    )


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda plan: plan.pop("mode"), "no mode"),
        (lambda plan: plan.pop("startStep"), "no startStep"),
        (lambda plan: plan.update(steps=[]), "no steps"),
        (lambda plan: plan.pop("sections"), "no sections list"),
        (
            lambda plan: plan["steps"][2].update(state="done"),
            "step extraction has state 'done'",
        ),
        (
            lambda plan: plan["steps"][2].pop("label"),
            "a step without an id or a label",
        ),
        (
            lambda plan: plan.update(startStep="ocr"),
            "the start step 'ocr' is not a step it runs",
        ),
        (
            lambda plan: plan["steps"][1].update(state="skipped"),
            "the start step 'classification' is not a step it runs",
        ),
    ],
)
def test_a_planned_run_that_is_malformed_is_named(change, problem):
    plan = _sent_back_plan()
    change(plan)

    assert problem in live.plan_problems(plan)


def test_something_that_is_not_a_planned_run_is_a_problem():
    assert live.plan_problems(None) == ["not an object"]
    assert live.plan_problems([]) == ["not an object"]


def test_a_dynamodb_value_is_read_as_plain_data():
    value = {
        "reason": "The net amount due 1250 is not the gross total.",
        "policyFailureCount": 2,
        "score": 0.5,
        "escalated": False,
        "note": None,
        "pages": [1, 2],
        "policyFailures": [{"ruleId": "a.b", "pages": [3]}],
    }

    assert live._plain(ddb(value)) == value
    assert live._plain(None) is None


def test_the_text_field_to_correct_is_one_whose_name_is_its_path():
    result = {
        "inference_result": {
            "Amount": 250,
            "Line Items": "two",
            "Empty": " ",
            "Address": {"City": "Seattle"},
            "Payee": "Ada Example",
        }
    }

    assert live.string_field(result) == "Payee"
    assert live.string_field({"inference_result": {"Amount": 250}}) == ""
    assert live.string_field({}) == ""


def test_a_new_section_is_numbered_one_past_every_other():
    assert live.next_section_id(["1", "2", "7"]) == "8"
    assert live.next_section_id(["cover", "2"]) == "3"
    assert live.next_section_id([]) == "1"


# -------------------------------------------------------- idp-cli status
def test_the_status_is_printed_by_the_code_of_the_command():
    """``default_document_status`` prints with the conversion and the formatter
    the command uses, so what it returns is what the command prints for a batch
    of that one document. Both must exist under these names."""
    from idp_cli import cli, display

    assert callable(cli._batch_status_to_display_dicts)
    assert callable(display.format_status_json)
    status = cli.cli.commands["status"]
    (output_format,) = [p for p in status.params if "--format" in p.opts]
    assert "json" in output_format.type.choices


# ------------------------------------------------------------ in-flight runs
def test_runs_that_called_no_updated_function_are_skipped_never_passed(state_file):
    stack = upgraded(state_file, straddle=False)

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.SKIP] * 3
    assert report.exit_code() == live.EXIT_INCOMPLETE
    detail = next(c.detail for c in report.checks if c.name == "in-flight-document-1")
    assert "it called no updated function" in detail
    # Both times are printed in UTC, whatever zone the stack reported them in.
    assert re.findall(r"T[\d:]+([+-]\d\d:\d\d)", detail) == ["+00:00", "+00:00"]


def test_an_in_flight_document_with_no_alert_is_skipped_not_failed(state_file):
    # The assessment is a model call: now and then the sample raises no alert, and
    # a document that needs no review is rightly not held.
    stack = FakeStack()
    stack.no_alerts.add("in-flight-1-2")
    stack = upgraded(state_file, stack=stack)

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [
        live.PASS,
        live.SKIP,
        live.PASS,
    ]
    detail = next(c.detail for c in report.checks if c.name == "in-flight-document-2")
    assert "the document needed no review" in detail


def test_an_in_flight_document_the_update_let_through_fails(state_file):
    stack = FakeStack()
    stack.defects.add("in_flight_run_not_held")
    stack = upgraded(state_file, stack=stack)

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.FAIL] * 3
    detail = next(c.detail for c in report.checks if c.name == "in-flight-document-1")
    assert "status=COMPLETED, review reasons counted: 1" in detail


def test_a_second_batch_started_during_the_update_is_kept_with_the_first(state_file):
    stack = upgraded(state_file)
    first = json.loads(state_file.read_text())
    # Started once the update had begun: these run on the new definition.
    assert run(stack, state_file, "start-in-flight").exit_code() == 0

    state = json.loads(state_file.read_text())
    assert len(state["in_flight"]) == 6 and len(set(state["in_flight"])) == 6
    assert state["in_flight"][:3] == first["in_flight"]
    # The update is still the one after the FIRST batch.
    assert state["in_flight_started_at"] == first["in_flight_started_at"]

    report = run(stack, state_file, "post-upgrade")
    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in range(1, 7)] == [live.PASS] * 6
    details = {c.name: c.detail for c in report.checks}
    assert "ran on the old definition" in details["in-flight-document-1"]
    assert "ran on the new definition" in details["in-flight-document-6"]


def test_a_status_object_that_lags_the_tracking_row_is_waited_for(state_file):
    stack = upgraded(state_file)
    stack.status_lag = 3

    report = run(stack, state_file, "post-upgrade")

    assert outcomes(report)["held-document-status-object"] == live.PASS


def test_a_status_object_that_never_changes_fails_after_the_wait(state_file):
    stack = upgraded(state_file)
    stack.status_lag = 10**6
    before = stack.clock

    report = run(stack, state_file, "post-upgrade")

    assert outcomes(report)["held-document-status-object"] == live.FAIL
    assert stack.clock - before >= live.STATUS_OBJECT_WAIT_SECONDS


def test_post_upgrade_without_three_held_documents_cannot_run(state_file):
    stack = FakeStack()

    report = run(stack, state_file, "post-upgrade")

    assert outcomes(report) == {"post-upgrade": live.ERROR}


H = live.History
NEW_HELD = H(("RoutingStep", "MarkReviewHold"))
NEW_THROUGH = H(("RoutingStep", "SummarizationStep"))


def minute(n: int | None) -> datetime | None:
    return None if n is None else datetime(2026, 1, 1, 12, n, tzinfo=timezone.utc)


def old_run(assembled: int | None) -> Any:
    """A run on the old definition whose assembly step was entered at that minute."""
    if assembled is None:
        return H(("CheckHITLRequired",))
    return H(
        ("Pipeline_ProcessResultsStep", "CheckHITLRequired"),
        (),
        (minute(assembled), minute(assembled)),
    )


@pytest.mark.parametrize(
    "history, status, reasons, updated, outcome",
    [
        # New definition: held by the routing step, or not.
        (NEW_HELD, "HITL_IN_PROGRESS", 1, None, live.PASS),
        (NEW_THROUGH, "COMPLETED", 1, None, live.FAIL),
        (NEW_HELD, "FAILED", 1, None, live.FAIL),
        # It needed no review, so it shows nothing; an unknown count is no excuse.
        (NEW_THROUGH, "COMPLETED", 0, None, live.SKIP),
        (NEW_THROUGH, "COMPLETED", None, None, live.FAIL),
        (NEW_THROUGH, "FAILED", 0, None, live.FAIL),
        # Old definition, assembled by the updated function.
        (old_run(5), "HITL_IN_PROGRESS", 1, (1, 2), live.PASS),
        (old_run(5), "HITL_IN_PROGRESS", 1, (1, 5), live.PASS),
        (old_run(5), "COMPLETED", 1, (1, 2), live.FAIL),
        (old_run(5), "COMPLETED", 0, (1, 2), live.SKIP),
        # Old definition, assembled before the functions changed: the release's own
        # code decided, whatever it decided.
        (old_run(5), "HITL_IN_PROGRESS", 1, (6, 7), live.SKIP),
        (old_run(5), "COMPLETED", 1, (6, 7), live.SKIP),
        # Assembled while the functions were being updated: a document let through
        # is a failure under either code; a hold cannot be credited to the new one.
        (old_run(5), "COMPLETED", 1, (5, 7), live.FAIL),
        (old_run(5), "HITL_IN_PROGRESS", 1, (5, 7), live.SKIP),
        (old_run(6), "HITL_IN_PROGRESS", 1, (5, 7), live.SKIP),
        # Old definition, and a time this check needs is not known.
        (old_run(None), "HITL_IN_PROGRESS", 1, (1, 2), live.SKIP),
        (old_run(5), "HITL_IN_PROGRESS", 1, None, live.SKIP),
        (old_run(5), "COMPLETED", 1, None, live.SKIP),
        # A run this check cannot place.
        (H(("OCRStep",)), "RUNNING", 1, (1, 2), live.ERROR),
    ],
)
def test_the_in_flight_verdict(history, status, reasons, updated, outcome):
    window = None if updated is None else (minute(updated[0]), minute(updated[1]))

    assert live.in_flight_verdict(history, status, reasons, window)[0] == outcome


def test_an_update_before_the_documents_were_started_is_not_the_one(state_file):
    stack = upgraded(state_file, straddle=False)
    began = live.parse_time(stack.function_events[0][2])
    # Taken for the update, it would put every assembly step after it.
    stack.function_events += function_update(began - timedelta(days=3))

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.SKIP] * 3


def test_a_redeploy_later_on_is_not_the_one(state_file):
    stack = upgraded(state_file)
    began = live.parse_time(stack.function_events[0][2])
    # Taken for the update, or for its end, it would put every assembly step
    # before the functions were ready.
    stack.function_events = [
        *function_update(began + timedelta(hours=2)),
        *stack.function_events,
    ]

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.PASS] * 3


def test_a_completion_from_before_the_update_does_not_end_it(state_file):
    stack = upgraded(state_file)
    began = live.parse_time(stack.function_events[0][2])
    # An update that was finishing as the documents were started, then the update
    # this check is about, which is ready only after the assembly steps ran.
    stack.function_events = [
        *(
            [name, "UPDATE_COMPLETE", (began - timedelta(seconds=20)).isoformat()]
            for name in live.ASSEMBLY_FUNCTIONS
        ),
        *function_update(began, seconds=300),
    ]

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.SKIP] * 3
    detail = next(c.detail for c in report.checks if c.name == "in-flight-document-1")
    assert "which code held the document is not known" in detail


def test_an_update_that_began_before_the_documents_has_no_known_start(state_file):
    stack = upgraded(state_file)
    # Only the end of the update falls after the documents were started.
    stack.function_events = [
        event for event in stack.function_events if event[1] == "UPDATE_COMPLETE"
    ]

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.SKIP] * 3


def test_a_stack_without_the_pattern_stack_skips_the_in_flight_runs(state_file):
    stack = upgraded(state_file)
    stack.defects.add("no_pattern_stack")

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.SKIP] * 3


def test_a_missing_required_field_is_a_reason_for_review_too(state_file):
    stack = FakeStack()
    stack.no_alerts.add("in-flight-1-1")
    stack.missing_required.add("in-flight-1-1")
    stack.defects.add("in_flight_run_not_held")
    stack = upgraded(state_file, stack=stack)

    report = run(stack, state_file, "post-upgrade")

    assert outcomes(report)["in-flight-document-1"] == live.FAIL
    detail = next(c.detail for c in report.checks if c.name == "in-flight-document-1")
    assert "review reasons counted: 1" in detail


def test_the_update_is_complete_when_the_last_assembly_function_is(state_file):
    stack = upgraded(state_file)
    began = live.parse_time(stack.function_events[0][2])
    # The second function is ready only after the runs reached their assembly step.
    slow = live.ASSEMBLY_FUNCTIONS[1]
    stack.function_events = [
        [name, status, at]
        if (name, status) != (slow, "UPDATE_COMPLETE")
        else [name, status, (began + timedelta(minutes=5)).isoformat()]
        for name, status, at in stack.function_events
    ]

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.SKIP] * 3
    detail = next(c.detail for c in report.checks if c.name == "in-flight-document-1")
    assert "which code held the document is not known" in detail


def test_a_stack_with_no_function_update_in_its_events_skips_the_in_flight_runs(
    state_file,
):
    stack = upgraded(state_file)
    stack.function_events = []

    report = run(stack, state_file, "post-upgrade")

    seen = outcomes(report)
    assert [seen[f"in-flight-document-{n}"] for n in (1, 2, 3)] == [live.SKIP] * 3


def test_a_row_that_counts_no_reasons_does_not_excuse_a_document_let_through(
    state_file,
):
    stack = FakeStack()
    stack.no_alerts.add("in-flight-1-1")
    stack.defects.add("tracking_row_counts_nothing")
    stack = upgraded(state_file, stack=stack)

    report = run(stack, state_file, "post-upgrade")

    assert outcomes(report)["in-flight-document-1"] == live.FAIL
    detail = next(c.detail for c in report.checks if c.name == "in-flight-document-1")
    assert "review reasons counted: None" in detail


def test_the_assembly_functions_are_the_ones_the_definition_calls():
    pattern = (REPO / "patterns/unified/template.yaml").read_text()
    called = set(re.findall(r"ProcessResultsLambdaArn: !GetAtt (\w+)\.Arn", pattern))
    # Guard the reading itself: the definition's assembly states use these names.
    definition = (REPO / "patterns/unified/statemachine/workflow.asl.json").read_text()
    assert "${PipelineProcessResultsLambdaArn}" in definition
    assert "${BDAProcessResultsLambdaArn}" in definition

    assert called == set(live.ASSEMBLY_FUNCTIONS)
    for name in live.ASSEMBLY_FUNCTIONS:
        assert re.search(
            rf"^  {name}:\n    Type: AWS::Serverless::Function", pattern, re.M
        )
    root = (REPO / "template.yaml").read_text()
    assert re.search(
        rf"^  {live.PATTERN_STACK}:\n(?:    .*\n)*?    Type: AWS::CloudFormation::Stack",
        root,
        re.M,
    )


# ----------------------------------------------------------------- history
def test_a_scheduled_task_is_counted_for_the_state_it_belongs_to():
    events = [
        {"id": 1, "previousEventId": 0, "type": "ExecutionStarted"},
        {
            "id": 2,
            "previousEventId": 0,
            "type": "ChoiceStateEntered",
            "stateEnteredEventDetails": {"name": "CheckRunPlan"},
        },
        {
            "id": 3,
            "previousEventId": 2,
            "type": "MapStateEntered",
            "stateEnteredEventDetails": {"name": "ProcessSections"},
        },
        # Two iterations of the map, interleaved, as the service records them.
        {
            "id": 4,
            "previousEventId": 3,
            "type": "TaskStateEntered",
            "stateEnteredEventDetails": {"name": "ExtractionStep"},
        },
        {
            "id": 5,
            "previousEventId": 3,
            "type": "TaskStateEntered",
            "stateEnteredEventDetails": {"name": "AssessmentStep"},
        },
        {"id": 6, "previousEventId": 4, "type": "LambdaFunctionScheduled"},
        {"id": 7, "previousEventId": 5, "type": "TaskScheduled"},
        {"id": 8, "previousEventId": 6, "type": "LambdaFunctionStarted"},
        {"id": 9, "previousEventId": 8, "type": "LambdaFunctionScheduled"},
    ]

    history = live.analyse_history(reversed(events))

    assert history.entered == (
        "CheckRunPlan",
        "ProcessSections",
        "ExtractionStep",
        "AssessmentStep",
    )
    # The retry (event 9) is the extraction state's, found through the event chain.
    assert history.scheduled == ("ExtractionStep", "AssessmentStep", "ExtractionStep")
    assert history.before("CheckRunPlan", "ExtractionStep")
    assert not history.before("ExtractionStep", "CheckRunPlan")
    assert not history.before("CheckRunPlan", "RoutingStep")
    assert history.touched({"OCRStep", "AssessmentStep"}) == ["AssessmentStep"]


def test_a_run_with_no_task_has_none_scheduled():
    history = live.analyse_history(events_for(["CheckRunPlan", "SetRejectedStatus"]))

    assert history.scheduled == () and history.has("SetRejectedStatus")


def test_every_state_the_check_names_is_in_the_definition():
    text = (REPO / "patterns/unified/statemachine/workflow.asl.json").read_text()
    names = set(re.findall(r'"([A-Za-z_]+)":\s*\{\s*"Type":', text))
    # Guard the reading itself: the definition's first state must be among them.
    assert live.RUN_PLAN_STATE in names

    assert sorted(live.NAMED_STATES - names) == []
    # The state that tells the old definition apart must not exist in this one.
    assert live.OLD_DEFINITION_STATE not in names


def test_the_samples_land_where_the_preset_says():
    # The queue a routing rule sends a document to is its review queue action.
    rules = {
        rule["id"]: [
            action["queue"]
            for action in rule["actions"]
            if action["type"] == "review_queue"
        ]
        for rule in PRESET["routing"]["tables"][0]["rules"]
    }
    # The model reads the same queue out of each routing rule.
    assert {rule: queues[0] for rule, queues in rules.items()} == PRESET_QUEUES

    for sample in live.SHOWCASE_SAMPLES:
        assert sample.path.is_file(), sample.path
        assert rules[sample.rule] == [sample.queue]
        # A built-in rule reads confidence scores, which a model gives and which
        # differ between runs: no sample is expected to land through one.
        assert not sample.rule.startswith("builtin-"), sample.name
    named = {q for queues in rules.values() for q in queues if q != live.DEFAULT_QUEUE}
    assert named == set(live.SHOWCASE_QUEUES)
    readme = (live.SHOWCASE_CONFIG.parent / "README.md").read_text()
    for sample in live.SHOWCASE_SAMPLES[:3]:
        assert f"`samples/{sample.path.relative_to(REPO / 'samples')}`" in readme


# ------------------------------------------------------------------- state
@pytest.mark.parametrize(
    "state",
    [
        {"users": {"admin": "someone@example.com"}},
        {"held": [{"execution": f"arn:aws:states:us-east-1:{ACCOUNT}:execution:w:x"}]},
        {"note": f"account {ACCOUNT}"},
        {"admin_password": "x"},
        {"users": {"token": "x"}},
        {"execution_arn": "x"},
        {"documents": ["eyJhbGciOiJSUzI1NiJ9.payload.signature"]},
    ],
)
def test_a_state_that_holds_something_private_is_refused(tmp_path, state):
    with pytest.raises(ValueError):
        live.save_state(tmp_path / "state.json", state)
    assert not (tmp_path / "state.json").exists()


def test_names_are_accepted_in_a_state(tmp_path):
    state = {
        "users": {"annotator": ANNOTATOR},
        # A message id can end in twelve digits; it is not an account id.
        "held": [{"execution": "W2.pdf-0f8fad5b-d9cb-469f-a165-708677289501"}],
        "in_flight_started_at": "2026-01-01T12:00:00+00:00",
        "documents": ["review-routing-live-abcd1234/held-1-W2_XL_input_clean_1000.pdf"],
    }

    live.save_state(tmp_path / "state.json", state)

    assert json.loads((tmp_path / "state.json").read_text()) == state


def test_a_state_file_of_another_stack_is_refused(state_file):
    stack = FakeStack()
    run(stack, state_file, "scenarios")
    args = live.parse_args(
        [
            "cleanup",
            "--stack-name",
            "OTHER",
            "--region",
            "us-west-2",
            "--state-file",
            str(state_file),
        ]
    )

    report = live.execute(args, stack.deps())

    assert outcomes(report) == {"cleanup": live.ERROR}
    assert "belongs to stack S" in report.checks[0].detail
    assert sorted(stack.queues) == ["approvals", "compliance", "default", "specialist"]


def test_an_account_id_in_a_detail_is_not_printed(capsys):
    report = live.RedactingReport()

    report.add("x", live.ERROR, f"AccessDenied for arn:aws:iam::{ACCOUNT}:role/r")

    assert ACCOUNT not in capsys.readouterr().out
    assert report.checks[0].detail == "AccessDenied for arn:aws:iam::<account>:role/r"


# ------------------------------------------------------------------ parser
def test_every_subcommand_takes_the_stack_the_region_and_the_state_file(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert list(live.SUBCOMMANDS) == [
        "pre-upgrade",
        "start-in-flight",
        "post-upgrade",
        "capture-switch",
        "check-switch",
        "scenarios",
        "reasons-and-restart",
        "scenario-rounds",
        "seed-demo",
        "cleanup",
    ]
    for command in live.SUBCOMMANDS:
        args = live.parse_args(
            [
                command,
                "--stack-name",
                "S",
                "--region",
                "us-east-1",
                "--state-file",
                "s.json",
            ]
        )
        assert (args.command, args.stack_name, args.region, args.state_file) == (
            command,
            "S",
            "us-east-1",
            "s.json",
        )
        for missing in (
            [command, "--region", "us-east-1", "--state-file", "s.json"],
            [command, "--stack-name", "S", "--state-file", "s.json"],
            [command, "--stack-name", "S", "--region", "us-east-1"],
        ):
            with pytest.raises(SystemExit):
                live.parse_args(missing)


def test_the_help_lists_every_subcommand(capsys):
    with pytest.raises(SystemExit) as stopped:
        live.parse_args(["--help"])

    assert stopped.value.code == 0
    listed = capsys.readouterr().out
    for command in live.SUBCOMMANDS:
        assert f"    {command}" in listed, command


def test_the_exit_code_is_zero_only_when_every_check_passed(state_file, capsys):
    stack = FakeStack()
    argv = [
        "--stack-name",
        "S",
        "--region",
        "us-west-2",
        "--state-file",
        str(state_file),
    ]

    assert live.main(["scenarios", *argv], stack.deps()) == 0
    assert "RESULT: PASS (exit 0)" in capsys.readouterr().out

    stack.defects.add("author_saves_routing")
    assert live.main(["scenarios", *argv], stack.deps()) == 1
    assert "RESULT: FAIL (exit 1)" in capsys.readouterr().out
