# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`idp-cli publish|deploy|delete --terraform`: the CLI's half.

The commands are thin calls into `client.stack.deploy_terraform`,
`client.stack.delete_terraform` and `client.publish.build(terraform=True)`; the
logic is tested in the SDK suite (`lib/idp_sdk/tests/unit/test_terraform_deploy.py`).
These tests pin what the CLI itself decides: the default module pointer for the
region, the options it refuses alongside `--terraform`, what reaches the SDK, and
that the CloudFormation path is not taken.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

import idp_cli.cli as cli_module
from idp_sdk.models import StackDeletionResult, StackDeploymentResult

pytestmark = pytest.mark.unit


@pytest.fixture
def runner():
    return CliRunner()


def _client():
    client = MagicMock()
    client.stack.deploy_terraform.return_value = StackDeploymentResult(
        success=True,
        operation="CREATE",
        status="APPLY_COMPLETE",
        stack_name="tfidp",
        outputs={"ApplicationWebURL": "https://app.example.com"},
    )
    client.stack.delete_terraform.return_value = StackDeletionResult(
        success=True,
        status="DESTROY_COMPLETE",
        stack_name="tfidp",
        retained_resources={
            "dynamodb_tables": [],
            "log_groups": [],
            "s3_buckets": [
                {
                    "logical_id": "module.idp.aws_s3_bucket.logging_bucket",
                    "physical_id": "tfidp-logging",
                }
            ],
            "other": [],
        },
    )
    return client


class TestDeployTerraform:
    def test_default_module_is_the_regions_latest_pointer(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client) as factory:
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--admin-email",
                    "a@example.com",
                    "--region",
                    "us-west-2",
                    "--parameters",
                    "MaxConcurrentWorkflows=5,LogLevel=INFO",
                    "--wait",
                ],
            )
        assert result.exit_code == 0, result.output
        factory.assert_called_once_with(stack_name="tfidp", region="us-west-2")
        kwargs = client.stack.deploy_terraform.call_args.kwargs
        assert kwargs == {
            "stack_name": "tfidp",
            "admin_email": "a@example.com",
            "parameters": {"MaxConcurrentWorkflows": "5", "LogLevel": "INFO"},
            "module_url": None,
            "pointer_url": (
                "https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/"
                "artifacts/genai-idp/terraform/idp-terraform-latest.json"
            ),
            "terraform_dir": None,
            "terraform_bin": "terraform",
            "assume_role_arn": None,
            "allow_replace_retained": False,
        }
        client.stack.deploy.assert_not_called()
        # The deploy prints the application URL; compare it parsed, by host.
        printed = [
            urlparse(t.rstrip(".,"))
            for t in result.output.split()
            if t.startswith("https://")
        ]
        assert ("https", "app.example.com") in [(u.scheme, u.hostname) for u in printed]

    def test_explicit_module_dir_and_binary(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "ap-southeast-9",
                    "--terraform-module-url",
                    "https://s3.ap-southeast-9.amazonaws.com/b/m.zip",
                    "--terraform-dir",
                    "here",
                    "--terraform-bin",
                    "tofu",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = client.stack.deploy_terraform.call_args.kwargs
        assert kwargs["module_url"] == "https://s3.ap-southeast-9.amazonaws.com/b/m.zip"
        assert kwargs["pointer_url"] is None
        assert kwargs["terraform_dir"] == "here"
        assert kwargs["terraform_bin"] == "tofu"

    def test_the_retained_resource_guard_is_overridden_only_by_its_flag(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                    "--terraform-allow-replace-retained",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = client.stack.deploy_terraform.call_args.kwargs
        assert kwargs["allow_replace_retained"] is True

    def test_the_stateful_guard_flag_has_a_new_name_and_keeps_the_old(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                    "--terraform-allow-replace-stateful",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = client.stack.deploy_terraform.call_args.kwargs
        assert kwargs["allow_replace_retained"] is True

    def test_a_region_with_no_published_module_needs_a_url(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "ap-southeast-9",
                ],
            )
        assert result.exit_code == 1
        assert "--terraform-module-url" in result.output
        client.stack.deploy_terraform.assert_not_called()

    @pytest.mark.parametrize(
        "extra,flag",
        [
            (["--custom-config", "./c.yaml"], "--custom-config"),
            (["--template-url", "https://x/t.yaml"], "--template-url"),
            (["--tags", "a=b"], "--tags"),
            (["--max-concurrent", "5"], "--max-concurrent"),
        ],
    )
    def test_cloudformation_only_options_are_refused(self, runner, extra, flag):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                ]
                + extra,
            )
        assert result.exit_code == 1
        assert flag in result.output
        client.stack.deploy_terraform.assert_not_called()

    def test_an_sdk_error_exits_one(self, runner):
        from idp_sdk.exceptions import IDPConfigurationError

        client = _client()
        client.stack.deploy_terraform.side_effect = IDPConfigurationError(
            "'terraform' was not found on PATH"
        )
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                ],
            )
        assert result.exit_code == 1
        assert "was not found on PATH" in result.output


