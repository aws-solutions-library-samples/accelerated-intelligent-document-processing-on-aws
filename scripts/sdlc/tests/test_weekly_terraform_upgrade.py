"""The weekly Terraform-upgrade schedule (``IDP_RUN_TERRAFORM_UPGRADE=true``).

Pinned here, offline:

* the variable counts on a scheduled pipeline only, and is refused beside every
  other schedule selector, because the run replaces the primary suite;
* the choice reaches the build in ``ci_run_options.json`` and is read back there;
* the run is labelled in the summary, the failure email and both Slack posts, and
  leaves the UX review stack and the nightly story jobs alone;
* FROM is the newest published release, read from the release bucket's latest
  pointer once and then taken from that version's own key, held to the pointer's
  checksum;
* the driver runs with both module URLs, the run's role and boundary, and the
  build's time budget, and the run's IAM is cleaned up whatever happens;
* the driver records what the upgrade plan deletes or replaces and fails a
  re-plan after the upgrade that is not empty.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import codebuild_deployment as cbd
import pytest
import terraform_upgrade_test as tut
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_CONFIG = REPO_ROOT / ".gitlab-ci.yml"
LABEL = "WEEKLY TERRAFORM UPGRADE RUN"
VAR = "IDP_RUN_TERRAFORM_UPGRADE"
POINTER_URL = (
    "https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/"
    "terraform/idp-terraform-latest.json"
)
BASE = "https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp"


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
        itd.terraform_upgrade_requested({VAR: "true", "CI_PIPELINE_SOURCE": source})
        is False
    )


def test_a_schedule_carrying_it_requests_the_run(itd):
    assert itd.terraform_upgrade_requested(
        {VAR: " TRUE ", "CI_PIPELINE_SOURCE": "schedule"}
    )
    assert not itd.terraform_upgrade_requested(
        {VAR: "false", "CI_PIPELINE_SOURCE": "schedule"}
    )


@pytest.mark.parametrize(
    "clash",
    [
        {"IDP_RUN_PROBES": "true"},
        {"IDP_TEST_REGION": "eu-central-1"},
        {"IDP_IAC": "terraform"},
        {"IDP_RUN_TERRAFORM_MIGRATION": "true"},
    ],
)
def test_it_is_refused_beside_another_selector(itd, clash):
    env = {VAR: "true", "CI_PIPELINE_SOURCE": "schedule", **clash}
    with pytest.raises(ValueError, match="cannot be combined"):
        itd.terraform_upgrade_requested(env)


def test_the_choice_travels_in_the_run_options(itd, tmp_path):
    options = itd.write_ci_run_options(
        str(tmp_path / "o.json"),
        environ={VAR: "true", "CI_PIPELINE_SOURCE": "schedule"},
    )
    assert options["terraform_upgrade"] is True
    assert cbd.terraform_upgrade_mode(environ={}, options=options) is True
    assert (
        cbd.terraform_upgrade_mode(environ={}, options={"terraform_upgrade": False})
        is False
    )
    # And a plain schedule is not one.
    plain = itd.write_ci_run_options(
        str(tmp_path / "p.json"), environ={"CI_PIPELINE_SOURCE": "schedule"}
    )
    assert plain["terraform_upgrade"] is False


def test_main_watches_the_upgrade_run_as_long_as_the_build_can_run(
    itd, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    for name in (
        "IDP_RUN_PROBES",
        "IDP_TEST_REGION",
        "IDP_IAC",
        "IDP_RUN_TERRAFORM_MIGRATION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "schedule")
    monkeypatch.setenv(VAR, "true")
    seen: list[dict] = []
    monkeypatch.setattr(itd, "create_deployment_package", lambda *a, **k: None)
    monkeypatch.setattr(itd, "upload_to_s3", lambda *a, **k: "v1")
    monkeypatch.setattr(itd, "monitor_pipeline", lambda *a, **k: seen.append(k) or True)
    with pytest.raises(SystemExit):
        itd.main()
    assert seen == [
        {
            "max_wait": itd.UPGRADE_RUN_MONITOR_MAX_WAIT_SECONDS,
            "handoff_after": itd.UPGRADE_RUN_MONITOR_HANDOFF_SECONDS,
        }
    ]
    written = json.loads((tmp_path / itd.CI_RUN_OPTIONS_FILE).read_text())
    assert written["terraform_upgrade"] is True


def test_main_stops_when_the_run_options_cannot_be_written(itd, monkeypatch, tmp_path):
    """The file is the only way the choice reaches CodeBuild, which reads no file
    as the CloudFormation nightly; going on would run that under a run the chat
    posts label as the upgrade run."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "IDP_RUN_PROBES",
        "IDP_TEST_REGION",
        "IDP_IAC",
        "IDP_RUN_TERRAFORM_MIGRATION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "schedule")
    monkeypatch.setenv(VAR, "true")
    started = []
    monkeypatch.setattr(itd, "write_ci_run_options", lambda *a, **k: None)
    monkeypatch.setattr(itd, "create_deployment_package", lambda *a, **k: None)
    monkeypatch.setattr(itd, "upload_to_s3", lambda *a, **k: started.append(1))
    monkeypatch.setattr(itd, "monitor_pipeline", lambda *a, **k: started.append(2))
    with pytest.raises(SystemExit) as raised:
        itd.main()
    assert raised.value.code == 1
    assert started == []


