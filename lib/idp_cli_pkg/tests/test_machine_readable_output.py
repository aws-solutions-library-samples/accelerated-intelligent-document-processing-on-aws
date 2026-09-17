# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Machine-readable CLI output must not be rendered by Rich.

Why these tests exist
---------------------
`config-revisions --json` emitted its payload with `console.print_json`, so in any
terminal with styling on the JSON began with an ANSI escape and
`idp-cli config-revisions --json | jq` failed at char 0 (issue 905).

The audit that followed found the same defect in eight more places, and two of the
three ways Rich alters a payload do not need colour at all:

- **Wrapping.** `console.print` hard-wraps at the console width, which is 80
  columns when stdout is not a terminal. `idp-cli config-download > config.yaml`
  therefore broke every line over 80 characters *in the redirected file*.
- **Markup.** `[...]` inside a string value is parsed as a Rich tag and silently
  dropped when it starts with a lowercase letter — a field description reading
  `value [in USD]` renders as `value `, and `[/bold]` raises `MarkupError` and
  kills the command outright.
- **Highlighting.** ANSI escapes, when styling is on.

So the rule is the one asserted here: payloads go through `emit_json` / `emit_raw`,
never through `console`.
"""

import io
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console
from rich.errors import MarkupError

from idp_cli import cli as cli_module
from idp_cli.cli import cli, emit_json, emit_raw
from idp_sdk.models import ConfigRevisionInfo, ConfigRevisionListResult

# A payload built to trip every one of the three failure modes at once: a value
# longer than any plausible console width, text Rich reads as a markup tag (the
# bracket must open with a lowercase letter, which `[in USD]` does), and a stray
# closing tag, which raises rather than mangles.
AWKWARD_PAYLOAD = {
    "notes": "a genuinely long revision note that runs well past eighty columns " * 3,
    "markup": "extract the [amount] field [in USD]",
    "stray_close": "see [/bold] in the prompt",
    "revision": 7,
}


@pytest.fixture
def forced_colour_console():
    """
    Point the CLI at a narrow console with styling forced ON.

    This is the environment the reporter had (`FORCE_COLOR=1`, or a pty) and the
    one the autouse `unstyled_cli_console` fixture deliberately removes — so these
    tests opt back in, to prove the command produces clean output even then rather
    than only when Rich happens to disable itself.

    Every styling input is passed explicitly: left to detection, `NO_COLOR=1` or
    `TERM=dumb` in the environment would quietly leave this console unstyled and
    the test would then prove nothing.
    """
    original = cli_module.console
    cli_module.console = Console(
        width=80,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
    )
    try:
        yield cli_module.console
    finally:
        cli_module.console = original


@pytest.mark.unit
def test_config_revisions_json_parses_with_colour_forced_on(forced_colour_console):
    """
    `--json` is a scripting interface, so it must parse regardless of the terminal.

    Fails before the fix with `JSONDecodeError: Expecting value: line 1 column 1`,
    because `console.print_json` puts a style escape before the opening brace.
    """
    with patch("idp_sdk.IDPClient") as mock_cls:
        client = MagicMock()
        mock_cls.return_value = client
        client.config.revisions.return_value = ConfigRevisionListResult(
            profile="lending",
            count=1,
            revisions=[
                ConfigRevisionInfo(
                    revision=3, published=True, notes=AWKWARD_PAYLOAD["notes"]
                )
            ],
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
    assert "\x1b[" not in result.output, (
        "--json output must contain no ANSI escapes; a consumer piping it into "
        f"jq gets a parse error at char 0. Got: {result.output[:120]!r}"
    )
    payload = json.loads(result.output)
    assert payload["profile"] == "lending"
    # And the note survived intact — wrapping would have split it across lines.
    assert payload["revisions"][0]["notes"] == AWKWARD_PAYLOAD["notes"]


@pytest.mark.unit
def test_emit_json_round_trips_through_a_narrow_forced_colour_console(
    forced_colour_console, capsys
):
    """
    The helper every `--json` path funnels through must be inert.

    Asserted on the helper rather than per command so that a new command using
    `emit_json` inherits the guarantee.
    """
    emit_json(AWKWARD_PAYLOAD)
    out = capsys.readouterr().out

    assert "\x1b[" not in out
    assert json.loads(out) == AWKWARD_PAYLOAD, (
        "the payload must survive verbatim: Rich would wrap the long value, drop "
        "'[amount]' and '[in USD]' as markup tags, and raise MarkupError on "
        "'[/bold]'"
    )


@pytest.mark.unit
def test_emit_json_passes_a_serialized_string_through_unchanged(capsys):
    """pydantic's `model_dump_json()` output is already JSON; don't re-encode it."""
    serialized = json.dumps(AWKWARD_PAYLOAD, indent=2)
    emit_json(serialized)
    assert capsys.readouterr().out == serialized + "\n"


@pytest.mark.unit
def test_emit_raw_leaves_yaml_byte_identical(forced_colour_console, capsys):
    """
    `config-download` / `config-template` write YAML to stdout to be redirected.

    Rich wrapping corrupts that even with colour off — an 80-column fold inside a
    long prompt value makes the file fail to load — so the YAML paths use
    `emit_raw`.
    """
    yaml_text = (
        "classes:\n"
        "  - name: invoice\n"
        "    prompt: |\n"
        "      Extract the amount [in USD] from this " + ("very " * 30) + "long line\n"
    )
    emit_raw(yaml_text)
    assert capsys.readouterr().out == yaml_text + "\n"


@pytest.mark.unit
def test_no_command_renders_a_payload_through_the_console():
    """
    Static guard: keep the next `--json` command from reintroducing the defect.

    A shape check on the source, not a behavioral one, because there is no way to
    enumerate every future command. It catches the forms that actually occurred
    (`print_json`, and `console.print` of a serialized or already-serialized
    payload); a wholly new form could still slip past, which is why the helper
    docstrings carry the reasoning too.
    """
    source = (Path(cli_module.__file__)).read_text(encoding="utf-8")
    offenders = [
        (n, line.strip())
        for n, line in enumerate(source.splitlines(), start=1)
        if re.search(
            r"console\.print_json\("
            r"|console\.print\(\s*[\w.]*(?:json\.dumps|yaml\.dump)"
            r"|console\.print\(\s*[\w.]*(?:json_output|yaml_content|model_dump_json)",
            line,
        )
    ]
    assert not offenders, (
        "machine-readable payloads must be written with emit_json()/emit_raw(), "
        "not rendered by Rich (see issue 905):\n"
        + "\n".join(f"  cli.py:{n}: {line}" for n, line in offenders)
    )


@pytest.mark.unit
def test_the_console_would_in_fact_corrupt_a_payload():
    """
    Pin the premise, so the guard above is not cargo cult.

    If a future Rich stops wrapping, eating markup and highlighting, this fails
    and the rules above can be relaxed deliberately rather than by accident.
    """
    buf = io.StringIO()
    styled = Console(
        file=buf,
        width=80,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
    )
    styled.print(json.dumps({"markup": "the amount [in USD]"}))
    rendered = buf.getvalue()
    assert "\x1b[" in rendered, "expected Rich to highlight"
    assert "[in USD]" not in re.sub(r"\x1b\[[0-9;]*m", "", rendered), (
        "expected Rich to consume '[in USD]' as a markup tag"
    )

    buf = io.StringIO()
    # force_terminal=False models the piped case explicitly rather than relying on
    # detection: with FORCE_COLOR in the environment even a StringIO console styles.
    plain = Console(file=buf, width=80, force_terminal=False)
    plain.print(json.dumps({"long": "x" * 200}))
    assert "\x1b[" not in buf.getvalue(), "a non-terminal console emits no escapes"
    with pytest.raises(json.JSONDecodeError):
        # ...and yet the payload is still broken, by the 80-column fold alone.
        json.loads(buf.getvalue())

    # A stray closing tag does not mangle the payload, it kills the command.
    with pytest.raises(MarkupError):
        Console(file=io.StringIO(), width=80, force_terminal=False).print(
            json.dumps({"prompt": "see [/bold] here"})
        )
