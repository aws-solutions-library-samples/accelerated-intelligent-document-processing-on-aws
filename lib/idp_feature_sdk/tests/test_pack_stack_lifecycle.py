# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`pack.create_or_update_stack`, `pack.deploy_pack` and the bucket-policy
helpers around them.

`create_or_update_stack` is the single create-or-update used by both
`idp-feature-cli deploy` and `deploy-pack`, and it exists because a feature
install has to be idempotent: a console install and a CLI deploy target the same
stack name, so re-running must upgrade in place rather than fail with
`AlreadyExistsException`. Four states it has to tell apart, all of which look
similar from the outside and three of which a naive wait loop reports wrongly:

  * a no-op update (`No updates are to be performed`), which is **success** — the
    stack is already at the requested version;
  * a stack in `ROLLBACK_COMPLETE`, which is a *failed create* that CloudFormation
    will never let you update, so it must be refused with the delete command
    rather than retried forever;
  * a stack that has **vanished** mid-wait, which is what `OnFailure=DELETE` does
    after a failed create — polling by name then 404s and a loop that treats a
    missing stack as "not ready yet" waits an hour and times out;
  * a terminal failure, where the useful information is the first failure *event*,
    not the status word.

`deploy_pack` sits on top and decides which parameters to submit. It submits only
what the wrapper declares, because CloudFormation rejects an undeclared parameter
outright — so a wrapper that does not take `AdminEmail` must not have one forced
on it, and a `--parameters` override naming something the wrapper has never heard
of must be dropped rather than passed through.

The bucket-policy helpers are here because their failure is the mirror image:
`apply_enforce_ssl_only` is additive hardening and must preserve an operator's own
statements, while `apply_public_artifacts_policy` must verify its change actually
took effect — account-level Block Public Access silently overrides a successful
`PutPublicAccessBlock`, and the resulting bucket looks configured and 403s every
anonymous read.
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws
from rich.console import Console

from idp_feature_sdk.pack import (
    _describe_one,
    _first_failure_reason,
    _partition,
    _statement_list,
    apply_enforce_ssl_only,
    apply_public_artifacts_policy,
    create_or_update_stack,
    deploy_pack,
    publish_host_accelerator,
)

pytestmark = pytest.mark.unit

_BUCKET = "artifacts-bkt"
_ARN = "arn:aws:cloudformation:us-east-1:123456789012:stack/s/abc"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """The wait loop sleeps 15s between polls. Left in, every wait test would add
    a quarter-minute per iteration; the loop's *logic* is what is under test."""
    monkeypatch.setattr("idp_feature_sdk.pack.time.sleep", lambda _s: None)


class _ScriptedCfn:
    """A CloudFormation client that answers `describe_stacks` from a script.

    Each entry is either a status string (a stack in that state) or None (no such
    stack). Used where moto cannot produce the state — a stack that disappears
    mid-wait, or a terminal ROLLBACK that moto's create path will not enter.
    """

    def __init__(self, statuses: list, events: list | None = None) -> None:
        self._statuses = list(statuses)
        self._events = events or []
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.describe_calls = 0

    def describe_stacks(self, StackName: str):  # noqa: N803 - boto3 casing
        self.describe_calls += 1
        status = (
            self._statuses.pop(0) if self._statuses else self._statuses_last_or_none()
        )
        if status is None:
            raise RuntimeError(f"Stack with id {StackName} does not exist")
        return {"Stacks": [{"StackStatus": status, "StackId": _ARN}]}

    def _statuses_last_or_none(self):
        return None

    def create_stack(self, **kwargs):
        self.created.append(kwargs)
        return {"StackId": _ARN}

    def update_stack(self, **kwargs):
        self.updated.append(kwargs)
        return {"StackId": _ARN}

    def describe_stack_events(self, StackName: str):  # noqa: N803
        return {"StackEvents": self._events}


def _console() -> Console:
    return Console(record=True, width=400)


# ---------------------------------------------------------------------------
# create_or_update_stack — the four states
# ---------------------------------------------------------------------------


def test_an_absent_stack_is_created_with_on_failure_delete(aws_env) -> None:
    """`OnFailure=DELETE` on a *create* is deliberate: a failed first install
    leaves no ROLLBACK_COMPLETE husk that the next deploy would have to refuse."""
    cfn = _ScriptedCfn([None, "CREATE_COMPLETE"])
    arn = create_or_update_stack(
        cfn=cfn,
        stack_name="s",
        template_url="https://b/t.yaml",
        parameters=[{"ParameterKey": "MainStackName", "ParameterValue": "host"}],
        console=_console(),
    )
    assert arn == _ARN
    assert cfn.updated == []
    (call,) = cfn.created
    assert call["OnFailure"] == "DELETE"
    assert call["TemplateURL"] == "https://b/t.yaml"
    assert call["Parameters"] == [
        {"ParameterKey": "MainStackName", "ParameterValue": "host"}
    ]
    assert call["Capabilities"] == [
        "CAPABILITY_IAM",
        "CAPABILITY_NAMED_IAM",
        "CAPABILITY_AUTO_EXPAND",
    ]


