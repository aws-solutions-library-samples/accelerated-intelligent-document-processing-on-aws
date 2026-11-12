"""The weekly Terraform-migration schedule (``IDP_RUN_TERRAFORM_MIGRATION=true``).

Pinned here, offline:

* the variable counts on a scheduled pipeline only, and is refused beside the
  other schedule selectors, because the run replaces the primary suite;
* the choice reaches the build in ``ci_run_options.json`` and is read back there;
* the run is labelled in the summary, the failure email and both Slack posts, and
  leaves the UX review stack and the nightly story jobs alone;
* the names it derives satisfy both the deploy role (``idp`` prefix, the
  ``idp-*-TFDeployRole`` grant) and the driver (no overlap, upstream's limit);
* every AWS call of the driver goes through the assumed-role profile.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from pathlib import Path

import codebuild_deployment as cbd
import pytest
import terraform_migration_test as tmt
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_CONFIG = REPO_ROOT / ".gitlab-ci.yml"
LABEL = "WEEKLY TERRAFORM MIGRATION RUN"
VAR = "IDP_RUN_TERRAFORM_MIGRATION"


@pytest.fixture
def itd():
    import integration_test_deployment as module

    return module


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load(CI_CONFIG.read_text(encoding="utf-8"))


# -- the job side -----------------------------------------------------------------


@pytest.mark.parametrize("source", ["merge_request_event", "push", "web", ""])
def test_the_variable_counts_on_a_schedule_only(itd, source):
    assert (
        itd.terraform_migration_requested({VAR: "true", "CI_PIPELINE_SOURCE": source})
        is False
    )


def test_a_schedule_carrying_it_requests_the_run(itd):
    assert itd.terraform_migration_requested(
        {VAR: " TRUE ", "CI_PIPELINE_SOURCE": "schedule"}
    )
    assert not itd.terraform_migration_requested(
        {VAR: "false", "CI_PIPELINE_SOURCE": "schedule"}
    )


@pytest.mark.parametrize(
    "clash",
    [
        {"IDP_RUN_PROBES": "true"},
        {"IDP_TEST_REGION": "eu-central-1"},
        {"IDP_IAC": "terraform"},
    ],
)
def test_it_is_refused_beside_another_selector(itd, clash):
    env = {VAR: "true", "CI_PIPELINE_SOURCE": "schedule", **clash}
    with pytest.raises(ValueError, match="cannot be combined"):
        itd.terraform_migration_requested(env)


def test_the_choice_travels_in_the_run_options(itd, tmp_path):
    options = itd.write_ci_run_options(
        str(tmp_path / "o.json"),
        environ={VAR: "true", "CI_PIPELINE_SOURCE": "schedule"},
    )
    assert options["terraform_migration"] is True
    assert cbd.terraform_migration_mode(environ={}, options=options) is True
    assert (
        cbd.terraform_migration_mode(environ={}, options={"terraform_migration": False})
        is False
    )


# -- the build side ------------------------------------------------------------------


def test_the_build_refuses_what_the_run_cannot_combine_with():
    assert cbd.terraform_migration_unsupported_reason({}) is None
    reason = cbd.terraform_migration_unsupported_reason({"IDP_IAC": "terraform"})
    assert reason is not None and "IDP_IAC" in reason


def test_the_summary_and_subject_are_labelled():
    env = {VAR: "true"}
    assert cbd.label_ci_run("body", env).startswith(f"🔀 {LABEL}")
    assert cbd.label_ci_run("body", {}) == "body"
    assert LABEL.lower() in cbd.failure_subject("idp-x", env)
    assert cbd.failure_subject("idp-x", {}) == "IDP CI failure: idp-x"


def test_a_migration_run_leaves_the_review_stack_alone(monkeypatch):
    options = {
        "pipeline_source": "schedule",
        "ref": "develop",
        "terraform_migration": True,
    }
    monkeypatch.delenv(VAR, raising=False)
    assert cbd.ux_nightly_update_requested(options) is False


@pytest.mark.parametrize("stack", ["idp-1111-235959", "idp-0101-000000"])
def test_the_derived_names_satisfy_the_role_the_reapers_and_the_driver(stack):
    old, new = cbd.terraform_migration_names(stack)
    # The new deployment is the run itself, so the startup sweeps reap a dead
    # build's leftovers of it.
    assert new == stack and cbd._IDP_RUN_PREFIX_RE.match(new)
    assert old.startswith(cbd.MANAGED_STACK_NAME_PREFIX)
    # upstream's IAM role names are its prefix plus 51 characters; IAM allows 64
    assert len(old) <= 13
    assert tmt.validate_names(old, new) == []


def test_a_name_that_is_not_a_run_name_is_refused():
    with pytest.raises(ValueError):
        cbd.terraform_migration_names("my-stack")


@pytest.mark.parametrize("limit_minutes", [None, 240, 300])
def test_the_budget_leaves_the_teardown_inside_the_build_limit(limit_minutes):
    deadline, timeout = cbd.terraform_migration_budget(20 * 60, limit_minutes)
    limit = (limit_minutes or cbd.TERRAFORM_MIGRATION_MIN_TIMEOUT_MINUTES) * 60
    assert deadline + cbd.TERRAFORM_MIGRATION_RESERVE_SECONDS == limit - 20 * 60
    assert deadline < timeout < limit - 20 * 60
    assert cbd.terraform_migration_budget(limit + 60, limit_minutes) == (0, 0)


def test_the_deadline_moves_with_the_builds_own_timeout():
    """The deadline is derived from the limit it is given, so raising the project's
    timeout moves it without a second edit beside it."""
    short, _ = cbd.terraform_migration_budget(0, 240)
    long, _ = cbd.terraform_migration_budget(0, 300)
    assert long - short == 60 * 60


class _FakeCodeBuild:
    def __init__(self, minutes=None, error=None):
        self.minutes, self.error = minutes, error

    def batch_get_builds(self, ids):
        if self.error:
            raise self.error
        return {"builds": [{"timeoutInMinutes": self.minutes}]}


def test_a_build_too_short_for_a_migration_run_is_refused(monkeypatch):
    monkeypatch.setenv("CODEBUILD_BUILD_ID", "app-sdlc:abc")
    minutes = cbd.TERRAFORM_MIGRATION_MIN_TIMEOUT_MINUTES - 60
    reason = cbd.terraform_migration_timeout_shortfall(_FakeCodeBuild(minutes))
    assert reason and f"{minutes} min" in reason and "codepipeline" in reason
    assert (
        cbd.terraform_migration_timeout_shortfall(
            _FakeCodeBuild(cbd.TERRAFORM_MIGRATION_MIN_TIMEOUT_MINUTES)
        )
        is None
    )


def test_an_unreadable_timeout_does_not_refuse_a_migration_run(monkeypatch):
    monkeypatch.setenv("CODEBUILD_BUILD_ID", "app-sdlc:abc")
    fake = _FakeCodeBuild(error=RuntimeError("AccessDenied"))
    assert cbd.terraform_migration_timeout_shortfall(fake) is None
    monkeypatch.delenv("CODEBUILD_BUILD_ID")
    assert cbd.terraform_migration_timeout_shortfall(_FakeCodeBuild(1)) is None


def test_main_refuses_a_short_build_before_publish():
    """The shortfall is checked beside the other refusals, which raise before the
    publish step, not inside run_terraform_migration after it."""
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text()
    main = source.split("\ndef main():", 1)[1]
    check = main.index("terraform_migration_timeout_shortfall()")
    assert check < main.index("terraform_migration_unsupported_reason()") + 200
    assert check < main.index("run_terraform_migration(")
    assert check < main.index("publish_success = True")


def test_the_template_timeout_meets_what_a_migration_run_needs():
    template = REPO_ROOT / "scripts" / "sdlc" / "cfn" / "codepipeline-s3.yml"
    (minutes,) = re.findall(
        r"^\s*TimeoutInMinutes:\s*(\d+)\s*$", template.read_text(), re.MULTILINE
    )
    assert int(minutes) >= cbd.TERRAFORM_MIGRATION_MIN_TIMEOUT_MINUTES


def test_the_monitor_and_the_job_outlast_the_build(itd, config):
    """CodeBuild timeout < monitor handoff < monitor max wait < GitLab job timeout,
    each with an allowance for what runs between their starts."""
    build = cbd.TERRAFORM_MIGRATION_MIN_TIMEOUT_MINUTES * 60
    handoff = itd.MIGRATION_RUN_MONITOR_HANDOFF_SECONDS
    max_wait = itd.MIGRATION_RUN_MONITOR_MAX_WAIT_SECONDS
    assert handoff - build >= 5 * 60
    assert max_wait > handoff
    hours, minutes = re.fullmatch(
        r"(\d+)h(\d+)m", config["integration_tests"]["timeout"]
    ).groups()
    job = (int(hours) * 60 + int(minutes)) * 60
    assert job - max_wait >= 10 * 60


def test_main_watches_the_migration_run_for_longest(itd, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("IDP_RUN_PROBES", "IDP_TEST_REGION", "IDP_IAC"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "schedule")
    monkeypatch.setenv(VAR, "true")
    seen: list[dict] = []
    # write_ci_run_options runs for real, into tmp_path: a migration run refuses
    # to go on without the file.
    monkeypatch.setattr(itd, "create_deployment_package", lambda *a, **k: None)
    monkeypatch.setattr(itd, "upload_to_s3", lambda *a, **k: "v1")
    monkeypatch.setattr(itd, "monitor_pipeline", lambda *a, **k: seen.append(k) or True)
    with pytest.raises(SystemExit):
        itd.main()
    assert seen == [
        {
            "max_wait": itd.MIGRATION_RUN_MONITOR_MAX_WAIT_SECONDS,
            "handoff_after": itd.MIGRATION_RUN_MONITOR_HANDOFF_SECONDS,
        }
    ]


def test_the_profile_assumes_the_role_from_the_container():
    text = cbd.render_migration_aws_config(
        "arn:aws:iam::123456789012:role/r", "us-east-1"
    )
    assert "role_arn = arn:aws:iam::123456789012:role/r" in text
    assert "credential_source = EcsContainer" in text
    assert text.startswith(f"[profile {cbd.TERRAFORM_MIGRATION_PROFILE}]")


def test_the_driver_gets_the_role_for_the_new_deployment_too():
    cmd = cbd.terraform_migration_driver_command(
        "idpo1",
        "idp-m1",
        "https://x/m.zip",
        "arn:r",
        "terraform",
        "/w",
        "us-east-1",
        "123456789012",
        "arn:aws:iam::123456789012:policy/idp-m1-PermissionsBoundary",
    )
    assert cmd[cmd.index("--terraform-assume-role-arn") + 1] == "arn:r"
    assert cmd[cmd.index("--expected-account") + 1] == "123456789012"
    assert cmd[cmd.index("--upstream-iam-path") + 1] == "/idp-m1-upstream/"
    assert cmd[cmd.index("--upstream-permissions-boundary-arn") + 1].endswith(
        "policy/idp-m1-PermissionsBoundary"
    )
    assert "scripts/sdlc/terraform_migration_test.py" in cmd


SERVICE_ROLE_TEMPLATE = (
    REPO_ROOT
    / "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml"
)


def _deploy_role_statements():
    """The derived TFDeployRole's statements, keyed by Sid, with each ``Fn::Sub``
    resource rendered for an ``idp`` prefix in account 123456789012."""
    template = cbd.terraform_deploy_role_template(SERVICE_ROLE_TEMPLATE.read_text())
    role = template["Resources"]["TerraformDeployRole"]
    subs = {
        "AWS::Partition": "aws",
        "AWS::AccountId": "123456789012",
        "ManagedStackNamePrefix": cbd.MANAGED_STACK_NAME_PREFIX,
        "AWS::StackName": "idp-1111-235959-iam",
    }

    def render(value):
        text = value["Fn::Sub"] if isinstance(value, dict) else value
        return re.sub(r"\$\{([^}]+)\}", lambda m: subs[m.group(1)], text)

    out = {}
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            resources = statement.get("Resource", [])
            resources = resources if isinstance(resources, list) else [resources]
            out[statement.get("Sid")] = (
                statement,
                [
                    render(r)
                    for r in resources
                    if isinstance(r, (str, dict))
                    and (isinstance(r, str) or "Fn::Sub" in r)
                ],
            )
    return out


@pytest.mark.parametrize(
    ("sid", "kind", "name"),
    [
        (
            "CreateOrChangeRolesOnlyWithBoundary",
            "role",
            "ConfigurationResolverRole-ab12",
        ),
        ("RoleLifecycleWithinNamePrefix", "role", "WebUIProxyRole-ab12"),
        ("PassStackRolesToStackServices", "role", "UploadResolverRole-ab12"),
        ("ManagedPoliciesWithinNamePrefix", "policy", "UploadResolverS3Policy-ab12"),
    ],
)
def test_upstream_iam_under_the_run_path_is_inside_the_existing_grant(sid, kind, name):
    """Upstream names these from fixed strings, outside role/idp*. Under the run's
    path the ARN is role/<path><name>, which the deploy role's existing statements
    already cover: nothing is widened for the migration run, and without the path
    the same name is outside every one of them."""
    import fnmatch

    path = cbd.terraform_migration_upstream_iam_path("idp-1111-235959")
    assert path.startswith(f"/{cbd.MANAGED_STACK_NAME_PREFIX}")
    statement, patterns = _deploy_role_statements()[sid]
    assert statement["Effect"] == "Allow"
    pathed = f"arn:aws:iam::123456789012:{kind}{path}{name}"
    bare = f"arn:aws:iam::123456789012:{kind}/{name}"
    assert any(fnmatch.fnmatchcase(pathed, p) for p in patterns), patterns
    assert not any(fnmatch.fnmatchcase(bare, p) for p in patterns), patterns


def test_role_creation_still_requires_the_runs_boundary():
    statement, _ = _deploy_role_statements()["CreateOrChangeRolesOnlyWithBoundary"]
    assert statement["Condition"]["StringEquals"]["iam:PermissionsBoundary"] == {
        "Ref": "CreatedRolePermissionsBoundaryArn"
    }


def _run_migration_with_fakes(monkeypatch, tmp_path, returncode=0, logs=None):
    """run_terraform_migration against fakes; returns what it called. ``logs``
    ({name: text}) are written into the driver's log directory by the fake
    driver; Bedrock is faked and records its prompt."""
    seen = {"prompts": []}

    def fake_bedrock(prompt):
        seen["prompts"].append(prompt)
        return "• the analysis"

    monkeypatch.setattr(cbd, "_invoke_bedrock", fake_bedrock)
    monkeypatch.setattr(cbd, "ci_region", lambda: "us-east-1")
    monkeypatch.setattr(cbd, "terraform_work_directory", lambda n: str(tmp_path / n))
    monkeypatch.setattr(cbd, "_caller_principal_arn", lambda: "arn:cb")
    monkeypatch.setattr(
        cbd,
        "create_iam_resources",
        lambda name, **k: ("arn:role", f"arn:aws:iam::1:policy/{name}-B"),
    )
    monkeypatch.setattr(cbd, "_wait_until_assumable", lambda arn: True)
    monkeypatch.setattr(cbd, "cleanup_iam_resources", lambda name: [])
    monkeypatch.setattr(cbd, "build_timeout_minutes", lambda *a: None)

    events = []

    class _Client:
        def get_caller_identity(self):
            return {"Account": "1"}

        def put_object(self, **kwargs):
            events.append(("put", kwargs["Key"], json.loads(kwargs["Body"])))

    monkeypatch.setattr(cbd.boto3, "client", lambda service, **k: _Client())
    monkeypatch.setattr(cbd, "_terraform_state_bucket", lambda: "ci-bucket")
    seen["events"] = events

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        events.append(("driver",))
        work = Path(cmd[cmd.index("--work-dir") + 1])
        for name, text in (logs or {}).items():
            (work / "logs").mkdir(parents=True, exist_ok=True)
            (work / "logs" / name).write_text(text)
        (work / "summary.txt").write_text("❌ phase upstream-deploy failed")

        class _P:
            pass

        _P.returncode = returncode
        return _P()

    monkeypatch.setattr(cbd.subprocess, "run", fake_run)
    seen["result"] = cbd.run_terraform_migration(
        "idp-1111-235959", "https://m.zip", "terraform"
    )
    return seen


def test_a_failed_migration_run_carries_an_ai_analysis_of_its_logs(
    monkeypatch, tmp_path
):
    """The analysis reads the driver's own summary and the tail of its newest
    log, which is where a failing Terraform command prints its error."""
    seen = _run_migration_with_fakes(
        monkeypatch,
        tmp_path,
        returncode=1,
        logs={"old-apply.log": "line\n" * 500 + "Error: AccessDenied iam:CreateRole"},
    )
    ok, summary = seen["result"]
    assert not ok
    assert "AI FAILURE ANALYSIS" in summary and "• the analysis" in summary
    (prompt,) = seen["prompts"]
    assert "phase upstream-deploy failed" in prompt
    assert "Error: AccessDenied iam:CreateRole" in prompt
    assert prompt.count("line\n") <= cbd.TERRAFORM_MIGRATION_ANALYSIS_LOG_LINES


def test_a_passing_migration_run_asks_for_no_analysis(monkeypatch, tmp_path):
    seen = _run_migration_with_fakes(monkeypatch, tmp_path, returncode=0)
    ok, summary = seen["result"]
    assert ok and seen["prompts"] == [] and "AI FAILURE ANALYSIS" not in summary


def test_a_failed_analysis_is_reported_not_raised(monkeypatch):
    def broken(prompt):
        raise RuntimeError("throttled")

    monkeypatch.setattr(cbd, "_invoke_bedrock", broken)
    text = cbd.generate_migration_failure_summary("❌ x", "(no logs)")
    assert "Failed to generate the migration failure analysis" in text
    assert "throttled" in text


def test_the_evidence_is_the_newest_logs_first(tmp_path):
    import os

    logs = tmp_path / "logs"
    logs.mkdir()
    for i, name in enumerate(["a.log", "b.log", "c.log", "d.log"]):
        (logs / name).write_text(name)
        os.utime(logs / name, (1000 + i, 1000 + i))
    evidence = cbd.migration_failure_evidence(str(tmp_path))
    shown = [n for n in ("d.log", "c.log", "b.log", "a.log") if f"--- {n}" in evidence]
    assert shown == ["d.log", "c.log", "b.log"][: cbd.TERRAFORM_MIGRATION_ANALYSIS_LOGS]
    assert evidence.index("--- d.log") < evidence.index("--- c.log")


def test_a_publish_failure_keeps_the_publish_analysis():
    """On a migration run main() returns before the ordinary summary, so the
    publish analysis it already generated has to be put into the run's own."""
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text()
    branch = source.split("# A weekly Terraform-migration run ends here", 1)[1]
    branch = branch.split("os._exit(", 1)[0]
    assert "if ai_summary:" in branch
    assert 'migration_summary += f"\\n\\n{ai_summary}"' in branch


