"""Unit tests for the unsubscribe_feature Lambda.

The product code comes from the feature's InstalledFeatures row (DynamoDB, via
moto) — baked from the manifest at install — not a host env map. Tests that
exercise the expire flow seed that row via the `installed_features_table`
fixture; the simulator POST itself is mocked at urllib.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import boto3
import pytest
from _helpers import make_appsync_event
from botocore.stub import Stubber


def _seed_row(table_name, feature_id, *, product_code=None):
    item = {"featureId": feature_id}
    if product_code is not None:
        item["productCode"] = product_code
    boto3.resource("dynamodb", region_name="us-east-1").Table(table_name).put_item(
        Item=item
    )


def _preload(
    monkeypatch,
    load_lambda,
    *,
    table_name="",
    simulator_endpoint="http://sim.example.com",
    default_customer="CUST-default",
    buyer_account="111122223333",
    source_tag="simulator",
):
    monkeypatch.setenv("SIMULATOR_ADMIN_ENDPOINT", simulator_endpoint)
    monkeypatch.setenv("INSTALLED_FEATURES_TABLE", table_name)
    monkeypatch.setenv("DEFAULT_CUSTOMER_IDENTIFIER", default_customer)
    monkeypatch.setenv("DEFAULT_BUYER_ACCOUNT_ID", buyer_account)
    monkeypatch.setenv("ADMIN_GROUP", "Admin")
    monkeypatch.setenv("SIMULATOR_SOURCE_TAG", source_tag)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    return load_lambda("unsubscribe_feature")


def _mock_urlopen_response(mod, body: dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return patch.object(mod.urllib.request, "urlopen", return_value=resp)


def test_happy_path(monkeypatch, load_lambda, installed_features_table):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    sim_body = {
        "customerIdentifier": "CUST-default",
        "productCode": "prod123",
        "featureId": "docs-by-status",
        "state": "EXPIRED",
        "expiresAt": 1_700_000_000.0,
    }
    with _mock_urlopen_response(mod, sim_body) as patched:
        result = mod.handler(
            make_appsync_event(
                "unsubscribeFeature",
                {"featureId": "docs-by-status"},
                groups=["Admin"],
            ),
            None,
        )
        call = patched.call_args
        req = call.args[0]
        assert req.full_url == "http://sim.example.com/admin/entitlements/expire"
        assert req.get_method() == "POST"
        sent = json.loads(req.data.decode("utf-8"))
        assert sent["customerIdentifier"] == "CUST-default"
        assert sent["productCode"] == "prod123"
        assert sent["featureId"] == "docs-by-status"

    assert result["featureId"] == "docs-by-status"
    assert result["state"] == "EXPIRED"
    assert result["source"] == "simulated"
    assert result["expiresAt"].endswith("Z")


def test_rejects_non_admin(monkeypatch, load_lambda, installed_features_table):
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    with pytest.raises(Exception, match="Admin"):
        mod.handler(
            make_appsync_event(
                "unsubscribeFeature",
                {"featureId": "docs-by-status"},
                groups=["Author"],
            ),
            None,
        )


def test_missing_feature_id(monkeypatch, load_lambda, installed_features_table):
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    with pytest.raises(ValueError, match="featureId"):
        mod.handler(
            make_appsync_event("unsubscribeFeature", {}, groups=["Admin"]),
            None,
        )


def test_missing_product_code_marketplace_mode_raises(
    monkeypatch, load_lambda, installed_features_table
):
    """In marketplace mode, a feature whose install row has no productCode raises."""
    _seed_row(installed_features_table, "docs-by-status")  # no productCode
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="marketplace",
    )
    with pytest.raises(mod.UnsubscribeError, match="productCode"):
        mod.handler(
            make_appsync_event(
                "unsubscribeFeature",
                {"featureId": "docs-by-status"},
                groups=["Admin"],
            ),
            None,
        )


def test_missing_product_code_simulator_mode_synthesizes(
    monkeypatch, load_lambda, installed_features_table
):
    """In simulator mode, a row without a productCode is synthesized (prod-<id>-sim)."""
    _seed_row(installed_features_table, "docs-by-status")  # no productCode
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        source_tag="simulator",
    )
    captured: dict = {}

    def fake_post(url, body):  # noqa: ARG001
        captured["body"] = body
        return {"expiresAt": None}

    monkeypatch.setattr(mod, "_post_json", fake_post)
    result = mod.handler(
        make_appsync_event(
            "unsubscribeFeature",
            {"featureId": "docs-by-status"},
            groups=["Admin"],
        ),
        None,
    )
    assert result["state"] == "EXPIRED"
    assert result["productCode"] == "prod-docs-by-status-sim"
    assert captured["body"]["productCode"] == "prod-docs-by-status-sim"


def test_missing_simulator_endpoint(monkeypatch, load_lambda, installed_features_table):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        simulator_endpoint="",
    )
    with pytest.raises(mod.UnsubscribeError, match="SIMULATOR_ADMIN_ENDPOINT"):
        mod.handler(
            make_appsync_event(
                "unsubscribeFeature",
                {"featureId": "docs-by-status"},
                groups=["Admin"],
            ),
            None,
        )


def test_simulator_http_error(monkeypatch, load_lambda, installed_features_table):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    err = HTTPError(
        url="http://sim.example.com/admin/entitlements/expire",
        code=500,
        msg="boom",
        hdrs=None,
        fp=io.BytesIO(b'{"error":"nope"}'),
    )
    with patch.object(mod.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(mod.UnsubscribeError, match="500"):
            mod.handler(
                make_appsync_event(
                    "unsubscribeFeature",
                    {"featureId": "docs-by-status"},
                    groups=["Admin"],
                ),
                None,
            )


def test_simulator_connection_refused(
    monkeypatch, load_lambda, installed_features_table
):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    err = URLError("connection refused")
    with patch.object(mod.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(mod.UnsubscribeError, match="reach simulator"):
            mod.handler(
                make_appsync_event(
                    "unsubscribeFeature",
                    {"featureId": "docs-by-status"},
                    groups=["Admin"],
                ),
                None,
            )


def test_no_customer_identifier_resolves_by_buyer_account(
    monkeypatch, load_lambda, installed_features_table
):
    """With no concrete CustomerIdentifier, unsubscribe resolves the random
    simulator-minted id via GetEntitlements filtered by the buyer AWS account,
    then expires THAT id against the simulator admin API."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        default_customer="",  # force the account-resolution path
        buyer_account="111122223333",
    )
    # Stub GetEntitlements(by account) → the random id the subscription minted.
    stubber = Stubber(mod._client())
    stubber.add_response(
        "get_entitlements",
        {"Entitlements": [{"CustomerIdentifier": "cust-62c036d80d5c"}]},
        {
            "ProductCode": "prod123",
            "Filter": {"CUSTOMER_AWS_ACCOUNT_ID": ["111122223333"]},
        },
    )
    stubber.activate()
    sim_body = {"state": "EXPIRED", "expiresAt": 1_700_000_000.0}
    try:
        with _mock_urlopen_response(mod, sim_body) as patched:
            result = mod.handler(
                make_appsync_event(
                    "unsubscribeFeature",
                    {"featureId": "docs-by-status"},
                    groups=["Admin"],
                ),
                None,
            )
            sent = json.loads(patched.call_args.args[0].data.decode("utf-8"))
            # The expire targets the resolved (random) id, not a synthesized one.
            assert sent["customerIdentifier"] == "cust-62c036d80d5c"
            assert sent["productCode"] == "prod123"
    finally:
        stubber.deactivate()
    assert result["state"] == "EXPIRED"
    assert result["customerIdentifier"] == "cust-62c036d80d5c"


