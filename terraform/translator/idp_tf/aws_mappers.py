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
        elif kind == "kms_alias_arn":
            # CodeBuild returns a short alias ("alias/aws/s3") as its full ARN.
            if isinstance(v, str) and v.startswith("alias/"):
                lines.append(
                    f'{entry[1]} = "arn:${{local.pseudo.partition}}:kms:'
                    f'${{local.pseudo.region}}:${{local.pseudo.account_id}}:{v}"'
                )
            else:
                lines.append(f"{entry[1]} = {tr.expr(v)}")
        elif kind == "state":
            # The provider names DISABLED/ENABLED the same way; a bare
            # ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS passes through.
            lines.append(f"{entry[1]} = {tr.expr(v)}")
        elif kind == "role_arn":
            # CloudFormation accepts a role name here; the provider wants an ARN.
            target = (
                tr.plans.get(v["Ref"])
                if isinstance(v, dict) and set(v) == {"Ref"}
                else None
            )
            if target is not None and target.cfn_type == "AWS::IAM::Role":
                lines.append(f"{entry[1]} = {tr.getatt(v['Ref'], 'Arn')}")
            else:
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


def default_name(logical_id: str, limit: int, global_name: bool = False) -> str:
    """Deterministic name for a type whose Terraform resource requires one.

    ``<stack>-<LogicalId>`` cut to fit, then an 8-character hash of the full
    pair. The stack name stays a prefix, as in CloudFormation's generated names,
    because templates grant access by it (``function:${AWS::StackName}*``).

    ``global_name`` is for names unique across the account rather than within
    a region (IAM roles): the region joins the hash, so the same stack name in
    two regions of one account does not collide. Only the hash changes, so the
    prefix scoped deploy roles match on is the same.
    """
    pair = f"${{var.{STACK_NAME_VAR}}}-{logical_id}"
    hashed = f"{pair}-${{local.pseudo.region}}" if global_name else pair
    return f'"${{substr("{pair}", 0, {limit - 9})}}-${{substr(md5("{hashed}"), 0, 8)}}"'


