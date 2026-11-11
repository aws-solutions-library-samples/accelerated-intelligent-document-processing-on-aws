# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Move a ``genai-idp-terraform`` deployment onto the built-in Terraform module in place.

``awslabs/genai-idp-terraform`` is a separate, hand-written Terraform implementation
of the accelerator. This module holds the logic behind
``idp-cli terraform migrate-from-genai-idp-terraform``: the new deployment (this
repository's published module, called as ``module "idp"``) takes over the old
deployment's buckets, tables, KMS key and Cognito user pool with ``import {}``
blocks, and everything else is created afresh and destroyed with the old
deployment.

The work is split into phases, and every phase that changes something is a dry run
unless asked to apply:

``plan``           old state -> ``adopt.tf``, the addresses to detach from the old
                   state, and a report of what is recreated or kept unmanaged.
``check_retention`` the module's S3 expiry rules against the oldest object in each
                   adopted bucket.
``freeze``         disable the old deployment's upload rule; report in-flight
                   documents in the tracking table.
``fix_config``     back up, then remove ``LatestRevision``/``PublishedRevision``
                   from the old ``Config#...`` rows.
``adopt_user_pool`` ``terraform import`` the user pool, then rewrite its ``schema``
                   in state to what the module declares.
``check_apply_plan`` the full adoption plan: every adopted resource imported, none
                   replaced or destroyed.
``detach``         remove the adopted addresses from the old state (backup first).
``check_destroy``  the old deployment's destroy plan deletes nothing adopted.

The functions here are pure where they can be (they take parsed JSON and return
findings) so the unit tests can drive them with fixtures; the AWS calls take a
boto3 client argument.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from idp_sdk.exceptions import IDPConfigurationError

MODULE_CALL = "idp"
MODULE_PREFIX = f"module.{MODULE_CALL}."
MIGRATION_DIR = "genai-idp-terraform-migration"
PLAN_FILE = "migration-plan.json"
DETACH_FILE = "detach-addresses.txt"
ADOPT_FILE = "adopt.tf"
ADOPT_MARKER = "# Written by `idp-cli terraform migrate-from-genai-idp-terraform plan`."
PLAN_FORMAT = 2

# Verdicts, one per resource kind.
ADOPT = "adopt"  # imported into the module with an `import {}` block
ADOPT_AFTER_FIX = "adopt-after-fix"  # imported, after a one-time data fix
ADOPT_BY_PARAMETER = "adopt-by-parameter"  # named in a stack parameter, not imported
RECREATE = "recreate"  # the module creates its own; destroyed with the old deployment
KEEP_UNMANAGED = "keep-unmanaged"  # left in the account, managed by neither
UNSUPPORTED = "unsupported"  # no tested path: the plan refuses
SETTING = "bucket-or-table-setting"  # follows the bucket/table/pool it belongs to

MEASURED = "measured"
INFERRED = "inferred"

# The user pool groups the module declares, by group name.
MODULE_GROUPS = {
    "Admin": "awscc_cognito_user_pool_group.admin_group",
    "Author": "awscc_cognito_user_pool_group.author_group",
    "Reviewer": "awscc_cognito_user_pool_group.reviewer_group",
    "Viewer": "awscc_cognito_user_pool_group.viewer_group",
}
ADMIN_USER_ADDRESS = "aws_cognito_user.admin_user"
ADMIN_GROUP_ATTACHMENT_ADDRESS = (
    "aws_cognito_user_in_group.admin_user_to_group_attachment"
)
USER_POOL_ADDRESS = "aws_cognito_user_pool.user_pool"

# Old-state types that hold data or identity. An instance of one of these that no
# entry in KINDS matches is refused, never skipped.
STATEFUL_TYPES = frozenset(
    {
        "aws_s3_bucket",
        "awscc_s3_bucket",
        "aws_dynamodb_table",
        "awscc_dynamodb_table",
        "aws_dynamodb_table_replica",
        "aws_dynamodb_global_table",
        "aws_kms_key",
        "awscc_kms_key",
        "aws_kms_replica_key",
        "aws_kms_external_key",
        "aws_kms_replica_external_key",
        "aws_cognito_user_pool",
        "aws_cognito_user",
        "aws_cognito_user_group",
        "aws_cognito_user_pool_domain",
        "aws_cognito_identity_provider",
        "aws_opensearchserverless_collection",
        "opensearch_index",
        "aws_bedrockagent_knowledge_base",
        "aws_bedrockagent_data_source",
        "awscc_bedrock_data_automation_project",
        "aws_secretsmanager_secret",
        "aws_efs_file_system",
        "aws_s3vectors_vector_bucket",
        "aws_s3vectors_index",
        "aws_glue_catalog_database",
        "aws_backup_vault",
        "aws_backup_plan",
        "aws_backup_selection",
    }
)

# Old-state types known to be safe to destroy with the old deployment: they hold no
# data the new deployment needs, and destroying one runs nothing against a kept
# resource. Derived from every resource type upstream tag v0.6.9-tf.0 declares
# (its `resource` blocks, all modules), less the stateful ones above and the ones
# that need a decision this command cannot make (a VPC and its parts, which the new
# deployment may have been pointed at; an `aws_cloudformation_stack`, whose custom
# resources run on delete). A type in neither this set nor STATEFUL_TYPES, and not
# handled as a setting below, is refused by `plan`, naming it.
#
# `null_resource` and `terraform_data` are here because upstream's only destroy-time
# provisioner (`lambda-layer-codebuild-idp`'s `cleanup_on_destroy`) deletes local
# temporary files and nothing in AWS; a destroy-time provisioner a fork added is not
# visible in state.
RECREATE_SAFE_TYPES = frozenset(
    {
        "aws_api_gateway_account",
        "aws_api_gateway_authorizer",
        "aws_api_gateway_deployment",
        "aws_api_gateway_gateway_response",
        "aws_api_gateway_integration",
        "aws_api_gateway_integration_response",
        "aws_api_gateway_method",
        "aws_api_gateway_method_response",
        "aws_api_gateway_method_settings",
        "aws_api_gateway_resource",
        "aws_api_gateway_rest_api",
        "aws_api_gateway_stage",
        "aws_appautoscaling_policy",
        "aws_appautoscaling_target",
        "aws_cloudfront_distribution",
        "aws_cloudfront_origin_access_control",
        "aws_cloudfront_response_headers_policy",
        "aws_cloudwatch_event_rule",
        "aws_cloudwatch_event_target",
        "aws_cloudwatch_log_group",
        "aws_codebuild_project",
        "aws_cognito_identity_pool",
        "aws_cognito_identity_pool_roles_attachment",
        "aws_cognito_user_pool_client",
        "aws_ecr_repository",
        "aws_glue_catalog_table",
        "aws_glue_crawler",
        "aws_glue_security_configuration",
        "aws_iam_policy",
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        "aws_lambda_event_source_mapping",
        "aws_lambda_function",
        "aws_lambda_function_url",
        "aws_lambda_layer_version",
        "aws_lambda_permission",
        "aws_opensearchserverless_access_policy",
        "aws_opensearchserverless_security_policy",
        "aws_sagemaker_endpoint",
        "aws_sagemaker_endpoint_configuration",
        "aws_sagemaker_model",
        "aws_sfn_state_machine",
        "aws_sqs_queue",
        "aws_sqs_queue_policy",
        "aws_ssm_parameter",
        "aws_wafv2_ip_set",
        "aws_wafv2_web_acl",
        "aws_wafv2_web_acl_association",
        "docker_image",
        "docker_registry_image",
        "local_file",
        "null_resource",
        "random_id",
        "random_string",
        "terraform_data",
        "time_sleep",
    }
)

# Settings of a user pool that are separate resources. Destroying one strips it from
# the pool, so on the adopted pool each is detached (kept, managed by neither).
POOL_SETTING_TYPES = frozenset(
    {
        "aws_cognito_user_pool_ui_customization",
        "aws_cognito_resource_server",
        "aws_cognito_risk_configuration",
        "aws_cognito_managed_login_branding",
    }
)
# Kept with the BDA project, which names them.
BDA_MEMBER_TYPES = frozenset({"awscc_bedrock_blueprint"})

# Settings that are separate Terraform resources but live on a bucket, a table or
# a user pool. Destroying one strips that setting from its parent, so on an adopted
# parent each is detached from the old state rather than destroyed; on a recreated
# parent each is destroyed with it. The value is the attribute naming the parent.
BUCKET_SETTING_ATTR = "bucket"
TABLE_SETTING_TYPES = {
    "aws_dynamodb_table_item": "table_name",
    "aws_dynamodb_resource_policy": "resource_arn",
    "aws_dynamodb_contributor_insights": "table_name",
    "aws_dynamodb_kinesis_streaming_destination": "table_name",
}
KEY_SETTING_TYPES = {"aws_kms_key_policy": "key_id", "aws_kms_grant": "key_id"}
# Pool members that keep a user signing in; detached when their pool is adopted.
POOL_MEMBER_TYPES = frozenset(
    {"aws_cognito_user", "aws_cognito_user_in_group", "aws_cognito_user_group"}
)


def _is_bucket_setting(tf_type: str) -> bool:
    return tf_type.startswith("aws_s3_bucket_") or tf_type == "aws_s3_object"


@dataclass(frozen=True)
class ResourceKind:
    """One kind of resource in a ``genai-idp-terraform`` state, and its verdict.

    ``old_type``/``old_name`` are the resource's type and name in the old
    configuration; ``module`` (a module call name, without instance keys) narrows
    the match when the same name appears in more than one module. ``new_address``
    is the module-relative address in this repository's module.
    """

    kind: str
    old_type: str
    old_name: str
    verdict: str
    evidence: str
    note: str
    new_address: Optional[str] = None
    module: Optional[str] = None
    parameter: Optional[str] = None


KINDS: Tuple[ResourceKind, ...] = (
    # --- Buckets ---------------------------------------------------------------
    ResourceKind(
        "input_bucket",
        "aws_s3_bucket",
        "input_bucket",
        ADOPT,
        MEASURED,
        "Documents to process. The module adds a DataRetentionInDays expiry rule.",
        "aws_s3_bucket.input_bucket",
    ),
    ResourceKind(
        "output_bucket",
        "aws_s3_bucket",
        "output_bucket",
        ADOPT,
        MEASURED,
        "Processing results. The module adds a DataRetentionInDays expiry rule.",
        "aws_s3_bucket.output_bucket",
    ),
    ResourceKind(
        "working_bucket",
        "aws_s3_bucket",
        "working_bucket",
        ADOPT,
        MEASURED,
        "Intermediate artifacts. The module adds a LogRetentionDays expiry rule.",
        "aws_s3_bucket.working_bucket",
    ),
    ResourceKind(
        "evaluation_baseline_bucket",
        "aws_s3_bucket",
        "evaluation_baseline_bucket",
        ADOPT,
        MEASURED,
        "Evaluation baselines.",
        "aws_s3_bucket.evaluation_baseline_bucket",
    ),
    ResourceKind(
        "test_set_bucket",
        "aws_s3_bucket",
        "test_sets",
        ADOPT,
        MEASURED,
        "Test-set documents.",
        "aws_s3_bucket.test_set_bucket",
        module="processing_environment_api",
    ),
    ResourceKind(
        "discovery_bucket",
        "aws_s3_bucket",
        "discovery_bucket",
        ADOPT,
        INFERRED,
        "Discovery uploads. Not adopted in the live test; same role and shape.",
        "aws_s3_bucket.discovery_bucket",
        module="discovery",
    ),
    ResourceKind(
        "logging_bucket",
        "aws_s3_bucket",
        "this",
        ADOPT,
        INFERRED,
        "S3 access logs. Not adopted in the live test. The module's expiry "
        "rule on this bucket is a fixed 180 days.",
        "aws_s3_bucket.logging_bucket",
        module="logging_bucket",
    ),
    ResourceKind(
        "reporting_bucket",
        "aws_s3_bucket",
        "reporting_bucket",
        ADOPT_BY_PARAMETER,
        INFERRED,
        "Reporting data. Kept by naming it in ReportingBucketName, so the module "
        "creates no reporting bucket of its own and adds no lifecycle rule.",
        parameter="ReportingBucketName",
    ),
    ResourceKind(
        "assets_bucket",
        "aws_s3_bucket",
        "assets_bucket",
        RECREATE,
        MEASURED,
        "Build artifacts of the old deployment only.",
        module="assets_bucket",
    ),
    ResourceKind(
        "web_app_bucket",
        "aws_s3_bucket",
        "web_app_bucket",
        RECREATE,
        MEASURED,
        "The built web UI; the module builds its own.",
        module="web_ui",
    ),
    ResourceKind(
        "finetuning_data_bucket",
        "aws_s3_bucket",
        "finetuning_data",
        UNSUPPORTED,
        INFERRED,
        "Fine-tuning data has no tested migration path. Copy what you need "
        "elsewhere and empty the bucket, or remove it from the old state "
        "yourself, before planning.",
    ),
    # --- Tables ----------------------------------------------------------------
    ResourceKind(
        "tracking_table",
        "aws_dynamodb_table",
        "tracking_table",
        ADOPT,
        MEASURED,
        "Document tracking. The module's second index builds on adoption.",
        "aws_dynamodb_table.tracking_table",
    ),
    ResourceKind(
        "configuration_table",
        "aws_dynamodb_table",
        "configuration_table",
        ADOPT_AFTER_FIX,
        MEASURED,
        "Configuration profiles. Run `fix-config` before the first apply, or the "
        "first deploy writes the default configuration over yours.",
        "aws_dynamodb_table.configuration_table",
    ),
    ResourceKind(
        "users_table",
        "aws_dynamodb_table",
        "users",
        ADOPT,
        MEASURED,
        "Users and their per-user scope.",
        "aws_dynamodb_table.users_table",
        module="rbac",
    ),
    ResourceKind(
        "chat_sessions_table",
        "aws_dynamodb_table",
        "agent_chat_sessions",
        ADOPT,
        MEASURED,
        "Agent chat sessions.",
        "aws_dynamodb_table.chat_sessions_table",
    ),
    ResourceKind(
        "chat_messages_table",
        "aws_dynamodb_table",
        "agent_chat_messages",
        ADOPT,
        INFERRED,
        "Agent chat messages; same keys as the module's table.",
        "aws_dynamodb_table.chat_messages_table",
    ),
    ResourceKind(
        "chat_memory_table",
        "aws_dynamodb_table",
        "agent_chat_memory",
        ADOPT,
        INFERRED,
        "Chat memory; same keys as the module's table.",
        "aws_dynamodb_table.id_helper_chat_memory_table",
    ),
    ResourceKind(
        "chat_document_sessions_table",
        "aws_dynamodb_table",
        "chat_document_sessions",
        ADOPT,
        INFERRED,
        "Document-chat sessions; same key as the module's table.",
        "module.apiresolverstack.aws_dynamodb_table.chat_document_sessions_table",
    ),
    ResourceKind(
        "agent_table",
        "aws_dynamodb_table",
        "agent_jobs",
        ADOPT,
        INFERRED,
        "Agent jobs. Its time-to-live attribute is `expiresAfter`; the module "
        "sets `ExpiresAfter`, so rows written before the move do not expire.",
        "aws_dynamodb_table.agent_table",
    ),
    ResourceKind(
        "discovery_tracking_table",
        "aws_dynamodb_table",
        "discovery_tracking",
        RECREATE,
        MEASURED,
        "Different key schema from the module's table; discovery job history "
        "is not carried over.",
    ),
    ResourceKind(
        "concurrency_table",
        "aws_dynamodb_table",
        "concurrency_table",
        RECREATE,
        MEASURED,
        "Holds only the in-flight counter; drained by `freeze`.",
    ),
    ResourceKind(
        "installed_features_table",
        "aws_dynamodb_table",
        "installed_features",
        RECREATE,
        INFERRED,
        "Features installed on the old deployment are not carried over.",
    ),
    ResourceKind(
        "test_sets_table",
        "aws_dynamodb_table",
        "test_sets",
        RECREATE,
        INFERRED,
        "The module has no table of this shape, so test-set definitions are not "
        "carried over (their documents stay in the adopted test-set bucket). "
        "Export the table first if you need them.",
    ),
    # --- Key -------------------------------------------------------------------
    ResourceKind(
        "encryption_key",
        "aws_kms_key",
        "encryption_key",
        ADOPT,
        MEASURED,
        "Encrypts the adopted buckets and tables. The module's key policy "
        "replaces the old one.",
        "awscc_kms_key.customer_managed_encryption_key",
    ),
    # --- Cognito ---------------------------------------------------------------
    ResourceKind(
        "user_pool",
        "aws_cognito_user_pool",
        "user_pool",
        ADOPT,
        MEASURED,
        "Users keep their passwords. Imported by `adopt-user-pool`, not by "
        "adopt.tf, because its schema in state has to be rewritten before "
        "the first apply. Usernames stay case-sensitive.",
        USER_POOL_ADDRESS,
    ),
    ResourceKind(
        "user_pool_group",
        "aws_cognito_user_group",
        "rbac",
        ADOPT,
        MEASURED,
        "A role group; adopted when the module declares a group of that name, "
        "otherwise kept in the pool unmanaged.",
        module="rbac",
    ),
    ResourceKind(
        "admin_group",
        "aws_cognito_user_group",
        "admin_group",
        ADOPT,
        INFERRED,
        "The Admin group of a deployment without RBAC.",
    ),
    ResourceKind(
        "admin_user",
        "aws_cognito_user",
        "admin_user",
        ADOPT,
        INFERRED,
        "Adopted as the module's admin user when its username is the admin "
        "email you pass, otherwise kept in the pool unmanaged.",
    ),
    ResourceKind(
        "user_pool_client",
        "aws_cognito_user_pool_client",
        "user_pool_client",
        RECREATE,
        MEASURED,
        "The module creates its own app client.",
    ),
    ResourceKind(
        "identity_pool",
        "aws_cognito_identity_pool",
        "identity_pool",
        RECREATE,
        MEASURED,
        "The module creates its own identity pool.",
    ),
    ResourceKind(
        "hosted_ui_domain",
        "aws_cognito_user_pool_domain",
        "hosted_ui",
        UNSUPPORTED,
        INFERRED,
        "A user pool has at most one domain and the module creates its own, so "
        "the old hosted-UI domain would block the apply. Not handled by this "
        "command yet.",
    ),
    # --- Knowledge base and BDA ------------------------------------------------
    ResourceKind(
        "kb_collection",
        "aws_opensearchserverless_collection",
        "knowledge_base_collection",
        RECREATE,
        MEASURED,
        "The module's knowledge base uses S3 Vectors; re-sync it from the "
        "adopted output bucket.",
    ),
    ResourceKind(
        "kb_index",
        "opensearch_index",
        "knowledge_base_index",
        RECREATE,
        MEASURED,
        "Part of the old OpenSearch knowledge base.",
    ),
    ResourceKind(
        "knowledge_base",
        "aws_bedrockagent_knowledge_base",
        "knowledge_base",
        RECREATE,
        MEASURED,
        "Recreated on S3 Vectors.",
    ),
    ResourceKind(
        "kb_data_source",
        "aws_bedrockagent_data_source",
        "knowledge_base_data_source",
        RECREATE,
        MEASURED,
        "Part of the old knowledge base.",
    ),
    ResourceKind(
        "bda_project",
        "awscc_bedrock_data_automation_project",
        "bda_project",
        KEEP_UNMANAGED,
        MEASURED,
        "The `bda` configuration row names this project, so it is kept, and "
        "managed by neither deployment.",
    ),
    ResourceKind(
        "reporting_database",
        "aws_glue_catalog_database",
        "reporting",
        RECREATE,
        MEASURED,
        "Catalog metadata only; the module creates its own database "
        "over the reporting bucket.",
    ),
)

# Types destroyed with the old deployment and replaced by the module's own, when
# no KINDS entry says otherwise. Anything not stateful falls here too.
GENERIC_RECREATE_NOTE = "Destroyed with the old deployment; the module creates its own."


# ---------------------------------------------------------------------------
# The old state
# ---------------------------------------------------------------------------


@dataclass
class OldInstance:
    """One resource instance in the old state."""

    address: str
    type: str
    name: str
    modules: Tuple[str, ...]
    attributes: Dict[str, Any]


def _index_suffix(key: Any) -> str:
    if key is None:
        return ""
    if isinstance(key, int):
        return f"[{key}]"
    return "[" + json.dumps(key) + "]"


_MODULE_SEGMENT = re.compile(
    r'module\.([A-Za-z0-9_-]+)(?:\[(?:\d+|"(?:[^"\\]|\\.)*")\])?'
)


def load_old_state(data: Dict[str, Any]) -> List[OldInstance]:
    """Managed instances of a ``terraform.tfstate`` (or ``terraform state pull``).

    Refuses ``terraform show -json`` output, whose shape differs and which drops
    nothing this needs but is easy to confuse with it.
    """
    if "format_version" in data and "values" in data:
        raise IDPConfigurationError(
            "this is `terraform show -json` output; pass the state itself, from "
            "`terraform state pull` in the old deployment's directory, or its "
            "terraform.tfstate file"
        )
    if data.get("version") != 4 or not isinstance(data.get("resources"), list):
        raise IDPConfigurationError(
            "not a Terraform state file (expected a version 4 state with a "
            "`resources` list, as `terraform state pull` prints)"
        )
    if not data.get("lineage"):
        raise IDPConfigurationError(
            "this state has no lineage, which `terraform state pull` always "
            "prints; the later phases use it to recognise the old deployment's "
            "state, so pass the state itself"
        )
    out: List[OldInstance] = []
    for res in data["resources"]:
        if res.get("mode") != "managed":
            continue
        module = res.get("module") or ""
        base = (module + "." if module else "") + f"{res['type']}.{res['name']}"
        modules = tuple(m.group(1) for m in _MODULE_SEGMENT.finditer(module))
        for inst in res.get("instances") or []:
            out.append(
                OldInstance(
                    address=base + _index_suffix(inst.get("index_key")),
                    type=res["type"],
                    name=res["name"],
                    modules=modules,
                    attributes=inst.get("attributes") or {},
                )
            )
    if not any(
        i.type == "aws_dynamodb_table" and i.name == "tracking_table" for i in out
    ):
        raise IDPConfigurationError(
            "this state has no tracking table, so it is not a genai-idp-terraform "
            "deployment this command knows how to move"
        )
    return out


def _match_kind(inst: OldInstance) -> Optional[ResourceKind]:
    candidates = [
        k
        for k in KINDS
        if k.old_type == inst.type
        and k.old_name == inst.name
        and (k.module is None or k.module in inst.modules)
    ]
    if not candidates:
        return None
    # A module-qualified entry beats an unqualified one.
    candidates.sort(key=lambda k: k.module is None)
    return candidates[0]


# ---------------------------------------------------------------------------
# The new module's source
# ---------------------------------------------------------------------------


@dataclass
class LifecycleRules:
    """One ``aws_s3_bucket_lifecycle_configuration`` in the module, as read.

    ``target`` is the root-module ``aws_s3_bucket`` resource name the rule's
    ``bucket`` resolves to, or ``bucket_name`` the literal bucket name it resolves
    to through a variable; both are None when the expression could not be
    resolved, and then ``bucket_expr`` says what it was.
    """

    resource: str  # display address, module-relative
    scope: str
    bucket_expr: str
    target: Optional[str] = None
    bucket_name: Optional[str] = None
    expiry: List[str] = field(default_factory=list)
    noncurrent: List[str] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)


