# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prove `scripts/check_ui_string_debt.py` fails for each thing it claims to catch.

A ratchet nobody has watched fail is not a ratchet. The UI string-debt gate runs
`src/ui/eslint.i18n.config.js` (the counting rule) and compares the per-file counts
to `scripts/ui_string_debt.json`; everything protecting the baseline lives in that
script. Each test below drives one failure mode against a synthetic tree, with the
ESLint invocation replaced by a fake that returns the JSON ESLint would, and asserts
the message names it — including the directions a plain "does it still pass?" check
cannot distinguish: a listed file that *gained* a string, a listed file now at zero
that must be delisted, a counting configuration edited without re-recording, and
`--write` being asked to *add* a file, which would otherwise turn a red gate green
in one command.

Nothing here needs `node`, and nothing can touch the real `src/ui`. One test at the
end runs the real gate against the real tree when the UI's dependencies are
installed; in CI they always are, so there it never skips (#1390's rule).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Whether this is a CI run, which decides whether an absent Node toolchain is a
#: skip or a failure in the real-tree test. Same rule as ``IN_CI`` in
#: ``test_ui_test_discovery.py``.
IN_CI = bool(os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS"))

LINT_SCRIPT = (
    "eslint --no-config-lookup --config eslint.i18n.config.js --cache "
    "--cache-strategy content --cache-location ../../.ui-i18n-eslintcache "
    "-f json 'src/**/*.{ts,tsx}'"
)

CONFIG_TEXT = "export default [{ rules: { 'i18next/no-literal-string': 'error' } }];\n"

#: The counting plugin's pin in the synthetic manifest; part of the recorded hash.
PLUGIN_PIN = "6.1.5"

RULE = "i18next/no-literal-string"


def _load_module():
    """Load `scripts/check_ui_string_debt.py` by path (scripts/ is not a package).

    Registered in ``sys.modules`` before it executes: its dataclasses resolve their
    postponed annotations through ``sys.modules[__module__]``.
    """
    path = REPO_ROOT / "scripts" / "check_ui_string_debt.py"
    spec = importlib.util.spec_from_file_location("_check_ui_string_debt", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


# --------------------------------------------------------------------------- #
# The synthetic checkout and the fake ESLint
# --------------------------------------------------------------------------- #
def _tree(
    tmp_path: Path,
    *,
    sources: tuple[str, ...] = (),
    toolchain: bool = True,
    script: str | None = LINT_SCRIPT,
    config: str = CONFIG_TEXT,
    plugin_pin: str | None = PLUGIN_PIN,
):
    """A checkout: src/ui with package.json (the lint script and the counting plugin's pin),
    the counting config, the sources and, optionally, eslint."""
    layout = MODULE.Layout(tmp_path)
    layout.ui_root.mkdir(parents=True)
    scripts = {} if script is None else {"i18n:lint": script}
    manifest = {"scripts": scripts}
    if plugin_pin is not None:
        manifest["devDependencies"] = {"eslint-plugin-i18next": plugin_pin}
    layout.package_json.write_text(json.dumps(manifest), encoding="utf-8")
    (layout.ui_root / "eslint.i18n.config.js").write_text(config, encoding="utf-8")
    for rel in sources:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// synthetic\n", encoding="utf-8")
    layout.baseline.parent.mkdir(parents=True, exist_ok=True)
    if toolchain:
        layout.eslint.parent.mkdir(parents=True)
        layout.eslint.write_text("#!/bin/sh\n", encoding="utf-8")
    return layout


def _sha(layout, plugin_pin: str = PLUGIN_PIN) -> str:
    """The hash the gate records, computed independently: the config file's bytes, then
    the pinned counting plugin as ``<name>@<version>`` on a line of its own."""
    digest = hashlib.sha256((layout.ui_root / "eslint.i18n.config.js").read_bytes())
    digest.update(f"\neslint-plugin-i18next@{plugin_pin}".encode("utf-8"))
    return digest.hexdigest()


def _baseline(
    layout,
    debt: dict[str, int],
    *,
    sha: str | None = None,
    mark: dict | None | str = "auto",
    reasons: list[str] | None = None,
) -> Path:
    document = {
        "$comment": ["synthetic"],
        "generator": "python3 scripts/check_ui_string_debt.py --write",
        "eslintConfig": "src/ui/eslint.i18n.config.js",
        "configSha256": _sha(layout) if sha is None else sha,
        "newDebtReasons": reasons or [],
        "stringDebt": debt,
    }
    if mark == "auto":
        document["highWaterMark"] = {"files": len(debt), "findings": sum(debt.values())}
    elif mark is not None:
        document["highWaterMark"] = mark
    layout.baseline.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return layout.baseline


def _message(line: int, column: int, text: str, *, jsx: bool = False) -> dict:
    """One ESLint message for a string of ``text`` sitting on one line."""
    return {
        "ruleId": RULE,
        "severity": 2,
        "message": f"hardcoded user-facing string: {text}",
        "line": line,
        "column": column,
        "nodeType": "JSXText" if jsx else "Literal",
        "endLine": line,
        "endColumn": column + len(text),
    }


def _result(layout, rel: str, strings: list[str], *, jsx: bool = False) -> dict:
    """An ESLint result object for ``rel`` with one finding per string, each on its own line."""
    lines = []
    messages = []
    for index, text in enumerate(strings, start=1):
        literal = text if jsx else f"'{text}'"
        lines.append(f"  label: {literal},")
        messages.append(_message(index, 10, literal, jsx=jsx))
    return {
        "filePath": str(layout.repo_root / rel),
        "messages": messages,
        "suppressedMessages": [],
        "errorCount": len(messages),
        "warningCount": 0,
        "source": "\n".join(lines) + "\n",
    }


def _fake_run(
    monkeypatch,
    layout,
    findings: dict[str, list[str]] | None = None,
    *,
    extra_results: list[dict] | None = None,
    returncode: int | None = None,
    stderr: str = "",
    stdout: str | None = None,
) -> list[dict]:
    """Replace the module's subprocess runner; returns the calls it received.

    ``findings`` maps a repo-relative path to the strings ESLint found in it (JSX text
    when the string is wrapped in ``<>``). Exit code 1 when there are findings, 0
    otherwise, unless ``returncode`` forces one (2 = tool error).
    """
    calls: list[dict] = []
    results = []
    for rel, strings in (findings or {}).items():
        jsx = bool(strings) and strings[0].startswith("<")
        texts = [s.strip("<>") for s in strings] if jsx else strings
        results.append(_result(layout, rel, texts, jsx=jsx))
    results.extend(extra_results or [])
    code = returncode if returncode is not None else (1 if results else 0)

    def run(command, **kwargs):
        calls.append({"argv": list(command), **kwargs})
        return subprocess.CompletedProcess(
            command,
            code,
            stdout=json.dumps(results) if stdout is None else stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(MODULE, "run", run)
    return calls


def _check(layout) -> list[str]:
    report = MODULE.Report()
    MODULE.check(layout, report)
    return report.failures


def _only(failures) -> str:
    assert len(failures) == 1, f"expected exactly one failure, got {failures}"
    return failures[0]


# --------------------------------------------------------------------------- #
# The clean case, so every test below is measuring the mutation and not noise
# --------------------------------------------------------------------------- #
def test_a_consistent_baseline_passes(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/b.ts"))
    _baseline(layout, {"src/ui/src/a.tsx": 2})
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello", "World"]})
    assert _check(layout) == []


def test_the_measurement_replays_the_npm_script_with_eslint_by_path(
    tmp_path, monkeypatch
):
    """The arguments come from package.json; only the binary is swapped, and the cwd is src/ui."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {})
    calls = _fake_run(monkeypatch, layout, {})
    assert _check(layout) == []
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[0] == str(layout.eslint)
    assert argv[1:] == [
        "--no-config-lookup",
        "--config",
        "eslint.i18n.config.js",
        "--cache",
        "--cache-strategy",
        "content",
        "--cache-location",
        "../../.ui-i18n-eslintcache",
        "-f",
        "json",
        "src/**/*.{ts,tsx}",
    ]
    assert calls[0]["cwd"] == layout.ui_root
    assert "npx" not in " ".join(argv)


# --------------------------------------------------------------------------- #
# The ratchet: a listed file may not accumulate, and nothing unlisted may carry any
# --------------------------------------------------------------------------- #
def test_a_listed_file_that_gains_a_string_fails_naming_the_findings(
    tmp_path, monkeypatch
):
    """The point of recording counts rather than names — and the failure shows where."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello", "Brand new"]})
    message = _only(_check(layout))
    assert "src/ui/src/a.tsx carries 2" in message
    assert "GAINED 1" in message
    assert "src/ui/src/a.tsx:2:10  'Brand new'" in message
    assert (
        "src/ui/src/i18n/README.md" not in message
    )  # the pointer is printed once, by main


def test_an_unlisted_file_with_a_string_fails(tmp_path, monkeypatch):
    """A new file and a converted file both have an implicit baseline of zero."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/new.tsx"))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    _fake_run(
        monkeypatch,
        layout,
        {"src/ui/src/a.tsx": ["Hello"], "src/ui/src/new.tsx": ["<Hello world>"]},
    )
    message = _only(_check(layout))
    assert "src/ui/src/new.tsx is not in the baseline (implicit count 0)" in message
    assert "carries 1 hardcoded" in message
    assert "src/ui/src/new.tsx:1:10  'Hello world'" in message
    assert "do not add the file to the baseline" in message


def test_a_listed_file_now_at_zero_is_vacuous_and_must_be_deleted(
    tmp_path, monkeypatch
):
    """The ratchet turns one way: a paid-off file is held at zero, not at its old count."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 3})
    _fake_run(monkeypatch, layout, {})
    message = _only(_check(layout))
    assert "src/ui/src/a.tsx is listed with 3 finding(s) but now has none" in message
    assert "vacuous" in message
    assert "--write" in message


def test_a_listed_path_that_no_longer_exists_is_stale(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 1, "src/ui/src/gone.tsx": 2})
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    message = _only(_check(layout))
    assert "src/ui/src/gone.tsx is listed but no longer exists" in message
    assert "stale" in message


def test_a_shrunken_count_passes_and_is_noted(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 5})
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    report = MODULE.Report()
    MODULE.check(layout, report)
    assert report.failures == []
    assert any("5 → 1" in note for note in report.notes), report.notes


