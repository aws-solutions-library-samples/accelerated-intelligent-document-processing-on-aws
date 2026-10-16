#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Live end-to-end check of the review queues (``make stacktest-review-queues``).

NOTE: needs reworking. The flow below self-claims the item (``claimReviewItem``)
and uses a per-profile ``default`` queue; queues are now system-wide and an Admin
assigns queue items to Annotators, so steps 2 and 4 no longer match the product.

Routes a real document into a review queue on a deployed stack, reviews it
through the REST API with a real Annotator ID token, and waits for the
finalize run. It proves what the offline suites cannot: that ProcessResults,
the two-phase state machine, the WorkflowTracker, the review API and the
finalize run agree on the real tables, buckets and executions.

WHAT IT DOES
------------
  1. Resolves the stack and its ``HITLTableName`` output. No output (an
     older or headless stack) -> SKIP.
  2. Uploads a temporary profile ``review-live-check-<rand>`` with HITL and
     review queues on and ``hitl.confidence_threshold: 0.99`` (so the sample
     routes), and creates a temporary Annotator who is a member of its
     ``default`` queue by email.
  3. Uploads the sample document under that profile and waits for
     ``review/status.json`` = ``in_review`` and tracking ``HITL_IN_PROGRESS``.
  4. With the reviewer's token: ``getMyReviewQueues`` lists the item,
     ``claimReviewItem``, ``saveReviewDraft`` (one field changed),
     ``completeReviewItem``.
  5. Waits for status ``complete`` and tracking ``COMPLETED``, then checks the
     phase-1 machine manifest is unchanged, the final manifest exists and the
     status object's ``sequence`` is above 1.
  6. ``--check-events``: a temporary EventBridge rule on the status object key,
     targeting a temporary SQS queue; expects two events (in review, complete).
  7. Always removes the document's queue rows and tracking row, the input and
     output objects, the profile, the user, the rule and the queue, and
     restores the app client's auth flows.

OUTCOMES AND EXIT CODES
-----------------------
Every check is PASS, FAIL, ERROR (could not be run) or SKIP (precondition
absent). A SKIP is never a PASS. Exit 0 only when every check passed; 1 when any
check failed; 2 when nothing failed but a check errored or was skipped.

USAGE
-----
  AWS_PROFILE=default python3 scripts/review_queues_live_check.py --stack-name IDP1 --region us-west-2

Requires: awscli v2 on PATH; credentials with Cognito admin, CloudFormation
read, S3 read/write on the input and output buckets, DynamoDB read/write on the
review queue and tracking tables, and (``--check-events``) EventBridge and SQS
create/delete (the deploy-account creds are enough). Processing one document
costs a few Bedrock calls.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rbac_common  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
SKIP = "SKIP"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCOMPLETE = 2

APP_GROUP = "Annotator"
SAMPLE = (
    Path(__file__).resolve().parents[1]
    / "samples"
    / "w2"
    / "W2_XL_input_clean_1000.pdf"
)
STATUS_SEGMENT = "review/status.json"

POLL_SECONDS = 15.0
ROUTE_TIMEOUT_SECONDS = 15 * 60
FINALIZE_TIMEOUT_SECONDS = 15 * 60
EVENT_WAIT_SECONDS = 120

OUTPUT_KEYS = {
    "review_table": "HITLTableName",
    "input_bucket": "S3InputBucketName",
    "output_bucket": "S3OutputBucketName",
    "tracking_table": "TrackingTableName",
}


@dataclass
class Check:
    name: str
    outcome: str
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, outcome: str, detail: str) -> None:
        self.checks.append(Check(name, outcome, detail))
        print(f"  [{outcome:5s}] {name}: {detail}")

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.add(name, PASS if ok else FAIL, detail)
        return ok

    def exit_code(self) -> int:
        return exit_code([c.outcome for c in self.checks])


