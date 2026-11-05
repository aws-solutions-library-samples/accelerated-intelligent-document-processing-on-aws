"""Hand-written CloudFormation -> hashicorp/aws mappings.

Each mapper lists every CloudFormation property it understands. A property not
listed is a :class:`TranslationError`, so a template change that uses a new
property fails the translation instead of being dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from . import hcl
from .awscc_schema import ANY, Hint
from .translate import NO_VALUE, STACK_NAME_VAR, TranslationError, is_intrinsic

if TYPE_CHECKING:
    from .translate import Plan, Translator

MAP = Hint("map", el=ANY)
# Spec key for provider-required attributes CloudFormation defaults silently.
DEFAULTS = "__defaults__"

# Spec entries:
#   ("attr", tf_name)            plain attribute
#   ("json", tf_name)            jsonencode()d attribute
#   ("map", tf_name)             map with keys kept verbatim
#   ("tags", tf_name)            CloudFormation [{Key, Value}] -> map
#   ("block", tf_name, spec)     nested block (one)
#   ("blocks", tf_name, spec)    nested blocks (list)
#   ("flatten", spec)            sub-properties hoisted into the parent
Spec = dict[str, tuple]


def _tags(tr: Translator, value: Any) -> str:
    if is_intrinsic(value):
        raise TranslationError(f"{tr._current}: Tags must be a literal list")
    items = []
    for t in value:
        if not isinstance(t.get("Key"), str):
            raise TranslationError(f"{tr._current}: tag key must be literal")
        items.append((t["Key"], tr.expr(t["Value"])))
    return hcl.obj(items, quote_keys=True)


def render_spec(tr: Translator, spec: Spec, props: dict[str, Any], where: str):
    lines: list[str] = []
    set_attrs: set[str] = set()
    for k, v in props.items():
        if k == DEFAULTS:
            continue
        entry = spec.get(k)
        if entry is None:
            raise TranslationError(f"{where}: unmapped property {k!r}")
        kind = entry[0]
        if kind != "flatten":
            set_attrs.add(entry[1])
        if kind == "attr":
            lines.append(f"{entry[1]} = {tr.expr(v)}")
        elif kind == "json":
            if isinstance(v, str) or is_intrinsic(v) and "Fn::Sub" in v:
                lines.append(f"{entry[1]} = {tr.expr(v)}")
            else:
                lines.append(f"{entry[1]} = jsonencode({tr.expr(v, json_ctx=True)})")
        elif kind == "map":
            lines.append(f"{entry[1]} = {tr.expr(v, MAP)}")
        elif kind == "tags":
            lines.append(f"{entry[1]} = {_tags(tr, v)}")
        elif kind == "flatten":
            lines.extend(render_spec(tr, entry[1], v, f"{where}.{k}"))
        elif kind == "block":
            lines.append(_block(tr, entry[1], entry[2], v, f"{where}.{k}"))
        elif kind == "blocks":
            if is_intrinsic(v):
                raise TranslationError(f"{where}.{k}: block list must be literal")
            for i, item in enumerate(v):
                lines.append(_block(tr, entry[1], entry[2], item, f"{where}.{k}[{i}]"))
        else:
            raise AssertionError(kind)
    for tf_attr, value in spec.get(DEFAULTS, ("", {}))[1].items():
        if tf_attr not in set_attrs:
            lines.append(f"{tf_attr} = {value}")
    return lines


def _if_leaves(tr: Translator, v: Any, guard: list[str]) -> list[tuple[str, Any]]:
    """Flatten an Fn::If tree into (guard expression, value) leaves."""
    if isinstance(v, dict) and set(v) == {"Fn::If"}:
        cond, a, b = v["Fn::If"]
        c = tr.cond_ref(cond)
        return _if_leaves(tr, a, guard + [c]) + _if_leaves(tr, b, guard + [f"!{c}"])
    if v == NO_VALUE:
        return []
    return [(" && ".join(guard) if guard else "true", v)]


def _block(tr: Translator, name: str, spec: Spec, v: Any, where: str) -> str:
    if isinstance(v, dict) and set(v) == {"Fn::If"}:
        # One dynamic block per branch that has a value; the guards are
        # mutually exclusive, so at most one renders.
        out = []
        for guard, value in _if_leaves(tr, v, []):
            if is_intrinsic(value):
                raise TranslationError(f"{where}: block value must be literal")
            body = render_spec(tr, spec, value, where)
            out.append(
                hcl.block(
                    f'dynamic "{name}"',
                    [
                        f"for_each = ({guard}) ? [1] : []",
                        hcl.block("content", body).rstrip(),
                    ],
                ).rstrip()
            )
        return "\n".join(out)
    if is_intrinsic(v):
        raise TranslationError(f"{where}: block value must be literal")
    return hcl.block(name, render_spec(tr, spec, v, where)).rstrip()


def default_name(logical_id: str, limit: int) -> str:
    """Deterministic name for a type whose Terraform resource requires one."""
    keep = limit - 21 - 9
    short = logical_id[:keep]
    return (
        f'"${{substr(var.{STACK_NAME_VAR}, 0, 20)}}-{short}-'
        f'${{substr(md5("${{var.{STACK_NAME_VAR}}}-{logical_id}"), 0, 8)}}"'
    )


@dataclass
class SpecMapper:
    tf_type: str
    spec: Spec
    ref_attr: str
    attrs: dict[str, Any]
    required_name: tuple[str, str, int] | None = None  # (cfn prop, tf attr, max)
    extra: Callable[[Translator, Plan, dict[str, Any]], list[str]] | None = None

    def getatt(self, attr: str) -> Any:
        if attr not in self.attrs:
            raise TranslationError(f"{self.tf_type}: unmapped GetAtt {attr}")
        return self.attrs[attr]

    def plan(self, tr: Translator, plan: Plan) -> None:
        return None

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = dict(plan.props)
        lines = render_spec(tr, self.spec, props, plan.logical_id)
        if self.required_name:
            cfn_prop, tf_attr, limit = self.required_name
            if cfn_prop not in props:
                lines.insert(0, f"{tf_attr} = {default_name(plan.logical_id, limit)}")
        head, tail = tr.meta_lines(plan, res)
        out = hcl.block(f'resource "{self.tf_type}" "{plan.name}"', head + lines + tail)
        return out


# --------------------------------------------------------------- IAM role
class IamRoleMapper:
    """One CloudFormation role becomes a role, its inline policies and its
    managed-policy attachments. Anything referencing the role also waits for
    those, which is what CloudFormation's single resource guarantees."""

    tf_type = "aws_iam_role"
    ref_attr = "name"
    spec: Spec = {
        "RoleName": ("attr", "name"),
        "Path": ("attr", "path"),
        "AssumeRolePolicyDocument": ("json", "assume_role_policy"),
        "PermissionsBoundary": ("attr", "permissions_boundary"),
        "Description": ("attr", "description"),
        "MaxSessionDuration": ("attr", "max_session_duration"),
        "Tags": ("tags", "tags"),
    }

    def getatt(self, attr: str) -> Any:
        return {"Arn": "arn", "RoleId": "unique_id"}[attr]

    def _policies(self, plan: Plan) -> list[Any]:
        pol = plan.props.get("Policies", [])
        if is_intrinsic(pol):
            raise TranslationError(f"{plan.logical_id}: Policies must be literal")
        return pol

    def plan(self, tr: Translator, plan: Plan) -> None:
        for i, _ in enumerate(self._policies(plan)):
            plan.extra_deps.append(f"aws_iam_role_policy.{plan.name}_{i}")
        if "ManagedPolicyArns" in plan.props:
            plan.extra_deps.append(f"aws_iam_role_policy_attachment.{plan.name}")

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = {
            k: v
            for k, v in plan.props.items()
            if k not in ("Policies", "ManagedPolicyArns")
        }
        lines = render_spec(tr, self.spec, props, plan.logical_id)
        head, tail = tr.meta_lines(plan, res)
        out = [hcl.block(f'resource "aws_iam_role" "{plan.name}"', head + lines + tail)]
        role = f"aws_iam_role.{plan.name}{'[0]' if plan.condition else ''}.name"
        for i, p in enumerate(self._policies(plan)):
            cond_lines = []
            body = p
            if isinstance(p, dict) and set(p) == {"Fn::If"}:
                c, a, b = p["Fn::If"]
                if b != NO_VALUE:
                    raise TranslationError(f"{plan.logical_id}: policy Fn::If shape")
                body = a
                cond_lines.append(f"count = {tr.cond_ref(c)} ? 1 : 0")
            if plan.condition:
                role_cond = tr.cond_ref(plan.condition)
                if cond_lines:
                    pol_cond = tr.cond_ref(p["Fn::If"][0])
                    cond_lines = [f"count = ({role_cond} && {pol_cond}) ? 1 : 0"]
                else:
                    cond_lines.append(f"count = {role_cond} ? 1 : 0")
            doc = body["PolicyDocument"]
            policy = (
                tr.expr(doc)
                if is_intrinsic(doc)
                else f"jsonencode({tr.expr(doc, json_ctx=True)})"
            )
            out.append(
                hcl.block(
                    f'resource "aws_iam_role_policy" "{plan.name}_{i}"',
                    cond_lines
                    + [
                        f"name = {tr.expr(body['PolicyName'])}",
                        f"role = {role}",
                        f"policy = {policy}",
                    ],
                )
            )
        if "ManagedPolicyArns" in plan.props:
            arns = tr.expr(plan.props["ManagedPolicyArns"])
            each = f"{{ for i, a in {arns} : tostring(i) => a }}"
            if plan.condition:
                each = f"{tr.cond_ref(plan.condition)} ? {each} : {{}}"
            out.append(
                hcl.block(
                    f'resource "aws_iam_role_policy_attachment" "{plan.name}"',
                    [
                        f"for_each = {each}",
                        f"role = {role}",
                        "policy_arn = each.value",
                    ],
                )
            )
        return "\n".join(out)


