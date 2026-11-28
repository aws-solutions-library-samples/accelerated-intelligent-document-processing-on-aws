# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Resource parity on a Terraform run (issue #2018).

A Terraform schedule that also carries ``IDP_TERRAFORM_PARITY=true`` deploys a
CloudFormation twin of the same commit beside the Terraform deployment, compares
the two with ``terraform/tools/parity.py`` against its baseline, reports the
verdict as a row of the step table and tears the twin down on every path.

Pinned here, offline, with fakes for the deploys, the teardown and the AWS
clients the tool reads (the tool itself runs for real):

* the request counts on a schedule only and needs a Terraform run, and reaches
  the build in ``ci_run_options.json``;
* the verdict follows the tool's own ``Report.ok`` (an unexplained difference, a
  one-sided resource and a stale baseline entry fail; an explained difference
  and an unreadable type do not), and a comparison that could not run is an
  error worded apart from a difference;
* no property value reaches the build log or the row;
* the twin is torn down after a failed deploy, a deploy that raised, a run that
  never compared, and a run that did, and the row fails the run without stopping
  the suite.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
STACK = "idp-0101-000000"
TWIN = f"{STACK}-cfn"
ROLE = "arn:aws:iam::123456789012:role/idp-0101-000000-iam-TFDeployRole"
#: A value only Cloud Control returns; it must never be printed.
SECRET_VALUE = "client-secret-value-that-must-stay-out-of-the-build-log"


@pytest.fixture
def itd():
    import integration_test_deployment as module

    return module


# ------------------------------------------------------------- the request


def test_the_request_counts_on_a_terraform_schedule_only(itd):
    sched = {"CI_PIPELINE_SOURCE": "schedule", "IDP_IAC": "terraform"}
    assert itd.terraform_parity_requested({**sched, "IDP_TERRAFORM_PARITY": "true"})
    assert not itd.terraform_parity_requested(sched)
    assert not itd.terraform_parity_requested(
        {**sched, "IDP_TERRAFORM_PARITY": "true", "CI_PIPELINE_SOURCE": "push"}
    )


def test_the_request_is_refused_on_a_cloudformation_schedule(itd):
    with pytest.raises(ValueError, match="IDP_IAC=terraform"):
        itd.terraform_parity_requested(
            {"CI_PIPELINE_SOURCE": "schedule", "IDP_TERRAFORM_PARITY": "true"}
        )


def test_the_request_reaches_the_build_and_is_read_back(cbd, itd, tmp_path):
    env = {
        "CI_PIPELINE_SOURCE": "schedule",
        "IDP_IAC": "terraform",
        "IDP_TERRAFORM_PARITY": "true",
    }
    options = itd.write_ci_run_options(str(tmp_path / "o.json"), environ=env)
    assert options["terraform_parity"] is True
    assert cbd.terraform_parity_mode(environ={}, options=options) is True
    assert not cbd.terraform_parity_mode(environ={}, options={})
    # The build's own environment wins, as for IDP_IAC.
    assert cbd.terraform_parity_mode({"IDP_TERRAFORM_PARITY": "true"}, {})


def test_the_build_refuses_parity_without_a_terraform_run(cbd):
    assert cbd.terraform_parity_unsupported_reason({"IDP_IAC": "terraform"}) is None
    reason = cbd.terraform_parity_unsupported_reason({"IDP_IAC": "cloudformation"})
    assert reason and "IDP_IAC=terraform" in reason


def test_the_twin_is_named_under_the_run_prefix(cbd):
    # The startup sweeps group a run's stacks by this prefix.
    assert (
        cbd._IDP_RUN_PREFIX_RE.match(cbd.terraform_parity_twin_name(STACK)).group(1)
        == STACK
    )


# ------------------------------------------- the tool, against fake clients


class _Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, StackName):
        return [{"StackResourceSummaries": self.pages[StackName]}]


