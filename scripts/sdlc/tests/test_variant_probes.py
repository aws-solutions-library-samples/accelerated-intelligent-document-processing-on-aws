"""Unit tests for the deployment-variant probe framework.

These exercise the probe launcher / quota cap / fail-fast isolation added to
`scripts/sdlc/codebuild_deployment.py` WITHOUT any AWS or subprocess calls:
boto3, run_command, and the deploy/cleanup helpers are all monkeypatched. They
verify the behaviors the harness is easy to regress on:

  * the per-variant concurrency/quota budget (resolve_probe_concurrency),
  * the deploy → validate → ALWAYS-cleanup lifecycle of a single probe,
  * CF-event capture before teardown on failure,
  * each probe thread opting out of the primary suite's fail-fast machinery
    (_thread_local.never_abort) so a primary failure can't kill a probe deploy,
  * the launcher folding independent per-probe results without one failure
    affecting the others, and honoring the concurrency cap.
"""

import threading
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_launch_stagger(monkeypatch):
    """Disable the probe launch stagger by default so launcher tests don't sleep.

    The real default is DEFAULT_PROBE_LAUNCH_STAGGER_SECS (120s/index); with it
    on, run_variant_probes tests would sleep index*120s. Stagger-resolver tests
    set IDP_PROBE_LAUNCH_STAGGER_SECS explicitly, which overrides this.
    """
    monkeypatch.setenv("IDP_PROBE_LAUNCH_STAGGER_SECS", "0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_probe(
    cbd,
    name="Test probe",
    suffix="test",
    params=None,
    validate=None,
    requires_vpc=False,
):
    return cbd.Probe(
        name=name,
        stack_suffix=suffix,
        deploy_params=params if params is not None else {"Foo": "Bar"},
        validate_fn=validate or (lambda stack_name: {"success": True}),
        requires_vpc=requires_vpc,
    )


_TEST_VPC_ENV = {
    "IDP_TEST_VPC_ID": "vpc-abc123",
    "IDP_TEST_PRIVATE_SUBNET_IDS": "subnet-a,subnet-b",
    "IDP_TEST_LAMBDA_SG_ID": "sg-xyz789",
    "IDP_TEST_APIGW_VPCE_ID": "vpce-def456",
}


def _set_test_vpc_env(monkeypatch):
    for k, v in _TEST_VPC_ENV.items():
        monkeypatch.setenv(k, v)


def _clear_test_vpc_env(monkeypatch):
    for k in _TEST_VPC_ENV:
        monkeypatch.delenv(k, raising=False)


def _stub_lifecycle(cbd, monkeypatch, *, deploy_status="CREATE_COMPLETE"):
    """Stub out IAM/deploy/status/cleanup so a probe run touches no AWS.

    Records calls in the returned dict so tests can assert on ordering,
    parameters, and that cleanup always ran.
    """
    calls = {"iam": [], "commands": [], "cleanup": [], "cf_events": []}

    # Probes call create_iam_resources(stack_name, create_boundary=False) and
    # deploy with an EMPTY boundary ARN, so the stub accepts the kwarg and
    # returns "" for the boundary.
    monkeypatch.setattr(
        cbd,
        "create_iam_resources",
        lambda stack_name, create_boundary=True: (
            calls["iam"].append(stack_name) or ("role-arn", "")
        ),
    )
    monkeypatch.setattr(cbd, "generate_stack_name", lambda: "idp-0101-000000")

    def fake_run_command(cmd, check=True, timeout=cbd.DEFAULT_COMMAND_TIMEOUT):
        calls["commands"].append(cmd)
        if "describe-stacks" in cmd:
            return _Completed(stdout=deploy_status)
        return _Completed(stdout="")

    monkeypatch.setattr(cbd, "run_command", fake_run_command)
    monkeypatch.setattr(
        cbd,
        "cleanup_stack",
        lambda result: calls["cleanup"].append(result["stack_name"]),
    )
    monkeypatch.setattr(
        cbd,
        "_capture_cf_events",
        lambda result, *names: calls["cf_events"].append(tuple(names)),
    )
    return calls


class _Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


# --------------------------------------------------------------------------- #
# resolve_probe_concurrency — the quota budget
# --------------------------------------------------------------------------- #


def test_concurrency_default_runs_probes_in_parallel(cbd, monkeypatch):
    monkeypatch.delenv("IDP_PROBE_MAX_CONCURRENCY", raising=False)
    # VPCs no longer bound probe concurrency (one shared persistent test VPC),
    # so the default fans out several probes at once — enough to cover the
    # default table in parallel. Clamped to the probe count.
    assert cbd.DEFAULT_PROBE_MAX_CONCURRENCY >= len(cbd.PROBE_VARIANTS)
    assert cbd.resolve_probe_concurrency(5) == 5
    assert cbd.resolve_probe_concurrency(3) == 3


def test_concurrency_env_override_is_clamped_to_probe_count(cbd, monkeypatch):
    # An override larger than the number of probes never spins up idle workers.
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "10")
    assert cbd.resolve_probe_concurrency(3) == 3
    assert cbd.resolve_probe_concurrency(1) == 1


def test_concurrency_env_override_honored_within_bounds(cbd, monkeypatch):
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "2")
    assert cbd.resolve_probe_concurrency(5) == 2


def test_concurrency_invalid_env_falls_back_to_default(cbd, monkeypatch):
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "not-a-number")
    # Falls back to the default, then clamps to the probe count.
    expected = min(cbd.DEFAULT_PROBE_MAX_CONCURRENCY, 5)
    assert cbd.resolve_probe_concurrency(5) == expected


def test_concurrency_nonpositive_env_falls_back_to_default(cbd, monkeypatch):
    expected = min(cbd.DEFAULT_PROBE_MAX_CONCURRENCY, 5)
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "0")
    assert cbd.resolve_probe_concurrency(5) == expected
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "-3")
    assert cbd.resolve_probe_concurrency(5) == expected


