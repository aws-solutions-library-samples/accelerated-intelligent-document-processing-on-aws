# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`deploy --terraform` keeps the value a deployment has for every parameter
not passed, as a CloudFormation update does.

The record is the deployment's label stack, whose parameters Terraform writes at
every apply from the module's variables: every parameter's value in effect,
passed or defaulted, NoEcho ones left out. These tests cover the outcomes that
matter on an upgrade: a parameter never passed keeps its value across a release
that changed its default; a second operator deploying from a fresh directory
inherits the deployment's values; a parameter new in the release takes its
default; a value passed again overrides the record; and a deployment whose label
stack cannot be read falls back to the directory's own record with a warning
naming the defaults that changed. Offline: the binary, S3 and CloudFormation are
replaced.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from idp_sdk import IDPClient
from idp_sdk._core import terraform_deploy as tf

pytestmark = pytest.mark.unit

REGION = "us-west-2"
MODULE_URL = "https://s3.us-west-2.amazonaws.com/b/p/2/terraform/m.zip"
REPO_ROOT = Path(__file__).resolve().parents[4]


def _param(variable, ptype="String", required=False, sensitive=False):
    return {
        "variable": variable,
        "type": ptype,
        "required": required,
        "sensitive": sensitive,
    }


#: The module being deployed: EnableMCP's default changed from "true" to
#: "false" since the previous release, and NewSetting is new in it.
NEW_MODULE = {
    "formatVersion": 1,
    "version": "0.6.16",
    "parameters": {
        "AdminEmail": _param("admin_email", required=True),
        "EnableMCP": _param("enable_mcp"),
        "LogLevel": _param("log_level"),
        "MaxConcurrentWorkflows": _param("max_concurrent_workflows", "Number"),
        "SubnetIds": _param("subnet_ids", "CommaDelimitedList"),
        "ExternalId": _param("external_id", sensitive=True),
        "NewSetting": _param("new_setting"),
    },
}
NEW_DEFAULTS = {
    "enable_mcp": "false",
    "log_level": "WARN",
    "max_concurrent_workflows": 100,
    "subnet_ids": [],
    "external_id": "",
    "new_setting": "on",
}
OLD_DEFAULTS = {
    "enable_mcp": "true",
    "log_level": "WARN",
    "max_concurrent_workflows": 100,
    "subnet_ids": [],
    "external_id": "",
}

#: What the label stack of a deployment made from the previous release, with
#: only AdminEmail and LogLevel passed, records: every non-NoEcho parameter of
#: that release, the defaulted ones at the default then in effect.
LABEL_PARAMETERS = {
    "AdminEmail": "a@example.com",
    "EnableMCP": "true",
    "LogLevel": "DEBUG",
    "MaxConcurrentWorkflows": "50",
    "SubnetIds": "",
}


def _write(directory, **kw):
    args = dict(
        region=REGION,
        module_url=MODULE_URL,
        stack_name="tfidp",
        admin_email=None,
        parameters={},
        manifest=NEW_MODULE,
    )
    args.update(kw)
    return tf.write_root_config(directory, **args)


def _legacy_directory(directory: Path, passed: dict) -> None:
    """A directory an earlier idp-cli wrote: only the passed values recorded."""
    directory.mkdir(parents=True, exist_ok=True)
    tfvars = {"stack_name": "tfidp"}
    for name, value in passed.items():
        tfvars[NEW_MODULE["parameters"][name]["variable"]] = value
    (directory / "terraform.tfvars").write_text(tf.render_tfvars(tfvars))
    (directory / tf.PARAMETERS_FILE).write_text(json.dumps(passed))


def _report(values, new=NEW_DEFAULTS, old=OLD_DEFAULTS):
    return tf.parameter_report(values, NEW_MODULE, new, old)


def _text(report, level=None):
    return "\n".join(line for lvl, line in report if level is None or lvl == level)


