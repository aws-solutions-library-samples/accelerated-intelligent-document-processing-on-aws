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

import ast
import os
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
    # label_ci_run applies the probe label and the region label together, and
    # failure_subject names every weekly run the build belongs to.
    assert "consolidated = label_ci_run(consolidated)" in main
    email = main[main.index("send_failure_notification(") :][:400]
    assert "failure_subject(stack_name)" in email


def test_the_combined_label_and_subject_carry_the_probe_run(cbd):
    environ = {"IDP_RUN_PROBES": "true", "AWS_REGION": "us-east-1"}
    assert cbd.label_ci_run("TABLE", environ).startswith(
        f"🔬 {cbd.VARIANT_PROBE_RUN_LABEL}"
    )
    assert cbd.failure_subject("s", environ) == (
        f"IDP CI failure ({cbd.VARIANT_PROBE_RUN_LABEL.lower()}): s"
    )


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


def _label_condition(body: str) -> ast.expr:
    """The expression deciding whether ``body`` labels its post as a probe run.

    In integration_tests that is the ``probe_run`` assignment; in
    nightly_notification it is the ``if`` whose body prefixes ``status``.
    """
    tree = ast.parse(body)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "probe_run" for t in node.targets
        ):
            return node.value
        if isinstance(node, ast.If) and SLACK_LABEL in ast.unparse(node.body[0]):
            return node.test
    raise AssertionError("no probe-run label condition found")


@pytest.mark.parametrize("job", ["integration_tests", "nightly_notification"])
@pytest.mark.parametrize(
    ("source", "probes", "labelled"),
    [
        ("schedule", "true", True),
        ("schedule", " TRUE ", True),
        ("web", "true", False),
        ("push", "true", False),
        ("schedule", "", False),
        ("schedule", "false", False),
    ],
)
def test_the_label_agrees_with_variant_probes_requested(
    config, job, source, probes, labelled, itd, monkeypatch
):
    """A manual run carrying IDP_RUN_PROBES runs no probes, so it is not labelled."""
    monkeypatch.setenv("CI_PIPELINE_SOURCE", source)
    monkeypatch.setenv("IDP_RUN_PROBES", probes)
    condition = _label_condition(_slack_bodies(config)[job])
    verdict = eval(  # noqa: S307 - the expression is this repo's own CI config
        compile(ast.Expression(condition), "<label>", "eval"), {"os": os}
    )
    assert bool(verdict) is labelled
    assert bool(verdict) is bool(itd.variant_probes_requested())


# ---------------------------------------------------------------------------
# The buildspec's read of the flag, run in a shell
# ---------------------------------------------------------------------------


def _buildspec_flag_lines() -> list[str]:
    lines = [
        line.strip()[2:]
        for line in BUILDSPEC_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if "IDP_RUN_PROBES" in line and line.strip().startswith("- ")
    ]
    reads = [line for line in lines if line.startswith("export IDP_RUN_PROBES=$(")]
    assert len(reads) == 1, lines
    start = lines.index(reads[0])
    return [lines[start], yaml.safe_load(lines[start + 1])]


@pytest.mark.parametrize(
    ("printed", "status", "expected"),
    [
        ("true", 0, "true"),
        # --output text prints `None` for an absent key, and exits 0.
        ("None", 0, "false"),
        ("", 0, "false"),
        ("TRUE", 0, "false"),
        # head-object itself failing falls through to the `|| echo`.
        ("", 1, "false"),
    ],
)
def test_the_buildspec_turns_anything_but_true_into_false(printed, status, expected):
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    stub = f'aws() {{ printf "%s\\n" "{printed}"; return {status}; }}'
    script = "\n".join(
        [stub, *_buildspec_flag_lines(), 'printf "%s" "$IDP_RUN_PROBES"']
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, script built from the repo
        [bash, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "SOURCE_BUCKET": "bucket"},
    )
    assert completed.stdout == expected


# ---------------------------------------------------------------------------
# The weekly run is watched until its final summary exists
# ---------------------------------------------------------------------------