class TestDeployTerraformVariants:
    POINTER = (
        "https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/"
        "artifacts/genai-idp/terraform/idp-terraform-{}-latest.json"
    )

    @pytest.mark.parametrize("variant", ["headless", "govcloud"])
    def test_a_variant_selects_its_own_pointer(self, runner, variant):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    f"--{variant}",
                    "--stack-name",
                    "tfidp",
                    "--admin-email",
                    "a@example.com",
                    "--region",
                    "us-west-2",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = client.stack.deploy_terraform.call_args.kwargs
        assert kwargs["pointer_url"] == self.POINTER.format(variant)
        assert kwargs["module_url"] is None
        # Headless has no Cognito, so no AdminEmail; GovCloud keeps the UI.
        expected_email = None if variant == "headless" else "a@example.com"
        assert kwargs["admin_email"] == expected_email
        client.stack.deploy.assert_not_called()

    def test_headless_and_govcloud_together_are_refused(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--headless",
                    "--govcloud",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                ],
            )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output
        client.stack.deploy_terraform.assert_not_called()

    def test_a_variant_with_an_explicit_module_url_is_refused(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--govcloud",
                    "--terraform-module-url",
                    "https://s3.us-west-2.amazonaws.com/b/m.zip",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                ],
            )
        assert result.exit_code == 1
        assert "--terraform-module-url" in result.output
        client.stack.deploy_terraform.assert_not_called()


class TestDeleteTerraform:
    def test_force_destroys_and_reports_retained(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "delete",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                    "--force",
                    "--terraform-dir",
                    "here",
                ],
            )
        assert result.exit_code == 0, result.output
        client.stack.delete_terraform.assert_called_once_with(
            stack_name="tfidp",
            terraform_dir="here",
            terraform_bin="terraform",
            assume_role_arn=None,
        )
        client.stack.delete.assert_not_called()
        assert "tfidp-logging" in result.output

    def test_declining_the_prompt_deletes_nothing(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "delete",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                ],
                input="n\n",
            )
        assert result.exit_code == 0
        assert "cancelled" in result.output
        client.stack.delete_terraform.assert_not_called()

    def test_force_delete_all_is_refused(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "delete",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                    "--force",
                    "--force-delete-all",
                ],
            )
        assert result.exit_code == 1
        client.stack.delete_terraform.assert_not_called()


class TestPublishTerraform:
    def test_module_urls_are_printed(self, runner, tmp_path):
        from idp_sdk.models.publish import PublishResult

        client = MagicMock()
        client.publish.build.return_value = PublishResult(
            success=True,
            template_url="https://s3.us-west-2.amazonaws.com/b/p/idp-main.yaml",
            terraform_module_url="https://s3.us-west-2.amazonaws.com/b/p/1/terraform/idp-terraform-1.zip",
            terraform_pointer_url="https://s3.us-west-2.amazonaws.com/b/p/terraform/idp-terraform-latest.json",
        )
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "publish",
                    "--source-dir",
                    str(tmp_path),
                    "--region",
                    "us-west-2",
                    "--terraform",
                ],
            )
        assert result.exit_code == 0, result.output
        assert client.publish.build.call_args.kwargs["terraform"] is True
        printed = client.publish.print_deployment_urls.call_args.kwargs
        assert printed["terraform_variant_modules"] == {}
        assert printed["terraform_module_url"].endswith("idp-terraform-1.zip")
        assert printed["terraform_pointer_url"].endswith("idp-terraform-latest.json")


class TestTheGuardIsUntouched:
    """Plain deploy and delete still refuse a Terraform-labelled stack."""

    @pytest.mark.parametrize("action", ["deploy", "delete"])
    def test_plain_commands_refuse_a_label_stack(self, action):
        from idp_sdk._core.stack import StackDeployer

        deployer = StackDeployer.__new__(StackDeployer)
        deployer.cfn = MagicMock()
        deployer.cfn.describe_stacks.return_value = {
            "Stacks": [{"Description": "GenAI IDP (Terraform) read-only label"}]
        }
        with pytest.raises(ValueError, match="is a Terraform deployment"):
            deployer._stack_exists("tfidp", action=action)


ROLE = "arn:aws:iam::123456789012:role/idp-tf-deployer"


class TestAssumeRole:
    def test_deploy_passes_the_role_to_the_sdk(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "deploy",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--admin-email",
                    "a@example.com",
                    "--region",
                    "us-west-2",
                    "--terraform-assume-role-arn",
                    ROLE,
                ],
            )
        assert result.exit_code == 0, result.output
        assert client.stack.deploy_terraform.call_args.kwargs["assume_role_arn"] == ROLE

    def test_delete_passes_the_role_to_the_sdk(self, runner):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    "delete",
                    "--terraform",
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                    "--force",
                    "--terraform-assume-role-arn",
                    ROLE,
                ],
            )
        assert result.exit_code == 0, result.output
        assert client.stack.delete_terraform.call_args.kwargs["assume_role_arn"] == ROLE

    @pytest.mark.parametrize("command", ["deploy", "delete"])
    def test_without_terraform_the_role_is_refused(self, runner, command):
        client = _client()
        with patch.object(cli_module, "IDPClient", return_value=client):
            result = runner.invoke(
                cli_module.cli,
                [
                    command,
                    "--stack-name",
                    "tfidp",
                    "--region",
                    "us-west-2",
                    "--force" if command == "delete" else "--wait",
                    "--terraform-assume-role-arn",
                    ROLE,
                ],
            )
        assert result.exit_code == 1
        assert "--terraform-assume-role-arn" in result.output
        client.stack.deploy.assert_not_called()
        client.stack.delete.assert_not_called()