class LambdaMapper(SpecMapper):
    """``Code.ZipFile`` (inline source) has no provider equivalent; it becomes
    an ``archive_file`` holding the source under the file name CloudFormation
    uses (``index.py`` / ``index.js``)."""

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        code = plan.props.get("Code", {})
        if not (isinstance(code, dict) and "ZipFile" in code):
            return super().emit(tr, plan, res)
        if set(code) != {"ZipFile"}:
            raise TranslationError(f"{plan.logical_id}: ZipFile mixed with S3 code")
        runtime = str(plan.props.get("Runtime", ""))
        if runtime.startswith("python"):
            fname = "index.py"
        elif runtime.startswith("nodejs"):
            fname = "index.js"
        else:
            raise TranslationError(f"{plan.logical_id}: ZipFile for runtime {runtime}")
        zip_name = f"{plan.name}_zipfile"
        data = hcl.block(
            f'data "archive_file" "{zip_name}"',
            [
                'type = "zip"',
                f"source_content = {tr.expr(code['ZipFile'])}",
                f'source_content_filename = "{fname}"',
                f'output_path = "${{path.module}}/.build/{zip_name}.zip"',
            ],
        )
        plan.props = {k: v for k, v in plan.props.items() if k != "Code"}
        try:
            body = super().emit(tr, plan, res)
        finally:
            plan.props = dict(plan.props, Code=code)
        extra = (
            f"  filename = data.archive_file.{zip_name}.output_path\n"
            f"  source_code_hash = data.archive_file.{zip_name}.output_base64sha256\n"
        )
        head, rest = body.split("\n", 1)
        return data + "\n" + head + "\n" + extra + rest