@dataclass
class SpecMapper:
    tf_type: str
    spec: Spec
    ref_attr: str
    attrs: dict[str, Any]
    required_name: tuple[str, str, int] | None = None  # (cfn prop, tf attr, max)
    extra: Callable[[Translator, Plan, dict[str, Any]], list[str]] | None = None
    #: CloudFormation property paths the provider replaces the resource for,
    #: beyond the registry's createOnlyProperties (see idp_tf.create_only).
    force_new: tuple[str, ...] = ()

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
#: A role whose template name changes is replaced. Functions and other
#: dependants take the role by ARN and update in place, so creating the new role
#: first means none of them is ever left pointing at a deleted role. Only a
#: change of ``path`` with the name unchanged would collide under this ordering,
#: and no template sets ``Path``.
ROLE_LIFECYCLE = "lifecycle {\n  create_before_destroy = true\n}"
#: A role the template leaves unnamed gets a name the translator generates, and
#: that name is kept for the life of the role, as CloudFormation keeps the name
#: it generated. Renaming it would replace the role, and a replaced role is not
#: contained: every custom resource taking the role's ARN as a property is
#: updated, its outputs are unknown until apply, and a resource taking one of
#: them as a create-only property is replaced with it. Measured on a live
#: upgrade, that replaced the knowledge base's data source.
GENERATED_NAME_ROLE_LIFECYCLE = (
    "lifecycle {\n"
    "  create_before_destroy = true\n"
    "  ignore_changes = [name, name_prefix]\n"
    "}"
)


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
        # Dependants wait for the role, its policies, and IAM propagation: a
        # service validating the role at create time (Step Functions checks
        # its log destination, Lambda its event source) otherwise sees a role
        # without permissions. CloudFormation absorbs this with internal retries.
        plan.extra_deps.append(f"time_sleep.{plan.name}_iam")

    @staticmethod
    def _retained_name(tr: Translator, plan: Plan) -> tuple[str, list[str]]:
        """A retained role's name, with a suffix fixed when it is first created.

        A delete keeps this role, so a deterministic name would collide with it
        when the same stack is deployed again; CloudFormation's random suffix
        never does. ``<stack>-<LogicalId>`` cut to fit, the 8-character hash
        ``default_name`` uses, then the creation time: 44 + 1 + 8 + 1 + 10 = 64.
        """
        stamp = f"{plan.name}_name_suffix"
        count = (
            [f"count = {tr.cond_ref(plan.condition)} ? 1 : 0"] if plan.condition else []
        )
        idx = "[0]" if plan.condition else ""
        pair = f"${{var.{STACK_NAME_VAR}}}-{plan.logical_id}"
        name = (
            f'"${{substr("{pair}", 0, 44)}}-${{substr(md5("{pair}-${{local.pseudo.region}}"), 0, 8)}}-'
            f'${{time_static.{stamp}{idx}.unix}}"'
        )
        return name, [hcl.block(f'resource "time_static" "{stamp}"', count)]

    @staticmethod
    def _has_managed(plan: Plan) -> bool:
        return plan.props.get("ManagedPolicyArns", NO_VALUE) != NO_VALUE

    def _policy_addresses(self, plan: Plan) -> list[str]:
        out = [
            f"aws_iam_role_policy.{plan.name}_{i}"
            for i, _ in enumerate(self._policies(plan))
        ]
        if self._has_managed(plan):
            out.append(f"aws_iam_role_policy_attachment.{plan.name}")
        return out

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = {
            k: v
            for k, v in plan.props.items()
            if k not in ("Policies", "ManagedPolicyArns")
        }
        lines = render_spec(tr, self.spec, props, plan.logical_id)
        stamp_blocks: list[str] = []
        lifecycle = ROLE_LIFECYCLE
        if "RoleName" not in props:
            # CloudFormation names an unnamed role <stack>-<LogicalId>-<random>,
            # and a deploying role scoped the usual way may create and pass only
            # roles under role/<stack prefix>*. Left unnamed, the provider would
            # pick terraform-<timestamp>, outside that scope. The name is
            # deterministic, so a re-plan is empty; it is unique across nested
            # modules because a module's stack name carries its parent's.
            if plan.logical_id in tr.retained:
                name, stamp_blocks = self._retained_name(tr, plan)
                # A retained role's name also carries a creation timestamp, so
                # a redeploy after a delete cannot collide with the kept role.
            else:
                name = default_name(plan.logical_id, 64, global_name=True)
            lifecycle = GENERATED_NAME_ROLE_LIFECYCLE
            lines.insert(0, f"name = {name}")
        head, tail = tr.meta_lines(plan, res)
        tail = tail + [lifecycle]
        out = stamp_blocks + [
            hcl.block(f'resource "aws_iam_role" "{plan.name}"', head + lines + tail)
        ]
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
        sleep_deps = [f"aws_iam_role.{plan.name}"] + self._policy_addresses(plan)
        idx = "[0]" if plan.condition else ""
        inline = [
            f"aws_iam_role_policy.{plan.name}_{i}"
            for i, p in enumerate(self._policies(plan))
            if not (isinstance(p, dict) and set(p) == {"Fn::If"})
        ]
        # Re-wait when a grant changes on an upgrade, not only on create.
        trigger = (
            "triggers = { policies = sha1(jsonencode(["
            + ", ".join(f"{a}{idx}.policy" for a in inline)
            + "])) }"
            if inline
            else None
        )
        out.append(
            hcl.block(
                f'resource "time_sleep" "{plan.name}_iam"',
                (
                    [f"count = {tr.cond_ref(plan.condition)} ? 1 : 0"]
                    if plan.condition
                    else []
                )
                + ['create_duration = "10s"']
                + ([trigger] if trigger else [])
                + ["depends_on = " + hcl.lst(sleep_deps)],
            )
        )
        if self._has_managed(plan):
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
        # CloudFormation tags every function aws:cloudformation:stack-name, and
        # run-time code finds the deployment's functions by it. That key is
        # reserved, so a Terraform deployment uses idp:stack-name, with the same
        # values (see data_mart_rollup).
        tags = plan.props.get("Tags")
        if tags is None or isinstance(tags, list):
            original = plan.props
            plan.props = dict(
                original,
                Tags=list(tags or [])
                + [{"Key": "idp:stack-name", "Value": {"Ref": "AWS::StackName"}}],
            )
            try:
                return self._emit(tr, plan, res)
            finally:
                plan.props = original
        return self._emit(tr, plan, res)

    def _emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
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
        source = code["ZipFile"]
        if not isinstance(source, str):
            raise TranslationError(f"{plan.logical_id}: ZipFile must be literal text")
        files = {fname: source.encode()}
        # CloudFormation makes `cfnresponse` importable by inline Python code;
        # package the stand-in so the same source runs under Terraform.
        if fname == "index.py" and "cfnresponse" in source:
            from pathlib import Path

            stand_in = Path(__file__).resolve().parents[2] / "bridge" / "cfnresponse.py"
            files["cfnresponse.py"] = stand_in.read_bytes()
        from .assets import deterministic_zip

        tr.assets[f"zips/{zip_name}.zip"] = deterministic_zip(files)
        plan.props = {k: v for k, v in plan.props.items() if k != "Code"}
        try:
            body = super().emit(tr, plan, res)
        finally:
            plan.props = dict(plan.props, Code=code)
        path = f'"${{path.module}}/zips/{zip_name}.zip"'
        extra = f"  filename = {path}\n  source_code_hash = filebase64sha256({path})\n"
        head, rest = body.split("\n", 1)
        return head + "\n" + extra + rest


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
        "ServiceRole": ("role_arn", "service_role"),
        "TimeoutInMinutes": ("attr", "build_timeout"),
        "QueuedTimeoutInMinutes": ("attr", "queued_timeout"),
        "EncryptionKey": ("kms_alias_arn", "encryption_key"),
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

    def __init__(
        self,
        tf_type: str,
        targets: dict[str, tuple[str, str]],
        name_prop,
        default_version: str | None = None,
    ):
        self.tf_type = tf_type
        self.targets = targets  # cfn prop -> (tf type, tf target attr)
        self.name_prop = name_prop
        # The Version a service stores when a policy omits it. The provider
        # waits until the stored policy matches the configured one, so an
        # omitted Version never matches and the apply waits forever.
        self.default_version = default_version

    def getatt(self, attr: str) -> Any:
        raise TranslationError(f"{self.tf_type}: unmapped GetAtt {attr}")

    def plan(self, tr: Translator, plan: Plan) -> None:
        plan.virtual = True
        for prop, (tf_type, _) in self.targets.items():
            items = plan.props.get(prop, [])
            if is_intrinsic(items):
                continue
            for i, _ in enumerate(items):
                plan.extra_deps.append(f"{tf_type}.{plan.name}_{prop.lower()}_{i}")
        if self.tf_type.startswith("aws_iam_"):
            # A new IAM grant takes seconds to propagate; a dependant (often a
            # custom resource) that uses it at once is otherwise refused.
            plan.extra_deps.append(f"time_sleep.{plan.name}_iam")

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = dict(plan.props)
        doc = props.pop("PolicyDocument")
        if (
            self.default_version
            and isinstance(doc, dict)
            and not is_intrinsic(doc)
            and "Version" not in doc
        ):
            doc = {"Version": self.default_version, **doc}
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
        if self.tf_type.startswith("aws_iam_"):
            created = [a for a in plan.extra_deps if not a.startswith("time_sleep.")]
            out.append(
                hcl.block(
                    f'resource "time_sleep" "{plan.name}_iam"',
                    head
                    + ['create_duration = "15s"', "depends_on = " + hcl.lst(created)],
                )
            )
        return "\n".join(out)


