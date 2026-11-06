"""Translate an expanded CloudFormation template into a Terraform module.

Every construct the translator does not understand raises
:class:`TranslationError` naming the resource and property. There is no
best-effort path: an unmapped type, property or intrinsic fails the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import hcl
from .awscc_schema import ANY, AwsccSchema, Hint, awscc_type_name

NO_VALUE = {"Ref": "AWS::NoValue"}
INTRINSICS = {
    "Ref",
    "Condition",
    "Fn::If",
    "Fn::Sub",
    "Fn::Join",
    "Fn::Select",
    "Fn::Split",
    "Fn::GetAtt",
    "Fn::Equals",
    "Fn::Not",
    "Fn::And",
    "Fn::Or",
    "Fn::FindInMap",
    "Fn::Base64",
    "Fn::GetAZs",
}
RESOURCE_KEYS = {
    "Type",
    "Properties",
    "DependsOn",
    "Condition",
    "Metadata",
    "DeletionPolicy",
    "UpdateReplacePolicy",
    # Legacy custom-resource attribute with no effect on deployment.
    "Version",
}
STACK_NAME_VAR = "stack_name"
# A module variable carrying a plan-time-known stand-in for a parameter that a
# condition reads. Resource attributes are replaced by a non-empty placeholder,
# which is exactly what CloudFormation's condition sees: a resource id is never
# empty and never equals a literal the template compares it with.
COND_SUFFIX = "__cond"
CUSTOM_TYPES = ("Custom::", "AWS::CloudFormation::CustomResource")

# CloudFormation `Ref` returns something other than the Cloud Control
# identifier (awscc `id`) for these types. Each has a composite
# primaryIdentifier in the CloudFormation registry, which awscc joins with "|",
# while Ref returns one member of it. Read from `describe-type` per type; a type
# with a composite identifier that is not listed here keeps `id`, which is
# right only where Ref also returns the joined value (WAFv2::IPSet,
# Bedrock::DataSource).
# Properties where awscc stores what the service returns, and the service
# returns a different spelling of the same value than templates write.
# (type, property) -> rewrite applied to the value before rendering.
AWSCC_PROPERTY_FIXUPS: dict[tuple[str, str], str] = {
    # The template gives the role ARN; Glue returns the role name.
    ("AWS::Glue::Crawler", "Role"): "role_name",
    # The same URL can appear twice (once through ${AWS::Region}, once
    # literally); Cognito keeps one.
    ("AWS::Cognito::UserPoolClient", "CallbackURLs"): "distinct",
    ("AWS::Cognito::UserPoolClient", "LogoutURLs"): "distinct",
    # API Gateway drops an empty response template.
    ("AWS::ApiGateway::Method", "Integration"): "drop_empty_templates",
}

# Resources a second writer owns at deploy time, so Terraform must not treat the
# difference as drift. Keyed by (type, logical id); each needs its reason, and a
# key that no longer names a resource in the template fails the translation.
LIFECYCLE_OVERRIDES: dict[tuple[str, str], tuple[list[str], str]] = {
    ("AWS::SSM::Parameter", "SettingsParameter"): (
        ["value"],
        "UpdateSettingsFunction (custom resource UpdateSettings) merges the "
        "deployment's settings into this parameter at deploy time",
    ),
}

AWSCC_REF_ATTR: dict[str, str] = {
    "AWS::ApiGateway::Authorizer": "authorizer_id",
    "AWS::ApiGateway::Deployment": "deployment_id",
    "AWS::ApiGateway::Resource": "resource_id",
    "AWS::ApiGateway::Stage": "stage_name",
    "AWS::Cognito::UserPoolClient": "client_id",
    "AWS::Cognito::UserPoolDomain": "domain",
    "AWS::Cognito::UserPoolGroup": "group_name",
    "AWS::Cognito::UserPoolIdentityProvider": "provider_name",
    "AWS::Cognito::UserPoolResourceServer": "identifier",
    "AWS::Cognito::UserPoolUser": "username",
    "AWS::Glue::Table": "name",
    "AWS::OpenSearchServerless::AccessPolicy": "name",
    "AWS::OpenSearchServerless::SecurityPolicy": "name",
}


class TranslationError(Exception):
    pass


def is_intrinsic(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) == 1
        and next(iter(value)) in (INTRINSICS)
    )


def may_be_novalue(value: Any) -> bool:
    if value == NO_VALUE:
        return True
    if isinstance(value, dict) and set(value) == {"Fn::If"}:
        _, a, b = value["Fn::If"]
        return may_be_novalue(a) or may_be_novalue(b)
    return False


@dataclass
class Plan:
    logical_id: str
    cfn_type: str
    kind: str  # awscc | aws | custom
    tf_type: str
    name: str
    condition: str | None
    props: dict[str, Any]
    ref_attr: str = "id"
    getatt: Callable[[str], Any] | None = None
    extra_deps: list[str] = field(default_factory=list)
    # True when no Terraform resource has this plan's address (the CloudFormation
    # resource became several); dependants then use ``extra_deps`` only.
    virtual: bool = False

    @property
    def address(self) -> str:
        return f"{self.tf_type}.{self.name}"


@dataclass
class Report:
    warnings: list[str] = field(default_factory=list)
    resource_kinds: dict[str, int] = field(default_factory=dict)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


class Translator:
    def __init__(
        self,
        template: dict[str, Any],
        schema: AwsccSchema,
        mode: str = "hybrid",
        aws_mapped: frozenset[str] | None = None,
        survey: bool = False,
        nested_loader: Callable[[str], dict[str, Any]] | None = None,
    ):
        from . import aws_mappers, aws_mappers_storage

        self.t = template
        self.schema = schema
        self.mode = mode
        self.mappers = {**aws_mappers.MAPPERS, **aws_mappers_storage.STORAGE_MAPPERS}
        self.aws_mapped = (
            aws_mapped if aws_mapped is not None else aws_mappers.HYBRID_TYPES
        )
        self.params: dict[str, dict[str, Any]] = template.get("Parameters", {})
        self.param_vars: dict[str, str] = {}
        for p in self.params:
            v = hcl.snake(p)
            if v == STACK_NAME_VAR:
                v = "param_" + v
            self.param_vars[p] = v
        self.conditions: dict[str, Any] = template.get("Conditions", {})
        self.mappings: dict[str, Any] = template.get("Mappings", {})
        self.report = Report()
        self.retained: list[str] = []
        # In survey mode every failure is collected instead of raised, so one
        # run lists everything a template needs.
        self.survey = survey
        self.errors: list[str] = []
        self.nested_loader = nested_loader
        self.children: dict[str, Translator] = {}
        # Parameters a condition reads. Across a module boundary their values
        # can be resource attributes, unknown until apply, while count must be
        # known at plan; see emit_module and COND_SUFFIX.
        self.cond_params: set[str] = set()
        self._collect_cond_params(self.conditions)
        self._in_conditions = False
        self._opaque = False
        # Only the root reads the pseudo-parameters from data sources. A data
        # source inside a module that has depends_on is deferred to apply, which
        # would make every condition reading it unknown at plan time.
        self.is_root = True
        # True while writing the resource directory, which keeps CloudFormation's
        # Ref semantics exactly.
        self._directory_mode = False
        # Set when an inline function imports CloudFormation's cfnresponse.
        self.needs_cfnresponse = False
        self.plans: dict[str, Plan] = {}
        self._refs: set[str] = set()
        self._current: str = ""
        self._plan_all()

    # ------------------------------------------------------------------ plan
    def _plan_all(self) -> None:
        for lid, res in self.t.get("Resources", {}).items():
            unknown = set(res) - RESOURCE_KEYS
            if unknown:
                raise TranslationError(f"{lid}: unsupported resource keys {unknown}")
            rtype = res["Type"]
            cond = res.get("Condition")
            props = res.get("Properties", {}) or {}
            name = hcl.snake(lid)
            if rtype.startswith(CUSTOM_TYPES):
                plan = Plan(
                    lid, rtype, "custom", "aws_lambda_invocation", name, cond, props
                )
            elif rtype == "AWS::CloudFormation::Stack":
                plan = Plan(lid, rtype, "module", "module", name, cond, props)
                self._plan_module(plan)
            elif (
                self.mode == "hybrid" and rtype in self.aws_mapped
            ) or not self.schema.has(rtype):
                mapper = self.mappers.get(rtype)
                if mapper is None:
                    msg = (
                        f"{lid}: {rtype} has no hashicorp/aws mapping and is not in "
                        "the vendored awscc schema. Add a mapping in aws_mappers, or "
                        "run terraform/translator/scripts/refresh_awscc_schema.py "
                        "if awscc supports it"
                    )
                    if not self.survey:
                        raise TranslationError(msg)
                    self.errors.append(msg)
                    continue
                plan = Plan(lid, rtype, "aws", mapper.tf_type, name, cond, props)
                plan.ref_attr = mapper.ref_attr
                plan.getatt = mapper.getatt
            else:
                plan = Plan(
                    lid, rtype, "awscc", awscc_type_name(rtype), name, cond, props
                )
                plan.ref_attr = AWSCC_REF_ATTR.get(rtype, "id")
            self.plans[lid] = plan
            self.report.resource_kinds[plan.kind] = (
                self.report.resource_kinds.get(plan.kind, 0) + 1
            )
            policy = res.get("DeletionPolicy")
            if policy and policy.lower() != "delete":
                # Terraform has no DeletionPolicy. `idp-cli delete --terraform`
                # removes these from state before destroying, so they are left
                # in place as CloudFormation leaves them.
                self.retained.append(lid)
        for plan in self.plans.values():
            if plan.kind == "aws":
                self.mappers[plan.cfn_type].plan(self, plan)
        # Assets (zips) the module ships, written by the CLI: relative path -> bytes.
        self.assets: dict[str, bytes] = {}

    def _collect_cond_params(self, node: Any) -> None:
        if isinstance(node, dict):
            if set(node) == {"Ref"} and node["Ref"] in self.params:
                self.cond_params.add(node["Ref"])
            for v in node.values():
                self._collect_cond_params(v)
        elif isinstance(node, list):
            for v in node:
                self._collect_cond_params(v)

    def _plan_module(self, plan: Plan) -> None:
        if self.nested_loader is None:
            raise TranslationError(
                f"{plan.logical_id}: nested stack needs a template loader"
            )
        url = plan.props["TemplateURL"]
        if not isinstance(url, str):
            raise TranslationError(f"{plan.logical_id}: TemplateURL must be literal")
        child = Translator(
            self.nested_loader(url),
            self.schema,
            mode=self.mode,
            aws_mapped=self.aws_mapped,
            survey=self.survey,
            nested_loader=self.nested_loader,
        )
        child.is_root = False
        self.children[plan.name] = child
        # CloudFormation makes anything that references a nested stack wait for
        # the whole stack. A reference to one module output waits only for what
        # feeds that output, and for nothing when the output is computed from
        # the stack name, so depend on the whole module instead.
        plan.extra_deps.append(plan.address)
        if self.survey:
            self.errors.extend(f"{plan.logical_id}/{e}" for e in child.errors)

    # ----------------------------------------------------------- references
    def cond_ref(self, name: str) -> str:
        if name not in self.conditions:
            raise TranslationError(f"unknown condition {name}")
        return "local.c_" + hcl.snake(name)

    def _wrap(self, plan: Plan, attr_expr: str) -> str:
        self._refs.add(plan.logical_id)
        if plan.condition:
            return f"one({plan.address}[*].{attr_expr})"
        return f"{plan.address}.{attr_expr}"

    def ref(self, name: str) -> str:
        pseudo = {
            "AWS::Region": "local.pseudo.region",
            "AWS::AccountId": "local.pseudo.account_id",
            "AWS::Partition": "local.pseudo.partition",
            "AWS::URLSuffix": "local.pseudo.dns_suffix",
            "AWS::StackName": f"var.{STACK_NAME_VAR}",
            "AWS::NoValue": "null",
        }
        if name in pseudo:
            return pseudo[name]
        if name == "AWS::StackId":
            return (
                '"arn:${local.pseudo.partition}:cloudformation:'
                "${local.pseudo.region}:"
                "${local.pseudo.account_id}:stack/"
                f'${{var.{STACK_NAME_VAR}}}/terraform"'
            )
        if name in self.param_vars:
            var = "var." + self.param_vars[name]
            if self._in_conditions and name in self.cond_params:
                cvar = var + COND_SUFFIX
                return f"({cvar} != null ? {cvar} : {var})"
            return var
        plan = self.plans.get(name)
        if self._opaque and plan is not None:
            return hcl.string(f"opaque-{name}")
        if plan is None:
            raise TranslationError(f"{self._current}: Ref to unknown name {name}")
        if plan.kind == "custom":
            self._refs.add(name)
            inner = f"jsondecode({plan.address}{'[0]' if plan.condition else ''}"
            return inner + ".result).PhysicalResourceId"
        return self._wrap(plan, plan.ref_attr)

    def getatt(self, name: str, attr: str) -> str:
        plan = self.plans.get(name)
        if self._opaque and plan is not None:
            return hcl.string(f"opaque-{name}-{attr}")
        if plan is None:
            raise TranslationError(f"{self._current}: GetAtt on unknown {name}")
        if plan.kind == "custom":
            self._refs.add(name)
            idx = "[0]" if plan.condition else ""
            return f"jsondecode({plan.address}{idx}.result).Data.{attr}"
        if plan.kind == "module":
            if not attr.startswith("Outputs."):
                raise TranslationError(f"{self._current}: GetAtt {name}.{attr}")
            out = attr[len("Outputs.") :]
            if out not in self.children[plan.name].t.get("Outputs", {}):
                raise TranslationError(
                    f"{self._current}: nested stack {name} has no output {out}"
                )
            return self._wrap(plan, out)
        if plan.kind == "aws":
            assert plan.getatt is not None
            got = plan.getatt(attr)
            if isinstance(got, tuple):
                tf_attr, fmt = got
                return fmt % self._wrap(plan, tf_attr)
            return self._wrap(plan, got)
        hint = self.schema.hint(plan.cfn_type)
        parts = []
        for piece in attr.split("."):
            found = hint.attr(piece)
            if found is None:
                raise TranslationError(
                    f"{self._current}: {plan.cfn_type} has no awscc attribute "
                    f"for GetAtt {attr}"
                )
            parts.append(found[0])
            hint = found[1]
        return self._wrap(plan, ".".join(parts))

    # ---------------------------------------------------------- expressions
    def expr(self, v: Any, hint: Hint = ANY, json_ctx: bool = False) -> str:
        if is_intrinsic(v):
            return self._intrinsic(v, hint, json_ctx)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if v is None:
            return "null"
        if isinstance(v, str):
            if hint.kind == "str" and not json_ctx and v.lstrip()[:1] in ("{", "["):
                # A JSON document written as text. awscc compares the stored
                # string byte for byte, and services return it minified in the
                # original key order (ECR does), so send exactly that. Not
                # jsonencode(): it sorts keys, which never matches.
                try:
                    parsed = json.loads(v)
                except ValueError:
                    return hcl.string(v)
                return hcl.string(json.dumps(parsed, separators=(",", ":")))
            return hcl.string(v)
        if isinstance(v, list):
            if hint.kind == "str" and not json_ctx:
                # awscc types some polymorphic properties (DynamoDB KeySchema)
                # as a JSON string.
                return "jsonencode(" + self.expr(v, ANY, json_ctx=True) + ")"
            el = hint.el if hint.kind in ("list", "set") and hint.el else ANY
            return self._list(v, el, json_ctx)
        if isinstance(v, dict):
            if hint.kind == "str" and not json_ctx:
                return "jsonencode(" + self.expr(v, ANY, json_ctx=True) + ")"
            if hint.kind == "obj" and not json_ctx:
                return self._schema_obj(v, hint)
            el = hint.el if hint.kind == "map" and hint.el else ANY
            return self._map(v, el, json_ctx)
        raise TranslationError(f"{self._current}: cannot render {v!r}")

    def _list(self, items: list[Any], el: Hint, json_ctx: bool) -> str:
        if (
            el.kind == "str"
            and not json_ctx
            and any(isinstance(i, (dict, list)) and not is_intrinsic(i) for i in items)
        ):
            return "jsonencode(" + self.expr(items, ANY, json_ctx=True) + ")"
        if not any(may_be_novalue(i) for i in items):
            return hcl.lst([self.expr(i, el, json_ctx) for i in items])
        parts = []
        for i in items:
            if may_be_novalue(i):
                parts.append(self._optional_list_item(i, el, json_ctx))
            else:
                parts.append(hcl.lst([self.expr(i, el, json_ctx)]))
        return "concat(" + ", ".join(parts) + ")"

    def _optional_list_item(self, v: Any, el: Hint, json_ctx: bool) -> str:
        if v == NO_VALUE:
            return "[]"
        cond, a, b = v["Fn::If"]
        ta = (
            self._optional_list_item(a, el, json_ctx)
            if may_be_novalue(a)
            else (hcl.lst([self.expr(a, el, json_ctx)]))
        )
        tb = (
            self._optional_list_item(b, el, json_ctx)
            if may_be_novalue(b)
            else (hcl.lst([self.expr(b, el, json_ctx)]))
        )
        return f"({self.cond_ref(cond)} ? {ta} : {tb})"

    def _map(self, d: dict[str, Any], el: Hint, json_ctx: bool) -> str:
        fixed = [
            (k, self.expr(x, el, json_ctx))
            for k, x in d.items()
            if not (may_be_novalue(x))
        ]
        optional = [(k, x) for k, x in d.items() if may_be_novalue(x)]
        base = hcl.obj(fixed, quote_keys=True)
        if not optional:
            return base
        parts = [base]
        for k, x in optional:
            parts.append(self._optional_entry(k, x, el, json_ctx))
        return "merge(" + ", ".join(parts) + ")"

    def _optional_entry(self, k: str, v: Any, el: Hint, json_ctx: bool) -> str:
        if v == NO_VALUE:
            return "{}"
        cond, a, b = v["Fn::If"]

        def side(x: Any) -> str:
            if may_be_novalue(x):
                return self._optional_entry(k, x, el, json_ctx)
            return hcl.obj([(k, self.expr(x, el, json_ctx))], quote_keys=True)

        return f"({self.cond_ref(cond)} ? {side(a)} : {side(b)})"

    def _schema_obj(self, d: dict[str, Any], hint: Hint) -> str:
        items = []
        for k, x in d.items():
            found = hint.attr(k)
            if found is None:
                raise TranslationError(
                    f"{self._current}: no awscc attribute for property {k!r}"
                )
            tf_name, child = found
            items.append((tf_name, self.expr(x, child)))
        return hcl.obj(items)

    def _intrinsic(self, v: dict[str, Any], hint: Hint, json_ctx: bool) -> str:
        fn, arg = next(iter(v.items()))
        if fn == "Ref":
            return self.ref(arg)
        if fn == "Condition":
            return self.cond_ref(arg)
        if fn == "Fn::GetAtt":
            if isinstance(arg, str):
                name, attr = arg.split(".", 1)
            else:
                name, attr = arg
            return self.getatt(name, attr)
        if fn == "Fn::If":
            cond, a, b = arg
            return (
                f"({self.cond_ref(cond)} ? {self.expr(a, hint, json_ctx)} : "
                f"{self.expr(b, hint, json_ctx)})"
            )
        if fn == "Fn::Sub":
            return self._sub(arg)
        if fn == "Fn::Join":
            delim, items = arg
            return f"join({hcl.string(delim)}, {self.expr(items, ANY, json_ctx)})"
        if fn == "Fn::Select":
            idx, items = arg
            return f"element({self.expr(items, ANY, json_ctx)}, {self.expr(idx)})"
        if fn == "Fn::Split":
            delim, src = arg
            return f"split({hcl.string(delim)}, {self.expr(src)})"
        if fn == "Fn::Equals":
            a, b = arg
            return f"(tostring({self.expr(a)}) == tostring({self.expr(b)}))"
        if fn == "Fn::Not":
            return f"!({self.expr(arg[0])})"
        if fn in ("Fn::And", "Fn::Or"):
            op = " && " if fn == "Fn::And" else " || "
            return "(" + op.join(self.expr(x) for x in arg) + ")"
        if fn == "Fn::FindInMap":
            m, k1, k2 = arg
            return f"local.mappings[{hcl.string(m)}][{self.expr(k1)}][{self.expr(k2)}]"
        if fn == "Fn::Base64":
            return f"base64encode({self.expr(arg)})"
        raise TranslationError(f"{self._current}: unsupported intrinsic {fn}")

    def _sub(self, arg: Any) -> str:
        if isinstance(arg, list):
            text, variables = arg
        else:
            text, variables = arg, {}
        out = []
        pos = 0
        for m in re.finditer(r"\$\{([^}]*)\}", text):
            out.append(hcl.escape_template(text[pos : m.start()]))
            name = m.group(1).strip()
            if name.startswith("!"):
                out.append(hcl.escape_template("${" + name[1:] + "}"))
            elif name in variables:
                out.append("${" + self.expr(variables[name]) + "}")
            elif "." in name and not name.startswith("AWS::"):
                res, attr = name.split(".", 1)
                out.append("${" + self.getatt(res, attr) + "}")
            else:
                out.append("${" + self.ref(name) + "}")
            pos = m.end()
        out.append(hcl.escape_template(text[pos:]))
        return '"' + "".join(out) + '"'

    # -------------------------------------------------------------- emitting
    def begin(self, lid: str) -> None:
        self._current = lid
        self._refs = set()

    def depends_on(self, plan: Plan, res: dict[str, Any]) -> list[str]:
        deps = res.get("DependsOn", [])
        if isinstance(deps, str):
            deps = [deps]
        addrs: list[str] = []
        for d in deps:
            target = self.plans[d]
            if not target.virtual:
                addrs.append(target.address)
            addrs.extend(target.extra_deps)
        for r in sorted(self._refs):
            if r != plan.logical_id:
                addrs.extend(self.plans[r].extra_deps)
        seen: list[str] = []
        for a in addrs:
            if a not in seen:
                seen.append(a)
        return seen

    def meta_lines(
        self, plan: Plan, res: dict[str, Any], extra_deps: list[str] | None = None
    ) -> tuple[list, list]:
        head = []
        if plan.condition:
            head.append(f"count = {self.cond_ref(plan.condition)} ? 1 : 0")
        tail = []
        deps = list(extra_deps or []) + self.depends_on(plan, res)
        if deps:
            tail.append("depends_on = " + hcl.lst(deps))
        return head, tail

    def emit_awscc(self, plan: Plan, res: dict[str, Any]) -> str:
        hint = self.schema.hint(plan.cfn_type)
        lines = []
        for k, x in plan.props.items():
            found = hint.attr(k)
            if found is None:
                raise TranslationError(
                    f"{plan.logical_id}: {plan.cfn_type} property {k!r} has no "
                    f"awscc attribute in {plan.tf_type}"
                )
            tf_name, child = found
            fixup = AWSCC_PROPERTY_FIXUPS.get((plan.cfn_type, k))
            if fixup == "role_name":
                x = self._role_name(x)
            if fixup == "drop_empty_templates":
                x = _drop_empty_templates(x)
            rendered = self.expr(x, child)
            if fixup == "distinct":
                rendered = f"try(distinct({rendered}), {rendered})"
            lines.append(f"{tf_name} = {rendered}")
        head, tail = self.meta_lines(plan, res)
        override = LIFECYCLE_OVERRIDES.get((plan.cfn_type, plan.logical_id))
        if override:
            attrs, reason = override
            tail = tail + [
                f"# {reason}",
                hcl.block(
                    "lifecycle", [f"ignore_changes = [{', '.join(attrs)}]"]
                ).rstrip(),
            ]
        return hcl.block(
            f'resource "{plan.tf_type}" "{plan.name}"', head + lines + tail
        )

    def _role_name(self, v: Any) -> Any:
        if isinstance(v, dict) and set(v) == {"Fn::GetAtt"}:
            arg = v["Fn::GetAtt"]
            name, attr = arg.split(".", 1) if isinstance(arg, str) else arg
            target = self.plans.get(name)
            if attr == "Arn" and target and target.cfn_type == "AWS::IAM::Role":
                return {"Ref": name}
        return v

    def emit_custom(self, plan: Plan, res: dict[str, Any]) -> str:
        props = dict(plan.props)
        token = props.pop("ServiceToken", None)
        if token is None:
            raise TranslationError(f"{plan.logical_id}: custom resource without token")
        payload = {
            "ServiceToken": token,
            "ResourceType": plan.cfn_type,
            "LogicalResourceId": plan.logical_id,
            "ResourceProperties": dict(props, ServiceToken=token),
        }
        body = self.expr(payload, ANY, json_ctx=True)
        token_expr = self.expr(token)
        deps_line = f"aws_lambda_permission.{plan.name}_bridge"
        head, tail = self.meta_lines(plan, res, [deps_line])
        _, perm_tail = self.meta_lines(plan, res, ["time_sleep.cfn_bridge_iam"])
        # The handler grants the bridge permission to invoke it, rather than the
        # bridge's own policy listing every handler: that would make the bridge
        # depend on every handler, and some handlers depend on custom resources.
        permission = hcl.block(
            f'resource "aws_lambda_permission" "{plan.name}_bridge"',
            head
            + [
                f'statement_id = "cfnbridge-{plan.logical_id}"',
                'action = "lambda:InvokeFunction"',
                f"function_name = {token_expr}",
                "principal = aws_iam_role.cfn_bridge.arn",
            ]
            + perm_tail,
        )
        lines = [
            "function_name = aws_lambda_function.cfn_bridge.function_name",
            'lifecycle_scope = "CRUD"',
            "input = jsonencode(merge("
            + body
            + f", {{ StackName = var.{STACK_NAME_VAR} }}))",
        ]
        self.custom_tokens.append(token_expr)
        invocation = hcl.block(
            f'resource "aws_lambda_invocation" "{plan.name}"', head + lines + tail
        )
        return permission + "\n" + invocation

    def emit_module(self, plan: Plan, res: dict[str, Any]) -> str:
        child = self.children[plan.name]
        props = dict(plan.props)
        props.pop("TemplateURL")
        props.pop("TimeoutInMinutes", None)
        params = props.pop("Parameters", {}) or {}
        if props:
            raise TranslationError(
                f"{plan.logical_id}: unsupported nested stack properties {set(props)}"
            )
        lines = [
            f'source = "./modules/{plan.name}"',
            "pseudo = local.pseudo",
            # CloudFormation names a nested stack <parent>-<LogicalId>-<random>.
            f'{STACK_NAME_VAR} = "${{var.{STACK_NAME_VAR}}}-{plan.logical_id}"',
        ]
        for p, value in params.items():
            if p not in child.param_vars:
                raise TranslationError(
                    f"{plan.logical_id}: parameter {p} is not declared by the "
                    "nested template"
                )
            expr = self.expr(value)
            ptype = child.params[p].get("Type", "String")
            already_list = isinstance(value, list) or (
                isinstance(value, dict)
                and set(value) == {"Ref"}
                and str(
                    self.params.get(value["Ref"], {}).get("Type", "String")
                ).startswith(("List<", "CommaDelimitedList"))
            )
            if (
                ptype.startswith("List<") or ptype == "CommaDelimitedList"
            ) and not already_list:
                # CloudFormation passes every parameter as text.
                expr = f'compact(split(",", {expr}))'
            lines.append(f"{child.param_vars[p]} = {expr}")
            if p in child.cond_params:
                self._opaque = True
                try:
                    known = self.expr(value)
                finally:
                    self._opaque = False
                if (
                    ptype.startswith("List<") or ptype == "CommaDelimitedList"
                ) and not already_list:
                    known = f'compact(split(",", {known}))'
                lines.append(f"{child.param_vars[p]}{COND_SUFFIX} = {known}")
        head, tail = self.meta_lines(plan, res)
        return hcl.block(f'module "{plan.name}"', head + lines + tail)

    def render(self) -> dict[str, str]:
        self.custom_tokens: list[str] = []
        resources: list[str] = []
        for lid, res in self.t.get("Resources", {}).items():
            plan = self.plans.get(lid)
            if plan is None:
                continue
            self.begin(lid)
            try:
                if plan.kind == "awscc":
                    resources.append(self.emit_awscc(plan, res))
                elif plan.kind == "custom":
                    resources.append(self.emit_custom(plan, res))
                elif plan.kind == "module":
                    resources.append(self.emit_module(plan, res))
                else:
                    resources.append(self.mappers[plan.cfn_type].emit(self, plan, res))
            except (TranslationError, KeyError) as exc:
                if not self.survey:
                    raise
                self.errors.append(f"{lid}: {exc}")
        files = {
            "main.tf": "\n".join(resources),
            "variables.tf": self._variables(),
            "locals.tf": self._locals(),
            "outputs.tf": self._outputs(),
        }
        return files

    def _variables(self) -> str:
        out = [
            hcl.block(
                f'variable "{STACK_NAME_VAR}"',
                [
                    "type = string",
                    'description = "Name of this deployment. It is the name of the '
                    "CloudFormation label stack, the value of every AWS::StackName "
                    "in the templates, and the prefix of generated resource names; "
                    'nested modules use <stack_name>-<LogicalId>."',
                ]
                + (
                    [
                        hcl.block(
                            "validation",
                            [
                                # The main template's IsStacknameLengthOK refuses
                                # more than 25; caught here at plan instead of
                                # part-way through an apply.
                                f"condition = length(var.{STACK_NAME_VAR}) <= 25",
                                'error_message = "stack_name must be at most 25 '
                                'characters."',
                            ],
                        ).rstrip(),
                        hcl.block(
                            "validation",
                            [
                                "condition = can(regex("
                                '"^[A-Za-z][A-Za-z0-9-]*$", '
                                f"var.{STACK_NAME_VAR}))",
                                'error_message = "stack_name must start with a '
                                "letter and contain only letters, digits and "
                                'hyphens (a CloudFormation stack name)."',
                            ],
                        ).rstrip(),
                    ]
                    if self.is_root
                    else []
                ),
            )
        ]
        for p, spec in self.params.items():
            ptype = spec.get("Type", "String")
            lines = []
            if ptype == "Number":
                lines.append("type = number")
            elif ptype.startswith("List<") or ptype == "CommaDelimitedList":
                lines.append("type = list(string)")
            else:
                lines.append("type = string")
            if "Description" in spec:
                lines.append(f"description = {hcl.string(str(spec['Description']))}")
            if "Default" in spec:
                d = spec["Default"]
                if ptype == "Number":
                    lines.append(f"default = {d}")
                elif lines[0] == "type = list(string)":
                    items = [s for s in str(d).split(",") if s] if d != "" else []
                    lines.append(f"default = {hcl.lst([hcl.string(i) for i in items])}")
                else:
                    lines.append(f"default = {hcl.string(str(d))}")
            if spec.get("NoEcho"):
                lines.append("sensitive = true")
            lines.extend(self._validations(p, spec, ptype))
            out.append(hcl.block(f'variable "{self.param_vars[p]}"', lines))
            if p in self.cond_params:
                vtype = lines[0]
                out.append(
                    hcl.block(
                        f'variable "{self.param_vars[p]}{COND_SUFFIX}"',
                        [
                            vtype,
                            "default = null",
                            'description = "Plan-time stand-in for the parameter '
                            "of the same name, read only by conditions. Set by the "
                            'parent module."',
                        ],
                    )
                )
        return "\n".join(out)

    def _validations(self, p: str, spec: dict[str, Any], ptype: str) -> list[str]:
        """CloudFormation's parameter constraints as Terraform validations, so a
        value CloudFormation would refuse at submit time is refused at plan."""
        name = self.param_vars[p]
        ref = f"var.{name}"
        hint = spec.get("ConstraintDescription")
        out: list[tuple[str, str]] = []
        allowed = spec.get("AllowedValues")
        if allowed:
            vals = hcl.lst([hcl.string(str(a)) for a in allowed])
            out.append(
                (
                    f"contains({vals}, tostring({ref}))",
                    f"{name} must be one of {allowed}.",
                )
            )
        pattern = spec.get("AllowedPattern")
        if pattern and not ptype.startswith("List<") and ptype != "CommaDelimitedList":
            if re.search(r"\(\?[=!<]", pattern):
                # Lookaround is Java regex that Terraform's RE2 cannot express.
                self.report.warn(
                    f"parameter {p}: AllowedPattern uses lookaround; not enforced"
                )
            else:
                anchored = hcl.string(f"^(?:{pattern})$")
                out.append(
                    (
                        f"can(regex({anchored}, tostring({ref})))",
                        f"{name} must match {pattern}.",
                    )
                )
        if ptype == "Number":
            if "MinValue" in spec:
                out.append(
                    (
                        f"{ref} >= {spec['MinValue']}",
                        f"{name} must be at least {spec['MinValue']}.",
                    )
                )
            if "MaxValue" in spec:
                out.append(
                    (
                        f"{ref} <= {spec['MaxValue']}",
                        f"{name} must be at most {spec['MaxValue']}.",
                    )
                )
        lines = []
        for condition, message in out:
            text = f"{message} {hint}" if hint else message
            lines.append(
                hcl.block(
                    "validation",
                    [f"condition = {condition}", f"error_message = {hcl.string(text)}"],
                ).rstrip()
            )
        return lines

    def retained_addresses(self) -> list[str]:
        """Terraform addresses (without instance keys) of every resource whose
        CloudFormation DeletionPolicy keeps it, with the resources it became."""
        out = []
        for lid in self.retained:
            plan = self.plans.get(lid)
            if plan is None:
                continue
            if not plan.virtual:
                out.append(plan.address)
            out.extend(a for a in plan.extra_deps if not a.startswith("time_sleep."))
        for name, child in self.children.items():
            out.extend(f"module.{name}.{a}" for a in child.retained_addresses())
        return sorted(set(out))

    def parameter_map(self) -> dict[str, dict[str, Any]]:
        """CloudFormation parameter name -> Terraform variable and type, for
        tools that take CloudFormation-style parameters (idp-cli deploy)."""
        return {
            p: {
                "variable": self.param_vars[p],
                "type": str(spec.get("Type", "String")),
                "required": "Default" not in spec,
                "sensitive": bool(spec.get("NoEcho")),
            }
            for p, spec in self.params.items()
        }

    def label_parameters(self) -> list[tuple[str, str, str]]:
        """(CloudFormation name, HCL value as text, description) per parameter
        the label stack shows. NoEcho parameters are left out."""
        out = []
        for p, spec in self.params.items():
            if spec.get("NoEcho"):
                continue
            ptype = str(spec.get("Type", "String"))
            ref = f"var.{self.param_vars[p]}"
            if ptype.startswith("List<") or ptype == "CommaDelimitedList":
                value = f'join(",", {ref})'
            else:
                value = f"tostring({ref})"
            out.append((p, value, str(spec.get("Description", ""))))
        return out

    def _locals(self) -> str:
        lines = []
        self._in_conditions = True
        try:
            for name, c in self.conditions.items():
                self.begin(f"Conditions.{name}")
                lines.append(f"c_{hcl.snake(name)} = {self.expr(c)}")
        finally:
            self._in_conditions = False
        if self.mappings:
            lines.append(f"mappings = {self.expr(self.mappings, ANY, json_ctx=True)}")
        if self.is_root:
            data = (
                'data "aws_region" "current" {}\n'
                'data "aws_partition" "current" {}\n'
                'data "aws_caller_identity" "current" {}\n'
            )
            pseudo = (
                "pseudo = {\n"
                "  region     = data.aws_region.current.region\n"
                "  partition  = data.aws_partition.current.partition\n"
                "  dns_suffix = data.aws_partition.current.dns_suffix\n"
                "  account_id = data.aws_caller_identity.current.account_id\n"
                "}"
            )
        else:
            data = (
                'variable "pseudo" {\n'
                '  description = "Region, partition, URL suffix and account, '
                'passed from the root module."\n'
                "  type = object({ region = string, partition = string, "
                "dns_suffix = string, account_id = string })\n"
                "}\n"
            )
            pseudo = "pseudo = var.pseudo"
        return data + "\n" + hcl.block("locals", [pseudo] + lines)

    def _outputs(self) -> str:
        out = []
        self._output_values: dict[str, str] = {}
        for name, spec in self.t.get("Outputs", {}).items():
            self.begin(f"Outputs.{name}")
            try:
                value = self.expr(spec["Value"])
            except (TranslationError, KeyError) as exc:
                if not self.survey:
                    raise
                self.errors.append(f"Outputs.{name}: {exc}")
                continue
            if spec.get("Condition"):
                value = f"{self.cond_ref(spec['Condition'])} ? {value} : null"
            self._output_values[name] = value
            lines = [f"value = {value}"]
            if "Description" in spec:
                lines.append(f"description = {hcl.string(str(spec['Description']))}")
            out.append(hcl.block(f'output "{name}"', lines))
        out.append(self._directory())
        return "\n".join(out)

    def _directory(self) -> str:
        """A CloudFormation-shaped view of this module: every resource's logical
        id, type and physical id (the value CloudFormation's Ref returns), the
        outputs, and the same for each nested module. Tools that look resources
        up through CloudFormation read this instead for a Terraform deployment."""
        entries = []
        for lid, plan in self.plans.items():
            self.begin(f"directory.{lid}")
            if plan.kind == "module":
                physical = (
                    '"arn:${local.pseudo.partition}:cloudformation:'
                    "${local.pseudo.region}:${local.pseudo.account_id}:stack/"
                    f'${{var.{STACK_NAME_VAR}}}-{lid}/terraform"'
                )
                if plan.condition:
                    physical = f"({self.cond_ref(plan.condition)} ? {physical} : null)"
            elif plan.virtual:
                continue
            else:
                self._directory_mode = True
                try:
                    physical = self.ref(lid)
                except (TranslationError, KeyError):
                    continue
                finally:
                    self._directory_mode = False
            entries.append(
                hcl.obj(
                    [
                        ("LogicalResourceId", hcl.string(lid)),
                        ("ResourceType", hcl.string(plan.cfn_type)),
                        ("PhysicalResourceId", f"try(tostring({physical}), null)"),
                    ]
                )
            )
        outputs = []
        for name, value in self._output_values.items():
            outputs.append((name, f"try(tostring({value}), null)"))
        nested = []
        for lid, plan in self.plans.items():
            if plan.kind != "module":
                continue
            ref = f"module.{plan.name}"
            value = (
                f"one({ref}[*].cfn_resource_directory)"
                if plan.condition
                else f"{ref}.cfn_resource_directory"
            )
            nested.append((lid, value))
        body = [
            "resources = [for e in "
            + hcl.lst(entries)
            + " : e if e.PhysicalResourceId != null]",
            "outputs = " + hcl.obj(outputs, quote_keys=True),
            "nested = " + hcl.obj(nested, quote_keys=True),
        ]
        value = hcl.obj([(b.split(" = ", 1)[0], b.split(" = ", 1)[1]) for b in body])
        return hcl.block(
            "locals", [f"cfn_resource_directory_self = {value}"]
        ) + hcl.block(
            'output "cfn_resource_directory"',
            [
                'description = "CloudFormation-shaped resource directory, read by '
                'idp-cli and the SDK for a Terraform deployment."',
                "value = local.cfn_resource_directory_self",
            ],
        )


def _drop_empty_templates(integration: Any) -> Any:
    if not isinstance(integration, dict):
        return integration
    out = dict(integration)
    responses = out.get("IntegrationResponses")
    if isinstance(responses, list):
        fixed = []
        for r in responses:
            if isinstance(r, dict) and isinstance(r.get("ResponseTemplates"), dict):
                templates = {k: v for k, v in r["ResponseTemplates"].items() if v != ""}
                r = (
                    dict(r, ResponseTemplates=templates)
                    if templates
                    else {k: v for k, v in r.items() if k != "ResponseTemplates"}
                )
            fixed.append(r)
        out["IntegrationResponses"] = fixed
    return out


def report_json(tr: Translator) -> str:
    return json.dumps(
        {
            "resourceKinds": tr.report.resource_kinds,
            "warnings": tr.report.warnings,
            "resources": {
                lid: {"type": p.cfn_type, "kind": p.kind, "address": p.address}
                for lid, p in tr.plans.items()
            },
        },
        indent=2,
    )
