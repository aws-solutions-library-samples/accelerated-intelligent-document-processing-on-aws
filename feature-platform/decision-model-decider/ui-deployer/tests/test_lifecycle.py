# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the ui-deployer does on Create and Delete, with the AWS clients stubbed.

Create copies the bundle, registers the feature, applies the preset with the
endpoint filled in, and answers CloudFormation SUCCESS. Delete unregisters and
removes the preset, and still answers SUCCESS when one of those fails. A failure
on Create answers FAILED with the reason.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path

import pytest

_HANDLER_DIR = Path(__file__).resolve().parents[1]
_PRESET = _HANDLER_DIR.parent / "config-preset" / "section-classification.yaml"
ALIAS_ARN = "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-decider:live"
REGISTER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:Register"
PRESET_ARN = "arn:aws:lambda:us-east-1:123456789012:function:ApplyPreset"


class _S3:
    def __init__(self):
        self.copies: list = []
        self.deletes: list = []
        self.gets: list = []

    def copy_object(self, **kwargs):
        self.copies.append(kwargs)

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs)

    def get_object(self, Bucket, Key):
        self.gets.append((Bucket, Key))
        return {"Body": io.BytesIO(_PRESET.read_bytes())}


class _Lambda:
    def __init__(self, fail_field: str | None = None):
        self.calls: list = []
        self.fail_field = fail_field

    def invoke(self, FunctionName, InvocationType, Payload):
        payload = json.loads(Payload)
        self.calls.append((FunctionName, payload))
        failed = payload["info"]["fieldName"] == self.fail_field
        return {
            "Payload": io.BytesIO(b'{"error": "boom"}' if failed else b"{}"),
            **({"FunctionError": "Unhandled"} if failed else {}),
        }


@pytest.fixture
def handler(monkeypatch):
    for name, value in {
        "FEATURE_ID": "decision-model-decider",
        "FEATURE_DISPLAY_NAME": "Decision Model: Strands Decider",
        "FEATURE_VERSION": "0.1.0",
        "WEBUI_BUCKET": "webui",
        "FEATURE_BUCKET": "artifacts",
        "FEATURE_ARTIFACT_PREFIX": "idp/extensions/decision-model-decider",
        "REGISTER_FEATURE_FUNCTION_ARN": REGISTER_ARN,
        "APPLY_FEATURE_CONFIG_PRESET_FUNCTION_ARN": PRESET_ARN,
        "DECIDER_ENDPOINT": ALIAS_ARN,
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    sys.path.insert(0, str(_HANDLER_DIR))
    sys.modules.pop("handler", None)
    try:
        module = importlib.import_module("handler")
    finally:
        sys.path.remove(str(_HANDLER_DIR))
    responses: list = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b""

    def _urlopen(req, timeout):
        responses.append((req.full_url, req.get_method(), json.loads(req.data)))
        return _Response()

    class _Sts:
        def get_caller_identity(self):
            return {"Arn": "arn:aws:sts::123456789012:assumed-role/cfn/x"}

    monkeypatch.setattr(module.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(module.boto3, "client", lambda name, **_kw: _Sts())
    module.responses = responses
    yield module
    sys.modules.pop("handler", None)


def _event(request_type: str) -> dict:
    return {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource-response.example/x",
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/host-feature-decision-model-decider/abc",
        "RequestId": "r-1",
        "LogicalResourceId": "RegisterFeatureResource",
    }


def test_create_registers_and_applies_the_preset_with_the_endpoint(
    handler, monkeypatch
):
    s3, lam = _S3(), _Lambda()
    monkeypatch.setattr(handler, "_s3", s3)
    monkeypatch.setattr(handler, "_lambda", lam)

    handler.lambda_handler(_event("Create"), None)

    assert s3.copies[0]["Key"] == (
        "features/decision-model-decider/v0.1.0/ui-bundle.js"
    )
    assert s3.copies[0]["CopySource"] == {
        "Bucket": "artifacts",
        "Key": "idp/extensions/decision-model-decider/0.1.0/ui-bundle.js",
    }
    assert s3.gets == [
        (
            "artifacts",
            "idp/extensions/decision-model-decider/0.1.0/"
            "config-preset/section-classification.yaml",
        )
    ]
    fields = [(arn, p["info"]["fieldName"]) for arn, p in lam.calls]
    assert fields == [
        (REGISTER_ARN, "registerFeature"),
        (PRESET_ARN, "applyFeatureConfigPreset"),
    ]
    registered = lam.calls[0][1]["arguments"]["input"]
    assert registered["featureId"] == "decision-model-decider"
    assert registered["stackName"] == "host-feature-decision-model-decider"
    assert registered["uiBundlePath"] == "features/decision-model-decider/v0.1.0/"
    applied = lam.calls[1][1]["arguments"]["input"]
    config = json.loads(applied["config"])
    stc = config["classification"]["split_then_classify"]["section_classification"]
    assert stc["decision_model"]["endpoint"] == ALIAS_ARN
    assert applied["featureId"] == "decision-model-decider"
    url, method, body = handler.responses[-1]
    assert method == "PUT" and url == _event("Create")["ResponseURL"]
    assert body["Status"] == "SUCCESS"
    assert body["RequestId"] == "r-1"


def test_a_failed_preset_fails_the_create_with_the_reason(handler, monkeypatch):
    monkeypatch.setattr(handler, "_s3", _S3())
    monkeypatch.setattr(
        handler, "_lambda", _Lambda(fail_field="applyFeatureConfigPreset")
    )

    handler.lambda_handler(_event("Create"), None)

    body = handler.responses[-1][2]
    assert body["Status"] == "FAILED"
    assert "applyFeatureConfigPreset resolver failed" in body["Reason"]


def test_delete_unregisters_and_removes_the_preset_even_when_one_fails(
    handler, monkeypatch
):
    s3, lam = _S3(), _Lambda(fail_field="unregisterFeature")
    monkeypatch.setattr(handler, "_s3", s3)
    monkeypatch.setattr(handler, "_lambda", lam)

    handler.lambda_handler(_event("Delete"), None)

    assert [p["info"]["fieldName"] for _, p in lam.calls] == [
        "unregisterFeature",
        "removeFeatureConfigPreset",
    ]
    assert s3.deletes[0]["Key"] == (
        "features/decision-model-decider/v0.1.0/ui-bundle.js"
    )
    assert handler.responses[-1][2]["Status"] == "SUCCESS"