class EventsRuleMapper:
    """A rule becomes ``aws_cloudwatch_event_rule`` plus one
    ``aws_cloudwatch_event_target`` per target. (Cloud Control's create for
    ``AWS::Events::Rule`` fails with an internal error for these rules.)"""

    tf_type = "aws_cloudwatch_event_rule"
    ref_attr = "name"
    #: The provider replaces a rule moved to another bus.
    force_new = ("EventBusName",)
    spec: Spec = {
        "Name": ("attr", "name"),
        "Description": ("attr", "description"),
        "ScheduleExpression": ("attr", "schedule_expression"),
        "EventPattern": ("json", "event_pattern"),
        "EventBusName": ("attr", "event_bus_name"),
        "State": ("state", "state"),
        "RoleArn": ("attr", "role_arn"),
    }
    target_spec: Spec = {
        "Id": ("attr", "target_id"),
        "Arn": ("attr", "arn"),
        "RoleArn": ("attr", "role_arn"),
        "Input": ("attr", "input"),
        "InputPath": ("attr", "input_path"),
        "InputTransformer": (
            "block",
            "input_transformer",
            {
                "InputPathsMap": ("map", "input_paths"),
                "InputTemplate": ("attr", "input_template"),
            },
        ),
        "DeadLetterConfig": ("block", "dead_letter_config", {"Arn": ("attr", "arn")}),
        "RetryPolicy": (
            "block",
            "retry_policy",
            {
                "MaximumEventAgeInSeconds": ("attr", "maximum_event_age_in_seconds"),
                "MaximumRetryAttempts": ("attr", "maximum_retry_attempts"),
                # The provider sends 0 for an unset member, which EventBridge
                # rejects; these are the service defaults.
                DEFAULTS: (
                    "defaults",
                    {
                        "maximum_event_age_in_seconds": "86400",
                        "maximum_retry_attempts": "185",
                    },
                ),
            },
        ),
        "SqsParameters": (
            "block",
            "sqs_target",
            {"MessageGroupId": ("attr", "message_group_id")},
        ),
    }

    def getatt(self, attr: str) -> Any:
        return {"Arn": "arn"}[attr]

    def _targets(self, tr: Translator, plan: Plan) -> list[tuple[str, str, Any]]:
        """(label, guard, target) per target; a conditional target is guarded."""
        targets = plan.props.get("Targets", [])
        if is_intrinsic(targets):
            raise TranslationError(f"{plan.logical_id}: Targets must be literal")
        out = []
        for i, target in enumerate(targets):
            leaves = _if_leaves(tr, target, [])
            for j, (guard, value) in enumerate(leaves):
                label = (
                    f"{plan.name}_{i}" if len(leaves) == 1 else f"{plan.name}_{i}_{j}"
                )
                out.append((label, guard, value))
        return out

    def plan(self, tr: Translator, plan: Plan) -> None:
        targets = plan.props.get("Targets", [])
        if not is_intrinsic(targets):
            for i, target in enumerate(targets):
                # As many as _targets() emits: none for a NoValue target.
                n = len(_if_leaves(tr, target, []))
                for j in range(n):
                    label = f"{plan.name}_{i}" if n == 1 else f"{plan.name}_{i}_{j}"
                    plan.extra_deps.append(f"aws_cloudwatch_event_target.{label}")

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = {k: v for k, v in plan.props.items() if k != "Targets"}
        lines = render_spec(tr, self.spec, props, plan.logical_id)
        if "Name" not in props:
            lines.insert(0, f"name = {default_name(plan.logical_id, 64)}")
        head, tail = tr.meta_lines(plan, res)
        out = [
            hcl.block(f'resource "{self.tf_type}" "{plan.name}"', head + lines + tail)
        ]
        idx = "[0]" if plan.condition else ""
        for label, guard, target in self._targets(tr, plan):
            body = render_spec(
                tr, self.target_spec, target, f"{plan.logical_id}.{label}"
            )
            guards = [g for g in (guard if guard != "true" else None,) if g]
            if plan.condition:
                guards.insert(0, tr.cond_ref(plan.condition))
            count = [f"count = ({' && '.join(guards)}) ? 1 : 0"] if guards else []
            rule = [f"rule = aws_cloudwatch_event_rule.{plan.name}{idx}.name"]
            if "EventBusName" in props:
                rule.append(
                    f"event_bus_name = aws_cloudwatch_event_rule.{plan.name}{idx}.event_bus_name"
                )
            out.append(
                hcl.block(
                    f'resource "aws_cloudwatch_event_target" "{label}"',
                    count + rule + body,
                )
            )
        return "\n".join(out)