def test_the_build_passes_the_boundary_it_created(monkeypatch, tmp_path):
    """The boundary the driver attaches is the one create_iam_resources made for
    this run: the role's condition compares the ARN, so any other is refused."""
    cmd = _run_migration_with_fakes(monkeypatch, tmp_path)["cmd"]
    boundary = cmd[cmd.index("--upstream-permissions-boundary-arn") + 1]
    assert boundary == "arn:aws:iam::1:policy/idp-1111-235959-B"


def test_the_build_records_the_run_before_the_driver_and_passes_the_mirror(
    monkeypatch, tmp_path
):
    """run.json is what lets a later build tear a dead run down, so it is in the
    bucket before the driver creates anything, and the driver keeps its states
    under the same prefix."""
    seen = _run_migration_with_fakes(monkeypatch, tmp_path)
    prefix = f"{cbd.TERRAFORM_MIGRATION_STATE_PREFIX}idp-1111-235959/"
    kinds = [e[0] for e in seen["events"]]
    assert kinds == ["put", "driver"]
    _, key, record = seen["events"][0]
    assert key == prefix + cbd.TERRAFORM_RUN_RECORD
    assert record["new_name"] == "idp-1111-235959"
    assert record["old_name"] == cbd.terraform_migration_names("idp-1111-235959")[0]
    assert record["role_arn"] == "arn:role"
    assert record["boundary_arn"] == "arn:aws:iam::1:policy/idp-1111-235959-B"
    assert record["upstream_iam_path"] == "/idp-1111-235959-upstream/"
    assert record["account"] == "1"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--state-mirror") + 1] == f"s3://ci-bucket/{prefix}"