def test_concurrency_never_zero_even_with_zero_probes(cbd, monkeypatch):
    # Defensive: max(1, min(cap, n)) must never return 0 (ThreadPoolExecutor
    # rejects max_workers<1). run_variant_probes never calls this with 0 probes,
    # but the clamp should still hold.
    monkeypatch.delenv("IDP_PROBE_MAX_CONCURRENCY", raising=False)
    assert cbd.resolve_probe_concurrency(0) == 1


# --------------------------------------------------------------------------- #
# _resolve_probe_launch_stagger — burst-flattening mitigation
# --------------------------------------------------------------------------- #


def test_launch_stagger_defaults_when_unset(cbd, monkeypatch):
    monkeypatch.delenv("IDP_PROBE_LAUNCH_STAGGER_SECS", raising=False)
    assert cbd._resolve_probe_launch_stagger() == float(
        cbd.DEFAULT_PROBE_LAUNCH_STAGGER_SECS
    )


def test_launch_stagger_zero_disables(cbd, monkeypatch):
    # 0 is a valid explicit value (simultaneous launch, pre-mitigation behavior)
    # and must NOT be coerced to the default.
    monkeypatch.setenv("IDP_PROBE_LAUNCH_STAGGER_SECS", "0")
    assert cbd._resolve_probe_launch_stagger() == 0.0


def test_launch_stagger_honors_override(cbd, monkeypatch):
    monkeypatch.setenv("IDP_PROBE_LAUNCH_STAGGER_SECS", "3.5")
    assert cbd._resolve_probe_launch_stagger() == 3.5


def test_launch_stagger_malformed_and_negative_fall_back(cbd, monkeypatch):
    default = float(cbd.DEFAULT_PROBE_LAUNCH_STAGGER_SECS)
    monkeypatch.setenv("IDP_PROBE_LAUNCH_STAGGER_SECS", "abc")
    assert cbd._resolve_probe_launch_stagger() == default
    monkeypatch.setenv("IDP_PROBE_LAUNCH_STAGGER_SECS", "-5")
    assert cbd._resolve_probe_launch_stagger() == default


# --------------------------------------------------------------------------- #
# deploy_and_test_probe — single-probe lifecycle
# --------------------------------------------------------------------------- #


def test_probe_happy_path_deploys_validates_cleans_up(cbd, monkeypatch):
    calls = _stub_lifecycle(cbd, monkeypatch)
    validated = []
    probe = _make_probe(
        cbd,
        suffix="apigw",
        params={"WebUIHosting": "APIGateway", "ApiGatewayVisibility": "GLOBAL"},
        validate=lambda stack_name: (
            validated.append(stack_name)
            or {"success": True, "web_url": "https://x/api"}
        ),
    )

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert result["success"] is True
    assert result["probe"] == probe.name
    assert result["stack_name"] == "idp-0101-000000-apigw"
    assert result["web_url"] == "https://x/api"
    # validator ran against the deployed stack
    assert validated == ["idp-0101-000000-apigw"]
    # cleanup ALWAYS runs (finally)
    assert calls["cleanup"] == ["idp-0101-000000-apigw"]
    # deploy command carried the probe's extra params + an EMPTY boundary
    # (probes deploy without a permissions boundary — only the primary suite
    # creates+tests one).
    deploy_cmd = next(c for c in calls["commands"] if "idp-cli deploy" in c)
    assert "PermissionsBoundaryArn=," in deploy_cmd or deploy_cmd.rstrip('"').endswith(
        "PermissionsBoundaryArn="
    )
    assert "WebUIHosting=APIGateway" in deploy_cmd
    assert "ApiGatewayVisibility=GLOBAL" in deploy_cmd
    assert "--stack-name idp-0101-000000-apigw" in deploy_cmd


def test_probe_sets_never_abort_on_its_thread(cbd, monkeypatch):
    _stub_lifecycle(cbd, monkeypatch)
    seen = {}

    def capture(stack_name):
        # by the time validation runs, the thread must be non-abortable
        seen["never_abort"] = getattr(cbd._thread_local, "never_abort", False)
        return {"success": True}

    probe = _make_probe(cbd, validate=capture)
    cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")
    assert seen["never_abort"] is True


def test_probe_deploy_status_not_complete_is_deploy_failure(cbd, monkeypatch):
    # CREATE_FAILED (not *_COMPLETE): the harness's "COMPLETE" not in <status>
    # check — matching the primary suite's deploy_and_test_stack — treats it as
    # a deploy failure.
    calls = _stub_lifecycle(cbd, monkeypatch, deploy_status="CREATE_FAILED")
    probe = _make_probe(cbd)

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert result["success"] is False
    assert result["failure_type"] == "deploy"
    assert "CREATE_FAILED" in result["error"]
    # CF events captured BEFORE teardown, and teardown still ran
    assert calls["cf_events"] == [("idp-0101-000000-test",)]
    assert calls["cleanup"] == ["idp-0101-000000-test"]


def test_probe_validation_failure_is_test_failure(cbd, monkeypatch):
    calls = _stub_lifecycle(cbd, monkeypatch)
    probe = _make_probe(
        cbd, validate=lambda stack_name: {"success": False, "error": "bad endpoint"}
    )

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert result["success"] is False
    assert result["failure_type"] == "test"
    assert result["error"] == "bad endpoint"
    # a validation (not deploy) failure still tears down
    assert calls["cleanup"] == ["idp-0101-000000-test"]


def test_probe_exception_captures_events_and_cleans_up(cbd, monkeypatch):
    calls = _stub_lifecycle(cbd, monkeypatch)

    def boom(stack_name):
        raise RuntimeError("kaboom")

    probe = _make_probe(cbd, validate=boom)
    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert result["success"] is False
    assert result["failure_type"] == "deploy"
    assert "kaboom" in result["error"]
    assert calls["cf_events"] == [("idp-0101-000000-test",)]
    # cleanup STILL runs on exception (finally)
    assert calls["cleanup"] == ["idp-0101-000000-test"]