def exit_code(outcomes: list[str]) -> int:
    """0 only when there is at least one check and every one passed."""
    if FAIL in outcomes:
        return EXIT_FAIL
    if not outcomes or any(o != PASS for o in outcomes):
        return EXIT_INCOMPLETE
    return EXIT_PASS


class CheckAborted(Exception):
    """A step could not run; the remaining steps would prove nothing."""


class CheckTimedOut(CheckAborted):
    """A wait ran out; reported as FAIL, never as a pass."""


def default_call_body(api_base, field_name, args, token):
    """POST /op/<field> and return ``(status, body_text)`` (the RBAC harness's)."""
    from test_api_rbac import call_body

    return call_body(api_base, field_name, args, token)


def default_upload_profile(stack: str, region: str, profile: str, config: dict):
    """Upload ``config`` as a configuration profile through the SDK."""
    import yaml

    from idp_sdk import IDPClient

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(config, f)
        path = f.name
    try:
        result = IDPClient(stack_name=stack, region=region).config.upload(
            path, config_profile=profile, validate=False
        )
    finally:
        os.unlink(path)
    if not result.success:
        raise CheckAborted(f"profile upload failed: {result.error}")


def default_delete_profile(stack: str, region: str, profile: str) -> None:
    from idp_sdk import IDPClient

    IDPClient(stack_name=stack, region=region).config.delete(config_profile=profile)


@dataclass
class Deps:
    """Everything that touches AWS or the network, injectable for tests."""

    resolve_stack: Callable[..., dict] = rbac_common.resolve_stack
    aws: Callable[..., Any] = rbac_common.aws
    enable_admin_auth: Callable[[dict], None] = rbac_common.enable_admin_auth
    restore_auth_flows: Callable[[dict], None] = rbac_common.restore_auth_flows
    create_cognito_user: Callable[..., None] = rbac_common.create_cognito_user
    delete_cognito_user: Callable[..., None] = rbac_common.delete_cognito_user
    get_id_token: Callable[..., Any] = rbac_common.get_id_token
    call_body: Callable[..., tuple] = default_call_body
    upload_profile: Callable[..., None] = default_upload_profile
    delete_profile: Callable[..., None] = default_delete_profile
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    rand: Callable[[], str] = lambda: secrets.token_hex(4)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Live review-queue check: route a document, claim, complete, finalize."
    )
    ap.add_argument("--stack-name", required=True)
    ap.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
    )
    ap.add_argument("--sample", default=str(SAMPLE), help="Document to route")
    ap.add_argument(
        "--check-events",
        action="store_true",
        help="Also expect the two status-object EventBridge events",
    )
    args = ap.parse_args(argv)
    if not args.region:
        ap.error("--region is required (or set AWS_REGION / AWS_DEFAULT_REGION)")
    return args


def stack_outputs(deps: Deps, ctx: dict) -> dict:
    """The outputs this check needs, ``""`` for any the stack lacks."""
    rows = deps.aws(
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        ctx["stack"],
        "--query",
        "Stacks[0].Outputs",
        "--output",
        "json",
        region=ctx["region"],
    )
    values = {r.get("OutputKey"): r.get("OutputValue") for r in rows or []}
    return {k: (values.get(v) or "").strip() for k, v in OUTPUT_KEYS.items()}


def _json(body: str) -> Any:
    try:
        return json.loads(body or "")
    except (ValueError, TypeError):
        return None


def change_one_field(result: dict) -> tuple[dict, str]:
    """A copy of a section result with one scalar inference field changed."""
    data = json.loads(json.dumps(result))
    inference = data.get("inference_result")
    if not isinstance(inference, dict):
        raise CheckAborted("the section result has no inference_result object")
    for name, value in inference.items():
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            inference[name] = f"{value} (reviewed)"
            return data, name
    raise CheckAborted("the section result has no scalar field to change")


