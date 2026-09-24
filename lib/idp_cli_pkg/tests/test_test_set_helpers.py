# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the Test Studio test-set helpers in `idp_cli.cli`.

Six module-level private functions sit between `idp-cli process --test-set` and the
deployed stack:

* `_invoke_test_set_resolver` finds the API-resolver Lambda by name and invokes it to
  make the backend notice a newly-uploaded test set folder.
* `_invoke_test_runner` finds a second Lambda the same way and invokes it with the run
  parameters, returning the test run it started.
* `_get_test_set_document_ids` lists the test set's `input/` prefix and synthesises the
  document ids the monitor will watch for.
* `_manifest_has_baselines` is a predicate over a manifest file.
* `_create_test_set_from_manifest` builds a test set folder from a manifest.
* `_process_test_set` chains the first three together and assembles the batch result
  the monitoring code consumes.

Two things shaped these tests. The first is that **the Lambda payload is the contract
with the deployed backend**: a renamed key, a string where an integer belongs or an
omitted field is not a crash, it is a run that quietly ignores the parameter it was
given — a `--config-revision` that is not pinned, or a `--number-of-files` that is
ignored so the whole test set is processed. So the payloads are asserted as parsed
JSON, field by field, including which optional fields are *absent*.

The second is that all four Lambda failure modes are handled differently by the two
invoking helpers, and none of them was exercised before. A non-200 `StatusCode`, an
`errorMessage` in an otherwise-successful response, a payload that is not JSON at all,
and a successful-but-empty result each get their own test against each helper. The
resolver degrades to a warning for all of them because its work is optional; the
runner raises, because a test run that did not start must not be monitored as though
it had. Where the handling is missing — `FunctionError` is never read, and an empty
result reaches a bare subscript — that is pinned as the current behaviour with a
docstring saying what the user sees.

