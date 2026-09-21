# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every document stating how many operations hold each API policy states it right.

``scripts/api_rbac_expectations.yaml`` is the source of truth for the per-operation
policy, and six documents restate its distribution in prose so a reader does not have
to parse the file. Those restatements go stale the moment an operation moves, and
until this module existed nothing compared them to anything:
``test_well_architected_doc.py`` derives the same numbers but asserts them against
``docs/well-architected.md`` alone, and the threat model's own currency gate
(``build_threat_model.py --check``) compares prose to the generated threat export,
which carries no RBAC policy data at all.

The gap was not theoretical. When the group floor last moved,
``feature-threats/rbac-authentication.md`` was updated in one paragraph and not in
another, so a single file gave two different distributions — and the stale half was
the statement the change had falsified. The per-group breakdown beside it sums to 118
either way, so even a careful reader re-adding the numbers would have seen nothing
wrong.

What this checks, and what it does not
--------------------------------------
Each entry in ``SHAPES`` is a **shape** rather than a sentence — "<n> ... declared
ANY", "<n> require any assigned group", a policy table row — so rewording the prose
around a count does not silently drop it from coverage the way a verbatim pattern
would. Every shape must fire somewhere (``test_every_shape_is_live``), or it has
quietly stopped reading anything.

It cannot promise that a count no shape matches is caught. ``test_no_unread_policy_count``
narrows that residual to the one spelling this corpus actually uses — a **bolded**
number immediately before a policy verb — because that is how the stale sentence was
written, and a bolded count no shape reads is therefore a failure rather than a
silence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_EXPECTATIONS = _REPO / "scripts" / "api_rbac_expectations.yaml"

# Every document that restates the distribution. `docs/well-architected.md` is
# deliberately absent: test_well_architected_doc.py already derives these numbers
# against it, with phrase templates fitted to that page's wording, and two gates
# reading one page in two ways is how they come to disagree about what is required.
SCANNED_DOCS = (
    "docs/rbac.md",
    "docs/migration-appsync-to-rest.md",
    ".claude/skills/api-rbac-test.md",
    "security/threat-modeling/feature-threats/rbac-authentication.md",
    "security/threat-modeling/architecture/system-overview.md",
)

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_NUM = r"(\d{1,3}|" + "|".join(NUMBER_WORDS) + r")"
# Markdown emphasis and backticks sit between the number and the words around it, so
# every shape tolerates them rather than matching only the unadorned spelling.
_M = r"[\s*`_]*"


def _as_int(token: str) -> int:
    token = token.strip().lower()
    return int(token) if token.isdigit() else NUMBER_WORDS[token]


_BLOCKQUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)


def _flat(text: str) -> str:
    """One long line, with blockquote markers removed.

    Two of the counts this module reads sit in a ``>`` blockquote and wrap across
    lines, so the words either side of the number are separated by ``"\\n> "``. A
    pattern that tolerates whitespace between them still does not match that, and the
    first version of this module missed exactly the sentence it was written for
    because of it. Prose shapes therefore read this view; table shapes, which need
    ``$`` to find the last cell of a row, read the raw text.
    """
    return re.sub(r"\s+", " ", _BLOCKQUOTE.sub("", text))


def _table_row(token: str) -> re.Pattern[str]:
    """A policy table row whose LAST cell is the count.

    Anchored at end-of-line rather than counting columns, because the two tables
    that carry these numbers have different widths — `docs/rbac.md` names the policy
    in its own cell, `system-overview.md` parenthesises it inside a prose cell.
    """
    return re.compile(token + r"[^\n]*\|" + _M + _NUM + _M + r"\|\s*$", re.MULTILINE)


# Which view of the document a shape reads. `LINES` shapes need real line structure.
FLAT, LINES = "flat", "lines"