LAMBDA = LambdaMapper(
    tf_type="aws_lambda_function",
    ref_attr="function_name",
    attrs={"Arn": "arn"},
    required_name=("FunctionName", "function_name", 64),
    spec={
        "FunctionName": ("attr", "function_name"),
        "Description": ("attr", "description"),
        "Role": ("attr", "role"),
        "Handler": ("attr", "handler"),
        "Runtime": ("attr", "runtime"),
        "MemorySize": ("attr", "memory_size"),
        "Timeout": ("attr", "timeout"),
        "Architectures": ("attr", "architectures"),
        "Layers": ("attr", "layers"),
        "PackageType": ("attr", "package_type"),
        "KmsKeyArn": ("attr", "kms_key_arn"),
        "ReservedConcurrentExecutions": ("attr", "reserved_concurrent_executions"),
        "CodeSigningConfigArn": ("attr", "code_signing_config_arn"),
        "Tags": ("tags", "tags"),
        "Code": (
            "flatten",
            {
                "S3Bucket": ("attr", "s3_bucket"),
                "S3Key": ("attr", "s3_key"),
                "S3ObjectVersion": ("attr", "s3_object_version"),
                "ImageUri": ("attr", "image_uri"),
            },
        ),
        "Environment": ("block", "environment", {"Variables": ("map", "variables")}),
        "TracingConfig": ("block", "tracing_config", {"Mode": ("attr", "mode")}),
        "DeadLetterConfig": (
            "block",
            "dead_letter_config",
            {"TargetArn": ("attr", "target_arn")},
        ),
        "EphemeralStorage": ("block", "ephemeral_storage", {"Size": ("attr", "size")}),
        "VpcConfig": (
            "block",
            "vpc_config",
            {
                "SubnetIds": ("attr", "subnet_ids"),
                "SecurityGroupIds": ("attr", "security_group_ids"),
            },
        ),
        "LoggingConfig": (
            "block",
            "logging_config",
            {
                "LogGroup": ("attr", "log_group"),
                "LogFormat": ("attr", "log_format"),
                "ApplicationLogLevel": ("attr", "application_log_level"),
                "SystemLogLevel": ("attr", "system_log_level"),
                DEFAULTS: ("defaults", {"log_format": '"Text"'}),
            },
        ),
        "ImageConfig": (
            "block",
            "image_config",
            {
                "Command": ("attr", "command"),
                "EntryPoint": ("attr", "entry_point"),
                "WorkingDirectory": ("attr", "working_directory"),
            },
        ),
    },
)

