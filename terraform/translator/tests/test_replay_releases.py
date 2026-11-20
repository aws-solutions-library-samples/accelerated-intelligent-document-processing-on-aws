"""Offline tests for ``terraform/tools/replay_releases.py --check``, the release
replay `make terraform-check` runs (``make terraform-replay-releases``).

The gate replays the current translator over the template tree of every release in
``terraform/tools/replay_releases.json``, each at its recorded commit, and must fail
on a translation failure, on a template it could not read, and on a release whose
commit is not in the clone. One real release is replayed here as well, so the
check's own reading of the translator's output is exercised end to end.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "terraform" / "tools" / "replay_releases.py"


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location("_replay_releases", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _result(tag="v0.6.15", **kw):
    return {
        "tag": tag,
        "types": ["AWS::S3::Bucket"],
        "failures": {},
        "unmapped": [],
        "shape": None,
        **kw,
    }


# -- the releases file ---------------------------------------------------------------


def test_the_committed_releases_file_lists_releases_at_commits(replay):
    releases = replay.load_releases()
    assert releases, "a gate over no release would pass having replayed nothing"
    assert all(t.startswith("v0.6.") for t in releases)
    # Every v0.6 release from the first API Gateway release on, with no gap: the
    # list is meant to be every release a deployment may still be on.
    numbers = sorted(int(t.rsplit(".", 1)[1]) for t in releases)
    assert numbers == list(range(numbers[0], numbers[-1] + 1))
    assert numbers[0] == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"releases": {}},
        {"releases": []},
        {"releases": {"v0.6.1": "abc"}},
        {"releases": {"0.6.1": "a" * 40}},
        {"releases": {"v0.6.1": "A" * 40}},
    ],
)
def test_an_empty_or_malformed_releases_file_is_refused(replay, tmp_path, body):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError):
        replay.load_releases(path)


# -- the verdict -----------------------------------------------------------------------


def test_a_clean_replay_passes(replay):
    assert replay.check_problems([_result(), _result("v0.6.14")]) == []


@pytest.mark.parametrize(
    "result, words",
    [
        (
            _result(failures={"unmapped type": ["x has no hashicorp/aws mapping"]}),
            "unmapped type",
        ),
        (_result(shape="cannot read template"), "template not read"),
        (_result(types=[]), "no resource types"),
        (
            {"tag": "v0.6.15", "missing": "commit c is not in this clone"},
            "not in this clone",
        ),
    ],
)
def test_each_way_of_not_translating_a_release_fails(replay, result, words):
    problems = replay.check_problems([_result("v0.6.14"), result])
    assert len(problems) == 1 and words in problems[0] and "v0.6.15" in problems[0]


def test_a_reported_failure_that_was_not_read_fails(replay):
    """The translator says how many failures it found; a failure that reached
    somewhere this does not read must not leave the release looking clean."""
    assert replay.check_problems([_result(reported=0)]) == []
    problems = replay.check_problems([_result(reported=2)])
    assert problems and "reported 2 failure(s)" in problems[0]
    # An error spanning lines reads as more lines than reported; that is not one.
    many = _result(reported=1, failures={"other": ["first line", "second line"]})
    assert not any("reported" in p for p in replay.check_problems([many]))


def test_the_replay_reads_the_translators_count_from_its_output(
    replay, monkeypatch, tmp_path
):
    """replay() against a stubbed translator that reports 3 failures and prints
    2 of them where the gate reads: the count must come from the output, and the
    shortfall must fail the release."""
    monkeypatch.setattr(replay, "extract", lambda rev, dest: None)

    class _Done:
        def __init__(self, stdout):
            self.returncode, self.stdout, self.stderr = 0, stdout, ""

    def fake_run(argv, **kwargs):
        if "-c" in argv:
            return _Done('{"AWS::S3::Bucket": ["BucketName"]}\n')
        return _Done(
            "A: AWS::X::Y has no hashicorp/aws mapping\n"
            "B: unsupported intrinsic Fn::Foo\n"
            "3 failure(s); {}\n"
        )

    monkeypatch.setattr(replay.subprocess, "run", fake_run)
    result = replay.replay("v0.6.15", "c" * 40)
    assert result["reported"] == 3
    problems = replay.check_problems([result])
    assert any("reported 3 failure(s) and only 2 were read" in p for p in problems)


def test_a_count_one_above_what_was_read_fails_and_equal_passes(replay):
    lines = {"other": ["a", "b"]}
    assert not any(
        "reported" in p
        for p in replay.check_problems([_result(reported=2, failures=lines)])
    )
    assert any(
        "reported 3" in p
        for p in replay.check_problems([_result(reported=3, failures=lines)])
    )


def test_the_replay_records_the_translators_own_count(replay):
    releases = replay.load_releases()
    tag = max(releases, key=lambda t: int(t.rsplit(".", 1)[1]))
    if not replay.commit_present(releases[tag]):
        pytest.skip(f"{releases[tag]} is not in this clone")
    assert replay.replay(tag, releases[tag])["reported"] == 0


def test_a_commit_missing_from_the_clone_fails_the_check_and_is_not_replayed(
    replay, monkeypatch
):
    replayed = []
    monkeypatch.setattr(replay, "replay", lambda tag, rev=None: replayed.append(tag))
    results, problems = replay.run_check({"v0.6.15": "0" * 40}, jobs=1)
    assert replayed == []
    assert problems and "not in this clone" in problems[0]


def test_each_release_is_replayed_at_its_recorded_commit(replay, monkeypatch):
    seen = {}
    monkeypatch.setattr(replay, "commit_present", lambda commit: True)
    monkeypatch.setattr(
        replay,
        "replay",
        lambda tag, rev=None: seen.setdefault(tag, rev) and _result(tag),
    )
    replay.run_check({"v0.6.14": "b" * 40, "v0.6.15": "c" * 40}, jobs=2)
    assert seen == {"v0.6.14": "b" * 40, "v0.6.15": "c" * 40}


def test_the_cli_exits_1_on_a_problem_and_0_on_none(replay, monkeypatch, capsys):
    monkeypatch.setattr(replay, "load_releases", lambda: {"v0.6.15": "c" * 40})
    monkeypatch.setattr(replay, "_hand_mapped", lambda: set())
    monkeypatch.setattr(
        replay, "run_check", lambda releases, jobs: ([_result()], ["v0.6.15: broke"])
    )
    assert replay.main(["--check"]) == 1
    assert "❌ v0.6.15: broke" in capsys.readouterr().out
    monkeypatch.setattr(replay, "run_check", lambda releases, jobs: ([_result()], []))
    assert replay.main(["--check"]) == 0


def test_check_takes_no_tags(replay):
    with pytest.raises(SystemExit):
        replay.main(["--check", "--tags", "v0.6.1"])


# -- one real release, end to end --------------------------------------------------------


def test_the_newest_listed_release_replays_cleanly_today(replay):
    """The real translator over the real template tree of the newest listed
    release, read by the same code the gate uses. Skipped only in a clone that
    lacks the commit (a shallow one); the gate itself fails there instead."""
    releases = replay.load_releases()
    tag = max(releases, key=lambda t: int(t.rsplit(".", 1)[1]))
    if not replay.commit_present(releases[tag]):
        pytest.skip(f"{releases[tag]} is not in this clone")
    result = replay.replay(tag, releases[tag])
    assert len(result["types"]) > 50
    assert replay.check_problems([result]) == []


# -- the wiring ------------------------------------------------------------------------


def test_terraform_check_runs_the_replay_in_check_mode():
    dry = subprocess.run(
        ["make", "-n", "--no-print-directory", "terraform-check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "terraform-replay-releases" in dry
    target = subprocess.run(
        ["make", "-n", "--no-print-directory", "terraform-replay-releases"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert re.search(r"terraform/tools/replay_releases\.py --check\b", target)


def test_both_ci_jobs_check_out_the_history_the_replay_reads():
    gitlab = yaml.safe_load((REPO / ".gitlab-ci.yml").read_text())
    assert str(gitlab["terraform_checks"]["variables"]["GIT_DEPTH"]) == "0"
    assert "make terraform-check" in gitlab["terraform_checks"]["script"]
    github = yaml.safe_load(
        (REPO / ".github" / "workflows" / "developer-tests.yml").read_text()
    )
    steps = github["jobs"]["terraform_checks"]["steps"]
    checkout = next(
        s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0
    assert any(s.get("run") == "make terraform-check" for s in steps)
