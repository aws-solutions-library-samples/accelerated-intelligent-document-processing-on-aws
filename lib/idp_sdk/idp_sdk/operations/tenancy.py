# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tenant isolation operations: declarations, access policies, audit and live check.

The declarations and every rule applied to them live in
``idp_common.config.tenants``; this namespace only reaches a stack's tables and
calls those functions, so the CLI, the API and the SDK refuse exactly the same
things.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from idp_sdk.exceptions import (
    IDPConfigurationError,
    IDPResourceNotFoundError,
    IDPValidationError,
)
from idp_sdk.models.tenancy import (
    TenantApplyResult,
    TenantAuditFinding,
    TenantAuditResult,
    TenantInfo,
    TenantListResult,
    TenantPlanAction,
    TenantPlanItem,
    TenantPolicyResult,
    TenantVerifyResult,
)

logger = logging.getLogger(__name__)

ISOLATION_PARAMETER = "TenantIsolationMode"
UNKNOWN_MODE = "Unknown"
# Bounded sample of the tracking table for the document check. The audit is a
# spot check, not a census, and a full scan of a busy table is the wrong cost for a
# command an operator runs interactively.
DEFAULT_DOCUMENT_SAMPLE = 500


def _to_info(entry: Mapping[str, Any]) -> TenantInfo:
    return TenantInfo(
        tenant_id=entry["tenantId"],
        prefixes=list(entry.get("prefixes") or []),
        profile_pattern=entry.get("profilePattern") or "",
        description=entry.get("description"),
        created_at=entry.get("createdAt"),
        created_by=entry.get("createdBy"),
        updated_at=entry.get("updatedAt"),
        updated_by=entry.get("updatedBy"),
    )


def _comparable(entry: Mapping[str, Any]) -> tuple:
    return (
        tuple(sorted(entry.get("prefixes") or [])),
        entry.get("profilePattern") or "",
        entry.get("description") or None,
    )