def test_the_monitor_and_the_job_outlast_the_build(itd, config):
    build = cbd.TERRAFORM_UPGRADE_MIN_TIMEOUT_MINUTES * 60
    handoff = itd.UPGRADE_RUN_MONITOR_HANDOFF_SECONDS
    max_wait = itd.UPGRADE_RUN_MONITOR_MAX_WAIT_SECONDS
    assert handoff - build >= 5 * 60
    assert max_wait > handoff
    hours, minutes = re.fullmatch(
        r"(\d+)h(\d+)m", config["integration_tests"]["timeout"]
    ).groups()
    assert (int(hours) * 60 + int(minutes)) * 60 - max_wait >= 10 * 60


def test_the_template_timeout_meets_what_an_upgrade_run_needs():
    template = REPO_ROOT / "scripts" / "sdlc" / "cfn" / "codepipeline-s3.yml"
    (minutes,) = re.findall(
        r"^\s*TimeoutInMinutes:\s*(\d+)\s*$", template.read_text(), re.MULTILINE
    )
    assert int(minutes) >= cbd.TERRAFORM_UPGRADE_MIN_TIMEOUT_MINUTES


def test_both_slack_posts_name_the_upgrade_run_and_not_the_nightly():
    """Each post labels the run from the variable and keeps it out of the
    CloudFormation-nightly label, as each does for the migration run."""
    text = CI_CONFIG.read_text(encoding="utf-8")
    assert text.count('"⏫ WEEKLY TERRAFORM UPGRADE RUN · "') == 2
    assert text.count("migration_run or upgrade_run)") == 2
    assert text.count("migration_run)") == 0


def test_the_nightly_story_jobs_never_run_on_the_upgrade_schedule(config):
    rule = {
        "if": '$CI_PIPELINE_SOURCE == "schedule" && '
        "$IDP_RUN_TERRAFORM_UPGRADE =~ /^\\s*true\\s*$/i",
        "when": "never",
    }
    for name in ("ai_ux_test_stories_nightly", "ai_ux_test_stories_digest"):
        rules = config[name]["rules"]
        assert rule in rules, name
        # Before the rule that runs every develop schedule, or it never applies.
        always = next(
            i
            for i, r in enumerate(rules)
            if r.get("if")
            == '$CI_PIPELINE_SOURCE == "schedule" && $CI_COMMIT_BRANCH == "develop"'
        )
        assert rules.index(rule) < always, name


# -- the build side ------------------------------------------------------------------


@pytest.mark.parametrize(
    "env, word",
    [
        ({"IDP_IAC": "terraform"}, "IDP_IAC"),
        ({"IDP_RUN_TERRAFORM_MIGRATION": "true"}, "IDP_RUN_TERRAFORM_MIGRATION"),
        ({"IDP_RUN_PROBES": "true"}, "IDP_RUN_PROBES"),
        (
            {"IDP_TEST_REGION": "eu-central-1", "AWS_REGION": "eu-central-1"},
            "eu-central-1",
        ),
    ],
)
def test_the_build_refuses_what_the_run_cannot_combine_with(env, word):
    assert cbd.terraform_upgrade_unsupported_reason({}) is None
    reason = cbd.terraform_upgrade_unsupported_reason(env)
    assert reason is not None and word in reason


