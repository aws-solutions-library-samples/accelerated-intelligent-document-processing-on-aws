# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The CLI has to print a blueprint the BDA sync could not delete.

A replace-mode sync removes a blueprint from the BDA project before deleting it — BDA
refuses to delete one the project still associates — so a failed delete leaves a
blueprint that no project-scoped read can see and that only the account-wide cleanup
will remove. The SDK now returns those ARNs; a CLI that prints "✓ BDA sync completed
successfully" and nothing else puts the user back where they started.

It is deliberately *not* folded into the failure counts: the classes all synced, and
reporting one as failed would be a different wrong answer. So each test below asserts
both halves — the ARN appears, and the success wording is unchanged.

`--stack-name` is a required option on both commands, so the runs here go through the
real option parsing; only `IDPClient` is doubled, and it returns real result models
rather than mocks so that a field the CLI reads but the model does not have would fail
here rather than render as a `MagicMock` repr.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli
from idp_sdk.models.config import ConfigActivateResult, ConfigSyncBdaResult

ORPHAN_ONE = "arn:aws:bedrock:us-west-2:123456789012:blueprint/idp-Receipt-aaaa"
ORPHAN_TWO = "arn:aws:bedrock:us-west-2:123456789012:blueprint/idp-Payslip-bbbb"


def _client(*, sync_result=None, activate_result=None):
    client = MagicMock()
    client.config.sync_bda.return_value = sync_result
    client.config.activate.return_value = activate_result
    return patch("idp_sdk.IDPClient", return_value=client)


def _run(args):
    return CliRunner().invoke(cli, args)


def _flat(run) -> str:
    """The output with runs of whitespace collapsed.

    `rich` hard-wraps at the console width, so a phrase asserted below can arrive
    split across two lines. Collapsing first makes these assertions about the words
    printed rather than about the width the test happened to run at.
    """
    return " ".join(run.output.split())


SYNC_ARGS = ["config-sync-bda", "--stack-name", "s", "--config-profile", "v1"]
ACTIVATE_ARGS = ["config-activate", "--stack-name", "s", "--config-profile", "v1"]


@pytest.mark.unit
class TestConfigSyncBdaOutput:
    def test_an_orphan_is_printed_beside_a_successful_sync(self):
        result = ConfigSyncBdaResult(
            success=True,
            direction="idp_to_bda",
            classes_synced=1,
            processed_classes=["Invoice"],
            orphaned_blueprint_arns=[ORPHAN_ONE],
        )
        with _client(sync_result=result):
            run = _run(SYNC_ARGS)

        assert run.exit_code == 0
        assert "BDA sync completed successfully" in _flat(run)
        assert "could not be deleted" in _flat(run)
        assert ORPHAN_ONE in _flat(run)
        assert "cleanup" in _flat(run)

    def test_a_clean_sync_prints_nothing_about_orphans(self):
        """Non-vacuity for the assertions above."""
        result = ConfigSyncBdaResult(
            success=True, direction="idp_to_bda", classes_synced=1
        )
        with _client(sync_result=result):
            run = _run(SYNC_ARGS)

        assert run.exit_code == 0
        assert "could not be deleted" not in _flat(run)
        assert "cleanup" not in _flat(run)

    def test_every_orphan_is_printed(self):
        result = ConfigSyncBdaResult(
            success=True,
            direction="idp_to_bda",
            classes_synced=1,
            orphaned_blueprint_arns=[ORPHAN_ONE, ORPHAN_TWO],
        )
        with _client(sync_result=result):
            run = _run(SYNC_ARGS)

        assert ORPHAN_ONE in _flat(run) and ORPHAN_TWO in _flat(run)
        assert "2 blueprint(s)" in _flat(run)

    def test_an_orphan_is_printed_on_the_failure_path_too(self):
        """This branch exits 1, so anything printed after the exit is never seen."""
        result = ConfigSyncBdaResult(
            success=False,
            direction="idp_to_bda",
            classes_synced=1,
            classes_failed=1,
            error="1 class(es) failed to sync",
            orphaned_blueprint_arns=[ORPHAN_ONE],
        )
        with _client(sync_result=result):
            run = _run(SYNC_ARGS)

        assert run.exit_code == 1
        assert ORPHAN_ONE in _flat(run)


@pytest.mark.unit
class TestConfigActivateOutput:
    def test_an_orphan_is_printed_beside_a_successful_activation(self):
        result = ConfigActivateResult(
            success=True,
            activated_version="v1",
            bda_synced=True,
            bda_classes_synced=1,
            bda_orphaned_blueprint_arns=[ORPHAN_ONE],
        )
        with _client(activate_result=result):
            run = _run(ACTIVATE_ARGS)

        assert run.exit_code == 0
        assert "Successfully activated configuration profile" in _flat(run)
        assert ORPHAN_ONE in _flat(run)

    def test_a_clean_activation_prints_nothing_about_orphans(self):
        result = ConfigActivateResult(
            success=True,
            activated_version="v1",
            bda_synced=True,
            bda_classes_synced=1,
        )
        with _client(activate_result=result):
            run = _run(ACTIVATE_ARGS)

        assert run.exit_code == 0
        assert "could not be deleted" not in _flat(run)

    def test_an_orphan_is_printed_on_a_failed_activation(self):
        """The path that made the placement matter. `bda_synced` is False on **every**
        failing return the SDK builds, including the one that carries this sync's own
        orphan list, so printing from inside an `if result.bda_synced:` branch — or
        after the `sys.exit(1)` — hides exactly the outcome most likely to have left a
        blueprint behind."""
        result = ConfigActivateResult(
            success=False,
            activated_version="v1",
            bda_synced=False,
            error="BDA sync error: boom",
            bda_orphaned_blueprint_arns=[ORPHAN_ONE],
        )
        with _client(activate_result=result):
            run = _run(ACTIVATE_ARGS)

        assert run.exit_code == 1
        assert "Failed to activate configuration profile" in _flat(run)
        assert ORPHAN_ONE in _flat(run)

    def test_an_activation_that_left_nothing_behind_prints_nothing(self):
        """Non-vacuity for the test above, on the same failing shape: the print is
        driven by the list being non-empty and by nothing else."""
        result = ConfigActivateResult(
            success=False,
            activated_version="v1",
            bda_synced=False,
            error="BDA sync error: boom",
        )
        with _client(activate_result=result):
            run = _run(ACTIVATE_ARGS)

        assert run.exit_code == 1
        assert "could not be deleted" not in _flat(run)
