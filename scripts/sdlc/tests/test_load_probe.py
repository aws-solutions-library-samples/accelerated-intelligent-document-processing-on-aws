# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for the load probe driver (scripts/sdlc/load_probe.py).

Nothing here reaches AWS: the driver takes its backend as an argument, and these
tests hand it a fake with a fake clock, so no test sleeps.
"""

import json

import load_probe as lp
import pytest

DECIDER = "arn:aws:lambda:us-east-1:111122223333:function:GENAIIDP-decider"


@pytest.fixture(autouse=True)
def _no_inherited_probe_settings(monkeypatch):
    # The scheduled pipeline sets these on every job, the fast checks included.
    for name in ("IDP_LOAD_PROBE_DECIDER_ARN", "IDP_RUN_LOAD_PROBE"):
        monkeypatch.delenv(name, raising=False)


def args(*extra):
    return lp.parse_args(["--stack-name", "S", "--run-id", "r1", *extra])


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(seconds, 1.0)


def event(id_, type_, prev=0, **details):
    e = {"id": id_, "type": type_, "previousEventId": prev}
    if details:
        e[details.pop("_key", "taskFailedEventDetails")] = details
    return e


def entered(id_, name, prev=0):
    return event(
        id_, "TaskStateEntered", prev, _key="stateEnteredEventDetails", name=name
    )


CLEAN_HISTORY = [entered(1, "OCR"), event(2, "TaskScheduled", 1)]
SECTION_OK = {
    "section_id": "1",
    "inference_result": {"Name": "A"},
    "explainability_info": [{"Name": {"confidence": 0.9}}],
}
SECTION_NO_CONFIDENCE = {"section_id": "2", "inference_result": {"Name": "A"}}


class FakeBackend:
    """Records calls; `outcome(doc)` decides each document's fate."""

    def __init__(self, outcome=None, existing=()):
        self.calls = []
        self.outcome = outcome or (
            lambda doc: ("COMPLETED", CLEAN_HISTORY, [SECTION_OK])
        )
        self.existing = set(existing)
        self.docs = {}

    def max_concurrency(self):
        return 100

    def profiles(self):
        return set(self.existing)

    def upload_profile(self, name, config):
        self.calls.append(("upload", name))
        return True

    def submit(self, directory, profile, batch_id):
        self.calls.append(("submit", batch_id))
        docs = [f"{batch_id}/{i}.pdf" for i in range(2)]
        for d in docs:
            self.docs[d] = self.outcome(d)
        return batch_id, docs

    def statuses(self, batch_id):
        return {d: o[0] for d, o in self.docs.items() if d.startswith(batch_id + "/")}

    def history(self, doc):
        return self.docs[doc][1]

    def sections(self, doc):
        return self.docs[doc][2]

    def delete_batch(self, batch):
        self.calls.append(("delete_batch", batch))

    def delete_profile(self, name):
        self.calls.append(("delete_profile", name))


def run(backend, *extra):
    lines = []
    code, report = lp.run_probe(
        args("--docs-per-arm", "2", *extra), backend, FakeClock(), lines.append
    )
    return code, report, lines


def test_the_caps_refuse_before_anything_is_created():
    for extra, refused in (
        (("--docs-per-arm", "20"), "over the cap of 40"),
        (("--max-cost-usd", "1"), "over the cap of $1.00"),
        (("--arms", "decision-model"), "no arm left to run"),
    ):
        backend = FakeBackend()
        code, report = lp.run_probe(args(*extra), backend, FakeClock(), lambda _: None)
        assert code == lp.CANNOT_RUN, extra
        assert any(refused in r for r in report["refusals"]), (extra, report)
        assert backend.calls == [], extra


