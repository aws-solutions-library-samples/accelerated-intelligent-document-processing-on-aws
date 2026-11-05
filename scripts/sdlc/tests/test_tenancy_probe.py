"""Unit tests for the tenant-isolation deployment-variant probe.

The probe deploys a stack with ``TenantIsolationMode=Strict`` and runs the live
harness ``scripts/security/live_checks/verify_tenant_isolation.py`` against it
(``validate_tenant_isolation`` in ``scripts/sdlc/codebuild_deployment.py``). These
tests touch no AWS: ``run_command`` is monkeypatched, and the harness output it
returns is produced by the harness's OWN row printer, so a change to the harness's
output format fails here rather than turning every weekly verdict into "no tally".

What they pin:

  * the probe is one row in PROBE_VARIANTS, deploys Strict, needs no VPC, and is
    launched by the same launcher as every other probe (no gate of its own),
  * the validator runs the harness non-interactively against the probe's stack and
    region, from this checkout, with the sample document the manual target uses,
  * it passes only on exit 0 AND a complete tally AND at least one ``verify:`` row,
    and each of the three vacuous shapes fails on its own.
"""

import importlib.util
import os
import shlex

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_launch_stagger(monkeypatch):
    """The launcher test below calls the real run_variant_probes; no sleeping."""
    monkeypatch.setenv("IDP_PROBE_LAUNCH_STAGGER_SECS", "0")


