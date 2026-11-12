# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each modelled entry's mitigations say what tests them, or say that nothing does.

Every entry block in ``security/threat-modeling/feature-threats/*.md`` carries one
``| **Evidence** |`` row after its mitigations. The row is a ``; ``-separated list of
citations, each in backticks, optionally followed by ``UNVERIFIED: <what is not
covered>``:

* ``make <target>`` -- a target defined in the ``Makefile`` or ``make/*.mk``;
* a test -- a pytest node (``path::Class::test``) or vitest node (``path::title``)
  that would be collected in a directory CI runs, or a test file itself;
* a gate -- a tracked ``.py`` or ``.sh`` script under ``scripts/``.

A row with no ``UNVERIFIED`` claims that its citations cover every mitigation the
entry lists. A row with ``UNVERIFIED`` says that at least one does not, and its text
says which, or that the citations have not yet been matched to each mitigation.

What this gate checks: every entry has exactly one such row, the row has the shape
above, every citation resolves, and the number of rows marked ``UNVERIFIED`` equals
:data:`PINNED_UNVERIFIED_MITIGATIONS`, so the count can only fall by a deliberate
edit. It does **not** check that a citation tests the mitigation it is listed beside;
that is a reviewer's reading, the same way the Mitigations text itself is.
"""

from __future__ import annotations

import functools
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FEATURE_DOCS = REPO_ROOT / "security" / "threat-modeling" / "feature-threats"
BUILD_SCRIPT = (
    REPO_ROOT / "security" / "threat-modeling" / "scripts" / "build_threat_model.py"
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_agent_capabilities as capabilities  # noqa: E402

#: Rows whose Evidence cell is marked UNVERIFIED. Lower it when a row's mitigations
#: are all cited; it must never be raised to admit a new unverified row silently.
PINNED_UNVERIFIED_MITIGATIONS = 95

_CITATION = re.compile(r"`([^`]+)`")
_UNVERIFIED = "UNVERIFIED:"
_HEADING = re.compile(r"^### [A-Z]+\.T\d+:", re.M)


def _build_module():
    spec = importlib.util.spec_from_file_location("build_threat_model", BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=None)
def entries() -> Dict[str, Tuple[str, List[Tuple[str, str]]]]:
    """Entry id -> (document, rows), parsed with the export builder's own patterns."""
    build = _build_module()
    out: Dict[str, Tuple[str, List[Tuple[str, str]]]] = {}
    for doc in sorted(FEATURE_DOCS.glob("*.md")):
        for match in build.BLOCK_RE.finditer(doc.read_text(encoding="utf-8")):
            rows = [
                (k.strip(), v.strip())
                for k, v in build.ROW_RE.findall(match.group("table"))
            ]
            if any(k == "Threat ID" for k, _ in rows):
                out[match.group("id")] = (doc.name, rows)
    return out


@functools.lru_cache(maxsize=None)
def _make_targets() -> Set[str]:
    sources = [REPO_ROOT / "Makefile", *sorted((REPO_ROOT / "make").glob("*.mk"))]
    text = "\n".join(p.read_text(encoding="utf-8") for p in sources)
    return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", text, re.M))


@functools.lru_cache(maxsize=None)
def _tracked() -> Set[str]:
    return set(
        subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )


def citation_resolves(citation: str) -> bool:
    if citation.startswith("make "):
        return citation[len("make ") :] in _make_targets()
    if "::" in citation:
        return capabilities.node_collects(citation)
    if citation not in _tracked():
        return False
    name = Path(citation).name
    if name.startswith("test_") and name.endswith(".py"):
        return capabilities._run_by_ci(citation)
    if re.search(r"\.test\.tsx?$", name):
        return citation.startswith("src/ui/")
    return citation.startswith("scripts/") and name.endswith((".py", ".sh"))


def parse_evidence(cell: str) -> Tuple[List[str], str]:
    """(citations, unverified text). Raises ValueError on any other shape."""
    head, marker, unverified = cell.partition(_UNVERIFIED)
    if marker and not unverified.strip():
        raise ValueError("UNVERIFIED: needs a statement of what is not covered")
    parts = [p.strip() for p in head.split(";") if p.strip()]
    citations = []
    for part in parts:
        match = _CITATION.fullmatch(part)
        if not match:
            raise ValueError(f"not a backticked citation: {part!r}")
        citations.append(match.group(1))
    if not citations and not marker:
        raise ValueError("cites nothing and is not marked UNVERIFIED")
    return citations, unverified.strip()


def _evidence(entry_id: str) -> List[str]:
    return [v for k, v in entries()[entry_id][1] if k == "Evidence"]


@pytest.mark.unit
def test_every_entry_heading_is_parsed() -> None:
    headings = sum(
        len(_HEADING.findall(p.read_text(encoding="utf-8")))
        for p in FEATURE_DOCS.glob("*.md")
    )
    assert entries(), "no entry blocks parsed"
    assert len(entries()) == headings, (
        f"{headings} entry headings but {len(entries())} entry tables parsed; a "
        "heading whose table the export builder cannot read is outside this gate"
    )


@pytest.mark.unit
@pytest.mark.parametrize("entry_id", sorted(entries()))
def test_each_entry_has_one_well_formed_evidence_row(entry_id: str) -> None:
    doc, rows = entries()[entry_id]
    keys = [k for k, _ in rows]
    assert "Mitigations" in keys, f"{entry_id} ({doc}) has no Mitigations row"
    evidence = _evidence(entry_id)
    assert len(evidence) == 1, (
        f"{entry_id} ({doc}) has {len(evidence)} Evidence rows; add one after its "
        "mitigations citing a test, gate or `make` target, or reading "
        "`UNVERIFIED: <what is not covered>`"
    )
    try:
        citations, _ = parse_evidence(evidence[0])
    except ValueError as exc:
        pytest.fail(f"{entry_id} ({doc}): {exc}")
    unresolved = [c for c in citations if not citation_resolves(c)]
    assert not unresolved, (
        f"{entry_id} ({doc}) cites what does not resolve to a make target, a "
        f"collected test or a gate script: {unresolved}"
    )


@pytest.mark.unit
def test_the_unverified_count_is_pinned() -> None:
    marked = sorted(t for t in entries() if any(_UNVERIFIED in e for e in _evidence(t)))
    assert len(marked) == PINNED_UNVERIFIED_MITIGATIONS, (
        f"{len(marked)} entries are marked UNVERIFIED and the pin is "
        f"{PINNED_UNVERIFIED_MITIGATIONS}. Lower the pin when a row's mitigations are "
        "all cited; a rise means a new entry or a removed citation, and is a "
        "deliberate edit."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "cell, ok",
    [
        ("`make api-test`", True),
        ("`make api-test`; UNVERIFIED: logging is not covered", True),
        ("UNVERIFIED: no listed mitigation cites a test", True),
        ("", False),
        ("UNVERIFIED:", False),
        ("make api-test", False),
        ("`make api-test`, `make lint`", False),
    ],
)
def test_the_row_shape_is_enforced(cell: str, ok: bool) -> None:
    if ok:
        parse_evidence(cell)
    else:
        with pytest.raises(ValueError):
            parse_evidence(cell)


@pytest.mark.unit
@pytest.mark.parametrize(
    "citation, ok",
    [
        ("make api-test-static", True),
        ("make no-such-target-here", False),
        ("scripts/sdlc/scan_api_rbac.py", True),
        ("scripts/api_rbac_expectations.yaml", False),
        (
            "scripts/tests/test_threat_model_evidence.py::test_the_unverified_count_is_pinned",
            True,
        ),
        ("scripts/tests/test_threat_model_evidence.py::test_no_such_test", False),
    ],
)
def test_citations_are_resolved_against_the_tree(citation: str, ok: bool) -> None:
    assert citation_resolves(citation) is ok
