# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The review queues resolver as a dispatcher: group gate, caller, errors, queue CRUD.

The queue semantics (claims, leases, dispositions, membership) belong to
``idp_common.review_queues`` and are tested there. This suite pins what the handler
adds, with a stub service: every field is group-gated server-side before any work,
the caller and their config-version scope come from verified claims and fail
closed, domain errors leave as exactly ``PermissionError``, ``ValueError`` or
``ResourceNotFound``, and the queue-definition operations map the API shape.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from idp_common.api_adapter import ResourceNotFound
from idp_common.review_queues import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewItemNotFound,
    ReviewValidationError,
)
from idp_common.review_queues.queue_defs import DEFAULT_SKIP_REASONS, QueueDefinition

_INDEX = Path(__file__).with_name("index.py")


def _load():
    name = "review_queues_resolver_index_under_test"
    with patch.dict(os.environ, {"HITL_TABLE_NAME": ""}):
        spec = importlib.util.spec_from_file_location(name, _INDEX)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _event(field, args=None, *, groups=("Admin",), email="Admin@Example.com"):
    claims = {"email": email, "sub": "sub-1", "cognito:groups": list(groups)}
    return {
        "info": {"fieldName": field},
        "arguments": {} if args is None else args,
        "identity": {"claims": claims},
    }


def _model(payload):
    m = MagicMock()
    m.model_dump.return_value = payload
    return m


@pytest.fixture
def module():
    m = _load()
    assert m._SERVICE is None  # no table at import
    m._SERVICE = MagicMock(name="ReviewQueueService")
    return m


@pytest.mark.unit
class TestGroupGate:
    def test_every_field_is_gated(self, module):
        assert set(module._FIELD_GROUPS) == set(module._FIELDS)

    def test_unsupported_field_is_a_bad_request(self, module):
        with pytest.raises(ValueError, match="unsupported field"):
            module.handler(_event("dropQueues"), None)

    @pytest.mark.parametrize(
        "field",
        [
            "createReviewQueue",
            "deleteReviewQueue",
            "reassignReviewItem",
            "removeReviewItem",
            "takeOverReviewItem",
        ],
    )
    def test_annotator_is_refused_admin_operations_before_any_work(self, module, field):
        with (
            patch.object(module, "_caller_context") as caller,
            pytest.raises(PermissionError, match="ROLE: .*Admin"),
        ):
            module.handler(_event(field, groups=("Annotator",)), None)
        caller.assert_not_called()
        assert module._SERVICE.mock_calls == []

    def test_viewer_is_refused_worker_operations(self, module):
        with pytest.raises(PermissionError, match="ROLE"):
            module.handler(_event("listMyReviewWork", groups=("Viewer",)), None)

    def test_unconfigured_table_is_a_server_fault(self, module):
        module._SERVICE = None
        with pytest.raises(RuntimeError):
            module.handler(_event("getReviewQueuesOverview"), None)


@pytest.mark.unit
class TestCaller:
    def test_admin_is_unscoped_and_never_looks_up_scope(self, module):
        module._SERVICE.overview.return_value = _model({"queues": []})
        with patch.object(module, "resolve_allowed_config_versions") as scope:
            assert module.handler(_event("getReviewQueuesOverview"), None) == {
                "queues": []
            }
        scope.assert_not_called()
        caller = module._SERVICE.overview.call_args.args[0]
        assert (caller.is_admin, caller.allowed_versions) == (True, None)
        assert caller.email == "admin@example.com"

    def test_annotator_carries_their_scope(self, module):
        module._SERVICE.list_my_work.return_value = _model({"items": []})
        with patch.object(
            module, "resolve_allowed_config_versions", return_value=["lending"]
        ):
            module.handler(
                _event("listMyReviewWork", groups=("Annotator",), email="a@x.com"),
                None,
            )
        caller = module._SERVICE.list_my_work.call_args.args[0]
        assert caller.is_admin is False
        assert caller.allowed_versions == ["lending"]

    def test_a_failed_scope_lookup_refuses(self, module):
        with (
            patch.object(
                module,
                "resolve_allowed_config_versions",
                side_effect=RuntimeError("table down"),
            ),
            pytest.raises(PermissionError, match="OUT_OF_SCOPE"),
        ):
            module.handler(
                _event("listMyReviewWork", groups=("Annotator",), email="a@x.com"),
                None,
            )
        module._SERVICE.list_my_work.assert_not_called()

    def test_no_caller_identity_refuses(self, module):
        event = _event("getReviewQueuesOverview")
        event["identity"]["claims"] = {"cognito:groups": ["Admin"]}
        with pytest.raises(PermissionError, match="ROLE: no caller identity"):
            module.handler(event, None)