# -- .gitlab-ci.yml -------------------------------------------------------------------


def _assignment(config, job, name) -> str:
    for step in config[job].get("after_script", []) + config[job]["script"]:
        match = re.search(r"<<'PY'\n(.*?)\n\s*PY\s*$", step, re.S | re.M)
        if not match:
            continue
        for node in ast.walk(ast.parse(textwrap.dedent(match.group(1)))):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return ast.unparse(node)
    raise AssertionError(f"no {name} assignment in {job}")


def test_both_slack_posts_label_the_run_the_same_way(config):
    posts = {
        job: _assignment(config, job, "migration_run")
        for job in ("integration_tests", "nightly_notification")
    }
    assert len(set(posts.values())) == 1, posts
    (assignment,) = set(posts.values())
    assert f"os.environ.get('{VAR}', '')" in assignment
    assert "os.environ.get('CI_PIPELINE_SOURCE', '') == 'schedule'" in assignment


@pytest.mark.parametrize("job", ["integration_tests", "nightly_notification"])
def test_the_label_goes_on_status(config, job):
    body = "\n".join(
        line
        for block in config[job].get("after_script", []) + config[job]["script"]
        for line in str(block).splitlines()
    )
    line = next(line for line in body.splitlines() if LABEL in line)
    assert line.strip().startswith("status =")


