# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The deployment-variant probes run on the weekly variant-probe schedule only.

The nightly and the weekly run are both scheduled pipelines of develop, so the
pipeline source cannot tell them apart. The weekly schedule carries the pipeline
variable IDP_RUN_PROBES=true, and integration_test_deployment.py honours it only
on a scheduled pipeline. What is pinned here:

* the decision itself, in both directions, including that a scheduled run without
  the variable (the nightly) gets no probes;
* that the decision reaches the S3 object metadata the buildspec reads, and the
  run-options file codebuild_deployment.py reads;
* that the weekly run leaves the UX review stack to the nightly;
* that its summary, failure email and Slack posts say it is the weekly run.
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
BUILDSPEC_TEMPLATE = REPO_ROOT / "scripts" / "sdlc" / "cfn" / "codepipeline-s3.yml"
SLACK_LABEL = "WEEKLY VARIANT PROBES"


@pytest.fixture
def itd():
    import integration_test_deployment as module

    return module


# ---------------------------------------------------------------------------
# The trigger rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_the_weekly_schedule_requests_the_probes(itd, value):
    environ = {"CI_PIPELINE_SOURCE": "schedule", "IDP_RUN_PROBES": value}
    assert itd.variant_probes_requested(environ) is True


@pytest.mark.parametrize(
    "environ",
    [
        {"CI_PIPELINE_SOURCE": "schedule"},
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_RUN_PROBES": ""},
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_RUN_PROBES": "false"},
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_RUN_PROBES": "1"},
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_RUN_PROBES": "None"},
        {"CI_PIPELINE_SOURCE": "web", "IDP_RUN_PROBES": "true"},
        {"CI_PIPELINE_SOURCE": "merge_request_event", "IDP_RUN_PROBES": "true"},
        {"CI_PIPELINE_SOURCE": "push", "IDP_RUN_PROBES": "true"},
        {"IDP_RUN_PROBES": "true"},
        {},
    ],
    ids=[
        "nightly-schedule",
        "schedule-empty-variable",
        "schedule-false",
        "schedule-one-is-not-true",
        "schedule-None-string",
        "run-pipeline-form",
        "mr-button",
        "develop-push-button",
        "no-source",
        "nothing",
    ],
)
def test_nothing_else_requests_the_probes(itd, environ):
    """In particular the nightly: a scheduled run is not enough on its own."""
    assert itd.variant_probes_requested(environ) is False


def test_the_rule_reads_the_job_environment_by_default(itd, monkeypatch):
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "schedule")
    monkeypatch.delenv("IDP_RUN_PROBES", raising=False)
    assert itd.variant_probes_requested() is False
    monkeypatch.setenv("IDP_RUN_PROBES", "true")
    assert itd.variant_probes_requested() is True


# ---------------------------------------------------------------------------
# The two channels the decision travels on
# ---------------------------------------------------------------------------


class _RecordingS3:
    def __init__(self):
        self.calls: list[dict] = []

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"VersionId": "v1"}


