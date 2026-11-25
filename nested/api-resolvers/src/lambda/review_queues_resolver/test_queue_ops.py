# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Queue administration through the resolver: pages, the service, direct invokes.

The handler holds no queue-definition logic of its own: it parses the call into a
typed argument model, hands it to ``ReviewQueueService`` and shapes the answer.
A direct Lambda invoke (the SDK and the CLI, with no identity) may run the four
queue-definition operations as a system Admin, and nothing else.
"""

from unittest.mock import MagicMock, patch

import pytest
from _harness import direct_event, event, load, stub_service

from idp_common.review_queues.api_models import (
    CreateQueueArgs,
    ListQueuesArgs,
    UpdateQueueArgs,
)
from idp_common.review_queues.queue_defs import QueueDefinition

pytestmark = pytest.mark.unit

QUEUE_FIELDS = (
    "listReviewQueues",
    "createReviewQueue",
    "updateReviewQueue",
    "deleteReviewQueue",
)


@pytest.fixture
def module():
    m = load()
    m._SERVICE = MagicMock(name="ReviewQueueService")
    return m


@pytest.fixture
def stored(module):
    """The handler over the real queue operations and a stub queue store."""
    module._SERVICE = stub_service()
    return module._SERVICE.directory.queue_defs


class TestListing:
    def test_a_page_has_its_items_and_the_next_token(self, module, stored):
        stored.list_page.return_value = (
            [QueueDefinition(id="claims"), QueueDefinition(id="default")],
            "after-default",
        )

        page = module.handler(event("listReviewQueues", {"limit": 2}), None)

        assert [q["id"] for q in page["items"]] == ["claims", "default"]
        assert page["nextToken"] == "after-default"
        assert page["items"][1]["isDefaultQueue"] is True

    def test_the_limit_and_the_token_reach_the_store(self, module, stored):
        stored.list_page.return_value = ([], None)

        page = module.handler(
            event("listReviewQueues", {"limit": 5, "nextToken": "after-claims"}), None
        )

        assert stored.list_page.call_args.args == (5, "after-claims")
        assert page == {"items": [], "nextToken": None}

    def test_a_call_with_no_arguments_gets_the_first_page(self, module, stored):
        stored.list_page.return_value = ([], None)

        module.handler(event("listReviewQueues"), None)

        limit, token = stored.list_page.call_args.args
        assert token is None
        assert limit >= 1

    def test_an_argument_the_operation_does_not_have_is_a_bad_request(self, module):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            module.handler(event("listReviewQueues", {"queue": "claims"}), None)


class TestTheServiceDoesTheWork:
    """With a service that is only a mock: what the handler passes and returns."""

    def test_list_goes_through_the_service(self, module):
        module._SERVICE.list_queues.return_value = ([QueueDefinition(id="q")], None)

        page = module.handler(event("listReviewQueues", {"limit": 3}), None)

        caller, args = module._SERVICE.list_queues.call_args.args
        assert caller.is_admin
        assert isinstance(args, ListQueuesArgs) and args.limit == 3
        assert [q["id"] for q in page["items"]] == ["q"]

    def test_create_goes_through_the_service(self, module):
        module._SERVICE.create_queue.return_value = QueueDefinition(
            id="claims", priority=70
        )

        view = module.handler(
            event("createReviewQueue", {"input": {"id": "claims", "priority": 70}}),
            None,
        )

        caller, args = module._SERVICE.create_queue.call_args.args
        assert caller.email == "admin@example.com"
        assert isinstance(args, CreateQueueArgs)
        assert (args.id, args.priority) == ("claims", 70)
        assert (view["id"], view["priority"]) == ("claims", 70)
        # The handler reads and writes no queue itself.
        assert module._SERVICE.directory.mock_calls == []

    def test_update_goes_through_the_service(self, module):
        module._SERVICE.update_queue.return_value = QueueDefinition(
            id="claims", version=4
        )

        view = module.handler(
            event("updateReviewQueue", {"input": {"id": "claims", "version": 3}}),
            None,
        )

        _, args = module._SERVICE.update_queue.call_args.args
        assert isinstance(args, UpdateQueueArgs)
        assert (args.id, args.version) == ("claims", 3)
        assert view["version"] == 4
        assert module._SERVICE.directory.mock_calls == []

    def test_a_call_without_an_input_is_a_bad_request(self, module):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            module.handler(event("createReviewQueue", {}), None)
        module._SERVICE.create_queue.assert_not_called()

    def test_the_handler_has_no_queue_definition_logic_of_its_own(self, module):
        assert not hasattr(module, "_queue_input")
        assert not hasattr(module, "_expected_version")


class TestADirectInvoke:
    """No identity: admitted by IAM on the function, for queue administration."""

    def _run(self, module, field, args=None):
        with patch.object(module, "_caller_context") as from_claims:
            result = module.handler(direct_event(field, args), None)
        from_claims.assert_not_called()
        return result

    def test_it_lists_the_queues(self, module, stored):
        stored.list_page.return_value = ([QueueDefinition(id="claims")], None)

        page = self._run(module, "listReviewQueues")

        assert [q["id"] for q in page["items"]] == ["claims"]

    def test_it_creates_a_queue(self, module, stored):
        stored.put.side_effect = lambda d, create, expected_version=None: d

        view = self._run(module, "createReviewQueue", {"input": {"id": "claims"}})

        assert view["id"] == "claims"
        assert stored.put.call_args.kwargs == {"create": True}

    def test_it_updates_a_queue(self, module, stored):
        stored.put.side_effect = lambda d, create, expected_version=None: d
        stored.read_current.return_value = QueueDefinition(id="claims", version=2)

        view = self._run(
            module, "updateReviewQueue", {"input": {"id": "claims", "priority": 80}}
        )

        assert view["priority"] == 80
        assert stored.put.call_args.kwargs == {"create": False, "expected_version": 2}

    @pytest.mark.parametrize("moved", [3, 0])
    def test_it_deletes_a_queue_and_says_how_many_items_moved(self, module, moved):
        """The API field is a Boolean; a direct invoke is told the number."""
        module._SERVICE.delete_queue.return_value = moved

        answer = self._run(module, "deleteReviewQueue", {"id": "claims"})

        assert answer == moved and answer is not True
        caller, queue_id = module._SERVICE.delete_queue.call_args.args
        assert queue_id == "claims"

    @pytest.mark.parametrize("field", QUEUE_FIELDS)
    def test_it_runs_as_a_system_admin_with_no_scope(self, module, field):
        seen = []
        module._FIELDS = dict(module._FIELDS)
        module._FIELDS[field] = lambda caller, args: seen.append(caller) or True

        self._run(module, field)

        (caller,) = seen
        assert caller.reviewer_key == "iam:direct-invoke"
        assert caller.is_admin is True
        assert caller.allowed_versions is None
        assert caller.email == ""

    def test_completing_a_review_is_refused(self, module):
        """A review is a person's decision: there is nobody to complete it as."""
        with pytest.raises(PermissionError, match="ROLE"):
            module.handler(
                direct_event(
                    "completeReviewItem", {"objectKey": "a.pdf", "cycleId": "0" * 21}
                ),
                None,
            )
        assert module._SERVICE.mock_calls == []

    def test_every_other_field_is_refused(self, module):
        refused = sorted(set(module._FIELDS) - set(QUEUE_FIELDS))
        assert "sendBackReviewItem" in refused and "testRoutingTable" in refused
        for field in refused:
            with pytest.raises(PermissionError, match="ROLE"):
                module.handler(direct_event(field), None)
        assert module._SERVICE.mock_calls == []

    def test_the_fields_a_direct_invoke_may_run_are_the_four(self, module):
        assert set(module._DIRECT_INVOKE_FIELDS) == set(QUEUE_FIELDS)

    @pytest.mark.parametrize("identity", [{}, {"claims": {}}])
    def test_an_identity_with_no_groups_is_not_a_direct_invoke(self, module, identity):
        """Only the absence of an identity is: an empty one is a caller in no group."""
        call = direct_event("listReviewQueues")
        call["identity"] = identity

        with pytest.raises(PermissionError, match="ROLE"):
            module.handler(call, None)

    def test_an_explicit_null_identity_is_a_direct_invoke(self, module, stored):
        stored.list_page.return_value = ([], None)
        call = direct_event("listReviewQueues")
        call["identity"] = None

        assert module.handler(call, None) == {"items": [], "nextToken": None}
