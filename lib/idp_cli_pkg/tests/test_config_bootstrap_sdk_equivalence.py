# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``client.config.bootstrap()`` does what ``idp-cli bootstrap --stack-name`` does.

The SDK operation exists so the fit check can reach Quick Start's CLI path through
the SDK. Both are driven here with the same options and the same environment, and
the call each makes into ``idp_common.synthesis.bootstrap.run_bootstrap`` — the
request, the test-set bucket, and the region of the configuration manager — has to
be the same call.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli import cli as cli_module

ARGS = dict(
    prompt="an invoice",
    class_name="Invoice",
    field_hints=["total", "due_date"],
    source_profile="catalog-src",
    target_profile="bootstrap-invoice",
    doc_count=4,
    quality_threshold=8,
    augment=True,
    model_id="us.amazon.nova-lite-v1:0",
)


def _bootstrap_result():
    from idp_common.synthesis.bootstrap import BootstrapResult

    return BootstrapResult(
        success=True, config_version="bootstrap-invoice", resolution_tier="pure_llm"
    )


def _cli_call(monkeypatch):
    client = MagicMock()
    client.config._configure_config_env.return_value = "IDP-Config"
    with (
        patch("idp_sdk.IDPClient", return_value=client),
        patch(
            "idp_common.synthesis.bootstrap.run_bootstrap",
            return_value=_bootstrap_result(),
        ) as run,
        patch(
            "idp_common.synthesis.engine.generator_available", return_value=(True, "")
        ),
        patch("idp_common.config.configuration_manager.ConfigurationManager") as mgr,
    ):
        argv = ["bootstrap", "-p", ARGS["prompt"], "--stack-name", "IDP"]
        argv += ["--class-name", ARGS["class_name"]]
        for hint in ARGS["field_hints"]:
            argv += ["--field-hint", hint]
        argv += ["--config-profile", ARGS["source_profile"]]
        argv += ["--target-profile", ARGS["target_profile"]]
        argv += ["--count", str(ARGS["doc_count"])]
        argv += ["--threshold", str(ARGS["quality_threshold"]), "--augment"]
        argv += ["--model-id", ARGS["model_id"], "--region", "eu-central-1"]
        result = CliRunner().invoke(cli_module.cli, argv)
    assert result.exit_code == 0, result.output
    return run.call_args, mgr.call_args


def _sdk_call():
    from idp_sdk import IDPClient

    client = IDPClient(stack_name="IDP", region="eu-central-1")
    with (
        patch.object(client.config, "_configure_config_env", return_value="IDP-Config"),
        patch(
            "idp_common.synthesis.bootstrap.run_bootstrap",
            return_value=_bootstrap_result(),
        ) as run,
        patch("idp_common.config.configuration_manager.ConfigurationManager") as mgr,
    ):
        out = client.config.bootstrap(**ARGS)
    assert out.success and out.config_profile == "bootstrap-invoice"
    return run.call_args, mgr.call_args


@pytest.mark.unit
def test_the_sdk_operation_makes_the_cli_commands_call(monkeypatch):
    monkeypatch.setenv("TEST_SET_BUCKET", "test-sets")
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    cli_run, cli_mgr = _cli_call(monkeypatch)
    import os

    assert os.environ["CONFIGURATION_TABLE_NAME"] == "IDP-Config"
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    sdk_run, sdk_mgr = _sdk_call()
    assert os.environ["CONFIGURATION_TABLE_NAME"] == "IDP-Config"

    assert sdk_run.args == cli_run.args  # the BootstrapRequest, field for field
    assert sdk_run.kwargs["test_set_bucket"] == cli_run.kwargs["test_set_bucket"]
    assert sdk_run.kwargs["test_set_bucket"] == "test-sets"
    assert sdk_mgr == cli_mgr  # ConfigurationManager(region="eu-central-1")


@pytest.mark.unit
def test_the_sdk_operation_can_skip_the_test_set(monkeypatch):
    from idp_sdk import IDPClient

    monkeypatch.setenv("TEST_SET_BUCKET", "test-sets")
    client = IDPClient(stack_name="IDP", region="us-west-2")
    with (
        patch.object(client.config, "_configure_config_env", return_value="T"),
        patch(
            "idp_common.synthesis.bootstrap.run_bootstrap",
            return_value=_bootstrap_result(),
        ) as run,
        patch("idp_common.config.configuration_manager.ConfigurationManager"),
    ):
        client.config.bootstrap("a form", generate_test_set=False)
    assert run.call_args.kwargs["test_set_bucket"] is None
