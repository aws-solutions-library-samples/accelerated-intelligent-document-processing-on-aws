# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``getReviewCompletionPlan``: what finishing a review runs, before it is finished.

The handler checks the caller's group, validates the two arguments and asks
``ReviewQueueService.completion_plan``. The service is the real one here, over a
moto review table, so what is pinned is who gets an answer through the handler
(the item's holder, and an Admin while the item is open or assigned), how each
refusal leaves it (403, 400 or 404) and the shape of the answer. What the plan
says for each kind of document is tested with the service, in
``lib/idp_common_pkg/tests/unit/review_queues/test_completion_restart.py``.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pytest
from _harness import event, load
from moto import mock_aws

from idp_common.api_adapter import ResourceNotFound
from idp_common.models import Document, Section
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo
from idp_common.run_plan.restart import layout_of

pytestmark = pytest.mark.unit

FIELD = "getReviewCompletionPlan"
REGION = "us-east-1"
TABLE = "hitl"
KEY = "claims/packet.pdf"
CYCLE = "0123456789abcdef01234"
ARGS = {"objectKey": KEY, "cycleId": CYCLE}
NOW = datetime(2026, 11, 16, 9, 0, 0, tzinfo=timezone.utc)

#: The reviewer who holds the item, as the handler keys them: by their email.
HOLDER = "rev@example.com"
#: The configuration the item is pinned to has summarization switched off.
CONFIG = {"summarization": {"enabled": False}}


def _section(section_id, pages, *, result=True):
    uri = f"s3://out/{KEY}/sections/{section_id}/result.json"
    return Section(
        section_id=section_id,
        classification="Invoice",
        page_ids=list(pages),
        extraction_result_uri=uri if result else None,
    )


def _document(*sections):
    document = Document(id=KEY, input_key=KEY)
    document.sections.extend(sections)
    return document


def _placed():
    """The document as it was placed for review: two sections, each with a result."""
    return _document(_section("1", ["1", "2"]), _section("2", ["3"]))


def _regrouped():
    """The reviewer moved page 2 out of section 1 into a new section 3."""
    return _document(
        _section("1", ["1"]),
        _section("2", ["3"]),
        _section("3", ["2"], result=False),
    )


def _stored():
    """Every item in the review table, for a before-and-after comparison."""
    items = boto3.resource("dynamodb", region_name=REGION).Table(TABLE).scan()["Items"]
    return sorted(json.dumps(item, sort_keys=True, default=str) for item in items)


@pytest.fixture
def module(monkeypatch):
    """The handler over the real service, a moto review table and one document."""
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        store = ReviewStore(TABLE, region=REGION)
        store.queue_defs.put(QueueDefinition(id="claims", priority=70), create=True)

        m = load()
        # A caller who is not an Admin has their configuration scope looked up;
        # these callers have none unless a case gives them one.
        m.scope = None
        monkeypatch.setattr(
            m, "resolve_allowed_config_versions", lambda *a, **kw: m.scope
        )
        m.store = store
        m.document = _placed()
        m.loads = []

        def document_loader(key):
            m.loads.append(key)
            return m.document

        m._SERVICE = ReviewQueueService(
            store,
            QueueDirectory(store.queue_defs),
            notifier=MagicMock(name="notifier"),
            finalize_sender=MagicMock(name="finalize_sender"),
            clock=lambda: NOW,
            s3=MagicMock(name="s3"),
            document_loader=document_loader,
            output_bucket="out",
            document_service=MagicMock(name="document_service"),
            configuration_of=lambda profile, revision: SimpleNamespace(
                mode="pipeline", config=CONFIG
            ),
        )
        yield m


def _seed(module, *, state="CLAIMED", owner=HOLDER, **more):
    """Put the document's review item in the table, held by ``owner`` when claimed."""
    now_ms = int(NOW.timestamp() * 1000)
    fields = dict(
        doc_key=KEY,
        cycle_id=CYCLE,
        profile="default",
        config_revision=4,
        queue="claims",
        state=state,
        enqueued_at="2026-11-16T00:00:00.000Z",
        enqueued_at_ms=now_ms,
        sections=["1", "2"],
        version=1,
        processing_mode="pipeline",
        placed_layout=layout_of(_placed()),
    )
    if state == "CLAIMED":
        fields.update(
            owner_key=owner,
            owner_email=owner,
            claimed_at_ms=now_ms,
            lease_minutes=60,
            lease_expires_at_ms=now_ms + 3_600_000,
        )
    if state in ("HOLDING", "FINALIZING"):
        fields["sweep_at_ms"] = now_ms + 600_000
    fields.update(more)
    module.store.client.put_item(item_to_dynamo(ReviewItemRecord(**fields)))


def _ask(module, args=None, *, groups=("Reviewer",), email="Rev@Example.com"):
    return module.handler(
        event(FIELD, ARGS if args is None else args, groups=groups, email=email),
        None,
    )


def _as_admin(module, args=None):
    return _ask(module, args, groups=("Admin",), email="Admin@Example.com")


class TestWhoIsServed:
    @pytest.mark.parametrize("group", ["Reviewer", "Annotator"])
    def test_the_holder(self, module, group):
        _seed(module)

        plan = _ask(module, groups=(group,))

        assert plan["complete"]["startStep"] == "apply_review_changes"
        assert plan["sendBack"]["startStep"] == "classification"
        assert module.loads == [KEY]

    def test_an_admin_on_an_open_item(self, module):
        _seed(module, state="OPEN")

        plan = _as_admin(module)

        assert plan["complete"]["startStep"] == "apply_review_changes"
        assert plan["sendBack"]["startStep"] == "classification"

    def test_an_admin_on_an_item_someone_else_holds(self, module):
        _seed(module)

        assert _as_admin(module)["complete"]["startStep"] == "apply_review_changes"


class TestRefused:
    @pytest.mark.parametrize("groups", [("Viewer",), ("Author",), ()])
    def test_a_caller_outside_the_groups_before_any_work(self, module, groups):
        _seed(module)

        with (
            patch.object(module, "_caller_context") as caller,
            patch.object(module._SERVICE, "completion_plan") as planned,
            pytest.raises(PermissionError, match="ROLE: .*Admin, Annotator, Reviewer"),
        ):
            _ask(module, groups=groups)
        caller.assert_not_called()
        planned.assert_not_called()

    def test_a_reviewer_who_does_not_hold_the_item_gets_a_403(self, module, caplog):
        """A ``PermissionError`` with the service's code, logged as a refusal."""
        _seed(module)

        with (
            caplog.at_level("WARNING"),
            pytest.raises(PermissionError, match="NOT_LEASE_HOLDER") as refused,
        ):
            _ask(module, email="other@example.com")

        assert type(refused.value) is PermissionError
        # Refused before the document or its configuration is read.
        assert module.loads == []
        (logged,) = [r for r in caplog.records if f"field={FIELD}" in r.getMessage()]
        assert logged.levelname == "WARNING"
        assert logged.getMessage().startswith("Rejecting ")
        assert "code=NOT_LEASE_HOLDER" in logged.getMessage()
        assert logged.exc_info is None

    def test_a_holder_whose_scope_leaves_out_the_items_profile(self, module):
        _seed(module)
        module.scope = ["another-profile"]

        with pytest.raises(PermissionError, match="OUT_OF_SCOPE") as refused:
            _ask(module)

        assert type(refused.value) is PermissionError
        assert module.loads == []

    def test_a_reviewer_on_an_item_nobody_holds(self, module):
        """A bad request (400): there is no review of theirs to finish."""
        _seed(module, state="OPEN")

        with pytest.raises(ValueError, match="INVALID_STATE") as refused:
            _ask(module)

        assert type(refused.value) is ValueError

    @pytest.mark.parametrize("state", ["HOLDING", "FINALIZING", "COMPLETE"])
    def test_an_admin_on_a_review_that_is_not_open_or_assigned(self, module, state):
        _seed(module, state=state)

        with pytest.raises(ValueError, match="INVALID_STATE"):
            _as_admin(module)

    def test_an_older_review_cycle(self, module):
        _seed(module)

        with pytest.raises(ValueError, match="STALE_CYCLE") as refused:
            _ask(module, dict(ARGS, cycleId="f" * 21))

        assert type(refused.value) is ValueError

    def test_a_document_with_no_review_item_is_a_404(self, module):
        with pytest.raises(ResourceNotFound, match="ITEM_NOT_FOUND") as missing:
            _as_admin(module)

        assert type(missing.value) is ResourceNotFound

    def test_a_document_that_no_longer_exists_is_a_404(self, module):
        _seed(module)
        module.document = None

        with pytest.raises(ResourceNotFound, match="DOCUMENT_NOT_FOUND") as missing:
            _ask(module)

        assert type(missing.value) is ResourceNotFound

    @pytest.mark.parametrize("missing", ["objectKey", "cycleId"])
    def test_a_call_that_leaves_out_an_argument(self, module, missing):
        _seed(module)
        args = {k: v for k, v in ARGS.items() if k != missing}

        with pytest.raises(ValueError, match="BAD_ARGUMENT") as refused:
            _ask(module, args)

        assert type(refused.value) is ValueError
        assert module.loads == []

    @pytest.mark.parametrize(
        "args",
        [
            dict(ARGS, cycleId="not-a-cycle"),
            dict(ARGS, objectKey=""),
            dict(ARGS, startStep="extraction"),
        ],
        ids=["malformed-cycle", "empty-key", "an-argument-it-does-not-have"],
    )
    def test_a_call_with_a_bad_argument(self, module, args):
        _seed(module)

        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            _ask(module, args)
        assert module.loads == []


class TestTheAnswer:
    def test_it_is_in_the_api_shape(self, module):
        """camelCase all the way down, and nothing the schema does not declare."""
        _seed(module)
        module.document = _regrouped()

        plan = _ask(module)

        assert set(plan) == {"complete", "sendBack"}
        for run in plan.values():
            assert set(run) == {"mode", "startStep", "steps", "sections"}
            assert run["mode"] == "pipeline"
            assert all(set(step) == {"id", "label", "state"} for step in run["steps"])
        assert plan["complete"]["startStep"] == "extraction"
        assert plan["complete"]["sections"] == [
            {"sectionId": "1", "cause": "pages_changed"},
            {"sectionId": "3", "cause": "added"},
        ]
        assert plan["complete"]["steps"][2] == {
            "id": "extraction",
            "label": "Extraction",
            "state": "run",
        }
        assert plan["sendBack"]["sections"] == []
        # The answer leaves the function as JSON.
        assert json.loads(json.dumps(plan)) == plan

    def test_every_step_has_one_of_the_three_states(self, module):
        """``not_enabled`` is a step the item's configuration has switched off."""
        _seed(module)

        plan = _ask(module)

        for run in plan.values():
            states = {step["id"]: step["state"] for step in run["steps"]}
            assert set(states.values()) == {"run", "skipped", "not_enabled"}
            assert states["summarization"] == "not_enabled"

    def test_it_changes_nothing(self, module):
        _seed(module)
        module.document = _regrouped()
        before = _stored()

        _ask(module)

        assert _stored() == before
        service = module._SERVICE
        assert service.s3.mock_calls == []
        assert service.finalize_sender.mock_calls == []
        assert service.notify.mock_calls == []
        assert service.document_service.mock_calls == []