def test_the_default_plan_fits_its_caps_and_each_arm_sets_its_feature():
    plan = lp.make_plan(args())
    assert [a.name for a in plan.arms] == ["split-then-classify", "holistic", "agentic"]
    assert "decision-model" in plan.skipped
    assert (
        lp.refusals(lp.make_plan(args("--decision-model-endpoint", DECIDER)), args())
        == []
    )

    from idp_common.config.models import IDPConfig

    expected = {
        "split-then-classify": lambda c: (
            c.classification.strategy == "split_then_classify"
        ),
        "holistic": lambda c: c.classification.combined.granularity == "packet",
        "agentic": lambda c: c.extraction.agentic.enabled,
        "decision-model": lambda c: c.extraction.confidence.method == "decision_model",
    }
    for arm in lp.build_arms(DECIDER):
        raw = lp.arm_config(arm, lp.DEFAULT_FAST_MODEL)
        # Stated in the uploaded profile itself, not left to the preset: a new
        # profile inherits the rest from the stack's own default.
        assert raw["extraction"]["confidence"]["mode"] == "separate", arm.name
        assert raw["hitl"]["enabled"] is False, arm.name
        config = IDPConfig.model_validate(raw)
        assert expected[arm.name](config), arm.name
        assert config.extraction.model == lp.DEFAULT_FAST_MODEL, arm.name
        assert config.extraction.confidence.mode == "separate", arm.name
        assert not config.hitl.enabled and not config.use_bda, arm.name


def test_retries_are_counted_per_state_entry_across_concurrent_iterations():
    # Two Map iterations enter Extraction concurrently; only the second retries,
    # twice. Attributing by state NAME rather than by entry would report 1 retry.
    events = [
        entered(1, "Extraction"),
        entered(2, "Extraction"),
        event(3, "TaskScheduled", 1),
        event(4, "TaskScheduled", 2),
        event(5, "TaskSucceeded", 3, _key="taskSucceededEventDetails", output="{}"),
        event(6, "TaskFailed", 4, error="ThrottlingException"),
        event(7, "TaskScheduled", 6),
        event(8, "TaskFailed", 7, error="TransientError"),
        event(9, "TaskScheduled", 8),
    ]
    retries, failures = lp.count_retries(events)
    assert retries == {"Extraction": 2}
    assert failures == {"ThrottlingException": 1, "TransientError": 1}


def test_failures_are_classified_by_the_task_error_behind_the_fail_state():
    history = [
        event(1, "TaskFailed", error="Lambda.Unknown", cause="x"),
        event(2, "TaskFailed", 1, error="TransientError", cause="read timed out"),
        event(
            3,
            "ExecutionFailed",
            2,
            _key="executionFailedEventDetails",
            error="States.TaskFailed",
            cause="Workflow Failed",
        ),
    ]
    assert lp.terminal_failure(history) == ("TransientError", "read timed out")
    for error, cause, kind in (
        ("TransientError", "", "transient"),
        ("Sandbox.Timedout", "", "transient"),
        (
            "Lambda.Unknown",
            json.dumps({"errorType": "ThrottlingException"}),
            "transient",
        ),
        ("RuntimeError", "Read timeout on endpoint URL", "transient"),
        ("ValidationException", "throttling mentioned in prose", "deterministic"),
        ("States.ExceedToleratedFailureThreshold", "", "unclassified"),
        ("", "", "unclassified"),
    ):
        assert lp.classify_failure(error, cause) == kind, (error, cause)


def test_a_section_with_values_and_no_confidence_is_a_gap():
    empty = {"section_id": "3", "inference_result": {"Name": None}}
    assert lp.confidence_gaps([SECTION_OK, SECTION_NO_CONFIDENCE, empty]) == ["2"]


def test_a_clean_run_passes_reports_concurrency_and_cleans_up_on_pass():
    backend = FakeBackend()
    code, report, _ = run(backend, "--cleanup", "on-pass")
    assert code == lp.PASS, report
    assert report["max_concurrent_workflows"] == 100
    uploads = [c[1] for c in backend.calls if c[0] == "upload"]
    assert uploads == [
        f"loadprobe-r1-{a}" for a in ("split-then-classify", "holistic", "agentic")
    ]
    assert sorted(c[1] for c in backend.calls if c[0] == "delete_profile") == sorted(
        uploads
    )
    # One batch per arm: a shared batch would mix identically named copies.
    assert len({c[1] for c in backend.calls if c[0] == "submit"}) == len(uploads)