class _Cfn:
    def __init__(self, stacks, described):
        self.stacks, self.described = stacks, described

    def get_paginator(self, name):
        assert name == "list_stack_resources"
        return _Paginator(self.stacks)

    def describe_stacks(self, StackName):
        return {"Stacks": [self.described[StackName]]}


class _S3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[(Bucket, Key)]).encode())}


class _CloudControl:
    def __init__(self, props):
        self.props = props

    def get_resource(self, TypeName, Identifier):
        return {
            "ResourceDescription": {
                "Properties": json.dumps(self.props[(TypeName, Identifier)])
            }
        }


class _Session:
    def __init__(self, clients):
        self.clients = clients

    def client(self, name, config=None):
        self.configs = getattr(self, "configs", {})
        self.configs[name] = config
        return self.clients[name]


def _summary(lid, rtype, physical):
    return {
        "LogicalResourceId": lid,
        "ResourceType": rtype,
        "PhysicalResourceId": physical,
    }


def _session(tf_secret=SECRET_VALUE, cfn_secret=SECRET_VALUE, tf_extra=()):
    """The twin and the Terraform deployment: an app client each, and a CodeBuild
    project each that differs only where the committed baseline says it does, so
    the real baseline has no stale entry."""
    stacks = {
        TWIN: [
            _summary("Client", "AWS::Cognito::UserPoolClient", "c1"),
            _summary("Build", "AWS::CodeBuild::Project", "b1"),
        ]
    }
    described = {
        STACK: {
            "Outputs": [
                {
                    "OutputKey": "TerraformResourceDirectory",
                    "OutputValue": "s3://bridge/dir.json",
                }
            ]
        }
    }
    directory = {
        "root": {
            "resources": [
                _summary("Client", "AWS::Cognito::UserPoolClient", "t1"),
                _summary("Build", "AWS::CodeBuild::Project", "b2"),
                *tf_extra,
            ],
            "nested": {},
        }
    }
    props = {
        ("AWS::Cognito::UserPoolClient", "c1"): {
            "ClientName": f"{TWIN}-client",
            "ClientSecret": cfn_secret,
        },
        ("AWS::Cognito::UserPoolClient", "t1"): {
            "ClientName": f"{STACK}-client",
            "ClientSecret": tf_secret,
        },
        ("AWS::CodeBuild::Project", "b1"): {"Name": "b1", "Artifacts": {"Type": "S3"}},
        ("AWS::CodeBuild::Project", "b2"): {
            "Name": "b2",
            "Artifacts": {"Type": "S3", "OverrideArtifactName": False},
            "LogsConfig": {"CloudWatchLogs": {"Status": "ENABLED"}},
        },
    }
    return _Session(
        {
            "cloudformation": _Cfn(stacks, described),
            "s3": _S3({("bridge", "dir.json"): directory}),
            "cloudcontrol": _CloudControl(props),
            "lambda": object(),
            "sqs": object(),
        }
    )


def test_the_reads_ride_out_throttling(cbd):
    session = _session()
    cbd.check_terraform_parity(TWIN, STACK, ROLE, session=session)
    assert session.configs["cloudcontrol"] is cbd._THROTTLE_RETRY_CONFIG
    assert session.configs["lambda"] is cbd._THROTTLE_RETRY_CONFIG
    assert session.configs["cloudformation"] is cbd._THROTTLE_RETRY_CONFIG
    assert session.configs["sqs"] is cbd._THROTTLE_RETRY_CONFIG


def test_two_matching_deployments_are_the_same(cbd):
    verdict = cbd.check_terraform_parity(TWIN, STACK, ROLE, session=_session())
    assert verdict["status"] == "same", verdict
    assert "2 difference(s) explained" in verdict["detail"]
    assert cbd.terraform_parity_rows(verdict) == {
        cbd.TERRAFORM_PARITY_STEP: {"status": "passed", "error": ""}
    }


