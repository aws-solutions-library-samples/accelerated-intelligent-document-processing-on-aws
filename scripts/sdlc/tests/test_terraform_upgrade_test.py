# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for the Terraform in-place upgrade test driver.

`terraform_upgrade_test.py` deploys a real release and upgrades it, so what can be
checked without AWS is what decides whether a green run means anything:

  * the pair of module URLs really is an upgrade (two different versions of one
    variant), because a run given the same URL twice would apply nothing and pass;
  * a replaced bucket, table, user pool or key is reported, and a resource a
    ``moved`` block relocated is not -- that distinction is the whole point of the
    tier, and it is made on physical identity rather than on Terraform address;
  * the upgrade deploy passes neither the stateful-replace override nor the
    parameters of the first deploy, so a refusal fails the run and the remembered
    parameters are actually exercised;
  * a refused plan's addresses reach the failure message rather than only a log;
  * a failed phase still tears down, and a preflight that refused tears nothing
    down, because the sweep deletes by name;
  * the Makefile target refuses to run without the four values it needs.
"""

import json
import subprocess
from pathlib import Path

import pytest
import terraform_migration_test as tmt
import terraform_upgrade_test as tut

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PUBLISHED = "https://s3.us-west-2.amazonaws.com/b/p/0.6.14/terraform"


# -- the module pair ---------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"{PUBLISHED}/idp-terraform-0.6.14.zip", ("0.6.14", None)),
        (f"{PUBLISHED}/idp-terraform-headless-0.6.14.zip", ("0.6.14", "headless")),
        (
            "s3::https://s3-us-west-2.amazonaws.com/b/p/idp-terraform-1.0.zip",
            ("1.0", None),
        ),
        ("./modules/idp-0.6.14", (None, None)),
        ("https://example.com/not-the-module.zip", (None, None)),
    ],
)
def test_module_coordinates_read_the_version_and_variant(url, expected):
    assert tut.module_coordinates(url) == expected


def test_the_same_url_twice_is_not_an_upgrade():
    """A run given one URL twice would apply nothing and report PASS, which is the
    most expensive way for this tier to be useless."""
    url = f"{PUBLISHED}/idp-terraform-0.6.14.zip"
    assert tut.module_pair_problems(url, url) == [
        "the two module URLs are the same, so there is no upgrade"
    ]


def test_two_versions_of_the_standard_module_are_accepted():
    assert (
        tut.module_pair_problems(
            f"{PUBLISHED}/idp-terraform-0.6.14.zip",
            "https://s3.us-west-2.amazonaws.com/b/p/0.6.15/terraform/idp-terraform-0.6.15.zip",
        )
        == []
    )


def test_crossing_variants_is_refused():
    problems = tut.module_pair_problems(
        f"{PUBLISHED}/idp-terraform-0.6.14.zip",
        f"{PUBLISHED}/idp-terraform-headless-0.6.15.zip",
    )
    assert any("different variants" in p for p in problems)


def test_a_variant_pair_is_refused_because_the_seeding_assumes_the_standard_module():
    problems = tut.module_pair_problems(
        f"{PUBLISHED}/idp-terraform-headless-0.6.14.zip",
        f"{PUBLISHED}/idp-terraform-headless-0.6.15.zip",
    )
    assert any("not covered by this test" in p for p in problems)


def test_a_url_that_names_no_version_is_refused():
    problems = tut.module_pair_problems(
        "https://example.com/module.zip", f"{PUBLISHED}/idp-terraform-0.6.15.zip"
    )
    assert any("does not end in" in p for p in problems)


# -- names -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["tfup1", "idp-tf-upgrade", "a1"])
def test_a_usable_stack_name_passes(name):
    assert tut.validate_stack_name(name) == []


@pytest.mark.parametrize("name", ["TFUP", "1tf", "tf_up", "tf-", "", "t" * 26])
def test_an_unusable_stack_name_is_refused(name):
    assert tut.validate_stack_name(name)


def test_a_name_that_differs_from_the_work_directorys_run_is_refused():
    """The teardown deletes by this name, so a mismatch must refuse rather than
    overwrite the recorded one."""
    assert tut.stored_name_problems({"stackName": "tfup1"}, "tfup2")
    assert tut.stored_name_problems({"stackName": "tfup1"}, "tfup1") == []
    assert tut.stored_name_problems({}, "tfup1") == []


def test_one_deployment_owns_one_name_for_the_sweep(tmp_path):
    """The inherited sweep takes the run's names from ``old_name``/``new_name``;
    an upgrade run has one deployment, so both are its stack name."""
    settings = _settings(tmp_path)
    assert (settings.old_name, settings.new_name) == ("tfup1", "tfup1")


# -- the plan and the guard --------------------------------------------------


def test_plan_summary_reads_the_last_plan_line():
    text = (
        "Plan: 1 to add, 0 to change, 0 to destroy.\n"
        "...\n"
        "Plan: 9 to add, 131 to change, 9 to destroy.\n"
    )
    assert tut.plan_summary(text) == {"add": 9, "change": 131, "destroy": 9}


def test_plan_summary_is_none_when_the_log_holds_no_plan():
    assert tut.plan_summary("Apply complete!") is None


def test_a_refused_plans_addresses_reach_the_failure_message():
    text = (
        "✗ Error: The plan would delete or replace 2 resource(s) that hold data:\n"
        "  - module.idp.aws_s3_bucket.evaluation_baseline_bucket[0] (CloudFormation retains it)\n"
        "  - module.idp.module.apiresolverstack.aws_iam_role.api_gateway_cloud_watch_role[0]\n"
        "Nothing was applied.\n"
    )
    assert tut.refused_addresses(text) == [
        "module.idp.aws_s3_bucket.evaluation_baseline_bucket[0]",
        "module.idp.module.apiresolverstack.aws_iam_role.api_gateway_cloud_watch_role[0]",
    ]
    assert tut.refused_addresses("Apply complete!") == []


# -- identity --------------------------------------------------------------


def test_a_replaced_data_holding_resource_is_reported():
    before = {
        "buckets": ["idp-input", "idp-output"],
        "tables": ["idp-tracking"],
        "user_pools": ["us-west-2_aaa"],
        "kms_keys": ["key-1"],
    }
    after = {
        "buckets": ["idp-input", "idp-output-2"],
        "tables": ["idp-tracking"],
        "user_pools": ["us-west-2_bbb"],
        "kms_keys": ["key-1"],
    }
    losses = tut.identity_losses(before, after)
    assert losses == ["bucket idp-output", "user_pool us-west-2_aaa"]


def test_a_resource_a_moved_block_relocated_is_not_a_loss():
    """Its Terraform address changed and its physical identity did not, which is
    what a ``moved`` block is for; matching by address would report it."""
    state_before = {
        "resources": [
            {
                "mode": "managed",
                "module": "module.idp",
                "type": "aws_dynamodb_table",
                "name": "tracking_table",
                "instances": [{"attributes": {"name": "idp-tracking"}}],
            }
        ]
    }
    state_after = {
        "resources": [
            {
                "mode": "managed",
                "module": "module.idp.module.patternstack",
                "type": "aws_dynamodb_table",
                "name": "tracking",
                "instances": [{"attributes": {"name": "idp-tracking"}}],
            }
        ]
    }
    before = tmt.physical_names(state_before)
    after = tmt.physical_names(state_after)
    assert tut.identity_losses(before, after) == []


def test_a_new_resource_and_a_renamed_role_are_not_losses():
    """A release may add a bucket and rename a role; neither loses data, and
    CloudFormation replaces an unnamed role on an update too."""
    before = {"buckets": ["idp-input"], "roles": ["idp-old-role"]}
    after = {"buckets": ["idp-input", "idp-new"], "roles": ["idp-new-role"]}
    assert tut.identity_losses(before, after) == []


def test_table_kinds_come_from_the_deployments_own_state():
    state = {
        "resources": [
            {
                "mode": "managed",
                "module": "module.idp",
                "type": "aws_dynamodb_table",
                "name": "configuration_table",
                "instances": [{"attributes": {"name": "idp-config"}}],
            },
            {
                "mode": "managed",
                "module": "module.idp",
                "type": "aws_dynamodb_table",
                "name": "tracking_table",
                "instances": [{"attributes": {"name": "idp-tracking"}}],
            },
            {
                "mode": "managed",
                "module": "module.idp",
                "type": "aws_dynamodb_table",
                "name": "concurrency_table",
                "instances": [{"attributes": {"name": "idp-concurrency"}}],
            },
        ]
    }
    assert tut.table_kinds(state) == {
        "idp-config": "configuration",
        "idp-tracking": "tracking_table",
        "idp-concurrency": "unknown",
    }


# -- the module manifest ----------------------------------------------------


def test_read_manifest_follows_the_installed_modules_entry(tmp_path):
    module = tmp_path / ".terraform" / "modules" / "idp"
    module.mkdir(parents=True)
    (module / "idp-terraform.json").write_text(json.dumps({"version": "0.6.15"}))
    (tmp_path / ".terraform" / "modules" / "modules.json").write_text(
        json.dumps(
            {
                "Modules": [
                    {"Key": "", "Dir": "."},
                    {"Key": "idp", "Dir": ".terraform/modules/idp"},
                ]
            }
        )
    )
    assert read_version(tmp_path) == "0.6.15"


def test_read_manifest_is_empty_when_nothing_is_installed(tmp_path):
    assert tut.read_manifest(tmp_path) == {}


def read_version(directory):
    return tut.read_manifest(directory).get("version")


# -- the deploy commands ----------------------------------------------------


class _Recording(tut.Run):
    """A Run whose ``idp-cli`` calls are recorded instead of made."""

    def __init__(
        self, settings, log_text="Plan: 0 to add, 0 to change, 0 to destroy.\n"
    ):
        super().__init__(settings)
        self.commands = []
        self.code = 0
        self.log_text = log_text

    def idp_cli(self, *args, log, timeout=0, check=True):
        self.commands.append(list(args))
        (self.work / "logs" / log).write_text(self.log_text)
        return self.code, ""


def test_the_upgrade_never_allows_replacing_a_stateful_resource(tmp_path):
    """The guard's refusal is the finding this tier exists to surface, so the run
    must not hand it the override."""
    run = _Recording(_settings(tmp_path))
    run.deploy(run.s.to_module_url, "upgrade.log", first=False)
    assert "--terraform-allow-replace-stateful" not in run.commands[0]
    assert "--terraform-allow-replace-retained" not in run.commands[0]


def test_the_upgrade_repeats_neither_the_admin_email_nor_the_parameters(tmp_path):
    """idp-cli remembers the first deploy's parameters by CloudFormation name; an
    upgrade that repeated them would not be testing that."""
    run = _Recording(_settings(tmp_path, parameters="LogLevel=INFO"))
    run.deploy(run.s.from_module_url, "deploy-from.log", first=True)
    run.deploy(run.s.to_module_url, "upgrade.log", first=False)
    first, upgrade = run.commands
    assert first[first.index("--parameters") + 1] == "LogLevel=INFO"
    assert "--admin-email" in first
    assert "--parameters" not in upgrade
    assert "--admin-email" not in upgrade
    assert upgrade[upgrade.index("--terraform-module-url") + 1] == run.s.to_module_url


def test_a_refused_upgrade_fails_the_phase_naming_the_addresses(tmp_path):
    run = _Recording(
        _settings(tmp_path),
        log_text=(
            "Plan: 1 to add, 2 to change, 3 to destroy.\n"
            "✗ Error: The plan would delete or replace 1 resource(s):\n"
            "  - module.idp.aws_s3_bucket.input_bucket (holds data)\n"
        ),
    )
    run.code = 1
    run.ctx.update(fromVersion="0.6.14", toVersion="0.6.15")
    with pytest.raises(tut.PhaseFailed) as e:
        run.phase_upgrade()
    assert "module.idp.aws_s3_bucket.input_bucket" in str(e.value)
    # The plan is recorded even though the apply never ran, so the report says what
    # the upgrade would have done.
    assert run.ctx["upgradePlan"] == {"add": 1, "change": 2, "destroy": 3}


# -- the driver's control flow ----------------------------------------------


class _Scripted(tut.Run):
    """A Run whose phases are stubs: each records itself, one may fail."""

    def __init__(self, settings, fail=None):
        super().__init__(settings)
        self.calls = []
        self.fail = fail

    def __getattribute__(self, name):
        if name.startswith("phase_"):
            phase = name[len("phase_") :].replace("_", "-")
            calls = object.__getattribute__(self, "calls")
            fail = object.__getattribute__(self, "fail")

            def stub():
                calls.append(phase)
                # The real phase_deploy_from marks the run as having created
                # something BEFORE it applies, so a failure inside it must still
                # tear down; the stub keeps that order. preflight sets its flag
                # only on success, as the real one does.
                if phase == "deploy-from":
                    self.ctx["created"] = True
                    self.ctx["deployStarted"] = True
                if phase == fail:
                    raise tut.PhaseFailed(f"{phase} broke")
                if phase == "preflight":
                    object.__setattr__(self, "preflight_passed", True)
                return "ok"

            return stub
        return object.__getattribute__(self, name)

    def _attempt(self, name, fn=None):
        try:
            self.run_phase(name)
        except Exception:  # noqa: BLE001 - keep tearing down
            pass


def _settings(tmp_path, **kw):
    base = dict(
        stack_name="tfup1",
        admin_email="admin@example.com",
        region="us-west-2",
        work_dir=tmp_path,
        terraform_bin="terraform",
        from_module_url=f"{PUBLISHED}/idp-terraform-0.6.14.zip",
        to_module_url=f"{PUBLISHED}/idp-terraform-0.6.15.zip",
    )
    base.update(kw)
    return tut.Settings(**base)


def test_a_clean_run_executes_every_phase_in_order(tmp_path):
    run = _Scripted(_settings(tmp_path))
    assert run.execute() == 0
    assert run.calls == list(tut.PHASES)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "PASS"
    assert result["fromModuleUrl"].endswith("idp-terraform-0.6.14.zip")
    assert "PASS" in (tmp_path / "summary.txt").read_text()


def test_a_failed_verification_still_destroys_and_sweeps(tmp_path):
    run = _Scripted(_settings(tmp_path), fail="verify")
    assert run.execute() == 1
    after = run.calls[run.calls.index("verify") + 1 :]
    assert after == ["destroy", "sweep", "leftover-check"]
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "FAIL"
    assert any("verify broke" in f for f in result["findings"])


def test_a_preflight_failure_runs_no_teardown_at_all(tmp_path):
    """A refusing preflight refuses exactly the cases where a sweep would reach
    another deployment: an unusable name, a clash with existing resources, the
    wrong account."""
    run = _Scripted(_settings(tmp_path), fail="preflight")
    assert run.execute() == 1
    assert run.calls == ["preflight"]


def test_a_failed_first_deploy_still_tears_down_what_it_created(tmp_path):
    """A half-applied deploy has created resources, and phase_deploy_from records
    that before the apply, so the teardown runs."""
    run = _Scripted(_settings(tmp_path), fail="deploy-from")
    assert run.execute() == 1
    assert run.calls == [
        "preflight",
        "deploy-from",
        "destroy",
        "sweep",
        "leftover-check",
    ]


def test_a_stored_created_marker_alone_does_not_permit_teardown(tmp_path):
    run = _Scripted(_settings(tmp_path), fail="preflight")
    run.ctx["created"] = True
    run.ctx["deployStarted"] = True
    assert run.execute() == 1
    assert run.calls == ["preflight"]


def test_keep_skips_the_teardown(tmp_path):
    run = _Scripted(_settings(tmp_path, keep=True), fail="seed")
    assert run.execute() == 1
    assert run.calls == ["preflight", "deploy-from", "seed"]


def test_stop_after_stops_without_tearing_down(tmp_path):
    run = _Scripted(_settings(tmp_path, stop_after="seed"))
    assert run.execute() == 0
    assert run.calls == ["preflight", "deploy-from", "seed"]
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "STOPPED"


def test_resume_skips_what_is_done_but_always_reruns_preflight(tmp_path):
    first = _Scripted(_settings(tmp_path, stop_after="seed"))
    first.execute()
    second = _Scripted(_settings(tmp_path, resume=True))
    assert second.execute() == 0
    assert second.calls[0] == "preflight"
    assert "seed" not in second.calls


def test_a_resumed_run_under_another_name_is_refused(tmp_path):
    first = _Scripted(_settings(tmp_path, stop_after="preflight"))
    first.execute()
    second = _Scripted(_settings(tmp_path, stack_name="tfup2", resume=True))
    assert second.execute() == 2
    assert second.calls == []


# -- the summary ------------------------------------------------------------


def test_the_summary_leads_with_the_findings():
    text = tut.summarise(
        {
            "status": "FAIL",
            "stackName": "tfup1",
            "region": "us-west-2",
            "fromModuleUrl": "a.zip",
            "toModuleUrl": "b.zip",
            "fromVersion": "0.6.14",
            "toVersion": "0.6.15",
            "findings": ["verify: bucket idp-input was replaced"],
            "upgradePlan": {"add": 1, "change": 2, "destroy": 3},
            "phases": [{"phase": "verify", "status": "FAIL", "seconds": 60}],
            "leftovers": [],
        }
    )
    lines = text.splitlines()
    assert lines[0] == "Terraform upgrade test: FAIL"
    assert "0.6.14 -> 0.6.15" in lines[1]
    assert lines.index("Findings:") < lines.index("Phases:")
    assert "Leftovers after teardown: 0" in text


# -- the Makefile target ----------------------------------------------------


def _make(*args):
    return subprocess.run(
        ["make", "--dry-run", "stacktest-terraform-upgrade", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_target_refuses_to_run_without_the_values_it_needs():
    for missing, args in (
        ("STACK_NAME", []),
        ("ADMIN_EMAIL", ["STACK_NAME=tfup1"]),
        ("FROM_MODULE_URL", ["STACK_NAME=tfup1", "ADMIN_EMAIL=a@example.com"]),
        (
            "TO_MODULE_URL",
            ["STACK_NAME=tfup1", "ADMIN_EMAIL=a@example.com", "FROM_MODULE_URL=x.zip"],
        ),
    ):
        proc = _make(*args)
        assert proc.returncode != 0, f"{missing} was not required"
        assert missing in proc.stderr


def test_the_target_passes_every_value_to_the_driver():
    proc = _make(
        "STACK_NAME=tfup1",
        "ADMIN_EMAIL=a@example.com",
        f"FROM_MODULE_URL={PUBLISHED}/idp-terraform-0.6.14.zip",
        f"TO_MODULE_URL={PUBLISHED}/idp-terraform-0.6.15.zip",
        "REGION=us-east-1",
        "PARAMETERS=LogLevel=INFO",
        "KEEP=1",
    )
    assert proc.returncode == 0, proc.stderr
    cmd = proc.stdout
    assert "scripts/sdlc/terraform_upgrade_test.py" in cmd
    for expected in (
        "--stack-name tfup1",
        "--admin-email a@example.com",
        "--from-module-url " + f"{PUBLISHED}/idp-terraform-0.6.14.zip",
        "--to-module-url " + f"{PUBLISHED}/idp-terraform-0.6.15.zip",
        "--region us-east-1",
        "--parameters LogLevel=INFO",
        "--keep",
    ):
        assert expected in cmd, expected


# -- what the snapshot covers ------------------------------------------------


def _bucket_state(*pairs):
    return {
        "resources": [
            {
                "mode": "managed",
                "module": "module.idp",
                "type": "aws_s3_bucket",
                "name": name,
                "instances": [{"attributes": {"bucket": bucket}}],
            }
            for name, bucket in pairs
        ]
    }


class _StateOnly(tut.Run):
    def __init__(self, settings, state):
        super().__init__(settings)
        self._state = state

    def state(self):
        return self._state


def test_the_snapshot_covers_the_data_buckets_but_not_the_access_log_bucket(tmp_path):
    """S3 delivers access logs on its own schedule, so an object landing during the
    settle loop or between the two snapshots is not the upgrade's doing. The
    identity check still covers the bucket."""
    run = _StateOnly(
        _settings(tmp_path),
        _bucket_state(
            ("input_bucket", "idp-input"),
            ("output_bucket", "idp-output"),
            ("logging_bucket", "idp-logs"),
        ),
    )
    targets = run.snapshot_targets()
    assert targets["buckets"] == ["idp-input", "idp-output"]
    identity = {
        k: v
        for k, v in __import__("terraform_migration_test")
        .physical_names(run._state)
        .items()
        if k in tut.IDENTITY_KINDS
    }
    assert "idp-logs" in identity["buckets"]
    assert tut.identity_losses(identity, {"buckets": ["idp-input", "idp-output"]}) == [
        "bucket idp-logs"
    ]


def test_the_snapshot_targets_are_recorded_once(tmp_path):
    """The comparison after the upgrade must cover what was snapshotted before it,
    even if the upgrade added a bucket."""
    run = _StateOnly(_settings(tmp_path), _bucket_state(("input_bucket", "idp-input")))
    assert run.snapshot_targets()["buckets"] == ["idp-input"]
    run._state = _bucket_state(("input_bucket", "idp-input"), ("new_bucket", "idp-new"))
    assert run.snapshot_targets()["buckets"] == ["idp-input"]
