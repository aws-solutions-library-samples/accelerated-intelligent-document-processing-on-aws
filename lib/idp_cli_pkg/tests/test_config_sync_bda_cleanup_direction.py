# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`config-sync-bda --direction cleanup-orphaned` — the route to the printed remedy.

`_print_orphaned_blueprints` tells the operator that the orphaned-blueprint cleanup is
what removes a blueprint a replace-mode sync disassociated but could not delete. That
statement was true and unfollowable: the cleanup existed only as a branch of the
`syncBdaIdp` resolver, `--direction` offered three values and none of them was it, and
`ConfigOperation.sync_bda` went straight to
`create_blueprints_from_custom_configuration` (#1207).

Two things these tests pin that are easy to get wrong:

- **The accepted value list is read off click's own `Choice`**, not written out here. A
  hand-written list is a second copy of the contract and would keep passing if the
  option were renamed or a value dropped.
- **The cleanup prompts, and declining must not call the SDK at all.** It deletes every
  prefixed blueprint the named profile does not account for, account-wide, so an
  assertion that the output said "cancelled" would pass over an implementation that
  printed that *after* deleting. The assertion is on the collaborator not being called.

`IDPClient` is doubled, and it returns a real `ConfigSyncBdaResult` rather than a mock,
so a field the CLI reads but the model does not have fails here instead of rendering as
a `MagicMock` repr.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli
from idp_sdk.models.config import ConfigSyncBdaResult

ORPHAN = "arn:aws:bedrock:us-west-2:123456789012:blueprint/idp-Receipt-aaaa"

CLEANUP_ARGS = [
    "config-sync-bda",
    "--stack-name",
    "s",
    "--config-profile",
    "v1",
    "--direction",
    "cleanup-orphaned",
]


def _direction_choices() -> list:
    """The values `--direction` accepts, read off the command click actually built.

    Derived from `cli.commands[...].params` rather than restated, so this is the
    option's own contract and not a copy of it.
    """
    command = cli.commands["config-sync-bda"]
    for param in command.params:
        if param.name == "direction":
            return list(param.type.choices)
    raise AssertionError("config-sync-bda has no --direction option")


def _client(result):
    client = MagicMock()
    client.config.sync_bda.return_value = result
    return client


def _flat(run) -> str:
    """Output with runs of whitespace collapsed — `rich` hard-wraps at console width."""
    return " ".join(run.output.split())


def _cleanup_result(*, success=True, deleted=2, failed=0, error=None, orphans=()):
    return ConfigSyncBdaResult(
        success=success,
        direction="cleanup_orphaned",
        cleanup_deleted_count=deleted,
        cleanup_failed_count=failed,
        orphaned_blueprint_arns=list(orphans),
        error=error,
    )


@pytest.mark.unit
class TestTheDirectionIsReachable:
    def test_cleanup_orphaned_is_an_accepted_direction(self):
        """The whole of #1207: the value the printed remedy names is selectable."""
        assert "cleanup-orphaned" in _direction_choices()

    def test_the_three_sync_directions_are_still_accepted(self):
        """Adding a value must not have replaced the existing contract."""
        assert set(_direction_choices()) >= {
            "bidirectional",
            "bda-to-idp",
            "idp-to-bda",
        }

    def test_an_unknown_direction_is_still_refused(self):
        """Non-vacuity for the two assertions above: the Choice really constrains."""
        run = CliRunner().invoke(
            cli,
            ["config-sync-bda", "--stack-name", "s", "--direction", "cleanup_orphaned"],
        )
        assert run.exit_code == 2, run.output

    def test_the_printed_remedy_names_the_cli_invocation(self):
        """The message under #1194 must point somewhere the reader can go.

        Asserted on the sync path, which is where a user meets it.
        """
        client = _client(
            ConfigSyncBdaResult(
                success=True,
                direction="idp_to_bda",
                classes_synced=1,
                orphaned_blueprint_arns=[ORPHAN],
            )
        )
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(
                cli, ["config-sync-bda", "--stack-name", "s", "--config-profile", "v1"]
            )

        flat = _flat(run)
        assert run.exit_code == 0
        assert "config-sync-bda" in flat
        assert "--direction cleanup-orphaned" in flat
        # The pointer to the API operation is kept, not replaced: the UI has no
        # control, so that is still the only route for a non-CLI user.
        assert "syncBdaIdp" in flat
        assert ORPHAN in flat


@pytest.mark.unit
class TestTheDirectionReachesTheSdk:
    def test_the_dashed_value_is_forwarded_with_underscores(self):
        client = _client(_cleanup_result())
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(cli, CLEANUP_ARGS + ["--force"])

        assert run.exit_code == 0, run.output
        client.config.sync_bda.assert_called_once_with(
            direction="cleanup_orphaned", mode="replace", config_version="v1"
        )

    def test_a_successful_cleanup_reports_the_deletion_count(self):
        client = _client(_cleanup_result(deleted=3))
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(cli, CLEANUP_ARGS + ["--force"])

        flat = _flat(run)
        assert run.exit_code == 0
        assert "3 deleted" in flat
        # Not reported as classes: the cleanup processes none, and the count fields
        # for classes stay at their defaults.
        assert "Classes synced" not in flat

    def test_a_cleanup_that_could_not_delete_exits_non_zero(self):
        """The counts are the outcome, so a failure has to reach the shell."""
        client = _client(
            _cleanup_result(
                success=False,
                deleted=1,
                failed=2,
                error="Deleted 1 orphaned blueprints, 2 failed",
                orphans=[ORPHAN],
            )
        )
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(cli, CLEANUP_ARGS + ["--force"])

        flat = _flat(run)
        assert run.exit_code == 1
        assert "Blueprints deleted: 1" in flat
        assert "Blueprints failed: 2" in flat
        # The ones it could not delete are still orphaned, so they are still named.
        assert ORPHAN in flat


@pytest.mark.unit
class TestTheDestructiveOperationConfirms:
    def test_declining_the_prompt_does_not_reach_the_sdk(self):
        """The assertion that matters: nothing is deleted, not merely that it said so."""
        client = _client(_cleanup_result())
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(cli, CLEANUP_ARGS, input="n\n")

        assert run.exit_code == 1, run.output
        assert "Cleanup cancelled" in _flat(run)
        client.config.sync_bda.assert_not_called()

    def test_the_default_answer_is_no(self):
        """An empty answer -- a bare Enter, or a closed stdin -- must not delete."""
        client = _client(_cleanup_result())
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(cli, CLEANUP_ARGS, input="\n")

        assert run.exit_code == 1
        client.config.sync_bda.assert_not_called()

    def test_accepting_the_prompt_runs_the_cleanup(self):
        """Non-vacuity for the two above: the prompt is answerable."""
        client = _client(_cleanup_result())
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(cli, CLEANUP_ARGS, input="y\n")

        assert run.exit_code == 0, run.output
        client.config.sync_bda.assert_called_once()

    def test_force_skips_the_prompt(self):
        """With no input available at all, --force must still run it."""
        client = _client(_cleanup_result())
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(cli, CLEANUP_ARGS + ["--force"], input="")

        assert run.exit_code == 0, run.output
        client.config.sync_bda.assert_called_once()

    def test_the_prompt_names_the_profile_that_decides_what_survives(self):
        """Naming the wrong profile deletes live blueprints, so the prompt says which."""
        client = _client(_cleanup_result())
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(cli, CLEANUP_ARGS, input="n\n")

        flat = _flat(run)
        assert "account-wide" in flat
        assert "v1" in flat

    def test_the_prompt_says_the_active_profile_when_none_is_named(self):
        client = _client(_cleanup_result())
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(
                cli,
                [
                    "config-sync-bda",
                    "--stack-name",
                    "s",
                    "--direction",
                    "cleanup-orphaned",
                ],
                input="n\n",
            )

        assert "the active profile" in _flat(run)

    def test_a_sync_direction_does_not_prompt(self):
        """Non-vacuity the other way: the prompt is scoped to the destructive value.

        `--mode replace` deletes too, but only inside the named profile's project;
        it has existing callers and gaining a prompt would break them.
        """
        client = _client(
            ConfigSyncBdaResult(success=True, direction="idp_to_bda", classes_synced=1)
        )
        with patch("idp_sdk.IDPClient", return_value=client):
            run = CliRunner().invoke(
                cli,
                [
                    "config-sync-bda",
                    "--stack-name",
                    "s",
                    "--direction",
                    "idp-to-bda",
                ],
                input="",
            )

        assert run.exit_code == 0, run.output
        client.config.sync_bda.assert_called_once()
        assert "cancelled" not in _flat(run)