def test_an_existing_stack_is_updated_rather_than_created(aws_env) -> None:
    """The idempotence contract: a console install and a CLI deploy name the same
    stack, so the second one must upgrade it in place."""
    cfn = _ScriptedCfn(["CREATE_COMPLETE", "UPDATE_COMPLETE"])
    arn = create_or_update_stack(
        cfn=cfn, stack_name="s", template_url="https://b/t.yaml", parameters=[]
    )
    assert arn == _ARN
    assert cfn.created == []
    assert len(cfn.updated) == 1
    assert "OnFailure" not in cfn.updated[0]


def test_a_rollback_complete_stack_is_refused_with_the_delete_command(
    aws_env,
) -> None:
    """CloudFormation cannot update a failed CREATE. Attempting it produces an
    opaque ValidationError, and retrying never succeeds — so the message has to
    carry the `delete-stack` command the operator needs."""
    cfn = _ScriptedCfn(["ROLLBACK_COMPLETE"])
    with pytest.raises(RuntimeError) as exc:
        create_or_update_stack(
            cfn=cfn, stack_name="s", template_url="https://b/t.yaml", parameters=[]
        )
    assert "ROLLBACK_COMPLETE" in str(exc.value)
    assert "delete-stack --stack-name s" in str(exc.value)
    assert cfn.updated == [] and cfn.created == []


def test_a_rollback_failed_stack_is_refused_too(aws_env) -> None:
    """The other undeletable-by-update state. Treating only ROLLBACK_COMPLETE as
    fatal would send an update at a ROLLBACK_FAILED stack and fail opaquely."""
    cfn = _ScriptedCfn(["ROLLBACK_FAILED"])
    with pytest.raises(RuntimeError, match="ROLLBACK_FAILED"):
        create_or_update_stack(
            cfn=cfn, stack_name="s", template_url="https://b/t.yaml", parameters=[]
        )


def test_a_no_op_update_is_success_and_returns_the_existing_stack_id(
    aws_env,
) -> None:
    """Re-deploying an unchanged feature is the normal case in a loop, and
    CloudFormation answers it with an error. Treating that as a failure would make
    every idempotent re-run exit non-zero."""

    class _NoopCfn(_ScriptedCfn):
        def update_stack(self, **_kwargs):
            raise RuntimeError(
                "An error occurred (ValidationError): No updates are to be performed."
            )

    cfn = _NoopCfn(["CREATE_COMPLETE"])
    console = _console()
    arn = create_or_update_stack(
        cfn=cfn,
        stack_name="s",
        template_url="https://b/t.yaml",
        parameters=[],
        console=console,
    )
    assert arn == _ARN
    assert "No changes" in console.export_text()


def test_a_genuine_create_error_is_wrapped_naming_the_operation(aws_env) -> None:
    class _FailingCfn(_ScriptedCfn):
        def create_stack(self, **_kwargs):
            raise RuntimeError("AccessDenied: not authorized to CreateStack")

    with pytest.raises(RuntimeError, match="Create failed") as exc:
        create_or_update_stack(
            cfn=_FailingCfn([None]),
            stack_name="s",
            template_url="https://b/t.yaml",
            parameters=[],
        )
    assert "AccessDenied" in str(exc.value)


def test_a_genuine_update_error_says_update_not_create(aws_env) -> None:
    """Which operation failed changes what the operator does next — an update
    failure leaves a running stack, a create failure leaves nothing."""

    class _FailingCfn(_ScriptedCfn):
        def update_stack(self, **_kwargs):
            raise RuntimeError("AccessDenied")

    with pytest.raises(RuntimeError, match="Update failed"):
        create_or_update_stack(
            cfn=_FailingCfn(["CREATE_COMPLETE"]),
            stack_name="s",
            template_url="https://b/t.yaml",
            parameters=[],
        )


def test_without_wait_the_arn_is_returned_without_polling(aws_env) -> None:
    """`--wait` off is the default for a reason: the caller gets the ARN and can
    watch the console. It must not poll even once, or the command's latency
    depends on how fast CloudFormation happens to start."""
    cfn = _ScriptedCfn([None])
    arn = create_or_update_stack(
        cfn=cfn,
        stack_name="s",
        template_url="https://b/t.yaml",
        parameters=[],
        wait=False,
    )
    assert arn == _ARN
    # One describe for the create-or-update decision, and no polling after.
    assert cfn.describe_calls == 1


def test_the_wait_polls_until_the_terminal_success_state(aws_env) -> None:
    cfn = _ScriptedCfn(
        [None, "CREATE_IN_PROGRESS", "CREATE_IN_PROGRESS", "CREATE_COMPLETE"]
    )
    console = _console()
    assert (
        create_or_update_stack(
            cfn=cfn,
            stack_name="s",
            template_url="https://b/t.yaml",
            parameters=[],
            console=console,
        )
        == _ARN
    )
    assert cfn.describe_calls == 4
    assert "CREATE_COMPLETE" in console.export_text()