def test_the_nightly_story_jobs_skip_the_migration_schedule(config):
    rules = config["ai_ux_test_stories_nightly"]["rules"]
    never = [r.get("if", "") for r in rules if r.get("when") == "never"]
    assert any(VAR in rule and "schedule" in rule for rule in never)
    index = next(i for i, r in enumerate(rules) if VAR in r.get("if", ""))
    always = next(i for i, r in enumerate(rules) if r.get("when") == "always")
    assert index < always


def test_the_migration_job_is_no_shared_gate():
    text = (REPO_ROOT / "scripts" / "tests" / "test_ci_gate_parity.py").read_text()
    shared = text.split("SHARED_GATES", 1)[1].split("]", 1)[0]
    assert (
        "terraform_migration" not in shared
        and "stacktest-terraform-migration" not in shared
    )


# -- the reaper for a dead migration run -----------------------------------------------

DEAD = "idp-1111-235959"
MPREFIX = f"{cbd.TERRAFORM_MIGRATION_STATE_PREFIX}{DEAD}/"
RECORD = {
    "old_name": "idpo11112359",
    "new_name": DEAD,
    "region": "us-east-1",
    "role_arn": "arn:aws:iam::1:role/idp-1111-235959-iam-TFDeployRole",
    "boundary_arn": "arn:aws:iam::1:policy/idp-1111-235959-PermissionsBoundary",
    "upstream_iam_path": "/idp-1111-235959-upstream/",
    "module_url": "https://m.zip",
    "account": "1",
}


