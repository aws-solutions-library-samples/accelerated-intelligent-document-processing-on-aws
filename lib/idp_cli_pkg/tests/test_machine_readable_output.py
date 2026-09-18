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

- **Wrapping.** `console.print` hard-wraps at the console width, which Rich takes
  from whichever standard stream is a terminal, falling back to 80 columns when
  none is. `idp-cli config-download > config.yaml` therefore broke every longer
  line *in the redirected file*.
- **Markup.** `[...]` inside a string value is parsed as a Rich tag and silently
  dropped when it starts with a lowercase letter — a field description reading
  `value [in USD]` renders as `value `, and `[/bold]` raises `MarkupError` and
  kills the command outright.
- **Highlighting.** ANSI escapes, when styling is on.

So the first rule is the one asserted here: payloads go through `emit_json` /
`emit_raw`, never through `console`.

The second rule is that the payload gets the stream to itself. Writing it
unrendered is necessary but not sufficient — `status --format json` and
`config-download` printed human progress to stdout ahead of it, so `| jq` still
failed at char 0 and a redirected config still gained a bogus top-level key. Those
lines go to `err_console` (stderr) now, and the two tests below pin it.

Note the assertions read `result.stdout`, not `result.output`: from Click 8.2 the
latter is the two streams combined, so it cannot tell a clean pipe from a polluted
one.
"""

import io
import json
import re
from importlib.metadata import version
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
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

    # `result.stdout`, not `result.output`: Click's `output` is the two streams
    # combined, and the guarantee under test is about stdout specifically — what a
    # consumer's pipe actually receives.
    assert result.exit_code == 0, result.output
    assert "\x1b[" not in result.stdout, (
        "--json output must contain no ANSI escapes; a consumer piping it into "
        f"jq gets a parse error at char 0. Got: {result.stdout[:120]!r}"
    )
    payload = json.loads(result.stdout)
    assert payload["profile"] == "lending"
    # And the note survived intact — wrapping would have split it across lines.
    assert payload["revisions"][0]["notes"] == AWKWARD_PAYLOAD["notes"]


@pytest.mark.unit
def test_status_json_stdout_carries_the_payload_and_nothing_else():
    """
    Writing the payload unrendered is not enough if human text shares the stream.

    `status --batch-id b --format json` printed four progress and summary lines
    ahead of the JSON, so `| jq` still failed with `Expecting value: line 1
    column 1` — the same symptom #905 reported against `config-revisions`. Those
    lines now go to stderr, where an interactive user still sees them.
    """
    search_results = {
        "success": True,
        "count": 1,
        "items": [{"ObjectKey": {"S": "batch-123/invoice.pdf"}}],
    }
    with (
        patch("idp_cli.search_tracking_table.TrackingTableSearcher") as searcher_cls,
        patch("idp_sdk.IDPClient"),
        patch("idp_cli.cli.display") as mock_display,
        patch("idp_cli.cli._batch_status_to_display_dicts", return_value=({}, {})),
    ):
        searcher_cls.return_value.search_by_pk_and_status.return_value = search_results
        mock_display.format_status_json.return_value = json.dumps(
            {"exit_code": 0, "documents": []}
        )
        result = CliRunner().invoke(
            cli,
            [
                "status",
                "--stack-name",
                "s",
                "--batch-id",
                "batch-123",
                "--format",
                "json",
            ],
        )

    assert json.loads(result.stdout) == {"exit_code": 0, "documents": []}, (
        "stdout must be the JSON document alone; anything else and `| jq` fails at "
        f"char 0. Got: {result.stdout[:160]!r}"
    )
    # The progress is not discarded, just moved — an interactive user still sees it.
    assert "Searching for documents" in result.stderr
    assert "Search Results for: batch-123" in result.stderr


@pytest.mark.unit
def test_config_download_to_stdout_is_loadable_yaml():
    """
    The redirect this documents — `config-download > config.yaml` — must be usable.

    This is the worst of the nine cases. Before the payload fix the progress line
    plus a folded long line made `yaml.safe_load` raise, which at least failed
    loudly. Unrendered YAML with the progress line still on stdout is worse: the
    file parses, and `Downloading config from stack` becomes an extra top-level key,
    so the config is silently wrong. Only splitting the streams fixes it.
    """
    yaml_content = (
        "classes:\n"
        "  - name: invoice\n"
        "    prompt: |\n"
        "      Extract the amount [in USD] from this " + ("very " * 30) + "long line\n"
    )
    with patch("idp_sdk.IDPClient") as mock_cls:
        mock_cls.return_value.config.download.return_value = MagicMock(
            yaml_content=yaml_content
        )
        result = CliRunner().invoke(
            cli, ["config-download", "--stack-name", "my-stack"]
        )

    loaded = yaml.safe_load(result.stdout)
    assert list(loaded) == ["classes"], (
        "a redirected config must have no key but its own; a progress line on stdout "
        f"parses as one and nothing tells the user. Got: {list(loaded)}"
    )
    # The long prompt line survived unfolded, and the bracketed text with it.
    assert "[in USD]" in loaded["classes"][0]["prompt"]
    assert "Downloading config from stack" in result.stderr


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
def test_emit_raw_leaves_yaml_unrendered(forced_colour_console, capsys):
    """
    `config-download` / `config-create` write YAML to stdout to be redirected.

    Rich wrapping corrupts that even with colour off — a fold inside a long prompt
    value makes the file fail to load — so the YAML paths use `emit_raw`.

    The guarantee is that the text is not *rendered*: not reflowed, not reinterpreted
    as markup, not highlighted. It is not byte-identical, and does not need to be —
    `click.echo` adds the trailing newline asserted below, which is what
    `console.print` did too, so no consumer sees a change.
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

    Matched against the whole file rather than line by line, because the repo's own
    formatter opens the hole otherwise. The call sites this replaced sat at 20-24
    spaces of indentation, where a slightly longer variable name pushes the call
    past the 88-column limit and `ruff format` rewrites it as::

        console.print(
            json.dumps(schema, indent=2)
        )

    A per-line search finds nothing in that form. `\\s*` in the pattern spans
    newlines, so one `re.S` search over the source catches both shapes; the line
    number is recovered by counting newlines before the match.

    Every module beside `cli.py` is scanned too. None of them prints a payload
    today, so this is prevention: a `--json` path added to `display.py`, `chat.py`
    or a new module would otherwise be invisible here.
    """
    pattern = (
        r"console\.print_json\("
        r"|console\.print\(\s*[\w.]*(?:json\.dumps|yaml\.dump)"
        r"|console\.print\(\s*[\w.]*(?:json_output|yaml_content|model_dump_json)"
    )
    package_dir = Path(cli_module.__file__).parent
    offenders = []
    for path in sorted(package_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(pattern, source, re.S):
            line_no = source.count("\n", 0, match.start()) + 1
            offenders.append(
                (f"{path.name}:{line_no}", " ".join(match.group(0).split()))
            )

    assert not offenders, (
        "machine-readable payloads must be written with emit_json()/emit_raw(), "
        "not rendered by Rich (see issue 905):\n"
        + "\n".join(f"  {where}: {snippet}" for where, snippet in offenders)
    )


@pytest.mark.unit
def test_the_guard_sees_the_form_ruff_format_produces():
    """
    The guard above must match across newlines, not line by line.

    `ruff format` splits a `console.print(json.dumps(...))` that exceeds 88 columns
    — which the replaced call sites did, at 20-24 spaces of indentation — into a
    three-line call. A line-by-line guard scores that as clean, so the repo's own
    formatter could reopen the exact hole the guard exists to close. This fails if
    the whole-source `re.S` search is ever narrowed back to per-line.
    """
    formatted_by_ruff = (
        "                        console.print(\n"
        "                            json.dumps(discovered_schema, indent=2)\n"
        "                        )\n"
    )
    pattern = r"console\.print\(\s*[\w.]*(?:json\.dumps|yaml\.dump)"

    assert not any(
        re.search(pattern, line) for line in formatted_by_ruff.splitlines()
    ), "precondition: the split call is invisible to a per-line search"
    assert re.search(pattern, formatted_by_ruff, re.S), (
        "the guard's pattern must match the multi-line call ruff format produces"
    )


@pytest.mark.unit
def test_the_console_would_in_fact_corrupt_a_payload():
    """
    Pin the premise, so the guard above is not cargo cult.

    If a future Rich stops wrapping, eating markup and highlighting, this fails
    and the rules above can be relaxed deliberately rather than by accident.

    The three behaviours were measured against Rich 14.1.0, which is named in each
    failure message: `pyproject.toml` only asks for `rich>=13.0.0`, so a Rich
    release that changed any of them would fail this test on an unrelated pull
    request, and whoever sees it needs to know which version the expectation came
    from. Failing on that is intended — the point is to force a deliberate look.
    """
    measured_against = "measured against rich 14.1.0; installed: " + version("rich")

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
    assert "\x1b[" in rendered, f"expected Rich to highlight ({measured_against})"
    assert "[in USD]" not in re.sub(r"\x1b\[[0-9;]*m", "", rendered), (
        f"expected Rich to consume '[in USD]' as a markup tag ({measured_against})"
    )

    buf = io.StringIO()
    # force_terminal=False models the piped case explicitly rather than relying on
    # detection: with FORCE_COLOR in the environment even a StringIO console styles.
    plain = Console(file=buf, width=80, force_terminal=False)
    plain.print(json.dumps({"long": "x" * 200}))
    assert "\x1b[" not in buf.getvalue(), (
        f"a non-terminal console emits no escapes ({measured_against})"
    )
    with pytest.raises(json.JSONDecodeError):
        # ...and yet the payload is still broken, by the 80-column fold alone.
        json.loads(buf.getvalue())

    # A stray closing tag does not mangle the payload, it kills the command.
    with pytest.raises(MarkupError):
        Console(file=io.StringIO(), width=80, force_terminal=False).print(
            json.dumps({"prompt": "see [/bold] here"})
        )
