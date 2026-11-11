# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Moving a genai-idp-terraform deployment onto the built-in module in place.

The fixtures under ``fixtures/genai_idp_terraform/`` are sanitised copies of a live
migration's inputs: the old deployment's state (``old_state.tfstate``, from
upstream tag v0.6.9-tf.0, ``examples/unified-processor``), the adoption plan that
deployment produced (``adopt_plan.json``), and the blocks of the published module
the migration reads (``module/``). Account ids, pool ids, key ids and emails are
placeholders.

``SPIKE_IMPORTS`` and ``SPIKE_DETACHED`` are what the live migration imported and
removed from the old state by hand (sanitised the same way); the plan must cover
both.
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk._core import terraform_deploy as tf
from idp_sdk._core import terraform_migrate as tm
from idp_sdk.exceptions import IDPConfigurationError

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "genai_idp_terraform"
REGION = "us-west-2"
POOL = "us-west-2_EXAMPLE01"
ADMIN = "admin@example.com"

SPIKE_IMPORTS = {
    "module.idp.aws_s3_bucket.input_bucket": "oldidp-input-i2pfi4o2",
    "module.idp.aws_s3_bucket.output_bucket": "oldidp-output-i2pfi4o2",
    "module.idp.aws_s3_bucket.working_bucket": "oldidp-working-i2pfi4o2",
    "module.idp.aws_s3_bucket.evaluation_baseline_bucket[0]": "oldidp-baseline-i2pfi4o2",
    "module.idp.aws_s3_bucket.test_set_bucket": "oldidp-bf1fipnn-api-test-sets",
    "module.idp.awscc_kms_key.customer_managed_encryption_key": "1234abcd-12ab-34cd-56ef-1234567890ab",
    "module.idp.aws_dynamodb_table.tracking_table": "idp-tracking-table-mr68w2wl",
    "module.idp.aws_dynamodb_table.configuration_table": "idp-configuration-table-mr68w2wl",
    "module.idp.aws_dynamodb_table.users_table": "oldidp-bf1fipnn-api-users",
    "module.idp.aws_dynamodb_table.chat_sessions_table": "oldidp-bf1fipnn-api-agent-chat-sessions",
    "module.idp.aws_cognito_user_pool.user_pool": POOL,
    "module.idp.awscc_cognito_user_pool_group.admin_group": f"{POOL}|Admin",
    "module.idp.awscc_cognito_user_pool_group.author_group": f"{POOL}|Author",
    "module.idp.awscc_cognito_user_pool_group.reviewer_group": f"{POOL}|Reviewer",
    "module.idp.awscc_cognito_user_pool_group.viewer_group": f"{POOL}|Viewer",
    "module.idp.aws_cognito_user.admin_user": f"{POOL}/{ADMIN}",
    "module.idp.aws_cognito_user_in_group.admin_user_to_group_attachment": f"{POOL},Admin,{ADMIN}",
}

_GEN = "module.genai_idp_accelerator.module."
SPIKE_DETACHED = [
    "aws_s3_bucket.input_bucket",
    "aws_s3_bucket.output_bucket",
    "aws_s3_bucket.working_bucket",
    "aws_s3_bucket.evaluation_baseline_bucket[0]",
    "aws_s3_bucket_public_access_block.input_bucket",
    "aws_s3_bucket_public_access_block.output_bucket",
    "aws_s3_bucket_public_access_block.working_bucket",
    "aws_s3_bucket_public_access_block.evaluation_baseline_bucket[0]",
    "aws_s3_bucket_server_side_encryption_configuration.evaluation_baseline_bucket[0]",
    "aws_s3_bucket_versioning.evaluation_baseline_bucket[0]",
    "aws_s3_bucket_notification.input_bucket_notification",
    "aws_kms_key.encryption_key",
    "aws_cognito_user_pool.user_pool",
    "awscc_bedrock_data_automation_project.bda_project[0]",
    _GEN + 'rbac[0].aws_cognito_user_group.rbac["Admin"]',
    _GEN + 'rbac[0].aws_cognito_user_group.rbac["Author"]',
    _GEN + 'rbac[0].aws_cognito_user_group.rbac["Reviewer"]',
    _GEN + 'rbac[0].aws_cognito_user_group.rbac["Viewer"]',
    _GEN + "rbac[0].aws_dynamodb_table.users",
    _GEN
    + "processing_environment.module.tracking_table[0].aws_dynamodb_table.tracking_table",
    _GEN
    + "processing_environment.module.configuration_table[0].aws_dynamodb_table.configuration_table",
    _GEN + "processing_environment_api[0].aws_dynamodb_table.agent_chat_sessions[0]",
    _GEN + "processing_environment_api[0].aws_s3_bucket.test_sets[0]",
    _GEN
    + "processing_environment_api[0].aws_s3_bucket_public_access_block.test_sets[0]",
    _GEN
    + "processing_environment_api[0].aws_s3_bucket_server_side_encryption_configuration.test_sets[0]",
    _GEN + "processing_environment_api[0].aws_s3_bucket_versioning.test_sets[0]",
    _GEN + "web_ui[0].aws_s3_bucket_cors_configuration.input_bucket_cors",
    _GEN + "web_ui[0].aws_s3_bucket_cors_configuration.output_bucket_cors",
    _GEN + "web_ui[0].aws_s3_bucket_cors_configuration.test_set_bucket_cors[0]",
    _GEN + "web_ui[0].aws_s3_bucket_cors_configuration.working_bucket_cors[0]",
]


def old_state():
    return json.loads((FIXTURES / "old_state.tfstate").read_text())


def module():
    return tm.ModuleSource.read(FIXTURES / "module")