# --------------------------------------------------------------------------- #
# The counting configuration is part of the measurement
# --------------------------------------------------------------------------- #
def test_an_edited_counting_configuration_fails_until_re_recorded(
    tmp_path, monkeypatch
):
    """A widened exclude list lowers every count silently; the hash makes it a visible --write."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    (layout.ui_root / "eslint.i18n.config.js").write_text(
        CONFIG_TEXT + "// one more exclude\n", encoding="utf-8"
    )
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    message = _only(_check(layout))
    assert "src/ui/eslint.i18n.config.js" in message
    assert "is not the counting configuration" in message
    assert "--write" in message


def test_a_bumped_counting_plugin_fails_until_re_recorded(tmp_path, monkeypatch):
    """The rule's behaviour (what it reaches, what it skips) is the plugin's, so its pinned
    version is part of the configuration the counts were measured with: a bump re-records
    the baseline as visibly as an edited exclude list does, and the message names it."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    manifest = json.loads(layout.package_json.read_text(encoding="utf-8"))
    manifest["devDependencies"]["eslint-plugin-i18next"] = "6.2.0"
    layout.package_json.write_text(json.dumps(manifest), encoding="utf-8")
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    message = _only(_check(layout))
    assert "eslint-plugin-i18next@6.2.0" in message
    assert "is not the counting configuration" in message
    assert "a bumped eslint-plugin-i18next" in message
    assert _sha(layout, "6.2.0") != _sha(layout), "the pin is part of the hash"


