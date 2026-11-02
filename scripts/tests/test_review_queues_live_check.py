# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The review-queues live check provisions the queue listing an Annotator needs.

An Annotator works only the queues whose Eligible Annotators list names them
(``QueueDefinition.allows``), and the Default queue is seeded with nobody listed. A
live check that only creates an Annotator therefore finds no queue on a fresh stack.
These tests drive the whole check against an in-memory stack whose queue membership
is decided by the real ``QueueDefinition`` model, and whose ``updateReviewQueue``
resets every field the input leaves out: it is the resolver's own ``_queue_input``.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from idp_common.review_queues.queue_defs import QueueDefinition, QueueDefStore

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[1]


def _load():
    sys.path.insert(0, str(SCRIPTS))
    try:
        path = SCRIPTS / "review_queues_live_check.py"
        spec = importlib.util.spec_from_file_location("review_queues_live_check", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["review_queues_live_check"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


live = _load()

OUTPUTS = {
    "HITLTableName": "hitl",
    "S3InputBucketName": "in",
    "S3OutputBucketName": "out",
    "TrackingTableName": "tracking",
}
EXISTING = "existing.annotator@example.com"
CONCURRENT = "added.meanwhile@example.com"


def _load_resolver():
    """The review-queues resolver, for its own ``_queue_input`` and ``_queue_view``.

    The fake API answers ``listReviewQueues`` and ``updateReviewQueue`` with the
    resolver's functions rather than a copy of them, so "an update resets what it
    leaves out" is the resolver's behaviour and not this file's belief about it.
    """
    index = (
        SCRIPTS.parent
        / "nested/api-resolvers/src/lambda/review_queues_resolver/index.py"
    )
    name = "review_queues_resolver_for_live_check_tests"
    with patch.dict(
        os.environ, {"HITL_TABLE_NAME": "", "AWS_DEFAULT_REGION": "us-west-2"}
    ):
        spec = importlib.util.spec_from_file_location(name, index)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


resolver = _load_resolver()
_view = resolver._queue_view


class FakeStack:
    """One stack: users, the Default queue, one document's lifecycle."""

    def __init__(
        self, enabled: bool = True, route: bool = True, open_to_all: bool = False
    ):
        self.default = QueueDefinition(
            id="default",
            name="Main review",
            description="custom description",
            priority=77,
            checklist=["Checked totals"],
            reject_reasons=["Custom reject"],
            skip_reasons=["Custom skip"],
            time_limit_minutes=240,
            eligible_annotators=[EXISTING],
            enabled=enabled,
        )
        self.original = self.default.model_copy(deep=True)
        # Listed first: listReviewQueues orders by priority, so Default need not be.
        self.other = QueueDefinition(id="escalation", priority=90)
        self.other_original = self.other.model_copy(deep=True)
        self.route = route
        # A defective product in which every Annotator works every queue.
        self.open_to_all = open_to_all
        self.unmintable: set[str] = set()
        self.users: dict[str, str] = {}
        self.tokens_minted: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.uploaded_key = ""
        self.claimed = False
        self.completed = False
        self.on_update = None
        self.on_list = None
        #: field name -> statuses to answer with, in order, instead of handling it.
        self.refuse: dict[str, list[int]] = {}
        #: Answer updateReviewQueue with this status after the write has landed.
        self.fail_after_write: int | None = None
        self.fail_upload = False
        self.fail_profile_delete = False
        self.sleeps: list[float] = []
        #: Seconds: one per AWS or API call, plus every sleep. A monotonic clock has
        #: an arbitrary origin, so it does not start at zero.
        self.clock = 86_400.0

    # ---------------------------------------------------------------- deps
    def deps(self):
        return live.Deps(
            resolve_stack=lambda stack, region: {
                "stack": stack,
                "region": region,
                "api_base": "https://api.example.invalid/api",
            },
            aws=self.aws,
            enable_admin_auth=lambda ctx: None,
            restore_auth_flows=lambda ctx: None,
            create_cognito_user=self.create_user,
            delete_cognito_user=lambda ctx, email: self.users.pop(email, None),
            get_id_token=self.token,
            call_body=self.call_body,
            upload_profile=lambda *a: None,
            delete_profile=self.delete_profile,
            sleep=self.sleep,
            monotonic=lambda: self.clock,
            rand=lambda: "abcd1234",
        )

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    def delete_profile(self, *args):
        if self.fail_profile_delete:
            raise RuntimeError("profile delete refused")

    def create_user(self, ctx, email, group, password):
        self.users[email] = group

    def token(self, ctx, email, password):
        self.tokens_minted.append(email)
        if email in self.unmintable:
            return None
        return f"{email}|" + "x" * 120

    # ----------------------------------------------------------------- aws
    def _status(self):
        if not self.uploaded_key or not self.route:
            return None
        base = {
            "cycle_id": "c1",
            "reason_count": 1,
            "machine_output": {"manifest_uri": "s3://out/machine.json"},
        }
        if self.completed:
            return {
                **base,
                "status": "complete",
                "disposition": "complete",
                "reviewer": "r",
                "sequence": 2,
                "final_output": {"manifest_uri": "s3://out/final.json"},
            }
        return {**base, "status": "in_review", "sequence": 1}

    def aws(self, service, op, *args, region=None):
        self.clock += 1
        if (service, op) == ("cloudformation", "describe-stacks"):
            return [{"OutputKey": k, "OutputValue": v} for k, v in OUTPUTS.items()]
        if (service, op) == ("s3api", "put-object"):
            if self.fail_upload:
                raise RuntimeError("put-object refused")
            self.uploaded_key = args[args.index("--key") + 1]
            return {}
        if (service, op) == ("s3", "cp"):
            uri = args[0]
            if uri.endswith(live.STATUS_SEGMENT):
                return self._status()
            if uri == "s3://out/section.json":
                return {"inference_result": {"employer": "Acme"}}
            return {"manifest": uri}
        if (service, op) == ("dynamodb", "get-item"):
            if not self._status():
                return {}
            state = "COMPLETED" if self.completed else "HITL_IN_PROGRESS"
            return {"Item": {"ObjectStatus": {"S": state}}}
        if (service, op) == ("dynamodb", "query"):
            return {"Items": []}
        return {}

    # ----------------------------------------------------------------- API
    def call_body(self, api_base, field_name, args, token):
        self.clock += 1
        caller = token.split("|", 1)[0]
        group = self.users.get(caller)
        self.calls.append((field_name, group))
        if self.refuse.get(field_name):
            status = self.refuse[field_name].pop(0)
            return status, json.dumps({"errors": [{"message": "refused"}]})
        handler = getattr(self, "op_" + field_name)
        body = json.dumps(handler(caller, group, args))
        if field_name == "updateReviewQueue" and self.fail_after_write:
            status, self.fail_after_write = self.fail_after_write, None
            return status, json.dumps({"errors": [{"message": "timed out"}]})
        return 200, body

    def _member(self, caller, group):
        return group == "Admin" or (
            group == "Annotator" and (self.open_to_all or self.default.allows(caller))
        )

    def op_listReviewQueues(self, caller, group, args):
        assert group == "Admin", "listReviewQueues is Admin-only"
        views = [_view(self.other), _view(self.default)]
        if self.on_list:
            hook, self.on_list = self.on_list, None
            hook()
        return views

    def op_updateReviewQueue(self, caller, group, args):
        assert group == "Admin", "updateReviewQueue is Admin-only"
        if args["input"]["id"] == self.other.id:
            self.other = resolver._queue_input(args["input"], self.other)
            return _view(self.other)
        self.default = resolver._queue_input(args["input"], self.default)
        if self.on_update:
            hook, self.on_update = self.on_update, None
            hook()
        return _view(self.default)

    def op_getMyReviewQueues(self, caller, group, args):
        member = self._member(caller, group)
        listed = member and self._status() is not None and not self.claimed
        items = [{"objectKey": self.uploaded_key}] if listed else []
        return {
            "queues": [{"name": "default"}] if member else [],
            "items": {"items": items},
        }

    def op_claimReviewItem(self, caller, group, args):
        assert self._member(caller, group)
        self.claimed = True
        return {"state": "CLAIMED"}

    def op_getReviewItem(self, caller, group, args):
        return {
            "sections": [{"sectionId": "1", "resultUri": "s3://out/section.json"}],
            "checklist": ["Checked totals"],
        }

    def op_saveReviewDraft(self, caller, group, args):
        return {"revision": 1, "changedFieldCount": 1}

    def op_completeReviewItem(self, caller, group, args):
        self.completed = True
        return {"state": "FINALIZING"}


def _run(stack: FakeStack) -> tuple[int, Any]:
    args = live.parse_args(["--stack-name", "S", "--region", "us-west-2"])
    report = live.Report()
    live.ReviewQueuesLiveCheck(args, stack.deps(), report).run()
    return report.exit_code(), report


def _outcomes(report: Any) -> dict[str, str]:
    return {c.name: c.outcome for c in report.checks}


ANNOTATOR = "review-live-abcd1234@example.invalid"
ADMIN = "review-live-admin-abcd1234@example.invalid"


def test_a_fresh_stack_passes_on_the_annotator_path():
    stack = FakeStack()

    code, report = _run(stack)

    assert code == live.EXIT_PASS, report.checks
    outcomes = _outcomes(report)
    assert outcomes["api-unlisted-annotator-sees-no-queue"] == live.PASS
    assert outcomes["admin-lists-annotator-on-default-queue"] == live.PASS
    assert outcomes["api-my-queues-lists-item"] == live.PASS
    # Every review step is the Annotator's; the Admin only edits the queue.
    review_ops = {
        "getMyReviewQueues",
        "claimReviewItem",
        "getReviewItem",
        "saveReviewDraft",
        "completeReviewItem",
    }
    assert {g for op, g in stack.calls if op in review_ops} == {"Annotator"}
    assert {op for op, g in stack.calls if g == "Admin"} == {
        "listReviewQueues",
        "updateReviewQueue",
    }


def test_the_default_queue_is_left_exactly_as_found():
    stack = FakeStack()

    _run(stack)

    assert stack.default == stack.original
    assert stack.other == stack.other_original
    assert not stack.users, "both temporary users are deleted"


def test_an_admins_concurrent_edit_survives_cleanup():
    stack = FakeStack()

    def concurrent_edit():
        stack.default = stack.default.model_copy(
            update={
                "eligible_annotators": [*stack.default.eligible_annotators, CONCURRENT]
            }
        )

    stack.on_update = concurrent_edit

    _run(stack)

    assert stack.default.eligible_annotators == [EXISTING, CONCURRENT]


def test_cleanup_mints_a_fresh_admin_token():
    stack = FakeStack()

    _run(stack)

    assert stack.tokens_minted.count(ADMIN) == 2


def test_the_listing_is_removed_when_the_document_never_routes():
    stack = FakeStack(route=False)

    code, report = _run(stack)

    assert code == live.EXIT_FAIL
    assert ANNOTATOR not in stack.default.eligible_annotators
    assert stack.default == stack.original


def test_a_disabled_default_queue_is_reported_and_not_switched_on():
    stack = FakeStack(enabled=False)

    code, report = _run(stack)

    assert code == live.EXIT_INCOMPLETE
    assert "disabled" in report.checks[-1].detail
    assert not any(op == "updateReviewQueue" for op, _ in stack.calls)
    assert stack.default == stack.original


def test_an_annotator_who_sees_a_queue_before_being_listed_fails_the_check():
    stack = FakeStack(open_to_all=True)

    code, report = _run(stack)

    assert code == live.EXIT_FAIL
    assert _outcomes(report)["api-unlisted-annotator-sees-no-queue"] == live.FAIL


@pytest.mark.parametrize("who", [ANNOTATOR, ADMIN])
def test_a_token_that_cannot_be_minted_stops_the_check(who):
    stack = FakeStack()
    stack.unmintable.add(who)

    code, report = _run(stack)

    assert code == live.EXIT_INCOMPLETE
    assert f"could not mint an ID token for {who}" in report.checks[-1].detail
    assert stack.default == stack.original


def test_an_admins_change_to_settings_an_update_keeps_is_not_undone():
    # The check's read can come from the resolver's 30-second cache: an Admin's
    # change landing after it must survive the check's write.
    stack = FakeStack()

    def admin_disables():
        stack.default = stack.default.model_copy(
            update={"enabled": False, "skip_reasons": ["Changed meanwhile"]}
        )

    stack.on_list = admin_disables

    _run(stack)

    assert stack.default.enabled is False
    assert stack.default.skip_reasons == ["Changed meanwhile"]


def test_a_listing_that_drops_existing_annotators_fails_the_check():
    stack = FakeStack()

    def wipe():
        stack.default = stack.default.model_copy(
            update={"eligible_annotators": [ANNOTATOR]}
        )

    stack.on_update = wipe

    code, report = _run(stack)

    assert code == live.EXIT_FAIL
    assert _outcomes(report)["admin-lists-annotator-on-default-queue"] == live.FAIL
    assert not stack.uploaded_key, "no document is sent to a queue nobody can see"


@pytest.mark.parametrize("status", [400, 403, 404])
def test_an_admin_call_that_is_refused_stops_the_check(status):
    stack = FakeStack()
    stack.refuse["listReviewQueues"] = [status]

    code, report = _run(stack)

    assert code == live.EXIT_INCOMPLETE
    assert f"listReviewQueues as Admin answered {status}" in report.checks[-1].detail
    assert not any(op == "updateReviewQueue" for op, _ in stack.calls)


def test_an_update_that_fails_after_writing_is_still_cleaned_up():
    stack = FakeStack()
    stack.fail_after_write = 502

    code, report = _run(stack)

    assert code == live.EXIT_INCOMPLETE
    assert stack.default == stack.original


@pytest.mark.parametrize("how", ["token", "list"])
def test_a_cleanup_that_cannot_unlist_the_annotator_is_a_failure(how):
    stack = FakeStack()
    real_token, real_list = stack.token, stack.op_listReviewQueues

    def token(ctx, email, password):
        # The run's Admin token is minted once; cleanup mints the second.
        if how == "token" and email == ADMIN and ADMIN in stack.tokens_minted:
            stack.tokens_minted.append(email)
            return None
        return real_token(ctx, email, password)

    def op_list(caller, group, args):
        # The run reads the queues once; refuse the cleanup's read.
        if how == "list":
            stack.refuse["listReviewQueues"] = [502]
        return real_list(caller, group, args)

    stack.token = token
    stack.op_listReviewQueues = op_list

    code, report = _run(stack)

    assert code == live.EXIT_FAIL
    assert _outcomes(report)["cleanup-default-queue-listing"] == live.FAIL
    assert ANNOTATOR in report.checks[-1].detail
    assert ANNOTATOR in stack.default.eligible_annotators


def test_cleanup_waits_out_a_cached_read_that_predates_the_listing():
    # An early stop sends the run to cleanup within seconds of the add, and the
    # cleanup's read can come from a resolver container whose cache is older.
    # This one serves the pre-add view until its cache expires after the write.
    stack = FakeStack()
    stack.fail_upload = True
    ttl = _queue_cache_ttl()
    before_add = _view(stack.default)
    real_list, real_update = stack.op_listReviewQueues, stack.op_updateReviewQueue
    written = {"at": None}

    def op_update(caller, group, args):
        stack.clock += 5  # the request's latency before the write lands
        result = real_update(caller, group, args)
        if written["at"] is None:
            written["at"] = stack.clock
        return result

    def op_list(caller, group, args):
        if written["at"] is not None and stack.clock < written["at"] + ttl:
            return [_view(stack.other), before_add]
        return real_list(caller, group, args)

    stack.op_updateReviewQueue = op_update
    stack.op_listReviewQueues = op_list

    _run(stack)

    assert stack.default == stack.original


def _queue_cache_ttl() -> float:
    ttl = inspect.signature(QueueDefStore.__init__).parameters["ttl"].default
    assert isinstance(ttl, float) and ttl > 0
    return ttl


def test_the_cleanup_wait_outlasts_the_resolvers_queue_cache():
    assert live.QUEUE_CACHE_SECONDS > _queue_cache_ttl()


def test_an_annotator_already_gone_long_after_the_listing_is_left_alone():
    stack = FakeStack()

    real_complete = stack.op_completeReviewItem

    def complete_then_admin_unlists(caller, group, args):
        stack.clock += 600  # a document's processing, at the least
        stack.default = stack.default.model_copy(
            update={"eligible_annotators": [EXISTING]}
        )
        return real_complete(caller, group, args)

    stack.op_completeReviewItem = complete_then_admin_unlists

    code, _ = _run(stack)

    assert code == live.EXIT_PASS
    assert [op for op, _ in stack.calls].count("listReviewQueues") == 2
    assert stack.default == stack.original


def test_another_cleanup_step_failing_does_not_blame_the_queue_listing():
    stack = FakeStack()
    stack.fail_profile_delete = True

    _, report = _run(stack)

    assert "cleanup-default-queue-listing" not in _outcomes(report)
