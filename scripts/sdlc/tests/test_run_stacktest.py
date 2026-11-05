"""Unit tests for the manual stack-test runner (scripts/sdlc/run_stacktest.py).

The runner lets the deploy-variant probes be invoked ON DEMAND (via `make
probe-*`) instead of automatically in CI. These tests exercise its arg handling
and mode selection WITHOUT AWS: the underlying validate_fn / _run_probe_attempt
are monkeypatched.
"""

import os
import sys

import pytest

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_SDLC = os.path.dirname(_HERE)
if _SDLC not in sys.path:
    sys.path.insert(0, _SDLC)


@pytest.fixture
def rp():
    import run_stacktest

    return run_stacktest


def test_list_returns_zero(rp, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_stacktest.py", "--list"])
    rc = rp.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "zapdast" in out and "jobsapi" in out and "tenancy" in out


def test_unknown_probe_errs(rp, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_stacktest.py", "nope"])
    assert rp.main() == 2


def _fake_probes(rp, monkeypatch, suffix, validate_fn, requires_vpc=False):
    """Replace _tests_by_suffix with one fake Probe (namedtuple is immutable)."""
    fake = rp.cbd.Probe(
        name=f"fake-{suffix}",
        stack_suffix=suffix,
        deploy_params={},
        validate_fn=validate_fn,
        requires_vpc=requires_vpc,
    )
    monkeypatch.setattr(rp, "_tests_by_suffix", lambda: {suffix: fake})
    return fake


def test_validate_existing_stack_runs_validator_only(rp, monkeypatch):
    called = {}

    def fake_validate(stack_name):
        called["stack"] = stack_name
        return {"success": True}

    _fake_probes(rp, monkeypatch, "zapdast", fake_validate)
    monkeypatch.setattr(
        sys, "argv", ["run_stacktest.py", "zapdast", "--stack-name", "idp-live"]
    )
    # _run_probe_attempt must NOT be called in validate-only mode.
    monkeypatch.setattr(
        rp.cbd, "_run_probe_attempt", lambda *a, **k: pytest.fail("should not deploy")
    )
    rc = rp.main()
    assert rc == 0
    assert called["stack"] == "idp-live"


def test_vpc_probe_without_wiring_errors(rp, monkeypatch):
    for v in (
        "IDP_TEST_VPC_ID",
        "IDP_TEST_PRIVATE_SUBNET_IDS",
        "IDP_TEST_LAMBDA_SG_ID",
        "IDP_TEST_APIGW_VPCE_ID",
    ):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(
        sys, "argv", ["run_stacktest.py", "jobsapi", "--stack-name", "idp-live"]
    )
    # jobsapi requires_vpc → without --vpc-* or env, it must refuse (rc 2).
    assert rp.main() == 2


def test_vpc_probe_with_flags_proceeds(rp, monkeypatch):
    _fake_probes(
        rp, monkeypatch, "jobsapi", lambda s: {"success": True}, requires_vpc=True
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_stacktest.py",
            "jobsapi",
            "--stack-name",
            "idp-live",
            "--vpc-id",
            "vpc-1",
            "--subnet-ids",
            "subnet-a,subnet-b",
            "--lambda-sg-id",
            "sg-1",
            "--apigw-vpce-id",
            "vpce-1",
        ],
    )
    assert rp.main() == 0


def test_self_deploy_requires_template_url(rp, monkeypatch):
    # No --stack-name and no --template-url → self-deploy mode can't proceed.
    monkeypatch.setattr(sys, "argv", ["run_stacktest.py", "zapdast"])
    assert rp.main() == 2
