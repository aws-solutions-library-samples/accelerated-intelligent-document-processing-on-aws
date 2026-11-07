# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Prove `scripts/check_ui_i18n_catalog.py` fails for each thing it claims to catch.

The gate keeps the committed English UI message catalog (`src/ui/src/i18n/en.json`)
equal to what `formatjs extract` produces from the UI's source, and compiles every
translation catalog the way the UI build does. Each test below drives one outcome
against a synthetic tree, with the two node invocations (the extraction and
`check-catalog.mjs`) replaced by a fake that records what it was asked to run, so
nothing here needs `node` and nothing can touch the real `src/ui`. One test at the
end runs the real gate against the real tree when the UI's dependencies are
installed; in CI they always are, so there it never skips (#1390's rule).
"""

from __future__ import annotations

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

EXTRACT_SCRIPT = (
    "formatjs extract 'src/**/*.{ts,tsx}' --ignore 'src/**/*.test.{ts,tsx}' "
    "--id-interpolation-pattern '[sha512:contenthash:base64:6]' --throws "
    "--out-file src/i18n/en.json"
)

EN_JSON = (
    json.dumps(
        {
            "i18n.switcher.label": {
                "defaultMessage": "Language",
                "description": "Tooltip of the language menu",
            }
        },
        indent=2,
    )
    + "\n"
)

DRIFTED_EN_JSON = EN_JSON.replace("Language", "Languages")


def _load_module():
    """Load `scripts/check_ui_i18n_catalog.py` by path (scripts/ is not a package).

    Registered in ``sys.modules`` before it executes: its dataclasses resolve their
    postponed annotations through ``sys.modules[__module__]``.
    """
    path = REPO_ROOT / "scripts" / "check_ui_i18n_catalog.py"
    spec = importlib.util.spec_from_file_location("_check_ui_i18n_catalog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _tree(
    tmp_path: Path,
    *,
    en: str | None = EN_JSON,
    catalogs: dict[str, str] | None = None,
    toolchain: bool = True,
    script: str | None = EXTRACT_SCRIPT,
):
    """A synthetic checkout: src/ui with package.json, the catalogs and, optionally, the toolchain."""
    layout = MODULE.Layout(tmp_path)
    layout.catalog_dir.mkdir(parents=True)
    scripts = {} if script is None else {"i18n:extract": script}
    layout.package_json.write_text(json.dumps({"scripts": scripts}), encoding="utf-8")
    if en is not None:
        layout.en_catalog.write_text(en, encoding="utf-8")
    for name, content in (catalogs or {}).items():
        (layout.catalog_dir / name).write_text(content, encoding="utf-8")
    layout.check_catalog.parent.mkdir(parents=True)
    layout.check_catalog.write_text("// stand-in\n", encoding="utf-8")
    if toolchain:
        layout.formatjs.parent.mkdir(parents=True)
        layout.formatjs.write_text("#!/bin/sh\n", encoding="utf-8")
    return layout


#: What `formatjs` 6.16.36 prints on a platform it ships no native binary for (an Intel
#: Mac, say): the wrapper is installed, so the toolchain check passes, and this is the
#: first thing that fails. Verbatim from `loadNative()` in its `bin/formatjs`.
NO_NATIVE_BINARY = (
    "Error: Native @formatjs/cli-lib binding is unavailable.\n"
    "@formatjs/cli-native-darwin-x64: Cannot find module '@formatjs/cli-native-darwin-x64'"
)


def _fake_run(
    monkeypatch,
    *,
    extracted: str = EN_JSON,
    extract_rc: int = 0,
    extract_stderr: str = 'Error: [FormatJS CLI] Duplicate message id: "i18n.switcher.label"',
    broken: dict[str, str] | None = None,
    node_missing: bool = False,
) -> list[list[str]]:
    """Replace the module's subprocess runner; returns the list of commands it was given.

    The extraction fake writes ``extracted`` to the ``--out-file`` the gate chose (or
    fails with ``extract_rc`` and ``extract_stderr``); the catalog fake fails for every
    locale in ``broken`` with that locale's text as its output, or raises the
    ``FileNotFoundError`` a missing ``node`` raises when ``node_missing`` is set.
    """
    calls: list[list[str]] = []
    broken = broken or {}

    def run(command, **kwargs):
        calls.append(list(command))
        if command[0].endswith("formatjs"):
            if extract_rc == 0:
                Path(command[command.index("--out-file") + 1]).write_text(
                    extracted, encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                command, extract_rc, stdout="", stderr=extract_stderr
            )
        if command[0] == "node":
            if node_missing:
                raise FileNotFoundError(2, "No such file or directory", "node")
            locale = command[2]
            if locale in broken:
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr=broken[locale]
                )
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{locale}.json: 1 message(s) compile", stderr=""
            )
        raise AssertionError(f"unexpected command {command!r}")

    monkeypatch.setattr(MODULE, "run", run)
    return calls


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _check(layout) -> list[str]:
    report = MODULE.Report()
    MODULE.check(layout, report)
    return report.failures


# --------------------------------------------------------------------------- #
# The clean case, so every test below is measuring its mutation and not noise
# --------------------------------------------------------------------------- #


def test_a_current_catalog_and_compiling_translations_pass(tmp_path, monkeypatch):
    layout = _tree(tmp_path, catalogs={"fr-CA.json": "{}"})
    calls = _fake_run(monkeypatch)
    assert _check(layout) == []
    assert [c[0].endswith("formatjs") for c in calls] == [True, False]
    assert calls[1] == ["node", str(layout.check_catalog), "fr-CA"]


def test_the_extraction_replays_the_npm_script_with_the_output_redirected(
    tmp_path, monkeypatch
):
    """The arguments come from package.json, so the gate cannot drift from the writer."""
    layout = _tree(tmp_path)
    calls = _fake_run(monkeypatch)
    _check(layout)
    (command,) = calls
    assert command[0] == str(layout.formatjs)
    assert command[1:7] == [
        "extract",
        "src/**/*.{ts,tsx}",
        "--ignore",
        "src/**/*.test.{ts,tsx}",
        "--id-interpolation-pattern",
        "[sha512:contenthash:base64:6]",
    ]
    assert "--throws" in command
    out_file = Path(command[command.index("--out-file") + 1])
    assert out_file.name == "en.json"
    assert not out_file.is_relative_to(tmp_path), (
        "the gate extracted into the tree it is checking; the comparison target "
        "must be a temporary file"
    )


# --------------------------------------------------------------------------- #
# Each failure mode, and that the message names the file and the fix
# --------------------------------------------------------------------------- #


def test_a_drifted_extraction_fails_naming_the_file_and_the_fix(tmp_path, monkeypatch):
    layout = _tree(tmp_path)
    _fake_run(monkeypatch, extracted=DRIFTED_EN_JSON)
    (failure,) = _check(layout)
    assert failure.startswith("src/ui/src/i18n/en.json is stale")
    assert '-    "defaultMessage": "Language",' in failure
    assert '+    "defaultMessage": "Languages",' in failure
    assert failure.rstrip().endswith(
        "Run 'make ui-i18n-extract' and commit src/ui/src/i18n/en.json"
    )


def test_a_missing_english_catalog_fails(tmp_path, monkeypatch):
    layout = _tree(tmp_path, en=None)
    calls = _fake_run(monkeypatch)
    (failure,) = _check(layout)
    assert "src/ui/src/i18n/en.json does not exist" in failure
    assert "make ui-i18n-extract" in failure
    assert calls == [], "nothing to diff against, so nothing should have run"


def test_a_failing_extraction_is_reported_with_its_output(tmp_path, monkeypatch):
    """Duplicate ids with different text make `formatjs extract --throws` exit 1."""
    layout = _tree(tmp_path)
    _fake_run(monkeypatch, extract_rc=1)
    (failure,) = _check(layout)
    assert "`formatjs extract` failed (exit 1)" in failure
    assert "Duplicate message id" in failure


def test_a_translation_catalog_the_build_would_reject_fails(tmp_path, monkeypatch):
    layout = _tree(tmp_path, catalogs={"fr-CA.json": "{}", "en-XA.json": "{}"})
    problem = (
        "fr-CA.json: i18n.switcher.label: the translation does not parse as ICU: "
        "EXPECT_ARGUMENT_CLOSING_BRACE at line 1, column 7\n"
        "fr-CA.json: 1 problem(s); a build with this catalog would fail"
    )
    calls = _fake_run(monkeypatch, broken={"fr-CA": problem})
    (failure,) = _check(layout)
    assert failure.startswith("src/ui/src/i18n/fr-CA.json would fail the UI build")
    assert "EXPECT_ARGUMENT_CLOSING_BRACE" in failure
    # Every translation is checked, in a stable order, and en.json is never "compiled".
    assert [c[2] for c in calls if c[0] == "node"] == ["en-XA", "fr-CA"]


def test_only_locale_named_json_files_are_catalogs(tmp_path, monkeypatch):
    layout = _tree(
        tmp_path,
        catalogs={
            "fr-CA.json": "{}",
            "zh-Hant-TW.json": "{}",
            "glossary.fr-CA.csv": "term,translation",
            "notes.json": "{}",
            "README.md": "# not a catalog",
        },
    )
    assert MODULE.catalog_locales(layout) == ["en", "fr-CA", "zh-Hant-TW"]
    calls = _fake_run(monkeypatch)
    assert _check(layout) == []
    assert [c[2] for c in calls if c[0] == "node"] == ["fr-CA", "zh-Hant-TW"]


def test_a_package_json_without_the_script_is_a_hard_error(tmp_path, monkeypatch):
    layout = _tree(tmp_path, script=None)
    _fake_run(monkeypatch)
    with pytest.raises(ValueError, match="has no 'i18n:extract' script"):
        _check(layout)


def test_a_script_of_another_shape_is_refused_rather_than_misread(
    tmp_path, monkeypatch
):
    layout = _tree(tmp_path, script="eslint src --max-warnings 0")
    _fake_run(monkeypatch)
    with pytest.raises(ValueError, match="expects `formatjs extract"):
        _check(layout)


# --------------------------------------------------------------------------- #
# Check-only: the tree is the same after the gate as before it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("extracted", [EN_JSON, DRIFTED_EN_JSON])
def test_the_gate_writes_nothing_whether_it_passes_or_fails(
    tmp_path, monkeypatch, extracted
):
    """Unlike codegen-check, a stale catalog is reported, never repaired in place."""
    layout = _tree(tmp_path, catalogs={"fr-CA.json": "{}"})
    _fake_run(monkeypatch, extracted=extracted)
    before = _snapshot(tmp_path)
    _check(layout)
    assert _snapshot(tmp_path) == before


# --------------------------------------------------------------------------- #
# The toolchain guard: skip off CI, fail in CI, never a pass
# --------------------------------------------------------------------------- #


def _without_ci(monkeypatch) -> None:
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI"):
        monkeypatch.delenv(name, raising=False)


def test_an_absent_toolchain_skips_off_ci_and_says_so(tmp_path, monkeypatch, capsys):
    _tree(tmp_path, toolchain=False)
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    calls = _fake_run(monkeypatch)
    assert MODULE.main([]) == 0
    out = capsys.readouterr().out
    assert (
        "⏭️  check-ui-i18n-catalog SKIPPED (not a pass: run make ui-lint first)" in out
    )
    assert "✅" not in out
    assert calls == []


@pytest.mark.parametrize("variable", ["CI", "GITHUB_ACTIONS", "GITLAB_CI"])
def test_an_absent_toolchain_fails_in_ci(tmp_path, monkeypatch, capsys, variable):
    _tree(tmp_path, toolchain=False)
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    monkeypatch.setenv(variable, "true")
    _fake_run(monkeypatch)
    assert MODULE.main([]) == 1
    out = capsys.readouterr().out
    assert "❌ check-ui-i18n-catalog" in out
    assert "src/ui/node_modules/.bin/formatjs is missing" in out
    assert "SKIPPED" not in out


def test_a_toolchain_with_no_native_binary_skips_off_ci_and_names_the_platform(
    tmp_path, monkeypatch, capsys
):
    """The wrapper is installed, so the toolchain check passes; the extraction then fails
    with the CLI's own text. That is neither a stale catalog nor something re-extracting
    fixes, so off CI it is the same visible skip as an absent toolchain."""
    _tree(tmp_path)
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    _fake_run(monkeypatch, extract_rc=1, extract_stderr=NO_NATIVE_BINARY)
    assert MODULE.main([]) == 0
    out = capsys.readouterr().out
    assert (
        "⏭️  check-ui-i18n-catalog SKIPPED (not a pass: this platform cannot run it)"
        in out
    )
    assert "ships no native binary for" in out
    assert "@formatjs/cli-native-darwin-x64" in out, "the CLI's own output is shown"
    assert "✅" not in out
    assert "is stale" not in out and "formatjs extract` failed" not in out, (
        "a platform that cannot run the tool must not be reported as a catalog problem"
    )


@pytest.mark.parametrize("variable", ["CI", "GITHUB_ACTIONS", "GITLAB_CI"])
def test_a_toolchain_with_no_native_binary_fails_in_ci(
    tmp_path, monkeypatch, capsys, variable
):
    _tree(tmp_path)
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    monkeypatch.setenv(variable, "true")
    _fake_run(monkeypatch, extract_rc=1, extract_stderr=NO_NATIVE_BINARY)
    assert MODULE.main([]) == 1
    out = capsys.readouterr().out
    assert "❌ check-ui-i18n-catalog" in out
    assert "no native binary for this runner" in out
    assert "SKIPPED" not in out


def test_any_other_extraction_failure_still_fails_with_its_output(
    tmp_path, monkeypatch, capsys
):
    """The skip is keyed on the CLI's native-binding text and nothing wider: a CLI that
    fails for any other reason, or words a future binding failure differently, is a
    hard failure carrying its output, never a pass."""
    _tree(tmp_path)
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)
    _fake_run(
        monkeypatch, extract_rc=1, extract_stderr="Error: something else entirely"
    )
    assert MODULE.main([]) == 1
    out = capsys.readouterr().out
    assert "`formatjs extract` failed (exit 1)" in out
    assert "something else entirely" in out
    assert "SKIPPED" not in out


def test_an_absent_node_is_reported_rather_than_raised(tmp_path, monkeypatch):
    """`node` runs by name from PATH; without it the catalog compile is a named failure,
    not a traceback, and the English check before it still reports normally."""
    layout = _tree(tmp_path, catalogs={"fr-CA.json": "{}"})
    _fake_run(monkeypatch, node_missing=True)
    (failure,) = _check(layout)
    assert failure.startswith("node could not be started")
    assert "src/ui/src/i18n/fr-CA.json was not compiled" in failure


def test_main_reports_a_pass_and_a_failure_with_the_right_exit_codes(
    tmp_path, monkeypatch, capsys
):
    _tree(tmp_path, catalogs={"fr-CA.json": "{}"})
    monkeypatch.setattr(MODULE, "REPO_ROOT", tmp_path)
    _without_ci(monkeypatch)

    _fake_run(monkeypatch)
    assert MODULE.main(["--verbose"]) == 0
    out = capsys.readouterr().out
    assert "✅ check-ui-i18n-catalog" in out
    assert "1 translation catalog(s) compile" in out
    assert "fr-CA.json: 1 message(s) compile" in out

    _fake_run(monkeypatch, extracted=DRIFTED_EN_JSON)
    assert MODULE.main([]) == 1
    out = capsys.readouterr().out
    assert "❌ check-ui-i18n-catalog: 1 problem(s)" in out
    assert "Run 'make ui-i18n-extract'" in out


# --------------------------------------------------------------------------- #
# The real tree
# --------------------------------------------------------------------------- #


def test_the_real_repository_satisfies_the_gate(capsys):
    """What `make check-ui-i18n-catalog` runs, on the committed tree.

    Needs the UI's npm dependencies. Off CI an unbuilt UI is ordinary and the test
    skips; in CI every job running this suite installs them, so their absence is a
    broken job and fails (see `_require_node` in test_ui_test_discovery.py).
    """
    layout = MODULE.Layout(REPO_ROOT)
    if not layout.formatjs.is_file():
        reason = (
            f"{layout.relative(layout.formatjs)} is not installed, so the real "
            "extraction cannot run (run `npm ci` in src/ui)."
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
    assert "✅ check-ui-i18n-catalog" in out
