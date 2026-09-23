# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``make srt-scan`` must start from the committed disposition register.

The v0.6.8 release validation ran ``make srt-scan`` on a tree whose gitignored
``.srt/issues.json`` predated ten suppressions committed a week earlier, and the
scanner reported all ten as open HIGH findings — a false red in a security gate.
These tests pin the restore step that closes that, and the one case where it
must NOT overwrite: a local disposition nobody has saved yet.
"""

import json
import sys
from pathlib import Path

import pytest

SRT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRT_DIR))
from register import (  # noqa: E402
    NON_VACUITY_EXEMPT_SOURCES,
    WHOLE_REPO_SUMMARIES,
    describe_unsynced,
    restore_committed_register,
    suppressed_sources,
    unsynced_dispositions,
    vacuous_suppressions,
)


def _issue(path, check="LAMBDA-002", status="suppressed", priority="HIGH", name="Fn"):
    return {
        "source": "security-matrix",
        "path": path,
        "resourceType": "AWS::Lambda::Function",
        "resourceName": name,
        "check_id": check,
        "priority": priority,
        "status": status,
    }


@pytest.fixture
def paths(tmp_path):
    committed = tmp_path / "scripts" / "srt" / "issues.json"
    live = tmp_path / ".srt" / "issues.json"
    committed.parent.mkdir(parents=True)
    return committed, live


def _write(path: Path, issues):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(issues))


@pytest.mark.unit
def test_no_committed_register_is_a_noop(paths):
    committed, live = paths
    _write(live, [_issue("a.yaml", status="Open")])
    result = restore_committed_register(committed, live)
    assert result.action == "no-committed-register"
    assert json.loads(live.read_text()) == [_issue("a.yaml", status="Open")]


@pytest.mark.unit
def test_committed_register_is_copied_over_a_missing_live_file(paths):
    committed, live = paths
    _write(committed, [_issue("nested/x/template.yaml")])
    result = restore_committed_register(committed, live)
    assert result.action == "restored"
    assert result.committed_count == 1
    assert json.loads(live.read_text()) == [_issue("nested/x/template.yaml")]


@pytest.mark.unit
def test_stale_live_state_is_replaced_so_suppressions_are_seen(paths):
    """The v0.6.8 case: live predates a committed suppression → live loses."""
    committed, live = paths
    _write(committed, [_issue("scripts/security/live_checks/oidc/template.yaml")])
    # Stale live: the same finding, still Open (never saw the suppression), plus
    # thousands of low-priority rows the scanner keeps that we do not care about.
    _write(
        live,
        [
            _issue("scripts/security/live_checks/oidc/template.yaml", status="Open"),
            _issue("src/a.py", check="B101", priority="LOW", status="Open"),
        ],
    )
    result = restore_committed_register(committed, live)
    assert result.action == "restored"
    restored = json.loads(live.read_text())
    assert restored == [_issue("scripts/security/live_checks/oidc/template.yaml")]
    assert restored[0]["status"] == "suppressed"


@pytest.mark.unit
def test_an_unsaved_local_disposition_blocks_the_overwrite(paths):
    """A HIGH finding suppressed locally but not committed must not be lost."""
    committed, live = paths
    _write(committed, [_issue("nested/x/template.yaml")])
    local_only = _issue("nested/y/template.yaml", check="DDB-002", status="suppressed")
    _write(live, [_issue("nested/x/template.yaml"), local_only])
    result = restore_committed_register(committed, live)
    assert result.action == "refused"
    assert not result.ok
    assert result.unsynced == [local_only]
    # and the live file is untouched
    assert len(json.loads(live.read_text())) == 2
    # the message names it
    text = describe_unsynced(result.unsynced)
    assert "DDB-002" in text and "nested/y/template.yaml" in text


@pytest.mark.unit
def test_discard_local_overrides_the_refusal(paths):
    committed, live = paths
    _write(committed, [_issue("nested/x/template.yaml")])
    _write(live, [_issue("nested/y/template.yaml", check="DDB-002")])
    result = restore_committed_register(committed, live, discard_local=True)
    assert result.action == "restored"
    assert json.loads(live.read_text()) == [_issue("nested/x/template.yaml")]


@pytest.mark.unit
def test_only_what_fix_py_would_persist_counts_as_unsynced():
    """Open, non-HIGH and gitignored dispositions are not 'unsaved work'."""
    committed = [_issue("nested/x/template.yaml")]
    live = [
        _issue("nested/x/template.yaml"),  # known
        _issue("nested/open.yaml", status="Open"),  # not a disposition
        _issue(
            "nested/low.yaml", priority="MEDIUM", status="suppressed"
        ),  # fix.py drops
        _issue(".aws-sam/packaged.yaml", check="KMS-007"),  # gitignored artifact
        _issue("nested/real.yaml", check="S3-005"),  # the one real unsaved one
    ]
    visible = lambda i: not (i.get("path") or "").startswith(".aws-sam/")  # noqa: E731
    got = unsynced_dispositions(live, committed, is_ci_visible=visible)
    assert [i["path"] for i in got] == ["nested/real.yaml"]


@pytest.mark.unit
def test_a_resolved_entry_counts_as_a_disposition():
    """``resolved`` is a disposition too (it is what re-detects as ``reopened``)."""
    got = unsynced_dispositions(
        [_issue("nested/r.yaml", status="resolved")], committed=[]
    )
    assert len(got) == 1


@pytest.mark.unit
def test_corrupt_live_file_is_treated_as_empty(paths):
    committed, live = paths
    _write(committed, [_issue("nested/x/template.yaml")])
    live.parent.mkdir(parents=True)
    live.write_text("{not json")
    result = restore_committed_register(committed, live)
    assert result.action == "restored"
    assert json.loads(live.read_text()) == [_issue("nested/x/template.yaml")]


# --------------------------------------------------------------------------- #
# Non-vacuity: a suppression that shields nothing (issue #1149)
# --------------------------------------------------------------------------- #
#
# The register carried 52 suppressed Bandit entries that shielded nothing, every one
# of them, because each site had since been fixed in source with an inline `# nosec`
# and the entry was never removed. Two checks already guarded the register — paths are
# tracked, suppressions carry a reason — and neither asked whether an entry still
# shields anything. The suppression key carries no line, so each dead entry was
# pre-suppressing every future finding of that check in that file.
#
# The live measurement needs the scanner and so lives in `run.py`. What is testable
# offline is the decision function and, more importantly, the *closure*: every source
# in the register is either measured or has a written reason why absence is not
# evidence for it.


def _bandit(path, line=1, check="B105", status="suppressed", issue="hardcoded"):
    return {
        "source": "Bandit",
        "path": path,
        "line": line,
        "check_id": check,
        "priority": "High",
        "status": status,
        "issue": issue,
    }


def test_a_suppression_the_scan_did_not_reproduce_is_vacuous():
    register = [_bandit("a.py"), _bandit("b.py")]
    findings = [_bandit("a.py", status="Open")]
    dead = vacuous_suppressions(register, findings, sources={"Bandit"})
    assert [i["path"] for i in dead] == ["b.py"]


def test_the_line_is_not_part_of_the_match():
    """SRT keys a disposition on (path, resourceType, resourceName, check_id).

    This is why a dead entry is dangerous rather than inert, and it is also why the
    match here must ignore the line: a finding that moved down the file is the SAME
    disposition to SRT, and treating it as a new one would report a live suppression
    as dead.
    """
    dead = vacuous_suppressions(
        [_bandit("a.py", line=10)],
        [_bandit("a.py", line=400, status="Open")],
        sources={"Bandit"},
    )
    assert dead == []


def test_a_path_spelled_differently_by_the_two_producers_still_matches():
    """The register and a scanner summary are written by different code paths.

    A `./` prefix on one side would read as "no match", and this check turns "no
    match" into "delete the entry" — so a normalisation gap here deletes live
    suppressions rather than merely missing dead ones.
    """
    dead = vacuous_suppressions(
        [_bandit("a/b.py")],
        [_bandit("./a/b.py", status="Open")],
        sources={"Bandit"},
    )
    assert dead == []


def test_a_resolved_entry_is_never_reported_as_vacuous():
    """`resolved` records that something was fixed; it does not shield.

    SRT re-opens a resolved entry on re-detection, which gates. Deleting one for being
    absent would remove the record that makes a regression visible, so the check is
    scoped to `suppressed` only.
    """
    dead = vacuous_suppressions(
        [_bandit("a.py", status="resolved")], [_bandit("b.py")], sources={"Bandit"}
    )
    assert dead == []


def test_an_unmeasured_source_is_left_alone():
    dead = vacuous_suppressions(
        [_issue("template.yaml")], [_bandit("a.py")], sources={"Bandit"}
    )
    assert dead == []


def test_an_empty_finding_set_reports_nothing_dead():
    """A summary with no findings is a scanner that produced nothing, not a clean tree.

    `scanner_health` only asks whether the summary file is *fresh*, so an empty but
    fresh summary passes there and arrives here. Reading it as evidence would report
    every suppression for that source as dead at once — the one failure this check
    must not have, since its remedy is deletion.
    """
    assert vacuous_suppressions([_bandit("a.py")], [], sources={"Bandit"}) == []


def test_every_source_in_the_register_is_measured_or_has_a_reason():
    """Universe closure over the register's sources.

    This is what stops the check being narrow *and* quiet. A suppression added under a
    source that nothing measures and nothing excuses would sit outside the ratchet with
    no sign of it, which is the state the whole register was in.
    """
    with open(SRT_DIR / "issues.json", encoding="utf-8") as handle:
        committed = json.load(handle)
    measured = set(WHOLE_REPO_SUMMARIES)
    excused = set(NON_VACUITY_EXEMPT_SOURCES)
    unaccounted = sorted(suppressed_sources(committed) - measured - excused)
    assert not unaccounted, (
        f"scripts/srt/issues.json has suppressed entries from source(s) {unaccounted}, "
        "which are neither measured for non-vacuity (register.WHOLE_REPO_SUMMARIES) nor "
        "recorded as unmeasurable (register.NON_VACUITY_EXEMPT_SOURCES). Add the source "
        "to the first if a scan's silence about it means the finding is gone, or to the "
        "second with the mechanism by which its finding set moves without the tree "
        "moving."
    )
    overlap = sorted(measured & excused)
    assert not overlap, (
        f"{overlap} is both measured and excused, so the reason written for it excuses "
        "a check that runs anyway — one of the two is wrong."
    )


def test_every_unmeasurable_source_is_one_the_register_actually_uses():
    """Non-vacuity for the excuse list itself.

    An entry for a source no suppression uses excuses nothing and pre-excuses whatever
    next arrives under that name — the same shape as the dead suppressions this whole
    check is about.
    """
    with open(SRT_DIR / "issues.json", encoding="utf-8") as handle:
        committed = json.load(handle)
    present = suppressed_sources(committed)
    stale = sorted(set(NON_VACUITY_EXEMPT_SOURCES) - present)
    assert not stale, (
        f"register.NON_VACUITY_EXEMPT_SOURCES excuses {stale}, which no suppressed "
        "entry names. Delete them."
    )
    empty = sorted(
        s for s, why in NON_VACUITY_EXEMPT_SOURCES.items() if not why.strip()
    )
    assert not empty, f"{empty} are excused with no reason written"


def test_the_bandit_suppressions_that_were_swept_are_gone():
    """Staleness for the sweep, in the direction that would not show up otherwise.

    The 52 dead entries were all `suppressed` Bandit findings. Their absence is not
    something the live check can assert — it has nothing to compare against offline —
    so this pins the outcome: a Bandit suppression reappearing in the committed
    register is a decision to re-open that class, and it has to be a deliberate edit
    here rather than a paste from `make srt-fix`.
    """
    with open(SRT_DIR / "issues.json", encoding="utf-8") as handle:
        committed = json.load(handle)
    bandit_suppressions = [
        f"{i.get('check_id')} {i.get('path')}:{i.get('line')}"
        for i in committed
        if i.get("source") == "Bandit"
        and (i.get("status") or "").lower() == "suppressed"
    ]
    assert not bandit_suppressions, (
        "scripts/srt/issues.json carries Bandit suppressions again:\n  "
        + "\n  ".join(bandit_suppressions)
        + "\n\nEvery one of the 52 that were there shielded nothing, because the site "
        "was fixed in source with an inline `# nosec` and the entry was never removed. "
        "A per-line `# nosec` with a reason is the mitigation for a real false positive "
        "here; for B105/B106 in test code that no deployment artifact is built from, "
        "ci_paths.NAME_HEURISTIC_EXEMPT already reports them without gating. If a "
        "register entry really is the right answer, delete this test and say why."
    )