def test_a_stack_that_vanishes_mid_wait_is_reported_with_its_last_status(
    aws_env,
) -> None:
    """What `OnFailure=DELETE` produces. The wait polls by StackId precisely so it
    keeps working after the named stack is gone; when even the id 404s, the last
    status observed is the only diagnostic there is, so it has to be in the
    message rather than a bare "stack not found"."""
    cfn = _ScriptedCfn([None, "CREATE_IN_PROGRESS", "ROLLBACK_IN_PROGRESS", None])
    with pytest.raises(RuntimeError) as exc:
        create_or_update_stack(
            cfn=cfn, stack_name="s", template_url="https://b/t.yaml", parameters=[]
        )
    assert "no longer exists" in str(exc.value)
    assert "ROLLBACK_IN_PROGRESS" in str(exc.value)
    assert "OnFailure=DELETE" in str(exc.value)


def test_a_terminal_failure_carries_the_first_failure_event_reason(
    aws_env,
) -> None:
    """The status word alone ("UPDATE_ROLLBACK_COMPLETE") says nothing about the
    cause. The reason is in the events, and surfacing it is the difference between
    a usable error and a trip to the console."""
    events = [
        {
            "ResourceStatus": "CREATE_FAILED",
            "LogicalResourceId": "RegisterFeature",
            "ResourceStatusReason": "Received response status [FAILED]",
        },
        {
            "ResourceStatus": "CREATE_IN_PROGRESS",
            "ResourceStatusReason": "User Initiated",
        },
    ]
    cfn = _ScriptedCfn([None, "CREATE_FAILED"], events=events)
    with pytest.raises(RuntimeError) as exc:
        create_or_update_stack(
            cfn=cfn, stack_name="s", template_url="https://b/t.yaml", parameters=[]
        )
    assert "CREATE_FAILED" in str(exc.value)
    assert "RegisterFeature" in str(exc.value)


def test_a_terminal_failure_with_no_readable_events_still_raises(aws_env) -> None:
    """A stack whose events cannot be read must not swallow the failure into a
    success — the status is terminal either way."""
    cfn = _ScriptedCfn([None, "ROLLBACK_COMPLETE"], events=[])
    with pytest.raises(RuntimeError, match="settled in ROLLBACK_COMPLETE"):
        create_or_update_stack(
            cfn=cfn, stack_name="s", template_url="https://b/t.yaml", parameters=[]
        )


def test_the_wait_times_out_rather_than_polling_forever(aws_env, monkeypatch) -> None:
    """A stack wedged in `*_IN_PROGRESS` (a custom resource that never answers)
    would otherwise hold the CLI open indefinitely."""
    clock = iter([0.0, 1.0, 10_000.0, 10_001.0])
    monkeypatch.setattr("idp_feature_sdk.pack.time.time", lambda: next(clock))
    cfn = _ScriptedCfn([None, "CREATE_IN_PROGRESS", "CREATE_IN_PROGRESS"])
    with pytest.raises(RuntimeError, match="Timed out waiting for stack s"):
        create_or_update_stack(
            cfn=cfn, stack_name="s", template_url="https://b/t.yaml", parameters=[]
        )


def test_explicit_capabilities_override_the_default_set(aws_env) -> None:
    cfn = _ScriptedCfn(
        [None],
    )
    create_or_update_stack(
        cfn=cfn,
        stack_name="s",
        template_url="https://b/t.yaml",
        parameters=[],
        wait=False,
        capabilities=["CAPABILITY_IAM"],
    )
    assert cfn.created[0]["Capabilities"] == ["CAPABILITY_IAM"]


# ---------------------------------------------------------------------------
# _describe_one
# ---------------------------------------------------------------------------


def test_describe_one_maps_a_missing_stack_to_none(aws_env) -> None:
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        assert _describe_one(cfn, "never-created") is None


def test_describe_one_propagates_any_other_error(aws_env) -> None:
    """A throttle or an AccessDenied must not read as "the stack does not exist" —
    that would turn a permissions problem into an attempted create, which then
    fails differently and confusingly."""

    class _Throttled:
        def describe_stacks(self, **_kw):
            raise RuntimeError("Throttling: Rate exceeded")

    with pytest.raises(RuntimeError, match="Throttling"):
        _describe_one(_Throttled(), "s")


# ---------------------------------------------------------------------------
# _first_failure_reason — the transform case
# ---------------------------------------------------------------------------


def test_a_transform_failure_is_found_despite_an_in_progress_status(aws_env) -> None:
    """A SAM transform failure arrives as a stack-level `*_IN_PROGRESS` event
    whose reason text contains "failed" — not as a `*_FAILED` status. Matching on
    status alone reports no reason at all for the single most common feature
    template error."""
    events = [
        {
            "ResourceStatus": "CREATE_IN_PROGRESS",
            "LogicalResourceId": "featurestack",
            "ResourceStatusReason": (
                "Transform AWS::Serverless-2016-10-31 failed with: "
                "'CodeUri' is not a valid S3 Uri"
            ),
        }
    ]
    reason = _first_failure_reason(_ScriptedCfn([], events=events), _ARN)
    assert reason is not None
    assert "not a valid S3 Uri" in reason
    assert reason.startswith("featurestack:")


