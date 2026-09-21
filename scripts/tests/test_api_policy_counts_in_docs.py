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

Nine measures are derived: the four policy classes (``total``, ``iam_only``, ``any``,
``any_group``), the two sums over them (``group_restricted``, ``explicit_list``), and
the three per-operation narrowings the same sentences restate (``scope_checked``,
``scope_filtered``, ``ownership``). The narrowings are in scope because two documents
restate them in the *same sentence* as the policy counts, from the same flags in the
same file, and a change that moves one — which this one does, adding
``getMyProfile``'s ``ownership`` — has to update both pages by hand. There is no
reason to read the number either side of a comma and not the one between them.

What this checks, and what it does not
--------------------------------------
Each entry in ``SHAPES`` is a **shape** rather than a sentence — "<n> ... declared
ANY", "<n> require any assigned group", a policy table row — so rewording the prose
around a count does not silently drop it from coverage the way a verbatim pattern
would. Five ratchets keep that honest:

* **Universe closure** (``test_no_document_states_counts_unread``): the membership of
  ``SCANNED_DOCS`` is *derived*, not trusted. Every tracked ``.md`` file is searched
  with the same shapes, and one that states a count while being neither scanned nor
  in ``EXCLUDED_DOCS`` fails. A hardcoded document list is how a sixth page restating
  the distribution wrongly would pass unnoticed.
* **Per-shape non-vacuity** (``test_every_shape_is_live``): parametrised over the
  **shapes**, not over the measures, so one dead pattern is a failure even when a
  sibling shape covers the same measure.
* **Per-document count pinning** (``test_document_still_states_its_counts``): each
  scanned document has a floor on how many counts are read out of it. Without it the
  per-document comparison passes *vacuously* on a document whose every count has been
  reworded past every shape.
* **Corpus agreement** (``test_the_corpus_agrees_with_itself``): two documents may not
  state different values for one measure, which is the shape the original defect took.
* **The exclusion's premise, per measure**
  (``test_the_sibling_gate_still_asserts_each_measure_it_is_trusted_for``): the single
  entry in ``EXCLUDED_DOCS`` is scoped to the measures another gate provably *asserts*,
  and each one is checked individually, by parsing that gate.
  ``test_the_sibling_test_is_collected_and_not_skipped`` covers the other half — an
  assertion that exists in source is not one that runs, and a module pytest does not
  collect or a ``skip`` mark would leave every expression in place.

It still cannot promise that a count no shape matches is caught inside a scanned
document, and the ratchets bound rather than remove that.
``test_no_unread_policy_count`` narrows it further for the one spelling this corpus
uses — a **bolded** number immediately before a policy verb — because that is how the
stale sentence was written.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_EXPECTATIONS = _REPO / "scripts" / "api_rbac_expectations.yaml"

# Every document that restates the distribution. Membership is asserted against a
# derived universe by `test_no_document_states_counts_unread`, so this tuple cannot
# quietly fall behind the tree.
SCANNED_DOCS = (
    "docs/rbac.md",
    "docs/migration-appsync-to-rest.md",
    ".claude/skills/api-rbac-test.md",
    "security/threat-modeling/feature-threats/rbac-authentication.md",
    "security/threat-modeling/architecture/system-overview.md",
    # Only the `[Unreleased]` section is read — see `_document_text`.
    "CHANGELOG.md",
)

# The one test in `test_well_architected_doc.py` the exclusion below defers to.
SIBLING_TEST = "test_api_authorization_counts_match_the_expectations_file"

# The measures that exclusion defers, each mapped to the expression it must appear as in
# the **first argument of an `_assert_count_phrase` call inside `SIBLING_TEST`**. This
# set is the bound on the exclusion, so a member narrows this gate and an absence widens
# it.
#
# Deriving a number is not asserting it, and the difference is the whole point of
# bounding this exclusion: `explicit_list` is *derived* in that module — as
# `group_restricted - len(any_group)` — but only inside an f-string in a failure
# message, so nothing compares it to the page. Naming it here would defer a measure to
# a check that is not there. The three narrowing measures (`scope_checked`,
# `scope_filtered`, `ownership`) are absent because that module does not read them at
# all; a page-scope exclusion would have covered them too.
SIBLING_GATE_MEASURES: dict[str, str] = {
    "total": "len(ops)",
    "iam_only": "len(iam_only)",
    "any": "len(any_auth)",
    "any_group": "len(any_group)",
    "group_restricted": "group_restricted",
}