def _uploaded_metadata(itd, monkeypatch, tmp_path, environ) -> dict:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "code.zip").write_bytes(b"zip")
    for name in ("CI_PIPELINE_SOURCE", "IDP_RUN_PROBES", "GITLAB_USER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    s3 = _RecordingS3()
    monkeypatch.setattr(itd.boto3, "client", lambda service, **k: s3)

    assert itd.upload_to_s3("bucket") == "v1"
    assert len(s3.calls) == 1
    return s3.calls[0]["Metadata"]


def test_the_weekly_run_sets_the_metadata_the_buildspec_reads(
    itd, monkeypatch, tmp_path
):
    metadata = _uploaded_metadata(
        itd,
        monkeypatch,
        tmp_path,
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_RUN_PROBES": "true"},
    )
    assert metadata.get("idp-run-probes") == "true"


@pytest.mark.parametrize(
    "environ",
    [
        {"CI_PIPELINE_SOURCE": "schedule"},
        {"CI_PIPELINE_SOURCE": "web", "IDP_RUN_PROBES": "true"},
    ],
    ids=["nightly", "run-pipeline-form"],
)
def test_no_other_run_sets_it(itd, monkeypatch, tmp_path, environ):
    metadata = _uploaded_metadata(itd, monkeypatch, tmp_path, environ)
    assert "idp-run-probes" not in metadata


def test_the_metadata_key_is_the_one_the_buildspec_reads():
    """A renamed key on either side reads as absent, and absent is `false`."""
    text = BUILDSPEC_TEMPLATE.read_text(encoding="utf-8")
    assert 'Metadata."idp-run-probes"' in text
    assert '[ "$IDP_RUN_PROBES" = "true" ] || export IDP_RUN_PROBES=false' in text


def test_the_run_options_record_the_decision(itd, tmp_path):
    weekly = itd.write_ci_run_options(
        str(tmp_path / "weekly.json"),
        environ={
            "CI_PIPELINE_SOURCE": "schedule",
            "CI_COMMIT_REF_NAME": "develop",
            "IDP_RUN_PROBES": "true",
        },
    )
    nightly = itd.write_ci_run_options(
        str(tmp_path / "nightly.json"),
        environ={"CI_PIPELINE_SOURCE": "schedule", "CI_COMMIT_REF_NAME": "develop"},
    )
    assert weekly["variant_probes"] is True
    assert nightly["variant_probes"] is False


# ---------------------------------------------------------------------------
# The build side
# ---------------------------------------------------------------------------


def test_the_weekly_run_leaves_the_ux_review_stack_to_the_nightly(cbd, itd, tmp_path):
    weekly = itd.write_ci_run_options(
        str(tmp_path / "weekly.json"),
        environ={
            "CI_PIPELINE_SOURCE": "schedule",
            "CI_COMMIT_REF_NAME": "develop",
            "IDP_RUN_PROBES": "true",
        },
    )
    nightly = itd.write_ci_run_options(
        str(tmp_path / "nightly.json"),
        environ={"CI_PIPELINE_SOURCE": "schedule", "CI_COMMIT_REF_NAME": "develop"},
    )
    assert cbd.ux_nightly_update_requested(nightly, environ={}) is True
    assert cbd.ux_nightly_update_requested(weekly, environ={}) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), (" TRUE ", True), ("false", False), ("None", False), ("", False)],
)
def test_the_build_reads_the_flag_the_buildspec_exports(cbd, value, expected):
    assert cbd.variant_probe_run({"IDP_RUN_PROBES": value}) is expected
    assert cbd.variant_probe_run({}) is False


def test_the_weekly_summary_says_what_run_it_is(cbd):
    labelled = cbd.label_variant_probe_run("TABLE", {"IDP_RUN_PROBES": "true"})
    assert labelled.endswith("TABLE")
    assert cbd.VARIANT_PROBE_RUN_LABEL in labelled.splitlines()[0]
    assert "not the nightly result" in labelled


def test_the_nightly_summary_is_unchanged(cbd):
    assert cbd.label_variant_probe_run("TABLE", {}) == "TABLE"
    assert cbd.label_variant_probe_run("TABLE", {"IDP_RUN_PROBES": "false"}) == "TABLE"


def test_main_labels_the_summary_and_the_failure_email():
    """Read from source: main() is not drivable offline without the whole run."""
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text(
        encoding="utf-8"
    )
    main = source[source.index("\ndef main(") :]
    assert "consolidated = label_variant_probe_run(consolidated)" in main
    email = main[main.index("send_failure_notification(") :][:400]
    assert "VARIANT_PROBE_RUN_LABEL" in email and "variant_probe_run()" in email


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load(CI_CONFIG.read_text(encoding="utf-8"))


def _heredoc(script: str) -> str:
    match = re.search(r"<<'PY'\n(.*?)\n\s*PY\s*$", script, re.S | re.M)
    assert match, "no python heredoc found"
    return textwrap.dedent(match.group(1))


def _slack_bodies(config) -> dict[str, str]:
    post = next(
        step
        for step in config["integration_tests"]["after_script"]
        if "slack-payload.json" in step
    )
    return {
        "integration_tests": _heredoc(post),
        "nightly_notification": _heredoc(config["nightly_notification"]["script"][0]),
    }


@pytest.mark.parametrize("job", ["integration_tests", "nightly_notification"])
def test_both_slack_posts_label_the_weekly_run(config, job):
    body = _slack_bodies(config)[job]
    assert SLACK_LABEL in body, f"{job}'s Slack post does not label the weekly run"
    assert 'os.environ.get("IDP_RUN_PROBES"' in body, (
        f"{job}'s label is not keyed on the schedule's IDP_RUN_PROBES variable"
    )
    label_line = next(line for line in body.splitlines() if SLACK_LABEL in line)
    assert label_line.strip().startswith("status ="), (
        "the label goes on `status`, a key the Slack workflow already declares; a "
        "new key would be ignored by the workflow"
    )