def admin_exists(pool_id, username):
    return tm.UserLookup(exists=True, groups=["Admin"])


def build(state=None, **kwargs):
    kwargs.setdefault("admin_email", ADMIN)
    kwargs.setdefault("user_lookup", admin_exists)
    return tm.build_plan(state or old_state(), module(), **kwargs)


def add_resource(state, tf_type, name, attributes, module_path=None):
    res = {
        "mode": "managed",
        "type": tf_type,
        "name": name,
        "instances": [{"attributes": attributes}],
    }
    if module_path:
        res["module"] = module_path
    state["resources"].append(res)
    return state


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class TestPlan:
    def test_every_import_the_live_migration_made_is_planned(self):
        plan = build()
        planned = {r.new_address: r.import_id for r in plan.imports}
        for address, import_id in SPIKE_IMPORTS.items():
            assert planned.get(address) == import_id, address

    def test_the_user_pool_is_imported_by_its_own_phase_not_adopt_tf(self):
        plan = build()
        pool = next(r for r in plan.imports if r.kind == "user_pool")
        assert pool.import_in_adopt_tf is False
        text = tm.render_adopt_tf(plan)
        assert "aws_cognito_user_pool.user_pool\n" not in text
        assert "to = module.idp.aws_dynamodb_table.tracking_table" in text
        # every other import is in adopt.tf exactly once
        for r in plan.imports:
            if r.import_in_adopt_tf:
                assert text.count(f"to = {r.new_address}\n") == 1

    def test_everything_the_live_migration_detached_is_detached(self):
        detached = set(build().detach_addresses)
        missing = [a for a in SPIKE_DETACHED if a not in detached]
        assert missing == []

    def test_settings_follow_their_bucket(self):
        by = {r.old_address: r for r in build().resources}
        kept = by["aws_s3_bucket_public_access_block.input_bucket"]
        assert (kept.verdict, kept.detach) == (tm.SETTING, True)
        assets = by[
            _GEN + "assets_bucket.aws_s3_bucket_public_access_block.assets_bucket"
        ]
        assert (assets.verdict, assets.detach) == (tm.RECREATE, False)

    def test_a_table_item_follows_its_table(self):
        by = {r.old_address: r for r in build().resources}
        counter = _GEN + (
            "processing_environment.module.concurrency_table[0]"
            ".aws_dynamodb_table_item.workflow_counter"
        )
        # the concurrency table is recreated, so its counter goes with it
        assert by[counter].detach is False
        state = add_resource(
            old_state(),
            "aws_dynamodb_table_item",
            "seed",
            {"table_name": "idp-configuration-table-mr68w2wl", "id": "x"},
        )
        assert {r.old_address: r for r in build(state).resources}[
            "aws_dynamodb_table_item.seed"
        ].detach is True

    def test_recreated_and_unmanaged_kinds(self):
        by = {r.kind: r for r in build().resources}
        assert by["kb_collection"].verdict == tm.RECREATE
        assert by["discovery_tracking_table"].verdict == tm.RECREATE
        assert by["identity_pool"].verdict == tm.RECREATE
        assert (by["bda_project"].verdict, by["bda_project"].detach) == (
            tm.KEEP_UNMANAGED,
            True,
        )
        reporting = by["reporting_bucket"]
        assert (reporting.verdict, reporting.detach, reporting.new_address) == (
            tm.ADOPT_BY_PARAMETER,
            True,
            None,
        )

    def test_parameters_the_old_deployment_requires_are_derived(self):
        plan = build(parameters={"LogRetentionDays": "90"})
        assert plan.parameters == {
            "LogRetentionDays": "90",
            "ReportingBucketName": "oldidp-reporting-i2pfi4o2",
            "ExternalIdPEmailMutable": "true",
        }

    def test_a_contradicting_parameter_is_refused(self):
        with pytest.raises(
            IDPConfigurationError, match="ExternalIdPEmailMutable=false contradicts"
        ):
            build(parameters={"ExternalIdPEmailMutable": "false"})

    def test_an_unknown_stateful_resource_is_refused_not_skipped(self):
        state = add_resource(
            old_state(), "aws_dynamodb_table", "mystery", {"name": "data"}
        )
        with pytest.raises(IDPConfigurationError, match=r"aws_dynamodb_table\.mystery"):
            build(state)

    @pytest.mark.parametrize(
        "tf_type,name,attrs",
        [
            ("aws_s3_bucket", "finetuning_data", {"bucket": "ft"}),
            (
                "aws_cognito_user_pool_domain",
                "hosted_ui",
                {"user_pool_id": POOL, "domain": "d"},
            ),
        ],
    )
    def test_an_unsupported_kind_is_refused(self, tf_type, name, attrs):
        state = add_resource(old_state(), tf_type, name, attrs)
        with pytest.raises(IDPConfigurationError, match=f"{tf_type}.{name}"):
            build(state)

    def test_an_unmapped_queue_is_recreated_not_refused(self):
        state = add_resource(old_state(), "aws_sqs_queue", "extra", {"name": "q"})
        assert {r.old_address: r.verdict for r in build(state).resources}[
            "aws_sqs_queue.extra"
        ] == tm.RECREATE

    def test_show_json_output_is_refused(self):
        with pytest.raises(IDPConfigurationError, match="terraform show -json"):
            build({"format_version": "1.0", "values": {}})

    def test_a_state_without_a_tracking_table_is_refused(self):
        state = old_state()
        state["resources"] = [
            r for r in state["resources"] if r["name"] != "tracking_table"
        ]
        with pytest.raises(IDPConfigurationError, match="no tracking table"):
            build(state)

    def test_a_counted_module_resource_gets_its_index(self):
        assert module().resource_address(
            "aws_s3_bucket.evaluation_baseline_bucket"
        ) == ("aws_s3_bucket.evaluation_baseline_bucket[0]")
        assert (
            module().resource_address("aws_s3_bucket.input_bucket")
            == "aws_s3_bucket.input_bucket"
        )

    def test_an_address_the_module_does_not_declare_is_refused(self, tmp_path):
        shutil.copytree(FIXTURES / "module", tmp_path, dirs_exist_ok=True)
        text = (FIXTURES / "module" / "main.tf").read_text()
        (tmp_path / "main.tf").write_text(
            text.replace(
                'resource "aws_dynamodb_table" "agent_table"',
                'resource "aws_dynamodb_table" "renamed"',
            )
        )
        mod = tm.ModuleSource.read(tmp_path)
        with pytest.raises(
            IDPConfigurationError, match="declares no aws_dynamodb_table.agent_table"
        ):
            tm.build_plan(old_state(), mod, admin_email=ADMIN, user_lookup=admin_exists)

    def test_an_admin_email_with_no_user_lets_the_module_create_one(self):
        plan = build(user_lookup=lambda p, u: tm.UserLookup(exists=False))
        assert not [r for r in plan.imports if r.kind.startswith("admin")]
        assert plan.admin_username is None
        assert any("case-sensitive" in w and "exact case" in w for w in plan.warnings)

    def test_an_existing_user_outside_the_admin_group_is_adopted_without_membership(
        self,
    ):
        plan = build(
            user_lookup=lambda p, u: tm.UserLookup(exists=True, groups=["Viewer"])
        )
        kinds = {r.kind for r in plan.imports}
        assert "admin_user" in kinds and "admin_group_membership" not in kinds

    def test_the_old_admin_user_is_adopted_when_the_names_match(self):
        state = add_resource(
            old_state(),
            "aws_cognito_user",
            "admin_user",
            {"user_pool_id": POOL, "username": ADMIN},
            None,
        )
        state["resources"][-1]["instances"][0]["index_key"] = 0
        called = []
        plan = build(state, user_lookup=lambda p, u: called.append(u))
        assert called == []  # the state answered the question
        user = next(
            r
            for r in plan.resources
            if r.old_address == "aws_cognito_user.admin_user[0]"
        )
        assert (user.new_address, user.import_id, user.detach) == (
            "module.idp.aws_cognito_user.admin_user",
            f"{POOL}/{ADMIN}",
            True,
        )

    def test_another_old_user_is_kept_in_the_pool(self):
        state = add_resource(
            old_state(),
            "aws_cognito_user",
            "admin_user",
            {"user_pool_id": POOL, "username": "Other@example.com"},
        )
        user = next(
            r
            for r in build(state).resources
            if r.old_address == "aws_cognito_user.admin_user"
        )
        assert (user.verdict, user.new_address, user.detach) == (
            tm.KEEP_UNMANAGED,
            None,
            True,
        )

    def test_a_group_the_module_does_not_declare_is_kept_in_the_pool(self):
        state = add_resource(
            old_state(),
            "aws_cognito_user_group",
            "rbac",
            {"user_pool_id": POOL, "name": "Auditors"},
            "module.genai_idp_accelerator.module.rbac[0]",
        )
        state["resources"][-1]["instances"][0]["index_key"] = "Auditors"
        group = next(
            r for r in build(state).resources if r.old_address.endswith('["Auditors"]')
        )
        assert (group.verdict, group.new_address, group.detach) == (
            tm.KEEP_UNMANAGED,
            None,
            True,
        )

    def test_the_upload_rule_is_the_one_on_the_input_bucket(self):
        rules = build().upload_rules
        assert [r["name"] for r in rules] == [
            "oldidp-bf1fipnn-processor-s3-event-rule-ud61nnd8"
        ]

    def test_import_ids_are_escaped_for_hcl(self):
        plan = build()
        r = next(r for r in plan.imports if r.kind == "input_bucket")
        r.import_id = "a${b}"
        assert 'id = "a$${b}"' in tm.render_adopt_tf(plan)

    def test_the_plan_file_round_trips(self):
        plan = build()
        again = tm.MigrationPlan.from_json(json.loads(json.dumps(plan.to_json())))
        assert again.to_json() == plan.to_json()


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