def test_the_summary_and_subject_are_labelled():
    env = {VAR: "true"}
    assert cbd.label_ci_run("body", env).startswith(f"⏫ {LABEL}")
    assert cbd.label_ci_run("body", {}) == "body"
    assert LABEL.lower() in cbd.failure_subject("idp-x", env)
    assert cbd.failure_subject("idp-x", {}) == "IDP CI failure: idp-x"


def test_an_upgrade_run_leaves_the_review_stack_alone(monkeypatch):
    options = {"pipeline_source": "schedule", "ref": "develop"}
    assert cbd.ux_nightly_update_requested(options, environ={}) is True
    assert (
        cbd.ux_nightly_update_requested({**options, "terraform_upgrade": True}, {})
        is False
    )
    assert cbd.ux_nightly_update_requested(options, environ={VAR: "true"}) is False


def test_a_build_too_short_for_an_upgrade_run_is_refused(monkeypatch):
    class _CodeBuild:
        def __init__(self, minutes):
            self.minutes = minutes

        def batch_get_builds(self, ids):
            return {"builds": [{"timeoutInMinutes": self.minutes}]}

    monkeypatch.setenv("CODEBUILD_BUILD_ID", "b:1")
    short = cbd.TERRAFORM_UPGRADE_MIN_TIMEOUT_MINUTES - 1
    reason = cbd.terraform_upgrade_timeout_shortfall(_CodeBuild(short))
    assert reason is not None and "codepipeline" in reason
    assert (
        cbd.terraform_upgrade_timeout_shortfall(
            _CodeBuild(cbd.TERRAFORM_UPGRADE_MIN_TIMEOUT_MINUTES)
        )
        is None
    )


def test_main_refuses_before_publish_and_dispatches_the_upgrade_driver():
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text()
    main = source.split("\ndef main():", 1)[1]
    check = main.index("terraform_upgrade_timeout_shortfall()")
    assert check < main.index("publish_success = True")
    assert main.index("terraform_upgrade_unsupported_reason()") < check
    assert "IAC_TERRAFORM if migration or upgrade else iac" in main
    branch = main.split("# A weekly Terraform-migration run ends here", 1)[1]
    branch = branch.split("os._exit(", 1)[0]
    assert "if migration or upgrade:" in branch
    assert "ok, migration_summary = run_terraform_upgrade(" in branch
    assert branch.index("print(DEPLOYMENT_SUMMARY_LOG_MARKER)") < branch.index(
        "print(text)"
    )


# -- FROM: the newest published release ------------------------------------------------


def _release(version="0.6.15", body=b"module zip bytes", **pointer_overrides):
    module_url = f"{BASE}/{version}/terraform/idp-terraform-{version}.zip"
    pointer = {
        "version": version,
        "moduleUrl": module_url,
        "sha256": hashlib.sha256(body).hexdigest(),
        **pointer_overrides,
    }
    objects = {POINTER_URL: json.dumps(pointer).encode(), module_url: body}
    reads: list[str] = []

    def fetch(url):
        reads.append(url)
        return objects[url]

    return fetch, reads, module_url


def test_from_is_the_pointers_version_at_its_own_key():
    fetch, reads, module_url = _release()
    source = cbd.resolve_terraform_upgrade_from("us-east-1", fetch=fetch)
    assert source["module_url"] == module_url
    assert source["version"] == "0.6.15"
    assert source["pointer_url"] == POINTER_URL
    # The pointer is read once; the module is read from the version's own key.
    assert reads == [POINTER_URL, module_url]


def test_the_pointer_is_the_one_idp_cli_reads_by_default():
    from idp_cli.cli import TEMPLATE_URLS
    from idp_sdk import default_terraform_pointer_url

    assert default_terraform_pointer_url(TEMPLATE_URLS["us-east-1"]) == POINTER_URL


def test_module_bytes_that_do_not_match_the_pointer_are_refused():
    fetch, _, module_url = _release(sha256="0" * 64)
    with pytest.raises(ValueError, match="has sha256"):
        cbd.resolve_terraform_upgrade_from("us-east-1", fetch=fetch)


def test_a_pointer_naming_another_key_is_refused_not_followed():
    """The mutable latest object, or any key but the version's own, is not what
    the run deploys."""
    fetch, reads, _ = _release(moduleUrl=f"{BASE}/idp-terraform-latest.zip")
    with pytest.raises(ValueError, match="not the release's own key"):
        cbd.resolve_terraform_upgrade_from("us-east-1", fetch=fetch)
    assert reads == [POINTER_URL]