_LIFECYCLE = re.compile(
    r'^resource "aws_s3_bucket_lifecycle_configuration" "([^"]+)" \{\n(.*?)^\}\n',
    re.S | re.M,
)
_BUCKET_REF = re.compile(r"aws_s3_bucket\.([A-Za-z0-9_-]+)(?:\[0\])?\.(?:id|bucket)")
_EXPIRATION = re.compile(r"(?<![A-Za-z_])expiration \{(.*?)\}", re.S)
_NONCURRENT = re.compile(r"noncurrent_version_expiration \{(.*?)\}", re.S)


@dataclass
class ModuleSource:
    """What the migration reads from the installed module's ``.tf`` files."""

    root: Path
    texts: Dict[str, str] = field(
        default_factory=dict
    )  # module path ("" = root, "a.b" = module.a.module.b) -> text
    sources: Dict[str, str] = field(default_factory=dict)  # module path -> directory
    unread: List[str] = field(default_factory=list)  # calls whose source is not local

    @classmethod
    def read(cls, root: Path) -> "ModuleSource":
        src = cls(root=Path(root))
        src._read_scope("", src.root)
        return src

    def _read_scope(self, scope: str, directory: Path) -> None:
        if scope and not directory.is_dir():
            self.unread.append(scope)
        self.texts[scope] = _read_tf(directory)
        for m in re.finditer(
            r'^module "([^"]+)" \{\n(.*?)^\}\n', self.texts[scope], re.S | re.M
        ):
            child = f"{scope}.{m.group(1)}" if scope else m.group(1)
            s = re.search(r'^\s*source\s*=\s*"\./([^"]+)"', m.group(2), re.M)
            if s:
                self.sources[child] = str(directory / s.group(1))
                self._read_scope(child, directory / s.group(1))
            else:
                self.unread.append(child)

    def _block(
        self, text: str, kind: str, a: str, b: Optional[str] = None
    ) -> Optional[str]:
        head = f'{kind} "{a}"' + (f' "{b}"' if b else "") + " {\n"
        start = text.find(head)
        if start < 0:
            return None
        end = text.find("\n}\n", start)
        return text[start + len(head) : end if end >= 0 else len(text)]

    def resource_address(self, address: str) -> str:
        """``address`` with ``[0]`` added wherever the module counts the resource
        or a module call. Refuses an address the module does not declare."""
        parts = address.split(".")
        out: List[str] = []
        scope = ""
        while parts and parts[0] == "module":
            call = parts[1]
            body = self._block(self.texts.get(scope, ""), "module", call)
            if body is None:
                raise IDPConfigurationError(
                    f"the module declares no module call {call!r}"
                )
            out.append(f"module.{call}" + ("[0]" if _counted(body) else ""))
            scope = f"{scope}.{call}" if scope else call
            parts = parts[2:]
        tf_type, name = parts
        body = self._block(self.texts.get(scope, ""), "resource", tf_type, name)
        if body is None:
            raise IDPConfigurationError(
                f"the installed module declares no {address}; it may be a different "
                "release from the one this command was written against"
            )
        out.append(f"{tf_type}.{name}" + ("[0]" if _counted(body) else ""))
        return ".".join(out)

    def _call_argument(self, scope: str, variable: str) -> Optional[Tuple[str, str]]:
        """(expression, parent scope) the call of ``scope`` passes ``variable``."""
        if not scope:
            return None
        parent, _, call = scope.rpartition(".")
        body = self._block(self.texts.get(parent, ""), "module", call)
        if body is None:
            return None
        m = re.search(rf"^\s*{re.escape(variable)}\s*=\s*(.+?)\s*$", body, re.M)
        return (m.group(1), parent) if m else None

    def resolve(
        self, expr: str, scope: str, values: Dict[str, Any], depth: int = 0
    ) -> Any:
        """The value of a simple expression: a literal, or ``var.X`` followed to
        the root variable (and ``values``, then its default) through each module
        call. ``_UNRESOLVED`` for anything else."""
        expr = expr.strip()
        if depth > 10:
            return _UNRESOLVED
        if re.fullmatch(r"\d+", expr):
            return int(expr)
        if re.fullmatch(r'"(?:[^"\\$%]|\\.)*"', expr):
            return json.loads(expr)
        m = re.fullmatch(r"var\.([A-Za-z0-9_]+)", expr)
        if not m:
            return _UNRESOLVED
        name = m.group(1)
        if not scope:
            if name in values:
                return values[name]
            default = self.variable_default(name, "")
            return _UNRESOLVED if default is None else default
        arg = self._call_argument(scope, name)
        if arg is not None:
            return self.resolve(arg[0], arg[1], values, depth + 1)
        default = self.variable_default(name, scope)
        return _UNRESOLVED if default is None else default

    def lifecycle_rules(self, values: Dict[str, Any]) -> List[LifecycleRules]:
        """Every lifecycle configuration in the module, at any depth, with the
        bucket each one targets resolved where the expression allows."""
        out: List[LifecycleRules] = []
        for scope, text in self.texts.items():
            for m in _LIFECYCLE.finditer(text):
                body = m.group(2)
                prefix = "".join(f"module.{c}." for c in scope.split(".") if c)
                b = re.search(r"^\s*bucket\s*=\s*(.+?)\s*$", body, re.M)
                rule = LifecycleRules(
                    resource=f"{prefix}aws_s3_bucket_lifecycle_configuration.{m.group(1)}",
                    scope=scope,
                    bucket_expr=b.group(1) if b else "(none)",
                )
                ref = _BUCKET_REF.fullmatch(rule.bucket_expr) if b else None
                if ref and not scope:
                    rule.target = ref.group(1)
                elif ref:
                    # A bucket of the submodule's own; adopted buckets are all
                    # root-module resources.
                    rule.target = f"{scope}:{ref.group(1)}"
                elif b:
                    value = self.resolve(rule.bucket_expr, scope, values)
                    if isinstance(value, str):
                        rule.bucket_name = value
                    elif scope and rule.bucket_expr.startswith("var."):
                        arg = self._call_argument(scope, rule.bucket_expr[4:])
                        root_ref = (
                            _BUCKET_REF.fullmatch(arg[0])
                            if arg is not None and arg[1] == ""
                            else None
                        )
                        if root_ref:
                            rule.target = root_ref.group(1)
                if re.search(r'^\s*dynamic "', body, re.M):
                    rule.unreadable.append("a `dynamic` block")
                for e in _EXPIRATION.finditer(body):
                    d = re.search(r"^\s*days\s*=\s*(.+?)\s*$", e.group(1), re.M)
                    if d:
                        rule.expiry.append(d.group(1))
                    else:
                        rule.unreadable.append(
                            "an `expiration` block with no `days` (a `date`, or "
                            "`expired_object_delete_marker`)"
                        )
                for e in _NONCURRENT.finditer(body):
                    d = re.search(
                        r"^\s*noncurrent_days\s*=\s*(.+?)\s*$", e.group(1), re.M
                    )
                    if d:
                        rule.noncurrent.append(d.group(1))
                    else:
                        rule.unreadable.append(
                            "a `noncurrent_version_expiration` block with no "
                            "`noncurrent_days`"
                        )
                out.append(rule)
        return out

    def inline_lifecycle(self, bucket_resource: str) -> bool:
        """Whether the root ``aws_s3_bucket.<bucket_resource>`` declares a
        deprecated inline ``lifecycle_rule``."""
        body = self._block(
            self.texts.get("", ""), "resource", "aws_s3_bucket", bucket_resource
        )
        return (
            body is not None
            and re.search(r"^\s*lifecycle_rule\s", body, re.M) is not None
        )

    def variable_default(self, name: str, scope: str = "") -> Any:
        body = self._block(self.texts.get(scope, ""), "variable", name)
        if body is None:
            return None
        m = re.search(r"^\s*default\s*=\s*(.+)$", body, re.M)
        if not m:
            return None
        raw = m.group(1).strip()
        try:
            return json.loads(raw)
        except ValueError:
            return raw


