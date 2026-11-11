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
KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
# Built, not written out, so no tool reads it as a real ARN.
ROLE_ARN = "arn:aws:iam::" + "1" * 12 + ":role/idp-terraform-deployer"

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


def kept_bucket_names(plan):
    """Every bucket the plan keeps and checks: adopted, and named by parameter."""
    return [b["bucket"] for b in plan.adopted_buckets] + [
        r.physical_id for r in plan.resources if r.verdict == tm.ADOPT_BY_PARAMETER
    ]


def module_copy(tmp_path, append="", replace=()):
    """A copy of the fixture module with ``append`` added to main.tf and each
    (old, new) in ``replace`` applied to it."""
    root = tmp_path / "module-copy"
    shutil.copytree(FIXTURES / "module", root)
    text = (root / "main.tf").read_text()
    for old, new in replace:
        assert old in text, old
        text = text.replace(old, new)
    (root / "main.tf").write_text(text + append)
    return tm.ModuleSource.read(root)


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

    def test_the_plan_records_the_old_states_lineage(self):
        plan = build()
        assert (plan.old_lineage, plan.old_serial) == (
            "00000000-0000-0000-0000-00000000abcd",
            42,
        )

    def test_a_state_without_a_lineage_is_refused(self):
        state = old_state()
        del state["lineage"]
        with pytest.raises(IDPConfigurationError, match="no lineage"):
            build(state)


def verdict_of(state, address):
    return {r.old_address: r for r in build(state).resources}[address]


class TestTypeClassification:
    """Every old-state type is classified; an unclassified one is refused."""

    def test_every_type_in_the_live_state_is_classified(self):
        handled = (
            tm.STATEFUL_TYPES
            | tm.RECREATE_SAFE_TYPES
            | tm.POOL_SETTING_TYPES
            | tm.POOL_MEMBER_TYPES
            | tm.BDA_MEMBER_TYPES
            | set(tm.TABLE_SETTING_TYPES)
            | set(tm.KEY_SETTING_TYPES)
            | {"aws_kms_alias", "aws_lambda_invocation"}
        )
        types = {r["type"] for r in old_state()["resources"]}
        assert [
            t
            for t in sorted(types)
            if t not in handled and not tm._is_bucket_setting(t)
        ] == []

    def test_a_type_in_neither_list_is_refused_naming_it(self):
        state = add_resource(old_state(), "aws_mystery_store", "x", {"id": "m"})
        with pytest.raises(
            IDPConfigurationError, match=r"resource type aws_mystery_store is not one"
        ):
            build(state)

    @pytest.mark.parametrize(
        "tf_type,attrs",
        [
            (
                "aws_dynamodb_table_replica",
                {"global_table_arn": "t", "id": "idp-tracking-table-mr68w2wl:eu"},
            ),
            ("aws_backup_vault", {"name": "vault", "id": "vault"}),
            ("aws_kms_replica_key", {"key_id": "mrk-1", "id": "mrk-1"}),
        ],
    )
    def test_a_stateful_type_with_no_verdict_is_refused(self, tf_type, attrs):
        state = add_resource(old_state(), tf_type, "probe", attrs)
        with pytest.raises(IDPConfigurationError, match=f"{tf_type}.probe.*holds data"):
            build(state)

    @pytest.mark.parametrize(
        "tf_type",
        ["aws_vpc", "aws_cloudformation_stack", "aws_security_group"],
    )
    def test_types_needing_a_decision_are_refused(self, tf_type):
        state = add_resource(old_state(), tf_type, "probe", {"id": "p"})
        with pytest.raises(IDPConfigurationError, match=f"resource type {tf_type}"):
            build(state)

    def test_a_ui_customization_on_the_adopted_pool_is_detached(self):
        state = add_resource(
            old_state(),
            "aws_cognito_user_pool_ui_customization",
            "ui",
            {"user_pool_id": POOL, "client_id": "ALL", "id": POOL},
        )
        ui = verdict_of(state, "aws_cognito_user_pool_ui_customization.ui")
        assert (ui.verdict, ui.detach) == (tm.SETTING, True)
        assert (
            "aws_cognito_user_pool_ui_customization.ui" in build(state).detach_addresses
        )

    def test_a_pool_setting_that_names_no_pool_is_refused(self):
        state = add_resource(
            old_state(), "aws_cognito_user_pool_ui_customization", "ui", {"id": "x"}
        )
        with pytest.raises(IDPConfigurationError, match="which user pool"):
            build(state)

    @pytest.mark.parametrize("scope", ["CRUD", None])
    def test_an_invocation_that_runs_on_destroy_is_detached(self, scope):
        attrs = {"function_name": "oldidp-bda-ocr-project", "id": "f,$LATEST,x"}
        if scope:
            attrs["lifecycle_scope"] = scope
        state = add_resource(old_state(), "aws_lambda_invocation", "ocr", attrs)
        inv = verdict_of(state, "aws_lambda_invocation.ocr")
        assert (inv.verdict, inv.detach) == (tm.KEEP_UNMANAGED, True)
        assert "delete action" in inv.note

    def test_a_create_only_invocation_is_destroyed_with_the_old_deployment(self):
        seeder = next(
            r for r in build().resources if r.old_type == "aws_lambda_invocation"
        )
        assert (seeder.verdict, seeder.detach) == (tm.RECREATE, False)

    def test_the_adopted_keys_alias_is_detached(self):
        alias = verdict_of(old_state(), "aws_kms_alias.encryption_key")
        assert (alias.verdict, alias.detach) == (tm.KEEP_UNMANAGED, True)

    def test_an_alias_of_another_key_is_destroyed(self):
        state = add_resource(
            old_state(),
            "aws_kms_alias",
            "other",
            {"name": "alias/o", "target_key_id": "arn:aws:kms:r:a:key/other"},
        )
        assert verdict_of(state, "aws_kms_alias.other").detach is False

    def test_an_alias_whose_key_is_not_recorded_is_refused(self):
        state = add_resource(old_state(), "aws_kms_alias", "other", {"name": "alias/o"})
        with pytest.raises(IDPConfigurationError, match="which KMS key"):
            build(state)

    def test_a_bucket_setting_that_names_no_bucket_is_refused(self):
        state = add_resource(
            old_state(), "aws_s3_bucket_policy", "orphan", {"id": "x", "policy": "{}"}
        )
        with pytest.raises(IDPConfigurationError, match="which bucket"):
            build(state)

    def test_a_blueprint_is_kept_with_the_bda_project(self):
        state = add_resource(
            old_state(), "awscc_bedrock_blueprint", "w2", {"blueprint_arn": "b"}
        )
        bp = verdict_of(state, "awscc_bedrock_blueprint.w2")
        assert (bp.verdict, bp.detach) == (tm.KEEP_UNMANAGED, True)


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------