@pytest.mark.parametrize(
    "overrides",
    [{"version": "latest"}, {"version": None}, {"sha256": "abc"}, {"sha256": None}],
)
def test_a_pointer_without_a_release_version_and_checksum_is_refused(overrides):
    fetch, _, _ = _release(**overrides)
    with pytest.raises(ValueError, match="names no"):
        cbd.resolve_terraform_upgrade_from("us-east-1", fetch=fetch)


def test_a_region_with_no_release_bucket_is_refused():
    with pytest.raises(ValueError, match="no published release bucket"):
        cbd.resolve_terraform_upgrade_from("ap-south-2", fetch=lambda url: b"")


def test_only_https_is_read():
    with pytest.raises(ValueError, match="non-HTTPS"):
        cbd._fetch_public_https("http://example.com/x")


# -- the run -----------------------------------------------------------------------


def _run_upgrade_with_fakes(monkeypatch, tmp_path, returncode=0, resolve=None):
    seen: dict = {"cleanup": [], "prompts": []}
    monkeypatch.setattr(
        cbd, "_invoke_bedrock", lambda prompt: seen["prompts"].append(prompt) or "• a"
    )
    monkeypatch.setattr(cbd, "ci_region", lambda: "us-east-1")
    monkeypatch.setattr(cbd, "terraform_work_directory", lambda n: str(tmp_path / n))
    monkeypatch.setattr(cbd, "_caller_principal_arn", lambda: "arn:cb")
    monkeypatch.setattr(
        cbd,
        "resolve_terraform_upgrade_from",
        resolve
        or (
            lambda region: {
                "module_url": f"{BASE}/0.6.15/terraform/idp-terraform-0.6.15.zip",
                "version": "0.6.15",
                "sha256": "a" * 64,
                "pointer_url": POINTER_URL,
            }
        ),
    )
    monkeypatch.setattr(
        cbd,
        "create_iam_resources",
        lambda name, **k: (
            seen.setdefault("iam", (name, k))
            and ("arn:role", f"arn:aws:iam::1:policy/{name}-B")
        ),
    )
    monkeypatch.setattr(cbd, "_wait_until_assumable", lambda arn: True)
    monkeypatch.setattr(
        cbd, "cleanup_iam_resources", lambda name: seen["cleanup"].append(name) or []
    )
    monkeypatch.setattr(cbd, "build_timeout_minutes", lambda *a: 240)

    class _Sts:
        def get_caller_identity(self):
            return {"Account": "123456789012"}

    monkeypatch.setattr(cbd.boto3, "client", lambda service, **k: _Sts())

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        work = Path(cmd[cmd.index("--work-dir") + 1])
        (work / "summary.txt").write_text("Terraform upgrade test: PASS\n")

        class _P:
            pass

        _P.returncode = returncode
        return _P()

    monkeypatch.setattr(cbd.subprocess, "run", fake_run)
    seen["result"] = cbd.run_terraform_upgrade(
        "idp-1111-235959", "https://ci/0.6.16/terraform/idp-terraform-0.6.16.zip", "tf"
    )
    return seen


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_the_driver_upgrades_from_the_release_to_this_build(monkeypatch, tmp_path):
    seen = _run_upgrade_with_fakes(monkeypatch, tmp_path)
    ok, summary = seen["result"]
    assert ok
    cmd = seen["cmd"]
    assert cmd[1] == "scripts/sdlc/terraform_upgrade_test.py"
    assert _arg(cmd, "--from-module-url").endswith(
        "/0.6.15/terraform/idp-terraform-0.6.15.zip"
    )
    assert _arg(cmd, "--to-module-url").endswith(
        "/0.6.16/terraform/idp-terraform-0.6.16.zip"
    )
    assert _arg(cmd, "--stack-name") == "idp-1111-235959"
    assert _arg(cmd, "--terraform-assume-role-arn") == "arn:role"
    assert _arg(cmd, "--parameters") == (
        "PermissionsBoundaryArn=arn:aws:iam::1:policy/idp-1111-235959-B"
    )
    assert _arg(cmd, "--expected-account") == "123456789012"
    assert _arg(cmd, "--admin-email") == cbd.SUPPRESS_INVITE_ADMIN_EMAIL
    # The run's role is the Terraform deploy role, not the migration's variant.
    assert seen["iam"] == ("idp-1111-235959", {"terraform_deployer_arn": "arn:cb"})
    assert seen["cleanup"] == ["idp-1111-235959"]
    # The deadline leaves the teardown reserve inside the build's limit.
    deadline = int(_arg(cmd, "--deadline-seconds"))
    assert seen["timeout"] - deadline == cbd.TERRAFORM_UPGRADE_RESERVE_SECONDS - 300
    assert "FROM: release 0.6.15" in summary
    assert "Terraform upgrade test: PASS" in summary
    assert "wrote no summary" not in summary
    # The driver's argv parses with the driver's own parser.
    argv = list(cmd[2:])
    argv[argv.index("--terraform-bin") + 1] = "/bin/true"
    settings = tut.parse_args(argv)
    assert settings.from_module_url.endswith("idp-terraform-0.6.15.zip")


