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
    describe_unsynced,
    restore_committed_register,
    unsynced_dispositions,
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