@pytest.mark.unit
class TestErrors:
    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (ReviewAccessDenied("NOT_MEMBER", "no"), PermissionError),
            (ReviewConflict("ALREADY_CLAIMED", "taken"), ValueError),
            (ReviewValidationError("BAD_CURSOR", "bad"), ValueError),
            (ReviewItemNotFound("NOT_FOUND", "gone"), ResourceNotFound),
        ],
    )
    def test_domain_errors_leave_as_exact_builtins(self, module, raised, expected):
        module._SERVICE.overview.side_effect = raised
        with pytest.raises(expected) as exc:
            module.handler(_event("getReviewQueuesOverview"), None)
        assert type(exc.value) is expected
        assert str(raised) in str(exc.value)

    def test_bad_arguments_are_a_bad_request(self, module):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            module.handler(_event("getReviewQueuesOverview", {"x": 1}), None)
        module._SERVICE.overview.assert_not_called()


@pytest.mark.unit
class TestQueueDefinitions:
    def test_create_maps_the_api_shape_both_ways(self, module):
        store = module._SERVICE.directory.queue_defs
        store.put.side_effect = lambda d, create: d
        result = module.handler(
            _event(
                "createReviewQueue",
                {
                    "input": {
                        "id": " Claims ",
                        "priority": 70,
                        "timeLimitMinutes": 15,
                        "eligibleAnnotators": ["A@x.com"],
                        "rejectReasons": ["Blurry"],
                    }
                },
            ),
            None,
        )
        (definition,) = store.put.call_args.args
        assert store.put.call_args.kwargs == {"create": True}
        assert isinstance(definition, QueueDefinition)
        assert result == {
            "id": "claims",
            "name": "Claims",
            "description": None,
            "priority": 70,
            "checklist": [],
            "rejectReasons": ["Blurry"],
            "skipReasons": list(DEFAULT_SKIP_REASONS),
            "timeLimitMinutes": 15,
            "eligibleAnnotators": ["a@x.com"],
            "isDefaultQueue": False,
            "confidenceThreshold": 0.8,
            "enabled": True,
        }

    def test_create_takes_the_threshold_and_switch(self, module):
        store = module._SERVICE.directory.queue_defs
        store.put.side_effect = lambda d, create: d
        result = module.handler(
            _event(
                "createReviewQueue",
                {"input": {"id": "q", "confidenceThreshold": 0.6, "enabled": False}},
            ),
            None,
        )
        assert (result["confidenceThreshold"], result["enabled"]) == (0.6, False)

    def test_a_threshold_outside_0_1_is_a_bad_request(self, module):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            module.handler(
                _event(
                    "createReviewQueue",
                    {"input": {"id": "q", "confidenceThreshold": 1.5}},
                ),
                None,
            )
        module._SERVICE.directory.queue_defs.put.assert_not_called()

    def test_update_writes_over_an_existing_queue(self, module):
        store = module._SERVICE.directory.queue_defs
        store.put.side_effect = lambda d, create: d
        store.get.return_value = QueueDefinition(
            id="default", enabled=True, confidence_threshold=0.9
        )
        result = module.handler(
            _event("updateReviewQueue", {"input": {"id": "default"}}), None
        )
        assert store.put.call_args.kwargs == {"create": False}
        assert result["isDefaultQueue"] is True
        # Left out of the input, the switch and threshold keep their stored values.
        assert (result["enabled"], result["confidenceThreshold"]) == (True, 0.9)
        store.invalidate.assert_called_once()

    def test_update_changes_the_threshold_and_switch(self, module):
        store = module._SERVICE.directory.queue_defs
        store.put.side_effect = lambda d, create: d
        store.get.return_value = QueueDefinition(id="default", enabled=False)
        result = module.handler(
            _event(
                "updateReviewQueue",
                {
                    "input": {
                        "id": "default",
                        "enabled": True,
                        "confidenceThreshold": 0.7,
                    }
                },
            ),
            None,
        )
        assert (result["enabled"], result["confidenceThreshold"]) == (True, 0.7)

    def test_update_keeps_the_stored_skip_reasons_when_omitted(self, module):
        """The queue dialog saves without the lists it does not show."""
        store = module._SERVICE.directory.queue_defs
        store.put.side_effect = lambda d, create: d
        store.get.return_value = QueueDefinition(
            id="claims", skip_reasons=["Needs a specialist"]
        )
        result = module.handler(
            _event("updateReviewQueue", {"input": {"id": "claims"}}), None
        )
        assert result["skipReasons"] == ["Needs a specialist"]

    def test_update_sets_the_skip_reasons_given(self, module):
        store = module._SERVICE.directory.queue_defs
        store.put.side_effect = lambda d, create: d
        store.get.return_value = QueueDefinition(id="claims")
        result = module.handler(
            _event(
                "updateReviewQueue",
                {"input": {"id": "claims", "skipReasons": [" Busy ", "Busy", "Other"]}},
            ),
            None,
        )
        assert result["skipReasons"] == ["Busy", "Other"]

    def test_skip_requires_a_reason(self, module):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            module.handler(
                _event(
                    "releaseReviewItem",
                    {"objectKey": "a.pdf", "cycleId": "0" * 21},
                ),
                None,
            )
        module._SERVICE.release.assert_not_called()

    def test_skip_passes_its_reason_and_note_to_the_service(self, module):
        module._SERVICE.release.return_value = _model({"state": "OPEN"})
        module.handler(
            _event(
                "releaseReviewItem",
                {
                    "objectKey": "a.pdf",
                    "cycleId": "0" * 21,
                    "reason": "Out of time",
                    "note": "back tomorrow",
                },
            ),
            None,
        )
        args = module._SERVICE.release.call_args.args
        assert args[1:] == ("a.pdf", "0" * 21, "Out of time", "back tomorrow")

    def test_invalid_queue_input_is_a_bad_request(self, module):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            module.handler(
                _event("createReviewQueue", {"input": {"id": "bad/name"}}), None
            )
        module._SERVICE.directory.queue_defs.put.assert_not_called()

    def test_delete_moves_items_through_the_service(self, module):
        module._SERVICE.delete_queue.return_value = 3
        assert (
            module.handler(_event("deleteReviewQueue", {"id": " Claims "}), None)
            is True
        )
        (caller, queue_id), _ = module._SERVICE.delete_queue.call_args
        assert queue_id == "claims" and caller.is_admin

    def test_delete_is_refused_while_an_item_is_finalizing(self, module):
        module._SERVICE.delete_queue.side_effect = ReviewConflict(
            "QUEUE_FINALIZING", "try again in a minute"
        )
        with pytest.raises(ValueError, match="QUEUE_FINALIZING"):
            module.handler(_event("deleteReviewQueue", {"id": "claims"}), None)

    def test_remove_passes_the_note(self, module):
        module._SERVICE.remove.return_value = _model({"objectKey": "a.pdf"})
        result = module.handler(
            _event(
                "removeReviewItem",
                {"objectKey": "a.pdf", "cycleId": "0" * 21, "note": "dup"},
            ),
            None,
        )
        assert result == {"objectKey": "a.pdf"}
        (_caller, key, cycle, note), _ = module._SERVICE.remove.call_args
        assert (key, cycle, note) == ("a.pdf", "0" * 21, "dup")

    def test_take_over_routes_to_the_service_with_the_admin_caller(self, module):
        module._SERVICE.take_over.return_value = _model({"objectKey": "a.pdf"})
        result = module.handler(
            _event("takeOverReviewItem", {"objectKey": "a.pdf", "cycleId": "0" * 21}),
            None,
        )
        assert result == {"objectKey": "a.pdf"}
        (caller, key, cycle), _ = module._SERVICE.take_over.call_args
        assert (key, cycle) == ("a.pdf", "0" * 21)
        assert caller.is_admin and caller.email == "admin@example.com"

    def test_take_over_of_an_open_item_is_a_bad_request(self, module):
        module._SERVICE.take_over.side_effect = ReviewConflict(
            "NOT_ASSIGNED", "the item isn't assigned; claim it instead"
        )
        with pytest.raises(ValueError, match="NOT_ASSIGNED"):
            module.handler(
                _event(
                    "takeOverReviewItem", {"objectKey": "a.pdf", "cycleId": "0" * 21}
                ),
                None,
            )

    def test_delete_needs_an_id(self, module):
        with pytest.raises(ValueError, match="id is required"):
            module.handler(_event("deleteReviewQueue", {"id": " "}), None)

    def test_list_annotators_needs_the_user_pool(self, module, monkeypatch):
        monkeypatch.delenv("USER_POOL_ID", raising=False)
        with pytest.raises(ValueError, match="USER_POOL_ID"):
            module.handler(_event("listAnnotators"), None)

    def test_list_annotators_reads_the_pool(self, module, monkeypatch):
        monkeypatch.setenv("USER_POOL_ID", "pool-1")
        with patch.object(
            module, "annotator_emails", return_value=["a@x.com"]
        ) as emails:
            assert module.handler(_event("listAnnotators"), None) == ["a@x.com"]
        emails.assert_called_once_with("pool-1")
