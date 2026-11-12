"""Upgrade safety: which resources hold data, and which a custom resource can
make Terraform replace.

Two questions are answered over a whole translated tree (the root
:class:`~idp_tf.translate.Translator` and its nested modules):

**Which addresses are stateful.** ``STATEFUL_TYPES`` names every resource type
whose replacement or deletion loses something the template cannot recreate: the
data in a bucket or table, the users of a user pool, what a key encrypted, an
ingested knowledge base. The addresses are computed from that list at
translation time and written into ``idp-terraform.json`` as
``statefulAddresses``; ``idp-cli deploy --terraform`` refuses a plan that
deletes or replaces any of them (and any ``retainedAddresses``) unless told
otherwise.

**Which create-only properties take a custom resource's output.** A custom
resource is an ``aws_lambda_invocation`` through the bridge. When any of its
inputs changes, its ``result`` is unknown until apply, so every value read from
it is unknown at plan. A property that cannot change in place and is fed such a
value, directly or through parameters, outputs and other resources, is planned
as a replacement, although CloudFormation, which sees the handler's actual
answer, updates nothing. Measured on a live upgrade: renaming the roles updated
the custom resource that creates the S3 Vectors knowledge base, and the
knowledge base's data source, whose ``KnowledgeBaseId`` is create-only, was
replaced. ``custom_output_edges`` lists every such edge. An edge on a stateful
resource must be handled in ``translate.STABLE_CUSTOM_OUTPUTS`` or the
translation fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import create_only

if TYPE_CHECKING:
    from .translate import Translator

#: Resource types whose replacement or deletion loses state. Each reason says
#: what is lost. Types deliberately left out, and why, are in NOT_STATEFUL.
STATEFUL_TYPES: dict[str, str] = {
    "AWS::S3::Bucket": "objects: documents, results, configuration and logs",
    "AWS::DynamoDB::Table": "items: tracking, configuration, users and history",
    "AWS::Cognito::UserPool": (
        "users and their passwords, which cannot be exported or recreated"
    ),
    "AWS::KMS::Key": (
        "a replaced key is scheduled for deletion, and everything it encrypted "
        "becomes unreadable when it is deleted"
    ),
    "AWS::Bedrock::KnowledgeBase": "the knowledge base and its ingestion history",
    "AWS::Bedrock::DataSource": (
        "the data source's ingestion jobs; a new one re-ingests every document"
    ),
    "AWS::OpenSearchServerless::Collection": "the vector index's documents",
    "AWS::SecretsManager::Secret": (
        "a value set after deployment (the external MCP agent credentials)"
    ),
    # Custom resources whose Delete removes data. An address change without a
    # moved block deletes the invocation, which runs the handler's Delete.
    "Custom::S3VectorBucketAndIndex": "the S3 Vectors bucket and its index",
    "Custom::S3VectorsKnowledgeBase": "the S3 Vectors knowledge base",
    "Custom::InvokeCreateOSSIndexLambdaFunction": (
        "the OpenSearch Serverless vector index"
    ),
    "Custom::McpCredentialsCleanup": (
        "every MCP service credential, which its Delete revokes"
    ),
}

#: Types a reader might expect in STATEFUL_TYPES, with the reason each is not.
NOT_STATEFUL: dict[str, str] = {
    "AWS::Logs::LogGroup": (
        "operational logs with a retention period; CloudFormation replaces a "
        "log group on a rename too"
    ),
    "AWS::SQS::Queue": (
        "transient messages; a replacement loses only messages in flight, as "
        "it does under CloudFormation"
    ),
    "AWS::ECR::Repository": "images, which the DockerBuild custom resource rebuilds",
    "AWS::Glue::Table": "metadata over objects in S3, recreated from the template",
    "AWS::Glue::Database": "metadata, recreated from the template",
    "AWS::SSM::Parameter": (
        "written from the template, except a parameter with a second writer, "
        "which is stateful through LIFECYCLE_OVERRIDES (see stateful_reason)"
    ),
}


def stateful_reason(tr: Translator, lid: str) -> str | None:
    """Why a resource is stateful, or None."""
    from .translate import LIFECYCLE_OVERRIDES

    plan = tr.plans[lid]
    if plan.cfn_type in STATEFUL_TYPES:
        return STATEFUL_TYPES[plan.cfn_type]
    if (plan.cfn_type, lid) in LIFECYCLE_OVERRIDES:
        # A second writer owns part of the value, so recreating the resource
        # from the template loses what that writer put there.
        return (
            "written at deploy time by "
            + LIFECYCLE_OVERRIDES[(plan.cfn_type, lid)][1].split(" ", 1)[0]
        )
    return None


def stateful_addresses(tr: Translator, prefix: str = "") -> list[str]:
    """Module-qualified addresses (no instance keys) of every stateful resource."""
    out = []
    for lid, plan in tr.plans.items():
        if plan.kind == "module" or plan.virtual:
            continue
        if stateful_reason(tr, lid) is not None:
            out.append(prefix + plan.address)
    for name, child in tr.children.items():
        out.extend(stateful_addresses(child, f"{prefix}module.{name}."))
    return sorted(out)


# ------------------------------------------------- custom-output reachability
@dataclass(frozen=True)
class Edge:
    """A create-only property fed by a custom resource's output."""

    stack: str  # nested-stack path of logical ids, "" for the root
    logical_id: str
    cfn_type: str
    address: str  # module-qualified
    prop: str  # property path, "/"-separated
    sources: tuple[str, ...]  # "<stack>:<LogicalId>" of the custom resources
    stateful: bool
    handled: bool

    def describe(self) -> str:
        where = f"{self.stack}/{self.logical_id}" if self.stack else self.logical_id
        state = "stateful" if self.stateful else "not stateful"
        mitigation = "handled" if self.handled else "UNHANDLED"
        return (
            f"{where} ({self.cfn_type}) create-only {self.prop} <- "
            f"{', '.join(self.sources)} [{state}, {mitigation}]"
        )