class _S3Listing:
    """A fake S3 client; `pages` is what list_objects_v2 answers, Prefix ignored
    on purpose, so a listing that trusts the API's prefix filter is caught."""

    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, api):
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                yield {"Contents": objects}

        return _P()

    def get_object(self, Bucket, Key):  # noqa: N803
        body = json.dumps(RECORD).encode()

        class _B:
            def read(self):
                return body

        return {"Body": _B()}


def test_the_nightly_listing_never_reads_a_migration_run():
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc)
    client = _S3Listing(
        [
            {
                "Key": f"{cbd.TERRAFORM_STATE_PREFIX}idp-0101-000000/main.tf",
                "LastModified": now,
            },
            {"Key": MPREFIX + cbd.TERRAFORM_RUN_RECORD, "LastModified": now},
        ]
    )
    assert set(cbd._terraform_state_listing("b", s3=client)) == {"idp-0101-000000"}
    assert set(
        cbd._terraform_state_listing(
            "b", s3=client, prefix=cbd.TERRAFORM_MIGRATION_STATE_PREFIX
        )
    ) == {DEAD}


def test_the_reaper_runs_the_drivers_teardown_with_the_recorded_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(cbd, "ensure_terraform_binary", lambda: "/bin/terraform")
    monkeypatch.setattr(
        cbd, "cleanup_iam_resources", lambda name: seen.setdefault("iam", name) and []
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")

    def fake_run(cmd, env, timeout, check):
        seen.update(cmd=cmd, env=env, timeout=timeout)
        seen["config"] = open(env["AWS_CONFIG_FILE"]).read()

        class _P:
            returncode = 0

        return _P()

    monkeypatch.setattr(cbd.subprocess, "run", fake_run)
    outcome = cbd._reap_terraform_migration(
        DEAD, "ci-bucket", [MPREFIX + cbd.TERRAFORM_RUN_RECORD], s3=_S3Listing([])
    )
    assert outcome == {"findings": [], "timed_out": False}
    cmd = seen["cmd"]

    def arg(flag):
        return cmd[cmd.index(flag) + 1]

    assert cmd[-2:] == ["--resume", "--teardown-only"]
    assert (arg("--old-name"), arg("--new-name")) == ("idpo11112359", DEAD)
    assert arg("--upstream-iam-path") == RECORD["upstream_iam_path"]
    assert arg("--upstream-permissions-boundary-arn") == RECORD["boundary_arn"]
    assert arg("--expected-account") == "1"
    assert arg("--state-mirror") == f"s3://ci-bucket/{MPREFIX}"
    assert f"role_arn = {RECORD['role_arn']}" in seen["config"]
    assert seen["env"]["AWS_PROFILE"] == cbd.TERRAFORM_MIGRATION_PROFILE
    assert "AWS_ACCESS_KEY_ID" not in seen["env"]
    assert seen["timeout"] == cbd.TERRAFORM_MIGRATION_REAP_TIMEOUT_SECONDS
    assert seen["iam"] == DEAD


def test_a_failed_teardown_is_a_finding_and_leaves_iam(monkeypatch):
    cleaned = []
    monkeypatch.setattr(cbd, "ensure_terraform_binary", lambda: "/bin/terraform")
    monkeypatch.setattr(cbd, "cleanup_iam_resources", lambda n: cleaned.append(n) or [])

    class _P:
        returncode = 1

    monkeypatch.setattr(cbd.subprocess, "run", lambda *a, **k: _P())
    outcome = cbd._reap_terraform_migration(
        DEAD, "b", [MPREFIX + cbd.TERRAFORM_RUN_RECORD], s3=_S3Listing([])
    )
    assert outcome["findings"] and not outcome["timed_out"]
    assert cleaned == []  # the role the next attempt runs as stays


def test_a_run_with_no_record_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(
        cbd.subprocess, "run", lambda *a, **k: pytest.fail("ran the driver")
    )
    outcome = cbd._reap_terraform_migration(
        DEAD, "b", [MPREFIX + "context.json"], s3=_S3Listing([])
    )
    assert "run.json" in outcome["findings"][0]


def _sweep(monkeypatch, age_seconds, reap_outcome=None, prior=0):
    from datetime import datetime, timedelta, timezone

    modified = datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds)
    monkeypatch.setattr(cbd, "_terraform_state_bucket", lambda: "b")
    monkeypatch.setattr(
        cbd.boto3,
        "client",
        lambda *a, **k: _S3Listing(
            [{"Key": MPREFIX + cbd.TERRAFORM_RUN_RECORD, "LastModified": modified}]
        ),
    )
    reaped, recorded = [], []
    monkeypatch.setattr(
        cbd,
        "_reap_terraform_migration",
        lambda name, bucket, keys: reaped.append(name) or reap_outcome,
    )
    monkeypatch.setattr(cbd, "_reap_attempts", lambda *a: prior)
    monkeypatch.setattr(
        cbd, "_record_reap_failure", lambda c, b, p, n, f: recorded.append((p, n))
    )
    pending, abandoned = set(), []
    findings = cbd.cleanup_stale_terraform_migrations(pending, abandoned)
    return findings, pending, abandoned, reaped, recorded


