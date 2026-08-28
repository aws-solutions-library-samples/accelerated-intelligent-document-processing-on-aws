# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The dispatcher must lose the race to API Gateway, not tie with it.

REST API Gateway abandons an integration after 29s and answers the browser
``504`` **with an empty body**. Every failure slower than that was therefore
indistinguishable from every other: the UI logged
``TestSets: Failed to load test sets: {errors:[{errorType:'HttpError',
message:'Request failed (504)'}]}`` and neither the dispatcher nor the resolver
log recorded which call was slow or why.

So the resolver invoke is bounded just under the gateway budget. When it trips,
the dispatcher owns the response: a 504 whose body names the field and the
timeout, and a log line naming the resolver ARN.

Retries are off on that client on purpose — botocore retries read timeouts, and a
second full-length attempt cannot fit in the remaining budget (for a mutation it
would also risk running the operation twice). Invoke-level throttling, the one
retry worth keeping, is handled explicitly. These tests pin all of it, plus the
template numbers the code assumes.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError, ReadTimeoutError

pytestmark = pytest.mark.unit


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "nested" / "api-resolvers").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing nested/api-resolvers")


_REPO = _find_repo_root()
_API_RESOLVERS = _REPO / "nested" / "api-resolvers"
_DISPATCHER_DIR = _API_RESOLVERS / "src" / "lambda" / "http_api_dispatcher"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def idx(monkeypatch):
    """The dispatcher module, with boto3 clients stubbed so import needs no AWS."""
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: object())
    if str(_DISPATCHER_DIR) not in sys.path:
        sys.path.insert(0, str(_DISPATCHER_DIR))
    _load_module("ddb_direct", _DISPATCHER_DIR / "ddb_direct.py")
    _load_module("validation", _DISPATCHER_DIR / "validation.py")
    return _load_module("index", _DISPATCHER_DIR / "index.py")


class _FakeLambda:
    """Stand-in for the boto3 lambda client, scripted per attempt."""

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, **kwargs):
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"Payload": io.BytesIO(json.dumps(outcome).encode("utf-8"))}


def _read_timeout():
    return ReadTimeoutError(endpoint_url="https://lambda.us-east-1.amazonaws.com")


def _throttle():
    return ClientError(
        {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}},
        "Invoke",
    )


def _http_event(field, arguments):
    return {
        "requestContext": {"http": {"method": "POST"}},
        "pathParameters": {"field": field},
        "body": json.dumps({"arguments": arguments}),
        "headers": {},
    }


ARN = "arn:aws:lambda:us-east-1:123456789012:function:x"


class TestResolverTimeout:
    def test_read_timeout_becomes_resolver_timeout(self, idx, monkeypatch):
        monkeypatch.setattr(idx, "_lambda", _FakeLambda(_read_timeout()))
        with pytest.raises(idx.ResolverTimeout):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})

    def test_no_automatic_retry_of_a_read_timeout(self, idx, monkeypatch):
        """A second full-length attempt cannot fit in the 29s budget."""
        fake = _FakeLambda(_read_timeout())
        monkeypatch.setattr(idx, "_lambda", fake)
        with pytest.raises(idx.ResolverTimeout):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert fake.calls == 1

    def test_handler_returns_504_naming_the_field(self, idx, monkeypatch):
        monkeypatch.setattr(idx, "_lambda", _FakeLambda(_read_timeout()))
        # getTestSets is an alias: it routes to the TestSetResolver, registered
        # in the map under addDocumentsToTestSet. Drive the real path.
        assert idx.FIELD_ALIASES["getTestSets"] == "addDocumentsToTestSet"
        idx.FIELD_FUNCTION_MAP["addDocumentsToTestSet"] = ARN

        resp = idx.handler(_http_event("getTestSets", {}))

        assert resp["statusCode"] == 504
        error = json.loads(resp["body"])["errors"][0]
        assert error["errorType"] == "Timeout", (
            "the UI cannot distinguish a slow operation from any other failure "
            "unless the error is labelled"
        )
        assert "getTestSets" in error["message"]

    def test_the_bound_is_inside_the_gateway_budget(self, idx):
        assert idx._RESOLVER_READ_TIMEOUT_SECONDS < 29, (
            "if the invoke bound is not strictly under the 29s REST API Gateway "
            "integration timeout, API Gateway wins the race and the browser gets "
            "a bodiless 504 again"
        )


class TestThrottleHandling:
    def test_throttling_is_retried_once_and_can_succeed(self, idx, monkeypatch):
        fake = _FakeLambda(_throttle(), {"ok": True})
        monkeypatch.setattr(idx, "_lambda", fake)

        result = idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})

        assert result == {"ok": True}
        assert fake.calls == 2

    def test_a_second_throttle_is_not_retried_again(self, idx, monkeypatch):
        fake = _FakeLambda(_throttle(), _throttle())
        monkeypatch.setattr(idx, "_lambda", fake)
        with pytest.raises(ClientError):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert fake.calls == 2

    def test_timeout_on_the_throttle_retry_still_reports_a_timeout(
        self, idx, monkeypatch
    ):
        fake = _FakeLambda(_throttle(), _read_timeout())
        monkeypatch.setattr(idx, "_lambda", fake)
        with pytest.raises(idx.ResolverTimeout):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert fake.calls == 2

    def test_other_client_errors_are_not_retried(self, idx, monkeypatch):
        fake = _FakeLambda(
            ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Invoke"
            )
        )
        monkeypatch.setattr(idx, "_lambda", fake)
        with pytest.raises(ClientError):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert fake.calls == 1


class TestTemplateAgreesWithTheCode:
    """The code's assumptions are template numbers; drift breaks the guarantee."""

    @pytest.fixture(scope="class")
    def resources(self) -> dict:
        class _CFNLoader(yaml.SafeLoader):
            pass

        def _cfn(loader, tag_suffix, node):
            tag = "!" + tag_suffix
            if isinstance(node, yaml.ScalarNode):
                return {tag: loader.construct_scalar(node)}
            if isinstance(node, yaml.SequenceNode):
                return {tag: loader.construct_sequence(node)}
            return {tag: loader.construct_mapping(node)}

        _CFNLoader.add_multi_constructor("!", _cfn)
        with open(_API_RESOLVERS / "template.yaml", "r", encoding="utf-8") as f:
            # nosec B506 - _CFNLoader subclasses yaml.SafeLoader and only adds a
            # no-op constructor for CFN intrinsic tags; input is this repo's own
            # committed template.
            return yaml.load(f, Loader=_CFNLoader)["Resources"]  # nosec B506

    def test_integration_timeout_is_explicit(self, resources):
        integration = resources["HttpApiMethod"]["Properties"]["Integration"]
        assert integration.get("TimeoutInMillis") == 29000, (
            "the 29s ceiling every /op operation must fit inside should be stated "
            "in the template, not left implicit"
        )

    def test_dispatcher_timeout_does_not_exceed_the_ceiling(self, resources, idx):
        timeout = resources["HttpApiDispatcherFunction"]["Properties"]["Timeout"]
        assert timeout <= 29, (
            f"the dispatcher's Timeout is {timeout}s but API Gateway abandons the "
            "integration at 29s, so the extra time is billed compute producing a "
            "response nobody receives"
        )
        assert idx._RESOLVER_READ_TIMEOUT_SECONDS < timeout, (
            "the invoke bound must trip before the function dies, or the "
            "labelled 504 is never returned"
        )
