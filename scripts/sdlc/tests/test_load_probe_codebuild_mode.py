"""The load-probe mode of the CodeBuild harness (codebuild_deployment.run_load_probe).

The weekly load-probe schedule's build updates the long-lived load-probe stack to
the run's template and probes it, instead of the primary suite. These pin who may
ask for it, which stack it may touch, and that a stack it could not bring current
is a red run with no probe.
"""

import pytest


def _options(**load_probe):
    return {
        "pipeline_source": "schedule",
        "load_probe": {"requested": True, **load_probe},
    }


@pytest.mark.unit
def test_only_a_scheduled_run_with_a_request_is_a_load_probe_run(cbd):
    assert (
        cbd.load_probe_options(_options(stack="idp-loadprobe"))["stack"]
        == "idp-loadprobe"
    )
    assert cbd.load_probe_options({**_options(), "pipeline_source": "web"}) == {}
    assert (
        cbd.load_probe_options({"pipeline_source": "schedule", "load_probe": {}}) == {}
    )
    assert cbd.load_probe_options({"pipeline_source": "schedule"}) == {}
    assert cbd.load_probe_options([]) == {}


@pytest.mark.unit
def test_the_load_probe_stack_is_long_lived(cbd):
    assert cbd.LOAD_PROBE_STACK_NAME in cbd.SWEEP_EXEMPT_STACK_NAMES
    assert cbd.LOAD_PROBE_STACK_NAME != cbd.UX_NIGHTLY_STACK_NAME


@pytest.mark.unit
@pytest.mark.parametrize(
    "name", ["idp-ux-nightly", "loadprobe-idp", "idp-loadprobe2", ""]
)
def test_no_stack_but_the_load_probe_stack_is_accepted(cbd, name):
    assert cbd.load_probe_stack_refusal(name)


@pytest.mark.unit
def test_the_load_probe_stack_is_accepted(cbd):
    assert cbd.load_probe_stack_refusal(cbd.LOAD_PROBE_STACK_NAME) == ""


@pytest.mark.unit
def test_a_load_probe_run_leaves_the_review_stack_alone(cbd):
    nightly = {"pipeline_source": "schedule", "ref": "develop"}
    env = {
        "AWS_DEFAULT_REGION": cbd.PRIMARY_CI_REGION,
        "AWS_REGION": cbd.PRIMARY_CI_REGION,
    }
    assert cbd.ux_nightly_update_requested(nightly, env)
    assert not cbd.ux_nightly_update_requested({**nightly, **_options()}, env)


@pytest.mark.unit
def test_a_refused_stack_is_red_and_touches_nothing(cbd, monkeypatch):
    touched = []
    monkeypatch.setattr(
        cbd, "ensure_load_probe_stack", lambda *a: touched.append(a) or {}
    )
    monkeypatch.setattr(cbd, "run_command", lambda *a, **k: touched.append(a))

    ok, summary = cbd.run_load_probe(
        {"stack": "idp-ux-nightly"}, "https://t.example/t.yaml"
    )

    assert not ok and "refused" in summary
    assert touched == []


@pytest.mark.unit
def test_a_stack_that_is_not_current_is_red_and_not_probed(cbd, monkeypatch):
    probed = []
    monkeypatch.setattr(
        cbd,
        "ensure_load_probe_stack",
        lambda *a: {"state": "failed", "phase": "update"},
    )
    monkeypatch.setattr(cbd, "run_command", lambda *a, **k: probed.append(a))

    ok, summary = cbd.run_load_probe(
        {"stack": "idp-loadprobe"}, "https://t.example/t.yaml"
    )

    assert not ok and "did not run" in summary
    assert probed == []


@pytest.mark.unit
@pytest.mark.parametrize("state", ["created", "updated", "up_to_date"])
def test_a_current_stack_is_probed_and_the_probe_decides(
    cbd, monkeypatch, tmp_path, state
):
    import subprocess

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SOURCE_BUCKET", raising=False)
    commands = []
    monkeypatch.setattr(cbd, "ensure_load_probe_stack", lambda *a: {"state": state})

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="probe output", stderr="")

    monkeypatch.setattr(cbd, "run_command", fake_run)

    ok, summary = cbd.run_load_probe(
        {"stack": "idp-loadprobe", "region": "us-east-1", "args": "--max-cost-usd 20"},
        "https://t.example/t.yaml",
    )

    (cmd,) = commands
    assert "scripts/sdlc/load_probe.py --stack-name idp-loadprobe" in cmd
    assert "--cleanup on-pass" in cmd and cmd.endswith("--max-cost-usd 20")
    assert not ok and "FAILED" in summary