def test_a_young_migration_run_is_kept_and_not_reaped(monkeypatch):
    findings, pending, _, reaped, _ = _sweep(monkeypatch, 60)
    assert (findings, pending, reaped) == ([], {DEAD}, [])


def test_a_dead_migration_run_is_reaped_and_released(monkeypatch):
    findings, pending, _, reaped, _ = _sweep(
        monkeypatch,
        cbd.IDP_STACK_STALE_AGE_SECONDS + 60,
        {"findings": [], "timed_out": False},
    )
    assert (findings, pending, reaped) == ([], set(), [DEAD])


def test_a_failed_reap_is_counted_and_keeps_the_run(monkeypatch):
    findings, pending, abandoned, _, recorded = _sweep(
        monkeypatch,
        cbd.IDP_STACK_STALE_AGE_SECONDS + 60,
        {"findings": [f"{DEAD}: broke"], "timed_out": False},
    )
    assert findings and pending == {DEAD} and abandoned == []
    assert recorded == [(MPREFIX, 1)]


def test_a_run_past_its_attempts_is_abandoned_to_a_note(monkeypatch):
    findings, pending, abandoned, reaped, _ = _sweep(
        monkeypatch,
        cbd.IDP_STACK_STALE_AGE_SECONDS + 60,
        prior=cbd.TERRAFORM_REAP_MAX_ATTEMPTS,
    )
    assert findings == [] and reaped == [] and pending == {DEAD}
    (note,) = abandoned
    assert MPREFIX in note and "--teardown-only" in note


