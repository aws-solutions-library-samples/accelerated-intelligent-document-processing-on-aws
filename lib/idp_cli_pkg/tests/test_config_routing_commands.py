# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``config-routing-export`` and ``config-routing-import``: routing tables as DMN.

Both commands work on local files and call no AWS service: the conversion is the
SDK's (``client.config.routing_to_dmn`` / ``routing_from_dmn``), which is
``idp_common``'s. The real conversion runs here, so a round trip through the two
commands is checked end to end; what the tests replace is nothing but the
filesystem location (``tmp_path``).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
import yaml
from click.testing import CliRunner

from idp_cli.cli import cli
from idp_common.config.routing_models import RoutingConfig

pytestmark = pytest.mark.unit

ROUTING = {
    "tables": [
        {
            "id": "review-routing",
            "stage": "after_policy_rule_validation",
            "hit_policy": "FIRST",
            "engine": "builtin",
            "rules": [
                {
                    "id": "compliance-policy-failure",
                    "label": "Failed policy rules",
                    "enabled": True,
                    "conditions": [
                        {
                            "input": "failed_policy_rule_count",
                            "path": "",
                            "aggregate": "any",
                            "test": "> 0",
                        }
                    ],
                    "actions": [{"type": "review_queue", "queue": "compliance"}],
                    "reason": "{failed_policy_rule_count} policy rules failed.",
                }
            ],
            "default_outcome": {"actions": [], "reason": ""},
        }
    ],
    "max_consecutive_placements": 3,
}


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"routing": ROUTING, "notes": "a preset"}))
    return path


def _run(*arguments):
    return CliRunner().invoke(cli, list(arguments))


def _tables(section: dict):
    return RoutingConfig.model_validate(section).tables


class TestExport:
    def test_the_routing_table_is_written_as_dmn(self, config_file, tmp_path):
        output = tmp_path / "routing.dmn"

        result = _run(
            "config-routing-export",
            "--config-file",
            str(config_file),
            "--output",
            str(output),
        )

        assert result.exit_code == 0, result.output
        xml = output.read_text()
        assert "<decisionTable" in xml and "failed_policy_rule_count" in xml
        assert "Wrote the routing table as DMN" in result.output

    def test_a_dash_writes_only_the_xml_to_stdout(
        self, config_file, tmp_path, monkeypatch
    ):
        # In a directory of its own: a dash read as a file name would leave a
        # file named "-" where the command ran.
        monkeypatch.chdir(tmp_path)

        result = _run("config-routing-export", "-f", str(config_file), "-o", "-")

        assert result.exit_code == 0, result.output
        assert result.stdout.lstrip().startswith("<?xml")
        assert "Wrote" not in result.stdout
        assert result.stderr == ""
        assert not (tmp_path / "-").exists()

    def test_the_dmn_version_is_the_callers(self, config_file, tmp_path):
        default, older = tmp_path / "a.dmn", tmp_path / "b.dmn"

        _run("config-routing-export", "-f", str(config_file), "-o", str(default))
        result = _run(
            "config-routing-export",
            "-f",
            str(config_file),
            "-o",
            str(older),
            "--dmn-version",
            "1.3",
        )

        assert result.exit_code == 0, result.output
        assert default.read_text() != older.read_text()

    def test_a_version_that_is_not_offered_is_a_usage_error(
        self, config_file, tmp_path
    ):
        result = _run(
            "config-routing-export",
            "-f",
            str(config_file),
            "-o",
            str(tmp_path / "x.dmn"),
            "--dmn-version",
            "2.0",
        )

        assert result.exit_code == 2
        assert not (tmp_path / "x.dmn").exists()

    def test_a_configuration_with_no_routing_section_exits_one(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("classes: []\n")
        output = tmp_path / "routing.dmn"

        result = _run("config-routing-export", "-f", str(path), "-o", str(output))

        assert result.exit_code == 1
        assert "no routing section" in result.stderr
        assert not output.exists()

    def test_a_file_that_does_not_exist_is_a_usage_error(self, tmp_path):
        result = _run(
            "config-routing-export",
            "-f",
            str(tmp_path / "missing.yaml"),
            "-o",
            str(tmp_path / "x.dmn"),
        )

        assert result.exit_code == 2


class TestImport:
    @pytest.fixture
    def dmn_file(self, config_file, tmp_path):
        path = tmp_path / "routing.dmn"
        _run("config-routing-export", "-f", str(config_file), "-o", str(path))
        return path

    def test_the_routing_section_is_written_as_a_configuration_file(
        self, dmn_file, tmp_path
    ):
        output = tmp_path / "routing.yaml"

        result = _run(
            "config-routing-import",
            "--dmn-file",
            str(dmn_file),
            "--output",
            str(output),
        )

        assert result.exit_code == 0, result.output
        written = yaml.safe_load(output.read_text())
        assert set(written) == {"routing"}
        assert _tables(written["routing"]) == _tables(ROUTING)
        assert "1 rule(s)" in result.output

    def test_a_json_name_writes_json(self, dmn_file, tmp_path):
        output = tmp_path / "routing.json"

        result = _run(
            "config-routing-import", "--dmn-file", str(dmn_file), "-o", str(output)
        )

        assert result.exit_code == 0, result.output
        assert _tables(json.loads(output.read_text())["routing"]) == _tables(ROUTING)

    def test_a_dash_writes_only_the_yaml_to_stdout(
        self, dmn_file, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)

        result = _run("config-routing-import", "--dmn-file", str(dmn_file), "-o", "-")

        assert result.exit_code == 0, result.output
        assert _tables(yaml.safe_load(result.stdout)["routing"]) == _tables(ROUTING)
        assert result.stderr == ""
        assert not (tmp_path / "-").exists()

    def test_the_two_commands_round_trip(self, dmn_file, tmp_path):
        """Export, import, export again: the second DMN file is the first."""
        section, again = tmp_path / "routing.yaml", tmp_path / "again.dmn"

        _run("config-routing-import", "--dmn-file", str(dmn_file), "-o", str(section))
        result = _run("config-routing-export", "-f", str(section), "-o", str(again))

        assert result.exit_code == 0, result.output
        assert again.read_text() == dmn_file.read_text()

    def test_a_file_that_is_not_a_decision_table_exits_one(self, tmp_path):
        path = tmp_path / "other.dmn"
        path.write_text("<notDmn/>")
        output = tmp_path / "routing.yaml"

        result = _run(
            "config-routing-import", "--dmn-file", str(path), "-o", str(output)
        )

        assert result.exit_code == 1
        assert "cannot be read" in result.stderr
        assert not output.exists()


def test_neither_command_makes_an_aws_client(config_file, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("a routing conversion made an AWS client")

    dmn = tmp_path / "routing.dmn"
    with patch.object(boto3, "client", refuse), patch.object(boto3, "resource", refuse):
        exported = _run("config-routing-export", "-f", str(config_file), "-o", str(dmn))
        imported = _run(
            "config-routing-import",
            "--dmn-file",
            str(dmn),
            "-o",
            str(tmp_path / "r.yaml"),
        )

    assert (exported.exit_code, imported.exit_code) == (0, 0), (
        exported.output,
        imported.output,
    )


@pytest.mark.parametrize("command", ["config-routing-export", "config-routing-import"])
def test_neither_command_takes_a_stack(command):
    options = {param.name for param in cli.commands[command].params}

    assert "stack_name" not in options and "region" not in options