class NoOpMapper:
    """``AWS::CloudFormation::WaitConditionHandle`` creates nothing; templates
    use it only as a dependency anchor."""

    tf_type = "terraform_data"
    ref_attr = "id"

    def getatt(self, attr: str) -> Any:
        raise TranslationError(f"WaitConditionHandle has no attribute {attr}")

    def plan(self, tr: Translator, plan: Plan) -> None:
        return None

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        if plan.props:
            raise TranslationError(f"{plan.logical_id}: unexpected properties")
        head, tail = tr.meta_lines(plan, res)
        return hcl.block(f'resource "terraform_data" "{plan.name}"', head + tail)


class EcrRepositoryMapper:
    """One repository becomes the repository, its lifecycle policy and its
    repository policy. ``EmptyOnDelete`` must be honoured on destroy, which the
    Cloud Control path does not do (see terraform/prototype/RESULTS.md)."""

    tf_type = "aws_ecr_repository"
    ref_attr = "name"
    spec: Spec = {
        "RepositoryName": ("attr", "name"),
        "EmptyOnDelete": ("attr", "force_delete"),
        "ImageTagMutability": ("attr", "image_tag_mutability"),
        "Tags": ("tags", "tags"),
        "ImageScanningConfiguration": (
            "block",
            "image_scanning_configuration",
            {"ScanOnPush": ("attr", "scan_on_push")},
        ),
        "EncryptionConfiguration": (
            "block",
            "encryption_configuration",
            {
                "EncryptionType": ("attr", "encryption_type"),
                "KmsKey": ("attr", "kms_key"),
            },
        ),
    }

    def getatt(self, attr: str) -> Any:
        return {"Arn": "arn", "RepositoryUri": "repository_url"}[attr]

    def plan(self, tr: Translator, plan: Plan) -> None:
        for prop, suffix in (
            ("LifecyclePolicy", "lifecycle"),
            ("RepositoryPolicyText", "policy"),
        ):
            if plan.props.get(prop, NO_VALUE) != NO_VALUE:
                plan.extra_deps.append(
                    f"aws_ecr_{suffix}_policy.{plan.name}"
                    if suffix == "lifecycle"
                    else f"aws_ecr_repository_policy.{plan.name}"
                )

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = {
            k: v
            for k, v in plan.props.items()
            if k not in ("LifecyclePolicy", "RepositoryPolicyText")
        }
        lines = render_spec(tr, self.spec, props, plan.logical_id)
        if "RepositoryName" not in props:
            lines.insert(0, f"name = lower({default_name(plan.logical_id, 256)})")
        head, tail = tr.meta_lines(plan, res)
        out = [
            hcl.block(
                f'resource "aws_ecr_repository" "{plan.name}"', head + lines + tail
            )
        ]
        repo = f"aws_ecr_repository.{plan.name}{'[0]' if plan.condition else ''}.name"
        count = (
            [f"count = {tr.cond_ref(plan.condition)} ? 1 : 0"] if plan.condition else []
        )
        lp = plan.props.get("LifecyclePolicy")
        if lp is not None and lp != NO_VALUE:
            extra = set(lp) - {"LifecyclePolicyText", "RegistryId"}
            if extra:
                raise TranslationError(f"{plan.logical_id}.LifecyclePolicy: {extra}")
            out.append(
                hcl.block(
                    f'resource "aws_ecr_lifecycle_policy" "{plan.name}"',
                    count
                    + [
                        f"repository = {repo}",
                        f"policy = {tr.expr(lp['LifecyclePolicyText'])}",
                    ],
                )
            )
        rp = plan.props.get("RepositoryPolicyText")
        if rp is not None and rp != NO_VALUE:
            policy = (
                tr.expr(rp)
                if isinstance(rp, str) or is_intrinsic(rp)
                else f"jsonencode({tr.expr(rp, json_ctx=True)})"
            )
            out.append(
                hcl.block(
                    f'resource "aws_ecr_repository_policy" "{plan.name}"',
                    count + [f"repository = {repo}", f"policy = {policy}"],
                )
            )
        return "\n".join(out)