def test_a_failure_from_a_previous_operation_is_not_reported(aws_env) -> None:
    """Scanning past the most recent "User Initiated" boundary would attribute the
    previous deploy's failure to this one — which sends the operator to fix
    something already fixed."""
    events = [
        {
            "ResourceStatus": "UPDATE_IN_PROGRESS",
            "ResourceStatusReason": "User Initiated",
        },
        {
            "ResourceStatus": "CREATE_FAILED",
            "LogicalResourceId": "Old",
            "ResourceStatusReason": "stale failure from last week",
        },
    ]
    assert _first_failure_reason(_ScriptedCfn([], events=events), _ARN) is None


def test_an_events_api_error_yields_no_reason_rather_than_masking_the_failure(
    aws_env,
) -> None:
    class _Broken:
        def describe_stack_events(self, **_kw):
            raise RuntimeError("AccessDenied")

    assert _first_failure_reason(_Broken(), _ARN) is None


# ---------------------------------------------------------------------------
# deploy_pack — which parameters get submitted
# ---------------------------------------------------------------------------


class _WrapperCfn:
    """A CloudFormation stand-in whose ``validate_template`` reports a given set
    of declared parameter names, recording what was then submitted.

    Not moto: moto's ``ValidateTemplate`` returns an empty ``Parameters`` list
    when handed a ``TemplateURL`` (it does not parse the fetched object), so every
    assertion about the submitted set would pass vacuously — with nothing
    declared, ``_maybe_submit`` drops every parameter and there is nothing left to
    be wrong. The declared set is therefore supplied explicitly here, which is
    also the only way to assert the *negative* case (a name the wrapper does not
    declare being dropped).
    """

    def __init__(self, declared: list[str], *, exists: bool = False) -> None:
        self._declared = declared
        self._exists = exists
        self.created: list[dict] = []
        self.updated: list[dict] = []

    def validate_template(self, TemplateURL: str):  # noqa: N803 - boto3 casing
        self.validated = TemplateURL
        return {"Parameters": [{"ParameterKey": k} for k in self._declared]}

    def describe_stacks(self, StackName: str):  # noqa: N803
        if not self._exists:
            raise RuntimeError(f"Stack with id {StackName} does not exist")
        return {"Stacks": [{"StackStatus": "CREATE_COMPLETE", "StackId": _ARN}]}

    def create_stack(self, **kwargs):
        self.created.append(kwargs)
        return {"StackId": _ARN}

    def update_stack(self, **kwargs):
        self.updated.append(kwargs)
        return {"StackId": _ARN}


def _install_cfn(monkeypatch, fake) -> None:
    real_client = boto3.client

    def _client(service, **kwargs):
        if service == "cloudformation":
            return fake
        return real_client(service, **kwargs)

    monkeypatch.setattr("idp_feature_sdk.pack.boto3.client", _client)


def _submitted(call: dict) -> dict[str, str]:
    return {p["ParameterKey"]: p["ParameterValue"] for p in call["Parameters"]}


_WRAPPER_URL = "https://wrapper-bkt.s3.us-east-1.amazonaws.com/extensions/p/deploy.yaml"


def test_deploy_pack_submits_admin_email_and_a_derived_host_stack_name(
    monkeypatch,
) -> None:
    """`HostStackName` has no default on the wrapper, and IDP resource names are
    built from it, so it must be short. A name derived from the wrapper stack name
    is what keeps the operator from having to supply it."""
    cfn = _WrapperCfn(["AdminEmail", "HostStackName", "LogLevel"])
    _install_cfn(monkeypatch, cfn)
    arn = deploy_pack(
        wrapper_url=_WRAPPER_URL,
        stack_name="claims",
        admin_email="admin@example.com",
        region="us-east-1",
        wait=False,
        console=_console(),
    )
    assert arn == _ARN
    assert cfn.validated == _WRAPPER_URL
    assert _submitted(cfn.created[0]) == {
        "AdminEmail": "admin@example.com",
        "HostStackName": "claims-IDPAccelerator",
    }


def test_the_derived_host_stack_name_is_capped_at_25_characters(monkeypatch) -> None:
    """CloudFormation-generated resource names concatenate it, and several AWS
    resource types cap at 64 characters. An over-long host name makes the *host*
    stack fail to create — after the wrapper has already started."""
    cfn = _WrapperCfn(["AdminEmail", "HostStackName"])
    _install_cfn(monkeypatch, cfn)
    long_name = "a-very-long-wrapper-stack-name-indeed"
    deploy_pack(
        wrapper_url=_WRAPPER_URL,
        stack_name=long_name,
        admin_email="a@example.com",
        region="us-east-1",
        wait=False,
        console=_console(),
    )
    host = _submitted(cfn.created[0])["HostStackName"]
    assert host == f"{long_name}-IDPAccelerator"[:25]
    assert len(host) == 25
    assert host == "a-very-long-wrapper-stack"


def test_only_parameters_the_wrapper_declares_are_submitted(monkeypatch) -> None:
    """CloudFormation rejects an undeclared parameter outright. A `--parameters`
    override naming something this wrapper does not have would otherwise turn a
    harmless typo into a refused deploy — and the baked publish-time defaults are
    what the wrapper is meant to use anyway."""
    cfn = _WrapperCfn(["AdminEmail", "HostStackName", "LogLevel"])
    _install_cfn(monkeypatch, cfn)
    deploy_pack(
        wrapper_url=_WRAPPER_URL,
        stack_name="claims",
        admin_email="a@example.com",
        region="us-east-1",
        extra_parameters={"LogLevel": "DEBUG", "NotAParameter": "x"},
        wait=False,
        console=_console(),
    )
    submitted = _submitted(cfn.created[0])
    assert submitted["LogLevel"] == "DEBUG"
    assert "NotAParameter" not in submitted


