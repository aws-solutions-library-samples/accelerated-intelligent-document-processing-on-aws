# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Custom::CustomerManagedKmsKeyCheck keeps CustomerManagedKmsKeyArn a
creation-time choice (#2016).

Pinned here: an update that changes the value is refused in either direction
(set, cleared, swapped), except while the stack rolls back; a create that adds
a supplied key to an existing stack is refused; a supplied key on a new stack
is checked for account, Region, manager, spec, usage and state; and the empty
default makes no API call at all.

The handler test drives real botocore clients behind ``Stubber``, so an
operation or parameter name the CloudFormation or KMS service model does not
have fails here rather than in a deployment.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

pytestmark = pytest.mark.unit

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
_spec = importlib.util.spec_from_file_location(
    "customer_managed_kms_key_check_index", _HERE / "index.py"
)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

REGION = "us-east-1"
ACCOUNT = "111122223333"
STACK_ID = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/idp/0a1b2c3d-1111-2222-3333-abcdef012345"
KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/1234abcd-12ab-34cd-56ef-1234567890ab"
OTHER_KEY = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/0987dcba-09fe-87dc-65ba-ab0987654321"

GOOD_META = {
    "AWSAccountId": ACCOUNT,
    "KeyManager": "CUSTOMER",
    "KeySpec": "SYMMETRIC_DEFAULT",
    "KeyUsage": "ENCRYPT_DECRYPT",
    "KeyState": "Enabled",
}


def _event(request, new, old=None):
    e = {
        "RequestType": request,
        "StackId": STACK_ID,
        "ResourceProperties": {"KeyArn": new, "StackName": "idp"},
    }
    if old is not None:
        e["OldResourceProperties"] = {"KeyArn": old, "StackName": "idp"}
    return e


class _Calls:
    def __init__(self, status="CREATE_IN_PROGRESS", meta=None, key_error=None):
        self.status, self.meta, self.key_error = status, meta or GOOD_META, key_error
        self.status_calls, self.key_calls = [], []

    def stack_status(self, stack_id):
        self.status_calls.append(stack_id)
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def describe_key(self, arn):
        self.key_calls.append(arn)
        if self.key_error:
            raise self.key_error
        return self.meta


def _decide(event, calls):
    return mod.decide(event, calls.stack_status, calls.describe_key, REGION, ACCOUNT)


class TestUpdate:
    @pytest.mark.parametrize(
        "old,new",
        [("", KEY), (KEY, ""), (KEY, OTHER_KEY)],
        ids=["set-on-existing", "cleared", "swapped"],
    )
    def test_a_change_in_either_direction_is_refused(self, old, new):
        calls = _Calls(status="UPDATE_IN_PROGRESS")
        status, reason = _decide(_event("Update", new, old), calls)
        assert status == mod.FAILED
        assert "fixed when the stack is created" in reason
        assert calls.key_calls == []

    def test_an_unchanged_value_passes_without_reading_anything(self):
        calls = _Calls(status=RuntimeError("must not be read"))
        assert _decide(_event("Update", KEY, KEY), calls)[0] == mod.SUCCESS
        assert calls.status_calls == []

    @pytest.mark.parametrize(
        "status", ["UPDATE_ROLLBACK_IN_PROGRESS", "ROLLBACK_IN_PROGRESS"]
    )
    def test_a_rollback_restoring_the_previous_value_passes(self, status):
        assert (
            _decide(_event("Update", "", KEY), _Calls(status=status))[0] == mod.SUCCESS
        )

    @pytest.mark.parametrize(
        "status",
        [RuntimeError("throttled"), mod.NO_STACK],
        ids=["lookup-error", "terraform"],
    )
    def test_a_change_fails_closed_when_the_status_is_not_a_rollback(self, status):
        assert (
            _decide(_event("Update", KEY, ""), _Calls(status=status))[0] == mod.FAILED
        )


class TestCreate:
    def test_the_empty_default_makes_no_call(self):
        calls = _Calls(status=RuntimeError("must not be read"))
        assert _decide(_event("Create", ""), calls)[0] == mod.SUCCESS
        assert calls.status_calls == [] and calls.key_calls == []

    def test_a_supplied_key_on_an_existing_stack_being_updated_is_refused(self):
        calls = _Calls(status="UPDATE_IN_PROGRESS")
        status, reason = _decide(_event("Create", KEY), calls)
        assert status == mod.FAILED and "only be set when a stack is created" in reason
        assert calls.key_calls == []

    @pytest.mark.parametrize("status", ["CREATE_IN_PROGRESS", mod.NO_STACK])
    def test_a_good_key_on_a_new_stack_passes(self, status):
        calls = _Calls(status=status)
        assert _decide(_event("Create", KEY), calls)[0] == mod.SUCCESS
        assert calls.key_calls == [KEY]

    def test_a_status_lookup_error_fails_a_supplied_key(self):
        assert (
            _decide(_event("Create", KEY), _Calls(status=RuntimeError("x")))[0]
            == mod.FAILED
        )

    @pytest.mark.parametrize(
        "field,value,needle",
        [
            ("AWSAccountId", "999988887777", "account"),
            ("KeyManager", "AWS", "AWS managed"),
            ("KeySpec", "RSA_2048", "symmetric"),
            ("KeyUsage", "SIGN_VERIFY", "ENCRYPT_DECRYPT"),
            ("KeyState", "PendingDeletion", "Enabled"),
            ("KeyState", "Disabled", "Enabled"),
        ],
    )
    def test_an_unusable_key_is_refused_with_its_reason(self, field, value, needle):
        calls = _Calls(meta=dict(GOOD_META, **{field: value}))
        status, reason = _decide(_event("Create", KEY), calls)
        assert status == mod.FAILED and needle in reason

    def test_a_key_in_another_region_is_refused(self):
        other = KEY.replace(REGION, "eu-west-1")
        status, reason = _decide(_event("Create", other), _Calls())
        assert status == mod.FAILED and "Region" in reason

    def test_a_key_that_cannot_be_described_is_refused(self):
        err = ClientError(
            {"Error": {"Code": "NotFoundException", "Message": "x"}}, "DescribeKey"
        )
        status, reason = _decide(_event("Create", KEY), _Calls(key_error=err))
        assert status == mod.FAILED and "NotFoundException" in reason


def test_delete_always_succeeds():
    calls = _Calls(status=RuntimeError("must not be read"))
    assert _decide(_event("Delete", KEY), calls)[0] == mod.SUCCESS
    assert calls.status_calls == []


def _run_handler(event, cfn_responses, kms_responses):
    cfn = boto3.client("cloudformation", region_name=REGION)
    kms = boto3.client("kms", region_name=REGION)
    sent = {}
    with Stubber(cfn) as cs, Stubber(kms) as ks:
        for op, resp, params in cfn_responses:
            if isinstance(resp, dict):
                cs.add_response(op, resp, params)
            else:
                cs.add_client_error(
                    op,
                    service_error_code=resp[0],
                    service_message=resp[1],
                    expected_params=params,
                )
        for op, resp, params in kms_responses:
            ks.add_response(op, resp, params)

        def client(name, **_):
            return {"cloudformation": cfn, "kms": kms}[name]

        def send(
            event, context, status, data, physicalResourceId=None, reason=None, **_
        ):
            sent.update(
                status=status, data=data, physical=physicalResourceId, reason=reason
            )

        ctx = SimpleNamespace(
            invoked_function_arn=f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:f"
        )
        with (
            patch.object(mod.boto3, "client", side_effect=client),
            patch.object(mod.cfnresponse, "send", side_effect=send),
            patch.dict("os.environ", {"AWS_REGION": REGION}),
        ):
            mod.handler(dict(event, ResponseURL="https://example.invalid/r"), ctx)
        cs.assert_no_pending_responses()
        ks.assert_no_pending_responses()
    return sent


def test_handler_checks_a_new_stack_key_through_the_service_models():
    sent = _run_handler(
        _event("Create", KEY),
        [
            (
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "idp",
                            "CreationTime": "2026-01-01T00:00:00Z",
                            "StackStatus": "CREATE_IN_PROGRESS",
                        }
                    ]
                },
                {"StackName": STACK_ID},
            )
        ],
        [
            (
                "describe_key",
                {
                    "KeyMetadata": dict(
                        GOOD_META, KeyId="1234abcd-12ab-34cd-56ef-1234567890ab"
                    )
                },
                {"KeyId": KEY},
            )
        ],
    )
    assert sent["status"] == mod.SUCCESS
    assert sent["data"] == {"KeyArn": KEY}
    assert sent["physical"] == "idp-kms-key-check"


