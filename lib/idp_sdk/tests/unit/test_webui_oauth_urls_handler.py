# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the WebUIClientOAuthUrls custom-resource inline Lambda.

That handler (embedded in template.yaml as InlineCode) does a read-modify-write
of the Cognito user-pool client's CallbackURLs/LogoutURLs to add the API Gateway
``/api`` URLs post-deploy (WebUIHosting=APIGateway). This is the most bug-prone
new code in the API Gateway hosting change, so this test extracts the inline
source straight from the template and exercises it with a fake Cognito client:
add-on-create, remove-on-delete, placeholder removal, and field preservation.
"""

import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PLACEHOLDER = "https://localhost/apigw-placeholder"
BASE_URL = "https://abc123.execute-api.us-gov-west-1.amazonaws.com/api"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


def _load_handler_module(fake_cognito):
    """Extract the inline handler source from template.yaml and exec it.

    Injects a fake ``boto3`` (returning fake_cognito) and a no-op ``cfnresponse``
    so the module imports cleanly outside Lambda.
    """

    cfnlint_decode = pytest.importorskip("cfnlint.decode.cfn_yaml")

    def _plain(node):
        if isinstance(node, dict):
            return {str(k): _plain(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_plain(x) for x in node]
        if isinstance(node, str):
            return str(node)
        return node

    template = _plain(cfnlint_decode.load(str(_repo_root() / "template.yaml")))
    code = template["Resources"]["WebUIClientOAuthUrlsFunction"]["Properties"][
        "InlineCode"
    ]
    assert isinstance(code, str) and "def handler" in code

    # Fake boto3 + cfnresponse so the inline module runs outside Lambda.
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, *a, **k: fake_cognito  # noqa: ARG005

    sent = {}
    fake_cfnresponse = types.ModuleType("cfnresponse")
    fake_cfnresponse.SUCCESS = "SUCCESS"
    fake_cfnresponse.FAILED = "FAILED"

    def _send(event, context, status, data, *a, **k):  # noqa: ARG001
        sent["status"] = status
        sent["data"] = data

    fake_cfnresponse.send = _send

    sys.modules["boto3"] = fake_boto3
    sys.modules["cfnresponse"] = fake_cfnresponse

    mod = types.ModuleType("webui_oauth_handler")
    exec(compile(code, "<WebUIClientOAuthUrlsFunction>", "exec"), mod.__dict__)  # nosec B102 - executes this repo's own shipped handler source under test
    return mod, sent


class _FakeCognito:
    """Minimal stand-in for the cognito-idp client used by the handler."""

    def __init__(self, callback=None, logout=None, extra=None):
        self.client = {
            "ClientName": "stack-Client",
            "CallbackURLs": list(callback or []),
            "LogoutURLs": list(logout or []),
            "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH"],
            "SupportedIdentityProviders": ["COGNITO"],
            "AllowedOAuthFlows": ["code"],
        }
        if extra:
            self.client.update(extra)
        self.updated_with = None

    def describe_user_pool_client(self, UserPoolId, ClientId):  # noqa: N803
        return {"UserPoolClient": dict(self.client)}

    def update_user_pool_client(self, **kwargs):
        self.updated_with = kwargs
        return {}


def _props():
    return {
        "UserPoolId": "us-gov-west-1_pool",
        "UserPoolClientId": "client123",
        "WebUIBaseUrl": BASE_URL,
    }


def test_create_adds_both_url_forms_and_removes_placeholder():
    fake = _FakeCognito(callback=[PLACEHOLDER], logout=[PLACEHOLDER])
    mod, sent = _load_handler_module(fake)
    mod.handler({"RequestType": "Create", "ResourceProperties": _props()}, None)

    assert sent["status"] == "SUCCESS"
    cb = set(fake.updated_with["CallbackURLs"])
    lo = set(fake.updated_with["LogoutURLs"])
    assert cb == {BASE_URL, BASE_URL + "/"}
    assert lo == {BASE_URL, BASE_URL + "/"}
    assert PLACEHOLDER not in cb and PLACEHOLDER not in lo


def test_update_is_idempotent_and_reapplies_urls():
    # Simulate drift: client got reset to just the placeholder.
    fake = _FakeCognito(callback=[PLACEHOLDER], logout=[PLACEHOLDER])
    mod, _ = _load_handler_module(fake)
    mod.handler({"RequestType": "Update", "ResourceProperties": _props()}, None)
    cb = set(fake.updated_with["CallbackURLs"])
    assert BASE_URL in cb and BASE_URL + "/" in cb and PLACEHOLDER not in cb


def test_delete_removes_the_urls():
    fake = _FakeCognito(
        callback=[BASE_URL, BASE_URL + "/", "https://keep.example.com"],
        logout=[BASE_URL, BASE_URL + "/"],
    )
    mod, sent = _load_handler_module(fake)
    mod.handler({"RequestType": "Delete", "ResourceProperties": _props()}, None)
    assert sent["status"] == "SUCCESS"
    cb = set(fake.updated_with["CallbackURLs"])
    assert BASE_URL not in cb and BASE_URL + "/" not in cb
    # Unrelated URLs are preserved.
    assert "https://keep.example.com" in cb


def test_preserves_existing_writable_fields():
    fake = _FakeCognito(callback=[PLACEHOLDER])
    mod, _ = _load_handler_module(fake)
    mod.handler({"RequestType": "Create", "ResourceProperties": _props()}, None)
    # Fields present on the client must be carried into the update call.
    assert fake.updated_with["ExplicitAuthFlows"] == ["ALLOW_USER_SRP_AUTH"]
    assert fake.updated_with["SupportedIdentityProviders"] == ["COGNITO"]
    assert fake.updated_with["AllowedOAuthFlows"] == ["code"]


def test_delete_never_blocks_teardown_on_error():
    class _Boom(_FakeCognito):
        def describe_user_pool_client(self, UserPoolId, ClientId):  # noqa: N803
            raise RuntimeError("cognito unavailable")

    fake = _Boom()
    mod, sent = _load_handler_module(fake)
    mod.handler({"RequestType": "Delete", "ResourceProperties": _props()}, None)
    # Delete must still report SUCCESS so stack teardown is not blocked.
    assert sent["status"] == "SUCCESS"