class _Unresolved:
    def __repr__(self) -> str:
        return "<unresolved>"


_UNRESOLVED: Any = _Unresolved()


def _read_tf(directory: Path) -> str:
    if not directory.is_dir():
        return ""
    return "\n".join(p.read_text() for p in sorted(directory.glob("*.tf")))


def _counted(body: str) -> bool:
    return re.search(r"^  (count|for_each)\s*=", body, re.M) is not None


_PROVIDER_BLOCK = r'^provider "{name}" \{{\n(.*?)^\}}\n'
_ROLE_ARN_LINE = re.compile(r'^\s*role_arn\s*=\s*("(?:[^"\\]|\\.)*")\s*$', re.M)


def terraform_role_arn(main_tf: Path) -> Optional[str]:
    """The role the new deployment's ``aws`` and ``awscc`` providers assume, read
    from its ``main.tf``; None when they assume none.

    The migration's own AWS calls use the same identity, so they read and change
    the account Terraform deploys to. Refuses when it cannot tell: no
    ``main.tf``, a provider block missing, or the two providers assuming
    different roles.
    """
    path = Path(main_tf)
    if not path.is_file():
        raise IDPConfigurationError(
            f"{path} does not exist, so this command cannot tell which credentials "
            "Terraform uses; run the `plan` phase first"
        )
    text = path.read_text()
    roles: Dict[str, Optional[str]] = {}
    for name in ("aws", "awscc"):
        m = re.search(_PROVIDER_BLOCK.format(name=name), text, re.S | re.M)
        if not m:
            raise IDPConfigurationError(
                f'{path} has no provider "{name}" block, so this command cannot '
                "tell which credentials Terraform uses"
            )
        arn = _ROLE_ARN_LINE.search(m.group(1))
        roles[name] = json.loads(arn.group(1)) if arn else None
    if roles["aws"] != roles["awscc"]:
        raise IDPConfigurationError(
            f"the aws and awscc providers in {path} assume different roles "
            f"({roles['aws']!r} and {roles['awscc']!r}); make them agree"
        )
    return roles["aws"]


def module_dir_from_root(directory: Path) -> Path:
    """The directory ``terraform init`` installed ``module "idp"`` into."""
    modules_json = Path(directory) / ".terraform" / "modules" / "modules.json"
    if modules_json.is_file():
        for mod in json.loads(modules_json.read_text()).get("Modules", []):
            if mod.get("Key") == MODULE_CALL:
                return Path(directory) / mod["Dir"]
    raise IDPConfigurationError(
        f"{directory} has no initialised module {MODULE_CALL!r}; run the `plan` phase "
        "(which runs `terraform init`) first"
    )


# ---------------------------------------------------------------------------
# Phase: plan
# ---------------------------------------------------------------------------


@dataclass
class PlannedResource:
    old_address: str
    old_type: str
    kind: str
    verdict: str
    evidence: str
    note: str
    physical_id: Optional[str] = None
    new_address: Optional[str] = None  # full, with module.idp. and instance keys
    import_id: Optional[str] = None
    detach: bool = False
    import_in_adopt_tf: bool = True


@dataclass
class UserLookup:
    """Whether a user exists in the old pool, and which groups it is in."""

    exists: bool
    groups: List[str] = field(default_factory=list)