The Lambda client is a hand-written fake rather than `moto`, because the interesting
thing is the request this code sends and the shape of the response it is handed, and
`moto` cannot produce a response from function code without running a container.
Everything S3 (`_get_test_set_document_ids`, `_create_test_set_from_manifest`) runs
against a real `moto` bucket and the objects are read back off it, except where the
point of the test is a response `moto` will not generate — a truncated listing.
"""

import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

TEST_SET_BUCKET = "idp-test-set-bucket"
RESOLVER_FUNCTION = "IDP-APIRESOLVERSTACK-1A2B-TestSetResolverFunction-xyz"
RUNNER_FUNCTION = "IDP-APIRESOLVERSTACK-1A2B-TestRunnerFunction-xyz"


class FakeLambdaPaginator:
    """A `list_functions` paginator that yields the pages it was constructed with."""

    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        for page in self._pages:
            yield {"Functions": [{"FunctionName": name} for name in page]}


class FakeLambdaClient:
    """A Lambda client that lists the function names given and returns canned responses.

    `invoke` records the keyword arguments it was called with, so a test can read the
    `FunctionName` and the `Payload` back and assert on the parsed JSON rather than on
    a string. `responses` is consumed in order; the last one repeats, so a test that
    only cares about one invocation does not have to count them.
    """

    def __init__(self, pages, responses=None):
        self._pages = pages
        self._responses = list(responses or [])
        self.invocations = []

    def get_paginator(self, operation_name):
        assert operation_name == "list_functions", operation_name
        return FakeLambdaPaginator(self._pages)

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)
        if not self._responses:
            raise AssertionError("invoke() called with no canned response left")
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    @property
    def sent_payload(self):
        """The JSON body of the single invocation, parsed."""
        assert len(self.invocations) == 1, self.invocations
        return json.loads(self.invocations[0]["Payload"])


def lambda_response(payload, status_code=200, function_error=None):
    """Build a response in the shape `Lambda.invoke` returns.

    `payload` is serialised to JSON unless it is already `bytes`, which is how the
    "payload is not JSON" tests inject a body botocore would happily hand back.
    """
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    response = {"StatusCode": status_code, "Payload": io.BytesIO(body)}
    if function_error is not None:
        response["FunctionError"] = function_error
    return response


def patched_boto3(fake_lambda=None, fake_s3=None):
    """Patch `idp_cli.cli.boto3.client` to hand out fakes for named services only.

    Anything not faked is built by the real `boto3`, so a test can put a fake Lambda
    client in front of a genuine moto-backed S3 client in the same call.
    """
    real_client = boto3.client
    fakes = {}
    if fake_lambda is not None:
        fakes["lambda"] = fake_lambda
    if fake_s3 is not None:
        fakes["s3"] = fake_s3

    def _client(service_name, **kwargs):
        if service_name in fakes:
            return fakes[service_name]
        return real_client(service_name, **kwargs)

    return patch("idp_cli.cli.boto3.client", side_effect=_client)


def all_keys(s3_client, bucket, prefix=""):
    """Every key under `prefix`, read back through the paginator.

    A bare `list_objects_v2` read-back stops at 1000 keys just as the code under test
    used to, so a test whose fixture is deliberately larger than one page cannot use one
    to check its own result: the assertion would be measured through the same ceiling it
    exists to catch.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    return {
        obj["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for obj in page.get("Contents", [])
    }


def resources(**overrides):
    """The `resources` dict the helpers are given, with `TestSetBucket` by default."""
    base = {"TestSetBucket": TEST_SET_BUCKET}
    base.update(overrides)
    return base


# ================================================================================
# _invoke_test_set_resolver
# ================================================================================


@pytest.mark.unit
def test_resolver_sends_the_get_test_sets_resolver_event(capsys):
    """The payload is an AppSync-shaped resolver event, and its exact keys matter.

    The deployed Lambda dispatches on `info.fieldName`, so a renamed or nested-wrong
    key produces a successful invocation that does nothing at all — the test set is
    uploaded but never registered in the tracking table, and the CLI still says the
    auto-detection completed. Asserting the parsed payload structure is the only way to
    catch that.
    """
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient([[RESOLVER_FUNCTION]], [lambda_response({"testSets": []})])

    with patched_boto3(fake_lambda=fake):
        assert _invoke_test_set_resolver("IDP", "set1", None, resources()) is None

    assert fake.invocations[0]["FunctionName"] == RESOLVER_FUNCTION
    assert fake.sent_payload == {"info": {"fieldName": "getTestSets"}, "arguments": {}}


@pytest.mark.unit
def test_resolver_reports_success_and_names_the_test_set(capsys):
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient([[RESOLVER_FUNCTION]], [lambda_response({"testSets": []})])

    with patched_boto3(fake_lambda=fake):
        _invoke_test_set_resolver("IDP", "fcc example", None, resources())

    output = capsys.readouterr().out
    assert "Auto-detecting test set: fcc example" in output
    assert "✓ Test set auto-detection completed" in output


@pytest.mark.unit
def test_resolver_matches_a_truncated_nested_stack_segment(capsys):
    """The match is the stack prefix plus the function fragment, deliberately loose.

    CloudFormation truncates long logical ids in physical resource names, so the nested
    stack segment can appear as `APIRESOLVE`, `APIRESOLVER` or the full
    `APIRESOLVERSTACK`. Matching the full segment would miss the truncated forms and
    silently skip registration on exactly the stacks whose names are long. All three
    spellings are checked here because that is the property the comment in the source
    claims, and nothing else asserted it.
    """
    from idp_cli.cli import _invoke_test_set_resolver

    for name in (
        "IDP-APIRESOLVE-TestSetResolverFunction-abc",
        "IDP-APIRESOLVER-TestSetResolverFunction-abc",
        "IDP-APIRESOLVERSTACK-TestSetResolverFunction-abc",
    ):
        fake = FakeLambdaClient([[name]], [lambda_response({})])
        with patched_boto3(fake_lambda=fake):
            _invoke_test_set_resolver("IDP", "set1", None, resources())
        assert fake.invocations[0]["FunctionName"] == name, name


@pytest.mark.unit
def test_resolver_ignores_another_stacks_function(capsys):
    """A same-named function in a different stack must not be invoked.

    One account commonly holds several IDP stacks. Invoking the wrong stack's resolver
    would register the test set against the wrong deployment, so the stack-name prefix
    is load-bearing and its absence is asserted by there being no invocation at all.
    """
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient(
        [["OTHER-APIRESOLVERSTACK-TestSetResolverFunction-abc"]], [lambda_response({})]
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_set_resolver("IDP", "set1", None, resources())

    assert fake.invocations == []
    assert "TestSetResolverFunction not found" in capsys.readouterr().out


@pytest.mark.unit
def test_resolver_finds_a_function_on_a_later_page(capsys):
    """Pagination is real: an account with many functions returns several pages.

    Without the paginator the function would be found only if it happened to fall in
    the first 50 functions of the account, which is how this kind of lookup works
    locally and fails in a busy account.
    """
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient(
        [["unrelated-one", "unrelated-two"], ["unrelated-three", RESOLVER_FUNCTION]],
        [lambda_response({})],
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_set_resolver("IDP", "set1", None, resources())

    assert fake.invocations[0]["FunctionName"] == RESOLVER_FUNCTION


@pytest.mark.unit
def test_resolver_missing_function_warns_and_returns_without_invoking(capsys):
    """Registration is best-effort, so a missing resolver is a warning, not a failure."""
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient([[]], [lambda_response({})])

    with patched_boto3(fake_lambda=fake):
        assert _invoke_test_set_resolver("IDP", "set1", None, resources()) is None

    assert fake.invocations == []
    output = capsys.readouterr().out
    assert (
        "Warning: TestSetResolverFunction not found, skipping auto-detection" in output
    )
    assert "✓" not in output


@pytest.mark.unit
def test_resolver_reports_a_function_error_payload_as_a_warning(capsys):
    """AWS returns StatusCode 200 for a handler that raised; the payload carries the error.

    This is the failure mode that actually happens — a permissions error or a bad
    environment variable in the resolver — and the response looks entirely successful
    at the HTTP level. The error type and message both have to reach the user or the
    only symptom is a test set that never appears in the UI.
    """
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient(
        [[RESOLVER_FUNCTION]],
        [
            lambda_response(
                {
                    "errorMessage": "User is not authorized to perform: dynamodb:Scan",
                    "errorType": "AccessDeniedException",
                },
                function_error="Unhandled",
            )
        ],
    )

    with patched_boto3(fake_lambda=fake):
        assert _invoke_test_set_resolver("IDP", "set1", None, resources()) is None

    output = capsys.readouterr().out
    assert "Warning: Test set resolver failed (AccessDeniedException)" in output
    assert "dynamodb:Scan" in output
    assert "✓ Test set auto-detection completed" not in output


@pytest.mark.unit
def test_resolver_names_the_error_type_as_unknown_when_absent(capsys):
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient(
        [[RESOLVER_FUNCTION]], [lambda_response({"errorMessage": "boom"})]
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_set_resolver("IDP", "set1", None, resources())

    assert "Test set resolver failed (Unknown): boom" in capsys.readouterr().out


@pytest.mark.unit
def test_resolver_reports_a_non_200_status_as_a_warning(capsys):
    """A throttled or failed invocation warns and names the status code."""
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient(
        [[RESOLVER_FUNCTION]], [lambda_response({}, status_code=502)]
    )

    with patched_boto3(fake_lambda=fake):
        assert _invoke_test_set_resolver("IDP", "set1", None, resources()) is None

    output = capsys.readouterr().out
    assert "Warning: Test set resolver invocation failed with status 502" in output
    assert "✓ Test set auto-detection completed" not in output


@pytest.mark.unit
def test_resolver_survives_a_payload_that_is_not_json(capsys):
    """A non-JSON body is caught by the broad handler and degrades to a warning.

    This happens when a Lambda is misconfigured badly enough to return an HTML error
    page, and the important property is that it does not propagate: the resolver is
    called at the end of `generate-manifest --test-set` after the files are already
    uploaded, so raising here would report a failure for an upload that succeeded.
    """
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient(
        [[RESOLVER_FUNCTION]], [lambda_response(b"<html>gateway timeout</html>")]
    )

    with patched_boto3(fake_lambda=fake):
        assert _invoke_test_set_resolver("IDP", "set1", None, resources()) is None

    assert "Warning: Could not auto-detect test set:" in capsys.readouterr().out


@pytest.mark.unit
def test_resolver_treats_an_empty_result_as_success(capsys):
    """An empty `{}` payload with status 200 is reported as a completed auto-detection.

    Nothing inspects the result, so "the Lambda ran" is the whole of the success test.
    Recorded rather than judged: the resolver's job is a side effect in the tracking
    table, and the CLI has no way to check it from the response. The consequence is
    that a resolver which ran but registered nothing reads as a success here, and the
    missing test set is only noticed later by whoever looks for it in the UI.
    """
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient([[RESOLVER_FUNCTION]], [lambda_response({})])

    with patched_boto3(fake_lambda=fake):
        _invoke_test_set_resolver("IDP", "set1", None, resources())

    assert "✓ Test set auto-detection completed" in capsys.readouterr().out


@pytest.mark.unit
def test_resolver_builds_its_client_in_the_requested_region():
    """`--region` has to reach the Lambda client or the lookup runs in the wrong region.

    A function list from the wrong region is empty, which this helper reports as
    "not found" — a silently skipped registration rather than an error.
    """
    from idp_cli.cli import _invoke_test_set_resolver

    fake = FakeLambdaClient([[RESOLVER_FUNCTION]], [lambda_response({})])

    with patched_boto3(fake_lambda=fake) as patched:
        _invoke_test_set_resolver("IDP", "set1", "eu-west-2", resources())

    assert patched.call_args_list[0].args[0] == "lambda"
    assert patched.call_args_list[0].kwargs == {"region_name": "eu-west-2"}


# ================================================================================
# _invoke_test_runner
# ================================================================================


@pytest.mark.unit
def test_runner_sends_only_the_test_set_id_when_nothing_else_is_given(capsys):
    """The minimal payload, asserted whole — including which keys are absent.

    `==` on the parsed payload rather than a series of `in` checks, because an
    accidentally-included `numberOfFiles: null` or `configVersion: ""` is exactly the
    kind of thing the backend would interpret rather than ignore.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]],
        [lambda_response({"testRunId": "run-1", "filesCount": 4})],
    )

    with patched_boto3(fake_lambda=fake):
        result = _invoke_test_runner("IDP", "set1", None, None, resources())

    assert result == {"testRunId": "run-1", "filesCount": 4}
    assert fake.invocations[0]["FunctionName"] == RUNNER_FUNCTION
    assert fake.sent_payload == {"arguments": {"input": {"testSetId": "set1"}}}
    assert "✓ Test run started: run-1" in capsys.readouterr().out


@pytest.mark.unit
def test_runner_includes_every_optional_field_when_given(capsys):
    """Context, file limit, config version and config revision all reach the payload.

    `configRevision` is coerced with `int()`, and the type matters: the backend writes
    it into the run record and a string there means the run records a revision it did
    not pin. The assertion checks the value *and* that it is an `int`, because
    `"7" == 7` is false but a JSON round trip hides which one was sent.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]],
        [lambda_response({"testRunId": "run-2", "filesCount": 2})],
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_runner(
            "IDP",
            "set1",
            "quarterly regression",
            "us-east-1",
            resources(),
            number_of_files=2,
            config_version="v3",
            config_revision="7",
        )

    sent = fake.sent_payload["arguments"]["input"]
    assert sent == {
        "testSetId": "set1",
        "context": "quarterly regression",
        "numberOfFiles": 2,
        "configVersion": "v3",
        "configRevision": 7,
    }
    assert isinstance(sent["configRevision"], int)
    assert "Limiting to 2 files" in capsys.readouterr().out


@pytest.mark.unit
def test_runner_omits_optional_fields_that_are_none(capsys):
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]], [lambda_response({"testRunId": "r", "filesCount": 1})]
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_runner(
            "IDP",
            "set1",
            None,
            None,
            resources(),
            number_of_files=None,
            config_version=None,
            config_revision=None,
        )

    assert set(fake.sent_payload["arguments"]["input"]) == {"testSetId"}


