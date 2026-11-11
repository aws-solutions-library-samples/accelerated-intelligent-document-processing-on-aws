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
    )
    assert cmd[cmd.index("--terraform-assume-role-arn") + 1] == "arn:r"
    assert cmd[cmd.index("--expected-account") + 1] == "123456789012"
    assert "scripts/sdlc/terraform_migration_test.py" in cmd


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