@dataclass
class _Node:
    tr: Translator
    stack: str
    prefix: str
    params: dict[str, frozenset[str]] = field(default_factory=dict)
    # Sources reaching any attribute of a resource / its identity (Ref).
    attrs: dict[str, frozenset[str]] = field(default_factory=dict)
    identity: dict[str, frozenset[str]] = field(default_factory=dict)
    outputs: dict[str, frozenset[str]] = field(default_factory=dict)
    children: dict[str, _Node] = field(default_factory=dict)


SUB_NAME = re.compile(r"\$\{([^}!][^}]*)\}")
EMPTY: frozenset[str] = frozenset()


def _build(tr: Translator, stack: str, prefix: str) -> _Node:
    node = _Node(tr, stack, prefix)
    for lid, plan in tr.plans.items():
        if plan.kind == "module":
            child_stack = f"{stack}/{lid}" if stack else lid
            node.children[lid] = _build(
                tr.children[plan.name], child_stack, f"{prefix}module.{plan.name}."
            )
    return node


def _sources(node: _Node, v: Any) -> frozenset[str]:
    if isinstance(v, list):
        out: frozenset[str] = EMPTY
        for x in v:
            out |= _sources(node, x)
        return out
    if not isinstance(v, dict):
        return EMPTY
    if len(v) == 1:
        fn, arg = next(iter(v.items()))
        if fn == "Ref" and isinstance(arg, str):
            return _ref(node, arg)
        if fn == "Fn::GetAtt":
            name, attr = arg.split(".", 1) if isinstance(arg, str) else arg
            return _getatt(node, name, attr)
        if fn == "Fn::Sub":
            text, variables = (arg if isinstance(arg, list) else (arg, {}))[:2]
            out = _sources(node, variables)
            for m in SUB_NAME.finditer(text if isinstance(text, str) else ""):
                ref = m.group(1).strip()
                if ref in variables:
                    continue
                if "." in ref and not ref.startswith("AWS::"):
                    out |= _getatt(node, *ref.split(".", 1))
                else:
                    out |= _ref(node, ref)
            return out
        if fn == "Fn::If":
            return _sources(node, arg[1:])
    out = EMPTY
    for x in v.values():
        out |= _sources(node, x)
    return out


def _source_id(node: _Node, lid: str) -> str:
    return f"{node.stack}:{lid}" if node.stack else lid


def _ref(node: _Node, name: str) -> frozenset[str]:
    if name in node.params:
        return node.params[name]
    plan = node.tr.plans.get(name)
    if plan is None:
        return EMPTY
    if plan.kind == "custom":
        return frozenset({_source_id(node, name)})
    return node.identity.get(name, EMPTY)


def _getatt(node: _Node, name: str, attr: str) -> frozenset[str]:
    plan = node.tr.plans.get(name)
    if plan is None:
        return EMPTY
    if plan.kind == "custom":
        return frozenset({_source_id(node, name)})
    if plan.kind == "module":
        out = attr.removeprefix("Outputs.")
        return node.children[name].outputs.get(out, EMPTY)
    # Conservative: an attribute of a resource whose inputs are unknown may be
    # one of those inputs passed through (an SSM parameter's Value is).
    return node.attrs.get(name, EMPTY) | node.identity.get(name, EMPTY)