@pytest.mark.unit
def test_runner_sends_revision_zero_but_drops_config_version_empty_string(capsys):
    """The two optional config fields use different guards, and it is observable.

    `configRevision` is gated on `is not None`, so revision 0 is sent. `configVersion`
    is gated on truthiness, so an empty string is dropped. That asymmetry is correct
    for these two — revision 0 is a real revision, an empty version name is not — but
    it is the sort of thing that gets "tidied" into one style, so it is pinned.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]], [lambda_response({"testRunId": "r", "filesCount": 1})]
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_runner(
            "IDP",
            "set1",
            "",
            None,
            resources(),
            config_version="",
            config_revision=0,
        )

    sent = fake.sent_payload["arguments"]["input"]
    assert sent == {"testSetId": "set1", "configRevision": 0}
    assert "configVersion" not in sent
    assert "context" not in sent, "an empty context is dropped, not sent as empty"


@pytest.mark.unit
def test_runner_sends_zero_files_without_saying_so(capsys):
    """`--number-of-files 0` is sent to the backend but not reported on screen.

    The payload guard is `is not None` and the print guard is truthiness, so 0 reaches
    the Lambda while the "Limiting to N files" line is suppressed. Whatever the backend
    does with 0, the user is not told that a limit was applied — pinned because the
    two guards disagreeing is easy to "fix" in the wrong direction.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]], [lambda_response({"testRunId": "r", "filesCount": 0})]
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_runner("IDP", "set1", None, None, resources(), number_of_files=0)

    assert fake.sent_payload["arguments"]["input"]["numberOfFiles"] == 0
    assert "Limiting to" not in capsys.readouterr().out


@pytest.mark.unit
def test_runner_raises_when_its_lambda_is_not_deployed(capsys):
    """Unlike the resolver, a missing test runner is fatal — and it must be.

    There is no fallback: if the runner is not there no test run exists, and carrying
    on would monitor a run id that was never created.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient([["IDP-APIRESOLVERSTACK-SomeOtherFunction-abc"]])

    with patched_boto3(fake_lambda=fake):
        with pytest.raises(
            ValueError, match="TestRunnerFunction not found for stack IDP"
        ):
            _invoke_test_runner("IDP", "set1", None, None, resources())

    assert fake.invocations == []


@pytest.mark.unit
def test_runner_raises_on_a_function_error_payload(capsys):
    """A handler that raised gives StatusCode 200 and an `errorMessage` in the payload.

    Both the error type and the message have to survive into the exception, because
    this is what the user sees when a test run fails to start — most often a missing
    config version or a permissions gap in the runner's role.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]],
        [
            lambda_response(
                {
                    "errorMessage": "Config version v9 does not exist",
                    "errorType": "ValidationError",
                },
                function_error="Unhandled",
            )
        ],
    )

    with patched_boto3(fake_lambda=fake):
        with pytest.raises(ValueError) as caught:
            _invoke_test_runner("IDP", "set1", None, None, resources())

    assert "Test runner execution failed (ValidationError)" in str(caught.value)
    assert "Config version v9 does not exist" in str(caught.value)


@pytest.mark.unit
def test_runner_raises_on_a_non_200_status(capsys):
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]], [lambda_response({"ok": True}, status_code=429)]
    )

    with patched_boto3(fake_lambda=fake):
        with pytest.raises(
            ValueError, match="Test runner invocation failed with status 429"
        ):
            _invoke_test_runner("IDP", "set1", None, None, resources())


@pytest.mark.unit
def test_runner_raises_a_raw_json_error_on_an_unparseable_payload(capsys):
    """DEFECT (pinned, not fixed): a non-JSON payload surfaces as a `JSONDecodeError`.

    `json.loads(response["Payload"].read())` is not guarded, so a body that is not JSON
    — a gateway error page, or a truncated response — propagates as
    `Expecting value: line 1 column 1 (char 0)`. The calling command catches it and
    prints it as its own error, so the user is told the JSON is malformed with no
    indication that it came from the test runner Lambda or which stack it was reached
    through. The verdict is right (the run does not proceed); the diagnosis is not.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]], [lambda_response(b"<html>502 Bad Gateway</html>")]
    )

    with patched_boto3(fake_lambda=fake):
        with pytest.raises(json.JSONDecodeError):
            _invoke_test_runner("IDP", "set1", None, None, resources())


@pytest.mark.unit
def test_runner_raises_a_keyerror_on_a_successful_but_empty_result(capsys):
    """DEFECT (pinned, not fixed): an empty `{}` result fails with `KeyError: 'testRunId'`.

    A 200 response whose payload carries neither `errorMessage` nor `testRunId` passes
    both guards and reaches `result['testRunId']` in the success print. That is a
    plausible response from a runner Lambda that returned early — and the user gets a
    bare `KeyError` naming an internal field, rather than being told the test run did
    not start. `filesCount` has the same exposure one caller up, in
    `_process_test_set`.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient([[RUNNER_FUNCTION]], [lambda_response({})])

    with patched_boto3(fake_lambda=fake):
        with pytest.raises(KeyError, match="testRunId"):
            _invoke_test_runner("IDP", "set1", None, None, resources())