def _normalize_policy(value: Any) -> Any:
    """A policy document with order and single-element-list differences removed.

    S3 hands a stored policy back with its own spelling — a one-member principal
    list as a string, statements in any order — so a byte comparison would report
    drift on every access point.
    """
    if isinstance(value, dict):
        return {k: _normalize_policy(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [_normalize_policy(v) for v in value]
        if len(items) == 1:
            return items[0]
        return sorted(items, key=lambda v: repr(v))
    return value


class TenancyOperation:
    """Tenant isolation: ``client.tenancy``."""

    def __init__(self, client):
        self._client = client

    # ----- plumbing --------------------------------------------------------

    def _boto(self, service: str):
        import boto3

        return boto3.client(service, region_name=self._client._region)

    def _resources(self, stack_name: str, logical_ids) -> Dict[str, str]:
        wanted = set(logical_ids)
        found: Dict[str, str] = {}
        paginator = self._boto("cloudformation").get_paginator("list_stack_resources")
        for page in paginator.paginate(StackName=stack_name):
            for r in page.get("StackResourceSummaries", []):
                if r.get("LogicalResourceId") in wanted:
                    found[r["LogicalResourceId"]] = r.get("PhysicalResourceId")
        return found

    def _table(self, name: str):
        import boto3

        return boto3.resource("dynamodb", region_name=self._client._region).Table(name)

    def _config_table(self, stack_name: str):
        table = self._resources(stack_name, {"ConfigurationTable"}).get(
            "ConfigurationTable"
        )
        if not table:
            raise IDPResourceNotFoundError(
                f"ConfigurationTable not found in stack '{stack_name}'"
            )
        return self._table(table)

    def _stores(self, stack_name: str):
        from idp_common.config.prefix_mappings import PrefixMappingStore
        from idp_common.config.tenants import TenantStore

        table = self._config_table(stack_name)
        return TenantStore(table), PrefixMappingStore(table)

    def _stored_scopes(self, stack_name: str):
        """Every stored user scope on the stack, for the tenant-save check.

        Refuses rather than skips when the stack has no UsersTable: the check
        exists so that a tenant set cannot leave a user reaching two tenants, and a
        save that could not look is not one that passed.
        """
        from idp_common.config.tenants import stored_scopes

        users = self._resources(stack_name, {"UsersTable"}).get("UsersTable")
        if not users:
            raise IDPResourceNotFoundError(
                f"UsersTable not found in stack '{stack_name}', so existing user "
                f"scopes cannot be checked against the tenant set"
            )
        return stored_scopes(self._table(users))

    def isolation_mode(self, stack_name: Optional[str] = None) -> str:
        """The stack's ``TenantIsolationMode`` parameter, or ``Unknown``."""
        name = self._client._require_stack(stack_name)
        stacks = self._boto("cloudformation").describe_stacks(StackName=name)["Stacks"]
        for p in stacks[0].get("Parameters") or []:
            if p.get("ParameterKey") == ISOLATION_PARAMETER:
                return p.get("ParameterValue") or UNKNOWN_MODE
        return UNKNOWN_MODE

    # ----- declarations ------------------------------------------------------

    def list(self, stack_name: Optional[str] = None) -> TenantListResult:
        name = self._client._require_stack(stack_name)
        tenants, _ = self._stores(name)
        entries = tenants.list()
        return TenantListResult(
            tenants=[_to_info(e) for e in entries],
            count=len(entries),
            isolation_mode=self.isolation_mode(name),
        )

    def put(
        self,
        tenant_id: str,
        prefixes: Sequence[str],
        profile_pattern: str,
        *,
        description: Optional[str] = None,
        stack_name: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> TenantInfo:
        """Create or replace one declaration.

        Refused (``IDPValidationError``) when the declaration is malformed, overlaps
        another tenant, would put an existing prefix mapping across a tenant
        boundary, or would leave an existing user scope reaching more than one
        tenant — the same checks the API's ``putTenant`` applies.
        """
        from idp_common.config.tenants import (
            TenantConflict,
            mappings_rejection_reason,
            stored_scopes_rejection_reason,
        )

        name = self._client._require_stack(stack_name)
        tenants, mappings = self._stores(name)
        existing_mappings = mappings.list()
        scopes = self._stored_scopes(name)
        try:
            stored = tenants.put(
                tenant_id,
                list(prefixes),
                profile_pattern,
                description=description,
                actor=actor or "idp-cli",
                validate_against=lambda t: (
                    mappings_rejection_reason(existing_mappings, t)
                    or stored_scopes_rejection_reason(scopes, t)
                ),
            )
        except ValueError as e:
            raise IDPValidationError(str(e)) from e
        except TenantConflict as e:
            raise IDPValidationError(str(e)) from e
        return _to_info(stored)

    def delete(self, tenant_id: str, stack_name: Optional[str] = None) -> bool:
        """Remove one declaration. Returns whether it existed."""
        name = self._client._require_stack(stack_name)
        tenants, _ = self._stores(name)
        return tenants.delete(tenant_id)

    def apply(
        self,
        declarations: Sequence[Mapping[str, Any]],
        *,
        prune: bool = False,
        dry_run: bool = True,
        stack_name: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> TenantApplyResult:
        """Make the stack's tenants match ``declarations``.

        The whole plan is validated before anything is written — against the
        declarations as a set, and against the stack's existing prefix mappings and
        stored user scopes — so
        a file with one bad entry writes nothing. ``dry_run`` (the default) returns
        the plan without writing.
        """
        from idp_common.config.tenants import (
            mappings_rejection_reason,
            normalize_tenant,
            sort_tenants,
            stored_scopes_rejection_reason,
            tenant_rejection_reason,
        )

        name = self._client._require_stack(stack_name)
        store, mapping_store = self._stores(name)
        current = {t["tenantId"]: t for t in store.list()}
        wanted: Dict[str, Dict[str, Any]] = {}
        for raw in declarations:
            entry = normalize_tenant(
                {
                    "tenantId": raw.get("tenantId") or raw.get("tenant_id"),
                    "prefixes": raw.get("prefixes"),
                    "profilePattern": raw.get("profilePattern")
                    or raw.get("profile_pattern"),
                    "description": raw.get("description"),
                }
            )
            if entry["tenantId"] in wanted:
                raise IDPValidationError(
                    f"Tenant {entry['tenantId']!r} is declared twice."
                )
            wanted[entry["tenantId"]] = entry

        final = dict(current) if not prune else {}
        final.update(wanted)
        final_set = sort_tenants(list(final.values()))

        plan: List[TenantPlanItem] = []
        for tid in sorted(set(current) | set(wanted)):
            before = current.get(tid)
            after = wanted.get(tid)
            if after is None:
                action = TenantPlanAction.DELETE if prune else TenantPlanAction.KEEP
            elif before is None:
                action = TenantPlanAction.CREATE
            elif _comparable(before) == _comparable(after):
                action = TenantPlanAction.UNCHANGED
            else:
                action = TenantPlanAction.UPDATE
            error = None
            if after is not None:
                error = tenant_rejection_reason(
                    after, [t for t in final_set if t["tenantId"] != tid]
                )
            plan.append(
                TenantPlanItem(
                    tenant_id=tid,
                    action=action,
                    before=_to_info(before) if before else None,
                    after=_to_info(after) if after else None,
                    error=error,
                )
            )
        set_error = mappings_rejection_reason(
            mapping_store.list(), final_set
        ) or stored_scopes_rejection_reason(self._stored_scopes(name), final_set)
        if set_error:
            plan.append(
                TenantPlanItem(
                    tenant_id="*", action=TenantPlanAction.KEEP, error=set_error
                )
            )

        result = TenantApplyResult(plan=plan, applied=False)
        if dry_run or any(item.error for item in plan):
            return result
        # Deletions first, so a prefix moving from one tenant to another is not
        # refused as an overlap with the tenant it is leaving.
        for item in plan:
            if item.action == TenantPlanAction.DELETE:
                store.delete(item.tenant_id)
        # Then the writes, in passes: a prefix moving between two tenants that are
        # both being updated is an overlap until the tenant losing it has been
        # written, whichever order they are visited in. The whole plan was
        # validated as a set above, so each pass must make progress; a pass that
        # makes none means the stack changed underneath this command.
        pending = [
            item.after
            for item in plan
            if item.action in (TenantPlanAction.CREATE, TenantPlanAction.UPDATE)
            and item.after
        ]
        while pending:
            failed: List[TenantInfo] = []
            last_error: Optional[IDPValidationError] = None
            for after in pending:
                try:
                    self.put(
                        after.tenant_id,
                        after.prefixes,
                        after.profile_pattern,
                        description=after.description,
                        stack_name=name,
                        actor=actor,
                    )
                except IDPValidationError as e:
                    failed.append(after)
                    last_error = e
            if len(failed) == len(pending):
                raise IDPValidationError(
                    f"Applied part of the plan, then {len(failed)} tenant(s) could not "
                    f"be written: {last_error}. Re-run to see the remaining plan."
                )
            pending = failed
        result.applied = True
        return result

    # ----- access policies -----------------------------------------------------

    def _buckets(self, stack_name: str) -> Dict[str, str]:
        found = self._resources(stack_name, {"InputBucket", "OutputBucket"})
        if not found.get("InputBucket") or not found.get("OutputBucket"):
            raise IDPResourceNotFoundError(
                f"Input or Output bucket not found in stack '{stack_name}'"
            )
        return found

    def generate_policies(
        self, principals: Mapping[str, Any], stack_name: Optional[str] = None
    ) -> TenantPolicyResult:
        """A CloudFormation template of per-tenant S3 access points.

        ``principals`` maps tenant ID to ``{uploaders, consumers, kmsKeyArn}``.
        """
        import yaml

        from idp_sdk._core.tenancy_policies import (
            PrincipalsError,
            build_template,
            load_principals,
        )

        name = self._client._require_stack(stack_name)
        tenants, _ = self._stores(name)
        entries = tenants.list()
        if not entries:
            raise IDPConfigurationError(f"Stack '{name}' declares no tenants.")
        try:
            clean = load_principals(principals, [t["tenantId"] for t in entries])
        except PrincipalsError as e:
            raise IDPValidationError(str(e)) from e
        buckets = self._buckets(name)
        template, names, warnings = build_template(
            name, buckets["InputBucket"], buckets["OutputBucket"], entries, clean
        )
        warnings.append(
            "Each uploader and consumer also needs, in its own account: an identity "
            "policy allowing s3:PutObject or s3:GetObject/s3:ListBucket on its access "
            "point ARN, and kms:GenerateDataKey (uploaders) or kms:Decrypt (consumers) "
            "on the stack's CustomerManagedEncryptionKey, whose key policy must admit "
            "it. A principal that also holds a direct grant on the bucket is not "
            "confined by these access points."
        )
        mode = self.isolation_mode(name)
        if mode != "Strict":
            warnings.append(
                f"The stack runs with TenantIsolationMode={mode}: its bucket policies do "
                f"not delegate to access points, so these grant nothing until it is "
                f"redeployed with Strict."
            )
        return TenantPolicyResult(
            template=yaml.safe_dump(template, sort_keys=False, width=100),
            access_points=names,
            warnings=warnings,
        )

    # ----- audit ---------------------------------------------------------------

    def audit(
        self,
        stack_name: Optional[str] = None,
        *,
        principals: Optional[Mapping[str, Any]] = None,
        document_sample: int = DEFAULT_DOCUMENT_SAMPLE,
    ) -> TenantAuditResult:
        """Read-only report of what weakens tenant isolation on a stack."""
        from idp_common.config.tenants import (
            mapping_rejection_reason,
            scope_rejection_reason,
            tenant_for_key,
            tenant_for_profile,
        )
        from idp_sdk._core.tenancy_policies import uncovered_tenant_prefixes

        name = self._client._require_stack(stack_name)
        tenant_store, mapping_store = self._stores(name)
        tenants = tenant_store.list()
        mappings = mapping_store.list()
        mode = self.isolation_mode(name)
        result = TenantAuditResult(isolation_mode=mode, tenant_count=len(tenants))
        add = result.findings.append

        if mode != "Strict":
            add(
                TenantAuditFinding(
                    kind="isolation-mode",
                    subject=name,
                    detail=(
                        f"TenantIsolationMode is {mode}: unscoped non-admin users see "
                        f"every document, and ingest does not refuse documents that "
                        f"cross a tenant boundary."
                    ),
                )
            )
        if not tenants:
            result.notes.append(
                "No tenants are declared; the boundary checks have nothing to compare."
            )
            return result

        for m in mappings:
            reason = mapping_rejection_reason(m["prefix"], m["configProfile"], tenants)
            if reason:
                add(
                    TenantAuditFinding(
                        kind="crossing-mapping", subject=m["prefix"], detail=reason
                    )
                )
        for tid, prefix, governing in uncovered_tenant_prefixes(mappings, tenants):
            add(
                TenantAuditFinding(
                    kind="uncovered-prefix",
                    subject=prefix,
                    detail=(
                        f"Tenant {tid!r} has no mapping of its own at {prefix!r}; "
                        + (
                            f"ancestor mapping {governing!r} governs documents there."
                            if governing
                            else "documents there process under the active profile."
                        )
                    ),
                )
            )

        self._audit_users(name, tenants, result, scope_rejection_reason)
        self._audit_documents(
            name, tenants, result, tenant_for_key, tenant_for_profile, document_sample
        )
        if principals is not None:
            self._audit_access_points(name, tenants, principals, result)
        return result

    def _audit_users(self, stack_name, tenants, result, scope_rejection_reason) -> None:
        found = self._resources(stack_name, {"UsersTable", "UserPool"})
        if not found.get("UsersTable"):
            result.notes.append("No UsersTable on this stack; users were not examined.")
            return
        admins: set = set()
        if found.get("UserPool"):
            paginator = self._boto("cognito-idp").get_paginator("list_users_in_group")
            for page in paginator.paginate(
                UserPoolId=found["UserPool"], GroupName="Admin"
            ):
                for user in page.get("Users", []):
                    for attr in user.get("Attributes", []):
                        if attr.get("Name") == "email":
                            admins.add(attr.get("Value", "").lower())
        else:
            result.notes.append(
                "No user pool found; Admin membership taken from UsersTable persona only."
            )
        table = self._table(found["UsersTable"])
        kwargs: Dict[str, Any] = {
            "ProjectionExpression": "PK, email, persona, allowedConfigVersions",
        }
        while True:
            page = table.scan(**kwargs)
            for row in page.get("Items", []):
                # The host keys a user's row `PK = USER#<id>` and stores `userId`
                # unprefixed; `SUB#` pointer items carry no scope of their own.
                if not str(row.get("PK", "")).startswith("USER#"):
                    continue
                email = str(row.get("email") or "")
                if row.get("persona") == "Admin" or email.lower() in admins:
                    continue
                scope = row.get("allowedConfigVersions")
                if not scope:
                    result.findings.append(
                        TenantAuditFinding(
                            kind="unscoped-user",
                            subject=email,
                            detail=(
                                f"{row.get('persona')} with no configuration scope: sees "
                                f"every tenant's documents unless the stack is Strict."
                            ),
                        )
                    )
                    continue
                reason = scope_rejection_reason(scope, tenants)
                if reason:
                    result.findings.append(
                        TenantAuditFinding(
                            kind="cross-tenant-scope", subject=email, detail=reason
                        )
                    )
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def _audit_documents(
        self, stack_name, tenants, result, tenant_for_key, tenant_for_profile, sample
    ) -> None:
        from boto3.dynamodb.conditions import Attr

        found = self._resources(stack_name, {"TrackingTable"})
        if not found.get("TrackingTable"):
            result.notes.append("No TrackingTable found; documents were not examined.")
            return
        table = self._table(found["TrackingTable"])
        kwargs: Dict[str, Any] = {
            # The document row itself (SK "none"). The same PK also holds one
            # history row per processing run, which repeats the profile, and a
            # deleted document leaves a row with no status; neither is a document.
            "FilterExpression": Attr("PK").begins_with("doc#") & Attr("SK").eq("none"),
            "ProjectionExpression": "PK, ConfigVersion, ObjectStatus, ConfigAssignmentError",
        }
        examined = 0
        while examined < sample:
            # filtered-scan-ok: a deliberately bounded spot check of `sample` documents, paginated on LastEvaluatedKey
            page = table.scan(**kwargs)
            for item in page.get("Items", []):
                if not item.get("ObjectStatus") or item.get("ConfigAssignmentError"):
                    # No status: not a live document. A refusal at ingest is the
                    # boundary working, and was never processed under any profile.
                    continue
                examined += 1
                key = str(item["PK"])[len("doc#") :]
                profile = item.get("ConfigVersion")
                kt = tenant_for_key(key, tenants)
                pt = tenant_for_profile(profile, tenants)
                if (kt or {}).get("tenantId") != (pt or {}).get("tenantId"):
                    result.findings.append(
                        TenantAuditFinding(
                            kind="cross-tenant-document",
                            subject=key,
                            detail=(
                                f"Key belongs to {(kt or {}).get('tenantId') or 'no tenant'} "
                                f"but was processed under {profile!r}, which belongs to "
                                f"{(pt or {}).get('tenantId') or 'no tenant'}."
                            ),
                        )
                    )
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        result.notes.append(
            f"Examined {examined} tracked documents (sample bound {sample})."
        )

    def _audit_access_points(self, stack_name, tenants, principals, result) -> None:
        import json

        from idp_sdk._core.tenancy_policies import (
            PrincipalsError,
            access_point_policy,
            literal_arn,
            load_principals,
            planned_access_points,
        )

        try:
            clean = load_principals(principals, [t["tenantId"] for t in tenants])
        except PrincipalsError as e:
            raise IDPValidationError(str(e)) from e
        identity = self._boto("sts").get_caller_identity()
        account = identity["Account"]
        partition = identity["Arn"].split(":")[1]
        region = self._client._region or self._boto("s3control").meta.region_name
        s3control = self._boto("s3control")
        planned, _ = planned_access_points(stack_name, tenants, clean)
        for ap in planned:
            expected = access_point_policy(
                ap["name"],
                ap["prefixes"],
                ap["principals"],
                ap["direction"],
                literal_arn(ap["name"], partition, region, account),
                ap["kmsKeyArn"],
            )
            try:
                live = json.loads(
                    s3control.get_access_point_policy(
                        AccountId=account, Name=ap["name"]
                    )["Policy"]
                )
            except Exception as e:  # noqa: BLE001 - absent and unreadable are both findings
                code = (
                    getattr(e, "response", {})
                    .get("Error", {})
                    .get("Code", type(e).__name__)
                )
                result.findings.append(
                    TenantAuditFinding(
                        kind="access-point-missing",
                        subject=ap["name"],
                        detail=f"Could not read the access point policy ({code}).",
                    )
                )
                continue
            if _normalize_policy(live) != _normalize_policy(expected):
                result.findings.append(
                    TenantAuditFinding(
                        kind="access-point-drift",
                        subject=ap["name"],
                        detail=(
                            f"Tenant {ap['tenantId']!r} {ap['direction']} policy differs "
                            f"from what generate-policies produces for the current "
                            f"declarations and principals."
                        ),
                    )
                )

    # ----- live check ----------------------------------------------------------

    def verify(
        self,
        document: bytes,
        document_name: str = "tenancy-verify.pdf",
        *,
        stack_name: Optional[str] = None,
        tenant_ids: Optional[Sequence[str]] = None,
        timeout_seconds: int = 240,
    ) -> TenantVerifyResult:
        """Live cross-tenant check. Creates and removes throwaway users and documents.

        See ``idp_sdk._core.tenancy_verify`` for exactly what is asserted.
        """
        from idp_sdk._core import ui_api
        from idp_sdk._core.tenancy_verify import run_verify

        name = self._client._require_stack(stack_name)
        tenant_store, mapping_store = self._stores(name)
        tenants = tenant_store.list()
        mappings = mapping_store.list()
        mode = self.isolation_mode(name)
        buckets = self._buckets(name)
        ctx = ui_api.resolve_ui_context(
            name, self._client._region, self._boto("cloudformation")
        )
        cognito = self._boto("cognito-idp")
        s3 = self._boto("s3")

        def make_admin(email: str, password: str) -> str:
            ui_api.create_cognito_user(cognito, ctx.user_pool, email, password, "Admin")
            return ui_api.id_token(cognito, ctx, email, password)

        def make_password(email: str, password: str) -> str:
            ui_api.set_password(cognito, ctx.user_pool, email, password)
            return ui_api.id_token(cognito, ctx, email, password)

        def delete_user(email: str) -> None:
            try:
                cognito.admin_delete_user(UserPoolId=ctx.user_pool, Username=email)
            except cognito.exceptions.UserNotFoundException:
                pass

        with ui_api.AdminAuthFlow(ctx, cognito):
            return run_verify(
                isolation_mode=mode,
                tenants=tenants,
                mappings=mappings,
                document=document,
                document_name=document_name,
                input_bucket=buckets["InputBucket"],
                api=lambda field, args, token: ui_api.call(
                    ctx.api_base, field, args, token
                ),
                make_admin=make_admin,
                make_password=make_password,
                delete_cognito_user=delete_user,
                s3=s3,
                tenant_ids=tenant_ids,
                timeout_seconds=timeout_seconds,
            )