class TestRetention:
    @pytest.fixture(autouse=True)
    def _aws(self, aws_credentials):
        with mock_aws():
            self.s3 = boto3.client("s3", region_name=REGION)
            self.plan = build()
            for name in kept_bucket_names(self.plan):
                self.s3.create_bucket(
                    Bucket=name,
                    CreateBucketConfiguration={"LocationConstraint": REGION},
                )
            self.s3.put_object(Bucket="oldidp-input-i2pfi4o2", Key="old.pdf", Body=b"x")
            yield

    def check(self, values=None, accept=(), days_later=400, mod=None):
        now = datetime.now(timezone.utc) + timedelta(days=days_later)
        return tm.check_retention(
            self.plan,
            mod or module(),
            values or {},
            self.s3,
            accept_expiry=accept,
            now=now,
        )

    def row(self, rows, kind):
        return next(r for r in rows if r["kind"] == kind)

    def test_an_object_older_than_the_expiry_is_a_finding(self):
        rows, findings, _ = self.check()
        assert (
            len(findings) == 1 and "input_bucket oldidp-input-i2pfi4o2" in findings[0]
        )
        assert "data_retention_in_days" in findings[0]
        row = self.row(rows, "input_bucket")
        assert (row["expiryDays"], row["ok"]) == (365, False)

    def test_a_longer_retention_parameter_clears_it(self):
        _, findings, _ = self.check(values={"data_retention_in_days": 500})
        assert findings == []

    def test_younger_objects_are_fine(self):
        _, findings, _ = self.check(days_later=10)
        assert findings == []

    def test_accept_expiry_clears_one_kind(self):
        rows, findings, _ = self.check(accept=("input_bucket",))
        assert findings == [] and "accepted" in self.row(rows, "input_bucket")["note"]

    def test_a_fixed_rule_says_so(self):
        self.s3.put_object(Bucket="oldidp-bf1fipnn-logs", Key="log", Body=b"x")
        _, findings, _ = self.check(values={"data_retention_in_days": 500})
        assert (
            len(findings) == 1
            and "fixed in the module" in findings[0]
            and "180" in findings[0]
        )

    def test_the_working_bucket_uses_log_retention(self):
        rows, _, _ = self.check(values={"log_retention_days": 14})
        assert self.row(rows, "working_bucket")["expiryDays"] == 14

    def test_the_result_says_what_was_not_checked(self):
        rows, _, notes = self.check(days_later=10)
        assert notes == [f"Not checked: {line}" for line in tm.RETENTION_LIMITS]
        row = self.row(rows, "input_bucket")
        assert row["checked"][0].startswith("current objects (ListObjectsV2)")
        assert row["checked"][1] == (
            "noncurrent versions: none, the bucket has never been versioned"
        )

    def test_an_expiry_it_cannot_evaluate_is_a_finding(self, tmp_path):
        mod = module_copy(
            tmp_path,
            replace=[
                (
                    "bucket = aws_s3_bucket.output_bucket.id\n  rule {\n"
                    "    expiration {\n      days = var.data_retention_in_days",
                    "bucket = aws_s3_bucket.output_bucket.id\n  rule {\n"
                    "    expiration {\n      days = local.output_days",
                )
            ],
        )
        rows, findings, _ = self.check(days_later=10, mod=mod)
        assert len(findings) == 1
        assert "output_bucket oldidp-output-i2pfi4o2" in findings[0]
        assert "`local.output_days`" in findings[0]
        assert "--accept-expiry output_bucket" in findings[0]
        assert self.row(rows, "output_bucket")["ok"] is False
        _, findings, _ = self.check(days_later=10, mod=mod, accept=("output_bucket",))
        assert findings == []

    def test_an_expiration_with_no_days_is_a_finding(self, tmp_path):
        mod = module_copy(
            tmp_path,
            replace=[
                (
                    "bucket = aws_s3_bucket.output_bucket.id\n  rule {\n"
                    "    expiration {\n      days = var.data_retention_in_days",
                    "bucket = aws_s3_bucket.output_bucket.id\n  rule {\n"
                    '    expiration {\n      date = "2027-01-01T00:00:00Z"',
                )
            ],
        )
        _, findings, _ = self.check(days_later=10, mod=mod)
        assert len(findings) == 1 and "no `days`" in findings[0]

    def test_a_lifecycle_whose_bucket_it_cannot_resolve_is_a_finding(self, tmp_path):
        mod = module_copy(
            tmp_path,
            append=(
                'resource "aws_s3_bucket_lifecycle_configuration" "mystery" {\n'
                "  bucket = local.some_bucket\n"
                "  rule {\n    expiration {\n      days = 1\n    }\n  }\n}\n"
            ),
        )
        _, findings, _ = self.check(days_later=10, mod=mod)
        assert findings == [
            "aws_s3_bucket_lifecycle_configuration.mystery: its bucket is "
            "`local.some_bucket`, which this command cannot resolve to a bucket, so "
            "it may put an expiry rule on a kept one; nothing about it was checked"
        ]

    def test_a_submodule_rule_on_an_adopted_bucket_counts(self, tmp_path):
        mod_root = tmp_path / "module-copy"
        shutil.copytree(FIXTURES / "module", mod_root)
        main = mod_root / "main.tf"
        main.write_text(
            main.read_text().replace(
                'source = "./modules/apiresolverstack"',
                'source = "./modules/apiresolverstack"\n'
                "  input_bucket_name = aws_s3_bucket.input_bucket.id",
            )
        )
        sub = mod_root / "modules" / "apiresolverstack" / "main.tf"
        sub.write_text(
            sub.read_text()
            + 'resource "aws_s3_bucket_lifecycle_configuration" "nested" {\n'
            "  bucket = var.input_bucket_name\n"
            "  rule {\n    expiration {\n      days = 5\n    }\n  }\n}\n"
        )
        rows, findings, _ = self.check(
            days_later=10, mod=tm.ModuleSource.read(mod_root)
        )
        assert self.row(rows, "input_bucket")["expiryDays"] == 5
        assert len(findings) == 1 and "expires objects after 5 days" in findings[0]

    def test_a_module_call_it_cannot_read_is_a_finding(self, tmp_path):
        mod = module_copy(
            tmp_path,
            append='module "remote" {\n  source = "example/remote/aws"\n}\n',
        )
        _, findings, _ = self.check(days_later=10, mod=mod)
        assert findings == [
            "module call remote has no local source this command can read, so a "
            "lifecycle rule inside it could target a kept bucket unseen"
        ]

    def test_an_inline_lifecycle_rule_is_a_finding(self, tmp_path):
        mod = module_copy(
            tmp_path,
            replace=[
                (
                    'resource "aws_s3_bucket" "output_bucket" {\n',
                    'resource "aws_s3_bucket" "output_bucket" {\n'
                    "  lifecycle_rule {\n    enabled = true\n  }\n",
                )
            ],
        )
        _, findings, _ = self.check(days_later=10, mod=mod)
        assert len(findings) == 1 and "inline `lifecycle_rule`" in findings[0]

    def test_a_bucket_kept_by_parameter_is_checked_too(self, tmp_path):
        mod = module_copy(
            tmp_path,
            append=(
                'resource "aws_s3_bucket_lifecycle_configuration" "by_name" {\n'
                "  bucket = var.reporting_bucket_name\n"
                "  rule {\n    expiration {\n      days = 3\n    }\n  }\n}\n"
            ),
        )
        self.s3.put_object(Bucket="oldidp-reporting-i2pfi4o2", Key="r", Body=b"x")
        rows, findings, _ = self.check(
            days_later=10,
            mod=mod,
            values={"reporting_bucket_name": "oldidp-reporting-i2pfi4o2"},
        )
        assert self.row(rows, "reporting_bucket")["expiryDays"] == 3
        assert len(findings) == 1 and "reporting_bucket" in findings[0]

    def test_an_old_noncurrent_version_is_a_finding(self, tmp_path):
        mod = module_copy(
            tmp_path,
            replace=[
                (
                    "bucket = aws_s3_bucket.output_bucket.id\n  rule {\n"
                    "    expiration {\n      days = var.data_retention_in_days\n"
                    "    }\n    noncurrent_version_expiration {\n"
                    "      noncurrent_days = var.data_retention_in_days",
                    "bucket = aws_s3_bucket.output_bucket.id\n  rule {\n"
                    "    expiration {\n      days = var.data_retention_in_days\n"
                    "    }\n    noncurrent_version_expiration {\n"
                    "      noncurrent_days = 7",
                )
            ],
        )
        bucket = "oldidp-output-i2pfi4o2"
        self.s3.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
        self.s3.put_object(Bucket=bucket, Key="r.json", Body=b"1")
        self.s3.put_object(Bucket=bucket, Key="r.json", Body=b"2")
        rows, findings, _ = self.check(days_later=10, mod=mod)
        row = self.row(rows, "output_bucket")
        # S3 dates to the second, so ten days and a fraction rounds up
        assert row["noncurrentExpiryDays"] == 7
        assert row["oldestNoncurrentAgeDays"] in (10, 11)
        assert len(findings) == 1 and "noncurrent object version" in findings[0]
        assert "versioning Enabled" in row["checked"][1]
        # the current objects alone are fine (10 days, 365-day rule)
        assert row["expiryDays"] == 365 and row["oldestObjectAgeDays"] in (10, 11)

    def test_a_delete_marker_dates_the_version_it_hides(self):
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket="versions-probe",
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        s3.put_bucket_versioning(
            Bucket="versions-probe", VersioningConfiguration={"Status": "Enabled"}
        )
        s3.put_object(Bucket="versions-probe", Key="k", Body=b"1")
        assert tm.oldest_noncurrent(s3, "versions-probe") is None
        s3.delete_object(Bucket="versions-probe", Key="k")
        marker = s3.list_object_versions(Bucket="versions-probe")["DeleteMarkers"][0]
        assert tm.oldest_noncurrent(s3, "versions-probe") == marker["LastModified"]


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


