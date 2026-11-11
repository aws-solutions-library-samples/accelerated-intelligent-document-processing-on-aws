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
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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
        self._role_arn: Optional[str] = None
        self._session: Any = None

    # -- helpers ------------------------------------------------------------

    def _region(self) -> str:
        region = self._client._region
        if not region:
            raise IDPConfigurationError(
                "a region is required for a Terraform migration"
            )
        return region

    def _use_role(self, role_arn: Optional[str]) -> None:
        """Make every AWS call this object makes use ``role_arn``, as Terraform
        does (None: the ambient credentials, as Terraform then does too)."""
        if role_arn != self._role_arn or self._session is None:
            self._role_arn = role_arn
            self._session = None

    def _use_terraform_credentials(self, directory: Path) -> None:
        from idp_sdk._core import terraform_migrate as tm

        self._use_role(tm.terraform_role_arn(directory / "main.tf"))

    def _boto(self, service: str):
        """A client with the credentials Terraform uses for this deployment.

        Terraform's providers assume ``--terraform-assume-role-arn`` when one was
        given, so the migration's own reads and writes (the admin-user lookup,
        the freeze, the configuration fix, the retention check and the apply's
        preconditions) assume the same role, from the ambient credentials, rather
        than acting in whatever account those point at.
        """
        import boto3

        region = self._region()
        if self._session is None:
            if self._role_arn:
                creds = boto3.client("sts", region_name=region).assume_role(
                    RoleArn=self._role_arn,
                    RoleSessionName="idp-cli-terraform-migrate",
                )["Credentials"]
                self._session = boto3.session.Session(
                    aws_access_key_id=creds["AccessKeyId"],
                    aws_secret_access_key=creds["SecretAccessKey"],
                    aws_session_token=creds["SessionToken"],
                    region_name=region,
                )
            else:
                self._session = boto3.session.Session(region_name=region)
        return self._session.client(service, region_name=region)

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
        plan = tm.MigrationPlan.from_json(json.loads(path.read_text()))
        self._use_terraform_credentials(directory)
        return plan

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
            assume_role_arn: As for ``client.stack.deploy_terraform``. The
                migration's own AWS calls (here and in every later phase, which
                read it back from ``main.tf``) assume the same role.

        Raises:
            IDPConfigurationError: An old stateful resource with no verdict, an
                unsupported resource, a contradicting parameter, an ``adopt.tf``
                not written by this command, or a CloudFormation stack of this name.
        """
        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core import terraform_migrate as tm
        from idp_sdk._core.terraform_stack import is_terraform_label

        name = self._client._require_stack(None)
        region = self._region()
        # The lookups below use the identity Terraform will use.
        self._use_role(tf.check_assume_role_arn(assume_role_arn))
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
        stack = self._client.stack._describe_stack(
            SimpleNamespace(cfn=self._boto("cloudformation")), name
        )
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
                plan.detach_addresses, Path(terraform_bin).name, mdir
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
        """Compare each kept bucket's objects with the module's expiry rules.

        The module puts a lifecycle rule on the buckets it manages (for example
        ``DataRetentionInDays`` on the input and output buckets), expiring both
        current objects and noncurrent versions. Adopting a bucket applies that
        rule to what is already there, so a bucket holding an object, or a
        noncurrent version, older than the rule is a finding unless its kind is in
        ``accept_expiry``. So is a rule whose period cannot be evaluated, a
        lifecycle configuration whose bucket cannot be resolved, and a module
        directory that cannot be read. The result's notes say what was not
        checked. Reads only (``ListObjectsV2``, ``GetBucketVersioning`` and, for a
        versioned bucket, ``ListObjectVersions`` over every version).
        """
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        module = tm.ModuleSource.read(tm.module_dir_from_root(directory))
        rows, findings, notes = tm.check_retention(
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
            notes=notes,
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
        ignore_documents: Iterable[str] = (),
    ) -> TerraformMigrationStepResult:
        """Stop the old deployment taking new uploads, and wait for in-flight work.

        Once the new deployment adopts the input bucket, the old deployment's
        EventBridge rule on that bucket would start a second workflow for every
        upload. With ``apply`` the rule is disabled (``DisableRule``; the old
        deployment's state still says enabled, which does not matter because it
        is never applied again). Then the whole tracking table is scanned for
        documents that have not finished, and documents awaiting human review
        (``HITL_IN_PROGRESS``) are reported separately; with ``wait`` the scan
        repeats until there are none or ``timeout_seconds`` passes.
        ``ignore_documents`` names documents (object keys) you have decided to
        abandon; they are listed but do not block.
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
            findings.append(tm.NO_UPLOAD_RULE)
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
        scan = tm.DocumentScan()
        if plan.tracking_table:
            while True:
                scan = tm.scan_documents(plan.tracking_table, ddb, ignore_documents)
                busy = scan.in_flight or scan.in_review
                if not busy or not wait or time.monotonic() >= deadline:
                    break
                notes.append(
                    f"{len(scan.in_flight)} document(s) in flight, "
                    f"{len(scan.in_review)} awaiting review; waiting"
                )
                self._sleep(poll_seconds)
        else:
            findings.append(
                "the migration plan records no tracking table, so nothing can say "
                "whether documents are in flight"
            )
        findings += scan.findings()
        if scan.ignored:
            notes.append(
                f"ignored with --ignore-document: {len(scan.ignored)} document(s) "
                "that have not finished"
            )
        return TerraformMigrationStepResult(
            phase="freeze",
            applied=apply and bool(changes),
            ok=not findings,
            changes=changes,
            findings=findings,
            notes=notes,
            back_out=back_out,
            details={
                "inFlight": scan.in_flight[:50],
                "inFlightCount": len(scan.in_flight),
                "inReview": scan.in_review[:50],
                "inReviewCount": len(scan.in_review),
                "ignored": scan.ignored,
                "documentsScanned": scan.scanned,
            },
        )

    # -- configuration fix -------------------------------------------------------

    def fix_config(
        self,
        terraform_dir: Optional[str] = None,
        *,
        apply: bool = False,
        restore: Optional[str] = None,
        restore_after_apply: bool = False,
    ) -> TerraformMigrationStepResult:
        """Remove the revision counters from the old configuration rows.

        The old deployment writes ``LatestRevision`` and ``PublishedRevision`` on
        its ``Config#<profile>`` rows but never writes the revision bodies they
        count. The module's first deploy reads those counters as a revision
        history that exists, and writes the default configuration over yours.
        With ``apply`` every ``Config#`` row is backed up to a file first, then the
        two attributes are removed. Refuses a table that already holds a revision
        index (that history is real). ``restore`` (a backup file) writes the
        backed-up rows back, with ``apply``. It is refused once the new deployment
        has recorded a revision history for a backed-up profile (a
        ``ConfigRevIndex#<profile>`` row, written by the first apply) unless
        ``restore_after_apply``: see the refusal for why.
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
            backup_data = json.loads(Path(restore).read_text())
            if backup_data.get("TableName") != table:
                raise IDPConfigurationError(
                    f"{restore} is a backup of {backup_data.get('TableName')!r}, not of "
                    f"this deployment's configuration table {table!r}; refusing to "
                    "write it there"
                )
            items = backup_data["Items"]
            changes = [f"put back {i['Configuration']['S']}" for i in items]
            indexed = [
                profile
                for profile in tm.backed_up_profiles(backup_data)
                if "Item"
                in ddb.get_item(
                    TableName=table,
                    Key={"Configuration": {"S": tm.REVISION_INDEX_PREFIX + profile}},
                    ProjectionExpression="Configuration",
                )
            ]
            if indexed and not restore_after_apply:
                return TerraformMigrationStepResult(
                    phase="fix-config",
                    applied=False,
                    ok=False,
                    changes=changes,
                    findings=[
                        "the new deployment has already recorded a revision history "
                        f"for {', '.join(indexed)} ("
                        + ", ".join(tm.REVISION_INDEX_PREFIX + p for p in indexed)
                        + "), so the first apply has run. Writing the backup back now "
                        "would replace each profile's head with the old row, undoing "
                        "whatever has been saved since, and put the old deployment's "
                        "LatestRevision and PublishedRevision counters back on rows "
                        "whose revision history the new deployment now keeps, where "
                        "they no longer describe it. To get a profile's pre-migration content "
                        "back after the first apply, run restore-config instead. "
                        "Pass --restore-after-apply only if you mean to discard the "
                        "new deployment's configuration history for these profiles"
                    ],
                )
            if apply:
                for item in items:
                    ddb.put_item(TableName=table, Item=tm.item_from_json(item))
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
                path,
                json.dumps(
                    {"TableName": table, "Items": tm.item_to_json(rows)}, indent=1
                ),
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
        accept_changes: Iterable[str] = (),
        ignore_documents: Iterable[str] = (),
    ) -> TerraformMigrationStepResult:
        """Plan the adoption, check it, and (with ``apply``) apply exactly that plan.

        First re-checks the earlier phases: retention, an old upload rule found
        and disabled, nothing in flight or awaiting review (but the documents in
        ``ignore_documents``), the configuration counters removed, and the user
        pool in state. Then saves a plan and refuses it unless every resource in
        ``adopt.tf`` is imported in place, nothing at all is replaced, destroyed
        or forgotten, and no adopted resource (or setting resource on a kept
        bucket) makes one of the refused transitions in
        ``terraform_migrate.REFUSED_CHANGES``, unless named in ``accept_changes``
        as ``<address>:<rule>``. Every other in-place change to those resources
        is listed in ``details["review"]``.
        """
        from idp_sdk._core import terraform_deploy as tf
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        findings: List[str] = []

        retention = self.check_retention(terraform_dir, accept_expiry)
        findings += retention.findings
        events = self._boto("events")
        if not plan.upload_rules:
            findings.append(tm.NO_UPLOAD_RULE)
        for rule in tm.upload_rule_states(plan, events):
            if rule["state"] != "DISABLED":
                findings.append(
                    f"the old upload rule {rule['name']} is enabled; run `freeze`"
                )
        ddb = self._boto("dynamodb")
        if plan.tracking_table:
            findings += tm.scan_documents(
                plan.tracking_table, ddb, ignore_documents
            ).findings()
        else:
            findings.append(
                "the migration plan records no tracking table, so nothing can say "
                "whether documents are in flight"
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
        review: List[str] = []
        applied = False
        try:
            runner.plan(MIGRATION_PLAN_FILE)
            plan_json = runner.show_plan(MIGRATION_PLAN_FILE)
            counts, plan_findings, review = tm.check_adoption_plan(
                plan_json, plan, accept_changes
            )
            findings += plan_findings
            switches = tm.ttl_switches(plan_json)
            if switches:
                findings += tm.ttl_switch_findings(
                    switches,
                    lambda table: ddb.describe_time_to_live(TableName=table).get(
                        "TimeToLiveDescription", {}
                    ),
                    self._region(),
                )
            if apply and not findings:
                runner.apply_plan(MIGRATION_PLAN_FILE)
                applied = True
        finally:
            plan_path.unlink(missing_ok=True)
        summary = (
            f"Plan: {counts.get('import', 0)} to import, {counts.get('add', 0)} to add, "
            f"{counts.get('change', 0)} to change, {counts.get('destroy', 0)} to destroy."
        )
        notes = [] if applied else ["nothing was applied"]
        if review:
            notes.append(
                f"{len(review)} in-place change(s) to adopted resources and kept "
                "buckets' settings are allowed; read them (details.review, or below) "
                "before applying"
            )
        return TerraformMigrationStepResult(
            phase="apply",
            applied=applied,
            ok=not findings,
            changes=[summary],
            findings=findings,
            notes=notes,
            details={"counts": counts, "review": review},
        )

    # -- configuration restore ------------------------------------------------------------

    def restore_config(
        self, terraform_dir: Optional[str] = None, *, apply: bool = False
    ) -> TerraformMigrationStepResult:
        """Put back each kept profile the new deployment's first deploy replaced.

        That deploy saves the module's preset over every profile it ships (the
        ``default`` profile, at least) as a new revision, after recording what the
        profile held as the revision before it. This finds each profile in the
        ``fix-config`` backup whose published revision is such a deploy revision,
        and with ``apply`` restores the earlier one as a new revision (history is
        never rewritten, so the preset stays inspectable).
        """
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        self._load(directory)
        backups = sorted(
            self._migration_dir(directory).glob("configuration-table-backup-*.json")
        )
        if not backups:
            raise IDPConfigurationError(
                "no configuration-table backup in the migration directory; run "
                "`fix-config --apply` before the adoption"
            )
        profiles = tm.backed_up_profiles(json.loads(backups[-1].read_text()))
        name = self._client._require_stack(None)
        self._client.config._configure_config_env(name)
        from idp_common.config.configuration_manager import ConfigurationManager

        manager = ConfigurationManager(region=self._region())
        if not manager.revisions.enabled:
            return TerraformMigrationStepResult(
                phase="restore-config",
                applied=False,
                ok=False,
                findings=["the new deployment's revision history is not readable"],
            )
        changes: List[str] = []
        notes: List[str] = []
        findings: List[str] = []
        restore: List[Tuple[str, int]] = []
        for profile in profiles:
            decision = tm.restore_decision(manager.list_revisions(profile))
            if decision.outcome == tm.NO_PRE_HISTORY:
                findings.append(
                    f"{profile}: the new deployment recorded revisions for this "
                    "profile but none of what it held before the migration (the "
                    "first deploy's backfill of the pre-history revision failed), so "
                    "whether its content was replaced cannot be told from the "
                    f"history and there is no revision to restore. The old content "
                    f"is in the fix-config backup {backups[-1]}; compare the "
                    f"profile's Config#{profile} row there with the current one and "
                    "put it back by hand if it was replaced"
                )
                continue
            if decision.outcome == tm.EDITED:
                who = ", ".join(f"revision {n} by {by}" for n, by in decision.edited_by)
                notes.append(
                    f"{profile}: left alone, because it was changed after the first "
                    f"deploy ({who}); its content before the migration is revision "
                    f"{decision.revision}, which you can restore yourself if that "
                    "change did not mean to replace it"
                )
                continue
            if decision.outcome != tm.RESTORE or decision.revision is None:
                notes.append(f"{profile}: content kept; nothing to restore")
                continue
            restore.append((profile, decision.revision))
            changes.append(
                f"restore revision {decision.revision} of {profile} (its content "
                "before the migration), replacing the module preset the first "
                "deploy published"
            )
        # A finding is about one profile and cannot be cleared by a re-run (the
        # backfill does not happen twice), so it does not hold back the others.
        if apply:
            for profile, revision in restore:
                manager.restore_revision(
                    profile, revision, created_by="genai-idp-terraform-migration"
                )
        return TerraformMigrationStepResult(
            phase="restore-config",
            applied=apply and bool(restore),
            ok=not findings,
            changes=changes,
            findings=findings,
            notes=notes,
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
        resource; refuses an old directory whose state is not the one the plan
        was built from (another lineage) or holds none of the addresses to
        detach. With ``apply`` the old state is saved to a backup file first
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
        pulled = old.capture("state", "pull")
        try:
            lineage = json.loads(pulled).get("lineage") if pulled.strip() else None
        except ValueError:
            lineage = None
        if not plan.old_lineage or lineage != plan.old_lineage:
            findings.append(
                f"the state in {old.directory} has lineage {lineage!r}, and the "
                f"migration plan was built from lineage {plan.old_lineage!r}; this is "
                "not the old deployment's state"
            )
        old_list = set(old.state_list())
        present = [a for a in plan.detach_addresses if a in old_list]
        absent = [a for a in plan.detach_addresses if a not in old_list]
        if plan.detach_addresses and not present:
            findings.append(
                f"none of the {len(plan.detach_addresses)} addresses to detach is in "
                f"the state in {old.directory}: either they were detached already "
                "(check `terraform state list` there) or this is not the old "
                "deployment's directory"
            )
        backup: Optional[str] = None
        if apply and present and not findings:
            mdir = self._migration_dir(directory)
            mdir.mkdir(parents=True, exist_ok=True)
            path = mdir / f"old-state-backup-before-detach-{tm.timestamp()}.tfstate"
            tf.write_private(path, pulled)
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
        Refuses anything else: a state file, an empty or errored plan, a plan that
        does more than delete, or a plan of another deployment. Reads only.
        """
        from idp_sdk._core import terraform_migrate as tm

        directory = self._directory(terraform_dir)
        plan = self._load(directory)
        try:
            destroy = json.loads(Path(destroy_plan_path).read_text())
        except (OSError, ValueError) as e:
            raise IDPConfigurationError(f"cannot read {destroy_plan_path}: {e}") from e
        findings, details = tm.check_destroy_plan(destroy, plan)
        notes = []
        if details.get("recordedNotInDestroyPlan"):
            notes.append(
                f"{len(details['recordedNotInDestroyPlan'])} resource(s) the migration "
                "plan expects to be destroyed are not in this destroy plan (a "
                "targeted plan, or already gone); they would be left behind"
            )
        return TerraformMigrationStepResult(
            phase="check-destroy",
            applied=False,
            ok=not findings,
            findings=findings,
            notes=notes,
            details=details,
        )