class _Executor:
    def __init__(self):
        self.shutdowns: list[bool] = []

    def shutdown(self, wait=True, **_):
        self.shutdowns.append(wait)


def test_a_probe_join_that_outlives_its_deadline_is_reported_not_waited_for(cbd):
    from concurrent.futures import Future

    executor = _Executor()
    pending: Future = Future()
    now = iter([cbd.PROBE_JOIN_DEADLINE_SECONDS + 1.0] * 2)

    results = cbd.join_variant_probes(
        pending, executor, started=0.0, clock=lambda: next(now)
    )

    assert len(results) == 1
    assert results[0]["success"] is False
    assert results[0]["probe"] == "probe supervisor"
    assert "still running" in results[0]["error"]
    assert executor.shutdowns == [False]


def test_a_timed_out_join_keeps_the_probes_that_finished(cbd):
    from concurrent.futures import Future

    finished = [{"probe": "waf", "success": True}]
    now = iter([cbd.PROBE_JOIN_DEADLINE_SECONDS + 1.0] * 2)

    results = cbd.join_variant_probes(
        Future(), _Executor(), started=0.0, clock=lambda: next(now), finished=finished
    )

    assert [row["probe"] for row in results] == ["waf", "probe supervisor"]


def test_each_probe_lands_in_the_callers_list_as_it_finishes(cbd, monkeypatch):
    monkeypatch.setenv("IDP_PROBE_LAUNCH_STAGGER_SECS", "0")
    monkeypatch.setattr(
        cbd,
        "deploy_and_test_probe",
        lambda probe, *_: {"probe": probe.name, "success": True},
    )
    probes = [p for p in cbd.PROBE_VARIANTS if p.stack_suffix != "zapdast"][:2]
    held: list[dict] = []

    returned = cbd.run_variant_probes("a@example.com", "url", probes, results=held)

    assert returned is held
    assert sorted(row["probe"] for row in held) == sorted(p.name for p in probes)


def test_a_finished_probe_join_hands_back_every_result(cbd):
    from concurrent.futures import Future

    executor = _Executor()
    done: Future = Future()
    rows = [{"probe": "waf", "success": True}, {"probe": "zap", "success": False}]
    done.set_result(rows)

    assert (
        cbd.join_variant_probes(done, executor, started=0.0, clock=lambda: 0.0) == rows
    )
    assert executor.shutdowns == [False]


def test_a_dead_probe_supervisor_is_one_failed_row(cbd):
    from concurrent.futures import Future

    executor = _Executor()
    died: Future = Future()
    died.set_exception(RuntimeError("boom"))

    finished = [{"probe": "waf", "success": True}]
    *kept, row = cbd.join_variant_probes(
        died, executor, started=0.0, clock=lambda: 0.0, finished=finished
    )
    assert kept == finished
    assert row["success"] is False
    assert row["error"] == "boom"


def _minutes(duration: str) -> int:
    hours = re.search(r"(\d+)h", duration)
    minutes = re.search(r"(\d+)m", duration)
    return (int(hours.group(1)) * 60 if hours else 0) + (
        int(minutes.group(1)) if minutes else 0
    )


def test_the_deadlines_nest_with_headroom(cbd, itd):
    """Join < CodeBuild timeout < monitor handoff window < GitLab job timeout.

    The join deadline counts from main()'s start, CodeBuild's timeout from the
    build's start, and the monitor from the pipeline's start, so each outer bound
    keeps an unmeasured allowance for the phases between those starts. The
    allowances are pinned here so narrowing one is deliberate. The 30 minutes
    below CodeBuild's timeout is mostly the install phase before main() starts,
    plus the Bedrock summary and the email after the join.
    """
    template = BUILDSPEC_TEMPLATE.read_text(encoding="utf-8")
    timeouts = re.findall(r"^\s*TimeoutInMinutes:\s*(\d+)\s*$", template, re.MULTILINE)
    assert len(timeouts) == 1, (
        f"expected one TimeoutInMinutes (the app-sdlc project) in "
        f"{BUILDSPEC_TEMPLATE.name}, found {len(timeouts)}; anchor this read to "
        "the project's logical id"
    )
    (timeout_minutes,) = timeouts
    job = yaml.safe_load(CI_CONFIG.read_text(encoding="utf-8"))["integration_tests"]

    join = cbd.PROBE_JOIN_DEADLINE_SECONDS
    assert int(timeout_minutes) * 60 - join >= 30 * 60
    assert itd.PROBE_RUN_MONITOR_HANDOFF_SECONDS - join >= 20 * 60
    assert (
        itd.PROBE_RUN_MONITOR_MAX_WAIT_SECONDS > itd.PROBE_RUN_MONITOR_HANDOFF_SECONDS
    )
    assert itd.PROBE_RUN_MONITOR_HANDOFF_SECONDS > itd.MONITOR_HANDOFF_SECONDS
    assert _minutes(job["timeout"]) * 60 - itd.PROBE_RUN_MONITOR_MAX_WAIT_SECONDS >= (
        10 * 60
    )