@dataclass
class MigrationPlan:
    resources: List[PlannedResource]
    parameters: Dict[str, str]
    warnings: List[str]
    pool_id: Optional[str]
    admin_username: Optional[str]
    tracking_table: Optional[str]
    configuration_table: Optional[str]
    upload_rules: List[Dict[str, str]]
    adopted_buckets: List[Dict[str, str]]  # {kind, bucket, resource}
    # The old state the plan was built from: `detach` refuses a state of another
    # lineage, and `check-destroy` a destroy plan of another deployment.
    old_lineage: Optional[str] = None
    old_serial: Optional[int] = None

    @property
    def imports(self) -> List[PlannedResource]:
        return [r for r in self.resources if r.new_address and r.import_id]

    @property
    def detach_addresses(self) -> List[str]:
        return sorted(r.old_address for r in self.resources if r.detach)

    def to_json(self) -> Dict[str, Any]:
        return {
            "formatVersion": PLAN_FORMAT,
            "resources": [asdict(r) for r in self.resources],
            "parameters": self.parameters,
            "warnings": self.warnings,
            "poolId": self.pool_id,
            "adminUsername": self.admin_username,
            "trackingTable": self.tracking_table,
            "configurationTable": self.configuration_table,
            "uploadRules": self.upload_rules,
            "adoptedBuckets": self.adopted_buckets,
            "oldLineage": self.old_lineage,
            "oldSerial": self.old_serial,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "MigrationPlan":
        if data.get("formatVersion") != PLAN_FORMAT:
            raise IDPConfigurationError(
                f"{PLAN_FILE} has format {data.get('formatVersion')!r}; re-run the "
                "`plan` phase with this version of idp-cli"
            )
        return cls(
            resources=[PlannedResource(**r) for r in data["resources"]],
            parameters=data.get("parameters") or {},
            warnings=data.get("warnings") or [],
            pool_id=data.get("poolId"),
            admin_username=data.get("adminUsername"),
            tracking_table=data.get("trackingTable"),
            configuration_table=data.get("configurationTable"),
            upload_rules=data.get("uploadRules") or [],
            adopted_buckets=data.get("adoptedBuckets") or [],
            old_lineage=data.get("oldLineage"),
            old_serial=data.get("oldSerial"),
        )


def _physical_id(inst: OldInstance) -> Optional[str]:
    a = inst.attributes
    if inst.type in ("aws_kms_key", "awscc_kms_key"):
        return a.get("key_id") or a.get("id")
    if inst.type == "aws_cognito_user":
        return a.get("username")
    return a.get("bucket") or a.get("name") or a.get("id")


def _import_id(new_type: str, inst: OldInstance, pool_id: Optional[str]) -> str:
    a = inst.attributes
    if new_type == "aws_s3_bucket":
        return a["bucket"]
    if new_type == "aws_dynamodb_table":
        return a["name"]
    if new_type == "awscc_kms_key":
        return a.get("key_id") or a["id"]
    if new_type == "aws_cognito_user_pool":
        return a["id"]
    if new_type == "awscc_cognito_user_pool_group":
        return f"{pool_id}|{a['name']}"
    if new_type == "aws_cognito_user":
        return f"{pool_id}/{a['username']}"
    raise IDPConfigurationError(f"no import id rule for {new_type}")


def _resource_type(address: str) -> str:
    return address.split(".")[-2] if address.count(".") >= 1 else address


def build_plan(
    old_state: Dict[str, Any],
    module: ModuleSource,
    *,
    admin_email: str,
    parameters: Optional[Dict[str, str]] = None,
    user_lookup: Optional[Callable[[str, str], UserLookup]] = None,
) -> MigrationPlan:
    """Map every managed instance of the old state to a verdict.

    Refuses, naming every case at once, an old stateful resource no entry in
    :data:`KINDS` covers, a kind whose verdict is ``unsupported``, two old
    resources adopted into one module address, and a parameter the operator set
    that contradicts one the plan derives.

    ``user_lookup(pool_id, username)`` reports whether the admin user exists in
    the old pool (when the old state does not hold it), so the plan can import it
    rather than let the module try to create a user that already exists.
    """
    instances = load_old_state(old_state)
    given = dict(parameters or {})
    refusals: List[str] = []
    warnings: List[str] = []
    planned: Dict[str, PlannedResource] = {}

    pools = [i for i in instances if i.type == "aws_cognito_user_pool"]
    pool = next((i for i in pools if _match_kind(i)), None)
    pool_id = pool.attributes.get("id") if pool else None

    # Parents first: which physical buckets / tables / pools / keys are adopted
    # or kept, so their settings can follow them.
    kept_buckets: Dict[str, str] = {}  # bucket name -> verdict
    kept_tables: Dict[str, str] = {}
    adopted_buckets: List[Dict[str, str]] = []
    derived: Dict[str, str] = {}

    for inst in instances:
        kind = _match_kind(inst)
        if kind is None:
            continue
        pr = PlannedResource(
            old_address=inst.address,
            old_type=inst.type,
            kind=kind.kind,
            verdict=kind.verdict,
            evidence=kind.evidence,
            note=kind.note,
            physical_id=_physical_id(inst),
        )
        if kind.verdict == UNSUPPORTED:
            refusals.append(f"{inst.address}: {kind.note}")
        elif kind.kind in ("user_pool_group", "admin_group"):
            group = inst.attributes.get("name")
            if group in MODULE_GROUPS:
                pr.new_address = MODULE_PREFIX + module.resource_address(
                    MODULE_GROUPS[group]
                )
                pr.import_id = _import_id(
                    "awscc_cognito_user_pool_group", inst, pool_id
                )
                pr.detach = True
            else:
                pr.verdict = KEEP_UNMANAGED
                pr.note = (
                    f"Group {group!r} is not one the module declares; it stays in the "
                    "adopted pool, managed by neither deployment."
                )
                pr.detach = True
        elif kind.kind == "admin_user":
            if inst.attributes.get("username") == admin_email:
                pr.new_address = MODULE_PREFIX + module.resource_address(
                    ADMIN_USER_ADDRESS
                )
                pr.import_id = _import_id("aws_cognito_user", inst, pool_id)
            else:
                pr.verdict = KEEP_UNMANAGED
                pr.note = (
                    "A user other than the admin email you passed; it stays in the "
                    "adopted pool and signs in as before."
                )
            pr.detach = True
        elif kind.verdict in (ADOPT, ADOPT_AFTER_FIX):
            assert kind.new_address  # every adopted kind names one
            pr.new_address = MODULE_PREFIX + module.resource_address(kind.new_address)
            pr.import_id = _import_id(_resource_type(kind.new_address), inst, pool_id)
            pr.detach = True
            if kind.kind == "user_pool":
                pr.import_in_adopt_tf = False
        elif kind.verdict == ADOPT_BY_PARAMETER:
            assert kind.parameter
            derived[kind.parameter] = str(pr.physical_id)
            pr.detach = True
        elif kind.verdict == KEEP_UNMANAGED:
            pr.detach = True
        if (
            pr.detach
            and inst.type in ("aws_s3_bucket", "awscc_s3_bucket")
            and pr.physical_id
        ):
            kept_buckets[pr.physical_id] = pr.verdict
            if pr.verdict in (ADOPT, ADOPT_AFTER_FIX) and pr.new_address:
                adopted_buckets.append(
                    {
                        "kind": kind.kind,
                        "bucket": pr.physical_id,
                        "resource": (kind.new_address or "").split(".")[-1],
                    }
                )
        if pr.detach and inst.type == "aws_dynamodb_table" and pr.physical_id:
            kept_tables[pr.physical_id] = pr.verdict
        planned[inst.address] = pr

    adopted_pool = pool_id if pool is not None and pool.address in planned else None
    kept_keys = {
        planned[i.address].physical_id
        for i in instances
        if i.address in planned
        and i.type == "aws_kms_key"
        and planned[i.address].detach
    }

    for inst in instances:
        if inst.address in planned:
            continue
        a = inst.attributes
        pr = PlannedResource(
            old_address=inst.address,
            old_type=inst.type,
            kind=inst.type,
            verdict=RECREATE,
            evidence=INFERRED,
            note=GENERIC_RECREATE_NOTE,
            physical_id=_physical_id(inst),
        )
        parent: Optional[str] = None

        def unseen(what: str, inst: OldInstance = inst) -> str:
            return (
                f"{inst.address} ({inst.type}): the state does not record which "
                f"{what} it belongs to, so this command cannot tell whether "
                "destroying it strips a setting from a kept resource. Nothing was "
                "written."
            )

        if _is_bucket_setting(inst.type):
            parent = a.get(BUCKET_SETTING_ATTR)
            if not parent:
                refusals.append(unseen("bucket"))
                continue
            if parent in kept_buckets:
                pr.verdict, pr.detach = SETTING, True
                pr.note = (
                    f"A setting of kept bucket {parent}; detached so destroying the old "
                    "deployment does not strip it. The module writes its own."
                )
        elif inst.type in TABLE_SETTING_TYPES:
            parent = str(a.get(TABLE_SETTING_TYPES[inst.type]) or "").split("/")[-1]
            if not parent:
                refusals.append(unseen("table"))
                continue
            if parent in kept_tables:
                pr.verdict, pr.detach = SETTING, True
                pr.note = f"Belongs to kept table {parent}; detached, not destroyed."
        elif inst.type in KEY_SETTING_TYPES:
            parent = str(a.get(KEY_SETTING_TYPES[inst.type]) or "").split("/")[-1]
            if not parent:
                refusals.append(unseen("KMS key"))
                continue
            if parent in kept_keys:
                pr.verdict, pr.detach = SETTING, True
                pr.note = "Belongs to the adopted key; detached, not destroyed."
        elif inst.type == "aws_kms_alias":
            parent = str(a.get("target_key_id") or "").split("/")[-1]
            if not parent:
                refusals.append(unseen("KMS key"))
                continue
            if parent in kept_keys:
                pr.verdict, pr.detach = KEEP_UNMANAGED, True
                pr.note = (
                    f"An alias of the adopted key; detached, so it keeps pointing at "
                    f"the key ({a.get('name')}), managed by neither deployment."
                )
        elif (
            inst.type in POOL_MEMBER_TYPES
            and adopted_pool
            and a.get("user_pool_id") == adopted_pool
        ):
            pr.verdict, pr.detach = KEEP_UNMANAGED, True
            pr.note = (
                "A user, group or group membership in the adopted pool; kept there, "
                "managed by neither deployment."
            )
        elif inst.type in POOL_SETTING_TYPES:
            parent = a.get("user_pool_id")
            if not parent:
                refusals.append(unseen("user pool"))
                continue
            if adopted_pool and parent == adopted_pool:
                pr.verdict, pr.detach = SETTING, True
                pr.note = (
                    "A setting of the adopted user pool; detached so destroying the "
                    "old deployment does not strip it from the pool."
                )
        elif inst.type in BDA_MEMBER_TYPES:
            pr.verdict, pr.detach = KEEP_UNMANAGED, True
            pr.note = (
                "Kept with the BDA project, which may name it; managed by neither "
                "deployment."
            )
        elif inst.type == "aws_lambda_invocation":
            if a.get("lifecycle_scope") == "CREATE_ONLY":
                pr.note = (
                    "Invoked once, on create; destroying it runs nothing (its "
                    "lifecycle_scope is CREATE_ONLY)."
                )
            else:
                scope = a.get("lifecycle_scope")
                pr.verdict, pr.detach = KEEP_UNMANAGED, True
                pr.note = (
                    f"Detached, so destroying the old deployment does not invoke "
                    f"{a.get('function_name')} with a delete action (its lifecycle_scope "
                    f"is {scope!r}"
                    + ("" if scope else ", not recorded in the state")
                    + "). Whatever that invocation manages stays in the account."
                )
        elif inst.type in STATEFUL_TYPES:
            refusals.append(
                f"{inst.address} ({inst.type} {pr.physical_id}): a resource that holds "
                "data and that this command has no verdict for. Nothing was written."
            )
            continue
        elif inst.type not in RECREATE_SAFE_TYPES:
            refusals.append(
                f"{inst.address}: resource type {inst.type} is not one this command "
                "knows is safe to destroy with the old deployment, nor one it knows "
                "how to keep. Nothing was written. Decide yourself: if it must survive, "
                f"remove it from the old state (`terraform state rm {inst.address}`) "
                "and save the state again; otherwise report the type so it can be "
                "classified."
            )
            continue
        elif inst.type == "aws_sqs_queue":
            pr.note = "Recreated; `freeze` waits for in-flight documents first."
        elif inst.type == "aws_cloudwatch_log_group":
            pr.note = "Log history is destroyed with the old deployment; export it first if you need it."
        planned[inst.address] = pr

    # The module's admin user: adopt an existing user of that exact name, or let
    # the module create it.
    admin_username: Optional[str] = None
    has_admin = any(
        p.new_address and p.new_address.endswith(ADMIN_USER_ADDRESS)
        for p in planned.values()
    )
    if adopted_pool:
        lookup = UserLookup(exists=has_admin, groups=[])
        if not has_admin and user_lookup is not None:
            lookup = user_lookup(adopted_pool, admin_email)
        if has_admin or lookup.exists:
            admin_username = admin_email
            if not has_admin:
                planned["<admin-user>"] = PlannedResource(
                    old_address="(not in the old state)",
                    old_type="aws_cognito_user",
                    kind="admin_user",
                    verdict=ADOPT,
                    evidence=MEASURED,
                    note="An existing user of the pool, adopted as the module's admin user.",
                    physical_id=admin_email,
                    new_address=MODULE_PREFIX
                    + module.resource_address(ADMIN_USER_ADDRESS),
                    import_id=f"{adopted_pool}/{admin_email}",
                )
            if has_admin or "Admin" in lookup.groups:
                planned["<admin-in-group>"] = PlannedResource(
                    old_address="(not in the old state)",
                    old_type="aws_cognito_user_in_group",
                    kind="admin_group_membership",
                    verdict=ADOPT,
                    evidence=MEASURED,
                    note="The admin user's Admin membership.",
                    physical_id=f"Admin/{admin_email}",
                    new_address=MODULE_PREFIX
                    + module.resource_address(ADMIN_GROUP_ATTACHMENT_ADDRESS),
                    import_id=f"{adopted_pool},Admin,{admin_email}",
                )
            else:
                warnings.append(
                    f"{admin_email} is not in the Admin group; the module adds it."
                )
        else:
            warnings.append(
                f"No user named exactly {admin_email!r} is in the old pool, so the module "
                "creates one. Usernames in the adopted pool are case-sensitive: pass "
                "the admin's username with the exact case it was created with if you "
                "meant an existing user."
            )

    # Parameters the plan derives from the old resources.
    if pool is not None:
        email = next(
            (
                s
                for s in pool.attributes.get("schema") or []
                if s.get("name") == "email"
            ),
            None,
        )
        if email is not None and email.get("mutable"):
            derived["ExternalIdPEmailMutable"] = "true"
        username_config = (pool.attributes.get("username_configuration") or [{}])[0]
        # Cognito's default, when a pool sets no username configuration, is
        # case-sensitive usernames.
        if username_config.get("case_sensitive", True) is not False:
            warnings.append(
                "The old user pool's usernames are case-sensitive, and stay so: each user "
                "signs in with the exact case of their username."
            )
    for name, value in derived.items():
        if name in given and given[name] != value:
            refusals.append(
                f"--parameters {name}={given[name]} contradicts the old deployment, "
                f"which needs {name}={value}"
            )

    # Two old resources into one address.
    seen: Dict[str, str] = {}
    for p in planned.values():
        if p.new_address:
            if p.new_address in seen:
                refusals.append(
                    f"{p.old_address} and {seen[p.new_address]} both map to {p.new_address}"
                )
            seen[p.new_address] = p.old_address

    if refusals:
        raise IDPConfigurationError(
            "refusing to plan the migration:\n  - " + "\n  - ".join(refusals)
        )

    tracking = next(
        (p.physical_id for p in planned.values() if p.kind == "tracking_table"), None
    )
    config = next(
        (p.physical_id for p in planned.values() if p.kind == "configuration_table"),
        None,
    )
    input_bucket = next(
        (p.physical_id for p in planned.values() if p.kind == "input_bucket"), None
    )
    upload_rules = []
    for inst in instances:
        if inst.type != "aws_cloudwatch_event_rule" or not input_bucket:
            continue
        pattern = str(inst.attributes.get("event_pattern") or "")
        if input_bucket in pattern and "aws.s3" in pattern:
            upload_rules.append(
                {
                    "name": inst.attributes.get("name") or inst.attributes.get("id"),
                    "eventBusName": inst.attributes.get("event_bus_name") or "default",
                    "oldAddress": inst.address,
                }
            )

    return MigrationPlan(
        resources=sorted(planned.values(), key=lambda p: (p.verdict, p.old_address)),
        parameters={**given, **derived},
        warnings=warnings,
        pool_id=adopted_pool,
        admin_username=admin_username,
        tracking_table=tracking,
        configuration_table=config,
        upload_rules=upload_rules,
        adopted_buckets=adopted_buckets,
        old_lineage=old_state.get("lineage"),
        old_serial=old_state.get("serial"),
    )


def _hcl_string(value: str) -> str:
    return json.dumps(value).replace("${", "$${").replace("%{", "%%{")


def render_adopt_tf(plan: MigrationPlan) -> str:
    lines = [
        ADOPT_MARKER,
        "# One import block per adopted resource. The user pool is not here: the",
        "# `adopt-user-pool` phase imports it with `terraform import`. Delete this",
        "# file once the adoption has been applied.",
        "",
    ]
    for r in sorted(plan.imports, key=lambda r: r.new_address or ""):
        if not r.import_in_adopt_tf:
            continue
        lines += [
            f"# {r.old_address} ({r.evidence})",
            "import {",
            f"  to = {r.new_address}",
            f"  id = {_hcl_string(r.import_id or '')}",
            "}",
            "",
        ]
    return "\n".join(lines)


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def detach_commands(
    addresses: Sequence[str],
    binary: str = "terraform",
    backup_dir: Optional[Path] = None,
) -> List[str]:
    """Shell lines, run in the old deployment's directory, that back up the old
    state and remove ``addresses`` from it.

    The backup holds every secret the old state does, so it is written readable
    by its owner only (``umask 077`` in a subshell) into ``backup_dir`` -- the
    migration directory -- rather than wherever the operator happens to be.
    """
    if not addresses:
        return []
    quoted = " ".join(_shell_quote(a) for a in addresses)
    target = "old-state-backup-before-detach.tfstate"
    if backup_dir is not None:
        target = str(Path(backup_dir) / target)
    return [
        f"(umask 077 && {binary} state pull > {_shell_quote(target)})",
        f"{binary} state rm {quoted}",
    ]


# ---------------------------------------------------------------------------
# Phase: retention
# ---------------------------------------------------------------------------


def resolve_days(
    expr: str, values: Dict[str, Any], module: ModuleSource, scope: str = ""
) -> Optional[int]:
    value = module.resolve(expr, scope, values)
    if value is _UNRESOLVED or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _root_variable(module: ModuleSource, expr: str, scope: str) -> Optional[str]:
    """The root-module variable ``expr`` comes from, when it is a plain
    ``var.X`` passed down unchanged through each module call."""
    for _ in range(10):
        m = re.fullmatch(r"var\.([A-Za-z0-9_]+)", expr.strip())
        if not m:
            return None
        if not scope:
            return m.group(1)
        arg = module._call_argument(scope, m.group(1))
        if arg is None:
            return None
        expr, scope = arg
    return None


def oldest_object(s3, bucket: str) -> Optional[datetime]:
    oldest: Optional[datetime] = None
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            lm = obj["LastModified"]
            if oldest is None or lm < oldest:
                oldest = lm
    return oldest


def oldest_noncurrent(s3, bucket: str) -> Optional[datetime]:
    """When the longest-noncurrent object version in ``bucket`` became noncurrent.

    S3 counts ``noncurrent_days`` from the moment a version stops being current,
    which is the ``LastModified`` of the version or delete marker that superseded
    it, so each key's versions are ordered newest first and each noncurrent
    version is dated by its successor. Reads every version (ListObjectVersions).
    """
    # (LastModified, IsLatest, is a version rather than a delete marker)
    by_key: Dict[str, List[Tuple[datetime, bool, bool]]] = {}
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        for v in page.get("Versions", []) or []:
            by_key.setdefault(v["Key"], []).append(
                (v["LastModified"], bool(v.get("IsLatest")), True)
            )
        for d in page.get("DeleteMarkers", []) or []:
            by_key.setdefault(d["Key"], []).append(
                (d["LastModified"], bool(d.get("IsLatest")), False)
            )
    oldest: Optional[datetime] = None
    for entries in by_key.values():
        # Newest first; LastModified has one-second resolution, so on a tie the
        # current entry goes first.
        entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
        for newer, older in zip(entries, entries[1:]):
            if older[2] and (oldest is None or newer[0] < oldest):
                oldest = newer[0]
    return oldest


#: Said in every check-retention result, so a pass is not read as more than it is.
RETENTION_LIMITS = (
    "Rule filters (prefix, tags, object size) are not read: every rule is treated "
    "as covering the whole bucket, so a finding may be stricter than the rule.",
    "`newer_noncurrent_versions` is not read: noncurrent versions are treated as "
    "expiring after `noncurrent_days` however many newer ones there are.",
    "Storage-class transitions and `abort_incomplete_multipart_upload` are not "
    "checked: they move objects or remove uploads that never completed, and delete "
    "no stored object.",
    "The lifecycle configuration each bucket has now is not read; the module's "
    "replaces it on the first apply.",
)


def _age_days(now: datetime, then: Optional[datetime]) -> Optional[int]:
    return None if then is None else math.ceil((now - then).total_seconds() / 86400)


def check_retention(
    plan: MigrationPlan,
    module: ModuleSource,
    values: Dict[str, Any],
    s3,
    *,
    accept_expiry: Iterable[str] = (),
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """(one row per kept bucket, findings, notes).

    A finding is a kept bucket holding an object, or a noncurrent object version,
    older than the shortest expiry the module's lifecycle rules would put on it;
    a rule whose period this command cannot evaluate; a lifecycle configuration
    in the module whose bucket it cannot resolve, or a module directory it cannot
    read (either could target a kept bucket). ``accept_expiry`` (bucket kinds)
    turns the first two into notes for that bucket. The notes say what was and
    was not checked.
    """
    now = now or datetime.now(timezone.utc)
    accepted = set(accept_expiry)
    rows: List[Dict[str, Any]] = []
    findings: List[str] = []
    notes: List[str] = [f"Not checked: {line}" for line in RETENTION_LIMITS]

    for call in module.unread:
        findings.append(
            f"module call {call} has no local source this command can read, so a "
            "lifecycle rule inside it could target a kept bucket unseen"
        )
    rules = module.lifecycle_rules(values)
    for rule in rules:
        if rule.target is None and rule.bucket_name is None:
            findings.append(
                f"{rule.resource}: its bucket is `{rule.bucket_expr}`, which this "
                "command cannot resolve to a bucket, so it may put an expiry rule on "
                "a kept one; nothing about it was checked"
            )

    buckets = list(plan.adopted_buckets) + [
        {"kind": r.kind, "bucket": str(r.physical_id), "resource": ""}
        for r in plan.resources
        if r.verdict == ADOPT_BY_PARAMETER
        and r.old_type in ("aws_s3_bucket", "awscc_s3_bucket")
    ]
    for b in buckets:
        kind = b["kind"]
        mine = [
            r
            for r in rules
            if (b["resource"] and r.target == b["resource"])
            or r.bucket_name == b["bucket"]
        ]
        expiry_exprs = [(e, r) for r in mine for e in r.expiry]
        noncurrent_exprs = [(e, r) for r in mine for e in r.noncurrent]
        unresolved: List[str] = []
        for r in mine:
            unresolved += [f"{r.resource}: {u}" for u in r.unreadable]
        if b["resource"] and module.inline_lifecycle(b["resource"]):
            unresolved.append(
                f"aws_s3_bucket.{b['resource']}: an inline `lifecycle_rule`"
            )

        def days(exprs: List[Tuple[str, LifecycleRules]]) -> List[int]:
            out = []
            for e, r in exprs:
                d = resolve_days(e, values, module, r.scope)
                if d is None:
                    unresolved.append(f"{r.resource}: `{e}`")
                else:
                    out.append(d)
            return out

        expiry_days = days(expiry_exprs)
        noncurrent_days = days(noncurrent_exprs)
        expiry = min(expiry_days) if expiry_days else None
        noncurrent = min(noncurrent_days) if noncurrent_days else None
        variables = sorted(
            {
                v
                for e, r in expiry_exprs + noncurrent_exprs
                for v in [_root_variable(module, e, r.scope)]
                if v
            }
        )
        checked: List[str] = []
        age = _age_days(now, oldest_object(s3, b["bucket"]))
        checked.append(
            "current objects (ListObjectsV2): "
            + ("none" if age is None else f"oldest is {age} days old")
        )
        status = s3.get_bucket_versioning(Bucket=b["bucket"]).get("Status")
        noncurrent_age: Optional[int] = None
        if status in ("Enabled", "Suspended"):
            noncurrent_age = _age_days(now, oldest_noncurrent(s3, b["bucket"]))
            checked.append(
                f"noncurrent versions (versioning {status}, ListObjectVersions): "
                + (
                    "none"
                    if noncurrent_age is None
                    else f"the oldest became noncurrent {noncurrent_age} days ago"
                )
            )
        else:
            checked.append(
                "noncurrent versions: none, the bucket has never been versioned"
            )

        problems: List[str] = []
        how = (
            "raise " + " or ".join(variables) + " (as its CloudFormation parameter "
            "in --parameters, then run `plan` again) to at least {age}"
            if variables
            else "the rule's period is fixed in the module, so copy the objects you "
            "need elsewhere first"
        )
        if expiry is not None and age is not None and age > expiry:
            problems.append(
                f"{kind} {b['bucket']}: its oldest object is {age} days old and the "
                f"module expires objects after {expiry} days, so the first apply would "
                f"start deleting it. To keep them, {how.format(age=age)}; or pass "
                f"--accept-expiry {kind}."
            )
        if (
            noncurrent is not None
            and noncurrent_age is not None
            and (noncurrent_age > noncurrent)
        ):
            problems.append(
                f"{kind} {b['bucket']}: a noncurrent object version has been "
                f"noncurrent for {noncurrent_age} days and the module expires "
                f"noncurrent versions after {noncurrent} days, so the first apply "
                f"would start deleting old versions. To keep them, "
                f"{how.format(age=noncurrent_age)}; or pass --accept-expiry {kind}."
            )
        if unresolved:
            problems.append(
                f"{kind} {b['bucket']}: the module's lifecycle rules include "
                + "; ".join(unresolved)
                + ", which this command cannot evaluate, so it cannot tell whether "
                "the first apply deletes objects already in the bucket. Find the "
                f"period yourself, then pass --accept-expiry {kind} to adopt it anyway."
            )
        row: Dict[str, Any] = {
            "kind": kind,
            "bucket": b["bucket"],
            "expiryDays": expiry,
            "oldestObjectAgeDays": age,
            "noncurrentExpiryDays": noncurrent,
            "versioning": status or "never enabled",
            "oldestNoncurrentAgeDays": noncurrent_age,
            "unresolved": unresolved,
            "variables": variables,
            "checked": checked,
            "ok": not problems or kind in accepted,
        }
        if problems and kind in accepted:
            row["note"] = f"accepted with --accept-expiry {kind}: " + " ".join(problems)
        elif problems:
            row["note"] = " ".join(problems)
            findings.extend(problems)
        rows.append(row)
    return rows, findings, notes


# ---------------------------------------------------------------------------
# Phase: freeze
# ---------------------------------------------------------------------------

# Statuses a document ends on (idp_common.models.Status). A workflow can also end
# on HITL_IN_PROGRESS, but that document is waiting for a reviewer, so it is
# reported on its own rather than treated as finished.
TERMINAL_STATUSES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "ABORTED",
        "REJECTED",
        "REDACTED_SUPERSEDED",
        "QUARANTINED",
        "MALWARE_SCAN_FAILED",
    }
)
REVIEW_STATUSES = frozenset({"HITL_IN_PROGRESS"})