LOG_GROUP = SpecMapper(
    tf_type="aws_cloudwatch_log_group",
    ref_attr="name",
    # CloudFormation's Arn ends in ":*"; the provider's does not.
    attrs={"Arn": ("arn", '"${%s}:*"')},
    spec={
        "LogGroupName": ("attr", "name"),
        "RetentionInDays": ("attr", "retention_in_days"),
        "KmsKeyId": ("attr", "kms_key_id"),
        "LogGroupClass": ("attr", "log_group_class"),
        "Tags": ("tags", "tags"),
    },
)

CODEBUILD = SpecMapper(
    tf_type="aws_codebuild_project",
    ref_attr="name",
    attrs={"Arn": "arn"},
    required_name=("Name", "name", 150),
    spec={
        "Name": ("attr", "name"),
        "Description": ("attr", "description"),
        "ServiceRole": ("attr", "service_role"),
        "TimeoutInMinutes": ("attr", "build_timeout"),
        "QueuedTimeoutInMinutes": ("attr", "queued_timeout"),
        "EncryptionKey": ("attr", "encryption_key"),
        "ConcurrentBuildLimit": ("attr", "concurrent_build_limit"),
        "Tags": ("tags", "tags"),
        "Artifacts": (
            "block",
            "artifacts",
            {
                "Type": ("attr", "type"),
                "Location": ("attr", "location"),
                "Name": ("attr", "name"),
                "Packaging": ("attr", "packaging"),
            },
        ),
        "Environment": (
            "block",
            "environment",
            {
                "Type": ("attr", "type"),
                "ComputeType": ("attr", "compute_type"),
                "Image": ("attr", "image"),
                "PrivilegedMode": ("attr", "privileged_mode"),
                "ImagePullCredentialsType": ("attr", "image_pull_credentials_type"),
                "EnvironmentVariables": (
                    "blocks",
                    "environment_variable",
                    {
                        "Name": ("attr", "name"),
                        "Value": ("attr", "value"),
                        "Type": ("attr", "type"),
                    },
                ),
            },
        ),
        "Source": (
            "block",
            "source",
            {
                "Type": ("attr", "type"),
                "Location": ("attr", "location"),
                "BuildSpec": ("attr", "buildspec"),
                "GitCloneDepth": ("attr", "git_clone_depth"),
            },
        ),
        "LogsConfig": (
            "block",
            "logs_config",
            {
                "CloudWatchLogs": (
                    "block",
                    "cloudwatch_logs",
                    {
                        "Status": ("attr", "status"),
                        "GroupName": ("attr", "group_name"),
                        "StreamName": ("attr", "stream_name"),
                    },
                ),
            },
        ),
        "VpcConfig": (
            "block",
            "vpc_config",
            {
                "VpcId": ("attr", "vpc_id"),
                "Subnets": ("attr", "subnets"),
                "SecurityGroupIds": ("attr", "security_group_ids"),
            },
        ),
        "Cache": (
            "block",
            "cache",
            {
                "Type": ("attr", "type"),
                "Location": ("attr", "location"),
                "Modes": ("attr", "modes"),
            },
        ),
    },
)


