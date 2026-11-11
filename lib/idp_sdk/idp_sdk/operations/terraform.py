# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Terraform-only operations: ``client.terraform``.

``client.terraform.migrate_from_genai_idp_terraform`` moves a deployment of the
separate ``awslabs/genai-idp-terraform`` implementation onto this repository's
published Terraform module in place, keeping its buckets, tables, key and user
pool. The logic is in :mod:`idp_sdk._core.terraform_migrate`; see
``docs/terraform-migration.md`` for the procedure.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from idp_sdk.exceptions import IDPConfigurationError
from idp_sdk.models.terraform import (
    TerraformMigrationPlanResult,
    TerraformMigrationResource,
    TerraformMigrationStepResult,
)

logger = logging.getLogger(__name__)

# The saved plan the adopt-user-pool and apply phases check; removed afterwards,
# because a plan file holds the variable values, NoEcho ones included.
MIGRATION_PLAN_FILE = "idp-cli-migrate.tfplan"


class TerraformOperation:
    """Operations that apply only to Terraform deployments."""

    def __init__(self, client):
        self._client = client
        self.migrate_from_genai_idp_terraform = GenaiIdpTerraformMigration(client)


class GenaiIdpTerraformMigration:
    """Move a ``genai-idp-terraform`` deployment onto the built-in module in place.

    Every method works on the new deployment's root configuration directory
    (``terraform_dir``, default ``./idp-terraform/<stack_name>``), where ``plan``
    writes ``adopt.tf`` and the ``genai-idp-terraform-migration/`` directory the
    later phases read. Methods that change something are dry runs unless
    ``apply=True``; each returns what it changes (or would) in ``changes``.
    """

    def __init__(self, client, sleep: Callable[[float], None] = time.sleep):
        self._client = client
        self._sleep = sleep

    # -- helpers ------------------------------------------------------------

    def _region(self) -> str:
        region = self._client._region
        if not region:
            raise IDPConfigurationError(
                "a region is required for a Terraform migration"
            )
        return region

    def _boto(self, service: str):
        import boto3

        return boto3.client(service, region_name=self._region())

    def _directory(self, terraform_dir: Optional[str]) -> Path:
        name = self._client._require_stack(None)
        return Path(terraform_dir or Path("idp-terraform") / name).resolve()

    def _migration_dir(self, directory: Path) -> Path:
        from idp_sdk._core import terraform_migrate as tm

        return directory / tm.MIGRATION_DIR

    def _load(self, directory: Path):
        from idp_sdk._core import terraform_migrate as tm

        path = self._migration_dir(directory) / tm.PLAN_FILE
        if not path.is_file():
            raise IDPConfigurationError(
                f"{path} does not exist; run the `plan` phase for this deployment first"
            )
        return tm.MigrationPlan.from_json(json.loads(path.read_text()))

    def _tfvars(self, directory: Path) -> Dict[str, Any]:
        from idp_sdk._core import terraform_deploy as tf

        path = directory / "terraform.tfvars"
        return tf.parse_tfvars(path.read_text()) if path.is_file() else {}

    def _user_lookup(self, pool_id: str, username: str):
        from botocore.exceptions import ClientError

        from idp_sdk._core import terraform_migrate as tm

        cognito = self._boto("cognito-idp")
        try:
            cognito.admin_get_user(UserPoolId=pool_id, Username=username)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "UserNotFoundException":
                return tm.UserLookup(exists=False)
            raise
        groups = cognito.admin_list_groups_for_user(
            UserPoolId=pool_id, Username=username
        )
        return tm.UserLookup(
            exists=True, groups=[g["GroupName"] for g in groups.get("Groups", [])]
        )

    # -- plan ---------------------------------------------------------------

    def plan(
        self,
        old_state_path: str,
        admin_email: str,
        parameters: Optional[Dict[str, str]] = None,
        *,
        module_url: Optional[str] = None,
        pointer_url: Optional[str] = None,
        terraform_dir: Optional[str] = None,
        terraform_bin: str = "terraform",
        assume_role_arn: Optional[str] = None,
    ) -> TerraformMigrationPlanResult:
        """Map the old state to the module and write the adoption files.

        Writes the new deployment's ``main.tf`` and ``terraform.tfvars`` (as
        ``client.stack.deploy_terraform`` would), runs ``terraform init``, then
        writes ``adopt.tf`` (one ``import {}`` block per adopted resource),
        ``genai-idp-terraform-migration/migration-plan.json`` and
        ``detach-addresses.txt``. Applies nothing and changes nothing in AWS; it
        reads the old user pool to find out whether ``admin_email`` names an
        existing user there.

        Args:
            old_state_path: The old deployment's ``terraform.tfstate``, or the
                output of ``terraform state pull`` in its directory.
            admin_email: The new deployment's admin user. When a user of exactly
                this name (usernames are case-sensitive) exists in the old pool it
                is adopted; otherwise the module creates one.
            parameters: Stack parameters by CloudFormation name. The plan adds the
                ones the old deployment requires (``ReportingBucketName``, and
                ``ExternalIdPEmailMutable`` when the old pool's email is mutable).
            module_url: Module zip URL. Without it, ``pointer_url`` is read.
            pointer_url: URL of an ``idp-terraform-latest.json`` pointer.
            terraform_dir: The new deployment's root configuration directory.
            terraform_bin: ``terraform``, ``tofu`` or a path to either.
            assume_role_arn: As for ``client.stack.deploy_terraform``.

        Raises:
            IDPConfigurationError: An old stateful resource with no verdict, an
                unsupported resource, a contradicting parameter, an ``adopt.tf``
                not written by this command, or a CloudFormation stack of this name.
        """
        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core import terraform_migrate as tm
        from idp_sdk._core.stack import StackDeployer
        from idp_sdk._core.terraform_stack import is_terraform_label

        name = self._client._require_stack(None)
        region = self._region()
        directory = self._directory(terraform_dir)
        runner = tf.TerraformRunner(terraform_bin, directory)
        try:
            old_state = json.loads(Path(old_state_path).read_text())
        except (OSError, ValueError) as e:
            raise IDPConfigurationError(f"cannot read {old_state_path}: {e}") from e
        # Fail on the old state before downloading anything.
        tm.load_old_state(old_state)

        adopt_path = directory / tm.ADOPT_FILE
        if adopt_path.exists() and tm.ADOPT_MARKER not in adopt_path.read_text():
            raise IDPConfigurationError(
                f"{adopt_path} exists and was not written by this command; refusing to "
                "overwrite it"
            )
        stack = self._client.stack._describe_stack(StackDeployer(region=region), name)
        if stack is not None and not is_terraform_label(stack):
            raise IDPConfigurationError(
                f"{name} is a CloudFormation stack; the new deployment needs a name no "
                "stack uses"
            )

        sha = None
        if not module_url:
            if not pointer_url:
                raise IDPConfigurationError(
                    "no Terraform module URL: pass module_url or pointer_url"
                )
            module_url, sha = tf.resolve_pointer(json.loads(tf.fetch_url(pointer_url)))
        manifest = tf.load_module_manifest(module_url, sha)

        def write_config(values: Dict[str, str]) -> None:
            tf.write_root_config(
                directory,
                region=region,
                module_url=module_url,
                stack_name=name,
                admin_email=admin_email,
                parameters=values,
                manifest=manifest,
                assume_role_arn=assume_role_arn,
            )

        # init needs a configuration; the parameters the old state requires are
        # known only once the module is installed, so the config is written twice.
        write_config(dict(parameters or {}))
        runner.init(upgrade=tf.needs_upgrade(directory, module_url))
        tf.record_module_source(directory, module_url)
        module = tm.ModuleSource.read(tm.module_dir_from_root(directory))

        plan = tm.build_plan(
            old_state,
            module,
            admin_email=admin_email,
            parameters=parameters,
            user_lookup=self._user_lookup,
        )
        write_config(plan.parameters)

        mdir = self._migration_dir(directory)
        mdir.mkdir(parents=True, exist_ok=True)
        adopt_path.write_text(tm.render_adopt_tf(plan))
        plan_path = mdir / tm.PLAN_FILE
        tf.write_private(plan_path, json.dumps(plan.to_json(), indent=2) + "\n")
        detach_path = mdir / tm.DETACH_FILE
        detach_path.write_text("".join(a + "\n" for a in plan.detach_addresses))

        return TerraformMigrationPlanResult(
            terraform_dir=str(directory),
            resources=[
                TerraformMigrationResource(**r) for r in plan.to_json()["resources"]
            ],
            parameters=plan.parameters,
            detach_addresses=plan.detach_addresses,
            detach_commands=tm.detach_commands(
                plan.detach_addresses, Path(terraform_bin).name
            ),
            warnings=plan.warnings,
            files={
                "adopt": str(adopt_path),
                "plan": str(plan_path),
                "detach": str(detach_path),
                "main": str(directory / "main.tf"),
                "tfvars": str(directory / "terraform.tfvars"),
            },
        )

    # -- retention ------------------------------------------------------------

    def check_retention(
        self,
        terraform_dir: Optional[str] = None,
        accept_expiry: Iterable[str] = (),
    ) -> TerraformMigrationStepResult:
        """Compare each adopted bucket's oldest object with the module's expiry.

        The module puts a lifecycle rule on the buckets it manages (for example
        ``DataRetentionInDays`` on the input and output buckets). Adopting a
        bucket applies that rule to objects that are already there, so a bucket
        holding an object older than the rule is a finding unless its kind is in
        ``accept_expiry``. Reads only (``ListObjectsV2`` over every object).
        """
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        module = tm.ModuleSource.read(tm.module_dir_from_root(directory))
        rows, findings = tm.check_retention(
            plan,
            module,
            self._tfvars(directory),
            self._boto("s3"),
            accept_expiry=accept_expiry,
        )
        return TerraformMigrationStepResult(
            phase="check-retention",
            applied=False,
            ok=not findings,
            findings=findings,
            details={"buckets": rows},
        )

    # -- freeze ---------------------------------------------------------------

    def freeze(
        self,
        terraform_dir: Optional[str] = None,
        *,
        apply: bool = False,
        wait: bool = False,
        timeout_seconds: int = 1800,
        poll_seconds: int = 30,
    ) -> TerraformMigrationStepResult:
        """Stop the old deployment taking new uploads, and wait for in-flight work.

        Once the new deployment adopts the input bucket, the old deployment's
        EventBridge rule on that bucket would start a second workflow for every
        upload. With ``apply`` the rule is disabled (``DisableRule``; the old
        deployment's state still says enabled, which does not matter because it
        is never applied again). Then the tracking table is scanned for documents
        whose status is not COMPLETED, FAILED or ABORTED; with ``wait`` the scan
        repeats until there are none or ``timeout_seconds`` passes.
        """
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        region = self._region()
        events = self._boto("events")
        ddb = self._boto("dynamodb")
        findings: List[str] = []
        changes: List[str] = []
        back_out: List[str] = []
        if not plan.upload_rules:
            findings.append(
                "the old state has no EventBridge rule on the input bucket, so this "
                "command cannot stop the old deployment's uploads; find what starts "
                "its workflows before going on"
            )
        for rule in tm.upload_rule_states(plan, events):
            if rule["state"] != "DISABLED":
                changes.append(
                    f"disable EventBridge rule {rule['name']} (bus {rule['eventBusName']})"
                )
                if apply:
                    events.disable_rule(
                        Name=rule["name"], EventBusName=rule["eventBusName"]
                    )
            back_out.append(
                f"aws events enable-rule --name {rule['name']} --event-bus-name "
                f"{rule['eventBusName']} --region {region}"
            )
        notes: List[str] = []
        if not apply and changes:
            findings.append("the old upload rule is still enabled (dry run)")
        deadline = time.monotonic() + timeout_seconds
        in_flight: List[Dict[str, str]] = []
        if plan.tracking_table:
            while True:
                in_flight = tm.in_flight_documents(plan.tracking_table, ddb)
                if not in_flight or not wait or time.monotonic() >= deadline:
                    break
                notes.append(f"{len(in_flight)} document(s) in flight; waiting")
                self._sleep(poll_seconds)
        if in_flight:
            findings.append(
                f"{len(in_flight)} document(s) are still being processed by the old "
                "deployment; wait for them (--wait) before applying"
            )
        return TerraformMigrationStepResult(
            phase="freeze",
            applied=apply and bool(changes),
            ok=not findings,
            changes=changes,
            findings=findings,
            notes=notes,
            back_out=back_out,
            details={"inFlight": in_flight[:50], "inFlightCount": len(in_flight)},
        )

    # -- configuration fix -------------------------------------------------------

    def fix_config(
        self,
        terraform_dir: Optional[str] = None,
        *,
        apply: bool = False,
        restore: Optional[str] = None,
    ) -> TerraformMigrationStepResult:
        """Remove the revision counters from the old configuration rows.

        The old deployment writes ``LatestRevision`` and ``PublishedRevision`` on
        its ``Config#<profile>`` rows but never writes the revision bodies they
        count. The module's first deploy reads those counters as a revision
        history that exists, and writes the default configuration over yours.
        With ``apply`` every ``Config#`` row is backed up to a file first, then the
        two attributes are removed. Refuses a table that already holds a revision
        index (that history is real). ``restore`` (a backup file) writes the
        backed-up rows back, with ``apply``.
        """
        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        table = plan.configuration_table
        if not table:
            raise IDPConfigurationError(
                "the migration plan adopts no configuration table"
            )
        ddb = self._boto("dynamodb")
        region = self._region()
        if restore:
            items = json.loads(Path(restore).read_text())["Items"]
            changes = [f"put back {i['Configuration']['S']}" for i in items]
            if apply:
                for item in items:
                    ddb.put_item(TableName=table, Item=item)
            return TerraformMigrationStepResult(
                phase="fix-config", applied=apply, ok=True, changes=changes
            )
        items = tm.scan_all(ddb, table)
        targets, refusals = tm.config_fix_targets(items)
        changes = [
            f"remove {', '.join(tm.REVISION_COUNTERS)} from {key} in {table}"
            for key in targets
        ]
        backup: Optional[str] = None
        if apply and targets and not refusals:
            mdir = self._migration_dir(directory)
            mdir.mkdir(parents=True, exist_ok=True)
            path = mdir / f"configuration-table-backup-{tm.timestamp()}.json"
            rows = [
                i for i in items if i["Configuration"]["S"].startswith(tm.CONFIG_PREFIX)
            ]
            tf.write_private(
                path, json.dumps({"TableName": table, "Items": rows}, indent=1)
            )
            backup = str(path)
            for key in targets:
                ddb.update_item(
                    TableName=table,
                    Key={"Configuration": {"S": key}},
                    UpdateExpression="REMOVE LatestRevision, PublishedRevision",
                    ConditionExpression="attribute_exists(Configuration)",
                )
        return TerraformMigrationStepResult(
            phase="fix-config",
            applied=apply and bool(targets) and not refusals,
            ok=not refusals,
            changes=changes,
            findings=refusals,
            backup_path=backup,
            back_out=(
                [
                    "idp-cli terraform migrate-from-genai-idp-terraform fix-config "
                    f"--stack-name {self._client._require_stack(None)} --region {region} "
                    f"--restore {backup} --apply"
                ]
                if backup
                else []
            ),
        )

    # -- user pool ----------------------------------------------------------------

    def adopt_user_pool(
        self,
        terraform_dir: Optional[str] = None,
        terraform_bin: str = "terraform",
        *,
        apply: bool = False,
    ) -> TerraformMigrationStepResult:
        """Import the user pool into the new state, then normalise its schema there.

        The pool is imported with ``terraform import`` (a state-only operation)
        rather than an ``import {}`` block, so that its ``schema`` can be
        rewritten before anything is applied: an imported pool's state records
        every detail Cognito reports for each schema attribute, which the provider
        reads as removing a schema item, and Cognito refuses that. The new
        deployment's state is backed up before it is rewritten.
        """
        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        pool = next((r for r in plan.resources if r.kind == "user_pool"), None)
        if pool is None or not pool.new_address or not pool.import_id:
            raise IDPConfigurationError("the migration plan adopts no user pool")
        runner = tf.TerraformRunner(terraform_bin, directory)
        changes: List[str] = []
        notes: List[str] = []
        in_state = pool.new_address in runner.state_list()
        if not in_state:
            changes.append(f"terraform import {pool.new_address} {pool.import_id}")
            if not apply:
                notes.append(
                    "the schema rewrite is computed after the import; run again with "
                    "--apply"
                )
                return TerraformMigrationStepResult(
                    phase="adopt-user-pool",
                    applied=False,
                    ok=True,
                    changes=changes,
                    notes=notes,
                )
            runner.run("import", "-input=false", pool.new_address, pool.import_id)
        plan_path = directory / MIGRATION_PLAN_FILE
        try:
            runner.plan(MIGRATION_PLAN_FILE)
            desired = tm.desired_pool_schema(
                runner.show_plan(MIGRATION_PLAN_FILE), pool.new_address
            )
        finally:
            plan_path.unlink(missing_ok=True)
        state = json.loads(runner.capture("state", "pull"))
        current = tm.state_schema(state, pool.new_address) or []
        schema, schema_changes, conflicts = tm.normalise_pool_schema(current, desired)
        changes += schema_changes
        backup: Optional[str] = None
        rewrite = any(c.startswith(("rewrite", "drop")) for c in schema_changes)
        if apply and rewrite and not conflicts:
            mdir = self._migration_dir(directory)
            mdir.mkdir(parents=True, exist_ok=True)
            path = mdir / f"new-state-backup-{tm.timestamp()}.tfstate"
            tf.write_private(path, json.dumps(state, indent=1))
            backup = str(path)
            pushed = mdir / "new-state-normalised.tfstate"
            tf.write_private(
                pushed,
                json.dumps(
                    tm.rewrite_state_schema(state, pool.new_address, schema), indent=1
                ),
            )
            try:
                runner.run("state", "push", str(pushed))
            finally:
                pushed.unlink(missing_ok=True)
        return TerraformMigrationStepResult(
            phase="adopt-user-pool",
            applied=apply,
            ok=not conflicts,
            changes=changes,
            findings=conflicts,
            notes=notes,
            backup_path=backup,
            back_out=(
                [f"{Path(terraform_bin).name} state push -force {backup}"]
                if backup
                else []
            )
            + [f"{Path(terraform_bin).name} state rm {pool.new_address}"],
            details={"schema": schema},
        )

    # -- apply ----------------------------------------------------------------------

    def apply(
        self,
        terraform_dir: Optional[str] = None,
        terraform_bin: str = "terraform",
        *,
        apply: bool = False,
        accept_expiry: Iterable[str] = (),
    ) -> TerraformMigrationStepResult:
        """Plan the adoption, check it, and (with ``apply``) apply exactly that plan.

        First re-checks the earlier phases: retention, the old upload rule
        disabled, nothing in flight, the configuration counters removed, and the
        user pool in state. Then saves a plan and refuses it unless every
        resource in ``adopt.tf`` is imported in place and nothing at all is
        replaced or destroyed.
        """
        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        findings: List[str] = []

        retention = self.check_retention(terraform_dir, accept_expiry)
        findings += retention.findings
        events = self._boto("events")
        for rule in tm.upload_rule_states(plan, events):
            if rule["state"] != "DISABLED":
                findings.append(
                    f"the old upload rule {rule['name']} is enabled; run `freeze`"
                )
        ddb = self._boto("dynamodb")
        if plan.tracking_table and tm.in_flight_documents(
            plan.tracking_table, ddb, limit=1
        ):
            findings.append(
                "documents are still in flight in the old deployment; run `freeze --wait`"
            )
        if plan.configuration_table:
            targets, _ = tm.config_fix_targets(
                tm.scan_all(ddb, plan.configuration_table)
            )
            if targets:
                findings.append(
                    "the configuration table still carries revision counters; run `fix-config`"
                )

        runner = tf.TerraformRunner(terraform_bin, directory)
        plan_path = directory / MIGRATION_PLAN_FILE
        counts: Dict[str, int] = {}
        applied = False
        try:
            runner.plan(MIGRATION_PLAN_FILE)
            counts, plan_findings = tm.check_adoption_plan(
                runner.show_plan(MIGRATION_PLAN_FILE), plan
            )
            findings += plan_findings
            if apply and not findings:
                runner.apply_plan(MIGRATION_PLAN_FILE)
                applied = True
        finally:
            plan_path.unlink(missing_ok=True)
        summary = (
            f"Plan: {counts.get('import', 0)} to import, {counts.get('add', 0)} to add, "
            f"{counts.get('change', 0)} to change, {counts.get('destroy', 0)} to destroy."
        )
        return TerraformMigrationStepResult(
            phase="apply",
            applied=applied,
            ok=not findings,
            changes=[summary],
            findings=findings,
            notes=[] if applied else ["nothing was applied"],
            details={"counts": counts},
        )

    # -- detach -------------------------------------------------------------------------

    def detach(
        self,
        old_terraform_dir: str,
        terraform_dir: Optional[str] = None,
        terraform_bin: str = "terraform",
        *,
        apply: bool = False,
    ) -> TerraformMigrationStepResult:
        """Remove the adopted and kept resources from the old deployment's state.

        After this, ``terraform destroy`` in the old deployment's directory leaves
        them alone. Refuses until the new deployment's state holds every adopted
        resource. With ``apply`` the old state is saved to a backup file first
        (``terraform state pull``), then ``terraform state rm`` runs in
        ``old_terraform_dir``.
        """
        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        new_state = set(tf.TerraformRunner(terraform_bin, directory).state_list())
        missing_new = sorted(
            r.new_address for r in plan.imports if r.new_address not in new_state
        )
        findings = [
            f"{a} is not in the new deployment's state yet; apply the adoption first"
            for a in missing_new
        ]
        old = tf.TerraformRunner(terraform_bin, Path(old_terraform_dir).resolve())
        old_list = set(old.state_list())
        present = [a for a in plan.detach_addresses if a in old_list]
        absent = [a for a in plan.detach_addresses if a not in old_list]
        backup: Optional[str] = None
        if apply and present and not findings:
            mdir = self._migration_dir(directory)
            mdir.mkdir(parents=True, exist_ok=True)
            path = mdir / f"old-state-backup-before-detach-{tm.timestamp()}.tfstate"
            tf.write_private(path, old.capture("state", "pull"))
            backup = str(path)
            old.state_rm(present)
        binary = Path(terraform_bin).name
        return TerraformMigrationStepResult(
            phase="detach",
            applied=apply and bool(present) and not findings,
            ok=not findings,
            changes=[f"{binary} state rm {a}" for a in present],
            findings=findings,
            notes=[f"already absent from the old state: {a}" for a in absent],
            backup_path=backup,
            back_out=[f"{binary} state push -force {backup}  # in {old.directory}"]
            if backup
            else [],
        )

    # -- the old deployment's destroy plan -------------------------------------------------

    def check_destroy(
        self, destroy_plan_path: str, terraform_dir: Optional[str] = None
    ) -> TerraformMigrationStepResult:
        """Check the old deployment's destroy plan deletes nothing that was kept.

        ``destroy_plan_path`` is ``terraform show -json`` of a plan saved with
        ``terraform plan -destroy -out=<file>`` in the old deployment's directory.
        Reads only.
        """
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        destroy = json.loads(Path(destroy_plan_path).read_text())
        findings = tm.check_destroy_plan(destroy, plan)
        deletes = sum(
            1
            for rc in destroy.get("resource_changes", [])
            if "delete" in rc["change"]["actions"]
        )
        return TerraformMigrationStepResult(
            phase="check-destroy",
            applied=False,
            ok=not findings,
            findings=findings,
            details={"deletions": deletes},
        )
