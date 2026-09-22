# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pin the counts this tree's prose states to the thing that produces them.

A count written into prose, describing something that changes whenever a gate or a
file is added, goes stale on the next change with nothing to say so. Two families of
that shape exist here and they are one mechanism, not two:

* the number of **shared CI gates**, which ``scripts/tests/test_ci_gate_parity.py``
  holds as :data:`SHARED_GATES` and which grew when the SRT scan and the dependency
  audit were brought under the parity guard;
* the number of **tracked Python files**, which the type gate's own documentation
  used to quote and which grows with the tree.

``test_ci_gate_parity.py`` already ratchets the gate *list* — every entry must run in
both CIs and name a real target — but the only assertion on its length is a floor
(``>= 8``), which is a guard against the list being emptied and says nothing about
what the documentation claims. Prose stating a count was therefore unchecked in both
directions: a gate could be added and the prose left alone, or the prose edited to a
number no longer true of the list.

**How each count is settled.** Either the prose derives it or the prose does not state
it:

* Where the number carries a substantive point, it stays in the prose and this module
  pins it. The shared-gate split is that case — that most but not all of them are
  steps in a *single* GitHub job is why those collapse to one requireable status
  check, and dropping the numbers from the prose would lose the argument.
* Where the number is incidental colour, the literal is gone and the prose names the
  derivation instead. The tracked-Python-file count is that case: "it analyses every
  tracked ``.py`` file" is both true and durable, where a figure is neither for long.
  The patterns for it below are therefore a guard against a literal being
  reintroduced, not a check on one that is there.

**The bar a derived count has to clear.** ``scripts/tests/test_contributing_doc.py``
carries a standard, in the comment where a count check on ``len(RUN_ROOTS)`` used to
be: derive a number when the number is a claim a reader **acts on**, not when it is
scenery, because a gate that fails a routine change for a cosmetic reason teaches
people to read gate failures as bureaucracy. Both counts here were judged against it,
and they landed on opposite sides.

The shared-gate split passes. A reader acts on it: it is why a red required check does
not say which gate failed, and it is what somebody enabling branch protection needs in
order to know how many contexts to require. It is also not merely cosmetic to keep in
step — a gate added to :data:`SHARED_GATES` either joins ``developer_tests``, which
moves the eight, or arrives as a job of its own, which moves the number of requireable
contexts and so changes what protection has to name.

The tracked-file count fails it, and is handled by deletion rather than by
enforcement: how many files the type gate reads is scenery next to the fact that it
reads all of them, so the prose says that instead and the patterns below only stop a
figure coming back.

**What this cannot do.** It reads prose with regular expressions, so it sees the
phrasings registered below and not a paraphrase. That hole is closed in the direction
that matters by :func:`test_every_document_making_a_count_claim_states_a_pinned_one`:
a document that is supposed to state a count and matches no pattern fails, so
rewording it into a form this module cannot read is a failure rather than a silent
pass. The reverse hole — a *new* document inventing an unregistered phrasing — stays
open, and is the reason the scan below runs over the whole tracked tree rather than
over the registered documents only: the anchored phrasings are specific enough to be
safe tree-wide, so a new file repeating one of them is caught wherever it is.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PARITY_MODULE = REPO_ROOT / "scripts/tests/test_ci_gate_parity.py"
DEVELOPER_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/developer-tests.yml"
SECURITY_WORKFLOW = REPO_ROOT / ".github/workflows/security-checks.yml"