def test_no_customer_identifier_and_no_subscription_raises(
    monkeypatch, load_lambda, installed_features_table
):
    """No concrete CustomerIdentifier and no entitlement under the buyer account
    (nothing to resolve) → UnsubscribeError rather than expiring a wrong id."""
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(
        monkeypatch,
        load_lambda,
        table_name=installed_features_table,
        default_customer="",
        buyer_account="111122223333",
    )
    stubber = Stubber(mod._client())
    stubber.add_response(
        "get_entitlements",
        {"Entitlements": []},
        {
            "ProductCode": "prod123",
            "Filter": {"CUSTOMER_AWS_ACCOUNT_ID": ["111122223333"]},
        },
    )
    stubber.activate()
    try:
        with pytest.raises(mod.UnsubscribeError, match="No CustomerIdentifier"):
            mod.handler(
                make_appsync_event(
                    "unsubscribeFeature",
                    {"featureId": "docs-by-status"},
                    groups=["Admin"],
                ),
                None,
            )
    finally:
        stubber.deactivate()


def test_falls_back_expires_at_when_simulator_omits_it(
    monkeypatch, load_lambda, installed_features_table
):
    _seed_row(installed_features_table, "docs-by-status", product_code="prod123")
    mod = _preload(monkeypatch, load_lambda, table_name=installed_features_table)
    with _mock_urlopen_response(mod, {"state": "EXPIRED"}):
        result = mod.handler(
            make_appsync_event(
                "unsubscribeFeature",
                {"featureId": "docs-by-status"},
                groups=["Admin"],
            ),
            None,
        )
    assert result["expiresAt"].endswith("Z")