def test_a_manifest_without_the_counting_plugin_pin_is_refused(tmp_path, monkeypatch):
    """A count whose rule is unpinned is a count nobody can reproduce."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",), plugin_pin=None)
    layout.baseline.write_text("{}", encoding="utf-8")
    _fake_run(monkeypatch, layout, {})
    with pytest.raises(ValueError) as excinfo:
        _check(layout)
    assert "does not pin eslint-plugin-i18next" in str(excinfo.value)


def test_a_baseline_naming_another_configuration_fails(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    path = _baseline(layout, {"src/ui/src/a.tsx": 1})
    document = json.loads(path.read_text())
    document["eslintConfig"] = "src/ui/eslint.config.js"
    path.write_text(json.dumps(document))
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    assert "is not the counting configuration" in _only(_check(layout))


def test_a_hand_grown_list_without_a_matching_mark_fails(tmp_path, monkeypatch):
    """Growth has to show up as a diff on two numbered lines, not 200 quiet ones."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/b.tsx"))
    _baseline(
        layout,
        {"src/ui/src/a.tsx": 1, "src/ui/src/b.tsx": 1},
        mark={"files": 1, "findings": 1},
    )
    _fake_run(
        monkeypatch, layout, {"src/ui/src/a.tsx": ["x"], "src/ui/src/b.tsx": ["y"]}
    )
    message = _only(_check(layout))
    assert "highWaterMark" in message
    assert "{'files': 1, 'findings': 1}" in message
    assert "{'files': 2, 'findings': 2}" in message