class _Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def harness(cbd):
    """The live harness module, loaded from the path the probe runs."""
    spec = importlib.util.spec_from_file_location(
        "verify_tenant_isolation_under_test", cbd.TENANT_ISOLATION_HARNESS
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _harness_output(harness, capsys, rows):
    """Text the harness prints for ``rows``, using its own printer and tally line."""
    recorded = []
    for outcome, check, detail in rows:
        harness._row(recorded, outcome, check, detail)
    bad = [r for r in recorded if r[0] != "PASS"]
    # Mirrors the harness's final line; test_the_tally_line_is_the_harness_s pins it.
    print(f"\n{len(recorded) - len(bad)}/{len(recorded)} PASS")
    return capsys.readouterr().out


_GREEN_ROWS = [
    ("PASS", "throwaway profiles created", "tivA-default, tivB-default"),
    ("PASS", "tenants declared and mapped", "tiv-a, tiv-b"),
    ("PASS", "verify: tenant tiv-a reads its own document", ""),
    ("PASS", "verify: tenant tiv-a is refused tenant tiv-b's document", ""),
    ("PASS", "tenant A's uploader is refused at B's access point", "AccessDenied"),
    ("PASS", "tenancy audit ran", "0 finding(s)"),
]


def _stub_run(cbd, monkeypatch, stdout, returncode=0, stderr=""):
    calls = []

    def fake_run_command(cmd, check=True, timeout=cbd.DEFAULT_COMMAND_TIMEOUT):
        calls.append({"cmd": cmd, "check": check, "timeout": timeout})
        return _Completed(stdout=stdout, returncode=returncode, stderr=stderr)

    monkeypatch.setattr(cbd, "run_command", fake_run_command)
    return calls


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_the_tenancy_probe_is_one_row_deploying_strict_without_a_vpc(cbd):
    rows = [p for p in cbd.PROBE_VARIANTS if p.stack_suffix == "tenancy"]
    assert len(rows) == 1, "exactly one tenant-isolation probe expected"
    probe = rows[0]
    assert probe.deploy_params == {"TenantIsolationMode": "Strict"}
    assert probe.validate_fn is cbd.validate_tenant_isolation
    # Nothing in it needs the persistent test VPC, and a requires_vpc row would
    # skip itself in any account without one.
    assert probe.requires_vpc is False


def test_the_launcher_runs_the_tenancy_probe_with_the_others(cbd, monkeypatch):
    # It rides the existing launcher, stagger and concurrency cap: no gate of its
    # own, and IDP_TEST_ZAP=false (the one per-probe gate) does not remove it.
    monkeypatch.setenv("IDP_TEST_ZAP", "false")
    launched = []
    monkeypatch.setattr(
        cbd,
        "deploy_and_test_probe",
        lambda probe, email, url: (
            launched.append(probe.stack_suffix)
            or {"success": True, "probe": probe.name}
        ),
    )
    cbd.run_variant_probes("admin@example.invalid", "https://tmpl")
    assert "tenancy" in launched


def test_the_harness_and_document_the_probe_runs_exist(cbd):
    assert os.path.isfile(cbd.TENANT_ISOLATION_HARNESS)
    assert cbd.TENANT_ISOLATION_HARNESS.endswith(
        os.path.join("scripts", "security", "live_checks", "verify_tenant_isolation.py")
    )
    assert os.path.isfile(cbd.TENANT_ISOLATION_DOCUMENT)


def test_the_tally_line_is_the_harness_s(cbd):
    # The validator reads "<passed>/<total> PASS"; the harness must still print it.
    with open(cbd.TENANT_ISOLATION_HARNESS, encoding="utf-8") as fh:
        source = fh.read()
    assert 'print(f"\\n{len(rows) - len(bad)}/{len(rows)} PASS")' in source


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


def test_the_validator_runs_the_harness_against_the_probe_stack(
    cbd, harness, capsys, monkeypatch
):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    calls = _stub_run(cbd, monkeypatch, _harness_output(harness, capsys, _GREEN_ROWS))

    cbd.validate_tenant_isolation("idp-0101-000000-tenancy")

    assert len(calls) == 1
    argv = shlex.split(calls[0]["cmd"])
    assert argv[1] == cbd.TENANT_ISOLATION_HARNESS
    assert argv[argv.index("--stack-name") + 1] == "idp-0101-000000-tenancy"
    assert argv[argv.index("--region") + 1] == "eu-central-1"
    assert argv[argv.index("--document") + 1] == cbd.TENANT_ISOLATION_DOCUMENT
    # Without --yes the harness waits on input() and the probe hangs to its timeout.
    assert "--yes" in argv
    # The verdict is read from the exit code and output, never from an exception.
    assert calls[0]["check"] is False
    assert calls[0]["timeout"] == cbd.TENANT_ISOLATION_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


def test_a_complete_green_run_passes(cbd, harness, capsys, monkeypatch):
    _stub_run(cbd, monkeypatch, _harness_output(harness, capsys, _GREEN_ROWS))
    result = cbd.validate_tenant_isolation("idp-x-tenancy")
    assert result["success"] is True, result
    assert result["tenancy_checks"] == f"{len(_GREEN_ROWS)}/{len(_GREEN_ROWS)}"
    assert result["tenancy_failed_checks"] == []


def test_a_failed_row_fails_and_is_named(cbd, harness, capsys, monkeypatch):
    rows = _GREEN_ROWS + [
        ("FAIL", "tenant A's uploader is refused under B's prefix", "allowed")
    ]
    _stub_run(cbd, monkeypatch, _harness_output(harness, capsys, rows), returncode=1)
    result = cbd.validate_tenant_isolation("idp-x-tenancy")
    assert result["success"] is False
    assert "exited 1" in result["error"]
    assert "refused under B's prefix" in result["error"]
    assert result["tenancy_failed_checks"] == [
        "FAIL tenant A's uploader is refused under B's prefix — allowed"
    ]


def test_a_stack_that_is_not_strict_fails(cbd, monkeypatch):
    # The harness refuses with exit 2 and no tally when the mode is not Strict —
    # which is what a probe deploy that silently dropped the parameter looks like.
    _stub_run(
        cbd,
        monkeypatch,
        "ERROR  TenantIsolationMode is 'Off'; this check needs 'Strict'.\n",
        returncode=2,
    )
    result = cbd.validate_tenant_isolation("idp-x-tenancy")
    assert result["success"] is False
    assert "exited 2" in result["error"]
    assert "TenantIsolationMode is 'Off'" in result["error"]


def test_exit_zero_without_a_tally_fails(cbd, harness, capsys, monkeypatch):
    recorded = []
    for outcome, check, detail in _GREEN_ROWS:
        harness._row(recorded, outcome, check, detail)
    _stub_run(cbd, monkeypatch, capsys.readouterr().out)
    result = cbd.validate_tenant_isolation("idp-x-tenancy")
    assert result["success"] is False
    assert "without a complete PASS tally" in result["error"]


def test_exit_zero_with_an_incomplete_tally_fails(cbd, harness, capsys, monkeypatch):
    out = _harness_output(harness, capsys, _GREEN_ROWS).replace(
        f"{len(_GREEN_ROWS)}/{len(_GREEN_ROWS)} PASS",
        f"{len(_GREEN_ROWS) - 1}/{len(_GREEN_ROWS)} PASS",
    )
    _stub_run(cbd, monkeypatch, out)
    result = cbd.validate_tenant_isolation("idp-x-tenancy")
    assert result["success"] is False
    assert "without a complete PASS tally" in result["error"]


def test_a_run_with_no_verify_rows_fails(cbd, harness, capsys, monkeypatch):
    rows = [r for r in _GREEN_ROWS if not r[1].startswith("verify: ")]
    _stub_run(cbd, monkeypatch, _harness_output(harness, capsys, rows))
    result = cbd.validate_tenant_isolation("idp-x-tenancy")
    assert result["success"] is False
    assert "no `verify:` rows" in result["error"]


def test_the_parser_knows_every_outcome_the_sdk_can_report(cbd):
    # A row whose outcome the parser does not recognise still fails the run (the
    # harness exits 1), but the failure would name nothing.
    from idp_sdk.models.tenancy import TenantVerifyOutcome

    assert set(cbd.TENANCY_ROW_OUTCOMES) == {o.value for o in TenantVerifyOutcome}


def test_a_skipped_check_fails_and_is_named(cbd, harness, capsys, monkeypatch):
    rows = _GREEN_ROWS + [
        ("SKIP", "verify: ingest:outside-every-tenant", "TenantIsolationMode=Off")
    ]
    _stub_run(cbd, monkeypatch, _harness_output(harness, capsys, rows), returncode=1)
    result = cbd.validate_tenant_isolation("idp-x-tenancy")
    assert result["success"] is False
    assert "SKIP verify: ingest:outside-every-tenant" in result["error"]


def test_audit_notes_are_not_rows(cbd, harness, capsys, monkeypatch):
    out = _harness_output(harness, capsys, _GREEN_ROWS)
    out = "NOTE   audit finding: prefix tenancy-verify/ — informational\n" + out
    _stub_run(cbd, monkeypatch, out)
    result = cbd.validate_tenant_isolation("idp-x-tenancy")
    assert result["success"] is True, result
    assert result["tenancy_failed_checks"] == []
