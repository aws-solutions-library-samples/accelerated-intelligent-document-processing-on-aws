# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No tracked file may name a `classification.granularity` or `.strategy` value
the model rejects.

⚠️ **`granularity` is the one classification setting that raises rather than
falling back.** `validate_granularity` refuses an unrecognised value by design —
silently treating a typo as `page` is how a configuration asking for packet
behaviour runs page behaviour for a month — and `ClassificationService.__init__`
constructs `IDPConfig` from whatever dict it is handed, so a bad value is a
`ValidationError` on the first cell of a notebook rather than a warning in a log.

The migration converts a *legacy* config correctly (`classificationMethod:
textbasedHolisticClassification` → `granularity: packet`). The one edit that
defeats it is **renaming the key and keeping the old value**: `_has_legacy_markers`
does not fire, the long legacy string goes straight to the validator, and the
notebook stops loading. That is the shape this gate exists to catch, and it is a
plausible one every time these settings are swept — a key rename is mechanical and
the values are not.

⚠️ **Nothing else in the tree can see it.** `**/*.ipynb` is a scope exclusion for
both ruff and basedpyright (`CLAUDE.md`), and no suite loads a notebook's config
dict — so for notebooks this is the only gate, which is why the subject is derived
from `git ls-files` rather than from a path list.

The subject is **notebooks and YAML/JSON configuration**, and that is an inclusion
rather than an exemption: a `.py` or `.ts` file naming one of these settings is
already read by ruff, basedpyright and the suites, and a *test* naming a rejected
value is doing its job — `test_packet_granularity.py` passes `granularity:
holistic` precisely to assert the refusal. Scanning those would mean carving the
deliberate cases back out one line at a time, which is the shape of exemption this
repository treats as a defect in its own right.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

VALID_GRANULARITY = {"page", "packet"}
VALID_STRATEGY = {"combined", "split_then_classify"}

# `"granularity": "x"` (JSON and a notebook's `\"`-escaped source) and
# `granularity: x` (YAML). An empty value is legitimate: the validator accepts it
# and resolves it to `page`.
#
# ⚠️ The value must be **quoted, or a bare scalar running to end of line**. A
# looser rule that accepted any identifier after the colon read the prose
# "granularity: one call per page" in this repo's own default config as a setting
# of `one`; a gate whose findings are mostly sentences is one a reader learns to
# wave through.
_SETTING = re.compile(
    r"""\\?["']?\b(granularity|strategy)\b\\?["']?    # the key, possibly \"-escaped
        \s*:\s*                                       # YAML / JSON colon
        (?:
            \\?["']([^"'\\]*)\\?["']                  # a quoted value
          | ([A-Za-z_]\w*)\s*(?:\#|$)                 # or a bare YAML scalar
        )
    """,
    re.VERBOSE,
)

_VALID = {"granularity": VALID_GRANULARITY, "strategy": VALID_STRATEGY}


def _is_flat_classification_config(path: Path) -> bool:
    """Whether ``path`` is a pre-0.9 flat configuration the migration converts.

    Such a file is read through ``v08_to_v09``, which maps the v0.8 spellings of
    ``strategy`` (``V08_STRATEGY_VALUES``) to the v0.9 values, so a v0.8 spelling
    in it is the format's own spelling rather than a stale one. A nested file is
    written in the current layout, so the same spelling there is still a finding:
    the reader would convert it, but a file in this tree that needs converting is
    one nobody updated.
    """
    from idp_common.config.migrations.v08_to_v09 import _has_flat_keys

    if path.suffix not in {".yaml", ".yml", ".json"}:
        return False
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unparseable means "not a config"
        return False
    return isinstance(doc, dict) and _has_flat_keys(doc)