@pytest.mark.unit
def test_runner_ignores_the_function_error_header_on_its_own(capsys):
    """DEFECT (pinned, not fixed): `FunctionError` in the response is never consulted.

    The only error signal this helper reads is an `errorMessage` key in the payload.
    `FunctionError` is the field AWS sets on the response itself to say the handler
    failed, and it is ignored. For a handler that raised an exception the two travel
    together, so the common case is caught by the payload check — which is why this is
    narrow rather than serious. What slips through is a handler that returns an error
    shape of its own design while Lambda still flags the invocation, and that is
    reported here as a started test run.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RUNNER_FUNCTION]],
        [
            lambda_response(
                {"testRunId": "run-3", "filesCount": 0, "error": "internal"},
                function_error="Unhandled",
            )
        ],
    )

    with patched_boto3(fake_lambda=fake):
        result = _invoke_test_runner("IDP", "set1", None, None, resources())

    assert result["testRunId"] == "run-3"
    assert "✓ Test run started: run-3" in capsys.readouterr().out


@pytest.mark.unit
def test_runner_finds_a_function_on_a_later_page(capsys):
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [["a", "b"], ["c", RUNNER_FUNCTION]],
        [lambda_response({"testRunId": "r", "filesCount": 1})],
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_runner("IDP", "set1", None, None, resources())

    assert fake.invocations[0]["FunctionName"] == RUNNER_FUNCTION


@pytest.mark.unit
def test_runner_does_not_pick_up_the_resolver_function(capsys):
    """Both Lambdas share the stack prefix, so only the fragment separates them.

    With both deployed, the runner must choose `TestRunnerFunction`. Choosing the
    resolver instead would invoke `getTestSets` with a run payload it ignores, and the
    CLI would then read `testRunId` out of a list of test sets.
    """
    from idp_cli.cli import _invoke_test_runner

    fake = FakeLambdaClient(
        [[RESOLVER_FUNCTION, RUNNER_FUNCTION]],
        [lambda_response({"testRunId": "r", "filesCount": 1})],
    )

    with patched_boto3(fake_lambda=fake):
        _invoke_test_runner("IDP", "set1", None, None, resources())

    assert fake.invocations[0]["FunctionName"] == RUNNER_FUNCTION


# ================================================================================
# _get_test_set_document_ids
# ================================================================================


@pytest.mark.unit
def test_document_ids_are_the_batch_id_joined_to_each_input_filename():
    """`<batch_id>/<filename>` is the id shape the tracking table is keyed on.

    Get this wrong and monitoring waits on ids that will never appear, which presents
    as a run that never finishes rather than as an error. The test set's own
    `.uploading` marker lives outside the `input/` prefix, and an object is placed there
    to prove the listing does not pick it up — if it did, the monitor would wait
    forever on a document id that is not a document.
    """
    from idp_cli.cli import _get_test_set_document_ids

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/a.pdf", Body=b"x")
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/b.pdf", Body=b"x")
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/.uploading", Body=b"x")
        s3.put_object(
            Bucket=TEST_SET_BUCKET, Key="set1/baseline/a.pdf/result.json", Body=b"{}"
        )

        ids = _get_test_set_document_ids("IDP", "set1", "run-9", None, resources())

    assert sorted(ids) == ["run-9/a.pdf", "run-9/b.pdf"]


@pytest.mark.unit
def test_document_ids_skips_a_folder_marker_key():
    """A zero-byte key ending in `/` is a console-created folder, not a document."""
    from idp_cli.cli import _get_test_set_document_ids

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/", Body=b"")
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/a.pdf", Body=b"x")

        ids = _get_test_set_document_ids("IDP", "set1", "run-9", None, resources())

    assert ids == ["run-9/a.pdf"]


@pytest.mark.unit
def test_document_ids_flattens_a_nested_key_to_its_basename():
    """A key in a subdirectory of `input/` loses its path, so two can collide.

    `filename = key.split("/")[-1]` keeps only the basename, so
    `input/2024/jan.pdf` and `input/2025/jan.pdf` both become `<batch>/jan.pdf`. The
    uploader in `generate-manifest` never creates subdirectories under `input/`, so
    this is only reachable for a test set folder assembled by hand or by the UI — which
    is why it is recorded here rather than reported as a live bug.
    """
    from idp_cli.cli import _get_test_set_document_ids

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/2024/jan.pdf", Body=b"x")
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/2025/jan.pdf", Body=b"x")

        ids = _get_test_set_document_ids("IDP", "set1", "run-9", None, resources())

    assert ids == ["run-9/jan.pdf", "run-9/jan.pdf"]


@pytest.mark.unit
def test_document_ids_raises_when_the_test_set_bucket_is_missing():
    """The bucket lookup failure is raised, not warned about, and that is deliberate.

    Without the bucket there is nothing to list, and returning an empty list would be
    indistinguishable from an empty test set.
    """
    from idp_cli.cli import _get_test_set_document_ids

    with pytest.raises(ValueError, match="TestSetBucket not found in stack resources"):
        _get_test_set_document_ids("IDP", "set1", "run-9", None, {})


@pytest.mark.unit
def test_document_ids_returns_empty_for_an_empty_prefix(capsys):
    """No objects under `input/` yields an empty list and no warning."""
    from idp_cli.cli import _get_test_set_document_ids

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket=TEST_SET_BUCKET
        )
        ids = _get_test_set_document_ids("IDP", "set1", "run-9", None, resources())

    assert ids == []
    assert "Warning" not in capsys.readouterr().out


@pytest.mark.unit
def test_document_ids_degrades_to_empty_on_an_s3_error(capsys):
    """A listing failure warns and returns `[]` rather than failing the run.

    The test run has already been started by this point, so raising would abandon a
    run that is genuinely executing. Losing monitoring is the lesser harm — but it is
    a real cost, so the warning has to be visible and the empty result has to be
    understood by the caller as "unknown", not "no documents".
    """
    from idp_cli.cli import _get_test_set_document_ids

    with mock_aws():
        ids = _get_test_set_document_ids(
            "IDP", "set1", "run-9", None, {"TestSetBucket": "no-such-bucket"}
        )

    assert ids == []
    assert (
        "Warning: Could not get document IDs from test set:" in capsys.readouterr().out
    )


@pytest.mark.unit
def test_document_ids_covers_a_test_set_over_one_thousand_files(api_calls):
    """Every input document gets an id, past the 1000-key ceiling on one listing.

    `list_objects_v2` returns at most 1000 keys per response and reports the rest
    through `NextContinuationToken`. Reading a single response gave ids for the first
    1000 documents only, so the monitor reported the run complete once those finished
    while the remainder were still being processed, and any evaluation computed from it
    was scored on a subset without saying so — a short pass on exactly the largest test
    sets, which are the ones a regression run cares about most.

    **The fixture has to exceed one page or it cannot tell the fix from the defect**: at
    1000 objects or fewer a single listing returns everything and both versions agree.
    The second page is produced by `moto` itself rather than by a hand-written double —
    1001 real objects in a real bucket, which is where the truncation semantics are
    authoritative. Measured on this fixture, `moto` answers an unpaginated
    `list_objects_v2` with `KeyCount` 1000 and `IsTruncated` true, and the paginator
    with 1001 keys over two pages.

    Both halves of the property are asserted: the count and the presence of a document
    that can only come from the second page, *and* — from the recorded API calls — that
    a second `ListObjectsV2` was actually issued carrying a continuation token. Without
    the second half a listing that happened to return everything in one response would
    pass this test while leaving the ceiling in place.
    """
    from idp_cli.cli import _get_test_set_document_ids

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        for i in range(1001):
            s3.put_object(
                Bucket=TEST_SET_BUCKET, Key=f"set1/input/doc{i:05d}.pdf", Body=b"x"
            )
        before_call = len(api_calls)

        ids = _get_test_set_document_ids("IDP", "set1", "run-9", None, resources())

        made_by_the_helper = api_calls[before_call:]

    assert len(ids) == 1001
    # Keys sort lexically, so the last document is only reachable on page two.
    assert "run-9/doc01000.pdf" in ids
    assert ids[0] == "run-9/doc00000.pdf"

    listings = [call for call in made_by_the_helper if call.operation == "ListObjectsV2"]
    assert len(listings) == 2, [call.operation for call in made_by_the_helper]
    assert "ContinuationToken" not in listings[0].params
    assert listings[0].params["Prefix"] == "set1/input/"
    assert listings[1].params.get("ContinuationToken"), (
        "the second page must be fetched with the token the first one returned"
    )


# ================================================================================
# _manifest_has_baselines
# ================================================================================


@pytest.mark.unit
def test_manifest_has_baselines_is_true_when_every_row_has_one(tmp_path):
    from idp_cli.cli import _manifest_has_baselines

    manifest = tmp_path / "m.csv"
    manifest.write_text(
        "document_path,baseline_source\ns3://b/a.pdf,s3://x/a/\ns3://b/c.pdf,s3://x/c/\n"
    )

    assert _manifest_has_baselines(str(manifest)) is True


@pytest.mark.unit
def test_manifest_has_baselines_is_true_when_only_some_rows_have_one(tmp_path):
    """The predicate is "any", not "all", and callers should read it that way.

    A manifest where one document in fifty has a baseline reads as a manifest with
    baselines, so the evaluation branch is taken for the whole batch and the other
    forty-nine are processed with nothing to compare against. That is arguably the
    right default — there is something to evaluate — but it is not what the name
    suggests to a reader, so it is pinned explicitly.
    """
    from idp_cli.cli import _manifest_has_baselines

    manifest = tmp_path / "m.csv"
    manifest.write_text(
        "document_path,baseline_source\ns3://b/a.pdf,s3://x/a/\ns3://b/c.pdf,\n"
    )

    assert _manifest_has_baselines(str(manifest)) is True


@pytest.mark.unit
def test_manifest_has_baselines_is_false_when_the_column_is_empty(tmp_path):
    """This is the shape `generate-manifest` writes with no `--baseline-dir`.

    Every cell is empty, which `pandas` reads as NaN, so the predicate is False. The
    round trip matters: the generator's default output must not read as having
    baselines, or every plain batch would take the evaluation path.
    """
    from idp_cli.cli import _manifest_has_baselines

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path,baseline_source\ns3://b/a.pdf,\ns3://b/c.pdf,\n")

    assert _manifest_has_baselines(str(manifest)) is False


@pytest.mark.unit
def test_manifest_has_baselines_is_false_without_the_column(tmp_path):
    from idp_cli.cli import _manifest_has_baselines

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path\ns3://b/a.pdf\n")

    assert _manifest_has_baselines(str(manifest)) is False


@pytest.mark.unit
def test_manifest_has_baselines_is_false_for_a_header_only_manifest(tmp_path):
    from idp_cli.cli import _manifest_has_baselines

    manifest = tmp_path / "m.csv"
    manifest.write_text("document_path,baseline_source\n")

    assert _manifest_has_baselines(str(manifest)) is False


@pytest.mark.unit
def test_manifest_has_baselines_reads_a_json_manifest(tmp_path):
    """`.json` switches the reader to `pandas.read_json`; both verdicts are checked."""
    from idp_cli.cli import _manifest_has_baselines

    with_baselines = tmp_path / "with.json"
    with_baselines.write_text(
        json.dumps([{"document_path": "s3://b/a.pdf", "baseline_source": "s3://x/a/"}])
    )
    without = tmp_path / "without.json"
    without.write_text(json.dumps([{"document_path": "s3://b/a.pdf"}]))

    assert _manifest_has_baselines(str(with_baselines)) is True
    assert _manifest_has_baselines(str(without)) is False


@pytest.mark.unit
def test_manifest_has_baselines_is_false_for_a_file_it_cannot_read(tmp_path):
    """DEFECT (pinned, not fixed): every failure is indistinguishable from "no baselines".

    The whole body is wrapped in `except Exception: return False`, so a missing file, a
    permissions error and an unparseable manifest all return the same answer as a
    perfectly valid manifest with an empty baseline column. A caller branching on this
    predicate silently skips evaluation for a manifest it could not read, with nothing
    printed. Three unreadable inputs are checked here — absent, a directory, and a
    manifest whose extension lies about its format — because each takes a different
    route to the same wrong-looking `False`.
    """
    from idp_cli.cli import _manifest_has_baselines

    assert _manifest_has_baselines(str(tmp_path / "absent.csv")) is False

    a_directory = tmp_path / "dir.csv"
    a_directory.mkdir()
    assert _manifest_has_baselines(str(a_directory)) is False

    # A CSV named .json: the extension picks read_json, which cannot read it.
    mislabelled = tmp_path / "actually-csv.json"
    mislabelled.write_text("document_path,baseline_source\ns3://b/a.pdf,s3://x/a/\n")
    assert _manifest_has_baselines(str(mislabelled)) is False


# ================================================================================
# _create_test_set_from_manifest
# ================================================================================


def _manifest_with(tmp_path, rows, name="m.csv"):
    """Write a two-column CSV manifest from `(document_path, baseline_source)` pairs."""
    path = tmp_path / name
    lines = ["document_path,baseline_source"]
    lines.extend(f"{doc},{baseline}" for doc, baseline in rows)
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.mark.unit
def test_create_test_set_uploads_local_inputs_and_baselines(tmp_path, capsys):
    """The local-file path, read back off the bucket.

    Three things at once: the input lands under `input/`, every file under the baseline
    directory lands under `baseline/<document filename>/` with its relative structure
    preserved, and the `.uploading` marker that keeps the resolver away from a
    half-built folder is gone by the end.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    baseline = tmp_path / "baselines" / "invoice"
    (baseline / "sections" / "1").mkdir(parents=True)
    (baseline / "sections" / "1" / "result.json").write_text("{}")
    (baseline / "summary.json").write_text("{}")

    manifest = _manifest_with(tmp_path, [(doc, baseline)])

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)

        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {
        "set1/input/invoice.pdf",
        "set1/baseline/invoice.pdf/sections/1/result.json",
        "set1/baseline/invoice.pdf/summary.json",
    }
    assert "✓ Test set 'set1' created with 1 files" in capsys.readouterr().out