# The one document excluded from this gate, and the measures it is excluded *for*. Every
# other measure on that page is still compared here. Registered in
# `scripts/tests/gate_exemptions.json`.
EXCLUDED_DOCS: dict[str, tuple[frozenset[str], str]] = {
    "docs/well-architected.md": (
        frozenset(SIBLING_GATE_MEASURES),
        "test_well_architected_doc.py::test_api_authorization_counts_match_the_"
        "expectations_file derives these five numbers from the same YAML and ASSERTS "
        "each of them against this page, with phrase templates fitted to its wording. "
        "Two gates reading one page in two ways is how they come to disagree about "
        "what is required, so one gate owns those five for this page. It asserts no "
        "others — `explicit_list` it derives but never compares — which is why the "
        "exclusion stops at these five.",
    ),
}

# How many counts each scanned document currently yields. A floor, not an equality:
# adding a count is fine, losing one to a rewording is the failure this pins.
MIN_FINDINGS_PER_DOC = {
    "docs/rbac.md": 6,
    "docs/migration-appsync-to-rest.md": 4,
    ".claude/skills/api-rbac-test.md": 3,
    "security/threat-modeling/feature-threats/rbac-authentication.md": 12,
    "security/threat-modeling/architecture/system-overview.md": 6,
    # The `[Unreleased]` section states the change, not the distribution, so it reads
    # zero today. Registered as a floor of 0 rather than omitted, so the document is
    # visibly in the scan rather than absent from both structures.
    "CHANGELOG.md": 0,
}


