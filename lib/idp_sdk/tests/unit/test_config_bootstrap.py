# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``client.config.bootstrap()``: Quick Start's prompt path, through the SDK."""

from unittest.mock import patch

import pytest

from idp_sdk import ConfigBootstrapResult, IDPClient


def _result(**overrides):
    from idp_common.synthesis.bootstrap import BootstrapResult

    fields = dict(
        success=True,
        config_version="invoices-v1",
        resolution_tier="catalog_adapt",
        catalog_match="invoice",
        schema={"$id": "Invoice"},
        test_set_id="invoices-v1",
        docs_generated=3,
        generator_available=True,
        messages=["Bootstrap complete"],
    )
    fields.update(overrides)
    return BootstrapResult(**fields)


def _run(monkeypatch, result, **kwargs):
    monkeypatch.setenv("TEST_SET_BUCKET", "test-sets")
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    client = IDPClient(stack_name="IDP", region="us-west-2")
    with (
        patch.object(client.config, "_configure_config_env", return_value="T") as env,
        patch(
            "idp_common.synthesis.bootstrap.run_bootstrap", return_value=result
        ) as run,
        patch("idp_common.config.configuration_manager.ConfigurationManager") as mgr,
    ):
        out = client.config.bootstrap("an invoice", **kwargs)
    return out, run, mgr, env


@pytest.mark.unit
def test_bootstrap_returns_every_field_of_the_result(monkeypatch):
    out, run, mgr, env = _run(monkeypatch, _result(), field_hints=["total"])
    assert isinstance(out, ConfigBootstrapResult)
    assert out.model_dump() == {
        "success": True,
        "config_profile": "invoices-v1",
        "class_schema": {"$id": "Invoice"},
        "resolution_tier": "catalog_adapt",
        "catalog_match": "invoice",
        "test_set_id": "invoices-v1",
        "docs_generated": 3,
        "generator_available": True,
        "messages": ["Bootstrap complete"],
        "error": None,
    }
    env.assert_called_once_with("IDP")
    mgr.assert_called_once_with(region="us-west-2")
    request = run.call_args.args[0]
    assert request.prompt == "an invoice" and request.field_hints == ["total"]
    assert run.call_args.kwargs["test_set_bucket"] == "test-sets"


@pytest.mark.unit
def test_bootstrap_maps_its_options_onto_the_request(monkeypatch):
    seen = []
    _out, run, _mgr, _env = _run(
        monkeypatch,
        _result(),
        class_name="Invoice",
        source_profile="src",
        target_profile="dst",
        doc_count=5,
        quality_threshold=9,
        augment=True,
        model_id="m",
        generate_test_set=False,
        status_callback=lambda pct, msg: seen.append(msg),
    )
    request = run.call_args.args[0]
    assert (request.class_name, request.config_version, request.target_version) == (
        "Invoice",
        "src",
        "dst",
    )
    assert (request.doc_count, request.quality_threshold, request.augment) == (
        5,
        9,
        True,
    )
    assert request.model_id == "m" and request.field_hints == []
    assert run.call_args.kwargs["test_set_bucket"] is None
    run.call_args.kwargs["status_cb"](50.0, "half")
    assert seen == ["half"]


@pytest.mark.unit
def test_a_failed_bootstrap_carries_its_error(monkeypatch):
    out, *_ = _run(
        monkeypatch, _result(success=False, config_version=None, error="no schema")
    )
    assert out.success is False and out.error == "no schema"
    assert out.config_profile is None