def write_planned_dir(directory, plan=None, role_arn=None):
    mdir = directory / tm.MIGRATION_DIR
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / tm.PLAN_FILE).write_text(json.dumps((plan or build()).to_json()))
    (directory / "main.tf").write_text(tf.render_provider_blocks(REGION, role_arn))


@pytest.fixture
def planned_dir(tmp_path):
    """A new deployment's directory as the `plan` phase leaves it."""
    directory = tmp_path / "idp-terraform" / "newidp"
    write_planned_dir(directory)
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
        assert any("in flight" in n and "waiting" in n for n in result.notes)

    @pytest.mark.parametrize(
        "status",
        ["REJECTED", "REDACTED_SUPERSEDED", "QUARANTINED", "MALWARE_SCAN_FAILED"],
    )
    def test_every_terminal_status_is_finished(self, planned_dir, status):
        doc(self.ddb, "a.pdf", status)
        result = migration().freeze(str(planned_dir), apply=True)
        assert result.ok is True and result.findings == []

    def test_a_document_awaiting_review_is_reported_on_its_own(self, planned_dir):
        doc(self.ddb, "r.pdf", "HITL_IN_PROGRESS")
        result = migration().freeze(str(planned_dir), apply=True)
        assert result.ok is False
        assert result.details["inReview"] == [
            {"key": "r.pdf", "status": "HITL_IN_PROGRESS"}
        ]
        assert result.details["inFlightCount"] == 0
        assert "waiting for human review" in result.findings[0]

    def test_an_ignored_document_does_not_block(self, planned_dir):
        doc(self.ddb, "a.pdf", "EXTRACTING")
        doc(self.ddb, "r.pdf", "HITL_IN_PROGRESS")
        result = migration().freeze(
            str(planned_dir), apply=True, ignore_documents=["a.pdf", "r.pdf"]
        )
        assert result.ok is True
        assert {d["key"] for d in result.details["ignored"]} == {"a.pdf", "r.pdf"}

    def test_no_upload_rule_in_the_old_state_is_a_finding(self, tmp_path):
        plan = build()
        plan.upload_rules = []
        directory = tmp_path / "d"
        write_planned_dir(directory, plan)
        result = migration().freeze(str(directory), apply=True)
        assert result.findings == [tm.NO_UPLOAD_RULE]


class PagedTable:
    """A scan that answers in pages of one item, as a large table does."""

    def __init__(self, items):
        self.items = items
        self.calls = 0

    def scan(self, **kwargs):
        self.calls += 1
        i = int((kwargs.get("ExclusiveStartKey") or {}).get("i", 0))
        resp = {"Items": self.items[i : i + 1]}
        if i + 1 < len(self.items):
            resp["LastEvaluatedKey"] = {"i": i + 1}
        return resp


def test_the_document_scan_reads_every_page():
    items = [
        {"PK": {"S": f"doc#{n}.pdf"}, "SK": {"S": "none"}, "ObjectStatus": {"S": "OCR"}}
        for n in range(1500)
    ]
    table = PagedTable(items)
    scan = tm.scan_documents("t", table)
    assert len(scan.in_flight) == 1500 and scan.scanned == 1500
    assert table.calls == 1500


