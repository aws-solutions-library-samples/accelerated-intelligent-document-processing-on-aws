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
PLAN_FORMAT = 1

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
        "aws_kms_key",
        "awscc_kms_key",
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
    }
)

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
class ModuleSource:
    """What the migration reads from the installed module's ``.tf`` files."""

    root: Path
    texts: Dict[str, str] = field(
        default_factory=dict
    )  # module path ("" = root) -> text
    sources: Dict[str, str] = field(default_factory=dict)  # call name -> directory

    @classmethod
    def read(cls, root: Path) -> "ModuleSource":
        src = cls(root=Path(root))
        src.texts[""] = _read_tf(src.root)
        for m in re.finditer(
            r'^module "([^"]+)" \{\n(.*?)^\}\n', src.texts[""], re.S | re.M
        ):
            s = re.search(r'^\s*source\s*=\s*"\./([^"]+)"', m.group(2), re.M)
            if s:
                src.sources[m.group(1)] = s.group(1)
                src.texts[m.group(1)] = _read_tf(src.root / s.group(1))
        return src

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
            scope = call
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

    def expiry_rules(self, bucket_resource: str) -> List[str]:
        """The ``expiration { days = ... }`` expressions of every lifecycle rule
        the module puts on ``aws_s3_bucket.<bucket_resource>`` (root module)."""
        text = self.texts.get("", "")
        exprs: List[str] = []
        pat = re.compile(
            r'^resource "aws_s3_bucket_lifecycle_configuration" "[^"]+" \{\n(.*?)^\}\n',
            re.S | re.M,
        )
        ref = re.compile(
            rf"bucket\s*=\s*aws_s3_bucket\.{re.escape(bucket_resource)}(\[0\])?\.id"
        )
        for m in pat.finditer(text):
            body = m.group(1)
            if not ref.search(body):
                continue
            exprs.extend(
                e.strip()
                for e in re.findall(r"expiration \{\s*days\s*=\s*([^\n]+)", body)
            )
        return exprs

    def variable_default(self, name: str) -> Any:
        body = self._block(self.texts.get("", ""), "variable", name)
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


def _read_tf(directory: Path) -> str:
    if not directory.is_dir():
        return ""
    return "\n".join(p.read_text() for p in sorted(directory.glob("*.tf")))