def test_a_wrapper_that_declares_no_admin_email_is_not_given_one(monkeypatch) -> None:
    """The gate works in both directions — the command's own two parameters are
    submitted conditionally too, not unconditionally. A wrapper that resolves the
    admin address itself would be refused outright if one were forced on it."""
    cfn = _WrapperCfn(["Something"])
    _install_cfn(monkeypatch, cfn)
    deploy_pack(
        wrapper_url=_WRAPPER_URL,
        stack_name="claims",
        admin_email="a@example.com",
        region="us-east-1",
        wait=False,
        console=_console(),
    )
    assert cfn.created[0]["Parameters"] == []


def test_a_wrapper_declaring_no_parameters_at_all_submits_none(monkeypatch) -> None:
    """The empty-declaration case, distinct from "we chose to send nothing": an
    empty list must reach CloudFormation, not a list built from the defaults."""
    cfn = _WrapperCfn([])
    _install_cfn(monkeypatch, cfn)
    deploy_pack(
        wrapper_url=_WRAPPER_URL,
        stack_name="claims",
        admin_email="a@example.com",
        region="us-east-1",
        extra_parameters={"LogLevel": "DEBUG"},
        wait=False,
        console=_console(),
    )
    assert cfn.created[0]["Parameters"] == []


def test_deploy_pack_uses_the_auto_expand_capability_set(monkeypatch) -> None:
    """A pack wrapper contains a nested `AWS::CloudFormation::Stack` and a SAM
    transform. Without CAPABILITY_AUTO_EXPAND the create is refused, and without
    the IAM capabilities the host stack's roles cannot be created."""
    cfn = _WrapperCfn(["AdminEmail"])
    _install_cfn(monkeypatch, cfn)
    deploy_pack(
        wrapper_url=_WRAPPER_URL,
        stack_name="claims",
        admin_email="a@example.com",
        region="us-east-1",
        wait=False,
        console=_console(),
    )
    assert cfn.created[0]["Capabilities"] == [
        "CAPABILITY_IAM",
        "CAPABILITY_NAMED_IAM",
        "CAPABILITY_AUTO_EXPAND",
    ]


def test_deploy_pack_updates_an_existing_wrapper_stack(monkeypatch) -> None:
    """A pack upgrade is an in-place wrapper update: the bumped FeatureVersion
    cascades into the nested feature stack and picks up the republished
    artifacts. Failing with AlreadyExists would make that impossible."""
    cfn = _WrapperCfn(["AdminEmail", "HostStackName", "LogLevel"], exists=True)
    _install_cfn(monkeypatch, cfn)
    arn = deploy_pack(
        wrapper_url=_WRAPPER_URL,
        stack_name="claims",
        admin_email="a@example.com",
        region="us-east-1",
        extra_parameters={"LogLevel": "DEBUG"},
        wait=False,
        console=_console(),
    )
    assert arn == _ARN
    assert cfn.created == []
    assert _submitted(cfn.updated[0])["LogLevel"] == "DEBUG"


def test_an_unreachable_wrapper_url_names_the_url_in_the_error(aws_env) -> None:
    """The commonest cause is a wrapper published privately and deployed
    cross-account. Naming the URL is what lets the operator see which bucket to
    check."""
    with mock_aws():
        boto3.client("cloudformation", region_name="us-east-1")
        with pytest.raises(RuntimeError, match="Failed to validate wrapper") as exc:
            deploy_pack(
                wrapper_url="https://nope.s3.us-east-1.amazonaws.com/x.yaml",
                stack_name="claims",
                admin_email="a@example.com",
                region="us-east-1",
                wait=False,
            )
    assert "nope.s3.us-east-1.amazonaws.com" in str(exc.value)


# ---------------------------------------------------------------------------
# _partition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("us-east-1", "aws"),
        ("us-gov-west-1", "aws-us-gov"),
        ("cn-north-1", "aws-cn"),
    ],
)
def test_the_partition_is_derived_from_the_region(region: str, expected: str) -> None:
    """A hardcoded `arn:aws:` S3 resource matches nothing in aws-us-gov or
    aws-cn, so a bucket policy built with it grants no access there — while
    appearing to apply."""
    assert _partition(region) == expected


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("us-gov-east-1", "aws-us-gov"),
        ("cn-northwest-1", "aws-cn"),
        ("eu-west-1", "aws"),
    ],
)
def test_the_partition_falls_back_to_a_prefix_check_when_boto3_cannot_answer(
    region: str, expected: str, monkeypatch
) -> None:
    """boto3's endpoint data does not know about a region added after the
    installed version, and it raises. Defaulting to `aws` in that case would
    silently produce an ARN that matches nothing in a GovCloud account."""

    class _Session:
        def get_partition_for_region(self, _region):
            raise RuntimeError("Could not find partition")

    monkeypatch.setattr("idp_feature_sdk.pack.boto3.Session", _Session)
    assert _partition(region) == expected


