import importlib
import os
import sys
from unittest.mock import MagicMock

import pytest

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../../../../nested/api-resolvers/src/lambda/discovery_upload_resolver",
    )
)


@pytest.fixture(autouse=True)
def _path_setup():
    sys.path.insert(0, LAMBDA_DIR)
    yield
    sys.path.remove(LAMBDA_DIR)
    sys.modules.pop("index", None)


def _reload():
    if "index" in sys.modules:
        del sys.modules["index"]
    return importlib.import_module("index")


def test_public_mode_path_addressing(monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mod = _reload()
    assert mod.s3_client.meta.config.s3["addressing_style"] == "path"
    assert mod.s3_client.meta.endpoint_url.endswith("amazonaws.com")


def test_private_mode_vpce(monkeypatch):
    monkeypatch.setenv(
        "S3_ENDPOINT_URL",
        "https://bucket.vpce-xyz.s3.us-west-2.vpce.amazonaws.com",
    )
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    mod = _reload()
    assert mod.s3_client.meta.config.s3["addressing_style"] == "virtual"
    assert (
        mod.s3_client.meta.endpoint_url
        == "https://bucket.vpce-xyz.s3.us-west-2.vpce.amazonaws.com"
    )


def _mock_config_manager(monkeypatch, existing):
    """Patch ConfigurationManager (imported lazily inside the resolver) so
    _clear_version_schema operates on an in-memory config dict."""
    manager = MagicMock()
    manager.get_raw_configuration.return_value = existing

    fake_cm_module = MagicMock()
    fake_cm_module.ConfigurationManager.return_value = manager
    monkeypatch.setitem(
        sys.modules, "idp_common.config.configuration_manager", fake_cm_module
    )
    return manager


def test_clear_version_schema_classes(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mod = _reload()
    existing = {
        "classes": [{"$id": "A"}, {"$id": "B"}],
        "extraction": {"model": "m"},
    }
    manager = _mock_config_manager(monkeypatch, existing)

    mod._clear_version_schema("v1", discovery_type="classes")

    # classes cleared, other sections preserved, saved back to same version
    saved_type, saved_config = manager.save_raw_configuration.call_args[0][:2]
    assert saved_type == "Config"
    assert saved_config["classes"] == []
    assert saved_config["extraction"] == {"model": "m"}
    assert manager.save_raw_configuration.call_args.kwargs["version"] == "v1"


def test_clear_version_schema_rules_clears_policy_classes(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mod = _reload()
    existing = {
        "policy_classes": [{"x-aws-idp-policy-type": "P"}],
        "classes": [{"$id": "A"}],
    }
    manager = _mock_config_manager(monkeypatch, existing)

    mod._clear_version_schema("v1", discovery_type="rules")

    saved_config = manager.save_raw_configuration.call_args[0][1]
    # rules discovery only clears policy_classes, leaves classes untouched
    assert saved_config["policy_classes"] == []
    assert saved_config["classes"] == [{"$id": "A"}]


def test_clear_version_schema_noop_when_empty(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mod = _reload()
    manager = _mock_config_manager(monkeypatch, {"classes": []})

    mod._clear_version_schema("v1", discovery_type="classes")

    # Nothing to clear -> no save call
    manager.save_raw_configuration.assert_not_called()


def test_clear_version_schema_noop_without_version(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mod = _reload()
    # No ConfigurationManager import should even happen; passing None returns early.
    mod._clear_version_schema(None, discovery_type="classes")


# --- multi-doc discovery zip upload -----------------------------------------
#
# The upload mutation (uploadMultiDocDiscoveryZip) and the start mutation
# (startMultiDocDiscovery) run as separate calls, and the UI passes only
# zipFileName to start — so both sides MUST derive the same deterministic
# object key, and the presigned URL must be a plain PUT URL (the UI uploads
# with fetch(url, {method: 'PUT'}), which cannot consume a POST form).


def _fake_creds(monkeypatch):
    """Presigned-URL generation signs locally but needs credentials configured."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


def test_multi_doc_zip_key_deterministic_and_sanitized(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    mod = _reload()
    assert (
        mod.multi_doc_zip_key("My Statements.zip")
        == "multi-doc-discovery/uploads/My_Statements.zip"
    )
    # Deterministic: same input, same key — no random job-id component.
    assert mod.multi_doc_zip_key("docs.zip") == mod.multi_doc_zip_key("docs.zip")


def test_upload_multi_doc_zip_returns_plain_put_url(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("DISCOVERY_BUCKET", "discovery-bucket")
    _fake_creds(monkeypatch)
    mod = _reload()

    result = mod.handle_upload_multi_doc_discovery_zip(
        {
            # An Admin: these tests are about the S3 key, and an Admin is never
            # profile-scoped, so the configuration-scope lookup is not reached.
            "identity": {"claims": {"cognito:groups": ["Admin"]}},
            "arguments": {
                "fileName": "docs.zip",
                "fileSize": 123,
                "configVersion": "v1",
            },
        },
        None,
    )

    # Plain presigned PUT URL string — not a json.dumps'd POST form.
    assert result["presignedUrl"].startswith("https://")
    assert not result["presignedUrl"].startswith("{")
    assert result["objectKey"] == mod.multi_doc_zip_key("docs.zip")


def test_start_multi_doc_discovery_fallback_matches_upload_key(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("DISCOVERY_BUCKET", "discovery-bucket")
    monkeypatch.delenv("DISCOVERY_TRACKING_TABLE", raising=False)
    monkeypatch.setenv(
        "MULTI_DOC_DISCOVERY_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:123456789012:stateMachine:multi-doc",
    )
    mod = _reload()
    mod.sfn_client = MagicMock()
    mod.sfn_client.start_execution.return_value = {"executionArn": "arn:exec"}

    mod.handle_start_multi_doc_discovery(
        {
            # An Admin: these tests are about the S3 key, and an Admin is never
            # profile-scoped, so the configuration-scope lookup is not reached.
            "identity": {"claims": {"cognito:groups": ["Admin"]}},
            "arguments": {
                "configVersion": "v1",
                "zipFileName": "docs.zip",
                "zipFileSize": 123,
            },
        },
        None,
    )

    import json

    sfn_input = json.loads(mod.sfn_client.start_execution.call_args.kwargs["input"])
    # The start handler must look where the upload handler put the zip.
    assert sfn_input["prefix"] == mod.multi_doc_zip_key("docs.zip")
    assert sfn_input["bucket"] == "discovery-bucket"
    assert sfn_input["isZipUpload"] is True


def test_start_multi_doc_discovery_honors_explicit_prefix(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("DISCOVERY_BUCKET", "discovery-bucket")
    monkeypatch.delenv("DISCOVERY_TRACKING_TABLE", raising=False)
    monkeypatch.setenv(
        "MULTI_DOC_DISCOVERY_STATE_MACHINE_ARN",
        "arn:aws:states:us-east-1:123456789012:stateMachine:multi-doc",
    )
    mod = _reload()
    mod.sfn_client = MagicMock()
    mod.sfn_client.start_execution.return_value = {"executionArn": "arn:exec"}

    mod.handle_start_multi_doc_discovery(
        {
            # An Admin: these tests are about the S3 key, and an Admin is never
            # profile-scoped, so the configuration-scope lookup is not reached.
            "identity": {"claims": {"cognito:groups": ["Admin"]}},
            "arguments": {
                "configVersion": "v1",
                "zipFileName": "docs.zip",
                "s3Prefix": "custom/location/docs.zip",
            },
        },
        None,
    )

    import json

    sfn_input = json.loads(mod.sfn_client.start_execution.call_args.kwargs["input"])
    # Callers that pass the objectKey back as s3Prefix are honored verbatim.
    assert sfn_input["prefix"] == "custom/location/docs.zip"