def test_a_failed_driver_fails_the_run_with_an_upgrade_analysis(monkeypatch, tmp_path):
    seen = _run_upgrade_with_fakes(monkeypatch, tmp_path, returncode=1)
    ok, summary = seen["result"]
    assert not ok
    assert summary.startswith("🤖 AI FAILURE ANALYSIS")
    assert "Terraform-upgrade" in seen["prompts"][0]
    assert "genai-idp-terraform" not in seen["prompts"][0]
    assert seen["cleanup"] == ["idp-1111-235959"]


def test_an_unresolvable_from_fails_before_any_iam_is_made(monkeypatch, tmp_path):
    def refuse(region):
        raise ValueError("pointer names no release version")

    seen = _run_upgrade_with_fakes(monkeypatch, tmp_path, resolve=refuse)
    ok, summary = seen["result"]
    assert not ok
    assert "pointer names no release version" in summary
    assert "iam" not in seen and "cmd" not in seen
    assert seen["cleanup"] == []


# -- the driver's record of the upgrade -------------------------------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Real ``terraform plan`` / ``tofu plan`` output, coloured as ``idp-cli deploy
#: --terraform`` leaves it in the log (it plans without ``-no-color``), captured
#: from Terraform v1.17.2 and OpenTofu v1.13.9 on a root whose ``module "idp"``
#: replaces one resource, destroys one and updates one.
REAL_PLANS = {
    "terraform": FIXTURES / "plan_terraform_colour.txt",
    "opentofu": FIXTURES / "plan_opentofu_colour.txt",
}


@pytest.mark.parametrize("tool", sorted(REAL_PLANS))
def test_the_plans_destroy_and_replace_set_is_read_from_real_output(tool):
    text = REAL_PLANS[tool].read_text()
    assert "\x1b[" in text, "the fixture must keep the colour codes the log has"
    assert tut.destructive_changes(text) == {
        "destroy": ["module.idp.terraform_data.b[0]"],
        "replace": ["module.idp.terraform_data.a"],
    }
    assert tut.plan_summary(text) == {"add": 1, "change": 1, "destroy": 2}


@pytest.mark.parametrize("tool", sorted(REAL_PLANS))
def test_only_the_last_plan_in_the_log_is_read(tool):
    text = REAL_PLANS[tool].read_text()
    earlier = text.replace("terraform_data.b[0]", "terraform_data.earlier")
    found = tut.destructive_changes(earlier + "\n...\n" + text)
    assert found["destroy"] == ["module.idp.terraform_data.b[0]"]


def test_a_tainted_or_deposed_object_is_recorded():
    text = (
        "Terraform will perform the following actions:\n"
        "\x1b[1m  # module.idp.terraform_data.c\x1b[0m is tainted, so must be "
        "\x1b[1m\x1b[31mreplaced\x1b[0m\n"
        "\x1b[1m  # module.idp.terraform_data.d\x1b[0m (deposed object 1a2b3c4d) "
        "will be \x1b[1m\x1b[31mdestroyed\x1b[0m\n"
    )
    assert tut.destructive_changes(text) == {
        "destroy": ["module.idp.terraform_data.d"],
        "replace": ["module.idp.terraform_data.c"],
    }


def test_an_as_requested_replacement_is_a_replacement():
    text = (
        "Terraform will perform the following actions:\n"
        "\x1b[1m  # module.idp.time_sleep.wait\x1b[0m will be "
        "\x1b[1m\x1b[31mreplaced\x1b[0m, as requested\n"
    )
    assert tut.destructive_changes(text)["replace"] == ["module.idp.time_sleep.wait"]