@pytest.mark.unit
def test_create_test_set_copies_an_s3_source_server_side(tmp_path, api_calls):
    """An `s3://` document is copied, not downloaded and re-uploaded.

    The `CopySource` parameters are the contract: a wrong bucket or a key that still
    carries the `s3://` scheme produces a `NoSuchKey` at run time. They are asserted
    off the recorded request rather than only by reading the destination back, because
    a successful copy does not tell you the source was parsed correctly if both happen
    to be the same bucket.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    manifest = _manifest_with(tmp_path, [("s3://source-bucket/docs/invoice.pdf", "")])

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="source-bucket")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket="source-bucket", Key="docs/invoice.pdf", Body=b"pdf")

        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {"set1/input/invoice.pdf"}
    copy = api_calls.only("CopyObject")
    # botocore flattens the {"Bucket", "Key"} form into the wire representation before
    # the call is made, so this is the string the service actually receives.
    assert copy.params["CopySource"] == "source-bucket/docs/invoice.pdf"
    assert copy.params["Bucket"] == TEST_SET_BUCKET
    assert copy.params["Key"] == "set1/input/invoice.pdf"


@pytest.mark.unit
def test_create_test_set_raises_without_a_test_set_bucket(tmp_path):
    from idp_cli.cli import _create_test_set_from_manifest

    manifest = _manifest_with(tmp_path, [("s3://b/a.pdf", "")])

    with pytest.raises(ValueError, match="TestSetBucket not found in stack resources"):
        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, {})


@pytest.mark.unit
def test_create_test_set_clears_only_its_own_prefix(tmp_path, capsys):
    """Recreating a test set deletes its previous contents and nothing else.

    A `Prefix` that dropped the trailing slash, or a delete built from a bucket-wide
    listing, would take out a neighbouring test set. The surviving key in another
    prefix is what makes that assertion real.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    manifest = _manifest_with(tmp_path, [(doc, "")])

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set1/input/stale.pdf", Body=b"old")
        s3.put_object(
            Bucket=TEST_SET_BUCKET, Key="set1/baseline/stale.pdf/r.json", Body=b"{}"
        )
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set2/input/keep.pdf", Body=b"keep")

        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {"set1/input/invoice.pdf", "set2/input/keep.pdf"}
    assert "Cleared 2 existing test set files" in capsys.readouterr().out