# ---------------------------------------------------------------------------
# _statement_list — the single-object Statement form
# ---------------------------------------------------------------------------


def test_a_single_statement_object_is_normalised_to_a_list() -> None:
    """The IAM grammar allows `Statement` to be one object rather than an array.
    Iterating that form directly yields its *keys*, so an operator's statement
    would be replaced by the strings "Sid", "Effect", "Principal" — a bucket
    policy that still applies and grants something entirely different."""
    statement = {"Sid": "OperatorRule", "Effect": "Allow", "Action": "s3:GetObject"}
    assert _statement_list({"Statement": statement}) == [statement]


def test_an_array_statement_is_copied_not_aliased() -> None:
    """Returning the caller's own list would let a later `append` mutate the
    policy dict the caller still holds."""
    original = [{"Sid": "A"}, {"Sid": "B"}]
    returned = _statement_list({"Statement": original})
    returned.append({"Sid": "C"})
    assert len(original) == 2


def test_no_policy_and_an_absent_statement_both_yield_an_empty_list() -> None:
    assert _statement_list(None) == []
    assert _statement_list({}) == []
    assert _statement_list({"Version": "2012-10-17"}) == []


def test_an_unsupported_statement_type_is_refused_by_name() -> None:
    """A string or a number here means the policy is not what we think it is.
    Proceeding would write a policy built from a misread one."""
    with pytest.raises(ValueError, match="str"):
        _statement_list({"Statement": "Allow"})


# ---------------------------------------------------------------------------
# apply_enforce_ssl_only
# ---------------------------------------------------------------------------


def test_the_ssl_statement_is_applied_to_a_bucket_with_no_policy(aws_env) -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        assert (
            apply_enforce_ssl_only(s3, _BUCKET, "us-east-1", console=_console()) is True
        )
        policy = json.loads(s3.get_bucket_policy(Bucket=_BUCKET)["Policy"])
    (statement,) = policy["Statement"]
    assert statement["Sid"] == "EnforceSSLOnly"
    assert statement["Effect"] == "Deny"
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert statement["Resource"] == [
        "arn:aws:s3:::artifacts-bkt",
        "arn:aws:s3:::artifacts-bkt/*",
    ]


def test_reapplying_the_ssl_statement_does_not_accumulate_copies(aws_env) -> None:
    """Publishing runs this on every invocation. Appending rather than replacing
    would grow the policy until it exceeds S3's 20KB limit, at which point every
    later publish fails."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        for _ in range(3):
            apply_enforce_ssl_only(s3, _BUCKET, "us-east-1", console=_console())
        policy = json.loads(s3.get_bucket_policy(Bucket=_BUCKET)["Policy"])
    assert len(policy["Statement"]) == 1


def test_an_operator_statement_stored_as_a_single_object_survives(aws_env) -> None:
    """The reason `_statement_list` exists, exercised end to end: a hand-written
    policy with one statement object must come back as two statements, both
    intact — not as a policy whose first statement is the string "Sid"."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        s3.put_bucket_policy(
            Bucket=_BUCKET,
            Policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": {
                        "Sid": "OperatorReadOnly",
                        "Effect": "Allow",
                        "Principal": {"AWS": "arn:aws:iam::123456789012:root"},
                        "Action": "s3:GetObject",
                        "Resource": "arn:aws:s3:::artifacts-bkt/*",
                    },
                }
            ),
        )
        apply_enforce_ssl_only(s3, _BUCKET, "us-east-1", console=_console())
        policy = json.loads(s3.get_bucket_policy(Bucket=_BUCKET)["Policy"])
    sids = [s["Sid"] for s in policy["Statement"]]
    assert sids == ["OperatorReadOnly", "EnforceSSLOnly"]


def test_a_policy_write_failure_raises_by_default(aws_env) -> None:
    """On a bucket we just created we own the policy, so a failure is a real
    problem and must stop the publish — the artifacts would otherwise be
    reachable over plain HTTP."""

    class _Denied:
        def get_bucket_policy(self, **_kw):
            raise RuntimeError("NoSuchBucketPolicy")

        def put_bucket_policy(self, **_kw):
            raise RuntimeError("AccessDenied: s3:PutBucketPolicy")

    with pytest.raises(RuntimeError) as exc:
        apply_enforce_ssl_only(_Denied(), _BUCKET, "us-east-1", console=_console())
    assert "EnforceSSLOnly" in str(exc.value)
    assert "add it manually" in str(exc.value)


def test_a_policy_write_failure_is_a_warning_on_a_pre_existing_bucket(
    aws_env,
) -> None:
    """The operator may own the policy on a bucket they supplied. Failing the
    publish there would make an operator-managed bucket unusable, so it warns and
    reports False — and the caller can tell the two apart."""

    class _Denied:
        def get_bucket_policy(self, **_kw):
            raise RuntimeError("NoSuchBucketPolicy")

        def put_bucket_policy(self, **_kw):
            raise RuntimeError("AccessDenied")

    console = _console()
    assert (
        apply_enforce_ssl_only(
            _Denied(), _BUCKET, "us-east-1", console=console, raise_on_error=False
        )
        is False
    )
    assert "Could not apply the EnforceSSLOnly" in " ".join(
        console.export_text().split()
    )