class TestKeptValues:
    def test_an_unpassed_parameter_keeps_its_value_across_a_changed_default(
        self, tmp_path
    ):
        """EnableMCP was never passed, so it was "true" by the old default; the
        new module's default is "false", and the upgrade keeps "true"."""
        _legacy_directory(
            tmp_path, {"AdminEmail": "a@example.com", "LogLevel": "DEBUG"}
        )
        values = _write(tmp_path, recorded=LABEL_PARAMETERS)
        assert values["enable_mcp"] == "true"
        tfvars = tf.parse_tfvars((tmp_path / "terraform.tfvars").read_text())
        assert tfvars["enable_mcp"] == "true"
        assert values.kept_from["EnableMCP"] == tf.SOURCE_LABEL_STACK
        report = _text(_report(values))
        assert (
            "EnableMCP = true (from the label stack; this module's default is false)"
            in report
        )
        assert "MaxConcurrentWorkflows = 50" in report
        # LogLevel came from the label too, and SubnetIds equals the default.
        assert "SubnetIds" not in report
        assert "1 equal this module's default" in report

    def test_a_second_operator_inherits_the_deployments_values(self, tmp_path):
        """A fresh directory against the same state has no file of its own; the
        label stack is read by every deploy and holds the settings."""
        fresh = tmp_path / "operator-b"
        values = _write(fresh, admin_email="b@example.com", recorded=LABEL_PARAMETERS)
        assert values["log_level"] == "DEBUG"
        assert values["max_concurrent_workflows"] == 50
        assert values["enable_mcp"] == "true"
        assert values["admin_email"] == "b@example.com"
        assert "AdminEmail" not in values.kept
        assert values.upgrade and not values.local
        tfvars = tf.parse_tfvars((fresh / "terraform.tfvars").read_text())
        assert tfvars["log_level"] == "DEBUG"
        assert tfvars["max_concurrent_workflows"] == 50

    def test_the_second_operator_is_told_noecho_values_are_not_recorded(self, tmp_path):
        values = _write(tmp_path / "b", recorded=LABEL_PARAMETERS)
        warning = _text(_report(values), logging.WARNING)
        assert "ExternalId" in warning and "NoEcho" in warning

    def test_a_parameter_new_in_the_release_takes_its_default(self, tmp_path):
        values = _write(tmp_path, recorded=LABEL_PARAMETERS)
        assert "new_setting" not in values
        assert "NewSetting" not in values.kept
        assert (
            "New parameter(s) in this module, taking its default: NewSetting = on"
            in _text(_report(values))
        )

    def test_a_new_parameter_is_reported_from_the_old_defaults_without_a_label(
        self, tmp_path
    ):
        _legacy_directory(tmp_path, {"AdminEmail": "a@example.com"})
        values = _write(tmp_path)
        assert "NewSetting = on" in _text(_report(values))

    def test_a_value_passed_again_overrides_the_record(self, tmp_path):
        _legacy_directory(tmp_path, {"AdminEmail": "a@example.com"})
        values = _write(
            tmp_path,
            parameters={"EnableMCP": "false", "MaxConcurrentWorkflows": "7"},
            recorded=LABEL_PARAMETERS,
        )
        assert values["enable_mcp"] == "false"
        assert values["max_concurrent_workflows"] == 7
        assert "EnableMCP" not in values.kept
        assert "EnableMCP" not in _text(_report(values))

    def test_the_label_stack_wins_over_a_stale_local_record(self, tmp_path):
        """Another operator applied LogLevel=INFO since this directory last
        deployed; the label stack is what the deployment has now."""
        _legacy_directory(
            tmp_path, {"AdminEmail": "a@example.com", "LogLevel": "DEBUG"}
        )
        values = _write(tmp_path, recorded={**LABEL_PARAMETERS, "LogLevel": "INFO"})
        assert values["log_level"] == "INFO"

    def test_a_noecho_value_comes_from_the_local_record(self, tmp_path):
        _legacy_directory(
            tmp_path, {"AdminEmail": "a@example.com", "ExternalId": "s3cret"}
        )
        values = _write(tmp_path, recorded=LABEL_PARAMETERS)
        assert values["external_id"] == "s3cret"
        assert values.kept_from["ExternalId"] == tf.PARAMETERS_FILE
        report = _text(_report(values))
        assert "s3cret" not in report
        assert "ExternalId = (sensitive)" in report

    def test_a_label_parameter_the_module_dropped_is_ignored(self, tmp_path):
        values = _write(tmp_path, recorded={**LABEL_PARAMETERS, "Gone": "x"})
        assert "Gone" not in json.loads((tmp_path / tf.PARAMETERS_FILE).read_text())
        assert values["enable_mcp"] == "true"

    def test_a_first_deploy_reports_nothing(self, tmp_path):
        values = _write(tmp_path, admin_email="a@example.com")
        assert not values.upgrade
        assert _report(values, old=None) == []


