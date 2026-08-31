"""
`--config-profile` / `config_profile` as the new name for `--config-version`.

Why these tests exist
---------------------
The product renamed the entity to **Configuration Profile**, but every script
anyone has written against the CLI or SDK says `--config-version` /
`config_version`. Both names therefore have to work, forever-ish, and the two
have to mean exactly the same thing — an alias that reaches a *different*
variable is worse than no alias, because the caller believes they selected a
profile and the run silently uses the active one.

Two of these tests walk the whole command tree / operation surface rather than
naming individual commands. That is deliberate: the failure mode being guarded
is "someone adds the twelfth command and only wires the old name", and a test
that enumerates today's commands cannot see that.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from idp_cli.cli import cli

# dest -> (new flag, former flag)
ALIASED_PARAMS = {
    "config_version": ("--config-profile", "--config-version"),
    "target_version": ("--target-profile", "--target-version"),
}


def _walk(cmd, name=""):
    yield name or cmd.name, cmd
    if isinstance(cmd, click.Group):
        for sub in cmd.commands.values():
            yield from _walk(sub, f"{name} {sub.name}".strip())


@pytest.mark.unit
def test_every_command_offers_both_flag_names():
    missing = []
    checked = 0
    for command_name, command in _walk(cli):
        for param in command.params:
            if param.name not in ALIASED_PARAMS:
                continue
            checked += 1
            new_flag, old_flag = ALIASED_PARAMS[param.name]
            opts = set(param.opts) | set(param.secondary_opts)
            if not {new_flag, old_flag} <= opts:
                missing.append((command_name, param.name, sorted(opts)))
    assert checked >= 12, f"expected the known aliased options, found {checked}"
    assert not missing, (
        f"commands exposing only one of the two names: {missing}. Both must work: "
        f"the new one because it matches the product's vocabulary, the old one "
        f"because existing scripts use it."
    )


@pytest.mark.unit
def test_help_says_the_old_name_still_works():
    result = CliRunner().invoke(cli, ["process", "--help"])
    assert result.exit_code == 0
    # Click prints both names on the option line; the help text has to say which
    # one to prefer, or a reader cannot tell whether the old one is deprecated
    # and about to be removed. Click hard-wraps help text to the terminal width,
    # so compare on collapsed whitespace rather than the literal sentence.
    flattened = " ".join(result.output.split())
    assert "--config-profile, --config-version" in flattened
    assert "--config-version is the former name and still works" in flattened


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["--config-profile", "--config-version"])
def test_both_flags_reach_the_sdk_identically(flag):
    with patch("idp_sdk.IDPClient") as mock_client_cls:
        client = MagicMock()
        mock_client_cls.return_value = client
        client.batch.process.return_value = {
            "batch_id": "b1",
            "document_ids": [],
            "queued": 0,
            "uploaded": 0,
            "failed": 0,
        }
        CliRunner().invoke(
            cli,
            ["process", "--stack-name", "s", "--dir", ".", flag, "lending"],
        )
        assert client.batch.process.called, "batch.process was never reached"
        assert client.batch.process.call_args.kwargs.get("config_version") == "lending"


@pytest.mark.unit
def test_the_cli_never_passes_the_new_keyword_to_the_sdk():
    """
    The CLI resolves the alias itself, at the Click layer, and calls the SDK with
    the single internal name. Passing both onward would give two places that can
    disagree about which profile a run used.
    """
    source = (Path(cli.callback.__code__.co_filename)).read_text()
    assert "config_profile=" not in source, (
        "the CLI should hand the SDK config_version only; Click already collapsed "
        "--config-profile / --config-version into that one dest"
    )