def upload_rule_states(plan: MigrationPlan, events) -> List[Dict[str, str]]:
    out = []
    for rule in plan.upload_rules:
        resp = events.describe_rule(
            Name=rule["name"], EventBusName=rule["eventBusName"]
        )
        out.append({**rule, "state": resp.get("State", "UNKNOWN")})
    return out


NO_UPLOAD_RULE = (
    "the old state has no EventBridge rule on the input bucket, so this command "
    "cannot stop the old deployment's uploads; find what starts its workflows "
    "before going on"
)


@dataclass
class DocumentScan:
    """The tracking table's documents that are not finished, from a full scan."""

    in_flight: List[Dict[str, str]] = field(default_factory=list)
    in_review: List[Dict[str, str]] = field(default_factory=list)
    ignored: List[Dict[str, str]] = field(default_factory=list)
    scanned: int = 0

    def findings(self) -> List[str]:
        out = []
        if self.in_flight:
            out.append(
                f"{len(self.in_flight)} document(s) are still being processed by the "
                "old deployment ("
                + ", ".join(sorted({d["status"] for d in self.in_flight}))
                + "); wait for them (freeze --wait), or name each one you have "
                "decided to abandon with --ignore-document"
            )
        if self.in_review:
            out.append(
                f"{len(self.in_review)} document(s) are waiting for human review "
                "(HITL_IN_PROGRESS). Whether the new deployment can complete a "
                "review the old one started has not been verified; finish or reject "
                "the reviews in the old web UI first, or name each document with "
                "--ignore-document"
            )
        return out