class PerTargetPolicyMapper:
    """A policy resource that applies one document to a list of targets
    (``AWS::SQS::QueuePolicy`` -> queues, ``AWS::IAM::Policy`` -> roles, users
    and groups) becomes one Terraform resource per target."""

    ref_attr = "id"

    def __init__(self, tf_type: str, targets: dict[str, tuple[str, str]], name_prop):
        self.tf_type = tf_type
        self.targets = targets  # cfn prop -> (tf type, tf target attr)
        self.name_prop = name_prop

    def getatt(self, attr: str) -> Any:
        raise TranslationError(f"{self.tf_type}: unmapped GetAtt {attr}")

    def plan(self, tr: Translator, plan: Plan) -> None:
        return None

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = dict(plan.props)
        doc = props.pop("PolicyDocument")
        name = props.pop(self.name_prop, None) if self.name_prop else None
        policy = (
            tr.expr(doc)
            if is_intrinsic(doc) or isinstance(doc, str)
            else f"jsonencode({tr.expr(doc, json_ctx=True)})"
        )
        head, tail = tr.meta_lines(plan, res)
        out = []
        for prop in list(props):
            if prop not in self.targets:
                raise TranslationError(f"{plan.logical_id}: unmapped property {prop!r}")
            tf_type, attr = self.targets[prop]
            items = props.pop(prop)
            if is_intrinsic(items):
                raise TranslationError(f"{plan.logical_id}.{prop}: must be literal")
            for i, target in enumerate(items):
                lines = list(head) + [
                    f"{attr} = {tr.expr(target)}",
                    f"policy = {policy}",
                ]
                if name is not None:
                    lines.insert(len(head), f"name = {tr.expr(name)}")
                label = f"{plan.name}_{prop.lower()}_{i}"
                out.append(hcl.block(f'resource "{tf_type}" "{label}"', lines + tail))
        return "\n".join(out)


MAPPERS: dict[str, Any] = {
    "AWS::SQS::QueuePolicy": PerTargetPolicyMapper(
        "aws_sqs_queue_policy", {"Queues": ("aws_sqs_queue_policy", "queue_url")}, None
    ),
    "AWS::IAM::Policy": PerTargetPolicyMapper(
        "aws_iam_role_policy",
        {
            "Roles": ("aws_iam_role_policy", "role"),
            "Users": ("aws_iam_user_policy", "user"),
            "Groups": ("aws_iam_group_policy", "group"),
        },
        "PolicyName",
    ),
    "AWS::IAM::Role": IamRoleMapper(),
    "AWS::Lambda::Function": LAMBDA,
    "AWS::Logs::LogGroup": LOG_GROUP,
    "AWS::CodeBuild::Project": CODEBUILD,
}

# Types emitted as hashicorp/aws in hybrid mode even though awscc has them.
HYBRID_TYPES = frozenset(
    {"AWS::IAM::Role", "AWS::Lambda::Function", "AWS::Logs::LogGroup"}
)