def test_a_difference_fails_and_no_value_is_printed(cbd, capsys):
    verdict = cbd.check_terraform_parity(
        TWIN, STACK, ROLE, session=_session(tf_secret="another-" + SECRET_VALUE)
    )
    assert verdict["status"] == "differs"
    assert "Client .ClientSecret" in verdict["detail"]
    assert verdict["detail"].startswith("differs: 1 unexplained difference(s)")
    out = capsys.readouterr().out
    assert "Client .ClientSecret" in out
    assert SECRET_VALUE not in out and SECRET_VALUE not in verdict["detail"]
    row = cbd.terraform_parity_rows(verdict)[cbd.TERRAFORM_PARITY_STEP]
    assert row["status"] == "failed"


def test_a_resource_on_one_side_only_fails(cbd):
    extra = (_summary("Extra", "AWS::SQS::Queue", "q1"),)
    verdict = cbd.check_terraform_parity(
        TWIN, STACK, ROLE, session=_session(tf_extra=extra)
    )
    assert verdict["status"] == "differs"
    assert "Extra (only in Terraform)" in verdict["detail"]


def test_a_comparison_that_could_not_run_is_an_error_not_a_difference(cbd):
    class Denied:
        def client(self, name, config=None):
            raise RuntimeError("AccessDenied")

    verdict = cbd.check_terraform_parity(TWIN, STACK, ROLE, session=Denied())
    assert verdict["status"] == "error"
    assert verdict["detail"].startswith("not compared:")
    assert "AccessDenied" in verdict["detail"]
    row = cbd.terraform_parity_rows(verdict)[cbd.TERRAFORM_PARITY_STEP]
    assert row["status"] == "failed"


# --------------------------------------- the verdict follows Report.ok


@pytest.fixture
def tool(cbd):
    return cbd._terraform_parity_tool()


def _report(tool, **fields):
    report = tool.Report(compared=3)
    for k, v in fields.items():
        setattr(report, k, v)
    return report


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({}, "same"),
        ({"explained": [("A", ".P", "x")]}, "same"),
        ({"unreadable": ["A (AWS::X::Y)"]}, "same"),
        ({"differences": [("A", ".P", "x")]}, "differs"),
        ({"only_cfn": ["A"]}, "differs"),
        ({"only_tf": ["A"]}, "differs"),
        ({"stale": [("AWS::X::Y", ".P")]}, "differs"),
    ],
)
def test_the_verdict_is_the_tools_own(cbd, tool, fields, status):
    report = _report(tool, **fields)
    verdict = cbd.judge_terraform_parity(report)
    assert verdict["status"] == status
    assert (status == "same") == report.ok


def test_the_row_counts_the_policies_compared_through_their_targets(cbd, tool):
    """A per-target policy is not a one-sided resource, so the row passes; the
    count keeps that visible rather than silent."""
    report = _report(tool, through_target=["P (AWS::SQS::QueuePolicy: q)"] * 2)
    verdict = cbd.judge_terraform_parity(report)
    assert verdict["status"] == "same"
    assert (
        "2 per-target polic(y/ies) compared through their targets"
        in (verdict["detail"])
    )


def test_the_listed_findings_are_bounded(cbd, tool):
    n = cbd.TERRAFORM_PARITY_LISTED_DIFFERENCES + 7
    diffs = [(f"R{i}", ".P", "value") for i in range(n)]
    verdict = cbd.judge_terraform_parity(_report(tool, differences=diffs))
    assert "; and 7 more" in verdict["detail"]
    assert f"R{n - 1} .P" not in verdict["detail"]
    assert len(verdict["findings"]) == n


# ------------------------------------------------------ the twin's lifetime


