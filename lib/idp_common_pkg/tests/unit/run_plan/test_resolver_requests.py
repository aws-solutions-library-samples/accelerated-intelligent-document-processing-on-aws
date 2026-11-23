# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Many documents reach the reprocess resolver in requests of bounded size.

The SDK and the MCP reprocess tool both send through ``reprocess_in_requests``.
These cases pin its rule: what each request carries, what happens when the first
request is refused, and what happens when a later one is.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from idp_common.run_plan import resolver_requests as requests
from idp_common.run_plan import steps
from idp_common.run_plan.resolver_requests import (
    DOCUMENTS_PER_REQUEST,
    FAILED,
    QUEUED,
    REFUSED,
    ResolverCallError,
    reprocess_in_requests,
)

pytestmark = pytest.mark.unit


def _keys(count: int) -> list:
    return [f"batch/doc-{n:03d}.pdf" for n in range(count)]


class Resolver:
    """Answers each request with a queued result per document, or as scripted."""

    def __init__(self, errors=None):
        self.requests = []
        self._errors = dict(errors or {})

    def send(self, arguments):
        self.requests.append(arguments)
        error = self._errors.get(len(self.requests))
        if error is not None:
            raise error
        return {
            "queued": len(arguments["objectKeys"]),
            "results": [
                {"objectKey": key, "status": QUEUED, "startStep": "extraction"}
                for key in arguments["objectKeys"]
            ],
        }


class TestTheRequests:
    def test_a_small_set_is_one_request_with_the_callers_arguments(self):
        resolver = Resolver()

        entries = reprocess_in_requests(
            resolver.send, ["a.pdf", "b.pdf"], {"startStep": "extraction"}
        )

        assert resolver.requests == [
            {"startStep": "extraction", "objectKeys": ["a.pdf", "b.pdf"]}
        ]
        assert [entry["objectKey"] for entry in entries] == ["a.pdf", "b.pdf"]

    def test_a_large_set_is_sent_in_order_in_requests_of_bounded_size(self):
        resolver = Resolver()
        keys = _keys(DOCUMENTS_PER_REQUEST * 2 + 3)

        entries = reprocess_in_requests(resolver.send, keys, {"startStep": "ocr"})

        sizes = [len(request["objectKeys"]) for request in resolver.requests]
        assert sizes == [DOCUMENTS_PER_REQUEST, DOCUMENTS_PER_REQUEST, 3]
        assert [key for r in resolver.requests for key in r["objectKeys"]] == keys
        assert all(request["startStep"] == "ocr" for request in resolver.requests)
        assert [entry["objectKey"] for entry in entries] == keys

    def test_a_set_of_exactly_one_request_is_one_request(self):
        resolver = Resolver()

        reprocess_in_requests(resolver.send, _keys(DOCUMENTS_PER_REQUEST), {})

        assert len(resolver.requests) == 1

    def test_no_documents_is_no_request(self):
        resolver = Resolver()

        assert reprocess_in_requests(resolver.send, [], {"startStep": "ocr"}) == []
        assert resolver.requests == []

    def test_the_callers_arguments_are_not_changed(self):
        arguments = {"startStep": "extraction"}

        reprocess_in_requests(Resolver().send, ["a.pdf"], arguments)

        assert arguments == {"startStep": "extraction"}

    def test_the_size_keeps_a_request_well_inside_the_resolvers_time(self):
        """The resolver answers within 60 seconds; a request is a few dozen
        documents, not hundreds."""
        assert 1 <= DOCUMENTS_PER_REQUEST <= 50


