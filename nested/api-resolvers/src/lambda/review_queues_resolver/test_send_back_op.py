# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``sendBackReviewItem``: authorise the reviewer, then ask for the run.

The service says whether the caller may send the item back (its holder, or an
Admin). The run itself is the reprocess function's: it is invoked with the
``origin`` of the request and with no identity, because this function has done
the authorising and a reviewer need not be allowed to reprocess documents.

The call names no step: where a sent-back run starts is the reprocess service's
decision, so the request carries none and a call that names one is refused.
"""

import io
import json
from unittest.mock import MagicMock

import pytest
from _harness import event, load

from idp_common.review_queues import ReviewAccessDenied, ReviewConflict

pytestmark = pytest.mark.unit

KEY = "claims/packet.pdf"
CYCLE = "0123456789abcdef01234"
ARGS = {"objectKey": KEY, "cycleId": CYCLE}


def _answer(body, *, error=False):
    response = {"Payload": io.BytesIO(json.dumps(body).encode("utf-8"))}
    if error:
        response["FunctionError"] = "Unhandled"
    return response


def _queued(step="classification"):
    return {
        "queued": 1,
        "results": [
            {
                "objectKey": KEY,
                "status": "QUEUED",
                "startStep": step,
                "reason": "",
                "message": "",
            }
        ],
    }


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setenv("REPROCESS_FUNCTION_NAME", "stack-ReprocessFunction")
    m = load()
    # A caller who is not an Admin has their configuration scope looked up; these
    # callers have none.
    monkeypatch.setattr(m, "resolve_allowed_config_versions", lambda *a, **kw: None)
    m._SERVICE = MagicMock(name="ReviewQueueService")
    m._SERVICE.authorize_send_back.return_value = MagicMock(cycle_id=CYCLE)
    m._LAMBDA = MagicMock(name="lambda")
    m._LAMBDA.invoke.return_value = _answer(_queued())
    return m


def _sent(module):
    (call,) = module._LAMBDA.invoke.call_args_list
    return call.kwargs, json.loads(call.kwargs["Payload"])


class TestTheHolder:
    def test_the_run_is_asked_of_the_reprocess_function(self, module):
        result = module.handler(
            event(
                "sendBackReviewItem",
                dict(ARGS, note="wrong class"),
                groups=("Reviewer",),
                email="Rev@Example.com",
            ),
            None,
        )

        kwargs, payload = _sent(module)
        assert kwargs["FunctionName"] == "stack-ReprocessFunction"
        assert kwargs["InvocationType"] == "RequestResponse"
        assert payload["info"] == {"fieldName": "reprocessDocument"}
        assert payload["arguments"] == {
            "objectKeys": [KEY],
            "origin": {
                "kind": "sent_back",
                "actor": "rev@example.com",
                "cycleId": CYCLE,
                "note": "wrong class",
            },
        }
        assert result == _queued()["results"][0]

    def test_the_invoke_carries_no_identity(self, module):
        """The reprocess function treats it as a trusted, direct invoke."""
        module.handler(event("sendBackReviewItem", ARGS, groups=("Annotator",)), None)

        _, payload = _sent(module)
        assert "identity" not in payload
        assert set(payload) == {"info", "arguments"}

    def test_the_service_is_asked_about_this_caller_item_and_cycle(self, module):
        module.handler(event("sendBackReviewItem", ARGS, groups=("Reviewer",)), None)

        caller, key, cycle = module._SERVICE.authorize_send_back.call_args.args
        assert (key, cycle) == (KEY, CYCLE)
        assert caller.is_admin is False

    def test_the_cycle_sent_on_is_the_items(self, module):
        module._SERVICE.authorize_send_back.return_value = MagicMock(cycle_id=CYCLE)

        module.handler(event("sendBackReviewItem", ARGS), None)

        _, payload = _sent(module)
        assert payload["arguments"]["origin"]["cycleId"] == CYCLE

    def test_the_request_names_no_step(self, module):
        """The reprocess service decides where a sent-back run starts."""
        module.handler(event("sendBackReviewItem", dict(ARGS, note="n")), None)

        _, payload = _sent(module)
        assert "startStep" not in payload["arguments"]
        assert set(payload["arguments"]) == {"objectKeys", "origin"}

    def test_the_step_the_run_starts_at_is_the_reprocess_functions_answer(self, module):
        module._LAMBDA.invoke.return_value = _answer(_queued("beginning"))

        result = module.handler(event("sendBackReviewItem", ARGS), None)

        assert result["startStep"] == "beginning"

    def test_a_refusal_by_the_reprocess_function_is_returned_as_it_is(self, module):
        refused = {
            "objectKey": KEY,
            "status": "REFUSED",
            "startStep": "",
            "reason": "CYCLE_CHANGED",
            "message": "the document has a newer review cycle",
        }
        module._LAMBDA.invoke.return_value = _answer(
            {"queued": 0, "results": [refused]}
        )

        result = module.handler(event("sendBackReviewItem", ARGS), None)

        assert result == refused


class TestRefusedBeforeAnyInvoke:
    def test_someone_who_does_not_hold_the_item(self, module):
        module._SERVICE.authorize_send_back.side_effect = ReviewAccessDenied(
            "NOT_LEASE_HOLDER", "you don't hold this item's claim"
        )

        with pytest.raises(PermissionError, match="NOT_LEASE_HOLDER"):
            module.handler(
                event("sendBackReviewItem", ARGS, groups=("Reviewer",)), None
            )
        module._LAMBDA.invoke.assert_not_called()

    def test_an_item_that_is_being_finalized(self, module):
        module._SERVICE.authorize_send_back.side_effect = ReviewConflict(
            "INVALID_STATE", "the item is FINALIZING"
        )

        with pytest.raises(ValueError, match="INVALID_STATE"):
            module.handler(event("sendBackReviewItem", ARGS), None)
        module._LAMBDA.invoke.assert_not_called()

    @pytest.mark.parametrize(
        "step", ["beginning", "classification", "extraction", "nowhere", ""]
    )
    def test_a_call_that_names_a_step(self, module, step):
        """A bad request (400), whatever the step: the operation takes none."""
        with pytest.raises(ValueError, match="BAD_ARGUMENT") as refused:
            module.handler(
                event("sendBackReviewItem", dict(ARGS, startStep=step)), None
            )
        assert type(refused.value) is ValueError
        assert module._SERVICE.mock_calls == []
        module._LAMBDA.invoke.assert_not_called()

    def test_a_viewer(self, module):
        with pytest.raises(PermissionError, match="ROLE"):
            module.handler(event("sendBackReviewItem", ARGS, groups=("Viewer",)), None)
        assert module._SERVICE.mock_calls == []
        module._LAMBDA.invoke.assert_not_called()

    @pytest.mark.parametrize("missing", ["objectKey", "cycleId"])
    def test_a_call_that_leaves_out_an_argument(self, module, missing):
        args = {k: v for k, v in ARGS.items() if k != missing}

        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            module.handler(event("sendBackReviewItem", args), None)
        module._LAMBDA.invoke.assert_not_called()


class TestAFault:
    def test_a_function_error_is_a_server_fault(self, module, caplog):
        """Not a refusal: the caller did nothing wrong, and gets no detail.

        The log names the kind of error for whoever operates the stack, and not
        its message, which can hold a document key.
        """
        module._LAMBDA.invoke.return_value = _answer(
            {"errorType": "KeyError", "errorMessage": "secret detail"}, error=True
        )

        with caplog.at_level("ERROR"), pytest.raises(RuntimeError) as fault:
            module.handler(event("sendBackReviewItem", ARGS), None)

        assert not isinstance(fault.value, (ValueError, PermissionError))
        assert "secret detail" not in str(fault.value)
        assert "KeyError" in caplog.text
        assert "secret detail" not in caplog.text

    @pytest.mark.parametrize(
        "body",
        [
            None,
            {},
            {"queued": 0, "results": []},
            {"queued": 1, "results": [dict(_queued()["results"][0], objectKey="x")]},
            True,
        ],
    )
    def test_an_answer_without_the_documents_result_is_a_server_fault(
        self, module, body
    ):
        module._LAMBDA.invoke.return_value = _answer(body)

        with pytest.raises(RuntimeError):
            module.handler(event("sendBackReviewItem", ARGS), None)