def _counted(body: str) -> bool:
    return re.search(r"^  (count|for_each)\s*=", body, re.M) is not None


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
        if _is_bucket_setting(inst.type):
            parent = a.get(BUCKET_SETTING_ATTR)
            if parent in kept_buckets:
                pr.verdict, pr.detach = SETTING, True
                pr.note = (
                    f"A setting of kept bucket {parent}; detached so destroying the old "
                    "deployment does not strip it. The module writes its own."
                )
        elif inst.type in TABLE_SETTING_TYPES:
            parent = str(a.get(TABLE_SETTING_TYPES[inst.type]) or "").split("/")[-1]
            if parent in kept_tables:
                pr.verdict, pr.detach = SETTING, True
                pr.note = f"Belongs to kept table {parent}; detached, not destroyed."
        elif inst.type in KEY_SETTING_TYPES:
            if (
                str(a.get(KEY_SETTING_TYPES[inst.type]) or "").split("/")[-1]
                in kept_keys
            ):
                pr.verdict, pr.detach = SETTING, True
                pr.note = "Belongs to the adopted key; detached, not destroyed."
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
        elif inst.type in STATEFUL_TYPES:
            refusals.append(
                f"{inst.address} ({inst.type} {pr.physical_id}): a resource that holds "
                "data and that this command has no verdict for. Nothing was written."
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


def detach_commands(addresses: Sequence[str], binary: str = "terraform") -> List[str]:
    """Shell lines that back up the old state and remove ``addresses`` from it."""
    if not addresses:
        return []
    quoted = " ".join("'" + a.replace("'", "'\\''") + "'" for a in addresses)
    return [
        f"{binary} state pull > old-state-backup-before-detach.tfstate",
        f"{binary} state rm {quoted}",
    ]


# ---------------------------------------------------------------------------
# Phase: retention
# ---------------------------------------------------------------------------


def resolve_days(
    expr: str, values: Dict[str, Any], module: ModuleSource
) -> Optional[int]:
    expr = expr.strip()
    if re.fullmatch(r"\d+", expr):
        return int(expr)
    m = re.fullmatch(r"var\.([A-Za-z0-9_]+)", expr)
    if m:
        v = values.get(m.group(1), module.variable_default(m.group(1)))
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def oldest_object(s3, bucket: str) -> Optional[datetime]:
    oldest: Optional[datetime] = None
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            lm = obj["LastModified"]
            if oldest is None or lm < oldest:
                oldest = lm
    return oldest


def check_retention(
    plan: MigrationPlan,
    module: ModuleSource,
    values: Dict[str, Any],
    s3,
    *,
    accept_expiry: Iterable[str] = (),
    now: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(one row per adopted bucket, findings). A finding is a bucket holding an
    object older than the shortest expiry rule the module would put on it."""
    now = now or datetime.now(timezone.utc)
    accepted = set(accept_expiry)
    rows: List[Dict[str, Any]] = []
    findings: List[str] = []
    for b in plan.adopted_buckets:
        exprs = module.expiry_rules(b["resource"])
        days = [
            d for d in (resolve_days(e, values, module) for e in exprs) if d is not None
        ]
        expiry = min(days) if days else None
        oldest = oldest_object(s3, b["bucket"])
        age = (
            None
            if oldest is None
            else math.ceil((now - oldest).total_seconds() / 86400)
        )
        variables = sorted(
            {
                m.group(1)
                for e in exprs
                for m in [re.fullmatch(r"var\.(\w+)", e.strip())]
                if m
            }
        )
        row = {
            "kind": b["kind"],
            "bucket": b["bucket"],
            "expiryDays": expiry,
            "oldestObjectAgeDays": age,
            "variables": variables,
            "ok": expiry is None
            or age is None
            or age <= expiry
            or b["kind"] in accepted,
        }
        if expiry is not None and age is not None and age > expiry:
            how = (
                "raise " + " or ".join(variables) + " (as its CloudFormation parameter "
                "in --parameters) to at least " + str(age)
                if variables
                else "the rule's period is fixed in the module, so copy the objects you "
                "need elsewhere first"
            )
            note = (
                f"accepted with --accept-expiry {b['kind']}"
                if b["kind"] in accepted
                else ""
            )
            msg = (
                f"{b['kind']} {b['bucket']}: its oldest object is {age} days old and the "
                f"module expires objects after {expiry} days, so the first apply would "
                f"start deleting it. To keep them, {how}; or pass --accept-expiry "
                f"{b['kind']}."
            )
            if not note:
                findings.append(msg)
            row["note"] = note or msg
        rows.append(row)
    return rows, findings


# ---------------------------------------------------------------------------
# Phase: freeze
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "ABORTED"})


def upload_rule_states(plan: MigrationPlan, events) -> List[Dict[str, str]]:
    out = []
    for rule in plan.upload_rules:
        resp = events.describe_rule(
            Name=rule["name"], EventBusName=rule["eventBusName"]
        )
        out.append({**rule, "state": resp.get("State", "UNKNOWN")})
    return out


def in_flight_documents(
    table_name: str, ddb, limit: int = 1000
) -> List[Dict[str, str]]:
    """Documents whose ``ObjectStatus`` is not terminal, from a tracking table scan."""
    found: List[Dict[str, str]] = []
    kwargs: Dict[str, Any] = {
        "TableName": table_name,
        "FilterExpression": "begins_with(PK, :doc) AND NOT (ObjectStatus IN (:c, :f, :a))",
        "ExpressionAttributeValues": {
            ":doc": {"S": "doc#"},
            ":c": {"S": "COMPLETED"},
            ":f": {"S": "FAILED"},
            ":a": {"S": "ABORTED"},
        },
        "ProjectionExpression": "PK, ObjectStatus",
    }
    while True:
        resp = ddb.scan(**kwargs)
        for item in resp.get("Items", []):
            found.append(
                {
                    "key": item["PK"]["S"][len("doc#") :],
                    "status": (item.get("ObjectStatus") or {}).get("S", ""),
                }
            )
            if len(found) >= limit:
                return found
        if "LastEvaluatedKey" not in resp:
            return found
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ---------------------------------------------------------------------------
# Phase: configuration fix
# ---------------------------------------------------------------------------

REVISION_COUNTERS = ("LatestRevision", "PublishedRevision")
CONFIG_PREFIX = "Config#"
REVISION_INDEX_PREFIX = "ConfigRevIndex#"


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


def check_adoption_plan(
    plan_json: Dict[str, Any], plan: MigrationPlan
) -> Tuple[Dict[str, int], List[str]]:
    """(counts, findings) for the new deployment's saved plan.

    A good plan imports every resource adopt.tf names (no-op or in-place update,
    never create, replace or delete), finds the user pool already in state with
    no schema item removed, and destroys nothing at all.
    """
    findings: List[str] = []
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
            before = {
                i.get("name")
                for i in (rc["change"].get("before") or {}).get("schema") or []
            }
            after = {
                i.get("name")
                for i in (rc["change"].get("after") or {}).get("schema") or []
            }
            if before - after or ("update" in actions and _schema_items_change(rc)):
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
    return counts, findings


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


def check_destroy_plan(destroy_json: Dict[str, Any], plan: MigrationPlan) -> List[str]:
    """Deletions in the old deployment's destroy plan that touch something kept."""
    kept = {
        str(r.physical_id)
        for r in plan.resources
        if r.detach and r.physical_id and r.verdict != SETTING
    }
    if plan.pool_id:
        kept.add(plan.pool_id)
    findings = []
    for rc in destroy_json.get("resource_changes", []):
        if "delete" not in rc["change"]["actions"]:
            continue
        before = rc["change"].get("before") or {}
        attrs = list(_IDENTITY_ATTRS)
        if rc["type"] in _POOL_REFERENCE_OK:
            attrs.remove("user_pool_id")
        if rc["type"] in STATEFUL_TYPES:
            attrs += list(_PRIMARY_ATTRS)
        hits = sorted({str(before.get(a)) for a in attrs if str(before.get(a)) in kept})
        if not hits and rc["type"] in STATEFUL_TYPES:
            # A key or user is identified by an ARN that ends in the kept id.
            hits = sorted(
                k for k in kept if str(before.get("arn", "")).endswith("/" + k)
            )
        if hits:
            findings.append(
                f"{rc['address']} ({rc['type']}) is in the destroy plan and touches "
                f"kept {', '.join(hits)}"
            )
    return findings


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
