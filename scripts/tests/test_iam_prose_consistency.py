# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The documents that enumerate the browser's S3 grant must agree with the template.

``test_browser_s3_grants.py`` pins the **policy**: which buckets
``CognitoAuthorizedRole`` may name. This file pins the **prose** against it. A security
deliverable asserting a grant the template does not contain is a defect of the same
kind as the reverse — a reader of ``rbac-authentication.md`` or ``docs/rbac.md`` acts on
the enumeration, and one that over-states the grant invites a reviewer to accept a
boundary narrower than they were told, or to "restore" a grant that was removed
deliberately.

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
2. the **same sentence**, before the match, cites one of the S3 actions the role is
   actually granted (derived — see ``browser_s3_policy.granted_s3_actions``). A bare
   ``s3:`` token was too weak: the role's grant is read-only, so "``s3:PutObject`` writes
   land on the Logging bucket" is not a statement about it, and reading it as one made
   the gate fail on true prose;
3. that sentence carries no **negation** before the match. "…and nothing on the
   Configuration bucket" is a true sentence *about* the grant which asserts the
   opposite of a claim, and a rule that read it as a claim accused the document of
   saying the reverse of what it says;
4. the role is named within :data:`_CONTEXT_CHARS` before it, so an unrelated bucket
   sentence elsewhere in the file is not attributed to this role.

The two directions are **not symmetric**
-----------------------------------------
* **Extra** — prose naming a bucket the policy does not grant — is asserted for every
  claim. That is the direction the defect travels in.
* **Missing** — a granted bucket the prose does not name — is asserted only for a claim
  that reads as **exhaustive** (an enumeration of two or more buckets). A deliberately
  narrow sentence is not a defect: ``reporting-analytics.md`` says the role grants
  ``s3:GetObjectVersion`` on the Output bucket, which is true and is *about* that one
  action. Asserting set equality on every claim made that sentence a failure, and the
  only documents that could be registered were the four the rule happened to fit —
  the set of documents compatible with the check rather than the set stating the grant.
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

# Documents that state this role's S3 grant in words. Each must carry at least one
# claim, so a document that stops describing the grant fails rather than silently
# dropping out of coverage. Membership is NOT the gate's scope — the scope is every
# tracked `.md` (see the module docstring); this list records which documents are
# *required* to keep saying it.
DOCUMENTS_STATING_THE_GRANT = (
    "docs/rbac.md",
    "docs/well-architected.md",
    "docs/govcloud-architecture.md",
    "docs/aws-services-and-roles.md",
    "security/threat-modeling/feature-threats/rbac-authentication.md",
    "security/threat-modeling/feature-threats/web-ui.md",
    "security/threat-modeling/feature-threats/reporting-analytics.md",
    # Enumerates the grant to argue about narrowing it, so its statement of the
    # current grant is the premise of the whole document rather than an aside.
    "docs/planning/identity-pool-group-scoping-plan.md",
)

# Read, and compared, but not required to carry a *current* claim: this document's only
# enumeration of the grant is in the historical ledger excluded below.
DOCUMENTS_WITH_HISTORICAL_CLAIMS_ONLY = ("security/threat-modeling/README.md",)

# Sections whose prose describes the past and is not a statement of current fact.
# `<path>: <markdown heading>` — bounded to ONE section: the region removed runs from
# the heading to the next same-level heading, not to end of file.
EXCLUDED_HISTORICAL_LEDGER_SECTIONS = {
    "security/threat-modeling/README.md": "## Version History",
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


def excluded_region(rel: str, text: str):
    """``(start, end)`` of the historical section to skip, or ``None``.

    The end is the next same-level (``## ``) heading, so the exclusion covers **one
    section** rather than truncating the file. Truncating is what the first version did,
    and it made every claim after the heading invisible — harmless only for as long as
    the excluded section happens to be last, which is not a property anyone maintains.

    The heading is located with a line-anchored search, not ``str.find``: the latter is
    a substring search, so the heading text quoted inside an earlier paragraph would
    move the exclusion hundreds of lines earlier with nothing to say so.
    """
    heading = EXCLUDED_HISTORICAL_LEDGER_SECTIONS.get(rel)
    if heading is None:
        return None
    start_match = re.search(rf"^{re.escape(heading)}\s*$", text, re.M)
    if start_match is None:
        return None
    next_heading = re.search(r"^## ", text[start_match.end() :], re.M)
    end = start_match.end() + next_heading.start() if next_heading else len(text)
    return start_match.start(), end


def _strip_historical(rel: str, text: str) -> str:
    """``text`` with the registered historical section blanked out.

    Replaced with newlines rather than deleted so that every line number this module
    reports still matches the file a reader will open.
    """
    region = excluded_region(rel, text)
    if region is None:
        return text
    start, end = region
    return text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]


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
    actions = granted_s3_actions()
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