def test_a_finished_documents_run_rows_are_not_in_flight():
    """Upstream writes a ``run#...`` row per processing run in the document's
    partition, with no ObjectStatus. Measured on a live upstream deployment: two
    COMPLETED documents were reported in flight with status "" through them."""
    items = [
        {
            "PK": {"S": "doc#seed/a.pdf"},
            "SK": {"S": "none"},
            "ObjectStatus": {"S": "COMPLETED"},
        },
        {"PK": {"S": "doc#seed/a.pdf"}, "SK": {"S": "run#20261111T013000Z-a.pdf-1"}},
        {
            "PK": {"S": "doc#seed/b.pdf"},
            "SK": {"S": "none"},
            "ObjectStatus": {"S": "OCR"},
        },
        {"PK": {"S": "doc#seed/b.pdf"}, "SK": {"S": "run#20261111T013100Z-b.pdf-2"}},
    ]
    scan = tm.scan_documents("t", PagedTable(items))
    assert [r["key"] for r in scan.in_flight] == ["seed/b.pdf"]
    assert scan.scanned == 2


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
                dict(
                    config_row("Config#default", LatestRevision=3, PublishedRevision=3),
                    # Upstream stores each configuration body compressed, as binary.
                    _compressed_config={"B": b"\x1f\x8b\x08\x00compressed"},
                ),
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
        assert self.row("Config#default")["_compressed_config"] == {
            "B": b"\x1f\x8b\x08\x00compressed"
        }

    def test_restore_refuses_a_backup_of_another_table(
        self, planned_dir, aws_credentials, tmp_path
    ):
        other = tmp_path / "other-backup.json"
        other.write_text(
            json.dumps({"TableName": "someone-elses-table", "Items": [config_row("x")]})
        )
        with pytest.raises(IDPConfigurationError, match="someone-elses-table"):
            migration().fix_config(str(planned_dir), apply=True, restore=str(other))
        assert "x" not in [
            i["Configuration"]["S"]
            for i in tm.scan_all(self.ddb, "idp-configuration-table-mr68w2wl")
        ]

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

    @pytest.mark.parametrize(
        "pool_bounds,module_bounds",
        [
            ([{"max_length": "256", "min_length": "0"}], []),
            ([{"max_length": "2048", "min_length": "5"}], []),
            (
                [{"max_length": "2048", "min_length": "0"}],
                [{"max_length": "100", "min_length": None}],
            ),
        ],
    )
    def test_a_real_bound_difference_is_a_conflict(self, pool_bounds, module_bounds):
        rc = spike_pool_change()
        before = json.loads(json.dumps(rc["change"]["before"]["schema"]))
        after = json.loads(json.dumps(rc["change"]["after"]["schema"]))
        before[0]["string_attribute_constraints"] = pool_bounds
        after[0]["string_attribute_constraints"] = module_bounds
        _, changes, conflicts = tm.normalise_pool_schema(before, after)
        assert len(conflicts) == 1 and "string_attribute_constraints" in conflicts[0]
        assert not any(c.startswith("rewrite 'email'") for c in changes)

    @pytest.mark.parametrize(
        "pool_bounds,module_bounds",
        [
            ([{"max_length": "2048", "min_length": "0"}], None),
            ([{"max_length": "256", "min_length": "0"}], [{"max_length": "256"}]),
            (
                [{"max_length": "256", "min_length": ""}],
                [{"max_length": "256", "min_length": None}],
            ),
        ],
    )
    def test_null_empty_and_default_bounds_are_only_normalised(
        self, pool_bounds, module_bounds
    ):
        rc = spike_pool_change()
        before = json.loads(json.dumps(rc["change"]["before"]["schema"]))
        after = json.loads(json.dumps(rc["change"]["after"]["schema"]))
        before[0]["string_attribute_constraints"] = pool_bounds
        after[0]["string_attribute_constraints"] = module_bounds
        _, _, conflicts = tm.normalise_pool_schema(before, after)
        assert conflicts == []

    def test_a_number_bound_difference_is_a_conflict(self):
        before = [
            {
                "name": "custom:n",
                "attribute_data_type": "Number",
                "mutable": True,
                "required": False,
                "number_attribute_constraints": [{"min_value": "1", "max_value": "9"}],
            }
        ]
        after = [dict(before[0], number_attribute_constraints=[])]
        _, _, conflicts = tm.normalise_pool_schema(before, after)
        assert len(conflicts) == 1 and "min_value" in conflicts[0]

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


# What each adopted type's before and after carry in a clean plan: every attribute
# a REFUSED_CHANGES row reads, at a value no row refuses.
SAFE_ATTRIBUTES = {
    "aws_s3_bucket": {"force_destroy": False},
    "aws_dynamodb_table": {
        "point_in_time_recovery": [{"enabled": True}],
        "deletion_protection_enabled": False,
        "global_secondary_index": [{"name": "gsi1"}],
        "local_secondary_index": [],
        "stream_enabled": False,
        "ttl": [{"enabled": False, "attribute_name": ""}],
        "server_side_encryption": [{"enabled": True, "kms_key_arn": "k"}],
    },
    "awscc_kms_key": {
        "enable_key_rotation": True,
        "enabled": True,
        "pending_window_in_days": None,
    },
    "aws_cognito_user_pool": {
        "deletion_protection": "ACTIVE",
        "mfa_configuration": "OFF",
        "lambda_config": [{"pre_token_generation": "arn-old"}],
    },
    "aws_cognito_user": {"enabled": True},
}

# The settings resources a clean plan creates on each adopted bucket.
SAFE_SETTINGS = {
    "aws_s3_bucket_versioning": {"versioning_configuration": [{"status": "Enabled"}]},
    "aws_s3_bucket_server_side_encryption_configuration": {
        "rule": [
            {
                "apply_server_side_encryption_by_default": [
                    {"sse_algorithm": "aws:kms", "kms_master_key_id": "arn-of-key"}
                ]
            }
        ]
    },
    "aws_s3_bucket_public_access_block": {
        "block_public_acls": True,
        "block_public_policy": True,
        "ignore_public_acls": True,
        "restrict_public_buckets": True,
    },
}


def clean_adoption_plan(plan):
    """A plan JSON in which every planned import is imported in place."""
    rcs = []
    for r in plan.imports:
        tf_type = tm._resource_type(r.new_address)
        safe = SAFE_ATTRIBUTES.get(tf_type, {})
        change = {
            "actions": ["update"],
            "before": json.loads(json.dumps(safe)),
            "after": json.loads(json.dumps(safe)),
        }
        if r.import_in_adopt_tf:
            change["importing"] = {"id": r.import_id}
        else:
            normal = [
                tm._module_created_form(i)
                for i in spike_pool_change()["change"]["after"]["schema"][:1]
            ]
            change["before"]["schema"] = normal
            change["after"]["schema"] = spike_pool_change()["change"]["after"]["schema"]
            change["after"]["lambda_config"] = [{"pre_token_generation": None}]
            change["after_unknown"] = {
                "lambda_config": [{"pre_token_generation": True}]
            }
        rcs.append({"address": r.new_address, "type": tf_type, "change": change})
    for b in plan.adopted_buckets:
        for tf_type, attrs in SAFE_SETTINGS.items():
            rcs.append(
                {
                    "address": f"module.idp.{tf_type}.{b['resource']}",
                    "type": tf_type,
                    "change": {
                        "actions": ["create"],
                        "before": None,
                        "after": {
                            "bucket": b["bucket"],
                            **json.loads(json.dumps(attrs)),
                        },
                    },
                }
            )
    rcs.append(
        {
            "address": "module.idp.aws_sqs_queue.q",
            "type": "aws_sqs_queue",
            "change": {"actions": ["create"]},
        }
    )
    return {"resource_changes": rcs}


def change_at(pj, suffix):
    return next(rc for rc in pj["resource_changes"] if rc["address"].endswith(suffix))[
        "change"
    ]