class TestRetention:
    @pytest.fixture(autouse=True)
    def _aws(self, aws_credentials):
        with mock_aws():
            self.s3 = boto3.client("s3", region_name=REGION)
            self.plan = build()
            for b in self.plan.adopted_buckets:
                self.s3.create_bucket(
                    Bucket=b["bucket"],
                    CreateBucketConfiguration={"LocationConstraint": REGION},
                )
            self.s3.put_object(Bucket="oldidp-input-i2pfi4o2", Key="old.pdf", Body=b"x")
            yield

    def check(self, values=None, accept=(), days_later=400):
        now = datetime.now(timezone.utc) + timedelta(days=days_later)
        return tm.check_retention(
            self.plan, module(), values or {}, self.s3, accept_expiry=accept, now=now
        )

    def test_an_object_older_than_the_expiry_is_a_finding(self):
        rows, findings = self.check()
        assert (
            len(findings) == 1 and "input_bucket oldidp-input-i2pfi4o2" in findings[0]
        )
        assert "data_retention_in_days" in findings[0]
        row = next(r for r in rows if r["kind"] == "input_bucket")
        assert (row["expiryDays"], row["ok"]) == (365, False)

    def test_a_longer_retention_parameter_clears_it(self):
        _, findings = self.check(values={"data_retention_in_days": 500})
        assert findings == []

    def test_younger_objects_are_fine(self):
        _, findings = self.check(days_later=10)
        assert findings == []

    def test_accept_expiry_clears_one_kind(self):
        rows, findings = self.check(accept=("input_bucket",))
        assert (
            findings == []
            and "accepted"
            in next(r for r in rows if r["kind"] == "input_bucket")["note"]
        )

    def test_a_fixed_rule_says_so(self):
        self.s3.put_object(Bucket="oldidp-bf1fipnn-logs", Key="log", Body=b"x")
        _, findings = self.check(values={"data_retention_in_days": 500})
        assert (
            len(findings) == 1
            and "fixed in the module" in findings[0]
            and "180" in findings[0]
        )

    def test_the_working_bucket_uses_log_retention(self):
        rows, _ = self.check(values={"log_retention_days": 14})
        assert (
            next(r for r in rows if r["kind"] == "working_bucket")["expiryDays"] == 14
        )


