# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline tests for Step 16, the Quick Start bootstrap CI step.

The step needs a live stack and Bedrock, so what is testable here is its
registration, the command it runs, and the check it applies to the profile it
downloads: a check that always passed would be worse than no step at all.
"""

import shlex
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.unit


def _profile(cbd, class_id=None, fields=3, key="$id"):
    return {
        "classes": [
            {
                key: class_id or cbd.QUICK_START_CLASS,
                "properties": {f"Field{n}": {"type": "string"} for n in range(fields)},
            }
        ]
    }


class TestStepRegistration:
    def test_step16_is_in_the_parallel_pool(self, cbd):
        names = [entry[1] for entry in cbd.PARALLEL_TEST_STEPS]
        assert "Step 16" in names

    def test_step16_is_reachable_from_all_test_steps(self, cbd):
        funcs = [entry[0] for entry in cbd.ALL_TEST_STEPS]
        assert cbd.test_step16_quick_start_bootstrap in funcs


class TestBootstrapCommand:
    def test_saves_one_named_profile_and_class_with_every_hint(self, cbd):
        argv = shlex.split(cbd.quick_start_bootstrap_command("idp-stack"))

        assert argv[:2] == ["idp-cli", "bootstrap"]
        assert argv[argv.index("--stack-name") + 1] == "idp-stack"
        assert argv[argv.index("--target-profile") + 1] == cbd.QUICK_START_PROFILE
        assert argv[argv.index("--class-name") + 1] == cbd.QUICK_START_CLASS
        assert argv[argv.index("--prompt") + 1] == cbd.QUICK_START_PROMPT
        hints = [argv[i + 1] for i, a in enumerate(argv) if a == "--field-hint"]
        assert hints == list(cbd.QUICK_START_FIELD_HINTS)
        assert argv[argv.index("--count") + 1] == "1"
        assert argv[argv.index("--model-id") + 1] == cbd.QUICK_START_MODEL

    def test_the_profile_is_its_own_so_the_parallel_pool_is_unaffected(self, cbd):
        assert cbd.QUICK_START_PROFILE not in {"default", ""}
        assert "activate" not in cbd.quick_start_bootstrap_command("s")


class TestAuthoringModel:
    def test_the_model_is_the_one_bootstrap_would_default_to(self, cbd):
        from idp_common.synthesis import schema_author

        class RecordingClient:
            def __init__(self):
                self.model_ids = []

            def invoke_model(self, *, model_id, **_):
                self.model_ids.append(model_id)
                raise RuntimeError("offline")

        client = RecordingClient()
        authored = schema_author.author_class_schema(
            "A paystub", bedrock_client=client, max_retries=1
        )

        assert authored is None
        assert client.model_ids == [cbd.QUICK_START_MODEL]

    def test_an_eu_build_authors_with_the_eu_profile(self, cbd):
        env = {"AWS_REGION": "eu-central-1"}
        assert cbd.quick_start_model(env) == "eu.anthropic.claude-sonnet-4-6"
        argv = shlex.split(cbd.quick_start_bootstrap_command("s", env))
        assert argv[argv.index("--model-id") + 1] == "eu.anthropic.claude-sonnet-4-6"

    @pytest.mark.parametrize("region", ["us-east-1", "eu-central-1", "ca-central-1"])
    def test_every_ci_region_authors_with_a_model_it_can_call(self, cbd, region):
        model = cbd.quick_start_model({"AWS_REGION": region})
        unavailable = cbd._update_configuration_module().CA_UNAVAILABLE_MODELS
        if region.startswith("ca-"):
            assert model not in unavailable
        if region.startswith("eu-"):
            assert not model.startswith("us.")


class TestProfileCheck:
    def test_accepts_the_requested_class_with_a_field_per_hint(self, cbd):
        assert cbd.quick_start_profile_problem(_profile(cbd)) is None

    def test_accepts_a_class_named_only_by_its_document_type(self, cbd):
        profile = _profile(cbd, key="x-aws-idp-document-type")
        assert cbd.quick_start_profile_problem(profile) is None

    def test_refuses_a_profile_without_the_requested_class(self, cbd):
        problem = cbd.quick_start_profile_problem(_profile(cbd, class_id="Invoice"))
        assert problem and cbd.QUICK_START_CLASS in problem and "Invoice" in problem

    def test_counts_nested_fields_as_the_authoring_prompt_encourages(self, cbd):
        nested = {
            "classes": [
                {
                    "$id": cbd.QUICK_START_CLASS,
                    "properties": {
                        "Employer": {
                            "type": "object",
                            "properties": {"Name": {"type": "string"}},
                        },
                        "PayPeriod": {
                            "type": "object",
                            "properties": {
                                "EndDate": {"type": "string"},
                                "NetPay": {"type": "number"},
                            },
                        },
                    },
                }
            ]
        }
        assert cbd.quick_start_profile_problem(nested) is None

    def test_refuses_a_class_with_fewer_fields_than_hints(self, cbd):
        too_few = len(cbd.QUICK_START_FIELD_HINTS) - 1
        problem = cbd.quick_start_profile_problem(_profile(cbd, fields=too_few))
        assert problem and "field" in problem

    @pytest.mark.parametrize("empty", [None, {}, {"classes": []}])
    def test_refuses_an_empty_profile(self, cbd, empty):
        assert cbd.quick_start_profile_problem(empty)


class TestStepOutcome:
    def _run(self, cbd, monkeypatch, bootstrap_stdout, downloaded):
        calls = []
        outputs = (
            list(bootstrap_stdout)
            if isinstance(bootstrap_stdout, list)
            else [bootstrap_stdout] * cbd.QUICK_START_ATTEMPTS
        )

        def fake_run_command(cmd, check=True, timeout=None):
            calls.append(cmd)
            if cmd.startswith("idp-cli bootstrap"):
                out = outputs.pop(0)
                if isinstance(out, Exception):
                    raise out
                return subprocess.CompletedProcess(cmd, 0, out, "")
            argv = shlex.split(cmd)
            with open(argv[argv.index("--output") + 1], "w") as f:
                yaml.safe_dump(downloaded, f)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cbd, "run_command", fake_run_command)
        return cbd.test_step16_quick_start_bootstrap("idp-stack"), calls

    def test_passes_when_the_saved_profile_holds_the_class(self, cbd, monkeypatch):
        stdout = f"✓ Config profile: {cbd.QUICK_START_PROFILE}\n"
        result, calls = self._run(cbd, monkeypatch, stdout, _profile(cbd))

        assert result == {"success": True}
        assert f"--config-version {cbd.QUICK_START_PROFILE}" in calls[1]

    def test_fails_when_bootstrap_reports_no_saved_profile(self, cbd, monkeypatch):
        result, calls = self._run(
            cbd, monkeypatch, "✗ Bootstrap failed\n", _profile(cbd)
        )

        assert result["success"] is False
        assert cbd.QUICK_START_PROFILE in result["error"]
        assert len(calls) == cbd.QUICK_START_ATTEMPTS
        assert all(c.startswith("idp-cli bootstrap") for c in calls)

    def test_one_failed_authoring_is_retried_before_the_step_fails(
        self, cbd, monkeypatch
    ):
        saved = f"✓ Config profile: {cbd.QUICK_START_PROFILE}\n"
        failure = Exception("Command failed: idp-cli bootstrap")
        result, calls = self._run(cbd, monkeypatch, [failure, saved], _profile(cbd))

        assert result == {"success": True}
        assert [c.split()[1] for c in calls] == [
            "bootstrap",
            "bootstrap",
            "config-download",
        ]

    def test_a_suite_that_already_failed_fast_is_not_retried(self, cbd, monkeypatch):
        calls = []

        def aborted(cmd, check=True, timeout=None):
            calls.append(cmd)
            raise Exception(f"Command aborted (test suite failed fast): {cmd}")

        monkeypatch.setattr(cbd, "run_command", aborted)
        cbd.ABORT_TESTS.set()
        try:
            result = cbd.test_step16_quick_start_bootstrap("idp-stack")
        finally:
            cbd.ABORT_TESTS.clear()

        assert result["success"] is False
        assert len(calls) == 1

    def test_fails_when_the_saved_profile_is_not_what_was_asked_for(
        self, cbd, monkeypatch
    ):
        stdout = f"✓ Config profile: {cbd.QUICK_START_PROFILE}\n"
        result, _ = self._run(cbd, monkeypatch, stdout, _profile(cbd, class_id="W2"))

        assert result["success"] is False
        assert "W2" in result["error"]

    def test_a_failing_command_is_a_failed_step_not_an_exception(
        self, cbd, monkeypatch
    ):
        def failing(cmd, check=True, timeout=None):
            raise Exception(f"Command failed: {cmd}")

        monkeypatch.setattr(cbd, "run_command", failing)
        result = cbd.test_step16_quick_start_bootstrap("idp-stack")
        assert result["success"] is False