@pytest.mark.parametrize(
    ("environ", "probe_run"),
    [
        ({"CI_PIPELINE_SOURCE": "schedule", "IDP_RUN_PROBES": "true"}, True),
        ({"CI_PIPELINE_SOURCE": "schedule"}, False),
    ],
    ids=["weekly", "nightly"],
)
def test_main_watches_the_weekly_run_for_longer(
    itd, monkeypatch, tmp_path, environ, probe_run
):
    monkeypatch.chdir(tmp_path)
    for name in ("CI_PIPELINE_SOURCE", "IDP_RUN_PROBES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    seen: list[dict] = []
    monkeypatch.setattr(itd, "write_ci_run_options", lambda *a, **k: None)
    monkeypatch.setattr(itd, "create_deployment_package", lambda *a, **k: None)
    monkeypatch.setattr(itd, "upload_to_s3", lambda *a, **k: "v1")
    monkeypatch.setattr(itd, "monitor_pipeline", lambda *a, **k: seen.append(k) or True)

    with pytest.raises(SystemExit):
        itd.main()

    (kwargs,) = seen
    if probe_run:
        assert kwargs == {
            "max_wait": itd.PROBE_RUN_MONITOR_MAX_WAIT_SECONDS,
            "handoff_after": itd.PROBE_RUN_MONITOR_HANDOFF_SECONDS,
        }
    else:
        assert kwargs == {}


def test_a_variable_the_rule_ignores_says_so(itd, monkeypatch, tmp_path, capsys):
    """IDP_RUN_PROBES on a clicked pipeline is ignored, and the log says why."""
    metadata = _uploaded_metadata(
        itd,
        monkeypatch,
        tmp_path,
        {"CI_PIPELINE_SOURCE": "web", "IDP_RUN_PROBES": "true"},
    )
    assert "idp-run-probes" not in metadata
    out = capsys.readouterr().out
    assert "IDP_RUN_PROBES is set, but this is a web pipeline" in out


def test_the_inline_buildspec_parses_and_every_command_is_a_string():
    """CodeBuild rejects a buildspec whose command list holds a mapping.

    An unquoted `- echo "probes: $X"` is exactly that, because `: ` makes the item a
    YAML key, and the build then fails at DOWNLOAD_SOURCE before any command runs.
    validate-buildspec reads buildspec files, not the one embedded in this template,
    so it is parsed here as CodeBuild parses it.
    """
    from test_codebuild_cognito_grants import CfnLoader

    template = yaml.load(  # nosec B506 - SafeLoader subclass, see CfnLoader
        BUILDSPEC_TEMPLATE.read_text(encoding="utf-8"), Loader=CfnLoader
    )
    buildspec = template["Resources"]["IdkInstallCodeBuild"]["Properties"]["Source"][
        "BuildSpec"
    ]
    assert isinstance(buildspec, str), "BuildSpec is expected to be a literal block"
    spec = yaml.safe_load(buildspec)
    bad = [
        (phase, i, command)
        for phase, body in spec["phases"].items()
        for i, command in enumerate((body or {}).get("commands", []))
        if not isinstance(command, str)
    ]
    assert not bad, f"buildspec commands that are not strings (quote them): {bad}"