class TestLegacyDeployment:
    def test_without_a_label_stack_the_changed_defaults_are_warned_about(
        self, tmp_path
    ):
        """A deployment made before idp-cli recorded effective values, whose
        label stack cannot be read: today's behaviour, with a warning naming
        every parameter whose default changed."""
        _legacy_directory(
            tmp_path, {"AdminEmail": "a@example.com", "LogLevel": "DEBUG"}
        )
        values = _write(tmp_path)
        assert "enable_mcp" not in values
        assert values["log_level"] == "DEBUG"
        warning = _text(_report(values), logging.WARNING)
        assert "EnableMCP: true -> false" in warning
        assert "--parameters" in warning
        # Unchanged defaults and passed values are not listed.
        assert "MaxConcurrentWorkflows" not in warning
        assert "LogLevel" not in warning

    def test_with_a_label_stack_the_previous_effective_values_are_kept(self, tmp_path):
        _legacy_directory(
            tmp_path, {"AdminEmail": "a@example.com", "LogLevel": "DEBUG"}
        )
        values = _write(tmp_path, recorded=LABEL_PARAMETERS)
        assert values["enable_mcp"] == "true"
        assert _text(_report(values), logging.WARNING) == ""

    def test_no_record_at_all_is_warned_about(self, tmp_path):
        """No label stack and an empty local record on an upgrade: the settings
        cannot be known."""
        (tmp_path / "terraform.tfvars").write_text(
            tf.render_tfvars({"stack_name": "tfidp", "admin_email": "a@example.com"})
        )
        values = _write(tmp_path)
        warning = _text(_report(values, old=None), logging.WARNING)
        assert "No record of this deployment's settings" in warning
        assert "EnableMCP" in warning

    def test_a_complete_local_record_needs_no_warning(self, tmp_path):
        """After this change the directory records every effective value, so a
        later upgrade without a label stack keeps them all."""
        first = _write(tmp_path, admin_email="a@example.com")
        tf.record_effective_parameters(tmp_path, NEW_MODULE, first, OLD_DEFAULTS)
        values = _write(tmp_path)
        assert values["enable_mcp"] == "true"
        assert _text(_report(values), logging.WARNING) == ""


class TestModuleDefaults:
    def test_defaults_are_read_from_the_translators_variables_tf(self):
        """The fixture is the real translator's output, not a hand-written copy."""
        pytest.importorskip("yaml")
        template = {
            "Parameters": {
                "EnableMCP": {
                    "Type": "String",
                    "Default": "false",
                    "AllowedValues": ["true", "false"],
                },
                "Max": {"Type": "Number", "Default": 100},
                "Ratio": {"Type": "Number", "Default": "2.5"},
                "Subnets": {"Type": "CommaDelimitedList", "Default": "a,b"},
                "Long": {
                    "Type": "CommaDelimitedList",
                    "Default": ",".join(f"item-number-{i:02d}" for i in range(12)),
                },
                "Empty": {"Type": "CommaDelimitedList", "Default": ""},
                "Tricky": {"Type": "String", "Default": 'q"${x}%{y}\\'},
                "Required": {"Type": "String"},
                "Secret": {"Type": "String", "Default": "", "NoEcho": True},
            },
            "Resources": {"Q": {"Type": "AWS::SQS::Queue", "Properties": {}}},
        }
        translator = REPO_ROOT / "terraform" / "translator"
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from idp_tf.awscc_schema import AwsccSchema\n"
            "from idp_tf.translate import Translator\n"
            "t = json.loads(sys.stdin.read())\n"
            "files = Translator(t, AwsccSchema(Path('schemas/awscc.json'))).render()\n"
            "print(json.dumps(files['variables.tf']))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            input=json.dumps(template),
            cwd=translator,
            env={"PYTHONPATH": str(translator), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=True,
        )
        variables_tf = json.loads(proc.stdout)
        defaults = tf.module_variable_defaults(variables_tf)
        assert defaults == {
            "enable_mcp": "false",
            "max": 100,
            "ratio": 2.5,
            "subnets": ["a", "b"],
            "long": [f"item-number-{i:02d}" for i in range(12)],
            "empty": [],
            "tricky": 'q"${x}%{y}\\',
            "secret": "",
            # The module's tags input, which every translated module declares.
            "tags": {},
        }

    def test_an_unreadable_default_is_left_out(self):
        text = 'variable "a" {\n  type = string\n  default = tomap({})\n}\n'
        assert tf.module_variable_defaults(text) == {}

    def test_no_installed_module_is_none(self, tmp_path):
        assert tf.read_module_defaults_from_dir(tmp_path) is None


# --------------------------------------------------------------------------------------
# Through `client.stack.deploy_terraform`, with the binary and AWS replaced
# --------------------------------------------------------------------------------------