def test_a_missing_mark_fails(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 1}, mark=None)
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["x"]})
    assert "highWaterMark" in _only(_check(layout))


# --------------------------------------------------------------------------- #
# The measurement must stay honest
# --------------------------------------------------------------------------- #
def test_an_eslint_tool_error_is_a_hard_error_with_its_stderr(tmp_path, monkeypatch):
    """Exit 1 is findings and expected; exit 2 is ESLint failing, and nothing was measured."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {})
    _fake_run(
        monkeypatch,
        layout,
        {},
        returncode=2,
        stderr="Oops! Something went wrong! TypeError: Cannot read config",
    )
    with pytest.raises(RuntimeError) as excinfo:
        _check(layout)
    assert "exited 2" in str(excinfo.value)
    assert "Cannot read config" in str(excinfo.value)


def test_a_message_that_is_not_a_finding_is_a_hard_error(tmp_path, monkeypatch):
    """A parse error or an unknown rule would otherwise make the file count as clean."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {})
    broken = {
        "filePath": str(layout.repo_root / "src/ui/src/a.tsx"),
        "messages": [
            {
                "ruleId": None,
                "fatal": True,
                "severity": 2,
                "message": "Parsing error: Unexpected token",
                "line": 3,
                "column": 1,
            }
        ],
        "source": "",
    }
    _fake_run(monkeypatch, layout, {}, extra_results=[broken], returncode=1)
    with pytest.raises(RuntimeError) as excinfo:
        _check(layout)
    assert "not i18next/no-literal-string findings" in str(excinfo.value)
    assert "src/ui/src/a.tsx:3: Parsing error" in str(excinfo.value)


def test_output_that_is_not_json_is_a_hard_error(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {})
    _fake_run(monkeypatch, layout, {}, returncode=1, stdout="src/a.tsx\n  1:1 error")
    with pytest.raises(RuntimeError) as excinfo:
        _check(layout)
    assert "not the JSON" in str(excinfo.value)