class _Calls:
    def __init__(self, deploy_result=None, raises=None):
        self.deploy_result = deploy_result or {"success": True}
        self.raises = raises
        self.deployed = []
        self.torn_down = []
        self.compared = []

    def deploy(self, name, email, url):
        self.deployed.append(name)
        if self.raises:
            raise self.raises
        return self.deploy_result

    def teardown(self, name):
        self.torn_down.append(name)
        return []

    def check(self, twin, stack, role):
        self.compared.append((twin, stack, role))
        return {"status": "same", "detail": "ok", "findings": []}


def _twin(cbd, calls):
    return cbd.TerraformParityTwin(
        STACK,
        "a@example.test",
        "https://t",
        deploy=calls.deploy,
        teardown=calls.teardown,
    ).start()


def test_a_deployed_twin_is_compared_then_torn_down(cbd):
    calls = _Calls()
    twin = _twin(cbd, calls)
    verdict = twin.compare(ROLE, wait_seconds=5, check=calls.check)
    assert verdict["status"] == "same"
    assert calls.compared == [(TWIN, STACK, ROLE)]
    assert twin.join() == []
    assert calls.torn_down == [TWIN]


@pytest.mark.parametrize(
    "calls",
    [
        _Calls(deploy_result={"success": False, "error": "ROLLBACK_COMPLETE"}),
        _Calls(raises=RuntimeError("boom")),
    ],
    ids=["failed", "raised"],
)
def test_a_twin_that_did_not_deploy_is_not_compared_and_is_torn_down(cbd, calls):
    twin = _twin(cbd, calls)
    verdict = twin.compare(ROLE, wait_seconds=5, check=calls.check)
    assert verdict["status"] == "error"
    assert verdict["detail"].startswith("not compared:")
    assert calls.compared == []
    twin.join()
    assert calls.torn_down == [TWIN]


def test_a_twin_that_did_not_deploy_is_torn_down_without_waiting_for_release(cbd):
    """Its resources are freed at once, not held until the suite is over."""
    calls = _Calls(deploy_result={"success": False, "error": "ROLLBACK_COMPLETE"})
    twin = _twin(cbd, calls)
    for _ in range(500):
        if calls.torn_down:
            break
        threading.Event().wait(0.01)
    assert calls.torn_down == [TWIN], "torn down before release() or join()"
    twin.join()
    assert calls.torn_down == [TWIN]


def test_the_twin_survives_the_suites_fail_fast_abort(cbd):
    """The suite's fail-fast kill sweep must not reach the twin's deploy or
    teardown: like a probe, it is an independent stack."""
    seen = []

    def deploy(name, email, url):
        seen.append(getattr(cbd._thread_local, "never_abort", False))
        return {"success": False, "error": "x"}

    def teardown(name):
        seen.append(getattr(cbd._thread_local, "never_abort", False))
        return []

    cbd.TerraformParityTwin(
        STACK, "a@example.test", "https://t", deploy=deploy, teardown=teardown
    ).start().join()
    assert seen == [True, True]


def test_a_run_that_never_compares_still_tears_the_twin_down(cbd):
    calls = _Calls()
    twin = _twin(cbd, calls)
    twin.join()
    assert calls.deployed == [TWIN] and calls.torn_down == [TWIN]


def test_a_twin_still_deploying_is_reported_not_waited_for(cbd):
    gate = threading.Event()
    calls = _Calls()

    def slow(name, email, url):
        gate.wait(5)
        return {"success": True}

    twin = cbd.TerraformParityTwin(
        STACK, "a@example.test", "https://t", deploy=slow, teardown=calls.teardown
    ).start()
    verdict = twin.compare(ROLE, wait_seconds=0, check=calls.check)
    assert (
        verdict["status"] == "error" and "not finished deploying" in verdict["detail"]
    )
    gate.set()
    twin.join()
    assert calls.torn_down == [TWIN] and calls.compared == []


def test_teardown_findings_are_named_as_the_twins(cbd):
    calls = _Calls()
    calls.teardown = lambda name: [f"{name}-iam still standing"]
    twin = _twin(cbd, calls)
    assert twin.join() == [f"parity twin: {TWIN}-iam still standing"]