class TestARefusedOrFailedRequest:
    @pytest.mark.parametrize("error_type", ["PermissionError", "KeyError"])
    def test_an_error_from_the_first_request_is_raised_and_nothing_else_is_sent(
        self, error_type
    ):
        error = ResolverCallError(error_type, "no")
        resolver = Resolver(errors={1: error})

        with pytest.raises(ResolverCallError) as raised:
            reprocess_in_requests(resolver.send, _keys(DOCUMENTS_PER_REQUEST + 1), {})

        assert raised.value is error
        assert len(resolver.requests) == 1

    @pytest.mark.parametrize(
        ("error_type", "status"),
        [("PermissionError", REFUSED), ("ValueError", REFUSED), ("KeyError", FAILED)],
    )
    def test_a_later_request_is_reported_on_its_documents_and_the_rest_are_sent(
        self, error_type, status
    ):
        size = DOCUMENTS_PER_REQUEST
        keys = _keys(size * 2 + 1)
        resolver = Resolver(
            errors={2: ResolverCallError(error_type, "a mapping refuses")}
        )

        entries = reprocess_in_requests(resolver.send, keys, {})

        assert len(resolver.requests) == 3
        assert [entry["objectKey"] for entry in entries] == keys
        assert {entry["status"] for entry in entries[:size]} == {QUEUED}
        assert entries[size : size * 2] == [
            {
                "objectKey": key,
                "status": status,
                "startStep": None,
                "reason": error_type,
                "message": "a mapping refuses",
            }
            for key in keys[size : size * 2]
        ]
        assert entries[-1]["status"] == QUEUED

    def test_a_subclass_of_the_error_is_handled_the_same_way(self):
        class Mine(ResolverCallError):
            pass

        resolver = Resolver(errors={2: Mine("ValueError", "bad")})

        entries = reprocess_in_requests(
            resolver.send, _keys(DOCUMENTS_PER_REQUEST + 1), {}
        )

        assert entries[-1]["status"] == REFUSED

    def test_any_other_exception_is_not_swallowed(self):
        resolver = Resolver(errors={2: RuntimeError("the network")})

        with pytest.raises(RuntimeError, match="the network"):
            reprocess_in_requests(resolver.send, _keys(DOCUMENTS_PER_REQUEST + 1), {})

    @pytest.mark.parametrize("answer", [True, None, {"queued": 1}, {"results": "x"}])
    def test_an_answer_with_no_results_is_refused(self, answer):
        with pytest.raises(ValueError, match="no results"):
            reprocess_in_requests(lambda arguments: answer, ["a.pdf"], {})


class TestWhatCountsAsARefusal:
    @pytest.mark.parametrize(
        "error_type",
        [
            "PermissionError",
            "ValueError",
            "RunPlanError",
            "UnknownStep",
            "StepNotInMode",
        ],
    )
    def test_what_the_resolver_raises_to_refuse(self, error_type):
        assert ResolverCallError(error_type, "x").is_refusal is True

    @pytest.mark.parametrize("error_type", ["KeyError", "RuntimeError", "Error", ""])
    def test_a_fault_is_not_a_refusal(self, error_type):
        assert ResolverCallError(error_type, "x").is_refusal is False

    def test_the_run_plan_errors_are_named_from_their_classes(self):
        for error in (steps.RunPlanError, steps.UnknownStep, steps.StepNotInMode):
            assert error.__name__ in requests.REFUSAL_ERRORS

    def test_the_error_reads_as_its_message(self):
        error = ResolverCallError("ValueError", "BAD_ARGUMENT: x")

        assert str(error) == "BAD_ARGUMENT: x"
        assert (error.error_type, error.error_message) == (
            "ValueError",
            "BAD_ARGUMENT: x",
        )


def test_the_module_is_light():
    """The SDK imports it on every reprocess and the MCP tool at load: it brings
    in the step list and nothing else of the library."""
    before = set(sys.modules)
    for name in [m for m in sys.modules if m.startswith("idp_common.run_plan")]:
        del sys.modules[name]

    importlib.import_module("idp_common.run_plan.resolver_requests")

    loaded = {m for m in set(sys.modules) - before if m.startswith("idp_common")}
    assert loaded <= {
        "idp_common.run_plan",
        "idp_common.run_plan.steps",
        "idp_common.run_plan.resolver_requests",
    }


def test_the_reprocess_service_uses_the_same_statuses():
    from idp_common.run_plan import reprocess

    assert (reprocess.QUEUED, reprocess.REFUSED, reprocess.FAILED) == (
        QUEUED,
        REFUSED,
        FAILED,
    )
    assert reprocess.QUEUED is requests.QUEUED