VISIBILITY: Spec = {
    "CloudWatchMetricsEnabled": ("attr", "cloudwatch_metrics_enabled"),
    "MetricName": ("attr", "metric_name"),
    "SampledRequestsEnabled": ("attr", "sampled_requests_enabled"),
}
# WAF actions are written as empty objects: {"Allow": {}}.
WAF_ACTION: Spec = {
    "Allow": ("block", "allow", {}),
    "Block": ("block", "block", {}),
    "Count": ("block", "count", {}),
}

WAF_WEB_ACL = SpecMapper(
    tf_type="aws_wafv2_web_acl",
    ref_attr="id",
    attrs={"Arn": "arn", "Id": "id"},
    spec={
        "Name": ("attr", "name"),
        "Description": ("attr", "description"),
        "Scope": ("attr", "scope"),
        "Tags": ("tags", "tags"),
        "DefaultAction": ("block", "default_action", WAF_ACTION),
        "VisibilityConfig": ("block", "visibility_config", VISIBILITY),
        "Rules": (
            "blocks",
            "rule",
            {
                "Name": ("attr", "name"),
                "Priority": ("attr", "priority"),
                "Action": ("block", "action", WAF_ACTION),
                "OverrideAction": (
                    "block",
                    "override_action",
                    {"None": ("block", "none", {}), "Count": ("block", "count", {})},
                ),
                "Statement": (
                    "block",
                    "statement",
                    {
                        "IPSetReferenceStatement": (
                            "block",
                            "ip_set_reference_statement",
                            {"Arn": ("attr", "arn")},
                        ),
                        "ManagedRuleGroupStatement": (
                            "block",
                            "managed_rule_group_statement",
                            {
                                "Name": ("attr", "name"),
                                "VendorName": ("attr", "vendor_name"),
                            },
                        ),
                        "RateBasedStatement": (
                            "block",
                            "rate_based_statement",
                            {
                                "Limit": ("attr", "limit"),
                                "AggregateKeyType": ("attr", "aggregate_key_type"),
                            },
                        ),
                    },
                ),
                "VisibilityConfig": ("block", "visibility_config", VISIBILITY),
            },
        ),
    },
)