def test_a_package_json_without_the_script_is_a_hard_error(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",), script=None)
    _baseline(layout, {})
    _fake_run(monkeypatch, layout, {})
    with pytest.raises(ValueError) as excinfo:
        _check(layout)
    assert "has no 'i18n:lint' script" in str(excinfo.value)


@pytest.mark.parametrize(
    "script",
    [
        "eslint --config eslint.i18n.config.js -f json 'src/**/*.{ts,tsx}'",
        "eslint --no-config-lookup --config eslint.i18n.config.js 'src/**/*.{ts,tsx}'",
        "eslint --no-config-lookup --config eslint.i18n.config.js -f stylish 'src/**/*.ts'",
        "eslint --no-config-lookup -f json 'src/**/*.{ts,tsx}'",
        "npx eslint --no-config-lookup --config eslint.i18n.config.js -f json src",
    ],
)
def test_a_script_of_another_shape_is_refused_rather_than_misread(
    tmp_path, monkeypatch, script
):
    """Without --no-config-lookup the main config leaks in; without --config there is
    nothing to pin; without JSON there is nothing to parse; npx can reach for the network."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",), script=script)
    _baseline(layout, {})
    _fake_run(monkeypatch, layout, {})
    with pytest.raises(ValueError) as excinfo:
        _check(layout)
    assert "this gate expects" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# --write: the list itself may only shrink. Without this, --write launders a new
# hardcoded string into a permanent exemption: the gate reddens on an unlisted file
# and says "do not add the file to the baseline", and --write adds it anyway.
# --------------------------------------------------------------------------- #
def test_write_refuses_to_add_a_newly_dirty_file(tmp_path, monkeypatch):
    """The exact laundering path: one command turning a red gate green."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/new.tsx"))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    _fake_run(
        monkeypatch,
        layout,
        {"src/ui/src/a.tsx": ["Hello"], "src/ui/src/new.tsx": ["One", "Two", "Three"]},
    )
    with pytest.raises(SystemExit) as excinfo:
        MODULE.write(layout)
    message = str(excinfo.value)
    assert "refusing to write" in message
    assert "src/ui/src/new.tsx" in message
    assert "files 1 → 2" in message
    assert "findings 1 → 4" in message
    assert "--allow-new-debt" in message
    assert json.loads(layout.baseline.read_text())["stringDebt"] == {
        "src/ui/src/a.tsx": 1
    }