def test_probe_iam_failure_still_cleans_up(cbd, monkeypatch):
    calls = _stub_lifecycle(cbd, monkeypatch)
    monkeypatch.setattr(
        cbd,
        "create_iam_resources",
        lambda stack_name, create_boundary=True: (None, None),
    )

    probe = _make_probe(cbd)
    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert result["success"] is False
    assert "IAM" in result["error"]
    assert calls["cleanup"] == ["idp-0101-000000-test"]


# --------------------------------------------------------------------------- #
# scoped retry — ONLY the transient CloudWatch Logs create-consistency race
# --------------------------------------------------------------------------- #


def _logs_race_event():
    # Shape matches get_cloudformation_logs()'s failure-event dicts.
    return {
        "resource_type": "AWS::Logs::LogGroup",
        "logical_id": "WorkflowTrackerLogGroup",
        "status": "CREATE_FAILED",
        "reason": (
            'Resource handler returned message: "The specified log group does '
            'not exist. (Service: CloudWatchLogs, Status Code: 400)" '
            "(HandlerErrorCode: InvalidRequest)"
        ),
    }


def test_is_transient_logs_race_matches_the_cwl_create_race(cbd):
    result = {"failure_type": "deploy", "cf_events": [_logs_race_event()]}
    assert cbd._is_transient_logs_race(result) is True


def test_is_transient_logs_race_ignores_collateral_cancelled_events(cbd):
    # The rolled-back siblings say "Resource creation cancelled" — NOT a match;
    # only the initiating LogGroup "does not exist" event qualifies.
    result = {
        "failure_type": "deploy",
        "cf_events": [
            {
                "resource_type": "AWS::S3::Bucket",
                "status": "CREATE_FAILED",
                "reason": "Resource creation cancelled",
            },
            {
                "resource_type": "AWS::SQS::Queue",
                "status": "CREATE_FAILED",
                "reason": "Resource creation cancelled",
            },
        ],
    }
    assert cbd._is_transient_logs_race(result) is False


def test_is_transient_logs_race_ignores_other_loggroup_errors(cbd):
    # A real config/permission error on a log group (e.g. KMS access denied)
    # is deterministic and must NOT be retried.
    result = {
        "failure_type": "deploy",
        "cf_events": [
            {
                "resource_type": "AWS::Logs::LogGroup",
                "status": "CREATE_FAILED",
                "reason": "AccessDenied: not authorized to perform kms:GenerateDataKey",
            }
        ],
    }
    assert cbd._is_transient_logs_race(result) is False


def test_is_transient_logs_race_never_matches_validation_failure(cbd):
    # Even if a stray "log group does not exist" string is present, a TEST
    # (validation) failure is never a deploy race.
    result = {
        "failure_type": "test",
        "cf_events": [_logs_race_event()],
    }
    assert cbd._is_transient_logs_race(result) is False


def test_is_transient_logs_race_handles_missing_or_malformed_events(cbd):
    assert cbd._is_transient_logs_race({"failure_type": "deploy"}) is False
    assert (
        cbd._is_transient_logs_race(
            {"failure_type": "deploy", "cf_events": [None, "junk", 42]}
        )
        is False
    )


def _codebuild_trust_race_event():
    # A nested-stack AWS::CodeBuild::Project created right after its service role;
    # IAM trust-policy propagation is eventually consistent, so CreateProject's
    # trust validation occasionally races the just-created role. Shape matches
    # get_cloudformation_logs()'s failure-event dicts.
    return {
        "resource_type": "AWS::CodeBuild::Project",
        "logical_id": "DockerBuildProject",
        "status": "CREATE_FAILED",
        "reason": (
            'Resource handler returned message: "CodeBuild is not authorized to '
            "perform: sts:AssumeRole on service role. Please verify that ... the "
            'role has the necessary trust policy configured. (Service: AWSCodeBuild)"'
        ),
    }


def test_is_transient_race_matches_the_codebuild_trust_propagation_race(cbd):
    # The broadened detector also retries the CodeBuild service-role trust
    # propagation race (real CI failure on the jobsapi probe).
    result = {"failure_type": "deploy", "cf_events": [_codebuild_trust_race_event()]}
    assert cbd._is_transient_deploy_race(result) is True
    # Back-compat alias resolves to the same broadened detector.
    assert cbd._is_transient_logs_race(result) is True


def test_is_transient_race_ignores_other_codebuild_errors(cbd):
    # A real CodeBuild misconfig (e.g. bad source location) is deterministic and
    # must NOT be retried — only the sts:AssumeRole trust race qualifies.
    result = {
        "failure_type": "deploy",
        "cf_events": [
            {
                "resource_type": "AWS::CodeBuild::Project",
                "status": "CREATE_FAILED",
                "reason": "Invalid source location: bucket does not exist",
            }
        ],
    }
    assert cbd._is_transient_deploy_race(result) is False


def _stub_attempts(cbd, monkeypatch, results):
    """Make _run_probe_attempt return each of `results` in order; record count."""
    seq = list(results)
    calls = {"attempts": 0}

    def fake_attempt(probe, admin_email, template_url, vpc_params):
        calls["attempts"] += 1
        return seq.pop(0)

    monkeypatch.setattr(cbd, "_run_probe_attempt", fake_attempt)
    return calls


def test_probe_retries_once_on_transient_logs_race_then_succeeds(cbd, monkeypatch):
    first = {
        "stack_name": "idp-0101-000000-test",
        "success": False,
        "probe": "Test probe",
        "failure_type": "deploy",
        "cf_events": [_logs_race_event()],
    }
    second = {
        "stack_name": "idp-0101-000001-test",
        "success": True,
        "probe": "Test probe",
    }
    calls = _stub_attempts(cbd, monkeypatch, [first, second])
    probe = _make_probe(cbd)

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert calls["attempts"] == 2  # retried exactly once
    assert result["success"] is True
    assert result["stack_name"] == "idp-0101-000001-test"


