# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The reason an inference test failed must reach the step's result.

`run_inference_test` printed its reason and returned a bare `False`, so the
step result that reaches the failure report said only "BDA config test failed".
That is the shape that made the NOT_FOUND monitor race expensive to read: a
batch declared failed before it started produces no output, and "BDA config test
failed" reads as a missing-output bug, which sends a reader to Step Functions —
where there is no failed execution to find, because nothing ever ran. The real
account ("no result file at pages/1/parsedResult.json after download-results")
was in the build log, hundreds of lines above the summary.

So every failure path is asserted to carry its reason, and the three step
wrappers to put that reason in the `error` they return.
"""

import json

import pytest


class _Completed:
    """What `run_command` hands back: something with a `.stdout`."""

    def __init__(self, stdout=""):
        self.stdout = stdout


def _call(cbd, **overrides):
    """Invoke `run_inference_test` with every argument named."""
    kwargs = {
        "stack_name": "idp-test",
        "sample_file": "lending_package.pdf",
        "batch_id": "test-bda",
        "verify_string": "ANYTOWN, USA 12345",
        "result_location": "pages/1/parsedResult.json",
        "content_path": "text",
    }
    kwargs.update(overrides)
    return cbd.run_inference_test(**kwargs)


@pytest.fixture
def harness(cbd, monkeypatch, tmp_path):
    """`run_inference_test` with its shell calls replaced.

    `find` is the only command whose output it reads, so the fake answers that
    and records the rest. The default is the happy path; a test narrows it.
    """
    state = {"result_file": "", "check_file": "", "raise_on": None}

    def fake_run_command(cmd, check=True, timeout=None):
        if state["raise_on"] and state["raise_on"] in cmd:
            raise RuntimeError(f"Command failed with exit code 1: {cmd}")
        if cmd.startswith("find ") and "parsedResult.json" in cmd:
            return _Completed(state["result_file"])
        if cmd.startswith("find "):
            return _Completed(state["check_file"])
        return _Completed("")

    monkeypatch.setattr(cbd, "run_command", fake_run_command)

    def write(name, payload):
        path = tmp_path / name
        path.write_text(json.dumps(payload))
        return str(path)

    state["write"] = write
    state["cbd"] = cbd
    return state


class TestEveryFailurePathNamesItself:
    def test_a_missing_result_file_says_which_path_was_looked_for(self, harness):
        """The exact shape the monitor race produced.

        `download-results` found nothing because nothing was ever processed, so
        the `find` comes back empty. The previous `False` left the reader with
        "BDA config test failed", which is about the config.
        """
        harness["result_file"] = ""

        outcome = _call(harness["cbd"])

        assert not outcome
        assert "pages/1/parsedResult.json" in outcome.reason
        assert "no result file" in outcome.reason

    def test_wrong_content_says_what_was_expected_and_what_was_there(self, harness):
        harness["result_file"] = harness["write"](
            "result.json", {"text": "SOMEWHERE ELSE, USA 99999"}
        )

        outcome = _call(harness["cbd"])

        assert not outcome
        assert "ANYTOWN, USA 12345" in outcome.reason
        assert "SOMEWHERE ELSE" in outcome.reason

    def test_a_failing_additional_check_carries_its_own_message(self, harness):
        harness["result_file"] = harness["write"](
            "result.json", {"text": "ANYTOWN, USA 12345"}
        )
        harness["check_file"] = harness["write"]("check.json", {"inference_result": {}})

        outcome = _call(
            harness["cbd"],
            additional_checks=[
                (
                    "BDA extraction verification",
                    "sections/1/result.json",
                    lambda data: (False, "No fields contain extracted data"),
                )
            ],
        )

        assert not outcome
        assert "BDA extraction verification" in outcome.reason
        assert "No fields contain extracted data" in outcome.reason

    def test_an_additional_check_that_raises_names_the_exception(self, harness):
        harness["result_file"] = harness["write"](
            "result.json", {"text": "ANYTOWN, USA 12345"}
        )
        harness["check_file"] = harness["write"]("check.json", {})

        def explode(data):
            raise KeyError("inference_result")

        outcome = _call(
            harness["cbd"],
            additional_checks=[("Extraction", "sections/1/result.json", explode)],
        )

        assert not outcome
        assert "Extraction" in outcome.reason
        assert "KeyError" in outcome.reason

    def test_a_failing_idp_cli_call_carries_the_exception_text(self, harness):
        """The run-inference / download-results invocations themselves.

        `run_command` raises on a non-zero exit, and that text is the only
        account of what the CLI did.
        """
        harness["raise_on"] = "run-inference"

        outcome = _call(harness["cbd"])

        assert not outcome
        assert "RuntimeError" in outcome.reason
        assert "run-inference" in outcome.reason

    def test_a_passing_test_is_truthy_and_carries_no_reason(self, harness):
        harness["result_file"] = harness["write"](
            "result.json", {"text": "ANYTOWN, USA 12345"}
        )

        outcome = _call(harness["cbd"])

        assert outcome
        assert outcome.reason == ""


class TestTheStepWrappersPassItOn:
    """The three steps that gate on `run_inference_test`.

    Parametrised over all three rather than written for the BDA step alone: the
    generic string was identical in each, and a fix applied to the one instance
    that bit is how the same defect comes back in the other two.
    """

    @pytest.mark.parametrize(
        "step,prefix",
        [
            ("test_step3_default_config", "Default config test failed"),
            ("test_step4_bda_mode", "BDA config test failed"),
            ("test_step5_rule_validation", "Rule validation test failed"),
        ],
    )
    def test_the_step_error_carries_the_reason(self, cbd, monkeypatch, step, prefix):
        reason = "no result file at pages/1/parsedResult.json after download-results"
        monkeypatch.setattr(cbd, "run_command", lambda *a, **k: _Completed(""))
        monkeypatch.setattr(
            cbd,
            "run_inference_test",
            lambda *a, **k: cbd.InferenceTestOutcome(False, reason),
        )

        result = getattr(cbd, step)("idp-test")

        assert result["success"] is False
        assert result["error"] == f"{prefix}: {reason}"

    @pytest.mark.parametrize(
        "step",
        [
            "test_step3_default_config",
            "test_step4_bda_mode",
            "test_step5_rule_validation",
        ],
    )
    def test_a_passing_step_succeeds(self, cbd, monkeypatch, step):
        """The other direction, so the wrappers are not asserted on failure only."""
        monkeypatch.setattr(cbd, "run_command", lambda *a, **k: _Completed(""))
        monkeypatch.setattr(
            cbd, "run_inference_test", lambda *a, **k: cbd.InferenceTestOutcome(True)
        )

        assert getattr(cbd, step)("idp-test")["success"] is True


class TestTheOutcomeValueItself:
    def test_it_is_falsy_on_failure_so_existing_call_sites_still_read_correctly(
        self, cbd
    ):
        assert not cbd.InferenceTestOutcome(False, "because")
        assert cbd.InferenceTestOutcome(True)

    def test_a_plain_bool_degrades_to_a_placeholder_rather_than_raising(self, cbd):
        """Stubs in other test modules return `False`, and so may a caller.

        Losing the reason is worse than never having had one, but an
        `AttributeError` here would replace the whole failure report with a
        traceback about the reporting code.
        """
        assert cbd.inference_failure_reason(False) == "reason not recorded"
        assert cbd.inference_failure_reason(None, fallback="unknown") == "unknown"

    def test_an_outcome_with_an_empty_reason_also_falls_back(self, cbd):
        assert (
            cbd.inference_failure_reason(cbd.InferenceTestOutcome(False))
            == "reason not recorded"
        )
