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

Retries are off on that client on purpose, and that is asserted on the RESOLVED
botocore config rather than on a stub — ``retries={"max_attempts": 1}`` means
*two* attempts, not one, so a test that only exercises a monkeypatched client
cannot tell the intended configuration from its opposite. botocore classifies a
read timeout as transient, so a second attempt would both outlive the function's
own Timeout (killing the labelled 504 before it can be returned) and run a
concurrent duplicate of the resolver. Invoke-level throttling, the one retry worth
keeping, is handled explicitly. These tests pin all of it, plus the template
numbers the code assumes.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

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
    """The dispatcher module, with boto3 clients stubbed so import needs no AWS.

    The stub records the kwargs each ``boto3.client`` call was made with, so the
    tests can assert on the *real* ``BotoConfig`` the module builds. Without that
    record there is nothing to assert against: the stub replaces the client
    wholesale, so ``idx._lambda`` has no ``.meta.config`` and the retry
    configuration — the thing that decides whether the labelled 504 can ever be
    returned — would be entirely untested.
    """
    import boto3

    created = {}

    def _fake_client(service, *args, **kwargs):
        created[service] = kwargs
        return object()

    monkeypatch.setattr(boto3, "client", _fake_client)
    if str(_DISPATCHER_DIR) not in sys.path:
        sys.path.insert(0, str(_DISPATCHER_DIR))
    _load_module("ddb_direct", _DISPATCHER_DIR / "ddb_direct.py")
    _load_module("validation", _DISPATCHER_DIR / "validation.py")
    mod = _load_module("index", _DISPATCHER_DIR / "index.py")
    mod._test_client_kwargs = created
    return mod


def _resolved_lambda_config(idx):
    """The dispatcher's own BotoConfig, resolved the way botocore resolves it.

    ``retries`` is asserted on the RESOLVED config rather than the literal dict,
    because botocore rewrites it: ``max_attempts`` is a RETRY count and becomes
    ``total_max_attempts = N + 1``. Reading the literal would let
    ``max_attempts: 1`` (two attempts) pass as "no retries".

    Creating the client performs no network I/O and needs no credentials —
    botocore resolves those lazily, at request time — so this neither reaches AWS
    nor depends on the environment. ``region_name`` is passed explicitly so a
    runner with no configured region cannot raise ``NoRegionError``.
    """
    import boto3

    config = idx._test_client_kwargs["lambda"]["config"]
    session = boto3.Session(region_name="us-east-1")
    return session.client("lambda", config=config).meta.config


def _total_attempts(config) -> int:
    retries = config.retries or {}
    return retries.get("total_max_attempts") or retries["max_attempts"] + 1


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


def _connect_timeout():
    return ConnectTimeoutError(endpoint_url="https://lambda.us-east-1.amazonaws.com")


def _throttle():
    return ClientError(
        {
            "Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"},
            "ResponseMetadata": {"HTTPStatusCode": 429},
        },
        "Invoke",
    )


def _service_error(status: int, code: str):
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
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


class TestConnectTimeout:
    """A connect timeout must not read as an internal error.

    connect_timeout raises ConnectTimeoutError, which is NOT a ReadTimeoutError,
    so without its own branch it reaches the generic handler and the browser gets
    500 InternalError for what is a timeout.
    """

    def test_connect_timeout_becomes_resolver_timeout(self, idx, monkeypatch):
        monkeypatch.setattr(idx, "_lambda", _FakeLambda(_connect_timeout()))
        with pytest.raises(idx.ResolverTimeout):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})

    def test_connect_timeout_is_not_retried(self, idx, monkeypatch):
        """The fault is persistent (networking), and a retry costs budget."""
        fake = _FakeLambda(_connect_timeout())
        monkeypatch.setattr(idx, "_lambda", fake)
        with pytest.raises(idx.ResolverTimeout):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert fake.calls == 1

    def test_connect_timeout_does_not_blame_the_resolver(self, idx, monkeypatch):
        """The resolver never started, so the message must not say it was slow."""
        monkeypatch.setattr(idx, "_lambda", _FakeLambda(_connect_timeout()))
        with pytest.raises(idx.ResolverTimeout) as excinfo:
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert "did not respond within" not in str(excinfo.value)

    def test_handler_maps_it_to_504_not_500(self, idx, monkeypatch):
        monkeypatch.setattr(idx, "_lambda", _FakeLambda(_connect_timeout()))
        idx.FIELD_FUNCTION_MAP["addDocumentsToTestSet"] = ARN

        resp = idx.handler(_http_event("getTestSets", {}))

        assert resp["statusCode"] == 504
        assert json.loads(resp["body"])["errors"][0]["errorType"] == "Timeout"