def test_the_age_gate_outlasts_the_drivers_longest_phase():
    """The driver mirrors its context at the end of every phase; its longest phase
    is a DynamoDB time-to-live wait of about an hour. A gate inside that would
    reap a run still in flight."""
    assert cbd.IDP_STACK_STALE_AGE_SECONDS >= 2 * 3600


def test_main_hands_the_migration_runs_to_the_stack_sweep(monkeypatch):
    monkeypatch.setattr(cbd, "install_suite_transcript", lambda: None)
    monkeypatch.setattr(cbd, "read_ci_run_options", lambda *a: {})
    seen = {}
    for sweep in cbd.STARTUP_SWEEPS:
        monkeypatch.setattr(cbd, sweep.func, lambda *a, **k: [])

    def migrations(pending, abandoned):
        pending.add(DEAD)
        return []

    monkeypatch.setattr(cbd, "cleanup_stale_terraform_migrations", migrations)
    monkeypatch.setattr(
        cbd, "cleanup_stale_idp_stacks", lambda **k: seen.update(k) or []
    )

    class _Stop(BaseException):
        pass

    def _stop(*_a, **_k):
        raise _Stop()

    monkeypatch.setattr(cbd, "publish_artifacts", _stop)
    with pytest.raises(_Stop):
        cbd.main()
    assert seen == {"terraform_runs": {DEAD}}


