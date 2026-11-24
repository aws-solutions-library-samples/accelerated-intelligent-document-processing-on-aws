# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``client.review_queues``: the production review queues, through the stack.

The four operations hold no queue logic. Each invokes the stack's review queues
resolver with an event that carries no identity, which the resolver runs as a
system Admin, so a queue created from the SDK passes the checks one created in
Review Management does. These tests pin what is sent and how the answer, or the
resolver's refusal, reaches the caller.

CloudFormation and Lambda are the stand-ins of ``conftest.py`` (``resolver_stack``,
``api_resolver``): a real botocore Lambda client behind a ``Stubber``.
"""

from __future__ import annotations

import pytest

from idp_sdk import (
    IDPClient,
    IDPError,
    IDPProcessingError,
    IDPResourceNotFoundError,
    IDPValidationError,
    ReviewQueueDefinition,
    ReviewQueuePage,
)
from idp_sdk._core import resolver_invoke
from idp_sdk.operations.review_queues import REVIEW_QUEUES_OUTPUT

pytestmark = pytest.mark.unit

STACK = "idp-queues-test"
REGION = "us-east-1"


def _view(queue_id: str, **fields) -> dict:
    """A queue as the resolver returns it."""
    view = {
        "id": queue_id,
        "name": queue_id.title(),
        "description": None,
        "priority": 50,
        "checklist": [],
        "rejectReasons": ["Illegible"],
        "skipReasons": ["Not mine"],
        "timeLimitMinutes": 120,
        "eligibleAnnotators": [],
        "isDefaultQueue": queue_id == "default",
        "enabled": True,
        "version": 1,
    }
    view.update(fields)
    return view


@pytest.fixture(autouse=True)
def stack(resolver_stack):
    resolver_stack.has(REVIEW_QUEUES_OUTPUT)
    return resolver_stack


@pytest.fixture
def resolver(api_resolver):
    return api_resolver


@pytest.fixture
def queues():
    return IDPClient(stack_name=STACK, region=REGION).review_queues


class TestList:
    def test_a_page_of_queues(self, queues, resolver, stack):
        resolver.answers(
            "listReviewQueues",
            {"limit": 25},
            {
                "items": [_view("claims", priority=80), _view("default")],
                "nextToken": None,
            },
        )

        page = queues.list()

        resolver.done()
        assert isinstance(page, ReviewQueuePage)
        assert (page.count, page.next_token, len(page)) == (2, None, 2)
        claims, default = page
        assert isinstance(claims, ReviewQueueDefinition)
        assert (claims.id, claims.priority, claims.is_default_queue) == (
            "claims",
            80,
            False,
        )
        assert claims.reject_reasons == ["Illegible"]
        assert claims.time_limit_minutes == 120
        assert default.is_default_queue is True and page[1] is default
        assert stack.lookups == [(STACK, "apiresolver", REVIEW_QUEUES_OUTPUT)]
        assert resolver.regions == [REGION]

    def test_the_limit_and_the_token_are_sent(self, queues, resolver):
        resolver.answers(
            "listReviewQueues",
            {"limit": 2, "nextToken": "token-1"},
            {"items": [_view("x")], "nextToken": "token-2"},
        )

        page = queues.list(limit=2, next_token="token-1")

        resolver.done()
        assert page.next_token == "token-2"

    def test_every_page_can_be_read_by_following_the_token(self, queues, resolver):
        resolver.answers(
            "listReviewQueues",
            {"limit": 1},
            {"items": [_view("a")], "nextToken": "t"},
        )
        resolver.answers(
            "listReviewQueues",
            {"limit": 1, "nextToken": "t"},
            {"items": [_view("b")], "nextToken": None},
        )

        found, token = [], None
        while True:
            page = queues.list(limit=1, next_token=token)
            found.extend(queue.id for queue in page)
            token = page.next_token
            if not token:
                break

        resolver.done()
        assert found == ["a", "b"]

    def test_no_queues_is_an_empty_page(self, queues, resolver):
        resolver.answers(
            "listReviewQueues", {"limit": 25}, {"items": [], "nextToken": None}
        )

        page = queues.list()

        assert (page.queues, page.count, page.next_token) == ([], 0, None)

    def test_a_refused_argument_is_a_validation_error(self, queues, resolver):
        resolver.raises(
            "listReviewQueues",
            {"limit": 25, "nextToken": "garbage"},
            "ValueError",
            "BAD_ARGUMENT: nextToken is not a token this operation returned",
        )

        with pytest.raises(IDPValidationError, match="BAD_ARGUMENT: nextToken"):
            queues.list(next_token="garbage")


class TestCreate:
    def test_a_definition_is_sent_under_the_api_names(self, queues, resolver):
        resolver.answers(
            "createReviewQueue",
            {
                "input": {
                    "id": "claims",
                    "name": "Claims",
                    "priority": 80,
                    "rejectReasons": ["Illegible"],
                    "timeLimitMinutes": 30,
                    "eligibleAnnotators": ["a@example.com"],
                    "enabled": True,
                }
            },
            _view("claims", priority=80, timeLimitMinutes=30),
        )

        created = queues.create(
            ReviewQueueDefinition(
                id="claims",
                name="Claims",
                priority=80,
                reject_reasons=["Illegible"],
                time_limit_minutes=30,
                eligible_annotators=["a@example.com"],
                enabled=True,
            )
        )

        resolver.done()
        assert isinstance(created, ReviewQueueDefinition)
        assert (created.id, created.priority, created.version) == ("claims", 80, 1)

    @pytest.mark.parametrize(
        "definition",
        [
            {"id": "claims", "reject_reasons": ["Illegible"], "time_limit_minutes": 30},
            {"id": "claims", "rejectReasons": ["Illegible"], "timeLimitMinutes": 30},
        ],
        ids=["python-names", "api-names"],
    )
    def test_a_dict_may_use_either_spelling(self, queues, resolver, definition):
        resolver.answers(
            "createReviewQueue",
            {
                "input": {
                    "id": "claims",
                    "rejectReasons": ["Illegible"],
                    "timeLimitMinutes": 30,
                }
            },
            _view("claims"),
        )

        queues.create(definition)

        resolver.done()

    def test_what_a_read_adds_is_not_sent_back(self, queues, resolver):
        """A definition read with ``list`` carries ``version`` and
        ``is_default_queue``; neither is part of what a create sends."""
        resolver.answers(
            "createReviewQueue",
            {"input": {"id": "claims", "name": "Claims"}},
            _view("claims"),
        )

        queues.create(
            ReviewQueueDefinition(
                id="claims", name="Claims", version=4, is_default_queue=False
            )
        )

        resolver.done()

    def test_an_id_that_is_taken_is_a_validation_error(self, queues, resolver):
        resolver.raises(
            "createReviewQueue",
            {"input": {"id": "claims"}},
            "ValueError",
            "QUEUE_EXISTS: a queue with this id exists",
        )

        with pytest.raises(IDPValidationError, match="QUEUE_EXISTS"):
            queues.create({"id": "claims"})

    @pytest.mark.parametrize("definition", [{}, {"name": "No id"}, {"id": 7}, "claims"])
    def test_something_that_is_not_a_definition_is_refused_before_any_request(
        self, queues, stack, definition
    ):
        with pytest.raises(IDPValidationError, match="Not a review queue definition"):
            queues.create(definition)
        assert stack.lookups == []


class TestUpdate:
    def test_the_definition_replaces_the_stored_one(self, queues, resolver):
        resolver.answers(
            "updateReviewQueue",
            {"input": {"id": "claims", "priority": 90}},
            _view("claims", priority=90, version=2),
        )

        updated = queues.update({"id": "claims", "priority": 90})

        resolver.done()
        assert (updated.priority, updated.version) == (90, 2)

    def test_the_expected_version_is_sent_when_given(self, queues, resolver):
        resolver.answers(
            "updateReviewQueue",
            {"input": {"id": "claims", "priority": 90, "version": 4}},
            _view("claims", priority=90, version=5),
        )

        queues.update({"id": "claims", "priority": 90}, expected_version=4)

        resolver.done()

    def test_version_zero_is_an_expected_version(self, queues, resolver):
        resolver.answers(
            "updateReviewQueue",
            {"input": {"id": "default", "enabled": False, "version": 0}},
            _view("default", enabled=False),
        )

        queues.update({"id": "default", "enabled": False}, expected_version=0)

        resolver.done()

    def test_the_version_of_the_definition_is_not_an_expected_version(
        self, queues, resolver
    ):
        """Only ``expected_version`` asks for the check, so a definition read
        earlier does not refuse an update by carrying its version along."""
        resolver.answers(
            "updateReviewQueue", {"input": {"id": "claims"}}, _view("claims", version=9)
        )

        queues.update(ReviewQueueDefinition(id="claims", version=3))

        resolver.done()

    def test_a_queue_changed_since_it_was_read_is_a_validation_error(
        self, queues, resolver
    ):
        resolver.raises(
            "updateReviewQueue",
            {"input": {"id": "claims", "version": 1}},
            "ValueError",
            "QUEUE_CHANGED: the queue was changed by someone else",
        )

        with pytest.raises(IDPValidationError, match="QUEUE_CHANGED"):
            queues.update({"id": "claims"}, expected_version=1)

    def test_a_queue_that_does_not_exist_is_not_found(self, queues, resolver):
        resolver.raises(
            "updateReviewQueue",
            {"input": {"id": "gone"}},
            "ResourceNotFound",
            "QUEUE_NOT_FOUND: no such queue",
        )

        with pytest.raises(IDPResourceNotFoundError, match="QUEUE_NOT_FOUND"):
            queues.update({"id": "gone"})


class TestDelete:
    @pytest.mark.parametrize("moved", [3, 0])
    def test_it_returns_how_many_documents_moved(self, queues, resolver, moved):
        resolver.answers("deleteReviewQueue", {"id": "claims"}, moved)

        assert queues.delete("claims") == moved
        resolver.done()

    def test_a_stack_that_answers_true_reports_no_count(self, queues, resolver):
        resolver.answers("deleteReviewQueue", {"id": "claims"}, True)

        answer = queues.delete("claims")

        assert answer == 0 and answer is not True

    def test_the_default_queue_cannot_be_deleted(self, queues, resolver):
        resolver.raises(
            "deleteReviewQueue",
            {"id": "default"},
            "ValueError",
            "PROTECTED_QUEUE: the Default queue can't be deleted",
        )

        with pytest.raises(IDPValidationError, match="PROTECTED_QUEUE"):
            queues.delete("default")


class TestErrors:
    def test_a_fault_in_the_function_is_a_processing_error(self, queues, resolver):
        resolver.raises(
            "listReviewQueues", {"limit": 25}, "RuntimeError", "not configured"
        )

        with pytest.raises(IDPProcessingError, match="not configured") as failed:
            queues.list()

        assert isinstance(failed.value, resolver_invoke.ResolverFunctionError)
        assert not isinstance(failed.value, IDPValidationError)

    def test_a_refusal_for_a_caller_is_not_called_a_bad_argument(
        self, queues, resolver
    ):
        resolver.raises(
            "listReviewQueues", {"limit": 25}, "PermissionError", "ROLE: requires Admin"
        )

        with pytest.raises(IDPError, match="ROLE") as refused:
            queues.list()

        assert not isinstance(refused.value, IDPValidationError)

    def test_a_stack_without_the_output_says_it_has_to_be_updated(self, queues, stack):
        stack.outputs = {}

        with pytest.raises(IDPResourceNotFoundError) as missing:
            queues.list()

        message = str(missing.value)
        assert REVIEW_QUEUES_OUTPUT in message and STACK in message
        assert "after release 0.6.15" in message

    def test_another_stack_may_be_named(self, queues, resolver, stack):
        resolver.answers("listReviewQueues", {"limit": 25}, {"items": []})

        queues.list(stack_name="another-stack")

        assert stack.lookups == [("another-stack", "apiresolver", REVIEW_QUEUES_OUTPUT)]

    def test_a_client_with_no_stack_is_a_configuration_error(self):
        from idp_sdk import IDPConfigurationError

        with pytest.raises(IDPConfigurationError):
            IDPClient(region=REGION).review_queues.list()
