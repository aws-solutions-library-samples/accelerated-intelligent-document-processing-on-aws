# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The bulk review resolver as a dispatcher: group gate, caller, scope, shapes, errors.

The bulk semantics belong to ``idp_common.review_queues`` and are tested there. This
suite pins what the handler adds, with stub services.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from idp_common.api_adapter import ResourceNotFound
from idp_common.review_queues import ReviewValidationError
from idp_common.review_queues.bulk_models import (
    BulkOutcome,
    BulkSkipResult,
    DocumentOutcome,
    OutcomeCount,
    SendToReviewResult,
)

_FIELDS = [
    "bulkSkipAllReviews",
    "getBulkReviewJob",
    "listBulkReviewJobs",
    "sendToReview",
]
_INDEX = Path(__file__).with_name("index.py")


@pytest.fixture
def mod():
    name = "bulk_review_resolver_index_under_test"
    spec = importlib.util.spec_from_file_location(name, _INDEX)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module._SERVICES = SimpleNamespace(bulk=MagicMock(), send=MagicMock())
    yield module
    sys.modules.pop(name, None)


def _event(field, args=None, *, groups=("Admin",), email="Admin@Example.com"):
    claims = {"email": email, "sub": "sub-1", "cognito:groups": list(groups)}
    return {
        "info": {"fieldName": field},
        "arguments": args or {},
        "identity": {"username": "u1", "claims": claims},
    }


SELECTED = {
    "input": {"mode": "SELECTED", "objectKeys": ["a"], "reason": "Duplicate document"}
}
PROFILE = {
    "input": {
        "mode": "PROFILE",
        "profile": "claims",
        "submittedFrom": "2026-01-01T00:00:00.000Z",
        "submittedTo": "2026-01-08T00:00:00.000Z",
        "reason": "Duplicate document",
        "dryRun": True,
    }
}


@pytest.mark.unit
class TestGate:
    def test_the_field_set_is_pinned(self, mod):
        assert sorted(mod._FIELDS) == sorted(_FIELDS)
        assert all(mod._FIELD_GROUPS[f] == {"Admin", "Reviewer"} for f in _FIELDS)

    @pytest.mark.parametrize("field", _FIELDS)
    @pytest.mark.parametrize("group", ["Viewer", "Author", "Annotator"])
    def test_other_groups_are_refused_before_any_work(self, mod, field, group):
        with pytest.raises(PermissionError, match="^ROLE"):
            mod.handler(_event(field, SELECTED, groups=(group,)), None)
        mod._SERVICES.bulk.submit.assert_not_called()

    def test_an_unknown_field_is_a_bad_argument(self, mod):
        with pytest.raises(ValueError, match="unsupported"):
            mod.handler(_event("nope"), None)


@pytest.mark.unit
class TestBulkSkip:
    def test_selected_mode_reaches_the_service_with_the_verified_caller(self, mod):
        mod._SERVICES.bulk.submit.return_value = BulkSkipResult(
            dry_run=False,
            run_mode="SYNC",
            sync_limit=50,
            counts=[OutcomeCount(outcome=BulkOutcome.SKIPPED, count=1)],
            results=[DocumentOutcome(object_key="a", outcome=BulkOutcome.SKIPPED)],
            job_id="brj-1",
        )
        out = mod.handler(_event("bulkSkipAllReviews", SELECTED), None)
        caller, request = mod._SERVICES.bulk.submit.call_args.args
        assert (caller.email, caller.username, caller.is_admin) == (
            "admin@example.com",
            "u1",
            True,
        )
        assert request.object_keys == ["a"] and request.resubmit is True
        assert out["runMode"] == "SYNC" and out["jobId"] == "brj-1"
        assert out["results"] == [
            {"objectKey": "a", "outcome": "SKIPPED", "kind": "none", "detail": None}
        ]

    def test_an_out_of_scope_profile_is_refused_before_the_service(self, mod):
        with patch.object(mod, "resolve_allowed_config_versions", return_value=["x"]):
            with pytest.raises(PermissionError):
                mod.handler(
                    _event("bulkSkipAllReviews", PROFILE, groups=("Reviewer",)), None
                )
        mod._SERVICES.bulk.submit.assert_not_called()

    def test_a_scope_lookup_failure_fails_closed(self, mod):
        with patch.object(
            mod, "resolve_allowed_config_versions", side_effect=RuntimeError("ddb")
        ):
            with pytest.raises(PermissionError, match="OUT_OF_SCOPE"):
                mod.handler(
                    _event("bulkSkipAllReviews", PROFILE, groups=("Reviewer",)), None
                )

    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"input": {"mode": "SELECTED", "reason": "Duplicate document"}},
            {"input": {**SELECTED["input"], "reason": "nope"}},
            {"input": {**SELECTED["input"], "extra": 1}},
        ],
    )
    def test_bad_input_is_a_value_error(self, mod, args):
        with pytest.raises(ValueError, match="BAD_ARGUMENT"):
            mod.handler(_event("bulkSkipAllReviews", args), None)

    def test_a_service_validation_error_is_a_value_error(self, mod):
        mod._SERVICES.bulk.submit.side_effect = ReviewValidationError(
            "BAD_ARGUMENT", "x"
        )
        with pytest.raises(ValueError):
            mod.handler(_event("bulkSkipAllReviews", SELECTED), None)


@pytest.mark.unit
class TestJobs:
    def test_a_job_that_is_not_yours_is_not_found(self, mod):
        mod._SERVICES.bulk.get_job.return_value = None
        with pytest.raises(ResourceNotFound):
            mod.handler(_event("getBulkReviewJob", {"jobId": "brj-x"}), None)

    def test_a_missing_job_id_is_a_bad_argument(self, mod):
        with pytest.raises(ValueError):
            mod.handler(_event("getBulkReviewJob", {}), None)


@pytest.mark.unit
class TestSendToReview:
    def test_an_unknown_key_is_an_in_band_outcome(self, mod):
        mod._SERVICES.send.send.return_value = SendToReviewResult(
            counts=[OutcomeCount(outcome=BulkOutcome.NOT_PERMITTED, count=1)],
            results=[
                DocumentOutcome(object_key="__x__", outcome=BulkOutcome.NOT_PERMITTED)
            ],
        )
        out = mod.handler(
            _event("sendToReview", {"objectKeys": ["__x__"], "reason": "Spot check"}),
            None,
        )
        assert out["results"][0]["outcome"] == "NOT_PERMITTED"

    def test_a_skip_reason_is_not_a_send_reason(self, mod):
        with pytest.raises(ValueError):
            mod.handler(
                _event(
                    "sendToReview",
                    {"objectKeys": ["a"], "reason": "Duplicate document"},
                ),
                None,
            )
