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
def test_the_derived_names_satisfy_the_role_and_the_driver(stack):
    old, new = cbd.terraform_migration_names(stack)
    assert old.startswith(cbd.MANAGED_STACK_NAME_PREFIX)
    assert new.startswith(f"{cbd.MANAGED_STACK_NAME_PREFIX}-")
    assert tmt.validate_names(old, new) == []


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