# ---------------------------------------------------------------------------
# Operations: a fake terraform binary and a planned directory
# ---------------------------------------------------------------------------


class FakeRunner:
    """Stands in for TerraformRunner; one shared record of every call."""

    calls: list = []
    state_lists: dict = {}
    plan_json: dict = {}
    pulled: dict = {}

    def __init__(self, binary, directory):
        self.directory = Path(directory)

    def init(self, upgrade=False):
        FakeRunner.calls.append(("init", self.directory))

    def run(self, *args):
        FakeRunner.calls.append(("run", self.directory, *args))
        if args[:2] == ("state", "push"):
            FakeRunner.pulled[self.directory] = json.loads(Path(args[2]).read_text())

    def plan(self, plan_file):
        (self.directory / plan_file).write_text("plan")
        FakeRunner.calls.append(("plan", self.directory))

    def show_plan(self, plan_file):
        return FakeRunner.plan_json

    def apply_plan(self, plan_file):
        FakeRunner.calls.append(("apply", self.directory))

    def state_list(self):
        return FakeRunner.state_lists.get(self.directory, [])

    def capture(self, *args):
        assert args == ("state", "pull")
        return json.dumps(FakeRunner.pulled[self.directory])

    def state_rm(self, addresses):
        FakeRunner.calls.append(("state rm", self.directory, tuple(addresses)))


@pytest.fixture
def fake_tf(monkeypatch):
    (
        FakeRunner.calls,
        FakeRunner.state_lists,
        FakeRunner.plan_json,
        FakeRunner.pulled,
    ) = [], {}, {}, {}
    monkeypatch.setattr(tf, "TerraformRunner", FakeRunner)
    return FakeRunner


@pytest.fixture
def planned_dir(tmp_path):
    """A new deployment's directory as the `plan` phase leaves it."""
    directory = tmp_path / "idp-terraform" / "newidp"
    mdir = directory / tm.MIGRATION_DIR
    mdir.mkdir(parents=True)
    (mdir / tm.PLAN_FILE).write_text(json.dumps(build().to_json()))
    mod = directory / ".terraform" / "modules"
    mod.mkdir(parents=True)
    (mod / "modules.json").write_text(
        json.dumps({"Modules": [{"Key": "idp", "Dir": str(FIXTURES / "module")}]})
    )
    return directory


def migration():
    return IDPClient(
        stack_name="newidp", region=REGION
    ).terraform.migrate_from_genai_idp_terraform