def test_probe_retries_transient_race_at_most_once(cbd, monkeypatch):
    # Race twice in a row: attempt-1 retries, attempt-2 is the last attempt and
    # its result is returned as-is (no third attempt).
    race = lambda sfx: {  # noqa: E731
        "stack_name": f"idp-0101-{sfx}-test",
        "success": False,
        "probe": "Test probe",
        "failure_type": "deploy",
        "cf_events": [_logs_race_event()],
    }
    calls = _stub_attempts(cbd, monkeypatch, [race("000000"), race("000001")])
    probe = _make_probe(cbd)

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert calls["attempts"] == 2  # capped — no third attempt
    assert result["success"] is False
    assert result["failure_type"] == "deploy"


def test_probe_does_not_retry_a_real_deploy_failure(cbd, monkeypatch):
    # A non-race deploy failure (e.g. genuine config error) must fail fast with
    # NO retry so real regressions surface immediately.
    real_fail = {
        "stack_name": "idp-0101-000000-test",
        "success": False,
        "probe": "Test probe",
        "failure_type": "deploy",
        "cf_events": [
            {
                "resource_type": "AWS::IAM::Role",
                "status": "CREATE_FAILED",
                "reason": "MalformedPolicyDocument",
            }
        ],
    }
    calls = _stub_attempts(cbd, monkeypatch, [real_fail])
    probe = _make_probe(cbd)

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert calls["attempts"] == 1  # no retry
    assert result["success"] is False


def test_probe_does_not_retry_a_validation_failure(cbd, monkeypatch):
    val_fail = {
        "stack_name": "idp-0101-000000-test",
        "success": False,
        "probe": "Test probe",
        "failure_type": "test",
        "error": "bad endpoint",
    }
    calls = _stub_attempts(cbd, monkeypatch, [val_fail])
    probe = _make_probe(cbd)

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert calls["attempts"] == 1  # no retry on a test failure
    assert result["success"] is False


def test_probe_skips_before_any_attempt_when_vpc_missing(cbd, monkeypatch):
    # A requires_vpc probe with no VPC env must skip WITHOUT calling an attempt.
    _clear_test_vpc_env(monkeypatch)
    calls = _stub_attempts(cbd, monkeypatch, [])
    probe = _make_probe(cbd, requires_vpc=True)

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert calls["attempts"] == 0
    assert result["success"] is True
    assert result["skipped"] is True


# --------------------------------------------------------------------------- #
# fail-fast isolation — a primary-suite abort must not kill a probe's commands
# --------------------------------------------------------------------------- #


def test_never_abort_thread_ignores_abort_tests(cbd, monkeypatch):
    """A run_command on a never_abort thread must run even when ABORT_TESTS is set.

    This is the core isolation guarantee: the primary suite fails fast and sets
    ABORT_TESTS, but a probe thread (never_abort=True) must keep going so its
    independent-stack deploy isn't killed mid-flight. We stub Popen so no real
    process starts and assert the command was NOT refused.
    """
    cbd.ABORT_TESTS.set()
    started = []

    class FakePopen:
        def __init__(self, *a, **k):
            started.append(a[0] if a else k.get("args"))
            self.pid = 4242
            self.returncode = 0

        def communicate(self, timeout=None):
            return ("ok", "")

    monkeypatch.setattr(cbd.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(cbd.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cbd.os, "killpg", lambda *a: None)

    result_box = {}

    def worker():
        cbd._thread_local.never_abort = True
        try:
            res = cbd.run_command("echo hi", check=False)
            result_box["stdout"] = res.stdout
        except Exception as e:  # noqa: BLE001
            result_box["error"] = str(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)

    assert "error" not in result_box, result_box
    assert result_box.get("stdout") == "ok"
    assert started == ["echo hi"]


def test_abortable_thread_is_refused_when_abort_set(cbd, monkeypatch):
    """Control case: a NON-never_abort background thread IS refused on abort.

    Confirms the isolation in the previous test comes from never_abort, not
    from the command never being abortable at all.
    """
    cbd.ABORT_TESTS.set()
    monkeypatch.setattr(
        cbd.subprocess, "Popen", lambda *a, **k: pytest.fail("Popen should not run")
    )
    result_box = {}

    def worker():
        # note: never_abort NOT set → abortable
        try:
            cbd.run_command("echo hi", check=False)
            result_box["ran"] = True
        except Exception as e:  # noqa: BLE001
            result_box["error"] = str(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)

    assert "error" in result_box
    assert "failed fast" in result_box["error"]


# --------------------------------------------------------------------------- #
# run_variant_probes — the concurrent launcher
# --------------------------------------------------------------------------- #


def test_launcher_runs_all_probes_and_folds_results(cbd, monkeypatch):
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "3")
    ran = []

    def fake_deploy(probe, admin_email, template_url):
        ran.append(probe.name)
        return {
            "stack_name": f"s-{probe.stack_suffix}",
            "success": True,
            "probe": probe.name,
        }

    monkeypatch.setattr(cbd, "deploy_and_test_probe", fake_deploy)

    probes = [
        _make_probe(cbd, name="A", suffix="a"),
        _make_probe(cbd, name="B", suffix="b"),
        _make_probe(cbd, name="C", suffix="c"),
    ]
    results = cbd.run_variant_probes("a@b.com", "https://tmpl", probes=probes)

    assert sorted(ran) == ["A", "B", "C"]
    assert len(results) == 3
    assert {r["probe"] for r in results} == {"A", "B", "C"}
    assert all(r["success"] for r in results)


def test_launcher_isolates_one_probe_failure(cbd, monkeypatch):
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "3")

    def fake_deploy(probe, admin_email, template_url):
        if probe.name == "B":
            return {
                "stack_name": "s-b",
                "success": False,
                "error": "B blew up",
                "failure_type": "deploy",
                "probe": "B",
            }
        return {
            "stack_name": f"s-{probe.stack_suffix}",
            "success": True,
            "probe": probe.name,
        }

    monkeypatch.setattr(cbd, "deploy_and_test_probe", fake_deploy)
    probes = [_make_probe(cbd, name=n, suffix=n.lower()) for n in ("A", "B", "C")]

    results = cbd.run_variant_probes("a@b.com", "https://tmpl", probes=probes)

    by_name = {r["probe"]: r for r in results}
    assert by_name["A"]["success"] is True
    assert by_name["C"]["success"] is True
    assert by_name["B"]["success"] is False
    assert by_name["B"]["error"] == "B blew up"