#: The sort key of a tracking table's document record (``PK`` = ``doc#<key>``).
DOCUMENT_RECORD_SK = "none"


def scan_documents(table_name: str, ddb, ignore: Iterable[str] = ()) -> DocumentScan:
    """Every document record of a tracking table whose ``ObjectStatus`` is not
    terminal, split into in flight and awaiting review. Paginates the whole
    table; ``ignore`` (object keys) moves a document to ``ignored``.

    A document record is the ``doc#<key>`` row with sort key ``none``. The same
    partition also holds one ``run#...`` row per processing run, which carries no
    ``ObjectStatus``; counting those would report every finished document as in
    flight, so they are skipped (and filtered server-side as well).
    """
    skip = set(ignore)
    scan = DocumentScan()
    kwargs: Dict[str, Any] = {
        "TableName": table_name,
        "FilterExpression": "begins_with(PK, :doc) AND SK = :record",
        "ExpressionAttributeValues": {":doc": {"S": "doc#"}, ":record": {"S": "none"}},
        "ProjectionExpression": "PK, SK, ObjectStatus",
    }
    while True:
        resp = ddb.scan(**kwargs)
        for item in resp.get("Items", []):
            if (item.get("SK") or {}).get("S") != DOCUMENT_RECORD_SK:
                continue
            scan.scanned += 1
            status = (item.get("ObjectStatus") or {}).get("S", "")
            if status in TERMINAL_STATUSES:
                continue
            row = {"key": item["PK"]["S"][len("doc#") :], "status": status}
            if row["key"] in skip:
                scan.ignored.append(row)
            elif status in REVIEW_STATUSES:
                scan.in_review.append(row)
            else:
                scan.in_flight.append(row)
        if "LastEvaluatedKey" not in resp:
            return scan
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ---------------------------------------------------------------------------
# Phase: configuration fix
# ---------------------------------------------------------------------------

REVISION_COUNTERS = ("LatestRevision", "PublishedRevision")
CONFIG_PREFIX = "Config#"
REVISION_INDEX_PREFIX = "ConfigRevIndex#"


def item_to_json(value: Any) -> Any:
    """A low-level DynamoDB item (or attribute value) in the AWS CLI's JSON form:
    binary (``B``, ``BS``) as base64 text. boto3 returns binary as ``bytes``, which
    ``json`` cannot write, and upstream stores every configuration body compressed
    in a ``B`` attribute (``_compressed_config``)."""
    import base64

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if k == "B" and isinstance(v, (bytes, bytearray)):
                out[k] = base64.b64encode(bytes(v)).decode()
            elif k == "BS" and isinstance(v, list):
                out[k] = [base64.b64encode(bytes(b)).decode() for b in v]
            else:
                out[k] = item_to_json(v)
        return out
    if isinstance(value, list):
        return [item_to_json(v) for v in value]
    return value


def item_from_json(value: Any) -> Any:
    """The inverse of :func:`item_to_json`, for ``PutItem``."""
    import base64

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if k == "B" and isinstance(v, str):
                out[k] = base64.b64decode(v)
            elif k == "BS" and isinstance(v, list):
                out[k] = [base64.b64decode(b) for b in v]
            else:
                out[k] = item_from_json(v)
        return out
    if isinstance(value, list):
        return [item_from_json(v) for v in value]
    return value