def tracking_and_config_tables(ddb):
    ddb.create_table(
        TableName="idp-tracking-table-mr68w2wl",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="idp-configuration-table-mr68w2wl",
        KeySchema=[{"AttributeName": "Configuration", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "Configuration", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def upload_rule(events, state="ENABLED"):
    events.put_rule(
        Name="oldidp-bf1fipnn-processor-s3-event-rule-ud61nnd8",
        EventPattern=json.dumps({"source": ["aws.s3"]}),
        State=state,
    )


def doc(ddb, key, status):
    ddb.put_item(
        TableName="idp-tracking-table-mr68w2wl",
        Item={
            "PK": {"S": f"doc#{key}"},
            "SK": {"S": "none"},
            "ObjectStatus": {"S": status},
        },
    )


# ---------------------------------------------------------------------------
# freeze
# ---------------------------------------------------------------------------


class TestFreeze:
    @pytest.fixture(autouse=True)
    def _aws(self, aws_credentials):
        with mock_aws():
            self.events = boto3.client("events", region_name=REGION)
            self.ddb = boto3.client("dynamodb", region_name=REGION)
            tracking_and_config_tables(self.ddb)
            upload_rule(self.events)
            yield

    def rule_state(self):
        return self.events.describe_rule(
            Name="oldidp-bf1fipnn-processor-s3-event-rule-ud61nnd8"
        )["State"]

    def test_a_dry_run_lists_the_rule_and_leaves_it_enabled(
        self, planned_dir, aws_credentials
    ):
        result = migration().freeze(str(planned_dir))
        assert result.changes == [
            "disable EventBridge rule oldidp-bf1fipnn-processor-s3-event-rule-ud61nnd8 (bus default)"
        ]
        assert (result.applied, result.ok) == (False, False)
        assert self.rule_state() == "ENABLED"

    def test_apply_disables_the_rule(self, planned_dir, aws_credentials):
        result = migration().freeze(str(planned_dir), apply=True)
        assert (result.applied, result.ok) == (True, True)
        assert self.rule_state() == "DISABLED"
        assert "aws events enable-rule" in result.back_out[0]

    def test_in_flight_documents_block(self, planned_dir, aws_credentials):
        doc(self.ddb, "a.pdf", "EXTRACTING")
        doc(self.ddb, "b.pdf", "COMPLETED")
        result = migration().freeze(str(planned_dir), apply=True)
        assert result.ok is False
        assert result.details["inFlight"] == [{"key": "a.pdf", "status": "EXTRACTING"}]

    def test_wait_polls_until_nothing_is_in_flight(self, planned_dir, aws_credentials):
        doc(self.ddb, "a.pdf", "QUEUED")
        op = migration()
        op._sleep = lambda s: doc(self.ddb, "a.pdf", "COMPLETED")
        result = op.freeze(
            str(planned_dir), apply=True, wait=True, poll_seconds=0, timeout_seconds=5
        )
        assert result.ok is True and result.details["inFlightCount"] == 0
        assert any("in flight; waiting" in n for n in result.notes)


# ---------------------------------------------------------------------------
# fix-config
# ---------------------------------------------------------------------------


def config_row(key, **extra):
    item = {"Configuration": {"S": key}, "Description": {"S": key}}
    item.update({k: {"N": str(v)} for k, v in extra.items()})
    return item


class TestFixConfig:
    @pytest.fixture(autouse=True)
    def _aws(self, aws_credentials):
        with mock_aws():
            self.ddb = boto3.client("dynamodb", region_name=REGION)
            tracking_and_config_tables(self.ddb)
            for item in (
                config_row("Config#default", LatestRevision=3, PublishedRevision=3),
                config_row("Config#bda", LatestRevision=1),
                config_row("Config#plain"),
                config_row("Schema"),
            ):
                self.ddb.put_item(
                    TableName="idp-configuration-table-mr68w2wl", Item=item
                )
            yield

    def row(self, key):
        return self.ddb.get_item(
            TableName="idp-configuration-table-mr68w2wl",
            Key={"Configuration": {"S": key}},
        )["Item"]

    def test_a_dry_run_lists_and_changes_nothing(self, planned_dir, aws_credentials):
        result = migration().fix_config(str(planned_dir))
        assert [c.split(" from ")[1].split(" in ")[0] for c in result.changes] == [
            "Config#bda",
            "Config#default",
        ]
        assert result.applied is False and "LatestRevision" in self.row(
            "Config#default"
        )

    def test_apply_backs_up_then_removes_the_counters(
        self, planned_dir, aws_credentials
    ):
        result = migration().fix_config(str(planned_dir), apply=True)
        assert result.applied is True
        for key in ("Config#default", "Config#bda"):
            assert not {"LatestRevision", "PublishedRevision"} & set(self.row(key))
        backup = json.loads(Path(result.backup_path).read_text())
        assert sorted(i["Configuration"]["S"] for i in backup["Items"]) == [
            "Config#bda",
            "Config#default",
            "Config#plain",
        ]
        assert oct(Path(result.backup_path).stat().st_mode & 0o777) == "0o600"

    def test_restore_puts_the_rows_back(self, planned_dir, aws_credentials):
        backup = migration().fix_config(str(planned_dir), apply=True).backup_path
        migration().fix_config(str(planned_dir), apply=True, restore=backup)
        assert self.row("Config#default")["LatestRevision"] == {"N": "3"}

    def test_a_table_with_a_revision_history_is_refused(
        self, planned_dir, aws_credentials
    ):
        self.ddb.put_item(
            TableName="idp-configuration-table-mr68w2wl",
            Item=config_row("ConfigRevIndex#default"),
        )
        result = migration().fix_config(str(planned_dir), apply=True)
        assert result.ok is False and "revision history" in result.findings[0]
        assert "LatestRevision" in self.row("Config#default")


# ---------------------------------------------------------------------------
# user pool schema
# ---------------------------------------------------------------------------

POOL_ADDRESS = "module.idp.aws_cognito_user_pool.user_pool"


def spike_pool_change():
    plan = json.loads((FIXTURES / "adopt_plan.json").read_text())
    return next(rc for rc in plan["resource_changes"] if rc["address"] == POOL_ADDRESS)


class TestPoolSchema:
    def test_the_imported_schema_is_rewritten_to_the_module_created_form(self):
        rc = spike_pool_change()
        schema, changes, conflicts = tm.normalise_pool_schema(
            rc["change"]["before"]["schema"], rc["change"]["after"]["schema"]
        )
        assert conflicts == []
        # what a pool the module created records (measured)
        assert schema == [
            {
                "attribute_data_type": "String",
                "developer_only_attribute": False,
                "mutable": True,
                "name": "email",
                "number_attribute_constraints": [],
                "required": True,
                "string_attribute_constraints": [],
            }
        ]
        assert any(c.startswith("rewrite 'email'") for c in changes)
        assert any("'idp_groups'" in c and "apply adds it" in c for c in changes)

    def test_an_immutable_difference_is_a_conflict_not_a_rewrite(self):
        rc = spike_pool_change()
        before = json.loads(json.dumps(rc["change"]["before"]["schema"]))
        before[0]["mutable"] = False
        _, _, conflicts = tm.normalise_pool_schema(
            before, rc["change"]["after"]["schema"]
        )
        assert len(conflicts) == 1 and "ExternalIdPEmailMutable" in conflicts[0]

    def test_an_attribute_the_module_does_not_declare_is_dropped_from_state(self):
        rc = spike_pool_change()
        before = rc["change"]["before"]["schema"] + [
            {"name": "custom:tenant", "attribute_data_type": "String"}
        ]
        schema, changes, _ = tm.normalise_pool_schema(
            before, rc["change"]["after"]["schema"]
        )
        assert [s["name"] for s in schema] == ["email"]
        assert any(c.startswith("drop 'custom:tenant'") for c in changes)

    def test_the_state_rewrite_bumps_the_serial(self):
        state = {
            "serial": 7,
            "resources": [
                {
                    "module": "module.idp",
                    "type": "aws_cognito_user_pool",
                    "name": "user_pool",
                    "instances": [{"attributes": {"schema": [{"name": "x"}]}}],
                }
            ],
        }
        new = tm.rewrite_state_schema(state, POOL_ADDRESS, [{"name": "email"}])
        assert new["serial"] == 8 and tm.state_schema(new, POOL_ADDRESS) == [
            {"name": "email"}
        ]
        assert tm.state_schema(state, POOL_ADDRESS) == [{"name": "x"}]


class TestAdoptUserPoolOperation:
    def state_with_pool(self, schema):
        return {
            "version": 4,
            "serial": 3,
            "lineage": "l",
            "resources": [
                {
                    "module": "module.idp",
                    "mode": "managed",
                    "type": "aws_cognito_user_pool",
                    "name": "user_pool",
                    "instances": [{"attributes": {"id": POOL, "schema": schema}}],
                }
            ],
        }

    def test_a_dry_run_only_lists_the_import(self, fake_tf, planned_dir):
        result = migration().adopt_user_pool(str(planned_dir))
        assert result.changes == [f"terraform import {POOL_ADDRESS} {POOL}"]
        assert not [c for c in fake_tf.calls if c[0] == "run"]

    def test_apply_imports_then_pushes_the_normalised_schema(
        self, fake_tf, planned_dir
    ):
        rc = spike_pool_change()
        fake_tf.plan_json = {"resource_changes": [rc]}
        fake_tf.pulled[planned_dir] = self.state_with_pool(
            rc["change"]["before"]["schema"]
        )
        result = migration().adopt_user_pool(str(planned_dir), apply=True)
        runs = [c[2:] for c in fake_tf.calls if c[0] == "run"]
        assert runs[0] == ("import", "-input=false", POOL_ADDRESS, POOL)
        assert runs[1][:2] == ("state", "push")
        pushed = fake_tf.pulled[planned_dir]
        assert pushed["serial"] == 4
        assert (
            tm.state_schema(pushed, POOL_ADDRESS)[0]["string_attribute_constraints"]
            == []
        )
        backup = json.loads(Path(result.backup_path).read_text())
        assert backup["serial"] == 3
        assert not (planned_dir / "idp-cli-migrate.tfplan").exists()
        assert not (
            planned_dir / tm.MIGRATION_DIR / "new-state-normalised.tfstate"
        ).exists()

    def test_a_conflict_pushes_nothing(self, fake_tf, planned_dir):
        rc = spike_pool_change()
        fake_tf.state_lists[planned_dir] = [POOL_ADDRESS]
        fake_tf.plan_json = {"resource_changes": [rc]}
        before = json.loads(json.dumps(rc["change"]["before"]["schema"]))
        before[0]["required"] = False
        fake_tf.pulled[planned_dir] = self.state_with_pool(before)
        result = migration().adopt_user_pool(str(planned_dir), apply=True)
        assert result.ok is False
        assert not [c for c in fake_tf.calls if c[0] == "run"]


# ---------------------------------------------------------------------------
# the adoption plan
# ---------------------------------------------------------------------------


def clean_adoption_plan(plan):
    """A plan JSON in which every planned import is imported in place."""
    rcs = []
    for r in plan.imports:
        change = {"actions": ["update"], "before": {}, "after": {}}
        if r.import_in_adopt_tf:
            change["importing"] = {"id": r.import_id}
        else:
            normal = [
                tm._module_created_form(i)
                for i in spike_pool_change()["change"]["after"]["schema"][:1]
            ]
            change["before"] = {"schema": normal}
            change["after"] = {
                "schema": spike_pool_change()["change"]["after"]["schema"]
            }
        rcs.append({"address": r.new_address, "type": "x", "change": change})
    rcs.append(
        {
            "address": "module.idp.aws_sqs_queue.q",
            "type": "x",
            "change": {"actions": ["create"]},
        }
    )
    return {"resource_changes": rcs}


class TestAdoptionPlanCheck:
    def test_the_live_single_pass_plan_is_refused_for_the_pool(self):
        spike = json.loads((FIXTURES / "adopt_plan.json").read_text())
        counts, findings = tm.check_adoption_plan(spike, build())
        assert counts["import"] == 17 and counts["destroy"] == 0
        assert any("run `adopt-user-pool` first" in f for f in findings)
        assert any("modify or remove existing schema items" in f for f in findings)

    def test_a_clean_plan_passes(self):
        plan = build()
        counts, findings = tm.check_adoption_plan(clean_adoption_plan(plan), plan)
        assert findings == []
        assert counts == {
            "import": len(plan.imports) - 1,
            "add": 1,
            "change": len(plan.imports),
            "destroy": 0,
        }

    def test_any_destroy_is_refused(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        pj["resource_changes"].append(
            {
                "address": "module.idp.aws_x.y",
                "type": "x",
                "change": {"actions": ["delete"]},
            }
        )
        _, findings = tm.check_adoption_plan(pj, plan)
        assert findings == [
            "module.idp.aws_x.y would be destroyed; an adoption destroys nothing, so stop and find out why"
        ]

    def test_a_replaced_adopted_resource_is_refused(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        rc = next(
            rc
            for rc in pj["resource_changes"]
            if rc["address"].endswith("tracking_table")
        )
        rc["change"]["actions"] = ["delete", "create"]
        _, findings = tm.check_adoption_plan(pj, plan)
        assert any("tracking_table would be replaced" in f for f in findings)
        assert any("tracking_table plans ['delete', 'create']" in f for f in findings)

    def test_a_create_instead_of_an_import_is_refused(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        rc = next(
            rc
            for rc in pj["resource_changes"]
            if rc["address"].endswith("input_bucket")
        )
        del rc["change"]["importing"]
        rc["change"]["actions"] = ["create"]
        _, findings = tm.check_adoption_plan(pj, plan)
        assert any(
            "input_bucket is in the plan but not as an import" in f for f in findings
        )

    def test_a_wrong_import_id_is_refused(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        rc = next(
            rc for rc in pj["resource_changes"] if rc["address"].endswith("users_table")
        )
        rc["change"]["importing"]["id"] = "other"
        _, findings = tm.check_adoption_plan(pj, plan)
        assert any("imports 'other'" in f for f in findings)


class TestApplyOperation:
    @pytest.fixture(autouse=True)
    def _aws(self, aws_credentials):
        with mock_aws():
            self.events = boto3.client("events", region_name=REGION)
            self.ddb = boto3.client("dynamodb", region_name=REGION)
            self.s3 = boto3.client("s3", region_name=REGION)
            tracking_and_config_tables(self.ddb)
            for b in build().adopted_buckets:
                self.s3.create_bucket(
                    Bucket=b["bucket"],
                    CreateBucketConfiguration={"LocationConstraint": REGION},
                )
            yield

    def test_preconditions_block_the_apply(self, fake_tf, planned_dir, aws_credentials):
        upload_rule(self.events, "ENABLED")
        self.ddb.put_item(
            TableName="idp-configuration-table-mr68w2wl",
            Item=config_row("Config#default", LatestRevision=2),
        )
        fake_tf.plan_json = clean_adoption_plan(build())
        result = migration().apply(str(planned_dir), apply=True)
        assert result.ok is False and result.applied is False
        assert any("run `freeze`" in f for f in result.findings)
        assert any("run `fix-config`" in f for f in result.findings)
        assert not [c for c in fake_tf.calls if c[0] == "apply"]
        assert not (planned_dir / "idp-cli-migrate.tfplan").exists()

    def test_a_clean_adoption_applies_the_checked_plan(
        self, fake_tf, planned_dir, aws_credentials
    ):
        upload_rule(self.events, "DISABLED")
        fake_tf.plan_json = clean_adoption_plan(build())
        result = migration().apply(str(planned_dir), apply=True)
        assert (result.ok, result.applied) == (True, True)
        assert result.changes[0].startswith("Plan: 22 to import, 1 to add")
        assert [c[0] for c in fake_tf.calls] == ["plan", "apply"]

    def test_a_dry_run_plans_but_does_not_apply(
        self, fake_tf, planned_dir, aws_credentials
    ):
        upload_rule(self.events, "DISABLED")
        fake_tf.plan_json = clean_adoption_plan(build())
        result = migration().apply(str(planned_dir))
        assert (result.ok, result.applied) == (True, False)
        assert [c[0] for c in fake_tf.calls] == ["plan"]


# ---------------------------------------------------------------------------
# detach and the old destroy plan
# ---------------------------------------------------------------------------


class TestDetach:
    def test_refused_until_the_new_state_holds_the_adopted_resources(
        self, fake_tf, planned_dir, tmp_path
    ):
        old = tmp_path / "old"
        old.mkdir()
        fake_tf.state_lists[old.resolve()] = SPIKE_DETACHED
        result = migration().detach(str(old), str(planned_dir), apply=True)
        assert result.ok is False and result.findings
        assert not [c for c in fake_tf.calls if c[0] == "state rm"]

    def test_apply_backs_up_the_old_state_then_removes_the_addresses(
        self, fake_tf, planned_dir, tmp_path
    ):
        old = (tmp_path / "old").resolve()
        old.mkdir()
        plan = build()
        fake_tf.state_lists[planned_dir] = [r.new_address for r in plan.imports]
        fake_tf.state_lists[old] = plan.detach_addresses[:-1] + [
            "aws_lambda_function.other"
        ]
        fake_tf.pulled[old] = {"version": 4, "serial": 9}
        result = migration().detach(str(old), str(planned_dir), apply=True)
        assert result.ok is True
        removed = next(c for c in fake_tf.calls if c[0] == "state rm")
        assert removed[1] == old and list(removed[2]) == plan.detach_addresses[:-1]
        assert json.loads(Path(result.backup_path).read_text())["serial"] == 9
        assert result.notes == [
            f"already absent from the old state: {plan.detach_addresses[-1]}"
        ]
        assert "state push -force" in result.back_out[0]

    def test_a_dry_run_removes_nothing(self, fake_tf, planned_dir, tmp_path):
        old = (tmp_path / "old").resolve()
        old.mkdir()
        plan = build()
        fake_tf.state_lists[planned_dir] = [r.new_address for r in plan.imports]
        fake_tf.state_lists[old] = plan.detach_addresses
        result = migration().detach(str(old), str(planned_dir))
        assert (
            len(result.changes) == len(plan.detach_addresses)
            and result.applied is False
        )
        assert not [c for c in fake_tf.calls if c[0] == "state rm"]


def deletion(address, tf_type, **before):
    return {
        "address": address,
        "type": tf_type,
        "change": {"actions": ["delete"], "before": before},
    }


class TestDestroyPlanCheck:
    def test_deleting_a_kept_bucket_or_its_setting_is_a_finding(self):
        plan = build()
        findings = tm.check_destroy_plan(
            {
                "resource_changes": [
                    deletion(
                        "aws_s3_bucket.input_bucket",
                        "aws_s3_bucket",
                        bucket="oldidp-input-i2pfi4o2",
                        id="oldidp-input-i2pfi4o2",
                    ),
                    deletion(
                        "aws_s3_bucket_policy.p",
                        "aws_s3_bucket_policy",
                        bucket="oldidp-output-i2pfi4o2",
                    ),
                    deletion(
                        "aws_kms_key.k",
                        "aws_kms_key",
                        key_id="1234abcd-12ab-34cd-56ef-1234567890ab",
                    ),
                    deletion(
                        "aws_cognito_user.u",
                        "aws_cognito_user",
                        user_pool_id=POOL,
                        username="x",
                    ),
                ]
            },
            plan,
        )
        assert len(findings) == 4

    def test_the_old_deployments_own_resources_may_go(self):
        plan = build()
        findings = tm.check_destroy_plan(
            {
                "resource_changes": [
                    deletion(
                        "aws_cognito_user_pool_client.c",
                        "aws_cognito_user_pool_client",
                        user_pool_id=POOL,
                        id="c",
                    ),
                    deletion(
                        "aws_wafv2_web_acl_association.cognito",
                        "aws_wafv2_web_acl_association",
                        resource_arn="arn",
                    ),
                    deletion(
                        "aws_s3_bucket.assets",
                        "aws_s3_bucket",
                        bucket="oldidp-bf1fipnn-assets-dyabdfvp",
                    ),
                    deletion(
                        "aws_lambda_function.f",
                        "aws_lambda_function",
                        function_name="oldidp-input-i2pfi4o2",
                    ),
                ]
            },
            plan,
        )
        assert findings == []


# ---------------------------------------------------------------------------
# the plan operation
# ---------------------------------------------------------------------------


class TestPlanOperation:
    @pytest.fixture
    def wired(self, monkeypatch, fake_tf, tmp_path):
        manifest = json.loads((FIXTURES / "module" / "idp-terraform.json").read_text())
        monkeypatch.setattr(tf, "load_module_manifest", lambda url, sha=None: manifest)
        monkeypatch.setattr(tm, "module_dir_from_root", lambda d: FIXTURES / "module")
        monkeypatch.setattr(
            "idp_sdk.operations.stack.StackOperation._describe_stack",
            lambda self, d, n: None,
        )
        monkeypatch.setattr(
            "idp_sdk.operations.terraform.GenaiIdpTerraformMigration._user_lookup",
            lambda self, p, u: tm.UserLookup(exists=True, groups=["Admin"]),
        )
        state = tmp_path / "old.tfstate"
        state.write_text((FIXTURES / "old_state.tfstate").read_text())
        return state

    def test_writes_the_adoption_files(self, wired, tmp_path, fake_tf):
        directory = tmp_path / "new"
        result = migration().plan(
            str(wired),
            ADMIN,
            {"LogRetentionDays": "90"},
            module_url="https://example.com/m.zip",
            terraform_dir=str(directory),
        )
        adopt = (directory / tm.ADOPT_FILE).read_text()
        assert (
            adopt.startswith(tm.ADOPT_MARKER)
            and adopt.count("import {") == result.import_count == 22
        )
        tfvars = tf.parse_tfvars((directory / "terraform.tfvars").read_text())
        assert tfvars["reporting_bucket_name"] == "oldidp-reporting-i2pfi4o2"
        assert tfvars["log_retention_days"] == 90
        saved = tm.MigrationPlan.from_json(
            json.loads((directory / tm.MIGRATION_DIR / tm.PLAN_FILE).read_text())
        )
        assert saved.detach_addresses == result.detach_addresses
        assert (
            directory / tm.MIGRATION_DIR / tm.DETACH_FILE
        ).read_text().splitlines() == result.detach_addresses
        assert result.detach_commands[0].startswith("terraform state pull > ")
        assert ("init", directory.resolve()) in fake_tf.calls

    def test_a_refusal_writes_no_adoption_files(self, wired, tmp_path):
        state = json.loads(wired.read_text())
        add_resource(state, "aws_dynamodb_table", "mystery", {"name": "data"})
        wired.write_text(json.dumps(state))
        directory = tmp_path / "new"
        with pytest.raises(IDPConfigurationError, match="mystery"):
            migration().plan(
                str(wired),
                ADMIN,
                module_url="https://example.com/m.zip",
                terraform_dir=str(directory),
            )
        assert not (directory / tm.ADOPT_FILE).exists()
        assert not (directory / tm.MIGRATION_DIR).exists()

    def test_a_foreign_adopt_tf_is_not_overwritten(self, wired, tmp_path):
        directory = tmp_path / "new"
        directory.mkdir()
        (directory / tm.ADOPT_FILE).write_text("import {}\n")
        with pytest.raises(IDPConfigurationError, match="not written by this command"):
            migration().plan(
                str(wired),
                ADMIN,
                module_url="https://example.com/m.zip",
                terraform_dir=str(directory),
            )
        assert (directory / tm.ADOPT_FILE).read_text() == "import {}\n"


class TestUserLookup:
    @mock_aws
    def test_reports_existence_and_groups(self, aws_credentials):
        cognito = boto3.client("cognito-idp", region_name=REGION)
        pool = cognito.create_user_pool(PoolName="p")["UserPool"]["Id"]
        cognito.admin_create_user(UserPoolId=pool, Username=ADMIN)
        cognito.create_group(UserPoolId=pool, GroupName="Admin")
        cognito.admin_add_user_to_group(
            UserPoolId=pool, Username=ADMIN, GroupName="Admin"
        )
        op = migration()
        assert op._user_lookup(pool, ADMIN) == tm.UserLookup(
            exists=True, groups=["Admin"]
        )
        assert op._user_lookup(pool, "nobody@example.com") == tm.UserLookup(
            exists=False
        )