COGNITO_USER_POOL = SpecMapper(
    tf_type="aws_cognito_user_pool",
    ref_attr="id",
    # The registry marks none of these create-only; the provider documents each
    # as forcing a new resource, because Cognito's UpdateUserPool takes none.
    force_new=("UserPoolName", "AliasAttributes", "UsernameAttributes"),
    attrs={
        "Arn": "arn",
        "UserPoolId": "id",
        "ProviderName": "endpoint",
        "ProviderURL": ("endpoint", '"https://${%s}"'),
    },
    spec={
        "UserPoolName": ("attr", "name"),
        "AutoVerifiedAttributes": ("attr", "auto_verified_attributes"),
        "AliasAttributes": ("attr", "alias_attributes"),
        "UsernameAttributes": ("attr", "username_attributes"),
        "EmailVerificationMessage": ("attr", "email_verification_message"),
        "EmailVerificationSubject": ("attr", "email_verification_subject"),
        "MfaConfiguration": ("attr", "mfa_configuration"),
        "DeletionProtection": ("attr", "deletion_protection"),
        "UserPoolTags": ("map", "tags"),
        "AdminCreateUserConfig": (
            "block",
            "admin_create_user_config",
            {
                "AllowAdminCreateUserOnly": ("attr", "allow_admin_create_user_only"),
                "InviteMessageTemplate": (
                    "block",
                    "invite_message_template",
                    {
                        "EmailMessage": ("attr", "email_message"),
                        "EmailSubject": ("attr", "email_subject"),
                        "SMSMessage": ("attr", "sms_message"),
                        # The provider sends "" when this is unset, which
                        # Cognito rejects; this is Cognito's own default text.
                        DEFAULTS: (
                            "defaults",
                            {
                                "sms_message": '"Your username is {username} and '
                                'temporary password is {####}."'
                            },
                        ),
                    },
                ),
            },
        ),
        "EmailConfiguration": (
            "block",
            "email_configuration",
            {
                "EmailSendingAccount": ("attr", "email_sending_account"),
                "SourceArn": ("attr", "source_arn"),
                "From": ("attr", "from_email_address"),
                "ReplyToEmailAddress": ("attr", "reply_to_email_address"),
                "ConfigurationSet": ("attr", "configuration_set"),
            },
        ),
        "LambdaConfig": (
            "block",
            "lambda_config",
            {
                "PreAuthentication": ("attr", "pre_authentication"),
                "PreSignUp": ("attr", "pre_sign_up"),
                "PreTokenGeneration": ("attr", "pre_token_generation"),
                "PostAuthentication": ("attr", "post_authentication"),
                "PostConfirmation": ("attr", "post_confirmation"),
                "CustomMessage": ("attr", "custom_message"),
                "DefineAuthChallenge": ("attr", "define_auth_challenge"),
                "CreateAuthChallenge": ("attr", "create_auth_challenge"),
                "VerifyAuthChallengeResponse": (
                    "attr",
                    "verify_auth_challenge_response",
                ),
                "UserMigration": ("attr", "user_migration"),
            },
        ),
        "Policies": (
            "flatten",
            {
                "PasswordPolicy": (
                    "block",
                    "password_policy",
                    {
                        "MinimumLength": ("attr", "minimum_length"),
                        "RequireLowercase": ("attr", "require_lowercase"),
                        "RequireNumbers": ("attr", "require_numbers"),
                        "RequireSymbols": ("attr", "require_symbols"),
                        "RequireUppercase": ("attr", "require_uppercase"),
                        "TemporaryPasswordValidityDays": (
                            "attr",
                            "temporary_password_validity_days",
                        ),
                    },
                ),
            },
        ),
        "Schema": (
            "blocks",
            "schema",
            {
                "AttributeDataType": ("attr", "attribute_data_type"),
                "Mutable": ("attr", "mutable"),
                "Name": ("attr", "name"),
                "Required": ("attr", "required"),
                "DeveloperOnlyAttribute": ("attr", "developer_only_attribute"),
                "StringAttributeConstraints": (
                    "block",
                    "string_attribute_constraints",
                    {
                        "MaxLength": ("attr", "max_length"),
                        "MinLength": ("attr", "min_length"),
                    },
                ),
            },
        ),
        "UserAttributeUpdateSettings": (
            "block",
            "user_attribute_update_settings",
            {
                "AttributesRequireVerificationBeforeUpdate": (
                    "attr",
                    "attributes_require_verification_before_update",
                ),
            },
        ),
        "UsernameConfiguration": (
            "block",
            "username_configuration",
            {"CaseSensitive": ("attr", "case_sensitive")},
        ),
        "AccountRecoverySetting": (
            "block",
            "account_recovery_setting",
            {
                "RecoveryMechanisms": (
                    "blocks",
                    "recovery_mechanism",
                    {"Name": ("attr", "name"), "Priority": ("attr", "priority")},
                ),
            },
        ),
    },
)