# -------------------------------------------- the row in the primary suite


class _FakeTwin:
    def __init__(self, verdict):
        self.verdict = verdict
        self.released = False
        self.compared_with = None

    def compare(self, role_arn):
        self.compared_with = role_arn
        return self.verdict

    def release(self):
        self.released = True


@pytest.fixture
def suite(cbd, monkeypatch, tmp_path):
    ran = []

    def _step(stack):
        ran.append(stack)
        return {"success": True}

    steps = [(_step, "Step 3", "x", "d")]
    monkeypatch.setattr(cbd, "PARALLEL_TEST_STEPS", steps)
    monkeypatch.setattr(cbd, "SEQUENTIAL_TEST_STEPS", [])
    monkeypatch.setattr(cbd, "ALL_TEST_STEPS", steps)
    monkeypatch.setattr(cbd, "terraform_suite_time_shortfall", lambda started: None)
    # The steps run with Review Routing off (a new stack turns it on); the upload
    # that switches it off is covered in test_review_routing_off_config.py.
    monkeypatch.setattr(
        cbd, "switch_off_review_routing", lambda stack, region=None: {"success": True}
    )
    run = cbd.TerraformRun(
        module_url="https://example.test/m.zip",
        directory=str(tmp_path / "run"),
        binary="terraform",
        role_arn=ROLE,
    )

    def deploy(twin, success=True):
        monkeypatch.setattr(
            cbd,
            "_deploy_primary_terraform_attempt",
            lambda *a: {
                "stack_name": STACK,
                "success": success,
                "iac": "terraform",
                "convergence": cbd.judge_terraform_replan(0, None),
            },
        )
        return cbd.deploy_and_test_stack(
            STACK, "a@example.test", "https://t", terraform=run, parity_twin=twin
        )

    return deploy, ran


def test_a_difference_fails_the_run_after_the_suite(cbd, suite):
    deploy, ran = suite
    twin = _FakeTwin({"status": "differs", "detail": "differs: X", "findings": []})
    result = deploy(twin)
    assert ran == [STACK], "the suite still runs beside a parity difference"
    assert twin.compared_with == ROLE and twin.released
    assert result["success"] is False and result["parity_failed"] is True
    assert "differs: X" in result["error"]
    rows = list(result["step_results"])
    assert rows[:2] == [cbd.TERRAFORM_CONVERGENCE_STEP, cbd.TERRAFORM_PARITY_STEP]
    summary = cbd.build_consolidated_summary(STACK, result, [], True, {})
    assert "OVERALL: FAIL" in summary and cbd.TERRAFORM_PARITY_STEP in summary
    assert "differs: X" in summary


def test_matching_deployments_pass(cbd, suite):
    deploy, _ = suite
    result = deploy(_FakeTwin({"status": "same", "detail": "", "findings": []}))
    assert result["success"] is True
    assert result["step_results"][cbd.TERRAFORM_PARITY_STEP]["status"] == "passed"
    summary = cbd.build_consolidated_summary(STACK, result, [], True, {})
    assert "OVERALL: PASS" in summary


def test_an_unhealthy_deployment_makes_no_comparison(cbd, suite):
    deploy, ran = suite
    twin = _FakeTwin({"status": "same", "detail": "", "findings": []})
    result = deploy(twin, success=False)
    assert twin.compared_with is None and ran == []
    assert cbd.TERRAFORM_PARITY_STEP not in (result.get("step_results") or {})


def test_without_a_twin_there_is_no_row(cbd, suite):
    deploy, _ = suite
    result = deploy(None)
    assert cbd.TERRAFORM_PARITY_STEP not in result["step_results"]
    assert result["success"] is True


# ------------------------------------------------------------ main()'s order