@pytest.mark.unit
def test_create_test_set_clears_every_object_past_the_first_page(
    tmp_path, capsys, api_calls
):
    """Recreating a test set of more than 1000 objects deletes all of them.

    The destructive half of the same unpaginated listing as
    `test_document_ids_covers_a_test_set_over_one_thousand_files`, and the worse half:
    the clear read one `list_objects_v2` response, so re-creating a test set larger than
    one page deleted its first 1000 objects and left the rest in place, orphaned under a
    prefix the caller was told it had emptied — and the very next thing the caller does
    is upload a new test set over it, so the survivors become one set's inputs and
    baselines mixed into another's, which is a wrong evaluation rather than a missing
    one. Fixing the read and not this would have left the more damaging direction.

    1001 stale objects, one manifest row. The assertions are that **no** stale object
    survives — named individually for the lexically last one, which a single listing
    cannot reach — and that every `DeleteObjects` request carried at most 1000 keys,
    which is the other half of the same ceiling: S3 rejects a larger request outright,
    and `moto` does not enforce that, so a test asserting only that the objects are gone
    would pass here against code that fails against the real service.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    manifest = _manifest_with(tmp_path, [(doc, "")])

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        for i in range(1001):
            s3.put_object(
                Bucket=TEST_SET_BUCKET, Key=f"set1/stale/{i:05d}.json", Body=b"{}"
            )
        s3.put_object(Bucket=TEST_SET_BUCKET, Key="set2/input/keep.pdf", Body=b"keep")
        before_call = len(api_calls)

        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())

        keys = all_keys(s3, TEST_SET_BUCKET)
        deletes = [
            call
            for call in api_calls[before_call:]
            if call.operation == "DeleteObjects"
        ]

    assert "set1/stale/01000.json" not in keys, (
        "the object beyond the first listing page survived the clear"
    )
    assert not any(key.startswith("set1/stale/") for key in keys)
    assert keys == {"set1/input/invoice.pdf", "set2/input/keep.pdf"}
    assert "Cleared 1001 existing test set files" in capsys.readouterr().out

    assert len(deletes) == 2, [len(d.params["Delete"]["Objects"]) for d in deletes]
    for delete in deletes:
        assert len(delete.params["Delete"]["Objects"]) <= 1000, (
            "DeleteObjects takes at most 1000 keys; a larger request is rejected by S3"
        )


@pytest.mark.unit
def test_create_test_set_places_the_marker_first_and_removes_it_last(
    tmp_path, api_calls
):
    """Ordering of the `.uploading` marker against the document uploads.

    The marker exists so the test set resolver's auto-detection skips a folder that is
    still being filled (issue #193). If it were written after the documents, or removed
    before the last one, the resolver could validate a partial test set and register it
    with the wrong file count.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    manifest = _manifest_with(tmp_path, [(doc, "")])

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(
            Bucket=TEST_SET_BUCKET
        )
        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())

    put_keys = [
        call.params["Key"] for call in api_calls if call.operation == "PutObject"
    ]
    assert put_keys == ["set1/.uploading", "set1/input/invoice.pdf"]
    deleted = api_calls.only("DeleteObject")
    assert deleted.params["Key"] == "set1/.uploading"
    operations = api_calls.operations()
    assert operations.index("DeleteObject") > operations.index("PutObject")


@pytest.mark.unit
def test_create_test_set_leaves_the_marker_behind_when_an_upload_fails(
    tmp_path, capsys
):
    """DEFECT (pinned, not fixed): a mid-upload failure leaves `.uploading` in the bucket.

    The marker is removed by a plain statement after the upload loop, not by a
    `try/finally`, so any exception during the loop — a missing local file, a denied
    `PutObject`, a network drop — propagates with the marker still in place. The
    resolver's auto-detection skips any folder carrying that marker, so the
    half-uploaded test set becomes permanently invisible to the backend and will not
    appear in the UI even after the underlying problem is fixed and the files are
    re-uploaded, unless someone deletes the marker object by hand.

    A manifest naming a local file that does not exist is the cheapest way to reach it;
    the manifest is written directly here rather than through `generate-manifest`,
    which would never emit a path it did not just find.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    missing = tmp_path / "gone.pdf"
    manifest = _manifest_with(tmp_path, [(missing, "")])

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)

        with pytest.raises(Exception):
            _create_test_set_from_manifest(
                str(manifest), "set1", "IDP", None, resources()
            )

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {"set1/.uploading"}, (
        "the upload marker outlives the failure and hides the test set"
    )


@pytest.mark.unit
def test_create_test_set_silently_uploads_no_baselines_for_an_s3_baseline_source(
    tmp_path, capsys
):
    """DEFECT (pinned, not fixed): an `s3://` baseline_source uploads zero baseline files.

    The baseline step is a local `glob.glob(os.path.join(baseline_source, "**", "*"))`,
    which matches nothing when `baseline_source` is an S3 URI. That is exactly what
    `generate-manifest --dir ... --test-set ...` writes into its manifest: it rewrites
    every `baseline_source` to `s3://<test set bucket>/<set>/baseline/<file>/`. So
    feeding a manifest produced by that command back into this function creates a test
    set whose `input/` is complete and whose `baseline/` is empty, with no warning and
    no error — and an evaluation over it has nothing to compare against.

    The document itself still copies fine, which is why the failure is invisible: the
    file count printed at the end counts manifest rows, not uploaded objects, so it
    reports "created with 1 files" either way.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    manifest = _manifest_with(
        tmp_path,
        [
            (
                "s3://source-bucket/docs/invoice.pdf",
                f"s3://{TEST_SET_BUCKET}/other/baseline/invoice.pdf/",
            )
        ],
    )

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="source-bucket")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        s3.put_object(Bucket="source-bucket", Key="docs/invoice.pdf", Body=b"pdf")
        s3.put_object(
            Bucket=TEST_SET_BUCKET,
            Key="other/baseline/invoice.pdf/result.json",
            Body=b"{}",
        )

        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET, Prefix="set1/").get(
                "Contents", []
            )
        }

    assert keys == {"set1/input/invoice.pdf"}
    output = capsys.readouterr().out
    assert "created with 1 files" in output
    assert "Warning" not in output, "nothing tells the user the baselines were skipped"