# ⚠️ **No skip list, deliberately.** A `(merge|splitting|retry)_strategy` key in
# some other namespace looks like it would need excusing, and it does not: `\b`
# cannot match between `_` and `strategy`, because `_` is a word character. One
# was written here anyway and would have shielded nothing while being an
# unregistered exemption surface — which `CLAUDE.md` treats as a defect in its
# own right, since a reason nobody can test is a reason nobody can audit. If a
# real collision ever appears, bound the fix to the line rather than the word, and
# register it in `scripts/tests/gate_exemptions.json`.


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    keep = {".ipynb", ".yaml", ".yml", ".json"}
    return [
        REPO / name for name in out.split("\0") if name and Path(name).suffix in keep
    ]


def _findings() -> list[tuple[str, int, str, str]]:
    from idp_common.config.migrations.v08_to_v09 import V08_STRATEGY_VALUES

    bad: list[tuple[str, int, str, str]] = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "granularity" not in text and "strategy" not in text:
            continue
        flat: bool | None = None
        for lineno, line in enumerate(text.splitlines(), start=1):
            for key, quoted, bare in _SETTING.findall(line):
                value = quoted or bare
                if value == "" or value in _VALID[key]:
                    continue
                if key == "strategy" and value in V08_STRATEGY_VALUES:
                    if flat is None:
                        flat = _is_flat_classification_config(path)
                    if flat:
                        continue
                bad.append((str(path.relative_to(REPO)), lineno, key, value))
    return bad


@pytest.mark.unit
def test_no_tracked_file_names_a_rejected_classification_value():
    bad = _findings()
    assert not bad, "values the config model rejects:\n" + "\n".join(
        f"  {p}:{n} — {k} = {v!r}" for p, n, k, v in bad
    )


@pytest.mark.unit
def test_the_scan_would_catch_the_defect_it_was_written_for():
    """A non-vacuity check on the pattern rather than on the tree: the tree is
    clean by construction once the gate passes, so what has to be pinned is that
    the regex actually fires on the shape that got through — the renamed key with
    the legacy value, in a notebook's JSON-escaped source line."""
    notebook_line = (
        '    "        \\"granularity\\": \\"multimodalPageLevelClassification\\",\\n",'
    )
    found = _SETTING.findall(notebook_line)
    assert found == [("granularity", "multimodalPageLevelClassification", "")]
    assert found[0][1] not in VALID_GRANULARITY

    for spelling in (
        "granularity: textbasedHolisticClassification",
        '"granularity": "textbasedHolisticClassification"',
        '    "    \\"granularity\\": \\"textbasedHolisticClassification\\",\\n",',
    ):
        hits = _SETTING.findall(spelling)
        assert hits, spelling
        assert all((q or b) not in VALID_GRANULARITY for _, q, b in hits), spelling

    # It does not fire on a correct value, on the empty value the validator
    # accepts, or on prose that happens to put a word after the key.
    for ok in (
        "granularity: page",
        '"granularity": "packet"',
        'granularity: ""',
        "# granularity: one call per page, or one per packet",
    ):
        assert all(
            (q or b) in VALID_GRANULARITY or (q or b) == ""
            for _, q, b in _SETTING.findall(ok)
        ), ok


@pytest.mark.unit
def test_a_v08_strategy_spelling_is_accepted_only_in_a_flat_file(tmp_path):
    """The v0.8 spelling is accepted where the migration reads it, and nowhere
    else: the same line in a nested file is a stale spelling."""
    flat = tmp_path / "flat.yaml"
    flat.write_text("classification:\n  model: m\n  strategy: single_pass\n")
    nested = tmp_path / "nested.yaml"
    nested.write_text("classification:\n  combined: {}\n  strategy: single_pass\n")
    assert _is_flat_classification_config(flat)
    assert not _is_flat_classification_config(nested)


@pytest.mark.unit
def test_every_notebook_is_still_valid_json():
    """A notebook is a JSON document, and the sweeps that touch these settings are
    text edits (`sed`, a scripted replace). A truncated or unbalanced notebook is
    unopenable, and no other gate in this repository reads these files, so a
    broken one would be found by a user rather than by CI."""
    broken = []
    for path in _tracked_text_files():
        if path.suffix != ".ipynb":
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            broken.append(f"  {path.relative_to(REPO)} — {exc}")
    assert not broken, "notebooks that no longer parse as JSON:\n" + "\n".join(broken)