# (measure name, view, compiled shape). The capture group is the count.
#
# Every gap between the number and its anchor forbids a digit, so a shape cannot
# bridge from one count to another number's anchor — "108 operations require a group,
# 18 of them via `ANY_GROUP`" has to read 18 for `any_group`, not 108.
SHAPES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # "8 of the 118 declared operations are declared `ANY`", "8 of the 118
    # operations are `ANY`", "The remaining 8 are declared `groups: ANY`"
    (
        "any",
        FLAT,
        re.compile(
            _NUM
            + r"\s+of\s+the\s+\d+\s+(?:declared\s+)?operations\s+"
            + r"(?:are\s+)?(?:declared\s+)?"
            + _M
            + r"`(?:groups:\s*)?ANY`(?!_)",
            re.IGNORECASE,
        ),
    ),
    (
        "any",
        FLAT,
        re.compile(
            r"remaining\s+"
            + _NUM
            + r"\s+are\s+declared\s+"
            + _M
            + r"`(?:groups:\s*)?ANY`(?!_)",
            re.IGNORECASE,
        ),
    ),
    # "8 require only authentication"
    (
        "any",
        FLAT,
        re.compile(_NUM + _M + r"require[sd]?" + _M + r"only authentication", re.I),
    ),
    # "group membership for those 8"
    ("any", FLAT, re.compile(r"group membership for those" + _M + _NUM, re.IGNORECASE)),
    # "| `ANY` | authentication only; ... | 8 |"
    ("any", LINES, _table_row(r"`ANY`(?!_)")),
    # "18 require any assigned group", "18 of those accept any assigned group"
    (
        "any_group",
        FLAT,
        re.compile(
            _NUM
            + r"[^.|\d\n]{0,40}?"
            + r"(?:require|accept)[sd]?"
            + _M
            + r"any"
            + _M
            + r"assigned group",
            re.IGNORECASE,
        ),
    ),
    # "18 are declared **`ANY_GROUP`**", "18 of them via `ANY_GROUP`". The number has
    # to be the subject: "and 3 mutations are declared `ANY_GROUP`" is a legitimate
    # sub-count of the same set, not a restatement of its size, so `operations` and
    # `of them/those` are the only nouns allowed between them.
    (
        "any_group",
        FLAT,
        re.compile(
            _NUM
            + r"(?:\s+operations?)?(?:\s+of\s+(?:them|those))?"
            + _M
            + r"(?:are|is|via)"
            + _M
            + r"(?:declared)?"
            + _M
            + r"`ANY_GROUP`",
            re.IGNORECASE,
        ),
    ),
    # "| `ANY_GROUP` | any group the stack creates ... | 18 |"
    ("any_group", LINES, _table_row(r"`ANY_GROUP`")),
    # "those eighteen API operations now refuse a caller in no group"
    (
        "any_group",
        FLAT,
        re.compile(
            r"those" + _M + _NUM + r"\s+API\s+operations" + _M + r"now refuse", re.I
        ),
    ),
    # "a group added there joins those 18 without an edit per operation"
    ("any_group", FLAT, re.compile(r"joins those" + _M + _NUM, re.IGNORECASE)),
    # "108 require a group", "108 operations require a group"
    (
        "group_restricted",
        FLAT,
        re.compile(
            _NUM + r"(?:\s+operations)?" + _M + r"require" + _M + r"a group",
            re.IGNORECASE,
        ),
    ),
    # "| a group list, e.g. `[Admin, Author]` | one of those groups | 90 |"
    ("explicit_list", LINES, _table_row(r"a group list")),
    # "90 name a subset of roles"
    (
        "explicit_list",
        FLAT,
        re.compile(_NUM + _M + r"name" + _M + r"a subset of roles", re.I),
    ),
    # "2 are IAM-only", "| `IAM_ONLY` | rejects every Cognito caller ... | 2 |"
    ("iam_only", FLAT, re.compile(_NUM + _M + r"(?:are|is)" + _M + r"IAM-only", re.I)),
    ("iam_only", LINES, _table_row(r"`IAM_ONLY`")),
    # "the 118 declared operations", "It covers 118 operations"
    (
        "total",
        FLAT,
        re.compile(
            r"(?:covers|the)" + _M + _NUM + _M + r"(?:declared\s+)?operations",
            re.IGNORECASE,
        ),
    ),
    # "Across all 118: 21 require `Admin`; ..." — the per-group breakdown's preamble,
    # where the noun is left implicit, so the shape above does not reach it.
    ("total", FLAT, re.compile(r"across all" + _M + _NUM + _M + r":", re.IGNORECASE)),
)