def test_the_job_finds_the_summary_of_a_migration_run():
    """The GitLab job cannot read deploy/summaries/, so it reads the summary from
    the log, from the marker on. A migration run returns before the ordinary
    summary prints it, so it prints the marker itself."""
    ci = CI_CONFIG.read_text(encoding="utf-8")
    assert f"sed -n '/{cbd.DEPLOYMENT_SUMMARY_LOG_MARKER}/,$p'" in ci
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text()
    branch = source.split("# A weekly Terraform-migration run ends here", 1)[1]
    branch = branch.split("os._exit(", 1)[0]
    assert branch.index("print(DEPLOYMENT_SUMMARY_LOG_MARKER)") < branch.index(
        "print(text)"
    )


def _additions(template):
    role = template["Resources"]["TerraformDeployRole"]["Properties"]
    (doc,) = [
        p["PolicyDocument"]
        for p in role["Policies"]
        if p["PolicyName"] == "TerraformDeployAdditions"
    ]
    return doc["Statement"]


def test_only_a_migration_runs_role_can_read_the_role_quota():
    """The driver's preflight reads the account's role count against its quota;
    a nightly's deploy role does not need that and does not get it."""
    text = SERVICE_ROLE_TEMPLATE.read_text()
    nightly = _additions(cbd.terraform_deploy_role_template(text))
    migration = _additions(
        cbd.terraform_deploy_role_template(text, terraform_migration=True)
    )
    quota = [s for s in migration if "iam:GetAccountSummary" in s.get("Action", [])]
    assert quota == list(cbd.TERRAFORM_MIGRATION_ROLE_ADDITIONS)
    assert not [s for s in nightly if "iam:GetAccountSummary" in s.get("Action", [])]
    # the read is the whole addition: nothing else is granted beside it
    assert [len(s) for s in migration if s not in nightly] == [4]
    (added,) = [s for s in migration if s not in nightly]
    assert added["Action"] == ["iam:GetAccountSummary"] and added["Effect"] == "Allow"


def test_the_migration_run_builds_its_role_with_the_quota_read(monkeypatch, tmp_path):
    seen = {}
    real = cbd.create_iam_resources

    def record(name, **kwargs):
        seen.update(kwargs)
        return ("arn:role", f"arn:aws:iam::1:policy/{name}-B")

    monkeypatch.setattr(cbd, "create_iam_resources", record)
    monkeypatch.setattr(cbd, "_invoke_bedrock", lambda prompt: "")
    assert real is not record
    monkeypatch.setattr(cbd, "ci_region", lambda: "us-east-1")
    monkeypatch.setattr(cbd, "terraform_work_directory", lambda n: str(tmp_path / n))
    monkeypatch.setattr(cbd, "_caller_principal_arn", lambda: "arn:cb")
    monkeypatch.setattr(cbd, "_wait_until_assumable", lambda arn: False)
    monkeypatch.setattr(cbd, "cleanup_iam_resources", lambda name: [])
    cbd.run_terraform_migration("idp-1111-235959", "https://m.zip", "terraform")
    assert seen.get("terraform_migration") is True
