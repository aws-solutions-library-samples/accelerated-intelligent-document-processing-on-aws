# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The documents that enumerate the browser's S3 grant must agree with the template.

``test_browser_s3_grants.py`` pins the **policy**: which buckets
``CognitoAuthorizedRole`` may name. This file pins the **prose** against it. Five
documents state that grant in words, and a security deliverable asserting a grant the
template does not contain is a defect of the same kind as the reverse — a reader of
`rbac-authentication.md` or `well-architected.md` acts on the enumeration, and one that
over-states the grant invites a reviewer to accept a boundary that is narrower than
they were told, or to "restore" a grant that was removed deliberately.

**Why a gate and not a careful edit.** Nothing else in this tree compares the two.
`check_prose_counts` in ``build_threat_model.py`` reads threat totals, statuses, risk
bands and STRIDE tallies — no policy content. ``test_browser_s3_grants.py`` reads only
``template.yaml``. So the whole tree can be green while the policy and the prose
disagree, and that is not hypothetical: it is precisely what a conflicting merge
produces when the side that auto-merges is the template and the side offering a
choosable stale hunk is the prose. Merge order is not a control; this is.

**What a claim is.** A sentence of the form ``… s3:<action> … on the <enum> bucket(s)``
where the same neighbourhood names the role. The enumeration is read as a claim only
when **every** word in it maps to a bucket this stack declares — which is what keeps
the collective nouns out ("the *document* buckets", "the *stack's* buckets" and the S3
Cross-Region-Replication and Macie recommendations elsewhere in
``well-architected.md`` are advice about buckets, not statements of this role's grant).

**What is deliberately not read.** The ``Version History`` section of
``security/threat-modeling/README.md``. That table is one of this repository's two
sanctioned historical ledgers, and its rows describe grants **as they were** — the v3.7
row says in terms that the Configuration bucket *was* on this role, which is true of the
past and would be a false positive here. Excluding a whole section is a blunt
instrument, so the exclusion is itself asserted: the section must exist, and it must
contain a claim that the gate would otherwise have flagged. An exclusion that stops
shielding anything is dead, and a dead exclusion pre-exempts whatever next occupies the
path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from browser_s3_policy import REPO_ROOT, ROLE, browser_readable_buckets

pytestmark = pytest.mark.unit

# Every bucket this stack declares, as the English name the documents use -> its
# CloudFormation logical id. Derived-looking but authored on purpose: prose does not
# contain logical ids, and the mapping from "Test Set" to `TestSetBucket` is a fact
# about English rather than about the template. Non-vacuity below guarantees the
# entries that matter are exercised.
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
# claim (see `test_every_registered_document_states_the_grant`), so a document that
# stops describing the grant fails here rather than silently dropping out of coverage.
DOCUMENTS_STATING_THE_GRANT = (
    "security/threat-modeling/feature-threats/rbac-authentication.md",
    "docs/well-architected.md",
    "docs/govcloud-architecture.md",
    "docs/aws-services-and-roles.md",
)

# Registered, and read, but not required to carry a *current* claim: its only
# enumeration of this grant is in the historical ledger excluded below. Listed rather
# than omitted so that a current claim added to it later is still checked.
DOCUMENTS_WITH_HISTORICAL_CLAIMS_ONLY = ("security/threat-modeling/README.md",)

# Sections whose prose describes the past and is not a statement of current fact.
# `<path>: <markdown heading>` — bounded to one section of one file, not a file-wide
# skip, so a claim anywhere else in that document is still read.
EXCLUDED_HISTORICAL_LEDGER_SECTIONS = {
    "security/threat-modeling/README.md": "## Version History",
}

# `on the <enum> bucket(s)`. `\s+` rather than a literal space because every one of
# these documents is hard-wrapped, so the enumeration and the word "buckets" are
# routinely on different lines — a literal-space pattern found no claim at all in
# `govcloud-architecture.md`, which is the quietest way for this gate to pass.
_CLAIM_RE = re.compile(
    r"on the ((?:[A-Za-z]+[\s,]+|and\s+)*?[A-Za-z]+)\s+buckets?\b", re.I
)

# How far back to look for the two markers that make an enumeration a statement of
# THIS role's grant rather than of anything else about buckets.
#
# 600 rather than a tighter figure because in `aws-services-and-roles.md` the role is
# named in the parent bullet and the grant is two bullets below it, 414 characters
# away. Widening the window does not loosen the rule: the `s3:` marker is still
# required, and the enumeration must still consist entirely of bucket names, which is
# what excludes every "on the ... buckets" phrase in this corpus that is advice about
# buckets rather than a statement of this role's grant. The claim inventory the window
# produces is pinned by `test_the_claim_inventory_is_exactly_the_expected_set`, so a
# widening that started picking up unrelated prose fails rather than passing quietly.
_CONTEXT_CHARS = 600
_ROLE_MARKERS = (ROLE.lower(), "authenticated role")


def _strip_historical(rel: str, text: str) -> str:
    """``text`` with any registered historical-ledger section removed."""
    heading = EXCLUDED_HISTORICAL_LEDGER_SECTIONS.get(rel)
    if heading is None:
        return text
    index = text.find(heading)
    return text if index < 0 else text[:index]


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


def _claims(rel: str, *, include_historical: bool = False):
    """``(line, enum, buckets)`` for every grant claim in one document."""
    full = (REPO_ROOT / rel).read_text()
    text = full if include_historical else _strip_historical(rel, full)
    out = []
    for match in _CLAIM_RE.finditer(text):
        before = text[max(0, match.start() - _CONTEXT_CHARS) : match.start()]
        lowered = before.lower()
        if "s3:" not in lowered:
            continue
        if not any(marker in lowered for marker in _ROLE_MARKERS):
            continue
        buckets = _enum_to_buckets(match.group(1))
        if buckets is None:
            continue
        out.append((text[: match.start()].count("\n") + 1, match.group(1), buckets))
    return out


ALL_REGISTERED = DOCUMENTS_STATING_THE_GRANT + DOCUMENTS_WITH_HISTORICAL_CLAIMS_ONLY


def test_every_registered_document_exists():
    """A renamed document must fail here, not narrow the gate silently."""
    for rel in ALL_REGISTERED:
        assert (REPO_ROOT / rel).is_file(), (
            f"{rel} is registered as stating {ROLE}'s S3 grant but does not exist. "
            "Update this list deliberately rather than letting the gate shrink."
        )


def test_the_policy_names_at_least_one_bucket():
    """Otherwise every comparison below is against an empty set."""
    assert browser_readable_buckets(), (
        f"{ROLE} names no bucket, so this gate would assert that the documents "
        "enumerate nothing. Either the browser no longer reads S3 directly — in which "
        "case these documents need rewriting, not this gate relaxing — or the policy "
        "parser has stopped finding its subject."
    )


@pytest.mark.parametrize("rel", DOCUMENTS_STATING_THE_GRANT)
def test_every_registered_document_states_the_grant(rel):
    """Non-vacuity, per document.

    Without this the gate passes for a document whose claim it can no longer find —
    which is indistinguishable, from the outside, from a document that agrees.
    """
    assert _claims(rel), (
        f"{rel} is registered as stating {ROLE}'s S3 grant, but no claim of the form "
        '"s3:<action> ... on the <Input and Output> buckets" was found in it. Either '
        "the wording changed so this gate can no longer read it (fix the pattern, or "
        "restore an enumeration), or the document stopped describing the grant, in "
        "which case remove it from DOCUMENTS_STATING_THE_GRANT deliberately."
    )


@pytest.mark.parametrize("rel", ALL_REGISTERED)
def test_the_prose_enumerates_exactly_what_the_policy_grants(rel):
    """The blocker this gate exists for.

    A document that names a bucket the policy does not grant is asserting a boundary
    the template does not implement; one that omits a bucket the policy does grant
    understates the reach a reader is being asked to accept. Both are wrong and this
    fails on either.
    """
    granted = browser_readable_buckets()
    for line, enum, buckets in _claims(rel):
        extra = sorted(buckets - granted)
        missing = sorted(granted - buckets)
        assert not extra and not missing, (
            f"{rel}:{line} says {ROLE} grants S3 on the {enum!r} buckets, but "
            f"template.yaml grants it on {sorted(granted)}.\n"
            + (f"  Named in prose but NOT granted: {extra}\n" if extra else "")
            + (f"  Granted but NOT named in prose: {missing}\n" if missing else "")
            + "This document is read as a statement of current fact. Fix the prose to "
            "match the template, or the template to match the prose — but they cannot "
            "disagree. If you are resolving a merge conflict, the template side is "
            "usually the one that auto-merged and the prose side is the stale one."
        )


# How many current grant claims each registered document carries. Pinned because the
# context window above is a heuristic: a widening that began absorbing unrelated prose
# would add claims here, and a rewording that took a claim out of the pattern's reach
# would remove one. Either way the gate's reach changed and that should be a decision.
EXPECTED_CLAIM_COUNTS = {
    "security/threat-modeling/feature-threats/rbac-authentication.md": 1,
    "docs/well-architected.md": 1,
    "docs/govcloud-architecture.md": 1,
    "docs/aws-services-and-roles.md": 1,
    "security/threat-modeling/README.md": 0,
}


def test_the_claim_inventory_is_exactly_the_expected_set():
    """What the reader picks up, pinned in both directions.

    A count that GREW means the pattern or the window started reading prose that is not
    a statement of this grant — which would make the gate fail for the wrong reason and
    invite someone to weaken it. A count that SHRANK means a claim went out of reach,
    which is how a gate goes quiet while still passing.
    """
    actual = {rel: len(_claims(rel)) for rel in ALL_REGISTERED}
    assert actual == EXPECTED_CLAIM_COUNTS, (
        "the set of grant claims this gate reads has changed.\n"
        f"  expected: {EXPECTED_CLAIM_COUNTS}\n"
        f"  actual:   {actual}\n"
        "If you reworded a grant statement, check the new wording is still read (a "
        "count of 0 means it is not). If you added one, update this map."
    )


@pytest.mark.parametrize("rel", sorted(EXCLUDED_HISTORICAL_LEDGER_SECTIONS))
def test_the_historical_ledger_exclusion_is_still_load_bearing(rel):
    """A dead exclusion pre-exempts whatever next occupies the path.

    The excluded section must still contain a claim the gate would otherwise flag. When
    it stops doing so — because the ledger row was reworded, or the grant changed to
    match what the row describes — this fails and the exclusion should be removed
    rather than kept on trust.
    """
    heading = EXCLUDED_HISTORICAL_LEDGER_SECTIONS[rel]
    full = (REPO_ROOT / rel).read_text()
    assert heading in full, (
        f"{rel} no longer contains the section {heading!r} that this gate excludes. "
        "Remove the exclusion, or point it at the section that replaced it."
    )

    granted = browser_readable_buckets()
    with_history = _claims(rel, include_historical=True)
    without_history = _claims(rel)
    shielded = [c for c in with_history if c not in without_history]
    assert shielded, (
        f"the {heading!r} exclusion for {rel} shields nothing — no claim inside it is "
        "one this gate would otherwise read — so it is dead config that pre-exempts "
        "whatever is written there next. Delete it."
    )
    assert any(buckets != granted for _line, _enum, buckets in shielded), (
        f"every claim the {heading!r} exclusion shields in {rel} now agrees with "
        f"template.yaml ({sorted(granted)}), so the exclusion is no longer needed. "
        "Delete it and let the section be checked like the rest of the file."
    )


def test_the_claim_reader_distinguishes_a_grant_from_advice_about_buckets():
    """The pattern's own discrimination, asserted rather than assumed.

    `_enum_to_buckets` returning ``None`` for a collective noun is what keeps the
    Macie and Cross-Region-Replication recommendations in `well-architected.md` — both
    of which say "on the ... buckets" — out of the claim set. If it started reading
    those, this gate would fail for reasons that have nothing to do with the grant, and
    the natural response would be to weaken it.
    """
    assert _enum_to_buckets("Input and Output") == {"InputBucket", "OutputBucket"}
    assert _enum_to_buckets("input and output") == {"InputBucket", "OutputBucket"}
    assert _enum_to_buckets("Input, Output and Configuration") == {
        "InputBucket",
        "OutputBucket",
        "ConfigurationBucket",
    }
    assert _enum_to_buckets("Test Set") == {"TestSetBucket"}
    assert _enum_to_buckets("Evaluation Baseline") == {"EvaluationBaselineBucket"}
    # Collective nouns and unrelated phrases name no bucket.
    assert _enum_to_buckets("document") is None
    assert _enum_to_buckets("document and configuration") is None
    assert _enum_to_buckets("stack's") is None
    assert _enum_to_buckets("OutputLocation") is None


def test_the_gate_would_catch_the_stale_enumeration_it_exists_for():
    """Drive the comparison directly with the exact stale wording.

    The failure mode is a merge resolving the prose to a side that still says
    "Input, Output and Configuration" while the template says two buckets. Asserting
    the comparison rejects that pairing keeps this gate honest without mutating any
    tracked file.
    """
    granted = {"InputBucket", "OutputBucket"}
    stale = _enum_to_buckets("Input, Output and Configuration")
    assert stale is not None and stale - granted == {"ConfigurationBucket"}, (
        "the stale three-bucket enumeration no longer reads as naming a bucket the "
        "two-bucket policy does not grant, so this gate would not catch it"
    )


def test_the_threat_model_version_history_has_one_row_per_version():
    """Two concurrent changes can both append the same version row with no conflict.

    Both sides bump the header to the same `Version` and then each appends its own
    `| 3.7 | ... |` row. Identical header lines auto-merge, the two rows are added at
    different offsets so git sees no conflict, and the export rebuilds cleanly — so the
    ledger ends up with two rows claiming to be the same revision and nothing says so.
    This is cheap to detect and there is no legitimate reason for a duplicate.
    """
    readme = REPO_ROOT / "security/threat-modeling/README.md"
    text = readme.read_text()
    heading = "## Version History"
    assert heading in text, f"{readme.name} has no {heading!r} section"
    history = text[text.index(heading) :]

    rows = re.findall(r"^\|\s*(\d+\.\d+)\s*\|", history, re.M)
    assert rows, "the Version History table has no version rows"
    duplicates = sorted({v for v in rows if rows.count(v) > 1})
    assert not duplicates, (
        f"the threat-model Version History lists {duplicates} more than once. Two "
        "changes in flight each appended a row for the same version; merge them into "
        "one row and bump the later one, then re-run "
        "security/threat-modeling/scripts/build_threat_model.py."
    )

    header = re.search(r"^\|\s*\*\*Version\*\*\s*\|\s*(\d+\.\d+)\s*\|", text, re.M)
    assert header, "no **Version** field in the document-information table"
    newest = max(rows, key=lambda v: tuple(int(p) for p in v.split(".")))
    assert header.group(1) == newest, (
        f"the document-information table says Version {header.group(1)} but the "
        f"newest Version History row is {newest}. One of the two was not updated."
    )


def test_the_documents_do_not_name_a_removed_bucket_anywhere_in_a_grant():
    """A belt-and-braces sweep for the one phrasing that shipped wrong before.

    The claim reader above is pattern-bound, so it can be defeated by a rewording it
    does not match. This looks for the specific stale enumeration as a literal, across
    every registered document *including* the historical sections, and allows it only
    where the ledger legitimately describes the past.
    """
    granted = browser_readable_buckets()
    if "ConfigurationBucket" in granted:
        pytest.skip("the Configuration bucket is granted, so the phrase is not stale")

    for rel in ALL_REGISTERED:
        text = _strip_historical(rel, (REPO_ROOT / rel).read_text())
        for phrase in (
            "Input, Output and Configuration",
            "input, output and configuration",
        ):
            assert phrase not in text, (
                f"{rel} still says {phrase!r} outside its historical sections, but "
                f"{ROLE} grants {sorted(granted)}. This is the exact stale wording a "
                "merge reintroduces."
            )


def test_no_registered_document_is_also_excluded_wholesale():
    """Universe closure: a path cannot be in both lists, and both lists are read."""
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
