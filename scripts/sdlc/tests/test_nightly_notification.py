# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A scheduled nightly must reach Slack even when the deploy stage never runs.

The Slack post lives in `integration_tests`' `after_script`, so it can only
describe outcomes it is positioned to see. On 2026-10-14 the 20:03 scheduled run
failed in `package_tests` — Claude Sonnet 4 reached the end-of-life date AWS
published for it, so the LEGACY expiry alarm in
`scripts/tests/test_model_surface_consistency.py` fired with **no code change at
all**. GitLab skipped `deployment_validation` and `integration_tests`, the
`after_script` never ran, and the channel got nothing.

A silent nightly is indistinguishable from a nightly nobody scheduled, which is
the worst available shape for a notification whose entire purpose is that
somebody has to watch the run. It is also the same defect class as the
`✅ PASSED`-on-handoff bug: a notifier reporting the status of the thing it
happens to be attached to rather than the status of the run.

`nightly_notification` closes it, and these tests pin the four properties that
make it work. Each one, if broken, restores the silence rather than failing
loudly — which is why they are asserted here instead of being left to review.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_CONFIG = REPO_ROOT / ".gitlab-ci.yml"
JOB = "nightly_notification"
POSTED_MARKER = "slack_posted.txt"


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load(CI_CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job(config):
    assert JOB in config, (
        f"{JOB} is gone from .gitlab-ci.yml, so a nightly that fails before the "
        "deploy stage is silent again."
    )
    return config[JOB]


@pytest.fixture(scope="module")
def script(job):
    assert len(job["script"]) == 1
    return job["script"][0]


def test_the_notify_stage_runs_after_the_deploy_stage(config, job):
    stages = config["stages"]
    assert job["stage"] in stages
    assert stages.index(job["stage"]) > stages.index("integration_tests"), (
        "the notify stage must come AFTER integration_tests, or it cannot know "
        "whether that job posted."
    )


def test_it_runs_even_when_an_earlier_stage_failed(job):
    """`when: always` is the whole mechanism — without it this job is skipped too."""
    scheduled = [rule for rule in job["rules"] if "schedule" in str(rule.get("if", ""))]
    assert scheduled, f"{JOB} no longer has a scheduled-pipeline rule"
    assert all(rule.get("when") == "always" for rule in scheduled), (
        "the scheduled rule must be `when: always`. On any other value GitLab "
        "skips this job when an upstream stage fails, which is precisely the "
        "case it exists to report."
    )


def test_it_is_confined_to_the_scheduled_run(job):
    assert job["rules"][-1] == {"when": "never"}, (
        "without a terminal `when: never` this would post on merge-request and "
        "push pipelines too."
    )


def test_it_cannot_redden_a_nightly(job):
    scheduled = [r for r in job["rules"] if "schedule" in str(r.get("if", ""))]
    assert all(r.get("allow_failure") is True for r in scheduled), (
        "a notification problem must not be the thing that fails a nightly."
    )


def test_it_declares_no_needs(job):
    """`needs` on a skipped job skips the dependent job, restoring the silence."""
    assert "needs" not in job, (
        f"{JOB} must NOT declare `needs`. GitLab skips a job whose needed job was "
        "skipped, so a `needs: [integration_tests]` here would reproduce the exact "
        "defect this job removes. It relies on default artifact download from "
        "earlier stages instead, and keys on the artifacts being absent."
    )


def test_integration_tests_writes_the_marker_only_on_a_successful_post(config):
    after = "\n".join(config["integration_tests"]["after_script"])
    assert POSTED_MARKER in after, (
        "integration_tests no longer records that it posted, so the fallback "
        "cannot tell and will double-post every successful nightly."
    )
    # The marker must be written in the success branch of the curl, not
    # unconditionally: a failed post leaves the channel empty, and that is a
    # case the fallback should still cover.
    success_branch = after[after.index('"💬 Nightly result posted to Slack"') :]
    failure_branch = success_branch[success_branch.index("else") :]
    assert POSTED_MARKER in success_branch[: success_branch.index("else")], (
        f"{POSTED_MARKER} must be written where the post SUCCEEDED."
    )
    assert POSTED_MARKER not in failure_branch, (
        f"{POSTED_MARKER} is written even when the Slack post failed, so the "
        "fallback stays quiet and nothing reaches the channel."
    )


def test_the_marker_is_published_as_an_artifact(config):
    artifacts = config["integration_tests"]["artifacts"]
    assert POSTED_MARKER in artifacts["paths"], (
        f"{POSTED_MARKER} is not an artifact, so the notify job in a later stage "
        "cannot see it and will double-post."
    )
    assert artifacts.get("when") == "always", (
        "artifacts must upload on failure too, or the marker and the deployment "
        "summary are both unavailable to the fallback on the runs that matter."
    )


def test_the_fallback_stays_quiet_when_the_post_already_happened(script):
    assert f"-f {POSTED_MARKER}" in script, (
        "the fallback does not check the marker, so every successful nightly gets "
        "two Slack messages."
    )


def test_it_posts_without_curl(script):
    """curl is absent from the job's image; integration_tests apt-installs it."""
    assert "urllib" in script
    assert not re.search(r"\bcurl\b", script), (
        "curl is NOT in python:3.13-bookworm and this job has no before_script to "
        "install it. A `curl: command not found` here restores the silence."
    )


def test_it_distinguishes_a_skipped_deploy_from_an_undelivered_result(script):
    """Two situations reach this job and they are not the same message."""
    assert "deployment_summary.txt" in script
    assert "FAILED BEFORE THE DEPLOY" in script
    assert "RESULT NOT DELIVERED" in script


def test_the_skipped_deploy_message_does_not_claim_a_test_result(script):
    assert "NOTHING was" in script and "no integration test ran" in script, (
        "the message for a pipeline that never deployed must say so explicitly — "
        "the failure mode being fixed is a reader assuming silence means fine."
    )


def test_the_payload_keys_match_the_slack_workflow_contract(script):
    """A Workflow Builder trigger takes a flat object with exactly these keys."""
    body = textwrap.dedent(
        re.search(r"<<'PY'\n(.*?)\n\s*PY\s*$", script, re.S).group(1)
    )
    for key in ("status", "commit", "job_url", "summary"):
        assert f'"{key}"' in body, (
            f"the Slack Workflow Builder trigger declares {key!r}; renaming it "
            "posts a blank variable rather than failing."
        )


def test_the_embedded_python_compiles(script):
    """A syntax error here is only discovered by a failing nightly."""
    body = textwrap.dedent(
        re.search(r"<<'PY'\n(.*?)\n\s*PY\s*$", script, re.S).group(1)
    )
    compile(body, "nightly_notification", "exec")


def test_the_heredoc_terminator_is_unindented(script):
    """`<<'PY'` (unlike `<<-`) needs the terminator at column 0 to terminate."""
    lines = script.split("\n")
    assert "PY" in lines, (
        "the heredoc terminator is indented after YAML's block-scalar dedent, so "
        "the shell never closes it and the job fails to parse at runtime."
    )