def test_an_unexpected_get_policy_error_is_not_mistaken_for_no_policy(
    aws_env,
) -> None:
    """Only `NoSuchBucketPolicy` means "there is no policy". Treating an
    AccessDenied the same way would overwrite a policy we could not read."""
    written: list = []

    class _Denied:
        def get_bucket_policy(self, **_kw):
            raise RuntimeError("AccessDenied: s3:GetBucketPolicy")

        def put_bucket_policy(self, **kw):
            written.append(kw)

    with pytest.raises(RuntimeError):
        apply_enforce_ssl_only(_Denied(), _BUCKET, "us-east-1", console=_console())
    assert written == [], "a policy we could not read must not be overwritten"


# ---------------------------------------------------------------------------
# apply_public_artifacts_policy
# ---------------------------------------------------------------------------


def test_the_public_policy_grants_read_on_both_published_prefixes(aws_env) -> None:
    """`extensions/*` and `host/*` are the two prefixes a cross-account deploy
    fetches from. Granting only one leaves either the feature artifacts or the
    host template unreadable, and the deploy 403s halfway."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        apply_public_artifacts_policy(s3, _BUCKET, console=_console())
        policy = json.loads(s3.get_bucket_policy(Bucket=_BUCKET)["Policy"])
        pab = s3.get_public_access_block(Bucket=_BUCKET)[
            "PublicAccessBlockConfiguration"
        ]
    (statement,) = [
        s for s in policy["Statement"] if s["Sid"] == "PackPublicArtifactsRead"
    ]
    assert statement["Action"] == "s3:GetObject"
    assert sorted(statement["Resource"]) == [
        "arn:aws:s3:::artifacts-bkt/extensions/*",
        "arn:aws:s3:::artifacts-bkt/host/*",
    ]
    # And the bucket is no longer blocking public policies.
    assert pab["BlockPublicPolicy"] is False
    assert pab["RestrictPublicBuckets"] is False
    # ACL blocking stays on — objects are made public by policy, not by ACL.
    assert pab["BlockPublicAcls"] is True


def test_the_public_policy_merges_rather_than_replacing(aws_env) -> None:
    """The EnforceSSLOnly statement is applied first and must survive. Replacing
    the policy would drop it, leaving the public artifacts readable over plain
    HTTP as well."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        apply_enforce_ssl_only(s3, _BUCKET, "us-east-1", console=_console())
        apply_public_artifacts_policy(s3, _BUCKET, console=_console())
        policy = json.loads(s3.get_bucket_policy(Bucket=_BUCKET)["Policy"])
    assert sorted(s["Sid"] for s in policy["Statement"]) == [
        "EnforceSSLOnly",
        "PackPublicArtifactsRead",
    ]


def test_reapplying_the_public_policy_does_not_duplicate_the_statement(
    aws_env,
) -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        for _ in range(3):
            apply_public_artifacts_policy(s3, _BUCKET, console=_console())
        policy = json.loads(s3.get_bucket_policy(Bucket=_BUCKET)["Policy"])
    assert len(policy["Statement"]) == 1


def test_a_block_that_does_not_stick_is_reported_rather_than_assumed(
    aws_env,
) -> None:
    """Account-level S3 Block Public Access silently overrides a *successful*
    bucket-level PutPublicAccessBlock. Trusting the call's return value would
    publish artifacts believed public, and the cross-account deploy 403s at
    download time in someone else's account."""

    class _AccountBlocked:
        def put_public_access_block(self, **_kw):
            return {}

        def get_public_access_block(self, **_kw):
            return {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            }

    with pytest.raises(RuntimeError) as exc:
        apply_public_artifacts_policy(_AccountBlocked(), _BUCKET, console=_console())
    assert "didn't stick" in str(exc.value)
    assert "account-level S3 Block Public Access" in str(exc.value)


def test_an_unreadable_block_configuration_does_not_block_the_publish(
    aws_env,
) -> None:
    """`s3:GetBucketPublicAccessBlock` may be denied while Put is allowed. An
    unreadable state is treated as "not blocking" so the publish proceeds — the
    alternative is refusing every publish on a bucket whose BPA we cannot read."""
    calls: list = []

    class _WriteOnly:
        exceptions = _S3Exceptions()

        def put_public_access_block(self, **_kw):
            return {}

        def get_public_access_block(self, **_kw):
            raise RuntimeError("AccessDenied")

        def get_bucket_policy(self, **_kw):
            raise _NoSuchBucketPolicy()

        def put_bucket_policy(self, **kw):
            calls.append(kw)

    apply_public_artifacts_policy(_WriteOnly(), _BUCKET, console=_console())
    assert len(calls) == 1
    assert "PackPublicArtifactsRead" in calls[0]["Policy"]