class ReviewQueuesLiveCheck:
    def __init__(self, args: argparse.Namespace, deps: Deps, report: Report):
        self.args = args
        self.deps = deps
        self.report = report
        tag = deps.rand()
        self.tag = tag
        self.profile = f"review-live-check-{tag}"
        self.user = f"review-live-{tag}@example.invalid"
        self.password = "Aa1!" + secrets.token_urlsafe(24)
        self.object_key = f"review-live-check-{tag}/{Path(args.sample).name}"
        self.rule = f"review-live-check-{tag}"
        self.ctx: dict = {}
        self.out: dict = {}
        self.auth_enabled = False
        self.user_created = False
        self.profile_created = False
        self.uploaded = False
        self.queue_url = ""
        self.rule_created = False

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        try:
            self._run()
        except CheckTimedOut as e:
            self.report.add("review-queues-live-check", FAIL, str(e))
        except CheckAborted as e:
            self.report.add("review-queues-live-check", ERROR, str(e))
        except Exception as e:  # noqa: BLE001 - report, never mask as a pass
            self.report.add(
                "review-queues-live-check",
                ERROR,
                f"unexpected {type(e).__name__}: {e}",
            )
        finally:
            self.cleanup()

    def _run(self) -> None:
        d = self.deps
        self.ctx = d.resolve_stack(self.args.stack_name, self.args.region)
        self.out = stack_outputs(d, self.ctx)
        if not self.out["review_table"]:
            self.report.add(
                "stack-has-review-queues",
                SKIP,
                "no HITLTableName output — an older or headless stack",
            )
            return
        missing = [k for k, v in self.out.items() if not v]
        if missing:
            raise CheckAborted(f"stack outputs missing: {', '.join(missing)}")
        if not Path(self.args.sample).is_file():
            raise CheckAborted(f"sample not found: {self.args.sample}")

        self._create_fixtures()
        if self.args.check_events:
            self._create_event_capture()
        self._upload_document()
        status = self._wait_status("in_review", ROUTE_TIMEOUT_SECONDS)
        self._wait_tracking("HITL_IN_PROGRESS", ROUTE_TIMEOUT_SECONDS)
        machine = (status.get("machine_output") or {}).get("manifest_uri")
        before = self._read_uri(machine) if machine else None
        self.report.check(
            "routed-in-review",
            status.get("status") == "in_review" and bool(machine),
            f"status={status.get('status')} reasons={status.get('reason_count')} "
            f"machine manifest={'yes' if machine else 'no'}",
        )
        self._review_through_api(status)
        final = self._wait_status("complete", FINALIZE_TIMEOUT_SECONDS)
        self._wait_tracking("COMPLETED", FINALIZE_TIMEOUT_SECONDS)
        self._check_final(final, machine, before)
        if self.args.check_events:
            self._check_events()

    # ------------------------------------------------------------- fixtures
    def _create_fixtures(self) -> None:
        d = self.deps
        d.enable_admin_auth(self.ctx)
        self.auth_enabled = True
        self.user_created = True
        d.create_cognito_user(self.ctx, self.user, APP_GROUP, self.password)
        config = {
            "hitl": {
                "enabled": True,
                "confidence_threshold": 0.99,
                "review_queues": {
                    "enabled": True,
                    "default_queue": "default",
                    "escalation_queue": "escalation",
                    "queues": [
                        {
                            "name": "default",
                            "member_groups": [],
                            "member_emails": [self.user],
                        },
                        {
                            "name": "escalation",
                            "member_groups": ["Admin"],
                            "member_emails": [],
                        },
                    ],
                },
            }
        }
        self.profile_created = True
        d.upload_profile(self.ctx["stack"], self.ctx["region"], self.profile, config)

    def _create_event_capture(self) -> None:
        d, region = self.deps, self.ctx["region"]
        created = d.aws("sqs", "create-queue", "--queue-name", self.rule, region=region)
        self.queue_url = (created or {}).get("QueueUrl", "")
        attrs = d.aws(
            "sqs",
            "get-queue-attributes",
            "--queue-url",
            self.queue_url,
            "--attribute-names",
            "QueueArn",
            region=region,
        )
        queue_arn = (attrs or {}).get("Attributes", {}).get("QueueArn", "")
        pattern = {
            "source": ["aws.s3"],
            "detail-type": ["Object Created"],
            "detail": {
                "bucket": {"name": [self.out["output_bucket"]]},
                "object": {"key": [f"{self.object_key}/{STATUS_SEGMENT}"]},
            },
        }
        rule = d.aws(
            "events",
            "put-rule",
            "--name",
            self.rule,
            "--event-pattern",
            json.dumps(pattern),
            region=region,
        )
        self.rule_created = True
        rule_arn = (rule or {}).get("RuleArn", "")
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "events.amazonaws.com"},
                    "Action": "sqs:SendMessage",
                    "Resource": queue_arn,
                    "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
                }
            ],
        }
        d.aws(
            "sqs",
            "set-queue-attributes",
            "--queue-url",
            self.queue_url,
            "--attributes",
            json.dumps({"Policy": json.dumps(policy)}),
            region=region,
        )
        d.aws(
            "events",
            "put-targets",
            "--rule",
            self.rule,
            "--targets",
            json.dumps([{"Id": "queue", "Arn": queue_arn}]),
            region=region,
        )

    def _upload_document(self) -> None:
        self.uploaded = True
        self.deps.aws(
            "s3api",
            "put-object",
            "--bucket",
            self.out["input_bucket"],
            "--key",
            self.object_key,
            "--body",
            self.args.sample,
            "--metadata",
            f"config-version={self.profile}",
            region=self.ctx["region"],
        )
        print(f"Uploaded s3://{self.out['input_bucket']}/{self.object_key}")

    # ---------------------------------------------------------------- waits
    def _read_uri(self, uri: str) -> Any:
        try:
            return self.deps.aws("s3", "cp", uri, "-", region=self.ctx["region"])
        except RuntimeError:
            return None

    def _status(self) -> dict | None:
        body = self._read_uri(
            f"s3://{self.out['output_bucket']}/{self.object_key}/{STATUS_SEGMENT}"
        )
        return body if isinstance(body, dict) else None

    def _tracking_status(self) -> str | None:
        row = self.deps.aws(
            "dynamodb",
            "get-item",
            "--table-name",
            self.out["tracking_table"],
            "--key",
            json.dumps({"PK": {"S": f"doc#{self.object_key}"}, "SK": {"S": "none"}}),
            "--consistent-read",
            region=self.ctx["region"],
        )
        item = (row or {}).get("Item") or {}
        return (item.get("ObjectStatus") or {}).get("S")

    def _wait(self, label: str, timeout: float, probe: Callable[[], Any]) -> Any:
        deadline = self.deps.monotonic() + timeout
        last = None
        while True:
            value, done = probe()
            last = value
            if done:
                return value
            if self.deps.monotonic() >= deadline:
                raise CheckTimedOut(
                    f"timed out after {int(timeout)}s waiting for {label} "
                    f"(last seen: {last!r:.120})"
                )
            self.deps.sleep(POLL_SECONDS)

    def _wait_status(self, want: str, timeout: float) -> dict:
        def probe():
            status = self._status()
            current = (status or {}).get("status")
            if current == "rejected" and want != "rejected":
                raise CheckAborted("the item was rejected")
            return status, current == want

        return self._wait(f"review status {want}", timeout, probe)

    def _wait_tracking(self, want: str, timeout: float) -> str:
        def probe():
            current = self._tracking_status()
            if current == "FAILED":
                raise CheckAborted("the document failed processing")
            return current, current == want

        return self._wait(f"tracking status {want}", timeout, probe)

    # ------------------------------------------------------------------ API
    def _api(self, field_name: str, args: dict, token: str) -> tuple[int, Any]:
        status, body = self.deps.call_body(
            self.ctx["api_base"], field_name, args, token
        )
        if status in (0, None) or (isinstance(status, int) and status >= 500):
            raise CheckAborted(f"{field_name} did not complete: {status} {body!r:.200}")
        return status, _json(body)

    def _review_through_api(self, status: dict) -> None:
        r = self.report
        token = self.deps.get_id_token(self.ctx, self.user, self.password)
        if not token or len(token) < 100:
            raise CheckAborted(f"could not mint an ID token for {self.user}")
        cycle = status.get("cycle_id")
        item_args = {"objectKey": self.object_key, "cycleId": cycle}

        code, mine = self._api("getMyReviewQueues", {"itemLimit": 50}, token)
        items = ((mine or {}).get("items") or {}).get("items") or []
        listed = any(i.get("objectKey") == self.object_key for i in items)
        if not r.check(
            "api-my-queues-lists-item",
            code == 200 and listed,
            f"status={code} listed={listed} queues={len((mine or {}).get('queues') or [])}",
        ):
            raise CheckAborted("the reviewer cannot see the item; nothing to review")

        code, claimed = self._api("claimReviewItem", item_args, token)
        if not r.check(
            "api-claim",
            code == 200 and (claimed or {}).get("state") == "CLAIMED",
            f"status={code} state={(claimed or {}).get('state')}",
        ):
            raise CheckAborted("claim failed")

        code, detail = self._api("getReviewItem", {"objectKey": self.object_key}, token)
        sections = (detail or {}).get("sections") or []
        if code != 200 or not sections or not sections[0].get("resultUri"):
            raise CheckAborted(
                f"getReviewItem answered {code} without a section result"
            )
        section = sections[0]
        result = self._read_uri(section["resultUri"])
        if not isinstance(result, dict):
            raise CheckAborted("could not read the section result")
        data, changed = change_one_field(result)
        code, draft = self._api(
            "saveReviewDraft",
            {
                **item_args,
                "sectionId": section["sectionId"],
                "data": json.dumps(data),
                "baseRevision": section.get("draftRevision") or 0,
            },
            token,
        )
        r.check(
            "api-save-draft",
            code == 200 and (draft or {}).get("changedFieldCount", 0) >= 1,
            f"status={code} field={changed} revision={(draft or {}).get('revision')} "
            f"changed={(draft or {}).get('changedFieldCount')}",
        )

        checklist = (detail or {}).get("checklist") or []
        args = {**item_args}
        if checklist:
            args["checklist"] = [True] * len(checklist)
        code, done = self._api("completeReviewItem", args, token)
        r.check(
            "api-complete",
            code == 200 and (done or {}).get("state") == "FINALIZING",
            f"status={code} state={(done or {}).get('state')}",
        )

    # --------------------------------------------------------------- checks
    def _check_final(self, final: dict, machine: str | None, before: Any) -> None:
        r = self.report
        r.check(
            "finalized-complete",
            final.get("status") == "complete"
            and final.get("disposition") == "complete",
            f"status={final.get('status')} disposition={final.get('disposition')} "
            f"reviewer={final.get('reviewer')}",
        )
        after = self._read_uri(machine) if machine else None
        r.check(
            "machine-manifest-unchanged",
            before is not None and before == after,
            f"manifest {machine}",
        )
        final_uri = (final.get("final_output") or {}).get("manifest_uri")
        r.check(
            "final-manifest-present",
            bool(final_uri) and self._read_uri(final_uri) is not None,
            f"final manifest {final_uri}",
        )
        r.check(
            "status-sequence-advanced",
            int(final.get("sequence") or 0) > 1,
            f"sequence={final.get('sequence')}",
        )

    def _check_events(self) -> None:
        d = self.deps
        deadline = d.monotonic() + EVENT_WAIT_SECONDS
        seen = 0
        while seen < 2 and d.monotonic() < deadline:
            got = d.aws(
                "sqs",
                "receive-message",
                "--queue-url",
                self.queue_url,
                "--max-number-of-messages",
                "10",
                "--wait-time-seconds",
                "10",
                region=self.ctx["region"],
            )
            seen += len((got or {}).get("Messages") or [])
        self.report.check(
            "status-object-events",
            seen == 2,
            f"{seen} Object Created events for {STATUS_SEGMENT} (expected 2)",
        )

    # -------------------------------------------------------------- cleanup
    def cleanup(self) -> None:
        """Best-effort and complete: each step runs even if an earlier one fails."""
        steps: list[tuple[str, Callable[[], None]]] = [
            ("event rule", self._delete_rule),
            ("event queue", self._delete_queue),
            ("review rows", self._delete_review_rows),
            ("tracking row", self._delete_tracking_row),
            ("documents", self._delete_objects),
            ("profile", self._delete_profile),
            ("user", self._delete_user),
            ("auth flows", self._restore_auth),
        ]
        for label, step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001 - keep cleaning up
                print(f"  cleanup of {label} failed: {type(e).__name__}: {e}")

    def _delete_rule(self) -> None:
        if not self.rule_created:
            return
        region = self.ctx["region"]
        self.deps.aws(
            "events",
            "remove-targets",
            "--rule",
            self.rule,
            "--ids",
            "queue",
            region=region,
        )
        self.deps.aws("events", "delete-rule", "--name", self.rule, region=region)

    def _delete_queue(self) -> None:
        if self.queue_url:
            self.deps.aws(
                "sqs",
                "delete-queue",
                "--queue-url",
                self.queue_url,
                region=self.ctx["region"],
            )

    def _delete_review_rows(self) -> None:
        if not self.uploaded:
            return
        region, table = self.ctx["region"], self.out["review_table"]
        pk = f"DOC#{self.object_key}"
        rows = self.deps.aws(
            "dynamodb",
            "query",
            "--table-name",
            table,
            "--key-condition-expression",
            "PK = :pk",
            "--expression-attribute-values",
            json.dumps({":pk": {"S": pk}}),
            "--projection-expression",
            "PK, SK",
            region=region,
        )
        for row in (rows or {}).get("Items") or []:
            self.deps.aws(
                "dynamodb",
                "delete-item",
                "--table-name",
                table,
                "--key",
                json.dumps({"PK": row["PK"], "SK": row["SK"]}),
                region=region,
            )

    def _delete_tracking_row(self) -> None:
        if not self.uploaded:
            return
        self.deps.aws(
            "dynamodb",
            "delete-item",
            "--table-name",
            self.out["tracking_table"],
            "--key",
            json.dumps({"PK": {"S": f"doc#{self.object_key}"}, "SK": {"S": "none"}}),
            region=self.ctx["region"],
        )

    def _delete_objects(self) -> None:
        if not self.uploaded:
            return
        region = self.ctx["region"]
        self.deps.aws(
            "s3",
            "rm",
            f"s3://{self.out['input_bucket']}/{self.object_key}",
            region=region,
        )
        self.deps.aws(
            "s3",
            "rm",
            f"s3://{self.out['output_bucket']}/{self.object_key}/",
            "--recursive",
            region=region,
        )

    def _delete_profile(self) -> None:
        if self.profile_created:
            self.deps.delete_profile(
                self.ctx["stack"], self.ctx["region"], self.profile
            )

    def _delete_user(self) -> None:
        if self.user_created:
            self.deps.delete_cognito_user(self.ctx, self.user)

    def _restore_auth(self) -> None:
        if self.auth_enabled:
            self.deps.restore_auth_flows(self.ctx)


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    args = parse_args(argv)
    report = Report()
    print(f"Review queues live check: stack {args.stack_name} ({args.region})")
    ReviewQueuesLiveCheck(args, deps or Deps(), report).run()
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