@pytest.mark.unit
def test_create_test_set_skips_an_empty_baseline_cell(tmp_path):
    """An empty `baseline_source` reads as NaN and is skipped without error."""
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    manifest = _manifest_with(tmp_path, [(doc, "")])

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())
        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {"set1/input/invoice.pdf"}


@pytest.mark.unit
def test_create_test_set_handles_a_manifest_with_no_baseline_column(tmp_path):
    """A one-column manifest is accepted; the `in row` guard covers the missing column."""
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    manifest = tmp_path / "m.csv"
    manifest.write_text(f"document_path\n{doc}\n")

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())
        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {"set1/input/invoice.pdf"}


@pytest.mark.unit
def test_create_test_set_reads_a_json_manifest(tmp_path, capsys):
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"document_path": str(doc)}]))

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_SET_BUCKET)
        _create_test_set_from_manifest(str(manifest), "set1", "IDP", None, resources())
        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=TEST_SET_BUCKET).get("Contents", [])
        }

    assert keys == {"set1/input/invoice.pdf"}
    assert "created with 1 files" in capsys.readouterr().out


@pytest.mark.unit
def test_create_test_set_only_warns_when_the_marker_cannot_be_deleted(tmp_path, capsys):
    """DEFECT (pinned, not fixed): a failed marker removal is a warning, and nothing else.

    Same shape as the `generate-manifest` case: the delete is wrapped in its own
    `except Exception`, so a bucket policy allowing `s3:PutObject` but not
    `s3:DeleteObject` leaves the `.uploading` marker in place while this function goes
    on to print that the test set was created. The resolver skips any folder carrying
    that marker, so the test set is complete in S3 and invisible to the backend, and
    the caller is told it succeeded.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    manifest = _manifest_with(tmp_path, [(doc, "")])

    class NoDeleteObject:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def delete_object(self, **kwargs):
            raise RuntimeError("AccessDenied: s3:DeleteObject")

    real_client = boto3.client

    with mock_aws():
        real_client("s3", region_name="us-east-1").create_bucket(Bucket=TEST_SET_BUCKET)
        fake_s3 = NoDeleteObject(real_client("s3", region_name="us-east-1"))

        with patched_boto3(fake_s3=fake_s3):
            _create_test_set_from_manifest(
                str(manifest), "set1", "IDP", None, resources()
            )

        keys = {
            obj["Key"]
            for obj in real_client("s3", region_name="us-east-1")
            .list_objects_v2(Bucket=TEST_SET_BUCKET)
            .get("Contents", [])
        }

    output = capsys.readouterr().out
    assert "Warning: Could not remove upload marker:" in output
    assert "created with 1 files" in output
    assert "set1/.uploading" in keys


@pytest.mark.unit
def test_create_test_set_warns_but_continues_when_it_cannot_clear(tmp_path, capsys):
    """A listing failure on the clear step is a warning; the upload still proceeds.

    Reached here by pointing at a bucket that does not exist, so both the list and the
    later put fail. The property being pinned is that the clear step alone does not
    abort — it is a tidy-up, and a brand-new test set has nothing to clear.
    """
    from idp_cli.cli import _create_test_set_from_manifest

    doc = tmp_path / "invoice.pdf"
    doc.write_text("pdf")
    manifest = _manifest_with(tmp_path, [(doc, "")])

    with mock_aws():
        with pytest.raises(Exception):
            _create_test_set_from_manifest(
                str(manifest), "set1", "IDP", None, {"TestSetBucket": "no-such-bucket"}
            )

    assert "Warning: Could not clear existing files:" in capsys.readouterr().out


# ================================================================================
# _process_test_set
# ================================================================================


def _resources_object(**overrides):
    """A stand-in for the SDK's stack-resources object.

    A real object rather than a `MagicMock`, because three of the six fields are read
    with `getattr(..., None)` and a `MagicMock` answers every `getattr` with a new mock
    — which would make the "optional field is absent" assertions pass vacuously.
    """
    fields = {
        "input_bucket": "in-bucket",
        "output_bucket": "out-bucket",
        "documents_table": "docs-table",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.unit
def test_process_test_set_builds_the_resources_dict_the_helpers_expect():
    """The six keys are the interface between this function and the three helpers.

    `_get_test_set_document_ids` looks up `TestSetBucket` by that exact string and
    raises if it is missing, so a renamed key here turns into a `ValueError` two calls
    away. The three optional fields come through `getattr(..., None)` and are checked
    for with a resources object that genuinely lacks them.
    """
    from idp_cli import cli as cli_module

    client = SimpleNamespace(
        stack=SimpleNamespace(
            get_resources=lambda: _resources_object(
                test_set_bucket=TEST_SET_BUCKET,
                state_machine_arn="arn:aws:states:us-east-1:1:stateMachine:m",
                document_queue="https://sqs.example.invalid/q",
            )
        )
    )
    seen = {}

    def _record_resolver(stack_name, test_set_name, region, resources_dict):
        seen["resolver"] = (stack_name, test_set_name, region, resources_dict)

    def _record_runner(*args, **kwargs):
        seen["runner"] = (args, kwargs)
        return {"testRunId": "run-1", "filesCount": 2}

    def _record_ids(stack_name, test_set, batch_id, region, resources_dict):
        seen["ids"] = (stack_name, test_set, batch_id, region, resources_dict)
        return ["run-1/a.pdf", "run-1/b.pdf"]

    with (
        patch.object(cli_module, "_invoke_test_set_resolver", _record_resolver),
        patch.object(cli_module, "_invoke_test_runner", _record_runner),
        patch.object(cli_module, "_get_test_set_document_ids", _record_ids),
    ):
        batch_result = cli_module._process_test_set(
            "IDP", "set1", "a context", "us-east-1", client
        )

    expected_resources = {
        "InputBucket": "in-bucket",
        "OutputBucket": "out-bucket",
        "DocumentsTable": "docs-table",
        "TestSetBucket": TEST_SET_BUCKET,
        "StateMachineArn": "arn:aws:states:us-east-1:1:stateMachine:m",
        "DocumentQueue": "https://sqs.example.invalid/q",
    }
    assert seen["resolver"] == ("IDP", "set1", "us-east-1", expected_resources)
    assert seen["ids"] == ("IDP", "set1", "run-1", "us-east-1", expected_resources)
    assert seen["runner"][0] == (
        "IDP",
        "set1",
        "a context",
        "us-east-1",
        expected_resources,
        None,
        None,
        None,
    )
    assert batch_result == {
        "batch_id": "run-1",
        "documents_queued": 2,
        "documents": [],
        "document_ids": ["run-1/a.pdf", "run-1/b.pdf"],
        "uploaded": 0,
        "skipped": 0,
        "failed": 0,
        "queued": 2,
    }


@pytest.mark.unit
def test_process_test_set_defaults_absent_optional_resources_to_none():
    """A resources object without the three optional fields yields explicit `None`s.

    Those `None`s are what make the downstream failure legible: `TestSetBucket: None`
    produces "TestSetBucket not found in stack resources" rather than an `AttributeError`
    on the resources object.
    """
    from idp_cli import cli as cli_module

    client = SimpleNamespace(stack=SimpleNamespace(get_resources=_resources_object))
    seen = {}

    with (
        patch.object(
            cli_module,
            "_invoke_test_set_resolver",
            lambda *args: seen.setdefault("resources", args[3]),
        ),
        patch.object(
            cli_module,
            "_invoke_test_runner",
            lambda *args, **kwargs: {"testRunId": "r", "filesCount": 0},
        ),
        patch.object(cli_module, "_get_test_set_document_ids", lambda *args: []),
    ):
        cli_module._process_test_set("IDP", "set1", None, None, client)

    assert seen["resources"]["TestSetBucket"] is None
    assert seen["resources"]["StateMachineArn"] is None
    assert seen["resources"]["DocumentQueue"] is None


@pytest.mark.unit
def test_process_test_set_truncates_document_ids_to_the_queued_count():
    """With `--number-of-files`, monitoring must watch only the documents actually queued.

    The test set holds five input files and the runner reports it queued two, so the
    five ids listed from S3 are cut to two. Without the cut the monitor would wait for
    three documents that were never submitted and the run would appear to hang. Which
    two is not meaningful — the S3 listing order has no relationship to the runner's
    selection — so only the count is asserted, and that limitation is worth knowing:
    the ids monitored may not be the ids queued.
    """
    from idp_cli import cli as cli_module

    client = SimpleNamespace(
        stack=SimpleNamespace(
            get_resources=lambda: _resources_object(test_set_bucket=TEST_SET_BUCKET)
        )
    )
    all_ids = [f"run-1/doc{index}.pdf" for index in range(5)]

    with (
        patch.object(cli_module, "_invoke_test_set_resolver", lambda *args: None),
        patch.object(
            cli_module,
            "_invoke_test_runner",
            lambda *args, **kwargs: {"testRunId": "run-1", "filesCount": 2},
        ),
        patch.object(cli_module, "_get_test_set_document_ids", lambda *args: all_ids),
    ):
        batch_result = cli_module._process_test_set(
            "IDP", "set1", None, None, client, number_of_files=2
        )

    assert batch_result["document_ids"] == all_ids[:2]
    assert batch_result["documents_queued"] == 2
    assert batch_result["queued"] == 2


@pytest.mark.unit
def test_process_test_set_does_not_truncate_when_no_limit_was_requested():
    """Without `--number-of-files` the id list is kept even if it exceeds the count.

    The truncation is gated on the limit having been asked for, not on the two numbers
    disagreeing. So if the runner reports a smaller `filesCount` than the test set
    holds for some other reason, monitoring still waits on every file — a run that
    looks stuck rather than one that finishes early. Pinned as the current behaviour;
    it is the safer of the two failure directions, since the alternative would silently
    stop monitoring documents that were queued.
    """
    from idp_cli import cli as cli_module

    client = SimpleNamespace(
        stack=SimpleNamespace(
            get_resources=lambda: _resources_object(test_set_bucket=TEST_SET_BUCKET)
        )
    )
    all_ids = ["run-1/a.pdf", "run-1/b.pdf", "run-1/c.pdf"]

    with (
        patch.object(cli_module, "_invoke_test_set_resolver", lambda *args: None),
        patch.object(
            cli_module,
            "_invoke_test_runner",
            lambda *args, **kwargs: {"testRunId": "run-1", "filesCount": 1},
        ),
        patch.object(cli_module, "_get_test_set_document_ids", lambda *args: all_ids),
    ):
        batch_result = cli_module._process_test_set("IDP", "set1", None, None, client)

    assert batch_result["document_ids"] == all_ids
    assert batch_result["documents_queued"] == 1


@pytest.mark.unit
def test_process_test_set_forwards_the_config_pin_to_the_runner():
    """`config_version` and `config_revision` must reach the runner unchanged.

    A run recorded against a profile without its revision pinned actually uses whatever
    that profile holds at execution time, which makes the result unreproducible. This
    is the seam where that pin could be dropped, so the positional arguments are
    asserted exactly.
    """
    from idp_cli import cli as cli_module

    client = SimpleNamespace(
        stack=SimpleNamespace(
            get_resources=lambda: _resources_object(test_set_bucket=TEST_SET_BUCKET)
        )
    )
    captured = {}

    def _runner(*args, **kwargs):
        captured["args"] = args
        return {"testRunId": "run-1", "filesCount": 1}

    with (
        patch.object(cli_module, "_invoke_test_set_resolver", lambda *args: None),
        patch.object(cli_module, "_invoke_test_runner", _runner),
        patch.object(
            cli_module, "_get_test_set_document_ids", lambda *args: ["run-1/a.pdf"]
        ),
    ):
        cli_module._process_test_set(
            "IDP",
            "set1",
            None,
            None,
            client,
            number_of_files=1,
            config_version="v4",
            config_revision=11,
        )

    assert captured["args"][5:] == (1, "v4", 11)


@pytest.mark.unit
def test_process_test_set_resolves_the_test_set_before_starting_the_run():
    """Order matters: register the test set, then start the run, then list its documents.

    The runner needs the test set to exist in the tracking table, and the document ids
    are keyed on the run id the runner returns, so neither of the two orderings can be
    swapped. A single recorded sequence is the cheapest way to hold that.
    """
    from idp_cli import cli as cli_module

    client = SimpleNamespace(
        stack=SimpleNamespace(
            get_resources=lambda: _resources_object(test_set_bucket=TEST_SET_BUCKET)
        )
    )
    order = []

    with (
        patch.object(
            cli_module,
            "_invoke_test_set_resolver",
            lambda *args: order.append("resolver"),
        ),
        patch.object(
            cli_module,
            "_invoke_test_runner",
            lambda *args, **kwargs: (
                order.append("runner"),
                {"testRunId": "run-1", "filesCount": 1},
            )[1],
        ),
        patch.object(
            cli_module,
            "_get_test_set_document_ids",
            lambda *args: (order.append("ids"), ["run-1/a.pdf"])[1],
        ),
    ):
        cli_module._process_test_set("IDP", "set1", None, None, client)

    assert order == ["resolver", "runner", "ids"]