def test_main_joins_the_twin_before_the_terraform_teardown():
    """The twin's teardown makes boto3 calls on the default session, which the
    Terraform teardown switches to the deploy role, so it must have finished."""
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text(
        encoding="utf-8"
    )
    main = source[source.index("\ndef main():") :]
    join = main.index("join_terraform_parity_twin(parity_twin, main_started)")
    teardown = main.index("teardown_primary_stack(")
    start = main.index("start_terraform_parity_twin(")
    suite = main.index("deploy_and_test_stack(")
    assert start < suite < join < teardown


# -------------------------------------------- main()'s helpers, and review gaps


def test_main_resolves_and_refuses_through_one_helper(cbd):
    env = {"IDP_IAC": "cloudformation", "IDP_TERRAFORM_PARITY": "true"}
    with pytest.raises(ValueError, match="IDP_IAC=terraform"):
        cbd.resolve_terraform_parity({}, STACK, env)
    env = {"IDP_IAC": "terraform"}
    assert cbd.resolve_terraform_parity({"terraform_parity": True}, STACK, env)
    assert env["IDP_TERRAFORM_PARITY"] == "true"
    env = {"IDP_IAC": "terraform"}
    assert not cbd.resolve_terraform_parity({}, STACK, env)
    assert env["IDP_TERRAFORM_PARITY"] == ""
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text(
        encoding="utf-8"
    )
    main = source[source.index("\ndef main():") :]
    assert "parity = resolve_terraform_parity(run_options, stack_name)" in main
    assert "parity_twin=parity_twin," in main
    assert (
        "        parity_twin = start_terraform_parity_twin(\n"
        "            parity, terraform, stack_name, admin_email, template_url\n"
    ) in main
    assert (
        "twin_findings = join_terraform_parity_twin(parity_twin, main_started)\n"
    ) in main


def test_a_parity_run_starts_a_twin_and_no_other_run_does(cbd, monkeypatch):
    started = []

    class Fake:
        def __init__(self, *a):
            started.append(a)

        def start(self):
            return self

    monkeypatch.setattr(cbd, "TerraformParityTwin", Fake)
    run = object()
    assert cbd.start_terraform_parity_twin(False, run, STACK, "e", "u") is None
    assert cbd.start_terraform_parity_twin(True, None, STACK, "e", "u") is None
    assert started == []
    assert isinstance(cbd.start_terraform_parity_twin(True, run, STACK, "e", "u"), Fake)
    assert started == [(STACK, "e", "u")]


def test_the_join_is_bounded_by_the_terraform_destroys_reserve(cbd, monkeypatch):
    monkeypatch.setattr(cbd, "build_timeout_minutes", lambda codebuild=None: 240)
    reserve = cbd.TERRAFORM_TEARDOWN_RESERVE_SECONDS
    assert cbd.terraform_parity_join_seconds(0, now=600) == 240 * 60 - 600 - reserve
    assert cbd.terraform_parity_join_seconds(0, now=240 * 60) == 0
    monkeypatch.setattr(cbd, "build_timeout_minutes", lambda codebuild=None: None)
    assert cbd.terraform_parity_join_seconds(0, now=600) is None
    assert cbd.join_terraform_parity_twin(None, 0) == []

    class Twin:
        def join(self, timeout=None):
            self.timeout = timeout
            return []

    monkeypatch.setattr(cbd, "build_timeout_minutes", lambda codebuild=None: 240)
    twin = Twin()
    started = cbd.time.monotonic()
    cbd.join_terraform_parity_twin(twin, started)
    assert twin.timeout is not None
    assert twin.timeout <= 240 * 60 - reserve


def test_a_teardown_past_the_deadline_is_a_finding_not_a_wait(cbd):
    gate = threading.Event()
    calls = _Calls()

    def slow_teardown(name):
        gate.wait(5)
        return []

    twin = cbd.TerraformParityTwin(
        STACK,
        "a@example.test",
        "https://t",
        deploy=calls.deploy,
        teardown=slow_teardown,
    ).start()
    findings = twin.join(timeout=0.05)
    gate.set()
    assert len(findings) == 1 and "time reserve" in findings[0]
    assert findings[0].startswith(f"parity twin: {TWIN}")


