"""
CLI surface for reading configuration revisions back.

Why these tests exist
---------------------
`--config-revision` shipped documented-but-absent once already (see
test_config_revision.py), because nothing asserted the command's option surface.
These assert the *whole* read-back path an automated tuning loop needs:

- `config-upload` prints the revision it just created, so the next command can
  pin it. This is the piece whose absence forces the IDP Auto Optimizer extension
  to mint a new named profile per iteration.
- `config-revisions` lists a profile's history, with `--json` for scripting.
- `config-download --config-revision` fetches an exact revision.
- `config-list` shows the current revision per profile.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli import cli as cli_module
from idp_cli.cli import cli
from idp_sdk.models import (
    ConfigDownloadResult,
    ConfigListResult,
    ConfigRevisionInfo,
    ConfigRevisionListResult,
    ConfigUploadResult,
    ConfigVersionInfo,
)


@pytest.fixture
def wide_console(monkeypatch):
    """
    Pin the CLI's Rich console width so table assertions are about CONTENT.

    Setting COLUMNS is not enough: Rich only consults the environment on some
    paths, and under pytest-xdist (`make test` runs `-n auto`) there is no TTY, so
    the console falls back to a narrow default and ellipsizes the wider columns —
    the test then passes standalone and fails in the suite (issue 714). Assigning
    `console.width` overrides detection outright.
    """
    monkeypatch.setattr(cli_module.console, "width", 200)


def _client(monkey_target="idp_sdk.IDPClient"):
    """Patch IDPClient and hand back the mock instance the CLI will use."""
    patcher = patch(monkey_target)
    mock_cls = patcher.start()
    client = MagicMock()
    mock_cls.return_value = client
    return patcher, client


@pytest.mark.unit
def test_upload_prints_the_revision_it_created():
    patcher, client = _client()
    try:
        client.config.upload.return_value = ConfigUploadResult(
            success=True, version="lending", version_created=False, revision=7
        )
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("c.yaml", "w") as f:
                f.write("classes: []\n")
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    "s",
                    "--config-file",
                    "c.yaml",
                    "--config-profile",
                    "lending",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "r7" in result.output, (
            "config-upload must print the revision it created; without it a script "
            "cannot pin the run to what it just uploaded"
        )
        # And it should say how to use it — a bare number is a puzzle.
        assert "--config-revision 7" in result.output
    finally:
        patcher.stop()


@pytest.mark.unit
def test_upload_on_a_stack_without_history_does_not_invent_a_revision():
    patcher, client = _client()
    try:
        client.config.upload.return_value = ConfigUploadResult(
            success=True, version="lending", version_created=True, revision=None
        )
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("c.yaml", "w") as f:
                f.write("classes: []\n")
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    "s",
                    "--config-file",
                    "c.yaml",
                    "--config-profile",
                    "lending",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "--config-revision" not in result.output
        assert "created" in result.output
    finally:
        patcher.stop()


@pytest.mark.unit
def test_config_revisions_command_exists():
    result = CliRunner().invoke(cli, ["config-revisions", "--help"])
    assert result.exit_code == 0
    flattened = " ".join(result.output.split())
    assert "--config-profile, --config-version" in flattened
    assert "--json" in flattened


@pytest.mark.unit
def test_config_revisions_lists_history(wide_console):
    patcher, client = _client()
    try:
        client.config.revisions.return_value = ConfigRevisionListResult(
            profile="lending",
            count=2,
            revisions=[
                ConfigRevisionInfo(
                    revision=3,
                    created_at="2026-08-31T10:00:00Z",
                    created_by="a@example.com",
                    notes="raised topK",
                    published=True,
                ),
                ConfigRevisionInfo(
                    revision=2,
                    created_at="2026-08-31T09:00:00Z",
                    created_by="system",
                    label="baseline",
                    pinned=True,
                ),
            ],
        )
        result = CliRunner().invoke(
            cli,
            ["config-revisions", "--stack-name", "s", "--config-profile", "lending"],
        )
        assert result.exit_code == 0, result.output
        assert "r3" in result.output and "r2" in result.output
        assert "raised topK" in result.output
        # Why a revision survives pruning has to be visible, or a user cannot tell
        # which of their revisions the retention cap will drop next.
        assert "labeled" in result.output
        assert "test run" in result.output
        client.config.revisions.assert_called_once_with(config_profile="lending")
    finally:
        patcher.stop()


@pytest.mark.unit
def test_config_revisions_json_is_machine_readable():
    patcher, client = _client()
    try:
        client.config.revisions.return_value = ConfigRevisionListResult(
            profile="lending",
            count=1,
            revisions=[ConfigRevisionInfo(revision=3, published=True)],
        )
        result = CliRunner().invoke(
            cli,
            [
                "config-revisions",
                "--stack-name",
                "s",
                "--config-profile",
                "lending",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["profile"] == "lending"
        assert payload["revisions"][0]["revision"] == 3
    finally:
        patcher.stop()


@pytest.mark.unit
def test_no_history_reads_as_no_history_not_an_error():
    patcher, client = _client()
    try:
        client.config.revisions.return_value = ConfigRevisionListResult(
            profile="untouched", count=0, revisions=[]
        )
        result = CliRunner().invoke(
            cli,
            ["config-revisions", "--stack-name", "s", "--config-profile", "untouched"],
        )
        # A profile untouched since the upgrade genuinely has no history; exiting
        # non-zero would break a script that is simply asking.
        assert result.exit_code == 0, result.output
        assert "No revisions" in result.output
    finally:
        patcher.stop()


@pytest.mark.unit
def test_download_accepts_a_revision_and_passes_it_through():
    patcher, client = _client()
    try:
        client.config.download.return_value = ConfigDownloadResult(
            config={}, yaml_content="classes: []\n", revision=7
        )
        result = CliRunner().invoke(
            cli,
            [
                "config-download",
                "--stack-name",
                "s",
                "--config-profile",
                "lending",
                "--config-revision",
                "7",
            ],
        )
        assert result.exit_code == 0, result.output
        assert client.config.download.call_args.kwargs["config_revision"] == 7
    finally:
        patcher.stop()


@pytest.mark.unit
def test_download_rejects_a_revision_without_a_profile():
    """
    "Revision 7 of whatever is active" changes meaning the moment someone
    activates another profile, so it is refused up front rather than resolved.
    """
    result = CliRunner().invoke(
        cli, ["config-download", "--stack-name", "s", "--config-revision", "7"]
    )
    assert result.exit_code != 0
    assert "requires --config-profile" in result.output


@pytest.mark.unit
def test_config_list_shows_the_current_revision(wide_console):
    patcher, client = _client()
    try:
        client.config.list.return_value = ConfigListResult(
            count=2,
            versions=[
                ConfigVersionInfo(
                    version_name="lending",
                    is_active=True,
                    latest_revision=9,
                    published_revision=7,
                ),
                ConfigVersionInfo(version_name="untouched"),
            ],
        )
        result = CliRunner().invoke(cli, ["config-list", "--stack-name", "s"])
        assert result.exit_code == 0, result.output
        # The PUBLISHED revision, not the latest: the head reflects r7, and r7 is
        # what a caller should pin.
        assert "r7" in result.output
        assert "r9" not in result.output
        # A profile with no history shows nothing rather than "r0", which would
        # read as a real revision.
        assert "r0" not in result.output
    finally:
        patcher.stop()


@pytest.mark.unit
def test_delete_warns_before_removing_a_stack_managed_profile():
    """
    The UI refuses to delete a managed profile, so the CLI is the only route for
    cleaning up profiles an uninstalled extension left behind. It must say what it
    is about to delete: the same flag also protects the built-in config_library
    presets, which the next stack update would simply restore.
    """
    patcher, client = _client()
    try:
        client.config.list.return_value = ConfigListResult(
            count=1,
            versions=[
                ConfigVersionInfo(version_name="claims-pack-v0.2.0", managed=True)
            ],
        )
        client.config.delete.return_value = MagicMock(success=True, error=None)
        result = CliRunner().invoke(
            cli,
            [
                "config-delete",
                "--stack-name",
                "s",
                "--config-profile",
                "claims-pack-v0.2.0",
            ],
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        assert "stack-managed" in result.output
        assert client.config.delete.called
    finally:
        patcher.stop()


@pytest.mark.unit
def test_delete_does_not_warn_for_an_ordinary_profile():
    patcher, client = _client()
    try:
        client.config.list.return_value = ConfigListResult(
            count=1, versions=[ConfigVersionInfo(version_name="lending", managed=False)]
        )
        client.config.delete.return_value = MagicMock(success=True, error=None)
        result = CliRunner().invoke(
            cli,
            ["config-delete", "--stack-name", "s", "--config-profile", "lending"],
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        assert "stack-managed" not in result.output
    finally:
        patcher.stop()


@pytest.mark.unit
def test_force_delete_skips_the_managed_lookup_entirely():
    """A script passing --force must not pay for a table scan, or depend on one."""
    patcher, client = _client()
    try:
        client.config.delete.return_value = MagicMock(success=True, error=None)
        result = CliRunner().invoke(
            cli,
            [
                "config-delete",
                "--stack-name",
                "s",
                "--config-profile",
                "claims-pack-v0.2.0",
                "--force",
            ],
        )
        assert result.exit_code == 0, result.output
        assert not client.config.list.called
    finally:
        patcher.stop()


@pytest.mark.unit
def test_upload_passes_revision_notes_through():
    """
    Without notes, an ordinary edit records a revision with a timestamp and an
    author but no statement of what changed — which makes the history of a profile
    an automated loop writes to unreadable.
    """
    patcher, client = _client()
    try:
        client.config.upload.return_value = ConfigUploadResult(
            success=True, version="lending", revision=4
        )
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("c.yaml", "w") as f:
                f.write("classes: []\n")
            result = runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    "s",
                    "--config-file",
                    "c.yaml",
                    "--config-profile",
                    "lending",
                    "--revision-notes",
                    "raised topK to 20",
                    "--version-description",
                    "Production tuning",
                ],
            )
        assert result.exit_code == 0, result.output
        kwargs = client.config.upload.call_args.kwargs
        assert kwargs["revision_notes"] == "raised topK to 20"
        # The two must not be conflated: description is the PROFILE's and is
        # overwritten by every save; notes are per-revision and immutable.
        assert kwargs["description"] == "Production tuning"
    finally:
        patcher.stop()


@pytest.mark.unit
def test_upload_without_notes_sends_none_rather_than_a_placeholder():
    patcher, client = _client()
    try:
        client.config.upload.return_value = ConfigUploadResult(
            success=True, version="lending", revision=4
        )
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("c.yaml", "w") as f:
                f.write("classes: []\n")
            runner.invoke(
                cli,
                [
                    "config-upload",
                    "--stack-name",
                    "s",
                    "--config-file",
                    "c.yaml",
                    "--config-profile",
                    "lending",
                ],
            )
        assert client.config.upload.call_args.kwargs["revision_notes"] is None
    finally:
        patcher.stop()