def test_transient_failures_gaps_and_unsettled_documents_fail_the_run():
    transient = [event(1, "TaskFailed", error="ThrottlingException")]
    deterministic = [event(1, "TaskFailed", error="ValidationException")]
    opaque = [
        event(
            1,
            "MapRunFailed",
            _key="mapRunFailedEventDetails",
            error="States.ExceedToleratedFailureThreshold",
        )
    ]

    def outcome(doc):
        if doc.endswith("holistic/0.pdf"):
            return ("FAILED", transient, [])
        if doc.endswith("holistic/1.pdf"):
            return ("FAILED", deterministic, [])
        if doc.endswith("agentic/0.pdf"):
            return ("COMPLETED", CLEAN_HISTORY, [SECTION_NO_CONFIDENCE])
        if doc.endswith("agentic/1.pdf"):
            return ("RUNNING", [], [])
        if doc.endswith("split-then-classify/0.pdf"):
            return ("FAILED", opaque, [])
        return ("COMPLETED", CLEAN_HISTORY, [SECTION_OK])

    backend = FakeBackend(outcome)
    code, report, _ = run(backend, "--cleanup", "on-pass", "--deadline-minutes", "5")
    assert code == lp.FAIL
    findings = "\n".join(report["findings"])
    assert "holistic/0.pdf: FAILED, transient cause ThrottlingException" in findings
    assert "holistic/1.pdf" not in findings  # deterministic: reported, not asserted
    assert report["arms"]["holistic"]["failures"][1]["kind"] == "deterministic"
    assert "agentic/0.pdf: sections 2" in findings
    assert "agentic/1.pdf: did not settle" in findings
    assert "split-then-classify/0.pdf: FAILED, unclassified cause" in findings
    assert report["elapsed_minutes"] <= 5 + lp.DEFAULT_POLL_SECONDS / 60
    assert not [c for c in backend.calls if c[0].startswith("delete")]


def test_an_existing_profile_is_never_written():
    backend = FakeBackend(existing={"loadprobe-r1-split-then-classify"})
    code, report, _ = run(backend)
    assert code == lp.CANNOT_RUN
    assert "refusing to write existing profile" in report["error"]
    assert backend.calls == []


def test_the_probe_runs_on_its_own_schedule_and_that_schedule_runs_nothing_else():
    import yaml

    config = yaml.safe_load((lp.REPO / ".gitlab-ci.yml").read_text())
    selector = "$IDP_RUN_LOAD_PROBE =~"
    schedule = '$CI_PIPELINE_SOURCE == "schedule"'

    first, last = config["load_probe"]["rules"]
    assert schedule in first["if"] and selector in first["if"], first
    assert last == {"when": "never"}, last

    # Every OTHER job a scheduled pipeline admits by name must refuse this one
    # before it admits a schedule, or the probe's pipeline would also deploy,
    # notify, or run the nightly stories. Derived, so a new schedule job is held
    # to it too.
    admitting = {
        name: job["rules"]
        for name, job in config.items()
        if isinstance(job, dict)
        and name != "load_probe"
        and any(
            schedule in r.get("if", "") and r.get("when") != "never"
            for r in job.get("rules", [])
        )
    }
    assert {"integration_tests", "nightly_notification"} <= set(admitting)
    for name, rules in admitting.items():
        ifs = [(r.get("if", ""), r.get("when")) for r in rules]
        refuse = next(
            (
                i
                for i, (cond, when) in enumerate(ifs)
                if selector in cond and when == "never"
            ),
            None,
        )
        admit = next(
            i
            for i, (cond, when) in enumerate(ifs)
            if schedule in cond and when != "never"
        )
        assert refuse is not None and refuse < admit, (name, ifs)
