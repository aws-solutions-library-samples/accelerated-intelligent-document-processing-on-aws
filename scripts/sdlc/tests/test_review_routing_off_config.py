# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The configuration a test deployment uploads to switch Review Routing off.

A new stack has Review Routing on in the profiles it creates. The integration
test and the GovCloud transform test process documents unattended, so they
upload `scripts/sdlc/config/review-routing-off.yaml` to the default profile
first. An upload to an existing profile is applied as a change to it, so the
file must hold that one setting and nothing else: any other key would overwrite
what the stack's own preset set. No AWS and no subprocess: `run_command` is
replaced.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def _config_file(cbd) -> Path:
    return REPO_ROOT / cbd.REVIEW_ROUTING_OFF_CONFIG


def test_the_file_switches_review_routing_off_and_nothing_else(cbd):
    assert cbd.REVIEW_ROUTING_OFF_CONFIG == (
        "scripts/sdlc/config/review-routing-off.yaml"
    )
    text = _config_file(cbd).read_text(encoding="utf-8")
    assert yaml.safe_load(text) == {"hitl": {"enabled": False}}
    assert text.splitlines()[:2] == [
        "# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
        "# SPDX-License-Identifier: MIT-0",
    ]


def test_the_file_validates(cbd):
    """What `idp-cli config-upload` checks before it stores anything."""
    from idp_common.config.merge_utils import validate_config

    config = yaml.safe_load(_config_file(cbd).read_text(encoding="utf-8"))
    result = validate_config(config)

    assert result["valid"] is True, result["errors"]
    assert result["errors"] == []
    # Every key in it is one the configuration models read.
    assert result["ignored_keys"] == []
    assert result["merged_config"]["hitl"]["enabled"] is False


@pytest.fixture
def commands(cbd, monkeypatch):
    """Record what `switch_off_review_routing` runs; the upload exits 0."""
    ran = []

    def fake_run_command(cmd, check=True, timeout=cbd.DEFAULT_COMMAND_TIMEOUT):
        ran.append((cmd, check))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(cbd, "run_command", fake_run_command)
    return ran


@pytest.mark.parametrize(
    ("region", "region_flag"),
    [(None, ""), ("", ""), ("us-gov-west-1", " --region us-gov-west-1")],
)
def test_the_upload_goes_to_the_default_profile(cbd, commands, region, region_flag):
    assert cbd.switch_off_review_routing("idp-x", region) == {"success": True}
    # check=False: a refused upload is reported, not raised.
    assert commands == [
        (
            "idp-cli config-upload --stack-name idp-x "
            "--config-file scripts/sdlc/config/review-routing-off.yaml "
            f"--config-version default{region_flag}",
            False,
        )
    ]


def test_the_region_defaults_to_the_builds_own(cbd, commands):
    assert cbd.switch_off_review_routing("idp-x") == {"success": True}
    assert "--region" not in commands[0][0]


@pytest.mark.parametrize(
    ("stdout", "stderr", "detail"),
    [
        (
            "uploading\n",
            "Traceback\n✗ Error: no such stack\n",
            "✗ Error: no such stack",
        ),
        (
            "✗ Failed to upload configuration: Validation failed\n",
            "",
            "Validation failed",
        ),
        ("", "", "no output"),
    ],
)
def test_a_non_zero_exit_is_a_failure_that_says_why(
    cbd, monkeypatch, stdout, stderr, detail
):
    monkeypatch.setattr(
        cbd,
        "run_command",
        lambda cmd, check=True, timeout=None: SimpleNamespace(
            stdout=stdout, stderr=stderr, returncode=1
        ),
    )
    result = cbd.switch_off_review_routing("idp-x")
    assert result["success"] is False
    assert result["error"].startswith("the configuration upload exited 1: ")
    assert detail in result["error"]


def test_a_result_with_no_exit_code_reads_as_success(cbd, monkeypatch):
    """The transform test's command stub returns an object with only `stdout`."""
    monkeypatch.setattr(
        cbd, "run_command", lambda cmd, **kwargs: SimpleNamespace(stdout="")
    )
    assert cbd.switch_off_review_routing("idp-x") == {"success": True}