#: Spelled-out numerals this tree's prose uses. Both spellings are accepted for every
#: pattern, because a document may reasonably write either.
_NUMBER_WORDS = {
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

#: A number token: a spelled-out numeral, or 1-5 digits with optional `**bold**`
#: markers and an optional thousands comma, which is how these appear in Markdown.
_NUMBER = r"(?:\*\*)?(" + "|".join(_NUMBER_WORDS) + r"|\d{1,3},?\d{0,3})(?:\*\*)?"


def _as_int(token: str) -> int | None:
    """Parse a captured number token, or ``None`` if it is not a number at all."""
    cleaned = token.strip("*").replace(",", "")
    if cleaned.isdigit():
        return int(cleaned)
    return _NUMBER_WORDS.get(cleaned.lower())


def _tracked(*suffixes: str) -> list[Path]:
    """Tracked files with any of ``suffixes``, discovered through ``git ls-files``.

    Discovery through git is what keeps build output, a packaged copy of the library
    and a sibling agent worktree from contributing matches, none of which a reader of
    this repository ever sees.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        REPO_ROOT / name
        for name in listed.split("\0")
        if name and name.endswith(suffixes)
    ]


def _parity_module():
    """Import ``test_ci_gate_parity`` by path — it is the source of truth for gates."""
    spec = importlib.util.spec_from_file_location("_parity_source", PARITY_MODULE)
    assert spec and spec.loader, f"cannot load {PARITY_MODULE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- The three derivations ----------------------------------------------------


def derive_shared_gate_total() -> int:
    """How many gates the parity guard asserts run in both CI systems."""
    return len(_parity_module().SHARED_GATES)


def derive_gates_in_developer_tests() -> int:
    """How many of them are steps in GitHub's single ``developer_tests`` job.

    This is the number the requireable-context argument rests on, so it is counted
    from the workflow rather than assumed to be "all of them" — which is what the
    documentation said until the two security gates joined the list.
    """
    parity = _parity_module()
    workflow = parity._uncommented(DEVELOPER_TESTS_WORKFLOW.read_text())
    return sum(1 for gate in parity.SHARED_GATES if gate in workflow)


def derive_requireable_contexts_with_a_shared_gate() -> int:
    """How many GitHub status-check contexts the shared gates are spread across.

    A context is a job, so this counts jobs holding at least one shared gate: the one
    in ``developer-tests.yml`` plus each job in ``security-checks.yml`` that holds
    one.
    """
    parity = _parity_module()
    contexts = 0
    for workflow in (DEVELOPER_TESTS_WORKFLOW, SECURITY_WORKFLOW):
        text = parity._uncommented(workflow.read_text())
        # Job bodies are split on the 2-space-indented `<job_id>:` that starts each.
        for body in re.split(r"^  (?=\S)", text, flags=re.MULTILINE)[1:]:
            if any(gate in body for gate in parity.SHARED_GATES):
                contexts += 1
    return contexts


def derive_tracked_python_files() -> int:
    """Every tracked ``.py`` file — the set the whole-tree type gate covers."""
    return len(_tracked(".py"))


# --- The registry -------------------------------------------------------------


@dataclass(frozen=True)
class DocumentedCount:
    """One count, the thing that produces it, and how prose is allowed to state it."""

    name: str
    derive: Callable[[], int]
    #: Each must contain exactly one capturing group, around the number.
    patterns: tuple[str, ...]
    #: Documents required to state this count. Empty means the correct number of
    #: statements is zero and these patterns are a reintroduction guard.
    documents: tuple[str, ...]
    why: str


DOCUMENTED_COUNTS = (
    DocumentedCount(
        name="shared gates in total",
        derive=derive_shared_gate_total,
        patterns=(
            rf"\b{_NUMBER} shared gates\b",
            rf"\bof the {_NUMBER} gates asserted by\b",
        ),
        documents=(
            "CLAUDE.md",
            "docs/testing.md",
            "scripts/sdlc/docs/CI_TEST_COVERAGE.md",
            "scripts/sdlc/check_branch_protection.py",
        ),
        why=(
            "len(SHARED_GATES) in test_ci_gate_parity.py, whose own length assertion "
            "is only a floor"
        ),
    ),
    DocumentedCount(
        name="shared gates that are steps in the developer_tests job",
        derive=derive_gates_in_developer_tests,
        patterns=(
            rf"\b{_NUMBER} of the (?:\w+|\d+) shared gates\b",
            rf"\b{_NUMBER} of the (?:\w+|\d+) gates asserted by\b",
        ),
        documents=(
            "CLAUDE.md",
            "docs/testing.md",
            "scripts/sdlc/docs/CI_TEST_COVERAGE.md",
            "scripts/sdlc/check_branch_protection.py",
        ),
        why=(
            "counted from .github/workflows/developer-tests.yml; it is the basis of "
            "the claim that these gates collapse to one requireable check"
        ),
    ),
    DocumentedCount(
        name="requireable contexts holding a shared gate",
        derive=derive_requireable_contexts_with_a_shared_gate,
        patterns=(rf"\b{_NUMBER} requireable contexts\b",),
        documents=(),
        why="GitHub jobs holding at least one shared gate, across the two workflows",
    ),
    DocumentedCount(
        name="tracked Python files",
        derive=derive_tracked_python_files,
        # Deliberately narrow, and bounded away from the historical statements about
        # what issue #975 measured, which are correct as written and must keep their
        # figures. Those all read "<n> of 1230 tracked .py files", so the lookbehind
        # for "of " is what separates a live claim from a dated one;
        # test_the_python_file_patterns_leave_the_historical_statements_alone proves
        # it against the real sentences.
        # `over <n> files` is deliberately NOT among these. It matched a sentence in
        # feature-platform/confbench-testset/shared/python/variants.py about walking a
        # HuggingFace dataset tree, which has nothing to do with this tree's Python
        # files — and a gate that reports an unrelated sentence is one people learn to
        # ignore. The residual is that the Makefile's phrasing would not be caught if
        # reintroduced in that exact form; it is written without a figure instead.
        patterns=(
            r"(?<!of )(?<!of \*\*)\b" + _NUMBER + r" tracked `?\.py`? files\b",
            rf"\banalyses (?:all )?{_NUMBER} files\b",
            rf"~1 minute, {_NUMBER} files",
        ),
        documents=(),
        why=(
            "git ls-files '*.py'; the prose names this derivation instead of quoting "
            "a figure, so the correct number of live statements is zero"
        ),
    ),
)

#: Suffixes scanned. Prose that states one of these counts lives in Markdown, in a
#: Python docstring, in the Makefile or in a make fragment.
_SCANNED_SUFFIXES = (".md", ".py", ".mk", "Makefile")


def _matches(count: DocumentedCount) -> list[tuple[Path, str, int | None]]:
    """Every statement of ``count`` in the tracked tree: (path, matched text, value)."""
    found = []
    for path in _tracked(*_SCANNED_SUFFIXES):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in count.patterns:
            # Case-insensitive: a number word at the start of a sentence is
            # capitalised, and a pattern that missed those would read as satisfied
            # while ignoring half the statements.
            for match in re.finditer(pattern, text, re.IGNORECASE):
                found.append((path, match.group(0), _as_int(match.group(1))))
    return found


@pytest.mark.unit
@pytest.mark.parametrize("count", DOCUMENTED_COUNTS, ids=lambda c: c.name)
def test_every_stated_count_matches_what_produces_it(count: DocumentedCount) -> None:
    """The assertion this module exists for, in the direction prose goes stale."""
    expected = count.derive()
    wrong = [
        (path.relative_to(REPO_ROOT).as_posix(), text, value)
        for path, text, value in _matches(count)
        if value != expected
    ]
    assert not wrong, (
        f"prose states a different number of {count.name} than the "
        f"{expected} produced by {count.why}:\n"
        + "\n".join(
            f"  {path}: {text!r} (reads as {value})" for path, text, value in wrong
        )
        + "\n\nCorrect the prose to the derived figure, or drop the literal and name "
        "the derivation instead — do not write the change up as a correction, since "
        "a document states what is true now."
    )


@pytest.mark.unit
@pytest.mark.parametrize("count", DOCUMENTED_COUNTS, ids=lambda c: c.name)
def test_every_document_making_a_count_claim_states_a_pinned_one(
    count: DocumentedCount,
) -> None:
    """Non-vacuity, per document.

    Without this, rewording a claim into a phrasing the patterns do not read would
    leave the test above passing because it matched nothing — which is how a count
    drifts while a green gate reports on it. A document listed here must match at
    least one pattern, so an entry whose patterns are vacuous against it fails here
    rather than going quiet.
    """
    matched_documents = {
        path.relative_to(REPO_ROOT).as_posix() for path, _, _ in _matches(count)
    }
    missing = [
        document for document in count.documents if document not in matched_documents
    ]
    assert not missing, (
        f"{missing} should each state the number of {count.name} in a form this "
        f"module reads, and none of {count.patterns} matched. Either the wording "
        "moved away from the registered phrasings — in which case restore one or add "
        "the new phrasing to the patterns — or the claim was removed, in which case "
        "drop the document from this entry."
    )


@pytest.mark.unit
def test_the_registry_is_not_empty_and_every_pattern_has_one_group() -> None:
    """A pattern with no capturing group would match and then read as ``None``."""
    # Asserted through a derived list rather than on the tuple itself: the literal is
    # statically non-empty, so `assert DOCUMENTED_COUNTS` is a condition a type
    # checker can prove always true (reportAssertAlwaysTrue) even though the case it
    # guards — somebody emptying the registry — is real.
    registered = [count.name for count in DOCUMENTED_COUNTS]
    assert registered, "nothing is pinned, so this module asserts nothing"
    for count in DOCUMENTED_COUNTS:
        assert count.patterns, f"{count.name} registers no patterns"
        for pattern in count.patterns:
            assert re.compile(pattern).groups == 1, (
                f"{pattern!r} for {count.name} must have exactly one capturing "
                f"group, around the number; it has {re.compile(pattern).groups}"
            )


#: The statements about what issue #975 measured. They are dated facts about a state
#: this repository has since left, they are correct as written, and re-pointing their
#: figures at today's tree would make them false.
_HISTORICAL_FILE_COUNT_STATEMENTS = (
    "442 of 1230 tracked `.py` files were read by neither `ruff check` nor `ruff format`",
    "so 442 of 1230 tracked .py files were read by neither the",
    "442 of 1230 tracked .py files were read",
    "reached 432 of 1230 files, and two `NameError`-class defects",
    "Type checking now covers all 1230 files",
)


@pytest.mark.unit
@pytest.mark.parametrize("statement", _HISTORICAL_FILE_COUNT_STATEMENTS)
def test_the_python_file_patterns_leave_the_historical_statements_alone(
    statement: str,
) -> None:
    """A guard on the guard: these sentences must not be dragged forward.

    The tracked-file patterns are the only ones here at risk of matching a dated
    statement, because the phrase they look for is the phrase those statements use.
    """
    count = next(c for c in DOCUMENTED_COUNTS if c.name == "tracked Python files")
    for pattern in count.patterns:
        match = re.search(pattern, statement, re.IGNORECASE)
        assert match is None, (
            f"{pattern!r} matches the historical statement {statement!r} (as "
            f"{match.group(0)!r} if so), so the gate would demand it be changed to "
            "today's figure. Narrow the pattern."
        )


@pytest.mark.unit
def test_this_module_does_not_match_its_own_patterns() -> None:
    """The scan reads every tracked ``.py`` file, which includes this one.

    The patterns are built around a substituted placeholder rather than written out
    with a number in them, so they cannot match their own source. That is a property
    worth asserting rather than assuming, since it is what lets the scan cover the
    whole tree with nothing carved out of it.
    """
    source = Path(__file__)
    for count in DOCUMENTED_COUNTS:
        self_matches = [text for path, text, _ in _matches(count) if path == source]
        assert not self_matches, (
            f"{count.name}'s patterns match this module's own source ({self_matches}), "
            "so it would have to be skipped by the scan. Rewrite the pattern so it "
            "cannot match the way it is spelled."
        )