def test_teardown_findings_reach_the_runs_teardown_verdict():
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text(
        encoding="utf-8"
    )
    main = source[source.index("\ndef main():") :]
    assert 'list(result["teardown"].get("findings") or []) + twin_findings' in main


def test_a_label_without_a_resource_directory_is_not_compared(cbd):
    """parity.tf_tree raises SystemExit there; it must not escape, or main()
    would skip both teardowns."""
    session = _session()
    session.clients["cloudformation"].described[STACK] = {"Outputs": []}
    verdict = cbd.check_terraform_parity(TWIN, STACK, ROLE, session=session)
    assert verdict["status"] == "error"
    assert "SystemExit" in verdict["detail"]


def test_the_role_session_leaves_the_default_session_alone(cbd, monkeypatch):
    class Sts:
        def assume_role(self, **kw):
            return {
                "Credentials": {
                    "AccessKeyId": "AKIAIOSFODNN7EXAMPLE",
                    "SecretAccessKey": "EXAMPLE",
                    "SessionToken": "EXAMPLE",
                }
            }

    monkeypatch.setattr(cbd.boto3, "client", lambda name, **kw: Sts())
    sentinel = object()
    monkeypatch.setattr(cbd.boto3, "DEFAULT_SESSION", sentinel)
    before = cbd.boto3.DEFAULT_SESSION
    session = cbd._role_session(ROLE)
    assert cbd.boto3.DEFAULT_SESSION is before
    assert session is not before


def test_compare_waits_for_a_twin_still_deploying(cbd):
    gate = threading.Event()
    calls = _Calls()

    def slow(name, email, url):
        gate.wait(5)
        return {"success": True}

    twin = cbd.TerraformParityTwin(
        STACK, "a@example.test", "https://t", deploy=slow, teardown=calls.teardown
    ).start()
    threading.Timer(0.05, gate.set).start()
    verdict = twin.compare(ROLE, wait_seconds=2, check=calls.check)
    assert verdict["status"] == "same" and calls.compared
    twin.join()


def test_a_failed_twin_names_its_first_failure(cbd):
    events = [
        {
            "logical_id": "Queue",
            "resource_type": "AWS::SQS::Queue",
            "status": "CREATE_FAILED",
            "reason": "Resource creation cancelled",
            "timestamp": "1",
        },
        {
            "logical_id": "Plan",
            "resource_type": "AWS::GuardDuty::MalwareProtectionPlan",
            "status": "CREATE_FAILED",
            "reason": "limit exceeded",
            "timestamp": "2",
        },
    ]
    calls = _Calls(deploy_result={"success": False, "error": "x", "cf_events": events})
    twin = _twin(cbd, calls)
    verdict = twin.compare(ROLE, wait_seconds=5, check=calls.check)
    twin.join()
    assert (
        "first failure: Plan (AWS::GuardDuty::MalwareProtectionPlan)"
        in (verdict["detail"])
    )
    assert "limit exceeded" in verdict["detail"]


def test_a_refused_run_carries_the_parity_failure(cbd, suite, monkeypatch):
    deploy, ran = suite
    monkeypatch.setattr(cbd, "terraform_suite_time_shortfall", lambda s: "no time")
    twin = _FakeTwin({"status": "differs", "detail": "differs: X", "findings": []})
    result = deploy(twin)
    assert ran == [] and result["parity_failed"] is True
    assert result["step_results"][cbd.TERRAFORM_PARITY_STEP]["status"] == "failed"
    prompt = cbd.terraform_failure_prompt(result, STACK)
    assert "terraform/tools/parity.py" in prompt