def test_write_refuses_to_raise_a_count(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello", "More"]})
    with pytest.raises(SystemExit) as excinfo:
        MODULE.write(layout)
    assert "src/ui/src/a.tsx: 1 → 2" in str(excinfo.value)


def test_write_allows_growth_only_with_a_recorded_reason(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/new.tsx"))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    _fake_run(
        monkeypatch,
        layout,
        {"src/ui/src/a.tsx": ["Hello"], "src/ui/src/new.tsx": ["One"]},
    )
    MODULE.write(layout, "a merge brought in a screen written before the ratchet")
    document = json.loads(layout.baseline.read_text())
    assert document["newDebtReasons"] == [
        "a merge brought in a screen written before the ratchet"
    ]
    assert document["stringDebt"] == {"src/ui/src/a.tsx": 1, "src/ui/src/new.tsx": 1}
    assert document["highWaterMark"] == {"files": 2, "findings": 2}


def test_write_shrinks_deletes_vacuous_entries_and_refreshes_the_hash(
    tmp_path, monkeypatch
):
    """Paying debt down must tighten the limit, and a widened config is recorded by --write."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/b.tsx"))
    _baseline(
        layout,
        {"src/ui/src/a.tsx": 5, "src/ui/src/b.tsx": 2},
        sha="0" * 64,
        mark={"files": 9, "findings": 40},
        reasons=["kept"],
    )
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    MODULE.write(layout)
    document = json.loads(layout.baseline.read_text())
    assert document["stringDebt"] == {"src/ui/src/a.tsx": 1}
    assert document["highWaterMark"] == {"files": 1, "findings": 1}
    assert document["configSha256"] == _sha(layout)
    assert document["eslintConfig"] == "src/ui/eslint.i18n.config.js"
    assert document["newDebtReasons"] == ["kept"]
    assert list(document) == [
        "$comment",
        "generator",
        "eslintConfig",
        "configSha256",
        "highWaterMark",
        "newDebtReasons",
        "stringDebt",
    ]
    assert layout.baseline.read_text().endswith("}\n")


def test_write_bootstraps_a_missing_baseline(tmp_path, monkeypatch, capsys):
    """The first baseline has nothing to grow from, so it is written as measured."""
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello", "World"]})
    MODULE.write(layout)
    document = json.loads(layout.baseline.read_text())
    assert document["stringDebt"] == {"src/ui/src/a.tsx": 2}
    assert document["newDebtReasons"] == []
    assert document["$comment"]
    assert "(first baseline)" in capsys.readouterr().out


def test_write_leaves_a_passing_tree_passing(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    MODULE.write(layout)
    assert _check(layout) == []


# --------------------------------------------------------------------------- #
# --explain and --summary
# --------------------------------------------------------------------------- #
def test_explain_prints_the_pinned_count_the_current_count_and_every_finding(
    tmp_path, monkeypatch, capsys
):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {"src/ui/src/a.tsx": 3})
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello", "More"]})
    assert MODULE.explain(layout, "src/ui/src/a.tsx") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "src/ui/src/a.tsx"
    assert "baseline count : 3" in out
    assert "current count  : 2" in out
    assert "src/ui/src/a.tsx:1:10  'Hello'" in out
    assert "src/ui/src/a.tsx:2:10  'More'" in out


def test_explain_accepts_a_src_ui_relative_path_and_an_unlisted_file(
    tmp_path, monkeypatch, capsys
):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {})
    _fake_run(monkeypatch, layout, {})
    assert MODULE.explain(layout, "src/a.tsx") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "src/ui/src/a.tsx"
    assert "baseline count : 0 (not listed)" in out
    assert "current count  : 0" in out


def test_explain_says_when_the_path_does_not_exist(tmp_path, monkeypatch, capsys):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {})
    _fake_run(monkeypatch, layout, {})
    assert MODULE.explain(layout, "src/ui/src/nowhere.tsx") == 2
    assert "does not exist in the checkout" in capsys.readouterr().out


def test_summary_prints_totals_and_the_top_offenders(tmp_path, monkeypatch, capsys):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/b.tsx"))
    _baseline(layout, {"src/ui/src/a.tsx": 3, "src/ui/src/b.tsx": 1})
    _fake_run(
        monkeypatch,
        layout,
        {"src/ui/src/a.tsx": ["One", "Two", "Three"], "src/ui/src/b.tsx": ["x"]},
    )
    MODULE.summarise(layout)
    out = capsys.readouterr().out
    assert "files with findings    : 2" in out
    assert "findings total         : 4" in out
    assert "baseline findings      : 4" in out
    lines = [line for line in out.splitlines() if "src/ui/src/" in line]
    assert lines[0].split() == ["3", "/", "3", "src/ui/src/a.tsx"]
    assert lines[1].split() == ["1", "/", "1", "src/ui/src/b.tsx"]


# --------------------------------------------------------------------------- #
# main(): the skip is a skip, never a pass; in CI it is a failure
# --------------------------------------------------------------------------- #
def _without_ci(monkeypatch) -> None:
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)


def test_an_absent_toolchain_skips_off_ci_and_says_so(tmp_path, monkeypatch, capsys):
    layout = _tree(tmp_path, toolchain=False)
    _baseline(layout, {})
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    calls = _fake_run(monkeypatch, layout, {})
    assert MODULE.main([]) == 0
    out = capsys.readouterr().out
    assert "⏭️  check-ui-string-debt SKIPPED (not a pass: run make ui-lint first)" in out
    assert "✅" not in out
    assert calls == []


@pytest.mark.parametrize("variable", ["CI", "GITHUB_ACTIONS", "GITLAB_CI"])
def test_an_absent_toolchain_fails_in_ci(tmp_path, monkeypatch, capsys, variable):
    layout = _tree(tmp_path, toolchain=False)
    _baseline(layout, {})
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    monkeypatch.setenv(variable, "true")
    _fake_run(monkeypatch, layout, {})
    assert MODULE.main([]) == 1
    out = capsys.readouterr().out
    assert "❌ check-ui-string-debt" in out
    assert "src/ui/node_modules/.bin/eslint is missing" in out
    assert "SKIPPED" not in out


def test_main_reports_a_pass_and_a_failure_with_the_right_exit_codes(
    tmp_path, monkeypatch, capsys
):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/new.tsx"))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)

    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    assert MODULE.main(["--verbose"]) == 0
    out = capsys.readouterr().out
    assert "✅ check-ui-string-debt: 1 file(s) still carry exactly the 1" in out
    assert "matches configSha256" in out

    _fake_run(
        monkeypatch,
        layout,
        {"src/ui/src/a.tsx": ["Hello"], "src/ui/src/new.tsx": ["<Hello world>"]},
    )
    assert MODULE.main([]) == 1
    out = capsys.readouterr().out
    assert "❌ check-ui-string-debt: 1 problem(s)" in out
    assert "src/ui/src/new.tsx:1:10  'Hello world'" in out
    assert (
        out.rstrip()
        .splitlines()[-1]
        .startswith(
            "How to declare a user-facing string, and how to pay a file down: "
            "src/ui/src/i18n/README.md."
        )
    )


def test_main_turns_a_tool_error_into_exit_1(tmp_path, monkeypatch, capsys):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx",))
    _baseline(layout, {})
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    _fake_run(monkeypatch, layout, {}, returncode=2, stderr="boom")
    assert MODULE.main([]) == 1
    out = capsys.readouterr().out
    assert "❌ check-ui-string-debt" in out
    assert "boom" in out


def test_the_gate_writes_nothing_whether_it_passes_or_fails(tmp_path, monkeypatch):
    layout = _tree(tmp_path, sources=("src/ui/src/a.tsx", "src/ui/src/new.tsx"))
    _baseline(layout, {"src/ui/src/a.tsx": 1})
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in sorted(tmp_path.rglob("*"))
        if p.is_file()
    }
    _fake_run(monkeypatch, layout, {"src/ui/src/a.tsx": ["Hello"]})
    assert MODULE.main([]) == 0
    _fake_run(
        monkeypatch,
        layout,
        {"src/ui/src/a.tsx": ["Hello"], "src/ui/src/new.tsx": ["x"]},
    )
    assert MODULE.main([]) == 1
    after = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in sorted(tmp_path.rglob("*"))
        if p.is_file()
    }
    assert after == before


# --------------------------------------------------------------------------- #
# The markers the exemption registry reads, and the real tree
# --------------------------------------------------------------------------- #
def test_the_docstring_carries_the_ratchet_markers_and_declares_the_gaps():
    """`gate_exemptions.json` names this script as count-pinned evidence; the
    registry test looks for "pinned", and the non-vacuity wording for "vacuous"."""
    doc = MODULE.__doc__ or ""
    assert "pinned" in doc
    assert "vacuous" in doc
    assert "ALL_CAPS" in doc or "ALL-CAPS" in doc
    assert "new Error" in doc


def test_the_real_repository_satisfies_the_gate(capsys):
    """What `make check-ui-string-debt` runs, on the committed tree.

    Needs the UI's npm dependencies. Off CI an unbuilt UI is ordinary and the test
    skips; in CI every job running this suite installs them, so their absence is a
    broken job and fails (see `_require_node` in test_ui_test_discovery.py).
    """
    layout = MODULE.Layout(REPO_ROOT)
    if not layout.eslint.is_file():
        reason = (
            f"{layout.relative(layout.eslint)} is not installed, so the real count "
            "cannot run (run `npm ci` in src/ui)."
        )
        if IN_CI:
            pytest.fail(
                f"{reason} In CI this is a job-configuration failure, not a reason "
                "to skip."
            )
        pytest.skip(reason)
    status = MODULE.main([])
    out = capsys.readouterr().out
    assert status == 0, out
    assert "✅ check-ui-string-debt" in out