def test_launcher_supervisor_guard_records_thread_death(cbd, monkeypatch):
    # deploy_and_test_probe catches its own exceptions, but if a probe thread
    # dies hard the launcher must still record a failure rather than dropping
    # the probe from the summary.
    def fake_deploy(probe, admin_email, template_url):
        raise RuntimeError("thread died")

    monkeypatch.setattr(cbd, "deploy_and_test_probe", fake_deploy)
    probes = [_make_probe(cbd, name="A", suffix="a")]

    results = cbd.run_variant_probes("a@b.com", "https://tmpl", probes=probes)

    assert len(results) == 1
    assert results[0]["success"] is False
    assert results[0]["probe"] == "A"
    assert "thread died" in results[0]["error"]


def test_launcher_respects_concurrency_cap(cbd, monkeypatch):
    # With cap=2 and 4 probes, no more than 2 should ever run at once.
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "2")
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def fake_deploy(probe, admin_email, template_url):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(0.05)
        with lock:
            state["current"] -= 1
        return {"stack_name": "s", "success": True, "probe": probe.name}

    monkeypatch.setattr(cbd, "deploy_and_test_probe", fake_deploy)
    probes = [_make_probe(cbd, name=str(i), suffix=str(i)) for i in range(4)]

    results = cbd.run_variant_probes("a@b.com", "https://tmpl", probes=probes)

    assert len(results) == 4
    assert state["peak"] <= 2, f"peak concurrency {state['peak']} exceeded cap 2"


def test_launcher_empty_table_is_noop(cbd, monkeypatch):
    monkeypatch.setattr(
        cbd,
        "deploy_and_test_probe",
        lambda *a, **k: pytest.fail("should not deploy with no probes"),
    )
    assert cbd.run_variant_probes("a@b.com", "https://tmpl", probes=[]) == []


# --------------------------------------------------------------------------- #
# The default probe table (regression guard on the migrated GLOBAL probe)
# --------------------------------------------------------------------------- #


def test_default_probe_table_has_global_apigw_row(cbd):
    names = [p.name for p in cbd.PROBE_VARIANTS]
    assert any("APIGateway" in n and "GLOBAL" in n for n in names)
    apigw = next(
        p for p in cbd.PROBE_VARIANTS if "APIGateway" in p.name and "GLOBAL" in p.name
    )
    assert apigw.stack_suffix == "apigw"
    assert apigw.deploy_params == {
        "WebUIHosting": "APIGateway",
        "ApiGatewayVisibility": "GLOBAL",
    }
    assert apigw.validate_fn is cbd.validate_apigw_global_hosting
    assert apigw.requires_vpc is False


def test_default_probe_table_covers_all_four_variants(cbd):
    by_suffix = {p.stack_suffix: p for p in cbd.PROBE_VARIANTS}
    # The four deployment-hosting variants are present (the ZAP DAST probe is a
    # fifth row, asserted separately in test_zap_dast_probe.py).
    assert {"apigw", "waf", "apigwpriv", "jobsapi"} <= set(by_suffix)
    # No-VPC probes.
    assert by_suffix["apigw"].requires_vpc is False
    assert by_suffix["waf"].requires_vpc is False
    # VPC-requiring probes flagged so their VPC params are injected from env.
    assert by_suffix["apigwpriv"].requires_vpc is True
    assert by_suffix["jobsapi"].requires_vpc is True
    # Distinguishing params.
    assert by_suffix["waf"].deploy_params.get("WAFAllowedIPv4Ranges")
    assert by_suffix["apigwpriv"].deploy_params["ApiGatewayVisibility"] == "PRIVATE"
    assert by_suffix["jobsapi"].deploy_params["EnableJobsApi"] == "true"
    # Every row wires a distinct validator.
    validators = {p.validate_fn for p in cbd.PROBE_VARIANTS}
    assert len(validators) == len(cbd.PROBE_VARIANTS)


def test_probe_deploy_params_carry_no_vpc_params_statically(cbd):
    # VPC params must NOT be hardcoded in the table — they are injected at
    # runtime from env so the same row works with or without the test VPC.
    for p in cbd.PROBE_VARIANTS:
        assert "VpcId" not in p.deploy_params
        assert "DeployInVPC" not in p.deploy_params


# --------------------------------------------------------------------------- #
# _test_vpc_params — persistent-test-VPC env resolution
# --------------------------------------------------------------------------- #


def test_vpc_params_none_when_env_unset(cbd, monkeypatch):
    _clear_test_vpc_env(monkeypatch)
    assert cbd._test_vpc_params() is None


def test_vpc_params_none_when_partially_set(cbd, monkeypatch):
    _set_test_vpc_env(monkeypatch)
    # Missing any one of the four core ids → None (can't deploy in-VPC safely).
    monkeypatch.delenv("IDP_TEST_APIGW_VPCE_ID", raising=False)
    assert cbd._test_vpc_params() is None


def test_vpc_params_full_mapping(cbd, monkeypatch):
    _set_test_vpc_env(monkeypatch)
    params = cbd._test_vpc_params()
    assert params["DeployInVPC"] == "true"
    assert params["VpcId"] == "vpc-abc123"
    # subnet list passed verbatim (comma-joined) to both subnet params
    assert params["PrivateSubnetIds"] == "subnet-a,subnet-b"
    assert params["LambdaSubnetIds"] == "subnet-a,subnet-b"
    assert params["LambdaSecurityGroupId"] == "sg-xyz789"
    assert params["ApiGatewayVpcEndpointId"] == "vpce-def456"


# --------------------------------------------------------------------------- #
# VPC-probe skip + injection behavior
# --------------------------------------------------------------------------- #