CACHE_BEHAVIOR: Spec = {
    "AllowedMethods": ("attr", "allowed_methods"),
    "CachedMethods": ("attr", "cached_methods"),
    "Compress": ("attr", "compress"),
    "DefaultTTL": ("attr", "default_ttl"),
    "MaxTTL": ("attr", "max_ttl"),
    "MinTTL": ("attr", "min_ttl"),
    "TargetOriginId": ("attr", "target_origin_id"),
    "ViewerProtocolPolicy": ("attr", "viewer_protocol_policy"),
    "ResponseHeadersPolicyId": ("attr", "response_headers_policy_id"),
    "CachePolicyId": ("attr", "cache_policy_id"),
    "OriginRequestPolicyId": ("attr", "origin_request_policy_id"),
    "PathPattern": ("attr", "path_pattern"),
    "ForwardedValues": (
        "block",
        "forwarded_values",
        {
            "QueryString": ("attr", "query_string"),
            "Headers": ("attr", "headers"),
            "QueryStringCacheKeys": ("attr", "query_string_cache_keys"),
            "Cookies": (
                "block",
                "cookies",
                {
                    "Forward": ("attr", "forward"),
                    "WhitelistedNames": ("attr", "whitelisted_names"),
                },
            ),
        },
    ),
    # CloudFormation defaults CachedMethods to GET, HEAD; the provider requires it.
    DEFAULTS: ("defaults", {"cached_methods": '["GET", "HEAD"]'}),
}


class CloudFrontMapper(SpecMapper):
    """An origin using origin access control carries
    ``S3OriginConfig: {OriginAccessIdentity: ""}`` in CloudFormation; the
    provider stores no ``s3_origin_config`` for it, so the empty block would
    read as a change on every plan."""

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        cfg = plan.props.get("DistributionConfig", {})
        cert = cfg.get("ViewerCertificate") if isinstance(cfg, dict) else None
        if isinstance(cert, dict) and cert.get("CloudFrontDefaultCertificate") in (
            True,
            "true",
        ):
            # With the default certificate CloudFront applies TLSv1 whatever is
            # asked for, so the setting reads as a change on every plan.
            cfg = dict(
                cfg,
                ViewerCertificate={
                    k: v for k, v in cert.items() if k != "MinimumProtocolVersion"
                },
            )
            plan.props = dict(plan.props, DistributionConfig=cfg)
        origins = cfg.get("Origins") if isinstance(cfg, dict) else None
        if isinstance(origins, list):
            fixed = []
            for o in origins:
                if isinstance(o, dict) and o.get("S3OriginConfig") == {
                    "OriginAccessIdentity": ""
                }:
                    o = {k: v for k, v in o.items() if k != "S3OriginConfig"}
                fixed.append(o)
            original = plan.props
            plan.props = dict(original, DistributionConfig=dict(cfg, Origins=fixed))
            try:
                return super().emit(tr, plan, res)
            finally:
                plan.props = original
        return super().emit(tr, plan, res)