def _tracked_markdown() -> list[str]:
    """Every tracked ``.md`` path, symlinks excluded.

    Symlinks are skipped because `.cline/skills/*.md` are symlinks to the `.claude`
    originals — the same bytes under a second name, which would double every finding
    and report a failure twice.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        rel
        for rel in out.split("\0")
        if rel and (_REPO / rel).is_file() and not (_REPO / rel).is_symlink()
    ]


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
    lines, so the words either side of the number are separated by ``"\\n> "`` — which
    a pattern tolerating whitespace between them still does not match, because of the
    ``>``. Prose shapes therefore read this flattened view; table shapes, which need
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
    # "the 118 declared operations", "It covers 118 operations". A bare "the N
    # operations" is deliberately NOT read: "The two operations are not equivalent" is
    # ordinary English about a pair, and a pattern that misreads it once will do so
    # again. "8 of the 118 operations are `ANY`" is still covered, by the `any` shape
    # above, which reads its own number.
    (
        "total",
        FLAT,
        re.compile(r"the" + _M + _NUM + _M + r"declared\s+operations", re.IGNORECASE),
    ),
    # "Across all 118: 21 require `Admin`; ..." — the per-group breakdown's preamble,
    # where the noun is left implicit, so the shape above does not reach it.
    ("total", FLAT, re.compile(r"across all" + _M + _NUM + _M + r":", re.IGNORECASE)),
    # The three per-object narrowings, restated in the same sentence on two pages:
    # "13 operations verify config-version scope, 4 filter their result rows by it,
    # and 9 verify per-object ownership" / "... 4 filter list rows by it, and 9
    # enforce per-object ownership". They come from the `scope_checked`,
    # `scope_filtered` and `ownership` flags in the same file as the policy counts,
    # and they are read here because the sentence is one sentence: a change that moves
    # a flag has to update two pages by hand, exactly as one that moves a policy, and
    # reading the numbers either side of a comma but not the one between them would
    # leave three measures restated with nothing comparing them.
    (
        "scope_checked",
        FLAT,
        re.compile(
            _NUM
            + _M
            + r"(?:operations\s+)?(?:additionally\s+)?(?:verify|enforce)"
            + _M
            + r"config-version scope",
            re.IGNORECASE,
        ),
    ),
    (
        "scope_filtered",
        FLAT,
        re.compile(
            _NUM + _M + r"filter" + r"[^.|\d\n]{0,20}?" + r"rows by it", re.IGNORECASE
        ),
    ),
    (
        "ownership",
        FLAT,
        re.compile(
            _NUM + _M + r"(?:verify|enforce)" + _M + r"per-object ownership", re.I
        ),
    ),
)


def _measures() -> dict[str, int]:
    """The distribution, derived from the expectations file."""
    ops = yaml.safe_load(_EXPECTATIONS.read_text(encoding="utf-8"))["operations"]
    policies = [entry["groups"] for entry in ops.values()]
    iam_only = [p for p in policies if p == "IAM_ONLY"]
    any_auth = [p for p in policies if p == "ANY"]
    any_group = [p for p in policies if p == "ANY_GROUP"]
    flagged = {
        flag: sum(1 for entry in ops.values() if entry.get(flag))
        for flag in ("scope_checked", "scope_filtered", "ownership")
    }
    return {
        "total": len(ops),
        "iam_only": len(iam_only),
        "any": len(any_auth),
        "any_group": len(any_group),
        "group_restricted": len(ops) - len(iam_only) - len(any_auth),
        "explicit_list": len(ops) - len(iam_only) - len(any_auth) - len(any_group),
        **flagged,
    }


_RELEASED_SECTION = re.compile(r"^## \[\d", re.MULTILINE)


def _document_text(rel: str) -> str:
    """The part of a document this gate is responsible for.

    For every file that is the whole of it. For ``CHANGELOG.md`` it is the
    ``[Unreleased]`` section only: a released entry is a frozen record of what was true
    at that release, so "26 of the 118 operations are declared `ANY`" in the v0.6.10
    entry is correct history and must not be rewritten. Truncating rather than
    excluding the file keeps the live section inside the gate — and inside the derived
    universe, so this is a scope decision rather than an exemption.
    """
    raw = (_REPO / rel).read_text(encoding="utf-8")
    if rel != "CHANGELOG.md":
        return raw
    cut = _RELEASED_SECTION.search(raw)
    return raw[: cut.start()] if cut else raw


def _counts_in(rel: str) -> list[tuple[str, int]]:
    """(measure, stated) for every count a shape reads out of one document."""
    raw = _document_text(rel)
    views = {FLAT: _flat(raw), LINES: raw}
    return [
        (measure, _as_int(match.group(1)))
        for measure, view, shape in SHAPES
        for match in shape.finditer(views[view])
    ]


#: Every document this gate compares, scanned and partially-excluded alike.
ALL_DOCS = (*SCANNED_DOCS, *sorted(EXCLUDED_DOCS))


def _findings() -> list[tuple[str, str, int, int]]:
    """(doc, measure, stated, expected) for every count this gate is responsible for.

    A document in ``EXCLUDED_DOCS`` contributes the measures its entry does *not*
    name, because the exclusion is per (document, measure) rather than per document.
    """
    measures = _measures()
    return [
        (rel, measure, stated, measures[measure])
        for rel in ALL_DOCS
        for measure, stated in _counts_in(rel)
        if measure not in EXCLUDED_DOCS.get(rel, (frozenset(),))[0]
    ]


@pytest.mark.parametrize("doc", ALL_DOCS)
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


@pytest.mark.parametrize("index", range(len(SHAPES)))
def test_every_shape_is_live(index: int) -> None:
    """A shape that matches nothing has stopped being a check.

    Parametrised over **shapes**, not measures. Over measures it asserts almost
    nothing: six of these shapes share a measure with a sibling, so one pattern could
    go dead — reading no document at all — while the measure-level check stayed green
    on its sibling's hits. That was true of a "the remaining N are declared
    `groups: ANY`" shape here, whose only phrasing lives on the one excluded page.
    """
    measure, view, shape = SHAPES[index]
    total = 0
    for rel in SCANNED_DOCS:
        raw = _document_text(rel)
        total += len(shape.findall(_flat(raw) if view == FLAT else raw))
    assert total, (
        f"SHAPES[{index}] ({measure}, {view}) matches nothing in any scanned document, "
        "so it is not a check. Either the prose was reworded past it — update the "
        "pattern — or the phrasing it reads no longer exists here, in which case "
        "delete the shape rather than leaving a dead one that makes the coverage look "
        "wider than it is."
    )


@pytest.mark.parametrize("doc", SCANNED_DOCS)
def test_document_still_states_its_counts(doc: str) -> None:
    """Each document must still yield at least the counts it yielded when pinned.

    ``test_every_policy_count_matches_the_expectations_file`` compares whatever it
    finds, so without a floor it passes **vacuously** on a document whose counts have
    all been reworded past every shape — which is not a document that has stopped
    making claims, only one this gate has stopped reading. Rewording all three counts
    on one page is enough to reach that state, and nothing else in this module reports
    it.
    """
    floor = MIN_FINDINGS_PER_DOC[doc]
    found = len(_counts_in(doc))
    assert found >= floor, (
        f"{doc} now yields {found} policy counts, down from the pinned floor of "
        f"{floor}. Either a count was removed — lower the floor in "
        "MIN_FINDINGS_PER_DOC deliberately, in the same change — or it was reworded "
        "into a phrasing no shape reads, which leaves the page making a claim nothing "
        "checks."
    )


def test_no_document_states_counts_unread() -> None:
    """The document universe is derived, so a new page cannot escape the gate.

    ``SCANNED_DOCS`` is authored, and an authored list of documents is exactly the
    structure that goes stale: a page added later restating the distribution wrongly
    would be checked by nothing. So every tracked ``.md`` file is searched with the
    same shapes, and one that states a count must be either scanned or in
    ``EXCLUDED_DOCS`` with a reason.

    ``CHANGELOG.md`` is scanned but only over ``[Unreleased]`` (see
    ``_document_text``), and the discovery below uses the same view, so a released
    entry's frozen counts are outside the universe by construction rather than by
    exemption.
    """
    measures = _measures()
    stray: dict[str, list[tuple[str, int]]] = {}
    for rel in _tracked_markdown():
        if rel in SCANNED_DOCS or rel in EXCLUDED_DOCS:
            continue
        counts = _counts_in(rel)
        if counts:
            stray[rel] = sorted(set(counts))
    assert not stray, (
        "a document states an API policy count and is in neither SCANNED_DOCS nor "
        "EXCLUDED_DOCS, so nothing checks it:\n"
        + "\n".join(
            f"  {rel}: {counts} (derived values: {measures})"
            for rel, counts in sorted(stray.items())
        )
        + "\nAdd it to SCANNED_DOCS, or to EXCLUDED_DOCS with a reason — and if you "
        "exclude it, register the exclusion in scripts/tests/gate_exemptions.json."
    )


@pytest.mark.parametrize("doc", sorted(EXCLUDED_DOCS))
def test_excluded_document_still_states_counts(doc: str) -> None:
    """An exclusion that shields nothing is dead, and pre-exempts whatever replaces it.

    Non-vacuity is asserted against the **exempted measures specifically**, not against
    any count on the page: a page that kept stating an unexempted number while losing
    every exempted one would otherwise keep the entry alive on a count the entry is not
    about.
    """
    assert (_REPO / doc).is_file(), f"{doc} is excluded but no longer exists"
    exempt, _reason = EXCLUDED_DOCS[doc]
    shielded = sorted({m for m, _ in _counts_in(doc) if m in exempt})
    assert shielded, (
        f"{doc} is excluded from this gate for {sorted(exempt)} on the grounds that "
        "another gate reads those, but it no longer states any of them. Remove the "
        "EXCLUDED_DOCS entry and its registry entry."
    )


_SIBLING_MODULE = _REPO / "scripts" / "tests" / "test_well_architected_doc.py"


def _counts_asserted_by_the_sibling_test() -> set[str]:
    """First arguments of every ``_assert_count_phrase`` call inside ``SIBLING_TEST``.

    Parsed, not grepped, and scoped to that one function — both of which are load
    bearing. A substring search over the whole module is satisfied by a derivation
    nothing asserts, by a mention in a docstring, and by an identical expression in a
    *different* test that makes no claim about this page's counts; all three leave the
    exclusion standing over numbers no check reads. The AST pattern is the one
    ``test_gate_exemption_registry.py::_predicates_called_in`` already uses, and for
    the same reason.
    """
    tree = ast.parse(
        _SIBLING_MODULE.read_text(encoding="utf-8"), filename=str(_SIBLING_MODULE)
    )
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == SIBLING_TEST
        ),
        None,
    )
    if fn is None:
        return set()
    return {
        ast.unparse(node.args[0])
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_assert_count_phrase"
        and node.args
    }


@pytest.mark.parametrize("measure", sorted(SIBLING_GATE_MEASURES))
def test_the_sibling_gate_still_asserts_each_measure_it_is_trusted_for(
    measure: str,
) -> None:
    """The exclusion's premise, computed per measure rather than asserted for the set.

    ``EXCLUDED_DOCS`` defers these measures for ``docs/well-architected.md`` to
    ``SIBLING_TEST``. Parametrised over one measure at a time because that is the grain
    at which the claim can be false: stated for the set it already was, naming a
    measure that module derives but never asserts.

    What is required is an **assertion**, located inside that function. Requiring only
    that a derivation exist somewhere in the module is defeated three ways — deleting
    every assertion while keeping the derivations, deleting one measure's assertion, or
    emptying the function entirely, since four of the five expressions also occur in a
    sibling test that asserts nothing about this page.
    """
    asserted = _counts_asserted_by_the_sibling_test()
    assert asserted, (
        f"{_SIBLING_MODULE.name} no longer declares {SIBLING_TEST}, or that function "
        "no longer calls _assert_count_phrase at all, so docs/well-architected.md's "
        "policy counts are read by nothing while an exclusion says they are covered. "
        "Move the page into SCANNED_DOCS and drop the EXCLUDED_DOCS entry."
    )
    expression = SIBLING_GATE_MEASURES[measure]
    assert expression in asserted, (
        f"{SIBLING_TEST} no longer ASSERTS {measure} — no _assert_count_phrase call in "
        f"it takes {expression!r} as its first argument (present: {sorted(asserted)}). "
        "EXCLUDED_DOCS defers that measure to it, so nothing would read it. Either "
        "restore the assertion there, or drop the measure from SIBLING_GATE_MEASURES, "
        "which brings the page back under this gate for it."
    )


def test_the_sibling_test_is_collected_and_not_skipped() -> None:
    """A present assertion is not an executed one.

    The check above reads source, so it is satisfied by assertions that exist and never
    run: a module pytest does not collect, a collection error, or a ``skip`` mark would
    all leave every expression in place while the page's counts are compared by nothing.
    Collection is asked of pytest itself rather than inferred, and the skip marks are
    read off the source, because a mark is what turns a collected test into one that
    reports nothing.
    """
    node = f"{_SIBLING_MODULE.relative_to(_REPO).as_posix()}::{SIBLING_TEST}"
    proc = subprocess.run(
        [
            "python",
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            node,
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and f"::{SIBLING_TEST}" in proc.stdout, (
        f"pytest does not collect {node}, so the assertions this exclusion defers to "
        "never run. EXCLUDED_DOCS would be shielding counts nothing checks.\n"
        f"{proc.stdout[-1500:]}{proc.stderr[-500:]}"
    )

    tree = ast.parse(_SIBLING_MODULE.read_text(encoding="utf-8"))
    fn = next(
        node_
        for node_ in ast.walk(tree)
        if isinstance(node_, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node_.name == SIBLING_TEST
    )
    marks = {
        ast.unparse(d)
        for d in fn.decorator_list
        if "skip" in ast.unparse(d) or "xfail" in ast.unparse(d)
    }
    assert not marks, (
        f"{SIBLING_TEST} carries {sorted(marks)}, so its assertions are collected but "
        "report nothing. Either remove the mark or move docs/well-architected.md into "
        "SCANNED_DOCS while it stands."
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
        for m in bolded.finditer(_flat(_document_text(rel)))
        if (rel, _as_int(m.group(1))) not in read
    ]
    assert not unread, (
        "a bolded policy count is not read by any shape in this module, so nothing "
        "would notice it going stale:\n"
        + "\n".join(f"  {rel}: {snippet!r} (={n})" for rel, n, snippet in unread)
        + "\nAdd a shape that reads it."
    )