class TestAdoptionPlanCheck:
    def test_the_live_single_pass_plan_is_refused_for_the_pool(self):
        spike = json.loads((FIXTURES / "adopt_plan.json").read_text())
        counts, findings, _ = tm.check_adoption_plan(spike, build())
        assert counts["import"] == 17 and counts["destroy"] == 0
        assert any("run `adopt-user-pool` first" in f for f in findings)
        assert any("modify or remove existing schema items" in f for f in findings)

    def test_a_clean_plan_passes(self):
        plan = build()
        counts, findings, review = tm.check_adoption_plan(
            clean_adoption_plan(plan), plan
        )
        assert findings == []
        settings = len(plan.adopted_buckets) * len(SAFE_SETTINGS)
        assert counts == {
            "import": len(plan.imports) - 1,
            "add": 1 + settings,
            "change": len(plan.imports),
            "destroy": 0,
        }
        # every setting created on a kept bucket is listed for the operator
        assert sum("created on kept bucket" in r for r in review) == settings

    def test_an_allowed_in_place_change_is_listed_for_review(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        change_at(pj, "tracking_table")["after"]["billing_mode"] = "PROVISIONED"
        _, findings, review = tm.check_adoption_plan(pj, plan)
        assert findings == []
        assert (
            "module.idp.aws_dynamodb_table.tracking_table: changes billing_mode"
            in review
        )

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
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert findings == [
            "module.idp.aws_x.y would be destroyed; an adoption destroys nothing, so stop and find out why"
        ]

    def test_a_forget_is_refused(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        change_at(pj, "users_table")["actions"] = ["forget"]
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert any("users_table would be forgotten" in f for f in findings)

    def test_a_replaced_adopted_resource_is_refused(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        change_at(pj, "tracking_table")["actions"] = ["delete", "create"]
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert any("tracking_table would be replaced" in f for f in findings)
        assert any("tracking_table plans ['delete', 'create']" in f for f in findings)

    def test_a_create_instead_of_an_import_is_refused(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        change = change_at(pj, "input_bucket")
        del change["importing"]
        change["actions"] = ["create"]
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert any(
            "input_bucket is in the plan but not as an import" in f for f in findings
        )

    def test_a_wrong_import_id_is_refused(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        change_at(pj, "users_table")["importing"]["id"] = "other"
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert any("imports 'other'" in f for f in findings)

    # One case per REFUSED_CHANGES row: (address suffix, attribute, before, after,
    # rule). `before` None means the case edits only `after`.
    REFUSAL_CASES = [
        (
            "tracking_table",
            "point_in_time_recovery",
            None,
            [{"enabled": False}],
            "pitr-off",
        ),
        (
            "tracking_table",
            "deletion_protection_enabled",
            True,
            False,
            "table-deletion-protection-off",
        ),
        ("tracking_table", "global_secondary_index", None, [], "gsi-removed"),
        ("tracking_table", "local_secondary_index", [{"name": "l"}], [], "lsi-removed"),
        ("tracking_table", "stream_enabled", True, False, "stream-off"),
        (
            "tracking_table",
            "ttl",
            [{"enabled": True, "attribute_name": "expiresAfter"}],
            [{"enabled": True, "attribute_name": "ExpiresAfter"}],
            "ttl-changed",
        ),
        (
            "tracking_table",
            "server_side_encryption",
            None,
            [{"enabled": False, "kms_key_arn": ""}],
            "table-encryption-removed",
        ),
        (
            "aws_s3_bucket.input_bucket",
            "force_destroy",
            None,
            True,
            "bucket-force-destroy-on",
        ),
        (
            "user_pool",
            "deletion_protection",
            None,
            "INACTIVE",
            "pool-deletion-protection-off",
        ),
        ("user_pool", "mfa_configuration", "ON", "OPTIONAL", "pool-mfa-relaxed"),
        ("user_pool", "lambda_config", None, [], "pool-trigger-removed"),
        ("user_pool", "schema", None, [], "pool-schema-item-removed"),
        (
            "customer_managed_encryption_key",
            "enable_key_rotation",
            None,
            False,
            "key-rotation-off",
        ),
        ("customer_managed_encryption_key", "enabled", None, False, "key-disabled"),
        (
            "customer_managed_encryption_key",
            "pending_window_in_days",
            30,
            7,
            "key-deletion-window-shortened",
        ),
        ("aws_cognito_user.admin_user", "enabled", None, False, "user-disabled"),
    ]

    @pytest.mark.parametrize(
        "suffix,attr,before,after,rule",
        REFUSAL_CASES,
        ids=[c[4] for c in REFUSAL_CASES],
    )
    def test_a_refused_transition_on_an_adopted_resource(
        self, suffix, attr, before, after, rule
    ):
        plan = build()
        pj = clean_adoption_plan(plan)
        change = change_at(pj, suffix)
        if before is not None:
            change["before"][attr] = before
        change["after"][attr] = after
        change.pop("after_unknown", None)
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        address = next(
            rc["address"]
            for rc in pj["resource_changes"]
            if rc["address"].endswith(suffix)
        )
        flag = f"--accept-change {address}:{rule}"
        assert [f for f in findings if flag in f], findings
        # and the flag it names clears exactly that finding
        _, again, review = tm.check_adoption_plan(pj, plan, [f"{address}:{rule}"])
        assert not [f for f in again if flag in f]
        assert any(r.startswith(address) and "(accepted)" in r for r in review)

    SETTING_CASES = [
        (
            "aws_s3_bucket_versioning",
            "versioning_configuration",
            [{"status": "Suspended"}],
            "versioning-suspended",
        ),
        (
            "aws_s3_bucket_server_side_encryption_configuration",
            "rule",
            [
                {
                    "apply_server_side_encryption_by_default": [
                        {"sse_algorithm": "AES256", "kms_master_key_id": ""}
                    ]
                }
            ],
            "bucket-encryption-key-removed",
        ),
        *(
            (
                "aws_s3_bucket_public_access_block",
                attr,
                False,
                f"public-access-{attr.replace('_', '-')}-off",
            )
            for attr in (
                "block_public_acls",
                "block_public_policy",
                "ignore_public_acls",
                "restrict_public_buckets",
            )
        ),
    ]

    @pytest.mark.parametrize(
        "tf_type,attr,after,rule", SETTING_CASES, ids=[c[3] for c in SETTING_CASES]
    )
    def test_a_refused_setting_created_on_a_kept_bucket(
        self, tf_type, attr, after, rule
    ):
        plan = build()
        pj = clean_adoption_plan(plan)
        change_at(pj, f"{tf_type}.input_bucket")["after"][attr] = after
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert [
            f
            for f in findings
            if f"--accept-change module.idp.{tf_type}.input_bucket:{rule}" in f
        ], findings

    def test_an_updated_setting_on_a_kept_bucket_is_checked(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        change = change_at(pj, "aws_s3_bucket_versioning.input_bucket")
        change["actions"] = ["update"]
        change["before"] = {
            "bucket": "oldidp-input-i2pfi4o2",
            "versioning_configuration": [{"status": "Enabled"}],
        }
        change["after"]["versioning_configuration"] = [{"status": "Suspended"}]
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert any("suspends versioning" in f for f in findings)

    def test_a_setting_on_a_new_bucket_is_not_checked(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        pj["resource_changes"].append(
            {
                "address": "module.idp.aws_s3_bucket_versioning.web",
                "type": "aws_s3_bucket_versioning",
                "change": {
                    "actions": ["create"],
                    "before": None,
                    "after": {"versioning_configuration": [{"status": "Suspended"}]},
                    "after_unknown": {"bucket": True},
                },
            }
        )
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert findings == []

    def test_an_attribute_the_plan_does_not_record_is_a_finding(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        del change_at(pj, "tracking_table")["before"]["point_in_time_recovery"]
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert findings == [
            "module.idp.aws_dynamodb_table.tracking_table: the plan does not record "
            "`point_in_time_recovery`, so this command cannot see whether the apply "
            "turns point-in-time recovery off"
        ]

    def test_an_unknown_trigger_is_a_replacement_not_a_removal(self):
        plan = build()
        pj = clean_adoption_plan(plan)
        # the clean plan already replaces the pre-token trigger with an unknown ARN
        assert change_at(pj, "user_pool")["after_unknown"]["lambda_config"]
        _, findings, _ = tm.check_adoption_plan(pj, plan)
        assert findings == []

    def test_every_row_has_a_case(self):
        rules = {row.rule for row in tm.REFUSED_CHANGES}
        cased = {c[4] for c in self.REFUSAL_CASES} | {c[3] for c in self.SETTING_CASES}
        assert rules <= cased
        # one row per rule, so a case per rule is a case per row
        assert len(rules) == len(tm.REFUSED_CHANGES)


class TestApplyOperation:
    @pytest.fixture(autouse=True)
    def _aws(self, aws_credentials):
        with mock_aws():
            self.events = boto3.client("events", region_name=REGION)
            self.ddb = boto3.client("dynamodb", region_name=REGION)
            self.s3 = boto3.client("s3", region_name=REGION)
            tracking_and_config_tables(self.ddb)
            for name in kept_bucket_names(build()):
                self.s3.create_bucket(
                    Bucket=name,
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
        assert result.changes[0].startswith("Plan: 22 to import, ")
        assert [c[0] for c in fake_tf.calls] == ["plan", "apply"]
        assert result.details["review"]

    def test_a_dry_run_plans_but_does_not_apply(
        self, fake_tf, planned_dir, aws_credentials
    ):
        upload_rule(self.events, "DISABLED")
        fake_tf.plan_json = clean_adoption_plan(build())
        result = migration().apply(str(planned_dir))
        assert (result.ok, result.applied) == (True, False)
        assert [c[0] for c in fake_tf.calls] == ["plan"]

    def test_a_document_in_flight_blocks_unless_ignored(
        self, fake_tf, planned_dir, aws_credentials
    ):
        upload_rule(self.events, "DISABLED")
        doc(self.ddb, "a.pdf", "CLASSIFYING")
        doc(self.ddb, "r.pdf", "HITL_IN_PROGRESS")
        fake_tf.plan_json = clean_adoption_plan(build())
        result = migration().apply(str(planned_dir), apply=True)
        assert result.applied is False
        assert any("still being processed" in f for f in result.findings)
        assert any("waiting for human review" in f for f in result.findings)
        result = migration().apply(
            str(planned_dir), apply=True, ignore_documents=["a.pdf", "r.pdf"]
        )
        assert (result.ok, result.applied) == (True, True)

    def test_no_upload_rule_blocks_the_apply_as_it_blocks_freeze(
        self, fake_tf, tmp_path, aws_credentials
    ):
        plan = build()
        plan.upload_rules = []
        directory = tmp_path / "d"
        write_planned_dir(directory, plan)
        mod = directory / ".terraform" / "modules"
        mod.mkdir(parents=True)
        (mod / "modules.json").write_text(
            json.dumps({"Modules": [{"Key": "idp", "Dir": str(FIXTURES / "module")}]})
        )
        fake_tf.plan_json = clean_adoption_plan(plan)
        result = migration().apply(str(directory), apply=True)
        assert tm.NO_UPLOAD_RULE in result.findings and result.applied is False

    def test_a_refused_change_is_applied_only_when_accepted(
        self, fake_tf, planned_dir, aws_credentials
    ):
        upload_rule(self.events, "DISABLED")
        pj = clean_adoption_plan(build())
        change_at(pj, "tracking_table")["after"]["stream_enabled"] = False
        change_at(pj, "tracking_table")["before"]["stream_enabled"] = True
        fake_tf.plan_json = pj
        result = migration().apply(str(planned_dir), apply=True)
        assert result.applied is False
        key = "module.idp.aws_dynamodb_table.tracking_table:stream-off"
        result = migration().apply(str(planned_dir), apply=True, accept_changes=[key])
        assert result.applied is True


# ---------------------------------------------------------------------------
# detach and the old destroy plan
# ---------------------------------------------------------------------------

LINEAGE = "00000000-0000-0000-0000-00000000abcd"


class TestDetach:
    def old_dir(self, tmp_path, fake_tf, addresses, lineage=LINEAGE):
        old = (tmp_path / "old").resolve()
        old.mkdir()
        fake_tf.state_lists[old] = addresses
        fake_tf.pulled[old] = {"version": 4, "serial": 9, "lineage": lineage}
        return old

    def test_refused_until_the_new_state_holds_the_adopted_resources(
        self, fake_tf, planned_dir, tmp_path
    ):
        old = self.old_dir(tmp_path, fake_tf, SPIKE_DETACHED)
        result = migration().detach(str(old), str(planned_dir), apply=True)
        assert result.ok is False and result.findings
        assert not [c for c in fake_tf.calls if c[0] == "state rm"]

    def test_apply_backs_up_the_old_state_then_removes_the_addresses(
        self, fake_tf, planned_dir, tmp_path
    ):
        plan = build()
        fake_tf.state_lists[planned_dir] = [r.new_address for r in plan.imports]
        old = self.old_dir(
            tmp_path,
            fake_tf,
            plan.detach_addresses[:-1] + ["aws_lambda_function.other"],
        )
        result = migration().detach(str(old), str(planned_dir), apply=True)
        assert result.ok is True
        removed = next(c for c in fake_tf.calls if c[0] == "state rm")
        assert removed[1] == old and list(removed[2]) == plan.detach_addresses[:-1]
        assert json.loads(Path(result.backup_path).read_text())["serial"] == 9
        assert oct(Path(result.backup_path).stat().st_mode & 0o777) == "0o600"
        assert result.notes == [
            f"already absent from the old state: {plan.detach_addresses[-1]}"
        ]
        assert "state push -force" in result.back_out[0]

    def test_a_dry_run_removes_nothing(self, fake_tf, planned_dir, tmp_path):
        plan = build()
        fake_tf.state_lists[planned_dir] = [r.new_address for r in plan.imports]
        old = self.old_dir(tmp_path, fake_tf, plan.detach_addresses)
        result = migration().detach(str(old), str(planned_dir))
        assert (
            len(result.changes) == len(plan.detach_addresses)
            and result.applied is False
        )
        assert not [c for c in fake_tf.calls if c[0] == "state rm"]

    def test_a_state_holding_none_of_the_addresses_is_refused(
        self, fake_tf, planned_dir, tmp_path
    ):
        plan = build()
        fake_tf.state_lists[planned_dir] = [r.new_address for r in plan.imports]
        old = self.old_dir(tmp_path, fake_tf, ["aws_lambda_function.other"])
        result = migration().detach(str(old), str(planned_dir), apply=True)
        assert result.ok is False
        assert any(
            "none of the" in f and "addresses to detach" in f for f in result.findings
        )

    def test_a_state_of_another_lineage_is_refused(
        self, fake_tf, planned_dir, tmp_path
    ):
        plan = build()
        fake_tf.state_lists[planned_dir] = [r.new_address for r in plan.imports]
        old = self.old_dir(
            tmp_path, fake_tf, plan.detach_addresses, lineage="another-deployment"
        )
        result = migration().detach(str(old), str(planned_dir), apply=True)
        assert result.ok is False and result.applied is False
        assert any("lineage 'another-deployment'" in f for f in result.findings)
        assert not [c for c in fake_tf.calls if c[0] == "state rm"]


def deletion(address, tf_type, **before):
    return {
        "address": address,
        "mode": "managed",
        "type": tf_type,
        "change": {"actions": ["delete"], "before": before},
    }


def destroy_plan(*changes):
    return {"format_version": "1.2", "resource_changes": list(changes)}


def recreated(plan, tf_type):
    return next(
        r.old_address
        for r in plan.resources
        if r.old_type == tf_type and r.verdict == tm.RECREATE
    )


class TestDestroyPlanCheck:
    def test_deleting_a_kept_resource_is_a_finding(self):
        plan = build()
        findings, _ = tm.check_destroy_plan(
            destroy_plan(
                deletion(recreated(plan, "aws_lambda_function"), "aws_lambda_function"),
                deletion(
                    recreated(plan, "aws_sqs_queue_policy"),
                    "aws_sqs_queue_policy",
                    bucket="oldidp-output-i2pfi4o2",
                ),
                deletion(
                    recreated(plan, "aws_cloudwatch_event_rule"),
                    "aws_cloudwatch_event_rule",
                    key_id=KEY_ID,
                ),
                deletion(
                    recreated(plan, "aws_iam_role"),
                    "aws_iam_role",
                    user_pool_id=POOL,
                ),
            ),
            plan,
        )
        assert len(findings) == 3
        assert all("touches kept" in f for f in findings)

    def test_the_old_deployments_own_resources_may_go(self):
        plan = build()
        findings, details = tm.check_destroy_plan(
            destroy_plan(
                deletion(
                    recreated(plan, "aws_cognito_user_pool_client"),
                    "aws_cognito_user_pool_client",
                    user_pool_id=POOL,
                    id="c",
                ),
                deletion(
                    recreated(plan, "aws_wafv2_web_acl_association"),
                    "aws_wafv2_web_acl_association",
                    resource_arn="arn",
                ),
                deletion(
                    recreated(plan, "aws_lambda_function"),
                    "aws_lambda_function",
                    function_name="oldidp-input-i2pfi4o2",
                ),
            ),
            plan,
        )
        assert findings == []
        assert details["deletions"] == 3 and details["recordedDeletions"] == 3

    @pytest.mark.parametrize(
        "payload,match",
        [
            ({}, "not `terraform show -json` output"),
            ([], "not a Terraform plan"),
            (old_state(), "Terraform state file"),
            ({"format_version": "1.2", "resource_changes": []}, "no resource changes"),
            (
                {"format_version": "1.2", "errored": True, "resource_changes": [{}]},
                "errored",
            ),
        ],
        ids=["empty", "a-list", "the-state-file", "no-changes", "errored"],
    )
    def test_anything_but_a_destroy_plan_is_refused(self, payload, match):
        findings, _ = tm.check_destroy_plan(payload, build())
        assert len(findings) == 1 and match in findings[0]

    def test_a_change_that_is_not_a_delete_is_refused(self):
        plan = build()
        rc = deletion(recreated(plan, "aws_lambda_function"), "aws_lambda_function")
        rc["change"]["actions"] = ["create"]
        findings, _ = tm.check_destroy_plan(destroy_plan(rc), plan)
        assert findings and "not a destroy plan" in findings[0]

    def test_another_deployments_destroy_plan_is_refused(self):
        findings, _ = tm.check_destroy_plan(
            destroy_plan(
                deletion("aws_lambda_function.somebody_else", "aws_lambda_function")
            ),
            build(),
        )
        assert findings == [
            "none of this plan's deletions is an address the migration plan recorded "
            "for the old deployment, so this is not the old deployment's destroy plan "
            "(or the plan was built from another state)"
        ]

    def test_a_detached_address_still_in_the_plan_is_a_finding(self):
        plan = build()
        findings, _ = tm.check_destroy_plan(
            destroy_plan(
                deletion(recreated(plan, "aws_lambda_function"), "aws_lambda_function"),
                deletion("aws_s3_bucket.input_bucket", "aws_s3_bucket"),
            ),
            plan,
        )
        assert findings == [
            "aws_s3_bucket.input_bucket is in the destroy plan, but the migration "
            "detached it from the old state; run `detach --apply` first"
        ]

    def test_an_address_the_migration_plan_never_saw_is_a_finding(self):
        plan = build()
        findings, _ = tm.check_destroy_plan(
            destroy_plan(
                deletion(recreated(plan, "aws_lambda_function"), "aws_lambda_function"),
                deletion("aws_lambda_function.added_later", "aws_lambda_function"),
            ),
            plan,
        )
        assert len(findings) == 1
        assert findings[0].startswith(
            "aws_lambda_function.added_later is in the destroy plan but not in the "
            "migration plan"
        )

    def test_the_operation_reports_the_recorded_resources_left_behind(
        self, planned_dir, tmp_path
    ):
        plan = build()
        path = tmp_path / "destroy.json"
        path.write_text(
            json.dumps(
                destroy_plan(
                    deletion(
                        recreated(plan, "aws_lambda_function"), "aws_lambda_function"
                    )
                )
            )
        )
        result = migration().check_destroy(str(path), str(planned_dir))
        assert result.ok is True
        assert result.details["recordedNotInDestroyPlan"]
        assert "would be left behind" in result.notes[0]

    def test_the_operation_refuses_the_state_file(self, planned_dir):
        result = migration().check_destroy(
            str(FIXTURES / "old_state.tfstate"), str(planned_dir)
        )
        assert result.ok is False and "state file" in result.findings[0]


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
        backup = (
            directory.resolve()
            / tm.MIGRATION_DIR
            / "old-state-backup-before-detach.tfstate"
        )
        assert result.detach_commands[0] == (
            f"(umask 077 && terraform state pull > '{backup}')"
        )
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


# ---------------------------------------------------------------------------
# the credentials the migration's own AWS calls use
# ---------------------------------------------------------------------------


class TestTerraformCredentials:
    def test_the_role_is_read_from_both_provider_blocks(self, tmp_path):
        main = tmp_path / "main.tf"
        main.write_text(tf.render_provider_blocks(REGION, ROLE_ARN))
        assert tm.terraform_role_arn(main) == ROLE_ARN
        main.write_text(tf.render_provider_blocks(REGION))
        assert tm.terraform_role_arn(main) is None

    def test_providers_that_disagree_are_refused(self, tmp_path):
        main = tmp_path / "main.tf"
        text = tf.render_provider_blocks(REGION, ROLE_ARN)
        cut = text.index('provider "awscc"')
        main.write_text(text[:cut] + 'provider "awscc" {\n  region = "us-west-2"\n}\n')
        with pytest.raises(IDPConfigurationError, match="assume different roles"):
            tm.terraform_role_arn(main)

    def test_no_main_tf_is_refused(self, tmp_path):
        with pytest.raises(
            IDPConfigurationError, match="cannot tell which credentials"
        ):
            tm.terraform_role_arn(tmp_path / "main.tf")

    def test_a_phase_acts_as_the_role_terraform_assumes(
        self, tmp_path, aws_credentials
    ):
        directory = tmp_path / "d"
        write_planned_dir(directory, role_arn=ROLE_ARN)
        with mock_aws():
            op = migration()
            # The role is in another account, so its tables are not the ambient
            # credentials' tables: the phase can only find them through the role.
            op._use_role(ROLE_ARN)
            tracking_and_config_tables(op._boto("dynamodb"))
            op._use_role(None)
            ambient = boto3.client("dynamodb", region_name=REGION).list_tables()
            assert ambient["TableNames"] == []
            result = op.fix_config(str(directory))
            arn = op._boto("sts").get_caller_identity()["Arn"]
        assert result.ok is True
        assert arn.endswith(
            "assumed-role/idp-terraform-deployer/idp-cli-terraform-migrate"
        )

    def test_without_a_role_a_phase_uses_the_ambient_credentials(
        self, planned_dir, aws_credentials
    ):
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name=REGION)
            tracking_and_config_tables(ddb)
            op = migration()
            op.fix_config(str(planned_dir))
            arn = op._boto("sts").get_caller_identity()["Arn"]
        assert "assumed-role" not in arn

    def test_the_plan_phase_looks_up_users_as_the_role(
        self, monkeypatch, fake_tf, tmp_path, aws_credentials
    ):
        manifest = json.loads((FIXTURES / "module" / "idp-terraform.json").read_text())
        monkeypatch.setattr(tf, "load_module_manifest", lambda url, sha=None: manifest)
        monkeypatch.setattr(tm, "module_dir_from_root", lambda d: FIXTURES / "module")
        monkeypatch.setattr(
            "idp_sdk.operations.stack.StackOperation._describe_stack",
            lambda self, d, n: None,
        )
        seen = []

        def lookup(self, pool_id, username):
            seen.append(self._boto("sts").get_caller_identity()["Arn"])
            return tm.UserLookup(exists=True, groups=["Admin"])

        monkeypatch.setattr(
            "idp_sdk.operations.terraform.GenaiIdpTerraformMigration._user_lookup",
            lookup,
        )
        state = tmp_path / "old.tfstate"
        state.write_text((FIXTURES / "old_state.tfstate").read_text())
        with mock_aws():
            migration().plan(
                str(state),
                ADMIN,
                module_url="https://example.com/m.zip",
                terraform_dir=str(tmp_path / "new"),
                assume_role_arn=ROLE_ARN,
            )
        assert len(seen) == 1 and "assumed-role/idp-terraform-deployer/" in seen[0]
        assert tm.terraform_role_arn(tmp_path / "new" / "main.tf") == ROLE_ARN


def _fake_terraform(tmp_path, stderr, code):
    script = tmp_path / "terraform"
    script.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit {code}\n")
    script.chmod(0o755)
    return str(script)


def test_state_list_of_a_directory_with_no_state_yet_is_empty(tmp_path):
    """Measured live: adopt-user-pool's first `state list`, in a directory `plan`
    had only initialised, exited 1 with this message and failed the phase."""
    from idp_sdk._core import terraform_deploy as tf

    binary = _fake_terraform(
        tmp_path,
        "No state file was found!\nState management commands require a state file.",
        1,
    )
    assert tf.TerraformRunner(binary, tmp_path).state_list() == []


def test_state_list_still_raises_any_other_failure(tmp_path):
    from idp_sdk._core import terraform_deploy as tf
    from idp_sdk.exceptions import IDPStackError

    binary = _fake_terraform(tmp_path, "Error: Backend initialization required", 1)
    with pytest.raises(IDPStackError, match="Backend initialization required"):
        tf.TerraformRunner(binary, tmp_path).state_list()


def _ttl_plan(before_attr, after_attr, before_on=True, after_on=True):
    def ttl(attr, on):
        return [{"enabled": on, "attribute_name": attr}]

    return {
        "resource_changes": [
            {
                "address": "module.idp.aws_dynamodb_table.agent_table",
                "type": "aws_dynamodb_table",
                "change": {
                    "actions": ["update"],
                    "before": {
                        "name": "agent-jobs-x",
                        "ttl": ttl(before_attr, before_on),
                    },
                    "after": {"name": "agent-jobs-x", "ttl": ttl(after_attr, after_on)},
                },
            }
        ]
    }


def test_a_ttl_attribute_switch_is_found():
    """Measured live: the module moves the agent-jobs table from expiresAfter to
    ExpiresAfter, and UpdateTimeToLive refused it mid-apply."""
    assert tm.ttl_switches(_ttl_plan("expiresAfter", "ExpiresAfter")) == [
        {
            "address": "module.idp.aws_dynamodb_table.agent_table",
            "table": "agent-jobs-x",
            "from": "expiresAfter",
            "to": "ExpiresAfter",
        }
    ]
    assert tm.ttl_switches(_ttl_plan("a", "a")) == []
    # Turning TTL on or off is one UpdateTimeToLive call, so it is not a switch.
    assert tm.ttl_switches(_ttl_plan("a", "b", before_on=False)) == []


@pytest.mark.parametrize(
    "status,expect",
    [
        ("ENABLED", "Enabled=false,AttributeName=expiresAfter"),
        ("DISABLING", "still turning off"),
    ],
)
def test_a_switch_is_refused_until_the_old_ttl_is_off(status, expect):
    switches = tm.ttl_switches(_ttl_plan("expiresAfter", "ExpiresAfter"))
    findings = tm.ttl_switch_findings(
        switches, lambda table: {"TimeToLiveStatus": status}, "us-west-2"
    )
    assert len(findings) == 1 and expect in findings[0]


def test_a_switch_on_a_table_whose_ttl_is_off_applies():
    switches = tm.ttl_switches(_ttl_plan("expiresAfter", "ExpiresAfter"))
    assert (
        tm.ttl_switch_findings(
            switches, lambda t: {"TimeToLiveStatus": "DISABLED"}, "r"
        )
        == []
    )
