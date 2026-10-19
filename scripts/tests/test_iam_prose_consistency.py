# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No document may claim the browser's Identity Pool role grants S3, because it does not.

``test_browser_s3_grants.py`` pins the **policy**: ``CognitoAuthorizedRole`` carries no
S3 statement at all, under any policy name, since the document read path moved behind a
resolver (#1033). This file pins the **prose** against it. A security deliverable
asserting a grant the template does not contain is the defect this gate exists for: a
reader of ``rbac-authentication.md`` or ``docs/rbac.md`` acts on the enumeration, and one
that over-states the grant invites a reviewer to accept a boundary narrower than they
were told, or to "restore" a grant that was removed deliberately.

**The two directions collapsed into one when the grant went to zero.** This gate used to
assert both that no claim named an ungranted bucket and that a complete enumeration named
every granted one. With no granted bucket there is no second direction: every positive
claim is stale, and the gate is "find any positive claim". That makes the assertion
stronger and the **recognizer** the only fragile part — so most of this file is now about
the recognizer still working, since an empty claim set is what a reader that has stopped
reading also reports.

**Why a gate and not a careful edit.** Nothing else in this tree compares the two.
``check_prose_counts`` in ``build_threat_model.py`` reads threat totals, statuses, risk
bands and STRIDE tallies — no policy content. ``test_browser_s3_grants.py`` reads only
``template.yaml``. So the whole tree can be green while the policy and the prose
disagree, and that is not hypothetical: it is what a conflicting merge produces when the
side that auto-merges is the template and the side offering a choosable stale hunk is
the prose. Merge order is not a control; this is.

The document set is **derived, not authored**
-----------------------------------------------
Every tracked ``.md`` is read, and a document carrying a readable claim that appears in
neither :data:`DOCUMENTS_STATING_THE_GRANT` nor
:data:`DOCUMENTS_WITH_HISTORICAL_CLAIMS_ONLY` fails
:func:`test_no_document_states_the_grant_outside_the_registered_set`. An authored list
was the wrong shape for this: the candidate set is computable from ``git ls-files``, so
by the registry doctrine it must be computed, and a hand-written one silently omitted
``docs/rbac.md`` — the document whose grant sentence this change actually edited, and the
one ``test_browser_s3_grants.py`` tells the reader to keep in step with the pinned set.

What counts as a claim
----------------------
``on the <enum> bucket(s)`` where **all four** hold:

1. every word of ``<enum>`` names a bucket this stack declares — which excludes the
   collective nouns ("the *document* buckets") and the Macie and Cross-Region-Replication
   recommendations in ``well-architected.md``, which also say "on the … buckets";
2. the **same sentence**, before the match, cites one of the S3 actions in
   :func:`claim_actions`. A bare ``s3:`` token was too weak: the grant was read-only, so
   "``s3:PutObject`` writes land on the Logging bucket" is not a statement about it, and
   reading it as one made the gate fail on true prose;
3. that sentence carries no **negation** before the match. "…and nothing on the
   Configuration bucket" is a true sentence *about* the grant which asserts the
   opposite of a claim, and a rule that read it as a claim accused the document of
   saying the reverse of what it says. ⚠️ This is now the main thing an author has to
   get right: a true sentence about the role granting nothing must put the negator
   immediately before the enumeration ("**nothing** on the Input and Output buckets"),
   not at the start of the clause ("does not grant ``s3:GetObject`` on the Input and
   Output buckets"), because the negation span is deliberately short — see
   :data:`_NEGATION_SPAN_CHARS` for why scanning the whole sentence was worse;
4. the role is named within :data:`_CONTEXT_CHARS` before it, so an unrelated bucket
   sentence elsewhere in the file is not attributed to this role.

Why the action vocabulary is not simply the derived one
-------------------------------------------------------
Condition 2 reads :func:`claim_actions`, which is the derived set **plus** the three
object-read actions the role used to hold. With the derived set empty, a vocabulary of
only the derived actions would match no sentence in any document — so the recognizer
would go completely inert, every stale enumeration would pass, and the gate would report
a clean tree while describing none of it. The historical actions are what keep the
sentence this gate exists to catch recognisable now that the grant it described is gone.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from browser_s3_policy import (
    REPO_ROOT,
    ROLE,
    browser_readable_buckets,
    granted_s3_actions,
)

pytestmark = pytest.mark.unit

# Every bucket this stack declares, as the English name the documents use -> its
# CloudFormation logical id. Authored on purpose: prose does not contain logical ids,
# and that "Test Set" means `TestSetBucket` is a fact about English rather than about
# the template.
BUCKET_PROSE_NAMES = {
    "input": "InputBucket",
    "output": "OutputBucket",
    "configuration": "ConfigurationBucket",
    "test set": "TestSetBucket",
    "working": "WorkingBucket",
    "reporting": "ReportingBucket",
    "evaluation baseline": "EvaluationBaselineBucket",
    "discovery": "DiscoveryBucket",
    "logging": "LoggingBucket",
}

# The S3 actions a sentence may cite and still be read as a claim ABOUT THIS ROLE.
#
# The derived set (`browser_s3_policy.granted_s3_actions`) is **empty**: the role grants
# no S3 (#1033). An empty vocabulary makes condition 2 unsatisfiable, so the recognizer
# would match nothing in any document — the gate would pass on a tree full of stale
# enumerations while reading none of it. The three object-read actions the role DID hold
# are therefore retained, because the sentence this gate exists to catch is exactly the
# sentence that cites one of them.
#
# Read-only on purpose. `s3:PutObject` is not here and must not be: this role never held
# a write action, so "`s3:PutObject` writes land on the Logging bucket" is a true
# sentence about something else, and admitting it made the gate fail on correct prose.
HISTORICAL_OBJECT_READ_ACTIONS = frozenset(
    {"s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"}
)


def claim_actions() -> set:
    """The action vocabulary condition 2 reads: derived, plus the historical three.

    A union rather than a replacement, so a write action added to the role tomorrow
    becomes part of the vocabulary without anyone remembering to extend a literal —
    which is the property the derived set was introduced for.
    """
    return granted_s3_actions() | set(HISTORICAL_OBJECT_READ_ACTIONS)


# Documents allowed to carry a positive grant claim, with one member's worth of reason
# each. Every claim outside this set is a failure, and so is every claim inside it that
# the registered historical-section exclusion does not cover — being on this list is
# permission to have a *historical* claim, not permission to have a claim.
#
# There is deliberately no "documents required to state the grant" list any more. There
# is no grant to state: the thing those eight documents must now say is that the role
# reaches no bucket, and a sentence saying that is a non-claim by construction (condition
# 3), so requiring one would be requiring the recognizer to find something it is built
# not to find. What replaces it is `test_no_tracked_document_states_the_grant`, which
# reads the whole tree.
DOCUMENTS_ALLOWED_A_HISTORICAL_CLAIM = (
    # An audit artefact for a versioned security deliverable: CLAUDE.md names its
    # `## Version History` table as one of two deliberate ledgers in this repository.
    # Its rows describe the control as it stood at each revision, so a row enumerating a
    # grant that has since been removed is the row doing its job.
    "security/threat-modeling/README.md",
    # Append-only release history. A merged entry describes the release it documents and
    # is not rewritten when the behaviour changes later (CLAUDE.md: product-behaviour
    # history in CHANGELOG.md is legitimate). The grant was narrowed after those entries
    # shipped, so the correct fix was an EXCLUDED_HISTORICAL_LEDGER_SECTIONS entry per
    # affected release section rather than an edit to the entries — which is what the
    # previous version of this comment said to do, and is what was done.
    "CHANGELOG.md",
)

# Sections whose prose describes the past and is not a statement of current fact.
# `<path>: <markdown heading>` — bounded to ONE section each: the region removed runs
# from the heading to the next same-level heading, not to end of file.
#
# One entry per release section that carries a claim, rather than one entry for
# `CHANGELOG.md` as a whole, so a claim in a *new* entry still fails. That is the
# difference between excluding the history and switching the gate off for the file.
EXCLUDED_HISTORICAL_LEDGER_SECTIONS = {
    "security/threat-modeling/README.md": ("## Version History",),
    # The 0.6.10 release notes record the eighteen operations tightened to ANY_GROUP and
    # state, correctly for that release, that the Identity Pool role still granted the
    # document buckets directly. #1033 is what changed that, two releases later.
    "CHANGELOG.md": ("## [0.6.10]",),
}

# `on the <enum> bucket(s)`, with EVERY run of whitespace as `\s+` — including the one
# after "the". These documents are hard-wrapped at 88 columns and the line can break
# anywhere in the phrase, so a literal space anywhere in the pattern makes a wrapped
# claim unreadable. Both halves of that cost real coverage: a literal space before
# "buckets" found no claim at all in `govcloud-architecture.md`, and one after "the"
# missed a claim that wrapped there. Either way the gate passes while reading nothing,
# which is the quietest failure it can have.
_CLAIM_RE = re.compile(
    r"on\s+the\s+((?:[A-Za-z]+[\s,]+|and\s+)*?[A-Za-z]+)\s+(buckets?)\b", re.I
)

# How far back the ROLE may be named. Generous (the role is named in a parent bullet in
# `aws-services-and-roles.md`, 414 characters from its grant) because attribution is the
# loosest of the four conditions; the granted-action and negation tests below are what
# make a candidate a claim, and both are evaluated on the sentence alone.
_CONTEXT_CHARS = 600
_ROLE_MARKERS = (ROLE.lower(), "authenticated role")

# Sentence boundaries, for the granted-action condition below.
#
# `|` is included because the threat-model entries are markdown table rows and a cell
# boundary is at least as strong a break as a full stop. The optional run after the `.`
# is markdown emphasis and closing punctuation: these documents routinely end a sentence
# inside bold — "…govern the API, not the buckets.** `CognitoIdentityPoolSetRole` …" —
# so a plain `\.\s` did not break there, and the *previous* sentence's words leaked into
# the next one's window. That is not cosmetic: it is what made a negation two sentences
# earlier suppress a real claim in both `well-architected.md` and
# `rbac-authentication.md`.
_SENTENCE_BREAK = re.compile(r"(?:\.[*_`\"')\]]*\s|\n\n|\|)")

# Words that make the phrase *deny* the grant rather than assert it.
#
# Checked only in the short span IMMEDIATELY before "on the", not across the sentence.
# A negation attaches to the phrase it precedes: "…and nothing on the Configuration
# bucket" denies, whereas "the floor gates the API, not the buckets. … grants
# `s3:GetObject` on the Input and Output buckets" contains a negation that has nothing
# to do with the enumeration. Scanning the whole sentence for "not " suppressed two of
# the corpus's real claims, which is the failure mode a gate must not have: it goes
# quiet rather than loud.
_NEGATION_SPAN_CHARS = 44
_NEGATORS = (
    "nothing",
    "not",
    "no ",
    "never",
    "neither",
    "absent",
    "rather than",
    "instead of",
)


def _tracked_markdown() -> list:
    """Every tracked ``.md`` path, from git.

    ``git ls-files`` rather than a filesystem walk for the reason
    ``exemption_discovery`` gives: a walk reports findings against build output and
    sibling worktrees, and this gate runs inside one.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def excluded_region(rel: str, text: str, heading: str):
    """``(start, end)`` of one registered historical section, or ``None``.

    The end is the next same-level (``## ``) heading, so the exclusion covers **one
    section** rather than truncating the file. Truncating is what the first version did,
    and it made every claim after the heading invisible — harmless only for as long as
    the excluded section happens to be last, which is not a property anyone maintains.
    It matters more now that ``CHANGELOG.md`` has an entry: that file's excluded section
    has 200-odd sections after it, every one of which must still be read.

    The heading is located with a line-anchored search, not ``str.find``: the latter is
    a substring search, so the heading text quoted inside an earlier paragraph would
    move the exclusion hundreds of lines earlier with nothing to say so.
    """
    start_match = re.search(rf"^{re.escape(heading)}\s*$", text, re.M)
    if start_match is None:
        return None
    next_heading = re.search(r"^## ", text[start_match.end() :], re.M)
    end = start_match.end() + next_heading.start() if next_heading else len(text)
    return start_match.start(), end


def excluded_regions(rel: str, text: str) -> list:
    """Every registered historical section in one document.

    A tuple of headings per path rather than one, so a second release section can be
    excluded without excluding the file. A heading that is no longer present yields
    nothing here and is caught by
    :func:`test_the_excluded_region_is_one_section_and_not_the_rest_of_the_file`.
    """
    regions = []
    for heading in EXCLUDED_HISTORICAL_LEDGER_SECTIONS.get(rel, ()):
        region = excluded_region(rel, text, heading)
        if region is not None:
            regions.append(region)
    return regions


def _strip_historical(rel: str, text: str) -> str:
    """``text`` with every registered historical section blanked out.

    Replaced with spaces rather than deleted so that every line number this module
    reports still matches the file a reader will open.
    """
    for start, end in sorted(excluded_regions(rel, text), reverse=True):
        text = text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]
    return text


def _enum_to_buckets(enum: str):
    """The bucket logical ids an enumeration names, or ``None`` if it names none.

    ``None`` — not an empty set — when any word is not a bucket name, because that is
    the signal that the phrase is a collective noun ("the document buckets") rather
    than an enumeration this gate should read.
    """
    cleaned = re.sub(r"\band\b", " ", enum, flags=re.I)
    cleaned = cleaned.replace(",", " ").strip().lower()
    # Longest names first so "evaluation baseline" and "test set" are consumed before
    # their single-word parts are looked up individually.
    for phrase in sorted(BUCKET_PROSE_NAMES, key=len, reverse=True):
        if " " in phrase:
            cleaned = cleaned.replace(phrase, phrase.replace(" ", "\u0000"))
    found = set()
    for word in cleaned.split():
        name = word.replace("\u0000", " ")
        if name not in BUCKET_PROSE_NAMES:
            return None
        found.add(BUCKET_PROSE_NAMES[name])
    return found or None


def _sentence_before(text: str, index: int) -> str:
    """The text from the start of ``index``'s sentence up to ``index``."""
    breaks = list(_SENTENCE_BREAK.finditer(text, 0, index))
    return text[breaks[-1].end() : index] if breaks else text[:index]


class Claim:
    """One statement, in prose, about which buckets :data:`ROLE` may read."""

    def __init__(self, rel: str, line: int, enum: str, buckets: set, exhaustive: bool):
        self.rel = rel
        self.line = line
        self.enum = enum
        self.buckets = buckets
        self.exhaustive = exhaustive

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"{self.rel}:{self.line} {self.enum!r}"


def claims(rel: str, *, include_historical: bool = False) -> list:
    """Every readable grant claim in one document."""
    actions = claim_actions()
    full = (REPO_ROOT / rel).read_text()
    text = full if include_historical else _strip_historical(rel, full)
    found = []
    for match in _CLAIM_RE.finditer(text):
        sentence = _sentence_before(text, match.start())
        if not any(action in sentence for action in actions):
            continue
        negation_span = sentence[-_NEGATION_SPAN_CHARS:].lower()
        if any(negator in negation_span for negator in _NEGATORS):
            continue
        window = text[max(0, match.start() - _CONTEXT_CHARS) : match.start()].lower()
        if not any(marker in window for marker in _ROLE_MARKERS):
            continue
        buckets = _enum_to_buckets(match.group(1))
        if buckets is None:
            continue
        found.append(
            Claim(
                rel,
                text[: match.start()].count("\n") + 1,
                match.group(1),
                buckets,
                # An enumeration of two or more buckets reads as the complete list; a
                # single-bucket sentence is usually about one action and says nothing
                # about the others.
                exhaustive=len(buckets) >= 2,
            )
        )
    return found


ALL_REGISTERED = DOCUMENTS_ALLOWED_A_HISTORICAL_CLAIM


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# the universe
# --------------------------------------------------------------------------- #
def test_the_role_grants_no_s3_so_every_claim_is_stale():
    """The premise the whole file rests on, read from the template.

    If this fails the grant came back, ``test_browser_s3_grants.py`` is the gate that
    says so, and the documents below are no longer wrong — so fix that one first
    rather than relaxing anything here.
    """
    assert not browser_readable_buckets(), (
        f"{ROLE} names buckets again ({sorted(browser_readable_buckets())}), so a "
        "sentence enumerating them is no longer a stale claim and this file is "
        "asserting the wrong thing. See test_browser_s3_grants.py and #1033."
    )
    assert not granted_s3_actions(), (
        f"{ROLE} grants S3 actions again ({sorted(granted_s3_actions())}). Same."
    )


def test_the_action_vocabulary_is_not_empty():
    """Otherwise condition 2 is unsatisfiable and the recognizer reads nothing.

    This is the assertion that replaces "the policy grants at least one action". The
    derived set is empty by design now; what must not be empty is the vocabulary the
    recognizer compares against, because an inert recognizer and a clean tree are
    indistinguishable from the outside.
    """
    assert claim_actions(), "claim_actions() is empty, so no sentence can be a claim"
    assert HISTORICAL_OBJECT_READ_ACTIONS <= claim_actions()
    assert "s3:PutObject" not in claim_actions(), (
        "a write action in the vocabulary makes 's3:PutObject writes land on the "
        "Logging bucket' read as a claim about this role, which it is not"
    )


def test_the_markdown_universe_is_discovered():
    """A derivation that returns nothing passes for the wrong reason."""
    tracked = _tracked_markdown()
    assert len(tracked) > 100, (
        f"git ls-files '*.md' returned {len(tracked)} paths, which is too few to be "
        "this repository's documentation. The derivation is broken, and a broken "
        "derivation makes every assertion below vacuous."
    )
    for rel in ALL_REGISTERED:
        assert rel in tracked, (
            f"{rel} is registered but git does not track it — renamed or deleted. "
            "Update the list deliberately rather than letting the gate shrink."
        )


# --------------------------------------------------------------------------- #
# the comparison -- now one direction, over the whole tree
# --------------------------------------------------------------------------- #
def test_no_tracked_document_states_the_grant():
    """The gate. Every tracked ``.md``, outside the registered historical sections.

    This replaces both of the old per-document comparisons. With the role granting
    nothing there is no "names a bucket the policy does not grant" versus "omits one it
    does" — every positive claim is stale, so the question is simply whether any
    document still makes one.

    Scope is every tracked ``.md`` rather than a registered subset, which is the
    property that made the previous version trustworthy: an authored list silently
    omitted ``docs/rbac.md``, the user-facing statement of the boundary and the one
    document the change at the time actually edited.
    """
    offenders = {}
    for rel in _tracked_markdown():
        found = claims(rel)
        if found:
            offenders[rel] = [(c.line, c.enum) for c in found]

    assert not offenders, (
        f"these documents state that {ROLE} grants S3 on named buckets, and it grants "
        f"S3 on nothing: {offenders}.\n"
        "Each is read as a statement of current fact. The browser's own credentials "
        "reach no bucket since the document read path moved behind "
        "getFileContents / getFilePresignedUrl / getFilePresignedUrls (#1033), so the "
        "sentence has to be rewritten rather than the gate relaxed.\n"
        "⚠️ If you are writing the TRUE sentence and it is being read as a claim, put "
        "the negator immediately before the enumeration — 'nothing on the Input and "
        "Output buckets' — rather than at the start of the clause. The negation span "
        "is deliberately short; see _NEGATION_SPAN_CHARS.\n"
        "If the document is an append-only ledger whose entry describes a past "
        "release, bound that ONE section in "
        "EXCLUDED_HISTORICAL_LEDGER_SECTIONS instead of editing the entry, and add the "
        "file to DOCUMENTS_ALLOWED_A_HISTORICAL_CLAIM with one member's worth of "
        "reason."
    )


# --------------------------------------------------------------------------- #
# what the reader picks up
# --------------------------------------------------------------------------- #
# How many claims each registered document carries with its historical sections
# INCLUDED, and how many of those read as exhaustive. Pinned because the recognizer is
# a heuristic over prose: a count that GREW means it started reading sentences that are
# not statements of this grant, and one that SHRANK means a claim went out of its reach
# — which is how this gate goes quiet while still passing.
#
# These are the only documents with a claim to count, because every other document in
# the tree is required to have none (see test_no_tracked_document_states_the_grant), and
# that assertion is the one that would otherwise be silently satisfied by a broken
# reader. So this inventory is doing more work than it used to, not less.
EXPECTED_CLAIM_COUNTS_WITH_HISTORY = {
    "security/threat-modeling/README.md": (1, 0),
    "CHANGELOG.md": (1, 1),
}


def test_the_claim_inventory_is_exactly_the_expected_set():
    """What the reader picks up, pinned in both directions and per claim kind."""
    actual = {
        rel: (
            len(claims(rel, include_historical=True)),
            sum(1 for c in claims(rel, include_historical=True) if c.exhaustive),
        )
        for rel in ALL_REGISTERED
    }
    assert actual == EXPECTED_CLAIM_COUNTS_WITH_HISTORY, (
        "the set of grant claims this gate reads has changed.\n"
        f"  expected (total, exhaustive): {EXPECTED_CLAIM_COUNTS_WITH_HISTORY}\n"
        f"  actual:                       {actual}\n"
        "A total of 0 means the historical claim this exclusion exists for is no "
        "longer read, which makes the exclusion dead config — see "
        "test_the_historical_ledger_exclusion_is_still_load_bearing."
    )


def test_every_registered_document_is_fully_covered_by_its_exclusions():
    """A registered document must carry NO claim once its exclusions are applied.

    Being on :data:`DOCUMENTS_ALLOWED_A_HISTORICAL_CLAIM` is permission to have a
    *historical* claim, not permission to have a claim. A document whose exclusion does
    not cover its claim would otherwise be reported by
    :func:`test_no_tracked_document_states_the_grant` — this says the same thing with a
    message that names the exclusion, because that is the thing to fix.
    """
    for rel in ALL_REGISTERED:
        found = claims(rel)
        assert not found, (
            f"{rel} is registered as carrying only historical claims, but "
            f"{[(c.line, c.enum) for c in found]} fall OUTSIDE its registered "
            f"sections ({EXCLUDED_HISTORICAL_LEDGER_SECTIONS.get(rel, ())}). Either "
            "bound the section that holds them, or rewrite them as the statements of "
            "current fact the rest of the tree has to make."
        )


# --------------------------------------------------------------------------- #
# the historical-ledger exclusion
# --------------------------------------------------------------------------- #
def _registered_sections() -> list:
    """``(path, heading)`` for every registered historical section.

    A function rather than a module constant so the three suites below parametrise
    over the registry itself, which is where the decision is recorded. The three
    assertions are deliberately separate rather than one test with three parts: they
    fail for different reasons and the remedies differ (re-point the heading, fix the
    region's end, delete the dead entry).
    """
    return [
        (rel, heading)
        for rel, headings in sorted(EXCLUDED_HISTORICAL_LEDGER_SECTIONS.items())
        for heading in headings
    ]


_SECTION_IDS = [f"{r}:{h}" for r, h in _registered_sections()]


@pytest.mark.parametrize("rel,heading", _registered_sections(), ids=_SECTION_IDS)
def test_the_excluded_region_is_one_section_and_not_the_rest_of_the_file(rel, heading):
    """F3: the exclusion's END is asserted, not assumed.

    The registry entry and the docstring both claim the exclusion is bounded to one
    section so that a claim anywhere else in the document is still read. That is a
    property of where the region ends, and for the threat-model README it held only
    accidentally while the excluded section happened to be the last one in the file.
    ``CHANGELOG.md`` is the case where it cannot hold accidentally: its excluded
    release section has every older release after it and the whole ``[Unreleased]``
    section before it, and both must still be read.
    """
    text = (REPO_ROOT / rel).read_text()
    region = excluded_region(rel, text, heading)
    assert region is not None, (
        f"{rel} no longer contains a line equal to {heading!r}. Remove the exclusion, "
        "or point it at the section that replaced it."
    )
    start, end = region
    assert text[start:].startswith(heading), "the region must begin at the heading"

    # The region ends at the next same-level heading, or at end of file when the
    # excluded section is last. Either way, everything OUTSIDE it is still read —
    # which is the property the registry entry claims.
    following = text[end:]
    assert end == len(text) or following.startswith("## "), (
        f"the excluded region for {rel}:{heading} ends at offset {end} on "
        f"{following[:40]!r}, which is neither end-of-file nor a '## ' heading"
    )

    # The end must be COMPUTED from a heading scan, not defaulted to end-of-file. That
    # distinction is invisible while the excluded section is last, so it is asserted
    # against text that has a section after it. The region's end must move to the new
    # heading and stop there.
    #
    # Deliberately not a size assertion: the threat model's excluded section is
    # legitimately 52% of its file, because each revision row is a paragraph. "Large"
    # is not the defect; "unbounded" is.
    appended = text + "\n## A Later Section\n\nBody.\n"
    later = excluded_region(rel, appended, heading)
    assert later is not None
    later_start, later_end = later
    assert later_start == start, "the region's start must not depend on what follows it"
    assert later_end < len(appended), (
        "the excluded region still runs to end-of-file when a later section exists, so "
        "it truncates rather than bounding, and every claim below the heading is "
        "invisible to this gate"
    )


@pytest.mark.parametrize("rel,heading", _registered_sections(), ids=_SECTION_IDS)
def test_a_claim_after_the_excluded_section_would_still_be_read(rel, heading):
    """The bounded-region property, exercised rather than inferred.

    Appending a section after the excluded one and confirming the reader sees it is the
    only way to distinguish "removes one section" from "truncates at the heading".
    Driven per exclusion, not once for the first one: the two entries differ in exactly
    the way that matters — one excludes the last section of its file and the other
    excludes a section in the middle — so a single-entry probe would exercise whichever
    one happened to sort first.
    """
    original = (REPO_ROOT / rel).read_text()
    action = sorted(claim_actions())[0]
    appended = (
        original
        + "\n## Appended By A Test\n\n"
        + f"`{ROLE}` grants `{action}` on the Input, Output and Configuration "
        + "buckets.\n"
    )
    stripped = _strip_historical(rel, appended)
    assert "Appended By A Test" in stripped, (
        "text after the excluded section was removed, so the exclusion truncates the "
        "file rather than removing one section — and every claim below the heading is "
        "invisible to this gate."
    )
    assert "on the Input, Output and Configuration" in stripped, (
        "the appended grant sentence did not survive stripping, so a claim after the "
        "excluded section would not be compared"
    )


@pytest.mark.parametrize("rel,heading", _registered_sections(), ids=_SECTION_IDS)
def test_the_historical_ledger_exclusion_is_still_load_bearing(rel, heading):
    """A dead exclusion pre-exempts whatever next occupies the path.

    Each exclusion must still shield a claim the gate would otherwise flag. When one
    stops doing so — because the ledger row was reworded, or the release section was
    trimmed — this fails and that exclusion should be removed rather than kept on
    trust. Parametrised per entry: the previous version read
    ``next(iter(...))`` as a default argument, so with two entries it would have
    checked one of them and reported on neither.
    """
    text = (REPO_ROOT / rel).read_text()
    region = excluded_region(rel, text, heading)
    assert region is not None
    start, end = region
    inside = text[start:end]

    # The claim must be inside THIS region, which is what makes the entry load-bearing
    # rather than some other entry on the same file carrying it. Located by comparing
    # the claim's character offset against the region, not by re-running the reader on
    # a slice: a slice loses the preceding context the role-attribution window needs.
    with_history = claims(rel, include_historical=True)
    shielded = [
        c
        for c in with_history
        if start
        <= sum(len(line) for line in text.splitlines(keepends=True)[: c.line - 1])
        < end
    ]
    assert shielded, (
        f"the exclusion {rel}:{heading} shields no claim this gate would otherwise "
        f"read (the file's claims are at lines {[c.line for c in with_history]}, the "
        f"region covers offsets {start}-{end}), so it is dead config that pre-exempts "
        "whatever is written there next. Delete it."
    )
    # And the shielded claim really is stale, i.e. the exclusion is still *needed*.
    # With the role granting nothing, any claim qualifies — asserted anyway so that if
    # the grant ever comes back this check does not quietly become vacuous.
    granted = browser_readable_buckets()
    assert any(c.buckets - granted for c in shielded), (
        f"every claim {rel}:{heading} shields now names only buckets template.yaml "
        f"grants ({sorted(granted)}), so the exclusion is no longer needed"
    )
    assert heading in inside


# --------------------------------------------------------------------------- #
# the recognizer's own discrimination
# --------------------------------------------------------------------------- #
def test_the_claim_reader_distinguishes_a_grant_from_advice_about_buckets():
    """`_enum_to_buckets` returning ``None`` for a collective noun is what keeps the
    Macie and Cross-Region-Replication recommendations in `well-architected.md` — both
    of which say "on the ... buckets" — out of the claim set."""
    assert _enum_to_buckets("Input and Output") == {"InputBucket", "OutputBucket"}
    assert _enum_to_buckets("input and output") == {"InputBucket", "OutputBucket"}
    assert _enum_to_buckets("Input, Output and Configuration") == {
        "InputBucket",
        "OutputBucket",
        "ConfigurationBucket",
    }
    assert _enum_to_buckets("Test Set") == {"TestSetBucket"}
    assert _enum_to_buckets("Evaluation Baseline") == {"EvaluationBaselineBucket"}
    assert _enum_to_buckets("document") is None
    assert _enum_to_buckets("document and configuration") is None
    assert _enum_to_buckets("stack's") is None
    assert _enum_to_buckets("OutputLocation") is None


def _read_probe(body: str) -> list:
    """Run the claim reader over a synthetic document, via a temporary tracked-looking
    path. Written to a scratch file under the repo so `claims` can read it by relpath.
    """
    scratch = REPO_ROOT / "_iam_prose_probe.md"
    scratch.write_text(body)
    try:
        return claims("_iam_prose_probe.md")
    finally:
        scratch.unlink(missing_ok=True)


def test_the_reader_does_not_fire_on_true_sentences_near_the_grant():
    """The shapes that are true prose and must not read as claims.

    Each of these appeared, or could appear, in a document that legitimately describes
    this role. A gate that fails on them gets weakened, which is the outcome this whole
    file is trying to avoid — so they are pinned as non-claims. The last two are the
    ones this change made load-bearing: they are the shape every one of the eight
    rewritten documents now uses.
    """
    read_action = sorted(claim_actions())[0]

    # 1. A negated clause inside the grant sentence. TRUE, and the opposite of a claim.
    negated = _read_probe(
        f"The `{ROLE}` authenticated role grants `{read_action}` on the Input and "
        "Output buckets, and nothing on the Configuration bucket.\n"
    )
    assert all("Configuration" not in c.enum for c in negated), (
        "the negated clause was read as a claim, so the gate would accuse the document "
        f"of saying the role grants the Configuration bucket: {negated}"
    )
    assert [c.enum for c in negated] == ["Input and Output"], (
        f"the positive half of the same sentence must still be read: {negated}"
    )

    # 2. A different actor, in its own sentence.
    resolver = _read_probe(
        f"The `{ROLE}` authenticated role grants `{read_action}` on the Input and "
        "Output buckets. A resolver mediates reads on the Configuration bucket.\n"
    )
    assert all("Configuration" not in c.enum for c in resolver), resolver

    # 3. Operational advice in its own sentence.
    advice = _read_probe(
        f"The `{ROLE}` authenticated role grants `{read_action}` on the Input and "
        "Output buckets. Enable versioning on the Output bucket.\n"
    )
    assert len(advice) == 1, advice

    # 4. A write action the role does not hold. Not a statement about this grant.
    write = _read_probe(
        f"The `{ROLE}` authenticated role grants `{read_action}` on the Input and "
        "Output buckets. `s3:PutObject` writes land on the Logging bucket.\n"
    )
    assert all("Logging" not in c.enum for c in write), (
        "a sentence about an action this role is not granted was read as a claim "
        f"about its grant: {write}"
    )

    # 5. THE TRUE SENTENCE, in the three spellings the rewritten documents use. Every
    # one of these says the role reaches no bucket, and every one of them cites an
    # action and names buckets — so if the negation rule did not hold, the gate would
    # fail on the correct prose in all eight documents at once and the obvious remedy
    # would be to weaken it.
    for body in (
        f"`{ROLE}` grants `{read_action}` on **nothing** — not on the Input and "
        "Output buckets, and not on any other.\n",
        f"`{ROLE}` holds `{read_action}` on no bucket at all: nothing on the Input "
        "and Output buckets, which it named until the read path moved behind a "
        "resolver.\n",
        f"`{ROLE}` carries no S3 statement, so `{read_action}` is granted neither "
        "on the Input and Output buckets nor anywhere else.\n",
    ):
        assert not _read_probe(body), (
            "a TRUE sentence stating the role grants nothing was read as a claim, so "
            f"the gate fails on the correct prose: {body!r}"
        )


def test_the_reader_does_fire_on_the_stale_enumeration():
    """The counterfactual: the exact wording a bad merge reintroduces IS a claim.

    Both spellings, because both are now stale: the three-bucket one that was already
    wrong, and the two-bucket one that was correct until #1033. The second is the one
    this change creates, and it is the one a conflicting merge will reintroduce from
    eight different files.
    """
    action = sorted(claim_actions())[0]
    for enum, expected in (
        (
            "Input, Output and Configuration",
            {"InputBucket", "OutputBucket", "ConfigurationBucket"},
        ),
        ("Input and Output", {"InputBucket", "OutputBucket"}),
    ):
        stale = _read_probe(
            f"The `{ROLE}` authenticated role grants `{action}` on the {enum} "
            "buckets.\n"
        )
        assert len(stale) == 1, (enum, stale)
        assert stale[0].buckets == expected
        assert stale[0].buckets - browser_readable_buckets() == expected, (
            f"the {enum!r} enumeration no longer reads as naming an ungranted bucket, "
            "so this gate would not catch it"
        )
        assert stale[0].exhaustive


# --------------------------------------------------------------------------- #
# belt and braces, independent of the pattern
# --------------------------------------------------------------------------- #
def test_no_tracked_document_carries_the_stale_enumeration_as_a_literal():
    """A pattern-independent sweep over EVERY tracked markdown file.

    The reader above can be defeated by a rewording it does not match; this cannot. It
    covers the whole universe rather than the registered subset — an earlier version
    iterated the registered list, so the document the gate was missing was also the
    document this sweep was missing.

    ⚠️ Only the three-bucket literal is swept, and the two-bucket one is deliberately
    NOT. "Input and Output" is ordinary English about these two buckets — they hold the
    documents, so most of the corpus mentions them together — and a literal sweep for it
    would report scores of true sentences. What distinguishes a claim from a mention is
    the action and the role in the same sentence, which is the reader's job, and this
    sweep exists only for the one phrase that has no innocent reading.
    """
    granted = browser_readable_buckets()
    if "ConfigurationBucket" in granted:
        pytest.skip("the Configuration bucket is granted, so the phrase is not stale")

    offenders = {}
    for rel in _tracked_markdown():
        text = _strip_historical(rel, (REPO_ROOT / rel).read_text())
        for phrase in (
            "Input, Output and Configuration",
            "input, output and configuration",
        ):
            if phrase in text:
                offenders.setdefault(rel, []).append(phrase)

    assert not offenders, (
        f"these documents name the Configuration bucket in {ROLE}'s grant outside "
        f"their historical sections, but the role grants {sorted(granted) or 'nothing'}: "
        f"{offenders}. This is the exact stale wording a merge reintroduces."
    )


def test_the_exclusions_belong_to_registered_documents():
    """Universe closure, structurally."""
    for rel in EXCLUDED_HISTORICAL_LEDGER_SECTIONS:
        assert rel in ALL_REGISTERED, (
            f"{rel} has a historical-section exclusion but is not in "
            "DOCUMENTS_ALLOWED_A_HISTORICAL_CLAIM, so nothing records why it is "
            "allowed a claim at all"
        )
    for rel in ALL_REGISTERED:
        assert EXCLUDED_HISTORICAL_LEDGER_SECTIONS.get(rel), (
            f"{rel} is allowed a historical claim but registers no section, so the "
            "claim is not bounded to anything — it would be reported by "
            "test_every_registered_document_is_fully_covered_by_its_exclusions"
        )
    assert Path(REPO_ROOT / "template.yaml").is_file()
