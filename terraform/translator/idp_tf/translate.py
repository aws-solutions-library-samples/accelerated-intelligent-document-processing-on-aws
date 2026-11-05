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
CUSTOM_TYPES = ("Custom::", "AWS::CloudFormation::CustomResource")

# CloudFormation `Ref` returns something other than the Cloud Control
# identifier (awscc `id`) for these types.
AWSCC_REF_ATTR: dict[str, str] = {}


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
    ):
        from . import aws_mappers

        self.t = template
        self.schema = schema
        self.mode = mode
        self.mappers = aws_mappers.MAPPERS
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
        # In survey mode every failure is collected instead of raised, so one
        # run lists everything a template needs.
        self.survey = survey
        self.errors: list[str] = []
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
            elif (
                self.mode == "hybrid" and rtype in self.aws_mapped
            ) or not self.schema.has(rtype):
                mapper = self.mappers.get(rtype)
                if mapper is None:
                    msg = (
                        f"{lid}: {rtype} has no awscc resource and no "
                        "hashicorp/aws mapping"
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
                self.report.warn(
                    f"{lid}: DeletionPolicy {policy} has no Terraform equivalent; "
                    "terraform destroy deletes it"
                )
        for plan in self.plans.values():
            if plan.kind == "aws":
                self.mappers[plan.cfn_type].plan(self, plan)

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
            "AWS::Region": "data.aws_region.current.region",
            "AWS::AccountId": "data.aws_caller_identity.current.account_id",
            "AWS::Partition": "data.aws_partition.current.partition",
            "AWS::URLSuffix": "data.aws_partition.current.dns_suffix",
            "AWS::StackName": f"var.{STACK_NAME_VAR}",
            "AWS::NoValue": "null",
        }
        if name in pseudo:
            return pseudo[name]
        if name == "AWS::StackId":
            return (
                '"arn:${data.aws_partition.current.partition}:cloudformation:'
                "${data.aws_region.current.region}:"
                "${data.aws_caller_identity.current.account_id}:stack/"
                f'${{var.{STACK_NAME_VAR}}}/terraform"'
            )
        if name in self.param_vars:
            return "var." + self.param_vars[name]
        plan = self.plans.get(name)
        if plan is None:
            raise TranslationError(f"{self._current}: Ref to unknown name {name}")
        if plan.kind == "custom":
            self._refs.add(name)
            inner = f"jsondecode({plan.address}{'[0]' if plan.condition else ''}"
            return inner + ".result).PhysicalResourceId"
        return self._wrap(plan, plan.ref_attr)

    def getatt(self, name: str, attr: str) -> str:
        plan = self.plans.get(name)
        if plan is None:
            raise TranslationError(f"{self._current}: GetAtt on unknown {name}")
        if plan.kind == "custom":
            self._refs.add(name)
            idx = "[0]" if plan.condition else ""
            return f"jsondecode({plan.address}{idx}.result).Data.{attr}"
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

    def meta_lines(self, plan: Plan, res: dict[str, Any]) -> tuple[list, list]:
        head = []
        if plan.condition:
            head.append(f"count = {self.cond_ref(plan.condition)} ? 1 : 0")
        tail = []
        deps = self.depends_on(plan, res)
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
            lines.append(f"{tf_name} = {self.expr(x, child)}")
        head, tail = self.meta_lines(plan, res)
        return hcl.block(
            f'resource "{plan.tf_type}" "{plan.name}"', head + lines + tail
        )

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
        lines = [
            "function_name = aws_lambda_function.cfn_bridge.function_name",
            'lifecycle_scope = "CRUD"',
            f"input = jsonencode(merge({body}, {{ StackName = var.{STACK_NAME_VAR} }}))",
        ]
        head, tail = self.meta_lines(plan, res)
        self.custom_tokens.append(self.expr(token))
        return hcl.block(
            f'resource "aws_lambda_invocation" "{plan.name}"', head + lines + tail
        )

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
                    'description = "Name used where CloudFormation would use '
                    'the stack name."',
                ],
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
            allowed = spec.get("AllowedValues")
            if allowed:
                vals = hcl.lst([hcl.string(str(a)) for a in allowed])
                name = self.param_vars[p]
                lines.append(
                    hcl.block(
                        "validation",
                        [
                            f"condition = contains({vals}, tostring(var.{name}))",
                            f'error_message = "{name} must be one of the '
                            'allowed values."',
                        ],
                    ).rstrip()
                )
            out.append(hcl.block(f'variable "{self.param_vars[p]}"', lines))
        return "\n".join(out)

    def _locals(self) -> str:
        lines = []
        for name, c in self.conditions.items():
            self.begin(f"Conditions.{name}")
            lines.append(f"c_{hcl.snake(name)} = {self.expr(c)}")
        if self.mappings:
            lines.append(f"mappings = {self.expr(self.mappings, ANY, json_ctx=True)}")
        data = (
            'data "aws_region" "current" {}\n'
            'data "aws_partition" "current" {}\n'
            'data "aws_caller_identity" "current" {}\n'
        )
        if not lines:
            return data
        return data + "\n" + hcl.block("locals", lines)

    def _outputs(self) -> str:
        out = []
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
            lines = [f"value = {value}"]
            if "Description" in spec:
                lines.append(f"description = {hcl.string(str(spec['Description']))}")
            out.append(hcl.block(f'output "{name}"', lines))
        return "\n".join(out)


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
