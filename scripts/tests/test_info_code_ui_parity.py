# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Sections panel's ``info`` label must not claim a recovery that did not happen.

Only ``assessment_recovered_with_retries`` reports a repair; every other
``info``-severity ``ProcessingIssue`` code reports something **missing** —
``extraction_sparse`` ("possible silent extraction loss"),
``extraction_off_schema_fields`` (fields dropped before assessment),
``extraction_row_count_unverified`` (no OCR evidence to check a row count against)
and ``assessment_geometry_incomplete`` (scored fields with no bounding box). This
file owns the enumeration, so no count is written in prose. Rendering them all as
"Auto-recovered" told an operator the opposite of what happened, on the notes most
worth reading
([#1521](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1521)).

The UI now enumerates what IS a recovery, so a new backend ``info`` code defaults
to the neutral label rather than silently claiming a repair. That default is safe
but silent, which is what this test is for: it derives the backend's ``info``
universe and fails when a code belongs to neither classification, so adding one
forces the author to decide which label it should carry.

Same shape and same reason as ``test_failure_code_ui_parity.py``: the UI set is a
literal in a different language from the backend that produces the codes, so
nothing about adding a code there would make it appear here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UI_FILE = REPO / "src/ui/src/components/common/processing-issues-utils.ts"
BACKEND_ROOT = REPO / "lib/idp_common_pkg/idp_common"

#: Backend ``info`` codes that report something MISSING rather than repaired, and
#: so must not be labelled as a recovery. One entry per code, each naming what it
#: actually reports — the reason the label would be wrong is a property of the
#: individual code, not of the group.
ADVISORY_INFO_CODES = {
    "extraction_sparse": "few schema fields populated — possible silent extraction loss",
    "extraction_off_schema_fields": "fields outside the schema, dropped before assessment",
    "assessment_geometry_incomplete": "scored fields carrying no bounding box",
    "extraction_row_count_unverified": "no OCR evidence to check an extracted row count against",
}


#: ``ProcessingIssue(...)`` call sites whose ``severity`` this file cannot resolve,
#: as ``(path relative to the repo, the severity expression) -> (count, why it
#: cannot be ``info``)``. A site here contributes nothing to the derived universe,
#: so it is a hole in the closure the rest of this file asserts — and each entry
#: carries one site's worth of reason, because whether a site can emit ``info`` is
#: a property of that expression and not of the group.
#:
#: Completeness that rests on reading call sites by hand is not a property a test
#: holds, so the set and the per-site count are pinned instead:
#: :func:`test_no_new_unresolvable_severity_site_appears` turns red on a new site,
#: or another call at an existing one, rather than letting the universe shrink
#: quietly.
UNRESOLVED_SEVERITY_SITES = {
    ("lib/idp_common_pkg/idp_common/classification/service.py", "severities[code]"): (
        1,
        "the mapping it indexes declares only 'error' and 'warning'",
    ),
    ("lib/idp_common_pkg/idp_common/assessment/batching.py", "severity"): (
        2,
        'both locals are "error" if ... else "warning"',
    ),
    (
        "lib/idp_common_pkg/idp_common/extraction/service.py",
        "'error' if shortfall_action == 'fail' else 'warning'",
    ): (1, "a conditional over two literals, neither of them 'info'"),
    (
        "lib/idp_common_pkg/idp_common/extraction/service.py",
        "'error' if fail_action == 'reject' else 'warning'",
    ): (1, "a conditional over two literals, neither of them 'info'"),
    (
        "lib/idp_common_pkg/idp_common/dynamodb/service.py",
        "iss.get('severity', 'info')",
    ): (
        1,
        "deserialization, not declaration: it rebuilds a stored issue and takes "
        "its code from data too, so it introduces no code this gate could "
        "enumerate. It can produce 'info' — and that is harmless here precisely "
        "because the code it carries was declared at one of the literal sites.",
    ),
}


def _constant_bindings(tree: ast.Module) -> dict[str, str]:
    """``NAME = "literal"`` bindings at module level **and inside class bodies**.

    Class bodies matter and are the reason this is not the sibling's
    ``_module_assignments``: these codes are class attributes reached as
    ``self.NO_OCR_EVIDENCE_CODE``, so a reader that walked only module level
    resolved nothing for them.
    """
    bindings: dict[str, str] = {}

    def record(node: ast.Assign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if node.value is None or not isinstance(node.value, ast.Constant):
            return
        if not isinstance(node.value.value, str):
            return
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = node.value.value

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            record(node)
    return bindings


def _resolve_str(expr: ast.expr | None, bindings: dict[str, str]) -> str | None:
    """A string from a literal, a bare name, or an attribute like ``self.X``.

    ⚠️ Names and attributes must resolve, not only literals.
    ``extraction_row_count_unverified`` is passed as
    ``self.NO_OCR_EVIDENCE_CODE``, so a derivation that collected only
    ``ast.Constant`` values cannot see it — and a code absent from the universe is
    one every closure assertion below is silent about, while still reporting
    green. An incomplete derivation reads as coverage, which is worse than no
    gate.
    """
    if expr is None:
        return None
    if isinstance(expr, ast.Constant):
        return expr.value if isinstance(expr.value, str) else None
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id)
    if isinstance(expr, ast.Attribute):
        # self.X / cls.X / SomeClass.X -> look the attribute name up directly.
        return bindings.get(expr.attr)
    return None


def _literal_text(expr: ast.expr | None) -> str:
    """The literal parts of a message expression, f-strings included.

    Enough to ask whether a message talks about recovery; the interpolated values
    are irrelevant to that question.
    """
    if expr is None:
        return ""
    parts: list[str] = []
    for child in ast.walk(expr):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return " ".join(parts)


def _backend_info_issues() -> dict[str, tuple[Path, str]]:
    """Every ``info``-severity ``ProcessingIssue``: code -> (file, message text).

    Read from source via AST rather than by importing, because the constructors
    sit inside functions whose modules pull in heavy optional dependencies.
    """
    found: dict[str, tuple[Path, str]] = {}
    for path in BACKEND_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        bindings = _constant_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "ProcessingIssue":
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            severity = _resolve_str(kw.get("severity"), bindings)
            code = _resolve_str(kw.get("code"), bindings)
            if severity != "info" or not code:
                continue
            found.setdefault(code, (path, _literal_text(kw.get("message"))))
    return found


def _unresolved_severity_sites() -> dict[tuple[str, str], int]:
    """Every ``ProcessingIssue`` call whose ``severity`` does not resolve to a string.

    Keyed on the source text of the expression rather than a line number, so
    moving code does not churn the pin.
    """
    found: dict[tuple[str, str], int] = {}
    for path in BACKEND_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        bindings = _constant_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "ProcessingIssue":
                continue
            sev_expr = next(
                (k.value for k in node.keywords if k.arg == "severity"), None
            )
            if sev_expr is None:
                continue
            if _resolve_str(sev_expr, bindings) is not None:
                continue
            key = (str(path.relative_to(REPO)), ast.unparse(sev_expr))
            found[key] = found.get(key, 0) + 1
    return found


def _backend_info_codes() -> dict[str, Path]:
    return {c: v[0] for c, v in _backend_info_issues().items()}


def _ui_recovery_codes() -> set[str]:
    source = UI_FILE.read_text(encoding="utf-8")
    match = re.search(
        r"const\s+RECOVERY_CODES\s*=\s*new\s+Set\(\s*(\[[^\]]*\])\s*\)", source
    )
    assert match, (
        f"{UI_FILE.relative_to(REPO)} no longer declares "
        "`const RECOVERY_CODES = new Set([...])`. That set is what decides whether "
        "a section's info note renders as 'Auto-recovered' or the neutral 'Info', "
        "so this parity check cannot run without it."
    )
    return set(re.findall(r"'([^']+)'", match.group(1)))


@pytest.mark.unit
def test_every_backend_info_code_is_classified() -> None:
    """A new ``info`` code must be declared a recovery or an advisory, not left to
    the default. The default is correct more often than not, which is exactly why
    nothing would notice it being wrong."""
    backend = _backend_info_codes()
    classified = _ui_recovery_codes() | set(ADVISORY_INFO_CODES)
    unclassified = {
        c: str(p.relative_to(REPO)) for c, p in backend.items() if c not in classified
    }
    assert not unclassified, (
        "these backend info-severity codes are classified neither as a recovery "
        "(RECOVERY_CODES in the UI) nor as an advisory (ADVISORY_INFO_CODES here), "
        f"so the Sections panel labels them by default: {unclassified}. Decide "
        "which the code is: does it report something repaired, or something missing?"
    )


@pytest.mark.unit
def test_the_ui_claims_no_recovery_the_backend_does_not_produce() -> None:
    backend = set(_backend_info_codes())
    stale = _ui_recovery_codes() - backend
    assert not stale, (
        "the UI's RECOVERY_CODES lists codes no backend module emits at info "
        f"severity: {sorted(stale)}. A dead entry pre-approves whatever next takes "
        "that name as a recovery."
    )


@pytest.mark.unit
def test_no_advisory_code_is_also_listed_as_a_recovery() -> None:
    overlap = _ui_recovery_codes() & set(ADVISORY_INFO_CODES)
    assert not overlap, (
        f"these codes are listed as both a recovery and an advisory: {sorted(overlap)}"
    )


@pytest.mark.unit
def test_every_advisory_entry_still_exists_in_the_backend() -> None:
    """Staleness ratchet: an advisory entry for a code nobody emits is dead, and a
    dead entry silently pre-classifies whatever next takes that name."""
    backend = set(_backend_info_codes())
    gone = set(ADVISORY_INFO_CODES) - backend
    assert not gone, (
        f"ADVISORY_INFO_CODES names codes the backend no longer emits at info "
        f"severity: {sorted(gone)}. Delete them."
    )


@pytest.mark.unit
@pytest.mark.parametrize("code", sorted(ADVISORY_INFO_CODES))
@pytest.mark.unit
def test_each_advisory_code_reports_an_absence_not_a_repair(code: str) -> None:
    """Non-vacuity, read off the BACKEND rather than off this file.

    Reads the ``message=`` the backend passes for the code and rejects one that
    talks about recovery. That is the condition which would mean the code belongs
    in the UI's ``RECOVERY_CODES`` instead of here, so it is the only thing that
    can tell a correct classification from a stale one.

    Asserting against this file's own dict value cannot do that: the value is
    authored here, so it says whatever the author typed. The check has to read the
    side it is classifying.
    """
    issues = _backend_info_issues()
    assert code in issues, f"{code} is no longer emitted at info severity"
    _path, message = issues[code]
    assert message, f"{code} is emitted with no literal message text to check"
    assert not re.search(r"recover", message, re.IGNORECASE), (
        f"{code} is classified as an advisory, but the message the backend emits "
        f"for it talks about recovery: {message[:160]!r}. If it reports a repair "
        "it belongs in the UI's RECOVERY_CODES, not here."
    )


@pytest.mark.unit
def test_both_sides_are_derived_at_full_strength() -> None:
    """A floor, not a truthiness check.

    Non-emptiness is satisfied by a derivation that found 4 of the 5 codes, and
    the missing one would be invisible to every other assertion here — each of
    them quantifies over the derived universe, so a code outside it is not
    unclassified, it is unseen. The sibling gate pins a floor for the same reason.

    Raise these numbers when the tree gains codes; do not lower one without
    naming the code that stopped being emitted.
    """
    backend = _backend_info_codes()
    assert len(backend) >= 5, (
        f"derived only {len(backend)} backend info codes ({sorted(backend)}); the "
        "tree has at least 5, so the walk is missing some and every closure "
        "assertion here is weaker than it looks"
    )
    assert len(_ui_recovery_codes()) >= 1, "the UI lists no recovery codes"


@pytest.mark.unit
def test_no_new_unresolvable_severity_site_appears() -> None:
    """The closure above quantifies over what this file can SEE.

    A ``ProcessingIssue`` whose ``severity`` is computed — a subscript, a
    conditional, a local — resolves to None and is dropped, so it joins neither the
    classified nor the unclassified set. The floor does not catch that either: the
    five literal codes still satisfy it. Pinning the known sites means the first
    one that could carry ``info`` turns this red instead of silently narrowing
    what every other assertion is about.

    Fixing a failure here means one of two things: teach :func:`_resolve_str` the
    new expression shape, or add the site to ``UNRESOLVED_SEVERITY_SITES`` after
    checking it cannot emit ``info``.
    """
    found = _unresolved_severity_sites()
    expected = {k: v[0] for k, v in UNRESOLVED_SEVERITY_SITES.items()}
    assert found == expected, (
        "the set of ProcessingIssue call sites with an unresolvable severity has "
        f"changed.\n  found:    {dict(sorted(found.items()))}\n  expected: "
        f"{dict(sorted(expected.items()))}\nEach such site is "
        "invisible to this gate's closure checks."
    )