def _measures() -> dict[str, int]:
    """The distribution, derived from the expectations file."""
    ops = yaml.safe_load(_EXPECTATIONS.read_text(encoding="utf-8"))["operations"]
    policies = [entry["groups"] for entry in ops.values()]
    iam_only = [p for p in policies if p == "IAM_ONLY"]
    any_auth = [p for p in policies if p == "ANY"]
    any_group = [p for p in policies if p == "ANY_GROUP"]
    return {
        "total": len(ops),
        "iam_only": len(iam_only),
        "any": len(any_auth),
        "any_group": len(any_group),
        "group_restricted": len(ops) - len(iam_only) - len(any_auth),
        "explicit_list": len(ops) - len(iam_only) - len(any_auth) - len(any_group),
    }


def _findings() -> list[tuple[str, str, int, int]]:
    """(doc, measure, stated, expected) for every count a shape reads."""
    measures = _measures()
    out: list[tuple[str, str, int, int]] = []
    for rel in SCANNED_DOCS:
        raw = (_REPO / rel).read_text(encoding="utf-8")
        views = {FLAT: _flat(raw), LINES: raw}
        for measure, view, shape in SHAPES:
            for match in shape.finditer(views[view]):
                out.append((rel, measure, _as_int(match.group(1)), measures[measure]))
    return out


@pytest.mark.parametrize("doc", SCANNED_DOCS)
def test_every_policy_count_matches_the_expectations_file(doc: str) -> None:
    wrong = [f for f in _findings() if f[0] == doc and f[2] != f[3]]
    assert not wrong, (
        f"{doc} states a policy count that disagrees with "
        "scripts/api_rbac_expectations.yaml:\n"
        + "\n".join(
            f"  {m}: page says {got}, file says {want}" for _, m, got, want in wrong
        )
        + "\nThe file is the source of truth. Fix the prose, and check whether the "
        "same number appears elsewhere in the document — a page correcting one "
        "paragraph and not another is the defect this gate exists to catch."
    )


def test_the_corpus_agrees_with_itself() -> None:
    """Two documents may not state different values for the same measure.

    Implied by the check above, and asserted separately because it is the property a
    reader actually relies on, and because it reports the contradiction as a
    contradiction rather than as two independent staleness failures.
    """
    seen: dict[str, dict[int, set[str]]] = {}
    for doc, measure, stated, _ in _findings():
        seen.setdefault(measure, {}).setdefault(stated, set()).add(doc)
    conflicts = {m: v for m, v in seen.items() if len(v) > 1}
    assert not conflicts, f"documents disagree about a policy count: {conflicts}"


@pytest.mark.parametrize("measure", sorted({m for m, _, _ in SHAPES}))
def test_every_shape_is_live(measure: str) -> None:
    """A shape that matches nothing has stopped being a check.

    Without this, rewording the corpus past every pattern for one measure would leave
    the suite green while nothing read that measure anywhere.
    """
    hits = [f for f in _findings() if f[1] == measure]
    assert hits, (
        f"no document states the {measure!r} count in any shape this module reads. "
        "Either the prose was reworded past every pattern — add a shape for the new "
        "wording — or the statement was dropped, in which case say so deliberately "
        "by removing the shape."
    )


def test_no_unread_policy_count() -> None:
    """A bolded count before a policy verb must be read by some shape.

    This is the narrow closure guard, and it is narrow on purpose: a wide one would
    have to treat every digit near the word "group" as a policy count, which in this
    corpus means issue numbers, line numbers and Cognito group names. The stale
    sentence that motivated this module was written as "**11 require any assigned
    group** ... and **15 require only authentication**", so a bolded number followed
    by require/accept/declare is exactly the shape that must not go unread.
    """
    bolded = re.compile(
        r"\*\*"
        + _NUM
        + r"\s+(?:of\s+(?:the\s+|those\s+)?\S+\s+)?"
        + r"(?:operations?\s+)?(?:are\s+|is\s+)?(?:require|accept|declared)",
        re.IGNORECASE,
    )
    read = {(doc, stated) for doc, _, stated, _ in _findings()}
    unread = [
        (rel, _as_int(m.group(1)), m.group(0))
        for rel in SCANNED_DOCS
        for m in bolded.finditer(_flat((_REPO / rel).read_text(encoding="utf-8")))
        if (rel, _as_int(m.group(1))) not in read
    ]
    assert not unread, (
        "a bolded policy count is not read by any shape in this module, so nothing "
        "would notice it going stale:\n"
        + "\n".join(f"  {rel}: {snippet!r} (={n})" for rel, n, snippet in unread)
        + "\nAdd a shape that reads it."
    )
