# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`idp-cli terraform migrate-from-genai-idp-terraform <phase>`: the CLI's half.

Each phase is a thin call into
`client.terraform.migrate_from_genai_idp_terraform`; the logic is tested in the
SDK suite (`lib/idp_sdk/tests/unit/test_terraform_migrate.py`). These tests pin
what the CLI decides: the command names, the default module pointer, that
nothing is applied without --apply, the exit status, and that --json writes only
the payload to stdout.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import idp_cli.cli as cli_module
import idp_cli.terraform as tf_cli
from idp_sdk.models import (
    TerraformMigrationPlanResult,
    TerraformMigrationResource,
    TerraformMigrationStepResult,
)

pytestmark = pytest.mark.unit

GROUP = ["terraform", "migrate-from-genai-idp-terraform"]
COMMON = ["--stack-name", "newidp", "--region", "us-west-2"]


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def migration():
    op = MagicMock()
    client = MagicMock()
    client.terraform.migrate_from_genai_idp_terraform = op
    with patch.object(tf_cli, "_client", return_value=client) as factory:
        op.factory = factory
        yield op


def step(phase, ok=True, applied=False, changes=("x",), findings=()):
    return TerraformMigrationStepResult(
        phase=phase,
        ok=ok,
        applied=applied,
        changes=list(changes),
        findings=list(findings),
    )


def test_the_group_and_its_phases_exist(runner):
    result = runner.invoke(cli_module.cli, [*GROUP, "--help"])
    assert result.exit_code == 0
    for phase in (
        "plan",
        "check-retention",
        "freeze",
        "fix-config",
        "adopt-user-pool",
        "apply",
        "detach",
        "check-destroy",
    ):
        assert phase in result.output


def test_plan_uses_the_regions_latest_module(runner, migration, tmp_path):
    state = tmp_path / "terraform.tfstate"
    state.write_text("{}")
    migration.plan.return_value = TerraformMigrationPlanResult(
        terraform_dir="d",
        resources=[
            TerraformMigrationResource(
                old_address="aws_s3_bucket.input_bucket",
                old_type="aws_s3_bucket",
                kind="input_bucket",
                verdict="adopt",
                evidence="measured",
                note="",
                new_address="module.idp.aws_s3_bucket.input_bucket",
                import_id="b",
            )
        ],
        detach_commands=["terraform state rm 'aws_s3_bucket.input_bucket'"],
    )
    result = runner.invoke(
        cli_module.cli,
        [
            *GROUP,
            "plan",
            *COMMON,
            "--old-state",
            str(state),
            "--admin-email",
            "Admin@example.com",
            "--parameters",
            "LogRetentionDays=90",
        ],
    )
    assert result.exit_code == 0, result.output
    args, kwargs = migration.plan.call_args
    assert args == (str(state), "Admin@example.com", {"LogRetentionDays": "90"})
    assert kwargs["module_url"] is None
    assert kwargs["pointer_url"] == (
        "https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/"
        "artifacts/genai-idp/terraform/idp-terraform-latest.json"
    )
    migration.factory.assert_called_once_with("newidp", "us-west-2")


@pytest.mark.parametrize(
    "phase,method,extra",
    [
        ("freeze", "freeze", []),
        ("fix-config", "fix_config", []),
        ("adopt-user-pool", "adopt_user_pool", []),
        ("apply", "apply", []),
    ],
)
def test_nothing_is_applied_without_apply(runner, migration, phase, method, extra):
    getattr(migration, method).return_value = step(phase)
    result = runner.invoke(cli_module.cli, [*GROUP, phase, *COMMON, *extra])
    assert result.exit_code == 0, result.output
    assert getattr(migration, method).call_args.kwargs["apply"] is False
    assert "Dry run: pass --apply" in result.output

    result = runner.invoke(cli_module.cli, [*GROUP, phase, *COMMON, *extra, "--apply"])
    assert getattr(migration, method).call_args.kwargs["apply"] is True


def test_detach_passes_the_old_directory(runner, migration, tmp_path):
    migration.detach.return_value = step("detach")
    result = runner.invoke(
        cli_module.cli,
        [*GROUP, "detach", *COMMON, "--old-terraform-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    args, kwargs = migration.detach.call_args
    assert args[0] == str(tmp_path) and kwargs == {"apply": False}


def test_a_finding_exits_non_zero_and_is_printed(runner, migration):
    migration.freeze.return_value = step(
        "freeze", ok=False, findings=["2 document(s) are still being processed"]
    )
    result = runner.invoke(cli_module.cli, [*GROUP, "freeze", *COMMON, "--apply"])
    assert result.exit_code == 1
    assert "2 document(s) are still being processed" in result.output


def test_json_writes_only_the_payload_to_stdout(migration):
    migration.check_retention.return_value = step("check-retention", changes=())
    result = CliRunner().invoke(
        cli_module.cli, [*GROUP, "check-retention", *COMMON, "--json"]
    )
    assert result.exit_code == 0
    # Click 8.2+ keeps stdout and stderr apart: the payload alone parses.
    assert json.loads(result.stdout)["phase"] == "check-retention"


def test_accept_expiry_is_passed_through(runner, migration):
    migration.apply.return_value = step("apply")
    runner.invoke(
        cli_module.cli,
        [
            *GROUP,
            "apply",
            *COMMON,
            "--accept-expiry",
            "logging_bucket",
            "--accept-expiry",
            "working_bucket",
        ],
    )
    assert migration.apply.call_args.kwargs["accept_expiry"] == (
        "logging_bucket",
        "working_bucket",
    )


def test_check_destroy_reads_the_plan_file(runner, migration, tmp_path):
    plan = tmp_path / "destroy.json"
    plan.write_text("{}")
    migration.check_destroy.return_value = step("check-destroy", changes=())
    result = runner.invoke(
        cli_module.cli, [*GROUP, "check-destroy", *COMMON, "--destroy-plan", str(plan)]
    )
    assert result.exit_code == 0, result.output
    assert migration.check_destroy.call_args.args[0] == str(plan)