class _Upgrade:
    """A driver Run with only what phase_upgrade reads."""

    def __init__(self, tmp_path, code, text):
        self.s = type(
            "S", (), {"to_module_url": "https://x/idp-terraform-0.6.16.zip"}
        )()
        self.dir = tmp_path
        self.work = tmp_path
        self.ctx: dict = {"fromVersion": "0.6.15", "toVersion": "0.6.16"}
        self._result = (code, text)

    def deploy(self, url, log, first):
        return self._result

    def record_names(self):
        pass

    def save(self):
        pass


def test_the_upgrade_phase_records_what_the_plan_removes(tmp_path):
    text = REAL_PLANS["terraform"].read_text()
    run = _Upgrade(tmp_path, 0, text)
    detail = tut.Run.phase_upgrade(run)
    assert run.ctx["upgradeDestructive"] == tut.destructive_changes(text)
    assert run.ctx["upgradeDestructive"]["replace"] == ["module.idp.terraform_data.a"]
    assert "1 to add, 1 to change, 2 to destroy" in detail


def test_a_refused_upgrade_still_records_what_the_plan_removes(tmp_path):
    text = REAL_PLANS["opentofu"].read_text()
    run = _Upgrade(tmp_path, 1, text)
    with pytest.raises(tut.PhaseFailed):
        tut.Run.phase_upgrade(run)
    assert run.ctx["upgradeDestructive"]["destroy"] == [
        "module.idp.terraform_data.b[0]"
    ]


def test_the_summary_names_them_and_the_replan():
    text = tut.summarise(
        {
            "status": "FAIL",
            "stackName": "s",
            "region": "r",
            "upgradeDestructive": tut.destructive_changes(
                REAL_PLANS["terraform"].read_text()
            ),
            "replan": "drift",
        }
    )
    assert "Replaced: module.idp.terraform_data.a" in text
    assert "Destroyed: module.idp.terraform_data.b[0]" in text
    assert "Re-plan after the upgrade: drift" in text


def test_the_replan_runs_between_the_upgrade_and_the_verification():
    assert tut.PHASES.index("upgrade") + 1 == tut.PHASES.index("replan")
    assert tut.PHASES.index("replan") < tut.PHASES.index("verify")
    assert "replan" not in tut.TEARDOWN_PHASES


class _Replan:
    """A driver Run with only what phase_replan reads."""

    def __init__(self, tmp_path):
        self.s = type("S", (), {"terraform_bin": "tf"})()
        self.dir = tmp_path
        self.ctx: dict = {}

    def save(self):
        pass


@pytest.mark.parametrize(
    "verdict, passes",
    [
        ({"status": "converged", "detail": "empty", "changes": []}, True),
        ({"status": "drift", "detail": "not empty: a", "changes": ["a"]}, False),
        ({"status": "error", "detail": "exit 1", "changes": []}, False),
    ],
)
def test_the_replan_uses_the_nightlys_verdict(monkeypatch, tmp_path, verdict, passes):
    seen = {}

    def fake_check(terraform):
        seen["terraform"] = (terraform.binary, terraform.directory)
        return verdict

    monkeypatch.setattr(cbd, "check_terraform_convergence", fake_check)
    run = _Replan(tmp_path)
    if passes:
        assert tut.Run.phase_replan(run) == verdict["detail"]
    else:
        with pytest.raises(tut.PhaseFailed, match="did not converge") as raised:
            tut.Run.phase_replan(run)
        assert verdict["detail"] in str(raised.value)
    assert seen["terraform"] == ("tf", str(tmp_path))
    assert run.ctx["replan"] == verdict["status"]


@pytest.mark.parametrize("done", list(tut.TEARDOWN_PHASES))
def test_a_resumed_run_past_a_teardown_phase_does_not_replan(
    monkeypatch, tmp_path, done
):
    monkeypatch.setattr(
        cbd,
        "check_terraform_convergence",
        lambda terraform: pytest.fail("re-planned a deployment already torn down"),
    )
    run = _Replan(tmp_path)
    run.ctx["completed"] = ["preflight", "deploy-from", "upgrade", done]
    assert tut.Run.phase_replan(run).startswith("not run")
    assert run.ctx["replan"] == "not run"