def scan_all(ddb, table_name: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"TableName": table_name}
    while True:
        resp = ddb.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def config_fix_targets(items: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """(profile rows carrying a revision counter, refusals).

    Refuses a table that already has a revision index: that table has a revision
    history, so the counters are real and must not be removed (the fix is for a
    table upstream wrote, before this repository's code has run against it).
    """
    keys = [i["Configuration"]["S"] for i in items]
    indexes = sorted(k for k in keys if k.startswith(REVISION_INDEX_PREFIX))
    if indexes:
        return [], [
            "the configuration table already holds a revision history ("
            + ", ".join(indexes[:5])
            + (", ..." if len(indexes) > 5 else "")
            + "), so its revision counters are real. This fix is for the table as the "
            "old deployment left it, before the first apply; do not run it after."
        ]
    targets = sorted(
        i["Configuration"]["S"]
        for i in items
        if i["Configuration"]["S"].startswith(CONFIG_PREFIX)
        and any(c in i for c in REVISION_COUNTERS)
    )
    return targets, []


# ---------------------------------------------------------------------------
# Phase: user pool schema normalisation
# ---------------------------------------------------------------------------

SCHEMA_CORE = ("attribute_data_type", "mutable", "required")

# The bounds Cognito reports for an attribute created without them. Measured for
# `email` in the live run (min_length "0", max_length "2048"); the same defaults
# for a custom string attribute are Cognito's documented behaviour, not measured.
# Number attributes have no default bounds.
CONSTRAINT_DEFAULTS = {
    "string_attribute_constraints": {"min_length": "0", "max_length": "2048"},
    "number_attribute_constraints": {"min_value": "", "max_value": ""},
}


def _effective_bounds(item: Dict[str, Any], key: str) -> Dict[str, str]:
    """An item's bounds with null, empty and absent all read as Cognito's
    default, so only a real difference in a bound remains."""
    blocks = item.get(key) or []
    block = blocks[0] if blocks else {}
    out = {}
    for bound, default in CONSTRAINT_DEFAULTS[key].items():
        value = block.get(bound)
        value = "" if value is None else str(value)
        out[bound] = value or default
    return out


def _module_created_form(item: Dict[str, Any]) -> Dict[str, Any]:
    """A schema item as the provider records it for a pool it created.

    Measured: a module-created pool's state has ``developer_only_attribute =
    false`` and empty-string constraint bounds where the configuration sets none;
    a plan's ``after`` carries ``null`` for those."""
    out = dict(item)
    if out.get("developer_only_attribute") is None:
        out["developer_only_attribute"] = False
    for key in ("string_attribute_constraints", "number_attribute_constraints"):
        out[key] = [
            {k: ("" if v is None else v) for k, v in c.items()}
            for c in out.get(key) or []
        ]
    return out


def normalise_pool_schema(
    state_schema: List[Dict[str, Any]], desired_schema: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """(the schema to write into state, what changes, conflicts).

    An imported pool's state records every detail Cognito reports for each schema
    attribute (``string_attribute_constraints`` with ``min_length = "0"``, for
    example), which the module does not declare; the provider reads the
    difference as removing a schema item, which Cognito refuses. Each item the
    module also declares is replaced by the module's form of it. Items the module
    does not declare are dropped from state (the attribute stays in the pool);
    items only the module declares stay out, so the apply adds them.

    A conflict is a difference Cognito cannot apply (data type, mutability,
    required) -- rewriting state would only hide it, so it is refused instead.
    So is a real difference in a string or number bound: the rewrite may turn a
    null, empty or absent bound into the form the module writes, or a bound equal
    to Cognito's default into the module's "none", and nothing else, because
    rewriting a bound the pool enforces would record in state a limit the pool
    does not have.
    """
    desired = {d["name"]: _module_created_form(d) for d in desired_schema}
    out: List[Dict[str, Any]] = []
    changes: List[str] = []
    conflicts: List[str] = []
    for item in state_schema:
        name = item.get("name")
        want = desired.get(name)
        if want is None:
            changes.append(
                f"drop {name!r} from state (the module does not declare it; it stays "
                "in the pool)"
            )
            continue
        bad = [k for k in SCHEMA_CORE if item.get(k) != want.get(k)]
        if bad:
            conflicts.append(
                f"schema attribute {name!r}: "
                + ", ".join(
                    f"{k} is {item.get(k)!r} in the pool, {want.get(k)!r} in the module"
                    for k in bad
                )
                + ". Cognito cannot change this; set the module parameter that controls "
                "it to the pool's value"
                + (" (ExternalIdPEmailMutable for email)" if name == "email" else "")
            )
            continue
        bounds = [
            f"{key} {bound} is {have[bound]!r} in the pool and {wanted[bound]!r} "
            "in the module"
            for key in CONSTRAINT_DEFAULTS
            for have, wanted in [
                (_effective_bounds(item, key), _effective_bounds(want, key))
            ]
            for bound in CONSTRAINT_DEFAULTS[key]
            if have[bound] != wanted[bound]
        ]
        if bounds:
            conflicts.append(
                f"schema attribute {name!r}: "
                + ", ".join(bounds)
                + " (an empty bound read as Cognito's default). Cognito cannot "
                "change an attribute's bounds, so the state is not rewritten; the "
                "module would have to declare the pool's bounds"
            )
            continue
        if item != want:
            diff = sorted(
                k for k in set(item) | set(want) if item.get(k) != want.get(k)
            )
            changes.append(f"rewrite {name!r} in state ({', '.join(diff)})")
        out.append(want)
    for name in desired:
        if name not in {i.get("name") for i in state_schema}:
            changes.append(
                f"leave {name!r} out of state; the apply adds it to the pool"
            )
    return out, changes, conflicts


def desired_pool_schema(
    plan_json: Dict[str, Any], address: str
) -> List[Dict[str, Any]]:
    for rc in plan_json.get("resource_changes", []):
        if rc.get("address") == address:
            after = rc["change"].get("after") or {}
            if "schema" in after:
                return after["schema"]
    raise IDPConfigurationError(
        f"the plan has no change for {address}; is the user pool imported?"
    )


def rewrite_state_schema(
    state: Dict[str, Any], address: str, schema: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """A copy of ``state`` with ``address``'s schema replaced and the serial
    incremented, as ``terraform state push`` requires."""
    new = json.loads(json.dumps(state))
    module, rest = _split_module(address)
    tf_type, name = rest.split(".")
    for res in new.get("resources", []):
        if (
            res.get("module", "") == module
            and res["type"] == tf_type
            and res["name"] == name
        ):
            res["instances"][0]["attributes"]["schema"] = schema
            new["serial"] = int(new.get("serial", 0)) + 1
            return new
    raise IDPConfigurationError(f"{address} is not in the new deployment's state")


def state_schema(state: Dict[str, Any], address: str) -> Optional[List[Dict[str, Any]]]:
    module, rest = _split_module(address)
    tf_type, name = rest.split(".")
    for res in state.get("resources", []):
        if (
            res.get("module", "") == module
            and res["type"] == tf_type
            and res["name"] == name
        ):
            return res["instances"][0]["attributes"].get("schema") or []
    return None


def _split_module(address: str) -> Tuple[str, str]:
    parts = address.split(".")
    i = 0
    while i < len(parts) and parts[i] == "module":
        i += 2
    return ".".join(parts[:i]), ".".join(parts[i:])


# ---------------------------------------------------------------------------
# Phase: the adoption plan
# ---------------------------------------------------------------------------


_MISSING: Any = object()


def _get(obj: Any, path: Sequence[str]) -> Any:
    for part in path:
        if isinstance(obj, list):
            if not part.isdigit() or int(part) >= len(obj):
                return _MISSING
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            if part not in obj:
                return _MISSING
            obj = obj[part]
        else:
            return _MISSING
    return obj


def _unknown(tree: Any, path: Sequence[str]) -> bool:
    """Whether ``after_unknown`` marks ``path`` (or a parent of it) unknown."""
    for part in path:
        if tree is True:
            return True
        tree = _get(tree, [part])
        if tree is _MISSING:
            return False
    return tree is True or (isinstance(tree, (dict, list)) and _any_true(tree))


def _any_true(tree: Any) -> bool:
    if tree is True:
        return True
    if isinstance(tree, dict):
        return any(_any_true(v) for v in tree.values())
    if isinstance(tree, list):
        return any(_any_true(v) for v in tree)
    return False


def _on(value: Any) -> bool:
    return value is True


def _names(items: Any) -> set:
    return {i.get("name") for i in items or [] if isinstance(i, dict)}


_MFA_RANK = {"OFF": 0, "OPTIONAL": 1, "ON": 2}


def _ttl(value: Any) -> Tuple[bool, str]:
    block = (value or [{}])[0] if isinstance(value, list) else {}
    enabled = block.get("enabled") is True
    return enabled, (block.get("attribute_name") or "") if enabled else ""


def _lambda_triggers(value: Any) -> Dict[str, Any]:
    block = (value or [{}])[0] if isinstance(value, list) else {}
    return {k: v for k, v in (block or {}).items() if v not in (None, "", [], {})}


def _kms_keys(rules: Any) -> List[Tuple[str, str]]:
    out = []
    for rule in rules or []:
        for d in (rule or {}).get("apply_server_side_encryption_by_default") or []:
            out.append((d.get("sse_algorithm") or "", d.get("kms_master_key_id") or ""))
    return out


@dataclass(frozen=True)
class RefusedChange:
    """One transition of one attribute that the adoption plan must not make.

    ``refuse(before, after, creating, after_unknown)`` gets the attribute's value
    before and after (``None`` before when the resource is being created, which
    happens only for a setting resource on a kept bucket) and whether the plan
    marks the after value unknown.
    """

    rule: str
    types: Tuple[str, ...]
    attribute: str
    refuse: Callable[[Any, Any, bool, bool], bool]
    reason: str


_S3_PAB = "aws_s3_bucket_public_access_block"

REFUSED_CHANGES: Tuple[RefusedChange, ...] = (
    RefusedChange(
        "pitr-off",
        ("aws_dynamodb_table",),
        "point_in_time_recovery",
        lambda b, a, c, u: (
            _on(_get(b, ["0", "enabled"])) and not _on(_get(a, ["0", "enabled"]))
        ),
        "turns point-in-time recovery off",
    ),
    RefusedChange(
        "table-deletion-protection-off",
        ("aws_dynamodb_table",),
        "deletion_protection_enabled",
        lambda b, a, c, u: _on(b) and not _on(a),
        "turns deletion protection off",
    ),
    RefusedChange(
        "gsi-removed",
        ("aws_dynamodb_table",),
        "global_secondary_index",
        lambda b, a, c, u: not u and bool(_names(b) - _names(a)),
        "removes a global secondary index",
    ),
    RefusedChange(
        "lsi-removed",
        ("aws_dynamodb_table",),
        "local_secondary_index",
        lambda b, a, c, u: not u and bool(_names(b) - _names(a)),
        "removes a local secondary index (which replaces the table)",
    ),
    RefusedChange(
        "stream-off",
        ("aws_dynamodb_table",),
        "stream_enabled",
        lambda b, a, c, u: _on(b) and not _on(a),
        "turns the table's stream off",
    ),
    RefusedChange(
        "ttl-changed",
        ("aws_dynamodb_table",),
        "ttl",
        lambda b, a, c, u: _ttl(b) != _ttl(a),
        "changes the time-to-live setting (items carrying the new attribute start "
        "expiring; items carrying the old one stop)",
    ),
    RefusedChange(
        "table-encryption-removed",
        ("aws_dynamodb_table",),
        "server_side_encryption",
        lambda b, a, c, u: (
            _on(_get(b, ["0", "enabled"])) and not _on(_get(a, ["0", "enabled"]))
        ),
        "removes the table's customer-managed encryption",
    ),
    RefusedChange(
        "versioning-suspended",
        ("aws_s3_bucket_versioning",),
        "versioning_configuration",
        lambda b, a, c, u: (
            _get(a, ["0", "status"]) in ("Suspended", "Disabled")
            and (c or _get(b, ["0", "status"]) == "Enabled")
        ),
        "suspends versioning",
    ),
    RefusedChange(
        "bucket-encryption-key-removed",
        ("aws_s3_bucket_server_side_encryption_configuration",),
        "rule",
        lambda b, a, c, u: (
            not u
            and not any(alg.startswith("aws:kms") and key for alg, key in _kms_keys(a))
            and (c or any(key for _, key in _kms_keys(b)))
        ),
        "leaves the bucket with no customer-managed KMS key",
    ),
    *(
        RefusedChange(
            f"public-access-{attr.replace('_', '-')}-off",
            (_S3_PAB,),
            attr,
            lambda b, a, c, u: not _on(a) and (c or _on(b)),
            f"turns {attr} off",
        )
        for attr in (
            "block_public_acls",
            "block_public_policy",
            "ignore_public_acls",
            "restrict_public_buckets",
        )
    ),
    RefusedChange(
        "bucket-force-destroy-on",
        ("aws_s3_bucket",),
        "force_destroy",
        lambda b, a, c, u: _on(a) and not _on(b),
        "sets force_destroy, so destroying the new deployment would delete every "
        "object in the bucket",
    ),
    RefusedChange(
        "pool-deletion-protection-off",
        ("aws_cognito_user_pool",),
        "deletion_protection",
        lambda b, a, c, u: b == "ACTIVE" and a != "ACTIVE",
        "turns the user pool's deletion protection off",
    ),
    RefusedChange(
        "pool-mfa-relaxed",
        ("aws_cognito_user_pool",),
        "mfa_configuration",
        lambda b, a, c, u: (
            not u and _MFA_RANK.get(str(a), -1) < _MFA_RANK.get(str(b), -1)
        ),
        "relaxes multi-factor authentication",
    ),
    RefusedChange(
        "pool-trigger-removed",
        ("aws_cognito_user_pool",),
        "lambda_config",
        lambda b, a, c, u: (
            not u and bool(set(_lambda_triggers(b)) - set(_lambda_triggers(a)))
        ),
        "removes a Lambda trigger (a pre-token trigger maps users to their groups)",
    ),
    RefusedChange(
        "pool-schema-item-removed",
        ("aws_cognito_user_pool",),
        "schema",
        lambda b, a, c, u: not u and bool(_names(b) - _names(a)),
        "removes a schema attribute, which Cognito refuses",
    ),
    RefusedChange(
        "key-rotation-off",
        ("awscc_kms_key",),
        "enable_key_rotation",
        lambda b, a, c, u: _on(b) and not _on(a),
        "turns key rotation off",
    ),
    RefusedChange(
        "key-disabled",
        ("awscc_kms_key",),
        "enabled",
        lambda b, a, c, u: _on(b) and a is False,
        "disables the key, so nothing encrypted with it can be read",
    ),
    RefusedChange(
        "key-deletion-window-shortened",
        ("awscc_kms_key",),
        "pending_window_in_days",
        lambda b, a, c, u: isinstance(b, int) and isinstance(a, int) and a < b,
        "shortens the key's deletion window",
    ),
    RefusedChange(
        "user-disabled",
        ("aws_cognito_user",),
        "enabled",
        lambda b, a, c, u: _on(b) and a is False,
        "disables the user",
    ),
)

#: Setting resources the table covers on a kept bucket, created or updated.
_BUCKET_SETTING_CHECKED = frozenset(
    t for row in REFUSED_CHANGES for t in row.types if t.startswith("aws_s3_bucket_")
)


def _changed_attributes(change: Dict[str, Any]) -> List[str]:
    before = change.get("before") or {}
    after = change.get("after") or {}
    unknown = change.get("after_unknown") or {}
    return sorted(
        k
        for k in set(before) | set(after) | set(unknown)
        if before.get(k) != after.get(k) or _any_true(unknown.get(k))
    )


def check_adoption_plan(
    plan_json: Dict[str, Any],
    plan: MigrationPlan,
    accept_changes: Iterable[str] = (),
) -> Tuple[Dict[str, int], List[str], List[str]]:
    """(counts, findings, changes to review) for the new deployment's saved plan.

    A good plan imports every resource adopt.tf names (no-op or in-place update,
    never create, replace or delete), finds the user pool already in state with
    no schema item removed, forgets nothing and destroys nothing at all.

    Every adopted resource, and every setting resource the plan creates or
    updates on a kept bucket, is also read attribute by attribute against
    :data:`REFUSED_CHANGES`. A refused transition is a finding unless
    ``accept_changes`` holds ``<address>:<rule>``. An attribute a row checks that
    the plan does not record at all is a finding too, because the check cannot
    see it. Every other in-place change to those resources is allowed and
    returned in the third list for the operator to read.

    A setting resource is recognised by its ``bucket``; an imported bucket's name
    is known at plan time, so a setting whose ``bucket`` is unknown belongs to a
    bucket the apply creates.
    """
    accepted = set(accept_changes)
    findings: List[str] = []
    review: List[str] = []
    by_address = {rc["address"]: rc for rc in plan_json.get("resource_changes", [])}
    counts = {"import": 0, "add": 0, "change": 0, "destroy": 0}
    for rc in by_address.values():
        actions = rc["change"]["actions"]
        if rc["change"].get("importing"):
            counts["import"] += 1
        if "create" in actions:
            counts["add"] += 1
        if "update" in actions:
            counts["change"] += 1
        if "delete" in actions:
            counts["destroy"] += 1
            findings.append(
                f"{rc['address']} would be "
                + ("replaced" if "create" in actions else "destroyed")
                + "; an adoption destroys nothing, so stop and find out why"
            )
        if "forget" in actions:
            findings.append(
                f"{rc['address']} would be forgotten (removed from state without "
                "being destroyed); an adoption forgets nothing, so stop and find out "
                "why"
            )

    kept_buckets = {b["bucket"] for b in plan.adopted_buckets} | {
        str(r.physical_id)
        for r in plan.resources
        if r.verdict == ADOPT_BY_PARAMETER
        and r.old_type in ("aws_s3_bucket", "awscc_s3_bucket")
    }
    subjects: List[Tuple[Dict[str, Any], bool]] = []
    for r in plan.imports:
        rc = by_address.get(r.new_address or "")
        if rc is not None:
            subjects.append((rc, False))
    for rc in by_address.values():
        if rc.get("type") not in _BUCKET_SETTING_CHECKED:
            continue
        actions = rc["change"]["actions"]
        if not {"create", "update"} & set(actions):
            continue
        after = rc["change"].get("after") or {}
        before = rc["change"].get("before") or {}
        if (after.get("bucket") or before.get("bucket")) in kept_buckets:
            subjects.append((rc, "create" in actions))

    for rc, creating in subjects:
        change = rc["change"]
        rows = [row for row in REFUSED_CHANGES if rc.get("type") in row.types]
        if not rows:
            continue
        before = {} if creating else (change.get("before") or {})
        after = change.get("after") or {}
        unknown = change.get("after_unknown") or {}
        for row in rows:
            attr = row.attribute
            is_unknown = _unknown(unknown, [attr])
            if (not creating and attr not in before) or (
                attr not in after and not is_unknown
            ):
                findings.append(
                    f"{rc['address']}: the plan does not record `{attr}`, so this "
                    f"command cannot see whether the apply {row.reason}"
                )
                continue
            b = None if creating else before.get(attr)
            if row.refuse(b, after.get(attr), creating, is_unknown):
                key = f"{rc['address']}:{row.rule}"
                if key in accepted:
                    review.append(f"{rc['address']}: {row.reason} (accepted)")
                else:
                    findings.append(
                        f"{rc['address']}: the apply {row.reason}"
                        + (
                            ""
                            if creating
                            else f" ({attr}: {b!r} -> {after.get(attr)!r})"
                        )
                        + ". Find out why before applying; if it is intended, pass "
                        f"--accept-change {key}"
                    )
        if creating:
            review.append(
                f"{rc['address']}: created on kept bucket {after.get('bucket')}"
            )
        elif "update" in change["actions"]:
            attrs = _changed_attributes(change)
            if attrs:
                review.append(f"{rc['address']}: changes {', '.join(attrs)}")

    for r in plan.imports:
        rc = by_address.get(r.new_address or "")
        if rc is None:
            findings.append(f"{r.new_address} is not in the plan")
            continue
        actions = rc["change"]["actions"]
        if not r.import_in_adopt_tf:
            if rc["change"].get("importing") or "create" in actions:
                findings.append(
                    f"{r.new_address} is not in state yet; run `adopt-user-pool` first"
                )
            if "update" in actions and _schema_items_change(rc):
                findings.append(
                    f"{r.new_address} would modify or remove existing schema items, "
                    "which Cognito refuses; run `adopt-user-pool --apply` to normalise "
                    "its schema in state"
                )
            continue
        imp = rc["change"].get("importing")
        if not imp:
            findings.append(f"{r.new_address} is in the plan but not as an import")
        elif imp.get("id") != r.import_id:
            findings.append(
                f"{r.new_address} imports {imp.get('id')!r}, expected {r.import_id!r}"
            )
        if not set(actions) <= {"no-op", "update"}:
            findings.append(
                f"{r.new_address} plans {actions}, expected an import in place"
            )
    return counts, findings, review


def _schema_items_change(rc: Dict[str, Any]) -> bool:
    before = (rc["change"].get("before") or {}).get("schema") or []
    after = {
        i.get("name"): i for i in (rc["change"].get("after") or {}).get("schema") or []
    }
    for item in before:
        want = after.get(item.get("name"))
        if want is not None and _module_created_form(want) != _module_created_form(
            item
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Phase: detach and the old deployment's destroy plan
# ---------------------------------------------------------------------------

# Types whose deletion is fine even though they name an adopted pool: the old
# deployment's own app clients and its WAF association.
_POOL_REFERENCE_OK = frozenset(
    {
        "aws_cognito_user_pool_client",
        "aws_wafv2_web_acl_association",
        "aws_cognito_identity_pool",
        "aws_cognito_identity_pool_roles_attachment",
    }
)
_IDENTITY_ATTRS = ("bucket", "table_name", "key_id", "user_pool_id")
_PRIMARY_ATTRS = ("id", "name", "arn")


def check_destroy_plan(
    destroy_json: Dict[str, Any], plan: MigrationPlan
) -> Tuple[List[str], Dict[str, Any]]:
    """(findings, details) for the old deployment's destroy plan.

    Refuses, before reading a single change, anything that is not a destroy plan
    of the old deployment: JSON without ``format_version`` and a non-empty
    ``resource_changes`` list (a state file, ``{}``, a plan with no changes), a
    plan Terraform marked ``errored``, a change that is not a delete (or a no-op),
    and a plan none of whose deletions is an address the migration plan recorded
    for the old deployment. Then a finding for each deletion of an address the
    migration detached (the detach did not happen), of an address the migration
    plan never saw (the old deployment changed since), and of anything that names
    a kept bucket, table, key, pool or user.
    """
    if not isinstance(destroy_json, dict):
        return ["this is not a Terraform plan (expected a JSON object)"], {}
    if "resources" in destroy_json and "version" in destroy_json:
        return [
            "this is a Terraform state file, not a plan; pass `terraform show -json` "
            "of `terraform plan -destroy -out=<file>` in the old deployment's "
            "directory"
        ], {}
    changes = destroy_json.get("resource_changes")
    if not destroy_json.get("format_version") or not isinstance(changes, list):
        return [
            "this is not `terraform show -json` output of a saved plan (no "
            "`format_version` and `resource_changes`)"
        ], {}
    if destroy_json.get("errored"):
        return [
            "Terraform marked this plan as errored, so it may not list everything "
            "the destroy would delete; fix the error and plan again"
        ], {}
    managed = [rc for rc in changes if rc.get("mode", "managed") == "managed"]
    if not managed:
        return [
            "the plan has no resource changes, so it shows nothing about what the "
            "destroy deletes; is this the destroy plan of the old deployment?"
        ], {}

    recorded = {r.old_address: r for r in plan.resources if r.old_address}
    findings: List[str] = []
    deletes: List[Dict[str, Any]] = []
    for rc in managed:
        actions = rc.get("change", {}).get("actions") or []
        if actions == ["no-op"]:
            continue
        if actions != ["delete"]:
            findings.append(
                f"{rc.get('address')} plans {actions}; a destroy plan only deletes, "
                "so this is not a destroy plan (was it saved with `-destroy`?)"
            )
            continue
        deletes.append(rc)
    if findings:
        return findings, {"deletions": len(deletes)}
    covered = [rc for rc in deletes if rc.get("address") in recorded]
    if not covered:
        return [
            "none of this plan's deletions is an address the migration plan recorded "
            "for the old deployment, so this is not the old deployment's destroy "
            "plan (or the plan was built from another state)"
        ], {"deletions": len(deletes)}

    kept = {
        str(r.physical_id)
        for r in plan.resources
        if r.detach and r.physical_id and r.verdict != SETTING
    }
    if plan.pool_id:
        kept.add(plan.pool_id)
    unrecorded: List[str] = []
    for rc in deletes:
        address = rc.get("address")
        planned = recorded.get(address)
        if planned is not None and planned.detach:
            findings.append(
                f"{address} is in the destroy plan, but the migration detached it "
                "from the old state; run `detach --apply` first"
            )
            continue
        if planned is None:
            unrecorded.append(address)
        before = rc["change"].get("before") or {}
        attrs = list(_IDENTITY_ATTRS)
        if rc.get("type") in _POOL_REFERENCE_OK:
            attrs.remove("user_pool_id")
        if rc.get("type") in STATEFUL_TYPES:
            attrs += list(_PRIMARY_ATTRS)
        hits = sorted({str(before.get(a)) for a in attrs if str(before.get(a)) in kept})
        if not hits and rc.get("type") in STATEFUL_TYPES:
            # A key or user is identified by an ARN that ends in the kept id.
            hits = sorted(
                k for k in kept if str(before.get("arn", "")).endswith("/" + k)
            )
        if hits:
            findings.append(
                f"{address} ({rc.get('type')}) is in the destroy plan and touches "
                f"kept {', '.join(hits)}"
            )
    for address in unrecorded:
        findings.append(
            f"{address} is in the destroy plan but not in the migration plan, so "
            "nothing decided whether it may be destroyed; the old deployment has "
            "changed since `plan` ran, so run `plan` again from its current state"
        )
    not_in_destroy = sorted(
        a
        for a, r in recorded.items()
        if not r.detach
        and a not in {rc.get("address") for rc in deletes}
        and not a.startswith("(")
    )
    return findings, {
        "deletions": len(deletes),
        "recordedDeletions": len(covered),
        "recordedNotInDestroyPlan": not_in_destroy,
    }


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
