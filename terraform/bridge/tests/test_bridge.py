"""Offline tests for the custom-resource bridge Lambda.

S3 and Lambda are replaced with in-memory fakes: the handler under test is
"invoked" by writing the response the fake handler chooses to the response key
the bridge put in ResponseURL.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from botocore.exceptions import ClientError

BRIDGE_DIR = Path(__file__).resolve().parents[1]


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.denied_reads = 0

    def generate_presigned_url(self, _op, Params, ExpiresIn):  # noqa: N803
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?sig=1"

    def get_object(self, Bucket, Key):  # noqa: N803
        if self.denied_reads:
            self.denied_reads -= 1
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": SimpleNamespace(read=lambda: self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **_):  # noqa: N803
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)


class FakeLambda:
    """Plays the custom-resource handler: replies through the response URL."""

    def __init__(self, s3: FakeS3, replies):
        self.s3 = s3
        self.replies = list(replies)
        self.events: list[dict] = []

    def invoke(self, FunctionName, InvocationType, Payload):  # noqa: N803
        event = json.loads(Payload)
        self.events.append(event)
        status, physical, data, reason = self.replies.pop(0)
        key = urlparse(event["ResponseURL"]).path.split("/", 2)[2]
        self.s3.objects[key] = json.dumps(
            {
                "Status": status,
                "PhysicalResourceId": physical,
                "Data": data,
                "Reason": reason,
            }
        ).encode()


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("BRIDGE_BUCKET", "bucket")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    monkeypatch.syspath_prepend(str(BRIDGE_DIR))
    sys.modules.pop("index", None)
    module = importlib.import_module("index")
    module.time.sleep = lambda *_: None
    return module


def context():
    return SimpleNamespace(
        get_remaining_time_in_millis=lambda: 600_000,
        invoked_function_arn="arn:aws:lambda:us-west-2:111122223333:function:bridge",
        log_stream_name="stream",
    )


def wire(bridge, replies):
    s3 = FakeS3()
    lam = FakeLambda(s3, replies)
    bridge.s3, bridge.lam = s3, lam
    return s3, lam


def event(action, props, prev=None):
    e = {
        "ServiceToken": "arn:handler",
        "ResourceType": "Custom::Thing",
        "LogicalResourceId": "Thing",
        "StackName": "stk",
        "ResourceProperties": props,
        "tf": {"action": action},
    }
    if prev is not None:
        e["tf"]["prev_input"] = {"ResourceProperties": prev}
    return e


def test_create_builds_a_cloudformation_event_and_returns_data(bridge):
    s3, lam = wire(bridge, [("SUCCESS", "phys-1", {"Out": "v"}, "")])
    out = bridge.handler(event("create", {"N": 5, "B": True}), context())
    assert out == {"PhysicalResourceId": "phys-1", "Data": {"Out": "v"}}
    sent = lam.events[0]
    assert sent["RequestType"] == "Create"
    # CloudFormation hands every scalar to a handler as a string.
    assert sent["ResourceProperties"] == {"N": "5", "B": "true"}
    assert sent["StackId"].startswith(
        "arn:aws:cloudformation:us-west-2:111122223333:stack/stk/"
    )
    assert json.loads(s3.objects["state/stk/Thing.json"]) == {
        "PhysicalResourceId": "phys-1"
    }


def test_update_passes_old_properties_and_the_recorded_physical_id(bridge):
    s3, lam = wire(bridge, [("SUCCESS", "phys-1", {}, "")])
    s3.objects["state/stk/Thing.json"] = b'{"PhysicalResourceId": "phys-1"}'
    bridge.handler(event("update", {"N": 2}, prev={"N": 1}), context())
    sent = lam.events[0]
    assert sent["RequestType"] == "Update"
    assert sent["PhysicalResourceId"] == "phys-1"
    assert sent["OldResourceProperties"] == {"N": "1"}


def test_a_replacement_on_update_deletes_the_old_physical_resource(bridge):
    s3, lam = wire(
        bridge, [("SUCCESS", "phys-2", {}, ""), ("SUCCESS", "phys-1", {}, "")]
    )
    s3.objects["state/stk/Thing.json"] = b'{"PhysicalResourceId": "phys-1"}'
    bridge.handler(event("update", {"N": 2}, prev={"N": 1}), context())
    assert [e["RequestType"] for e in lam.events] == ["Update", "Delete"]
    assert lam.events[1]["PhysicalResourceId"] == "phys-1"
    assert json.loads(s3.objects["state/stk/Thing.json"]) == {
        "PhysicalResourceId": "phys-2"
    }


def test_delete_clears_the_record(bridge):
    s3, lam = wire(bridge, [("SUCCESS", "phys-1", {}, "")])
    s3.objects["state/stk/Thing.json"] = b'{"PhysicalResourceId": "phys-1"}'
    bridge.handler(event("delete", {}), context())
    assert lam.events[0]["RequestType"] == "Delete"
    assert "state/stk/Thing.json" not in s3.objects


def test_an_unrecorded_delete_still_reaches_the_handler_and_never_fails(bridge):
    s3, lam = wire(bridge, [("FAILED", "", {}, "boom")])
    out = bridge.handler(event("delete", {}), context())
    assert lam.events[0]["RequestType"] == "Delete"
    assert out["PhysicalResourceId"] == ""


def test_a_failed_response_fails_the_apply_with_the_reason(bridge):
    wire(bridge, [("FAILED", "", {}, "it broke")])
    with pytest.raises(RuntimeError, match="it broke"):
        bridge.handler(event("create", {}), context())


def test_access_denied_from_the_handler_is_retried_once(bridge):
    s3, lam = wire(
        bridge,
        [
            ("FAILED", "", {}, "AccessDeniedException: not yet"),
            ("SUCCESS", "p", {}, ""),
        ],
    )
    out = bridge.handler(event("create", {}), context())
    assert out["PhysicalResourceId"] == "p"
    assert len(lam.events) == 2


def test_reads_refused_while_iam_propagates_are_retried(bridge):
    s3, _ = wire(bridge, [("SUCCESS", "p", {}, "")])
    s3.denied_reads = 2
    assert bridge.handler(event("create", {}), context())["PhysicalResourceId"] == "p"