def _variables_tf(defaults):
    blocks = []
    for name, value in defaults.items():
        blocks.append(
            f'variable "{name}" {{\n  type = string\n'
            f"  default = {tf._hcl_value(value)}\n}}\n"
        )
    return "\n".join(blocks)


def _install(directory: Path, defaults) -> None:
    """What `terraform init` leaves: the idp module under .terraform/modules."""
    mod = directory / ".terraform" / "modules" / "idp"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "variables.tf").write_text(_variables_tf(defaults))
    (directory / ".terraform" / "modules" / "modules.json").write_text(
        json.dumps({"Modules": [{"Key": "idp", "Dir": ".terraform/modules/idp"}]})
    )


def _zip(manifest) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(tf.MANIFEST_NAME, json.dumps(manifest))
    return buf.getvalue()


def _deploy(monkeypatch, directory, label_parameters, parameters=None, email=None):
    from idp_sdk.operations.stack import StackOperation

    label = {
        "StackId": "arn:aws:cloudformation:us-west-2:111122223333:stack/tfidp/x",
        "Description": "IDP (Terraform) read-only",
        "Outputs": [
            {"OutputKey": "TerraformResourceDirectory", "OutputValue": "s3://b/k"}
        ],
    }
    if label_parameters is not None:
        label["Parameters"] = [
            {"ParameterKey": k, "ParameterValue": v}
            for k, v in label_parameters.items()
        ]
    monkeypatch.setattr(
        StackOperation, "_describe_stack", staticmethod(lambda d, n: label)
    )
    monkeypatch.setattr(tf, "fetch_url", lambda url, s3_factory=None: _zip(NEW_MODULE))
    monkeypatch.setattr(tf.shutil, "which", lambda b: "/usr/bin/terraform")

    def run(cmd, **kw):
        if cmd[1] == "init":
            _install(directory, NEW_DEFAULTS)
        out = json.dumps({"resource_changes": []}) if cmd[1] == "show" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(tf.subprocess, "run", run)
    return IDPClient(stack_name="tfidp", region=REGION).stack.deploy_terraform(
        admin_email=email,
        parameters=parameters,
        module_url=MODULE_URL,
        terraform_dir=str(directory),
    )


class TestDeployOperation:
    def test_an_upgrade_keeps_and_lists_the_values_and_records_them_all(
        self, monkeypatch, tmp_path, caplog
    ):
        _legacy_directory(
            tmp_path, {"AdminEmail": "a@example.com", "LogLevel": "DEBUG"}
        )
        _install(tmp_path, OLD_DEFAULTS)
        with caplog.at_level(logging.INFO):
            _deploy(monkeypatch, tmp_path, LABEL_PARAMETERS)
        tfvars = tf.parse_tfvars((tmp_path / "terraform.tfvars").read_text())
        assert tfvars["enable_mcp"] == "true"
        assert "EnableMCP = true (from the label stack" in caplog.text
        assert "NewSetting = on" in caplog.text
        recorded = json.loads((tmp_path / tf.PARAMETERS_FILE).read_text())
        # Every parameter's value in effect, the new one's default included.
        assert recorded == {
            "AdminEmail": "a@example.com",
            "EnableMCP": "true",
            "LogLevel": "DEBUG",
            "MaxConcurrentWorkflows": 50,
            "SubnetIds": [],
            "ExternalId": "",
            "NewSetting": "on",
        }
        assert (tmp_path / tf.PARAMETERS_FILE).stat().st_mode & 0o077 == 0

    def test_a_second_operator_from_a_fresh_directory_inherits(
        self, monkeypatch, tmp_path
    ):
        fresh = tmp_path / "b"
        _deploy(monkeypatch, fresh, LABEL_PARAMETERS, email="b@example.com")
        tfvars = tf.parse_tfvars((fresh / "terraform.tfvars").read_text())
        assert tfvars["log_level"] == "DEBUG"
        assert tfvars["max_concurrent_workflows"] == 50
        assert tfvars["enable_mcp"] == "true"

    def test_a_legacy_deployment_without_a_label_stack_warns(
        self, monkeypatch, tmp_path, caplog
    ):
        _legacy_directory(tmp_path, {"AdminEmail": "a@example.com"})
        _install(tmp_path, OLD_DEFAULTS)
        with caplog.at_level(logging.INFO):
            _deploy(monkeypatch, tmp_path, None)
        assert "enable_mcp" not in (tmp_path / "terraform.tfvars").read_text()
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert "  EnableMCP: true -> false" in warnings