def test_a_failure_to_relax_the_block_is_actionable(aws_env) -> None:
    class _Denied:
        def put_public_access_block(self, **_kw):
            raise RuntimeError("AccessDenied")

    with pytest.raises(RuntimeError) as exc:
        apply_public_artifacts_policy(_Denied(), _BUCKET, console=_console())
    assert "BlockPublicPolicy=False" in str(exc.value)


def test_a_failure_to_write_the_public_policy_is_surfaced(aws_env) -> None:
    class _Denied:
        exceptions = _S3Exceptions()

        def put_public_access_block(self, **_kw):
            return {}

        def get_public_access_block(self, **_kw):
            return {"PublicAccessBlockConfiguration": {}}

        def get_bucket_policy(self, **_kw):
            raise RuntimeError("AccessDenied: s3:GetBucketPolicy")

        def put_bucket_policy(self, **_kw):
            raise AssertionError("must not be reached")

    with pytest.raises(RuntimeError, match="Failed to set public-read policy"):
        apply_public_artifacts_policy(_Denied(), _BUCKET, console=_console())


# ---------------------------------------------------------------------------
# publish_host_accelerator
# ---------------------------------------------------------------------------


def test_a_source_dir_without_publish_py_is_refused(tmp_path) -> None:
    """Better than shelling out into a directory that is not the accelerator
    repo: `idp-cli publish` there would either fail obscurely or publish the
    wrong template to `host/idp-main.yaml`."""
    with pytest.raises(FileNotFoundError, match="--source-dir"):
        publish_host_accelerator(
            source_dir=tmp_path,
            artifacts_bucket="b-us-east-1",
            artifacts_prefix="host",
            region="us-east-1",
        )


def test_the_bucket_is_split_back_into_the_basename_idp_cli_expects(
    tmp_path, monkeypatch
) -> None:
    """`idp-cli publish` takes a *basename* and appends the region itself. Passing
    the full bucket name would have it publish to `<bucket>-<region>-<region>`,
    while the URL returned here names `<bucket>` — so the wrapper would be baked
    with a host template URL pointing at a key that was never written."""
    (tmp_path / "publish.py").write_text("# host\n", encoding="utf-8")
    recorded: list = []
    monkeypatch.setattr(
        "idp_feature_sdk.pack.subprocess.run",
        lambda cmd, cwd=None, check=None: recorded.append((cmd, cwd)),
    )

    url = publish_host_accelerator(
        source_dir=tmp_path,
        artifacts_bucket="idp-artifacts-us-east-1",
        artifacts_prefix="host",
        region="us-east-1",
        console=_console(),
    )
    (cmd, cwd) = recorded[0]
    assert cmd[cmd.index("--bucket-basename") + 1] == "idp-artifacts"
    assert cmd[cmd.index("--region") + 1] == "us-east-1"
    assert cmd[cmd.index("--prefix") + 1] == "host"
    assert cmd[cmd.index("--source-dir") + 1] == str(tmp_path)
    assert cwd == tmp_path
    # basename + region must reconstitute the bucket the URL names.
    assert (
        url
        == "https://idp-artifacts-us-east-1.s3.us-east-1.amazonaws.com/host/idp-main.yaml"
    )


def test_a_bucket_not_ending_in_the_region_is_passed_through_whole(
    tmp_path, monkeypatch
) -> None:
    """An operator-supplied bucket need not follow the convention. Stripping a
    suffix that is not there would truncate the name."""
    (tmp_path / "publish.py").write_text("# host\n", encoding="utf-8")
    recorded: list = []
    monkeypatch.setattr(
        "idp_feature_sdk.pack.subprocess.run",
        lambda cmd, cwd=None, check=None: recorded.append(cmd),
    )
    url = publish_host_accelerator(
        source_dir=tmp_path,
        artifacts_bucket="my-corporate-bucket",
        artifacts_prefix="host",
        region="eu-west-1",
        console=_console(),
    )
    cmd = recorded[0]
    assert cmd[cmd.index("--bucket-basename") + 1] == "my-corporate-bucket"
    assert url == (
        "https://my-corporate-bucket.s3.eu-west-1.amazonaws.com/host/idp-main.yaml"
    )


def test_a_failing_idp_cli_publish_propagates(tmp_path, monkeypatch) -> None:
    """`check=True`: a host publish that failed must not leave the caller with a
    URL to a template that was never uploaded."""
    import subprocess

    (tmp_path / "publish.py").write_text("# host\n", encoding="utf-8")

    def _boom(cmd, cwd=None, check=None):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("idp_feature_sdk.pack.subprocess.run", _boom)
    with pytest.raises(subprocess.CalledProcessError):
        publish_host_accelerator(
            source_dir=tmp_path,
            artifacts_bucket="b-us-east-1",
            artifacts_prefix="host",
            region="us-east-1",
            console=_console(),
        )


# ---------------------------------------------------------------------------
# Helpers for the hand-rolled S3 stand-ins above
# ---------------------------------------------------------------------------


class _NoSuchBucketPolicy(Exception):
    pass


class _S3Exceptions:
    """Mimics ``client.exceptions.from_code(...)``, which the module uses to catch
    NoSuchBucketPolicy without importing botocore error classes."""

    def from_code(self, code: str):
        assert code == "NoSuchBucketPolicy"
        return _NoSuchBucketPolicy