def _walk(v: Any, path: tuple[str, ...]):
    """(path, value) for every intrinsic leaf below ``v``. Lists add no
    segment; an ``Fn::If`` puts both branches at its own path."""
    from .translate import is_intrinsic

    if isinstance(v, dict) and set(v) == {"Fn::If"}:
        for branch in v["Fn::If"][1:]:
            yield from _walk(branch, path)
    elif is_intrinsic(v):
        yield path, v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield from _walk(x, (*path, k))
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x, path)


def _force_new(tr: Translator, cfn_type: str, kind: str) -> list[tuple[str, ...]]:
    extra: tuple[str, ...] = ()
    if kind == "aws":
        extra = tuple(getattr(tr.mappers.get(cfn_type), "force_new", ()) or ())
    return create_only.paths(cfn_type, extra)


def _pass(node: _Node) -> bool:
    """One propagation step over a node and its children; True if anything moved."""
    changed = False
    tr = node.tr
    for lid, plan in tr.plans.items():
        if plan.kind == "module":
            child = node.children[lid]
            for p, value in (plan.props.get("Parameters") or {}).items():
                got = _sources(node, value)
                if got != child.params.get(p, EMPTY):
                    child.params[p] = got
                    changed = True
            changed |= _pass(child)
            continue
        if plan.kind == "custom":
            continue
        from .translate import STABLE_CUSTOM_OUTPUTS

        # A property in STABLE_CUSTOM_OUTPUTS is emitted with ignore_changes, so
        # its planned value is the one in state: known, and never a replacement.
        props = {
            k: v
            for k, v in plan.props.items()
            if (plan.cfn_type, lid, k) not in STABLE_CUSTOM_OUTPUTS
        }
        reach = _sources(node, props)
        if reach != node.attrs.get(lid, EMPTY):
            node.attrs[lid] = reach
            changed = True
        fn = _force_new(tr, plan.cfn_type, plan.kind)
        replaced: frozenset[str] = EMPTY
        for path, leaf in _walk(props, ()):
            if create_only.is_create_only(path, fn):
                replaced |= _sources(node, leaf)
        if replaced != node.identity.get(lid, EMPTY):
            node.identity[lid] = replaced
            changed = True
    for name, spec in tr.t.get("Outputs", {}).items():
        got = _sources(node, spec.get("Value"))
        if got != node.outputs.get(name, EMPTY):
            node.outputs[name] = got
            changed = True
    return changed


def _edges(node: _Node, out: list[Edge]) -> None:
    from .translate import STABLE_CUSTOM_OUTPUTS

    tr = node.tr
    for lid, plan in tr.plans.items():
        if plan.kind == "module":
            _edges(node.children[lid], out)
            continue
        if plan.kind == "custom":
            continue
        fn = _force_new(tr, plan.cfn_type, plan.kind)
        by_prop: dict[str, frozenset[str]] = {}
        for path, leaf in _walk(plan.props, ()):
            if not create_only.is_create_only(path, fn):
                continue
            got = _sources(node, leaf)
            if got:
                key = "/".join(path)
                by_prop[key] = by_prop.get(key, EMPTY) | got
        for prop, sources in sorted(by_prop.items()):
            top = prop.split("/", 1)[0]
            out.append(
                Edge(
                    stack=node.stack,
                    logical_id=lid,
                    cfn_type=plan.cfn_type,
                    address=node.prefix + plan.address,
                    prop=prop,
                    sources=tuple(sorted(sources)),
                    stateful=stateful_reason(tr, lid) is not None,
                    handled=(plan.cfn_type, lid, top) in STABLE_CUSTOM_OUTPUTS,
                )
            )


def custom_output_edges(tr: Translator) -> list[Edge]:
    """Every create-only property in the tree fed by a custom resource output."""
    root = _build(tr, "", "")
    for _ in range(50):
        if not _pass(root):
            break
    else:  # pragma: no cover - the lattice is finite, so this cannot loop
        raise RuntimeError("custom-output propagation did not converge")
    out: list[Edge] = []
    _edges(root, out)
    return out


def unknown_create_only_types(tr: Translator) -> list[str]:
    """Types in the tree that ``schemas/create_only.json`` does not cover."""
    out: set[str] = set()
    for plan in tr.plans.values():
        if plan.kind in ("aws", "awscc") and not create_only.known(plan.cfn_type):
            out.add(plan.cfn_type)
    for child in tr.children.values():
        out.update(unknown_create_only_types(child))
    return sorted(out)