CLOUDFRONT_DISTRIBUTION = CloudFrontMapper(
    tf_type="aws_cloudfront_distribution",
    ref_attr="id",
    attrs={"DomainName": "domain_name", "Id": "id"},
    spec={
        "Tags": ("tags", "tags"),
        "DistributionConfig": (
            "flatten",
            {
                "Comment": ("attr", "comment"),
                "DefaultRootObject": ("attr", "default_root_object"),
                "Enabled": ("attr", "enabled"),
                "HttpVersion": ("attr", "http_version"),
                "IPV6Enabled": ("attr", "is_ipv6_enabled"),
                "PriceClass": ("attr", "price_class"),
                "Aliases": ("attr", "aliases"),
                "WebACLId": ("attr", "web_acl_id"),
                "CustomErrorResponses": (
                    "blocks",
                    "custom_error_response",
                    {
                        "ErrorCachingMinTTL": ("attr", "error_caching_min_ttl"),
                        "ErrorCode": ("attr", "error_code"),
                        "ResponseCode": ("attr", "response_code"),
                        "ResponsePagePath": ("attr", "response_page_path"),
                    },
                ),
                "DefaultCacheBehavior": (
                    "block",
                    "default_cache_behavior",
                    CACHE_BEHAVIOR,
                ),
                "CacheBehaviors": ("blocks", "ordered_cache_behavior", CACHE_BEHAVIOR),
                "Logging": (
                    "block",
                    "logging_config",
                    {
                        "Bucket": ("attr", "bucket"),
                        "IncludeCookies": ("attr", "include_cookies"),
                        "Prefix": ("attr", "prefix"),
                    },
                ),
                "Origins": (
                    "blocks",
                    "origin",
                    {
                        "DomainName": ("attr", "domain_name"),
                        "Id": ("attr", "origin_id"),
                        "OriginPath": ("attr", "origin_path"),
                        "OriginAccessControlId": ("attr", "origin_access_control_id"),
                        "S3OriginConfig": (
                            "block",
                            "s3_origin_config",
                            {
                                "OriginAccessIdentity": (
                                    "attr",
                                    "origin_access_identity",
                                )
                            },
                        ),
                    },
                ),
                "Restrictions": (
                    "block",
                    "restrictions",
                    {
                        "GeoRestriction": (
                            "block",
                            "geo_restriction",
                            {
                                "RestrictionType": ("attr", "restriction_type"),
                                "Locations": ("attr", "locations"),
                            },
                        ),
                    },
                ),
                "ViewerCertificate": (
                    "block",
                    "viewer_certificate",
                    {
                        "CloudFrontDefaultCertificate": (
                            "attr",
                            "cloudfront_default_certificate",
                        ),
                        "MinimumProtocolVersion": ("attr", "minimum_protocol_version"),
                        "AcmCertificateArn": ("attr", "acm_certificate_arn"),
                        "SslSupportMethod": ("attr", "ssl_support_method"),
                    },
                ),
            },
        ),
    },
)


MAPPERS: dict[str, Any] = {
    "AWS::Events::Rule": EventsRuleMapper(),
    "AWS::WAFv2::WebACL": WAF_WEB_ACL,
    "AWS::Cognito::UserPool": COGNITO_USER_POOL,
    "AWS::CloudFront::Distribution": CLOUDFRONT_DISTRIBUTION,
    "AWS::CloudFormation::WaitConditionHandle": NoOpMapper(),
    "AWS::ECR::Repository": EcrRepositoryMapper(),
    "AWS::SQS::QueuePolicy": PerTargetPolicyMapper(
        "aws_sqs_queue_policy",
        {"Queues": ("aws_sqs_queue_policy", "queue_url")},
        None,
        default_version="2008-10-17",
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
    {
        "AWS::IAM::Role",
        "AWS::Lambda::Function",
        "AWS::Logs::LogGroup",
        "AWS::ECR::Repository",
        "AWS::Events::Rule",
        "AWS::S3::Bucket",
        "AWS::S3::BucketPolicy",
        "AWS::DynamoDB::Table",
        "AWS::Lambda::Permission",
        "AWS::Lambda::EventSourceMapping",
        "AWS::CloudWatch::Dashboard",
        "AWS::Cognito::UserPoolUser",
        "AWS::Cognito::UserPoolUserToGroupAttachment",
    }
)