def test_vpc_probe_skips_when_no_test_vpc(cbd, monkeypatch):
    _clear_test_vpc_env(monkeypatch)
    calls = _stub_lifecycle(cbd, monkeypatch)
    probe = _make_probe(cbd, name="Jobs API", suffix="jobsapi", requires_vpc=True)

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    # Skipped, not failed — absent infra is not a regression.
    assert result["success"] is True
    assert result["skipped"] is True
    assert result["probe"] == "Jobs API"
    # Nothing deployed: no IAM, no commands, no cleanup.
    assert calls["iam"] == []
    assert calls["commands"] == []
    assert calls["cleanup"] == []


def test_vpc_probe_injects_vpc_params_into_deploy(cbd, monkeypatch):
    _set_test_vpc_env(monkeypatch)
    calls = _stub_lifecycle(cbd, monkeypatch)
    probe = _make_probe(
        cbd,
        name="PRIVATE",
        suffix="apigwpriv",
        params={"WebUIHosting": "APIGateway", "ApiGatewayVisibility": "PRIVATE"},
        requires_vpc=True,
    )

    result = cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    assert result["success"] is True
    deploy_cmd = next(c for c in calls["commands"] if "idp-cli deploy" in c)
    # Probe params AND injected VPC params both present.
    assert "ApiGatewayVisibility=PRIVATE" in deploy_cmd
    assert "DeployInVPC=true" in deploy_cmd
    assert "VpcId=vpc-abc123" in deploy_cmd
    assert "LambdaSubnetIds=subnet-a,subnet-b" in deploy_cmd
    assert "ApiGatewayVpcEndpointId=vpce-def456" in deploy_cmd


def test_no_vpc_probe_injects_nothing(cbd, monkeypatch):
    # A requires_vpc=False probe must not get VPC params even if the env is set.
    _set_test_vpc_env(monkeypatch)
    calls = _stub_lifecycle(cbd, monkeypatch)
    probe = _make_probe(
        cbd, suffix="apigw", params={"WebUIHosting": "APIGateway"}, requires_vpc=False
    )

    cbd.deploy_and_test_probe(probe, "a@b.com", "https://tmpl")

    deploy_cmd = next(c for c in calls["commands"] if "idp-cli deploy" in c)
    assert "DeployInVPC" not in deploy_cmd
    assert "VpcId" not in deploy_cmd


def test_launcher_folds_skipped_probe(cbd, monkeypatch):
    _clear_test_vpc_env(monkeypatch)
    monkeypatch.setenv("IDP_PROBE_MAX_CONCURRENCY", "4")
    # Real deploy_and_test_probe (not stubbed) so the skip path executes; stub
    # only the AWS-touching helpers in case a non-VPC probe runs.
    _stub_lifecycle(cbd, monkeypatch)
    probes = [
        _make_probe(cbd, name="novpc", suffix="nv", requires_vpc=False),
        _make_probe(cbd, name="needsvpc", suffix="hv", requires_vpc=True),
    ]
    results = cbd.run_variant_probes("a@b.com", "https://tmpl", probes=probes)
    by_name = {r["probe"]: r for r in results}
    assert by_name["novpc"]["success"] is True
    assert not by_name["novpc"].get("skipped")
    assert by_name["needsvpc"]["skipped"] is True


# --------------------------------------------------------------------------- #
# New validators (mock boto3)
# --------------------------------------------------------------------------- #


def _fake_boto3(cbd, monkeypatch, clients):
    """Patch cbd.boto3.client to return the given {service: mock} objects."""
    monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: clients[name])


class _FakeApiGw:
    def __init__(self, apis=None, rest_api=None):
        self._apis = apis or []
        self._rest_api = rest_api

    def get_rest_apis(self, limit=500):
        return {"items": self._apis}

    def get_rest_api(self, restApiId):
        if self._rest_api is None:
            raise Exception("NotFoundException")
        return self._rest_api


class _FakeCfn:
    def __init__(self, outputs):
        self._outputs = outputs

    def describe_stacks(self, StackName):
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": k, "OutputValue": v}
                        for k, v in self._outputs.items()
                    ]
                }
            ]
        }


def test_validate_private_hosting_pass(cbd, monkeypatch):
    apis = [
        {
            "name": "idp-s-api",
            "endpointConfiguration": {"types": ["PRIVATE"]},
            "policy": "{...}",
        }
    ]
    _fake_boto3(cbd, monkeypatch, {"apigateway": _FakeApiGw(apis=apis)})
    res = cbd.validate_apigw_private_hosting("idp-s")
    assert res["success"] is True
    assert "PRIVATE" in res["endpoint_types"]


def test_validate_private_hosting_fails_when_regional(cbd, monkeypatch):
    apis = [
        {
            "name": "idp-s-api",
            "endpointConfiguration": {"types": ["REGIONAL"]},
            "policy": "{...}",
        }
    ]
    _fake_boto3(cbd, monkeypatch, {"apigateway": _FakeApiGw(apis=apis)})
    res = cbd.validate_apigw_private_hosting("idp-s")
    assert res["success"] is False
    assert "PRIVATE" in res["error"]


def test_validate_private_hosting_fails_without_policy(cbd, monkeypatch):
    apis = [{"name": "idp-s-api", "endpointConfiguration": {"types": ["PRIVATE"]}}]
    _fake_boto3(cbd, monkeypatch, {"apigateway": _FakeApiGw(apis=apis)})
    res = cbd.validate_apigw_private_hosting("idp-s")
    assert res["success"] is False
    assert "resource policy" in res["error"]


def test_validate_jobs_api_pass(cbd, monkeypatch):
    outputs = {
        "ApiGatewayEndpoint": "https://abc123.execute-api.us-east-1.amazonaws.com/beta"
    }
    _fake_boto3(
        cbd,
        monkeypatch,
        {
            "cloudformation": _FakeCfn(outputs),
            "apigateway": _FakeApiGw(rest_api={"id": "abc123"}),
        },
    )
    res = cbd.validate_jobs_api("idp-s")
    assert res["success"] is True
    assert "execute-api" in res["jobs_url"]