class TestTheClientConfigItself:
    """Asserted on the resolved config, not on a stub.

    Every other test here monkeypatches ``idx._lambda``, which replaces the
    botocore client wholesale and therefore bypasses the retry layer entirely.
    Those tests prove the explicit loop behaves, but they pass identically whether
    the client allows one attempt or ten — so the configuration that decides
    whether the labelled 504 is reachable at all needs its own assertion here.
    """

    def test_exactly_one_attempt(self, idx):
        config = _resolved_lambda_config(idx)
        assert _total_attempts(config) == 1, (
            "the resolver invoke must make exactly ONE attempt. botocore reads "
            "`max_attempts` as a RETRY count and normalizes it to "
            "`total_max_attempts = N + 1`, so `max_attempts: 1` silently means "
            "two attempts: the second starts after the read bound has already "
            "elapsed and is killed by the function Timeout before the "
            "ReadTimeoutError handler can return the labelled 504, AND it runs a "
            "concurrent duplicate of the resolver (a duplicate mutation for "
            "copyToBaseline, startTestRun, deleteTests, ...). Write "
            "`total_max_attempts: 1`."
        )

    def test_the_timeouts_are_the_ones_the_code_reasons_about(self, idx):
        config = _resolved_lambda_config(idx)
        assert config.read_timeout == idx._RESOLVER_READ_TIMEOUT_SECONDS
        assert config.connect_timeout == idx._RESOLVER_CONNECT_TIMEOUT_SECONDS

    def test_worst_case_leaves_room_for_cold_start_and_response(self, idx, resources):
        """The bound is on OUR clock; API Gateway's 29s started before init.

        Init is not free here (the idp_common layer, plus an SSM get_parameter for
        FIELD_FUNCTION_MAP at import), and it is billed against API Gateway's
        budget but not against this function's. So the invoke worst case must
        leave slack, not merely fit.
        """
        config = _resolved_lambda_config(idx)
        worst_case = _total_attempts(config) * (
            config.connect_timeout + config.read_timeout
        )
        timeout = resources["HttpApiDispatcherFunction"]["Properties"]["Timeout"]

        assert worst_case <= timeout - 5, (
            f"worst-case invoke is {worst_case}s against a {timeout}s function "
            "Timeout, leaving under 5s for cold-start init plus building the "
            "response. On a cold start the labelled 504 then loses the race to "
            "API Gateway in exactly the situation it exists for."
        )


class TestTransientInvokeErrorsAreRetried:
    """Turning botocore's retries off must not make a healthy stack fragile.

    The read-timeout bound requires ``max_attempts=1`` on the client, which also
    switches off the retries that had nothing to do with timeouts. Those are
    reissued here instead — one immediate retry, only for failures that never ran
    the resolver, so a public deployment is no more exposed to a transient Lambda
    hiccup than it was before.
    """

    def test_throttling_is_retried_once_and_can_succeed(self, idx, monkeypatch):
        fake = _FakeLambda(_throttle(), {"ok": True})
        monkeypatch.setattr(idx, "_lambda", fake)

        result = idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})

        assert result == {"ok": True}
        assert fake.calls == 2

    def test_a_lambda_service_5xx_is_retried(self, idx, monkeypatch):
        """The retry botocore used to provide for us."""
        fake = _FakeLambda(_service_error(500, "ServiceException"), {"ok": True})
        monkeypatch.setattr(idx, "_lambda", fake)

        assert idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}}) == {
            "ok": True
        }
        assert fake.calls == 2

    def test_an_unfamiliar_5xx_code_is_still_retried(self, idx, monkeypatch):
        """Classified by HTTP status, not only by a hardcoded code list."""
        fake = _FakeLambda(_service_error(503, "SomethingNewFromLambda"), {"ok": True})
        monkeypatch.setattr(idx, "_lambda", fake)

        assert idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}}) == {
            "ok": True
        }
        assert fake.calls == 2

    def test_eni_capacity_errors_are_retried(self, idx, monkeypatch):
        fake = _FakeLambda(
            _service_error(502, "ENILimitReachedException"), {"ok": True}
        )
        monkeypatch.setattr(idx, "_lambda", fake)

        assert idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}}) == {
            "ok": True
        }
        assert fake.calls == 2

    def test_retry_happens_at_most_once(self, idx, monkeypatch):
        fake = _FakeLambda(_throttle(), _throttle())
        monkeypatch.setattr(idx, "_lambda", fake)
        with pytest.raises(ClientError):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert fake.calls == 2

    def test_timeout_on_the_retry_still_reports_a_timeout(self, idx, monkeypatch):
        fake = _FakeLambda(_throttle(), _read_timeout())
        monkeypatch.setattr(idx, "_lambda", fake)
        with pytest.raises(idx.ResolverTimeout):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert fake.calls == 2

    @pytest.mark.parametrize(
        "code,status",
        [
            ("AccessDeniedException", 403),
            ("ResourceNotFoundException", 404),
            ("InvalidParameterValueException", 400),
        ],
    )
    def test_deterministic_errors_are_not_retried(self, idx, monkeypatch, code, status):
        """A retry only delays the same answer and doubles the latency."""
        fake = _FakeLambda(_service_error(status, code))
        monkeypatch.setattr(idx, "_lambda", fake)
        with pytest.raises(ClientError):
            idx._invoke_resolver(ARN, {"info": {"fieldName": "getTestSets"}})
        assert fake.calls == 1


@pytest.fixture(scope="module")
def resources() -> dict:
    """Resources from the nested api-resolvers template."""

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


class TestTemplateAgreesWithTheCode:
    """The code's assumptions are template numbers; drift breaks the guarantee."""

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