ALL_REGISTERED = DOCUMENTS_STATING_THE_GRANT + DOCUMENTS_WITH_HISTORICAL_CLAIMS_ONLY


# --------------------------------------------------------------------------- #
# the universe
# --------------------------------------------------------------------------- #
def test_the_policy_names_at_least_one_bucket_and_one_action():
    """Otherwise every comparison below is against an empty set."""
    assert browser_readable_buckets(), (
        f"{ROLE} names no bucket, so this gate would assert that the documents "
        "enumerate nothing. Either the browser no longer reads S3 directly — in which "
        "case these documents need rewriting, not this gate relaxing — or the policy "
        "parser has stopped finding its subject."
    )
    assert granted_s3_actions(), (
        f"{ROLE} grants no s3: action, so no sentence can satisfy the granted-action "
        "condition and this gate would read no claims at all."
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


def test_no_document_states_the_grant_outside_the_registered_set():
    """Universe closure: every claim-bearing document is classified.

    This is what makes the registered lists trustworthy at all. Without it the gate
    covers whatever someone remembered to list, and what it missed was ``docs/rbac.md``
    — the user-facing statement of the boundary, and the file this change edited.
    """
    unregistered = {}
    for rel in _tracked_markdown():
        if rel in ALL_REGISTERED:
            continue
        found = claims(rel)
        if found:
            unregistered[rel] = [(c.line, c.enum) for c in found]

    assert not unregistered, (
        "these tracked documents state " + ROLE + "'s S3 grant but are in neither "
        "DOCUMENTS_STATING_THE_GRANT nor DOCUMENTS_WITH_HISTORICAL_CLAIMS_ONLY, so "
        f"nothing compared them against the template: {unregistered}. Add each to the "
        "first list (it will then be compared, and required to keep stating the "
        "grant), or to the second if its only enumeration is historical."
    )


# --------------------------------------------------------------------------- #
# the comparison
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", DOCUMENTS_STATING_THE_GRANT)
def test_every_registered_document_states_the_grant(rel):
    """Non-vacuity, per document.

    Without this the gate passes for a document whose claim it can no longer find —
    indistinguishable, from the outside, from a document that agrees.
    """
    assert claims(rel), (
        f"{rel} is registered as stating {ROLE}'s S3 grant, but no claim was found in "
        "it. A claim needs an enumeration of declared bucket names, one of the role's "
        f"granted actions ({sorted(granted_s3_actions())}) in the same sentence, no "
        "negation in that sentence, and the role named within "
        f"{_CONTEXT_CHARS} characters. Either the wording changed so this gate can no "
        "longer read it, or the document stopped describing the grant — in which case "
        "remove it from DOCUMENTS_STATING_THE_GRANT deliberately."
    )


@pytest.mark.parametrize("rel", ALL_REGISTERED)
def test_no_claim_names_a_bucket_the_policy_does_not_grant(rel):
    """The direction the defect travels in, asserted for every claim.

    A document naming a bucket the template does not grant is asserting a boundary the
    deployment does not implement. This is the blocker this gate exists for: the stale
    three-bucket enumeration a conflicting merge reintroduces.
    """
    granted = browser_readable_buckets()
    for claim in claims(rel):
        extra = sorted(claim.buckets - granted)
        assert not extra, (
            f"{rel}:{claim.line} says {ROLE} grants S3 on the {claim.enum!r} "
            f"buckets, naming {extra}, which template.yaml does NOT grant it "
            f"(it grants {sorted(granted)}).\n"
            "This document is read as a statement of current fact. Fix the prose to "
            "match the template, or the template to match the prose — they cannot "
            "disagree. If you are resolving a merge conflict, the template side is "
            "usually the one that auto-merged and the prose side is the stale one.\n"
            "If the sentence is *denying* that the role has that bucket, phrase the "
            "denial before the enumeration ('nothing on the X bucket') so it reads as "
            "the denial it is."
        )


@pytest.mark.parametrize("rel", ALL_REGISTERED)
def test_an_exhaustive_enumeration_names_every_granted_bucket(rel):
    """The other direction, for claims that read as a complete list only.

    Applied to multi-bucket enumerations, not to every claim. A deliberately narrow
    sentence — ``reporting-analytics.md`` on ``s3:GetObjectVersion`` and the Output
    bucket — is true and says nothing about the other buckets; asserting set equality
    on it reported correct prose as a disagreement.
    """
    granted = browser_readable_buckets()
    for claim in claims(rel):
        if not claim.exhaustive:
            continue
        missing = sorted(granted - claim.buckets)
        assert not missing, (
            f"{rel}:{claim.line} enumerates the {claim.enum!r} buckets, which reads as "
            f"the complete list, but template.yaml also grants {missing}. Either name "
            "them, or narrow the sentence to the single bucket it is about."
        )


def test_the_exhaustive_direction_is_not_vacuous():
    """At least one live claim must be exhaustive, or the check above tests nothing."""
    exhaustive = [c for rel in ALL_REGISTERED for c in claims(rel) if c.exhaustive]
    assert exhaustive, (
        "no claim in the corpus reads as an exhaustive enumeration, so "
        "test_an_exhaustive_enumeration_names_every_granted_bucket asserts nothing. "
        "Either the prose stopped enumerating the grant, or `exhaustive` no longer "
        "recognises the form it is written in."
    )


# --------------------------------------------------------------------------- #
# what the reader picks up
# --------------------------------------------------------------------------- #
# How many claims each registered document carries, and how many of those read as
# exhaustive. Pinned because the recognizer is a heuristic over prose: a count that
# GREW means it started reading sentences that are not statements of this grant, and one
# that SHRANK means a claim went out of its reach — which is how this gate goes quiet
# while still passing.
EXPECTED_CLAIM_COUNTS = {
    "docs/rbac.md": (1, 1),
    "docs/well-architected.md": (1, 1),
    "docs/govcloud-architecture.md": (1, 1),
    "docs/aws-services-and-roles.md": (1, 1),
    "security/threat-modeling/feature-threats/rbac-authentication.md": (1, 1),
    "security/threat-modeling/feature-threats/web-ui.md": (1, 1),
    "security/threat-modeling/feature-threats/reporting-analytics.md": (1, 0),
    "docs/planning/identity-pool-group-scoping-plan.md": (1, 1),
    "security/threat-modeling/README.md": (0, 0),
}


def test_the_claim_inventory_is_exactly_the_expected_set():
    """What the reader picks up, pinned in both directions and per claim kind."""
    actual = {
        rel: (len(claims(rel)), sum(1 for c in claims(rel) if c.exhaustive))
        for rel in ALL_REGISTERED
    }
    assert actual == EXPECTED_CLAIM_COUNTS, (
        "the set of grant claims this gate reads has changed.\n"
        f"  expected (total, exhaustive): {EXPECTED_CLAIM_COUNTS}\n"
        f"  actual:                       {actual}\n"
        "A total of 0 means a grant sentence is no longer read. A drop in the "
        "exhaustive count means a complete enumeration now reads as a narrow one, "
        "which silently stops the missing-bucket direction applying to it."
    )


# --------------------------------------------------------------------------- #
# the historical-ledger exclusion
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel", sorted(EXCLUDED_HISTORICAL_LEDGER_SECTIONS))
def test_the_excluded_region_is_one_section_and_not_the_rest_of_the_file(rel):
    """F3: the exclusion's END is asserted, not assumed.

    The registry entry and the docstring both claim the exclusion is bounded to one
    section so that a claim anywhere else in the document is still read. That is a
    property of where the region ends, and it held only accidentally while the excluded
    section happened to be the last one in the file.
    """
    heading = EXCLUDED_HISTORICAL_LEDGER_SECTIONS[rel]
    text = (REPO_ROOT / rel).read_text()
    region = excluded_region(rel, text)
    assert region is not None, (
        f"{rel} no longer contains a line equal to {heading!r}. Remove the exclusion, "
        "or point it at the section that replaced it."
    )
    start, end = region
    assert text[start:].startswith(heading), "the region must begin at the heading"

    # The region ends at the next same-level heading, or at end of file when the
    # excluded section is last. Either way, everything OUTSIDE it is still read —
    # which is the property the registry entry claims.
    tail = text[end:]
    assert end == len(text) or tail.startswith("## "), (
        f"the excluded region for {rel} ends at offset {end} on "
        f"{tail[:40]!r}, which is neither end-of-file nor a '## ' heading"
    )

    # The end must be COMPUTED from a heading scan, not defaulted to end-of-file. That
    # distinction is invisible while the excluded section is last — which it is — so it
    # is asserted against text that has a section after it. The region's end must move
    # to the new heading and stop there.
    #
    # Deliberately not a size assertion: the excluded section is legitimately 52% of
    # this file, because each revision row is a paragraph. "Large" is not the defect;
    # "unbounded" is.
    appended = text + "\n## A Later Section\n\nBody.\n"
    later = excluded_region(rel, appended)
    assert later is not None
    later_start, later_end = later
    assert later_start == start, "the region's start must not depend on what follows it"
    assert later_end < len(appended), (
        "the excluded region still runs to end-of-file when a later section exists, so "
        "it truncates rather than bounding, and every claim below the heading is "
        "invisible to this gate"
    )
    assert appended[later_end:].startswith("## A Later Section"), (
        f"the region ends at {appended[later_end : later_end + 40]!r} rather than at "
        "the heading that follows it"
    )


def test_a_claim_after_the_excluded_section_would_still_be_read():
    """The bounded-region property, exercised rather than inferred.

    Appending a section after the excluded one and confirming the reader sees it is the
    only way to distinguish "removes one section" from "truncates at the heading" — the
    two are indistinguishable while the excluded section is last, which it is.
    """
    rel = next(iter(EXCLUDED_HISTORICAL_LEDGER_SECTIONS))
    original = (REPO_ROOT / rel).read_text()
    actions = sorted(granted_s3_actions())
    appended = (
        original
        + "\n## Appended By A Test\n\n"
        + f"`{ROLE}` grants `{actions[0]}` on the Input, Output and Configuration "
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


def test_the_historical_ledger_exclusion_is_still_load_bearing(
    rel=next(iter(EXCLUDED_HISTORICAL_LEDGER_SECTIONS)),
):
    """A dead exclusion pre-exempts whatever next occupies the path.

    The excluded section must still contain a claim the gate would otherwise flag. When
    it stops doing so — because the ledger row was reworded, or the grant changed to
    match what the row describes — this fails and the exclusion should be removed
    rather than kept on trust.
    """
    granted = browser_readable_buckets()
    with_history = claims(rel, include_historical=True)
    without_history = claims(rel)
    keys = {(c.line, c.enum) for c in without_history}
    shielded = [c for c in with_history if (c.line, c.enum) not in keys]
    assert shielded, (
        f"the historical-section exclusion for {rel} shields nothing — no claim inside "
        "it is one this gate would otherwise read — so it is dead config that "
        "pre-exempts whatever is written there next. Delete it."
    )
    assert any(c.buckets - granted for c in shielded), (
        f"every claim the exclusion shields in {rel} now names only buckets "
        f"template.yaml grants ({sorted(granted)}), so the exclusion is no longer "
        "needed. Delete it and let the section be compared like the rest of the file."
    )


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
    """The four shapes that are true prose and must not read as claims.

    Each of these appeared, or could appear, in a document that legitimately describes
    this role. A gate that fails on them gets weakened, which is the outcome this whole
    file is trying to avoid — so they are pinned as non-claims.
    """
    actions = sorted(granted_s3_actions())
    read_action = actions[0]

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
    assert "s3:PutObject" not in granted_s3_actions()


def test_the_reader_does_fire_on_the_stale_enumeration():
    """The counterfactual: the exact wording a bad merge reintroduces IS a claim."""
    actions = sorted(granted_s3_actions())
    stale = _read_probe(
        f"The `{ROLE}` authenticated role grants `{actions[0]}` on the Input, Output "
        "and Configuration buckets.\n"
    )
    assert len(stale) == 1, stale
    assert stale[0].buckets - browser_readable_buckets() == {"ConfigurationBucket"}, (
        "the stale three-bucket enumeration no longer reads as naming an ungranted "
        "bucket, so this gate would not catch it"
    )
    assert stale[0].exhaustive


# --------------------------------------------------------------------------- #
# belt and braces, independent of the pattern
# --------------------------------------------------------------------------- #
def test_no_tracked_document_carries_the_stale_enumeration_as_a_literal():
    """A pattern-independent sweep over EVERY tracked markdown file.

    The reader above can be defeated by a rewording it does not match; this cannot. It
    covers the whole universe rather than the registered subset — the earlier version
    iterated the registered list, so the document the gate was missing was also the
    document this sweep was missing.
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
        f"their historical sections, but the role grants {sorted(granted)}: "
        f"{offenders}. This is the exact stale wording a merge reintroduces."
    )


def test_the_two_document_lists_are_disjoint_and_the_exclusions_belong_to_them():
    """Universe closure, structurally."""
    overlap = set(DOCUMENTS_STATING_THE_GRANT) & set(
        DOCUMENTS_WITH_HISTORICAL_CLAIMS_ONLY
    )
    assert not overlap, overlap
    for rel in EXCLUDED_HISTORICAL_LEDGER_SECTIONS:
        assert rel in ALL_REGISTERED, (
            f"{rel} has a historical-section exclusion but is not a registered "
            "document, so nothing reads the rest of it"
        )
    assert Path(REPO_ROOT / "template.yaml").is_file()