def test_validate_jobs_api_fails_without_output(cbd, monkeypatch):
    _fake_boto3(
        cbd, monkeypatch, {"cloudformation": _FakeCfn({}), "apigateway": _FakeApiGw()}
    )
    res = cbd.validate_jobs_api("idp-s")
    assert res["success"] is False
    assert "ApiGatewayEndpoint" in res["error"]


class _FakeWafv2:
    def __init__(self, acls=None, resources=None):
        self._acls = acls or []
        self._resources = resources or []

    def list_web_acls(self, Scope, Limit=100):
        return {"WebACLs": self._acls}

    def list_resources_for_web_acl(self, WebACLArn, ResourceType):
        return {"ResourceArns": self._resources}


def test_validate_waf_pass(cbd, monkeypatch):
    acls = [{"Name": "idp-s-api-acl", "ARN": "arn:aws:wafv2:...:webacl/idp-s-api-acl"}]
    _fake_boto3(
        cbd, monkeypatch, {"wafv2": _FakeWafv2(acls=acls, resources=["arn:...:stage"])}
    )
    res = cbd.validate_waf_enabled("idp-s")
    assert res["success"] is True


def test_validate_waf_fails_when_absent(cbd, monkeypatch):
    _fake_boto3(cbd, monkeypatch, {"wafv2": _FakeWafv2(acls=[])})
    res = cbd.validate_waf_enabled("idp-s")
    assert res["success"] is False
    assert "not found" in res["error"]


def test_validate_waf_fails_when_unassociated(cbd, monkeypatch):
    acls = [{"Name": "idp-s-api-acl", "ARN": "arn:aws:wafv2:...:webacl/idp-s-api-acl"}]
    _fake_boto3(cbd, monkeypatch, {"wafv2": _FakeWafv2(acls=acls, resources=[])})
    res = cbd.validate_waf_enabled("idp-s")
    assert res["success"] is False
    assert "not associated" in res["error"]


# --------------------------------------------------------------------------- #
# build_consolidated_summary — the always-on status table
# --------------------------------------------------------------------------- #


def test_consolidated_summary_pass(cbd):
    primary = {
        "stack_name": "idp-x",
        "success": True,
        "step_results": {
            "Step 3: Default config": {"status": "passed", "error": ""},
            "Step 4: BDA mode": {"status": "passed", "error": ""},
        },
    }
    probes = [
        {"probe": "GLOBAL", "success": True},
        {"probe": "Jobs API", "success": True, "skipped": True, "detail": "no VPC"},
    ]
    out = cbd.build_consolidated_summary("idp-x", primary, probes, True)
    assert "OVERALL: PASS" in out
    assert "Step 3: Default config" in out
    assert "GLOBAL" in out
    assert "Jobs API" in out
    # A skipped probe must NOT flip the overall result to FAIL.
    assert "OVERALL: FAIL" not in out


def test_consolidated_summary_fail_on_primary_step(cbd):
    primary = {
        "stack_name": "idp-y",
        "success": False,
        "failure_type": "test",
        "error": "boom",
        "step_results": {
            "Step 3: Default config": {"status": "passed", "error": ""},
            "Step 7: Test Studio": {"status": "failed", "error": "eval timeout"},
            "Step 12: API RBAC": {"status": "cancelled", "error": ""},
        },
    }
    out = cbd.build_consolidated_summary("idp-y", primary, [], True)
    assert "OVERALL: FAIL" in out
    assert "eval timeout" in out


def test_consolidated_summary_fail_on_probe(cbd):
    primary = {"stack_name": "idp-z", "success": True, "step_results": {}}
    probes = [{"probe": "WAF", "success": False, "error": "WebACL missing"}]
    out = cbd.build_consolidated_summary("idp-z", primary, probes, True)
    assert "OVERALL: FAIL" in out
    assert "WebACL missing" in out


def test_consolidated_summary_publish_failure(cbd):
    out = cbd.build_consolidated_summary("idp-z", None, [], False)
    assert "OVERALL: FAIL" in out
    assert "Not run (publish failed)" in out


# --------------------------------------------------------------------------- #
# create_iam_resources — adaptive retry so concurrent IAM bursts ride out the
# account-wide throttle instead of dying at "reached max retries: 4"
# --------------------------------------------------------------------------- #


def test_create_iam_resources_uses_adaptive_retry_clients(cbd, monkeypatch):
    # The IAM CreatePolicy throttle killed a probe at Step 0 (before deploy).
    # Assert every boto3 client built here gets the adaptive-retry Config so the
    # burst backs off through the throttle.
    seen_configs = []

    class _Cfn:
        def create_stack(self, **k):
            return {}

        def get_waiter(self, name):
            return type("W", (), {"wait": lambda self, **k: None})()

        def describe_stacks(self, StackName):
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {
                                "OutputKey": "ServiceRoleArn",
                                "OutputValue": "arn:aws:iam::1:role/r",
                            }
                        ]
                    }
                ]
            }

        exceptions = type("E", (), {"AlreadyExistsException": Exception})()

    class _Iam:
        def create_policy(self, **k):
            return {}

        exceptions = type("E", (), {"EntityAlreadyExistsException": Exception})()

    class _Sts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    impls = {"cloudformation": _Cfn(), "iam": _Iam(), "sts": _Sts()}

    def fake_client(name, *a, **k):
        seen_configs.append((name, k.get("config")))
        return impls[name]

    monkeypatch.setattr(cbd.boto3, "client", fake_client)
    # Point the template open() at any readable file (content is not parsed here).
    import builtins

    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open", lambda *a, **k: real_open(__file__), raising=True
    )

    role_arn, boundary_arn = cbd.create_iam_resources("idp-test")
    assert role_arn and boundary_arn

    # The cloudformation + iam clients (mutating, burst-prone) must use adaptive
    # retry with raised max_attempts.
    by_service = {name: cfg for name, cfg in seen_configs}
    for svc in ("cloudformation", "iam"):
        cfg = by_service.get(svc)
        assert cfg is not None, f"{svc} client built without a Config"
        assert cfg.retries["mode"] == "adaptive"
        assert cfg.retries["max_attempts"] >= 5