def test_handler_reads_a_missing_stack_as_terraform():
    """The bridge's synthetic stack id: DescribeStacks answers ValidationError."""
    sent = _run_handler(
        _event("Create", KEY),
        [
            (
                "describe_stacks",
                ("ValidationError", f"Stack with id {STACK_ID} does not exist"),
                {"StackName": STACK_ID},
            )
        ],
        [("describe_key", {"KeyMetadata": dict(GOOD_META, KeyId="k")}, {"KeyId": KEY})],
    )
    assert sent["status"] == mod.SUCCESS


def test_handler_refuses_a_clear_and_keeps_the_physical_id():
    event = dict(_event("Update", "", KEY), PhysicalResourceId="idp-kms-key-check")
    sent = _run_handler(
        event,
        [
            (
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "idp",
                            "CreationTime": "2026-01-01T00:00:00Z",
                            "StackStatus": "UPDATE_IN_PROGRESS",
                        }
                    ]
                },
                {"StackName": STACK_ID},
            )
        ],
        [],
    )
    assert sent["status"] == mod.FAILED
    assert sent["physical"] == "idp-kms-key-check"
    json.dumps(sent)  # the response body must serialise


@pytest.mark.parametrize(
    "request_type,status", [("Delete", "SUCCESS"), ("Create", "FAILED")]
)
def test_an_unexpected_error_never_blocks_a_delete(request_type, status):
    """A Delete that failed would wedge the stack's deletion; anything else that
    errors is reported as FAILED rather than left waiting."""
    sent = {}

    def send(event, context, s, data, physicalResourceId=None, reason=None, **_):
        sent.update(status=s, reason=reason)

    ctx = SimpleNamespace(
        invoked_function_arn=f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:f"
    )
    with (
        patch.object(mod, "decide", side_effect=RuntimeError("boom")),
        patch.object(mod.boto3, "client"),
        patch.object(mod.cfnresponse, "send", side_effect=send),
        patch.dict("os.environ", {"AWS_REGION": REGION}),
    ):
        mod.handler(
            dict(_event(request_type, KEY), ResponseURL="https://example.invalid/r"),
            ctx,
        )
    assert sent["status"] == status
    assert "boom" in sent["reason"]


@pytest.mark.parametrize(
    "code,message",
    [
        ("ValidationError", "Rate exceeded for this operation"),
        ("AccessDenied", "Role does not exist or is not authorized"),
    ],
)
def test_only_a_missing_stack_reads_as_terraform(code, message):
    """Any other DescribeStacks error is a failed lookup, so a supplied key on a
    Create is refused rather than checked as if no stack existed."""
    sent = _run_handler(
        _event("Create", KEY),
        [("describe_stacks", (code, message), {"StackName": STACK_ID})],
        [],
    )
    assert sent["status"] == mod.FAILED
    assert "Could not read this stack's status" in sent["reason"]