# --------------------------------------------------------------------------- #
# GLOBAL APIGateway hosting: the CSP the SPA document must actually carry
#
# The policy is a static API Gateway response-parameter value whose interior
# contains single quotes, and no offline check can prove the service emits it
# intact. This probe is the only place that observes the real header, so its
# assertions are what stand between a mis-parse and a blank SPA in a
# GovCloud/private deployment (threat UI.T07).
# --------------------------------------------------------------------------- #

_APIGW_APIS = [{"name": "idp-s-api", "endpointConfiguration": {"types": ["REGIONAL"]}}]
_APIGW_OUTPUTS = {
    "ApplicationWebURL": "https://abc123.execute-api.us-east-1.amazonaws.com/api"
}
_GOOD_CSP = (
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net/npm/; "
    "object-src 'none'; frame-ancestors 'none';"
)


def _apigw_probe(cbd, monkeypatch, csp_header, http_code="200"):
    """Run validate_apigw_global_hosting with a canned response-header block."""
    _fake_boto3(
        cbd,
        monkeypatch,
        {
            "apigateway": _FakeApiGw(apis=_APIGW_APIS),
            "cloudformation": _FakeCfn(_APIGW_OUTPUTS),
        },
    )

    def fake_run_command(cmd, check=True, timeout=None):
        # One request carries both the header dump and the status code, so the
        # probe cannot judge the headers of a response it never status-checked.
        assert "-D -" in cmd and "http_code" in cmd, cmd
        # A redirect hop first: only the FINAL block is the rendered document.
        body = "HTTP/2 301\r\nlocation: /api/\r\ncontent-security-policy: bogus\r\n\r\n"
        body += "HTTP/2 200\r\ncontent-type: text/html\r\n"
        if csp_header is not None:
            body += f"content-security-policy: {csp_header}\r\n"
        return _Completed(stdout=body + f"\n{http_code}")

    monkeypatch.setattr(cbd, "run_command", fake_run_command)
    return cbd.validate_apigw_global_hosting("idp-s")


def test_apigw_global_hosting_pass_includes_the_csp(cbd, monkeypatch):
    res = _apigw_probe(cbd, monkeypatch, _GOOD_CSP)
    assert res["success"] is True
    assert res["csp"] == _GOOD_CSP


def test_apigw_global_hosting_fails_without_a_csp(cbd, monkeypatch):
    """APIGateway mode shipped with no CSP at all for several releases."""
    res = _apigw_probe(cbd, monkeypatch, None)
    assert res["success"] is False
    assert "no Content-Security-Policy" in res["error"]


def test_apigw_global_hosting_fails_when_the_quoted_keywords_are_lost(cbd, monkeypatch):
    """`script-src self` (unquoted) reads as a HOST named "self" — SPA blanks.

    This is the failure mode of API Gateway mis-parsing the single-quoted static
    value, and it returns HTTP 200 with a header present, so nothing but this
    assertion would catch it.
    """
    res = _apigw_probe(cbd, monkeypatch, "script-src self unsafe-inline;")
    assert res["success"] is False
    assert "lost its quoted keywords" in res["error"]


def test_apigw_global_hosting_fails_on_a_blanket_https_script_src(cbd, monkeypatch):
    """The v0.6.x AppSec finding, asserted against the deployed header."""
    res = _apigw_probe(
        cbd, monkeypatch, "script-src 'self' 'unsafe-inline' https:; object-src 'none';"
    )
    assert res["success"] is False
    assert "allows any origin via ['https:']" in res["error"]


def test_apigw_global_hosting_accepts_an_https_host_source(cbd, monkeypatch):
    """A `https://host` source must not trip the blanket-`https:` check."""
    res = _apigw_probe(
        cbd, monkeypatch, "script-src 'self' https://cdn.jsdelivr.net/npm/;"
    )
    assert res["success"] is True


def test_apigw_global_hosting_fails_when_the_outer_quotes_survive(cbd, monkeypatch):
    """The other half of the mis-parse: `'script-src ...'` with quotes intact.

    Browsers discard an invalid first directive, and this policy has no
    `default-src` to fall back on, so the header is present and enforces nothing.
    """
    res = _apigw_probe(cbd, monkeypatch, f"'{_GOOD_CSP}'")
    assert res["success"] is False
    assert "static-value quotes" in res["error"]


def test_apigw_global_hosting_accepts_reordered_script_src_sources(cbd, monkeypatch):
    """CSP source order is not significant, so the probe must not depend on it."""
    res = _apigw_probe(
        cbd,
        monkeypatch,
        "script-src 'unsafe-inline' https://cdn.jsdelivr.net/npm/ 'self'; "
        "frame-ancestors 'none'",
    )
    assert res["success"] is True


def test_apigw_global_hosting_accepts_a_policy_ending_in_a_keyword(cbd, monkeypatch):
    """A trailing `'none'` is not a leftover static-value quote."""
    res = _apigw_probe(cbd, monkeypatch, "script-src 'self'; frame-ancestors 'none'")
    assert res["success"] is True


def test_apigw_global_hosting_fails_without_a_script_src(cbd, monkeypatch):
    res = _apigw_probe(cbd, monkeypatch, "object-src 'none'; base-uri 'none';")
    assert res["success"] is False
    assert "no script-src" in res["error"]


def test_apigw_global_hosting_reports_a_bad_status_not_a_missing_csp(cbd, monkeypatch):
    """A throttled/5xx response must be blamed on the status, not the template.

    `DEFAULT_5XX` gateway responses deliberately carry no CSP, so judging headers
    from a response that was never status-checked would report "no
    Content-Security-Policy header" for a transient.
    """
    res = _apigw_probe(cbd, monkeypatch, None, http_code="503")
    assert res["success"] is False
    assert "expected 200" in res["error"]
