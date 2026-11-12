# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the roles that run an agent on document-derived text may reach.

The analytics agent holds an Athena tool and a Code Interpreter session, and the
functions that run it hold every grant the agent's tools can exercise. The same holds
for every role the agent capability manifest (``scripts/tests/agent_capabilities.yaml``)
names for an entry whose inputs include document-derived text. These checks bound
those roles to what their code paths use:

* the Code Interpreter actions are granted on the AWS-managed interpreter ARN pattern,
  never on ``"*"``, and a role whose entry sends output to the interpreter holds them;
* ``lambda:InvokeFunction`` names functions; a wildcard ARN is allowed only where the
  role's manifest declaration names it under ``invoke`` with the code path that needs
  it;
* each S3 object read on the input or output bucket, and each S3 object write or
  delete anywhere, is one the manifest declares for that role under
  ``object_access``, with the code path that ``uses`` it. A grant no code path uses
  is declared ``unused``, which ``test_agent_capabilities.py`` counts as an open
  finding, so an unused ``DeleteObject`` cannot be declared away quietly.

The universe is derived from source twice over: every function in a template whose
code constructs an agent (a call to ``create_analytics_agent`` or to an
``agent_factory.create_*`` method), found by reading each function's ``CodeUri``, and
the manifest's document-input roles. The grants are read from the template with SAM's
policy templates expanded from ``samtranslator``'s own ``policy_templates.json``.

⚠️ This reads identity policies only. A bucket policy, an SCP, a permissions boundary
and the KMS key policy also decide what a deployed role can do, and none of them is in
this repository.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set, Tuple

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_agent_capabilities as capabilities  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATES = (
    "template.yaml",
    "patterns/unified/template.yaml",
    *sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "nested").glob("*/template.yaml")
    ),
)

#: A function runs an agent when its code calls one of these.
AGENT_CONSTRUCTION = re.compile(
    r"\bcreate_analytics_agent\(|\bagent_factory\.create_\w+\("
)

#: The functions known to run the analytics agent when this file was written. The
#: derivation must still find each one; it may find more, and those are checked too.
MEASURED_AGENT_FUNCTIONS = {
    ("template.yaml", "AgentChatProcessorFunction"),
    ("template.yaml", "ChatStreamProcessorFunction"),
    ("template.yaml", "AgentProcessorFunction"),
    ("template.yaml", "AgentCoreMCPHandlerFunction"),
}

#: Buckets that hold documents and their processing results.
DOCUMENT_BUCKETS = {"InputBucket", "OutputBucket"}

#: Per role, the S3 object access its code uses: ``(kind, bucket, key pattern)`` with
#: the code path that needs it, or the finding that nothing does. ``kind`` is ``read``
#: (document buckets only), ``write`` or ``delete``. A key pattern is the part of the
#: resource after the bucket. Declared in the agent capability manifest's ``roles``
#: section, next to the entries that name each role.
DECLARED_OBJECT_ACCESS: Dict[str, Dict[Tuple[str, str, str], str]] = {
    role: {
        (item["kind"], item["bucket"], item["key"]): item.get("uses") or item["unused"]
        for item in decl.get("object_access", [])
    }
    for role, decl in capabilities.manifest()["roles"].items()
}

#: Per role, the ``lambda:InvokeFunction`` resources containing a wildcard that the
#: manifest declares, each with the code path that needs it.
DECLARED_WILDCARD_INVOKES: Dict[str, Dict[str, str]] = {
    role: {
        item["resource"]: item.get("uses") or item["unused"]
        for item in decl.get("invoke", [])
    }
    for role, decl in capabilities.manifest()["roles"].items()
}

READ_ACTIONS = ("s3:GetObject", "s3:GetObjectVersion")
WRITE_ACTIONS = ("s3:PutObject",)
DELETE_ACTIONS = ("s3:DeleteObject", "s3:DeleteObjectVersion")


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag(loader, suffix, node):
    key = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
        return {key: value}
    if isinstance(node, yaml.SequenceNode):
        return {key: loader.construct_sequence(node, deep=True)}
    return {key: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag)


@functools.lru_cache(maxsize=None)
def _load(template: str) -> Dict[str, Any]:
    return yaml.load((REPO_ROOT / template).read_text(), Loader=_CfnLoader) or {}


@functools.lru_cache(maxsize=None)
def _agent_functions() -> Set[Tuple[str, str]]:
    found = set()
    for template in TEMPLATES:
        resources = _load(template).get("Resources") or {}
        for logical_id, resource in resources.items():
            if resource.get("Type") != "AWS::Serverless::Function":
                continue
            code_uri = (resource.get("Properties") or {}).get("CodeUri")
            if not isinstance(code_uri, str):
                continue
            code_dir = (REPO_ROOT / template).parent / code_uri
            if not code_dir.is_dir():
                continue
            for source in code_dir.rglob("*.py"):
                parts = set(source.relative_to(code_dir).parts)
                if "tests" in parts or source.name.startswith("test_"):
                    continue
                if AGENT_CONSTRUCTION.search(source.read_text(errors="ignore")):
                    found.add((template, logical_id))
                    break
    return found


@functools.lru_cache(maxsize=None)
def _sam_templates() -> Dict[str, Any]:
    import samtranslator

    data = (
        Path(samtranslator.__file__).parent
        / "policy_templates_data"
        / "policy_templates.json"
    )
    return json.loads(data.read_text())["Templates"]


def _substitute(node: Any, params: Dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if set(node) == {"Ref"} and node["Ref"] in params:
            return params[node["Ref"]]
        return {k: _substitute(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, params) for v in node]
    return node


def _statements(policies: List[Any]) -> Iterator[Dict[str, Any]]:
    """Every Allow statement in a SAM ``Policies`` list, SAM templates expanded and
    both arms of an ``Fn::If`` included."""
    sam = _sam_templates()

    def flatten(stmt):
        if isinstance(stmt, dict) and "Fn::If" in stmt:
            for arm in stmt["Fn::If"][1:]:
                yield from flatten(arm)
        elif isinstance(stmt, dict) and stmt.get("Effect") == "Allow":
            yield stmt

    for entry in policies:
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if key == "Statement":
                for stmt in value if isinstance(value, list) else [value]:
                    yield from flatten(stmt)
            elif key in sam:
                definition = _substitute(sam[key]["Definition"], value or {})
                for stmt in definition["Statement"]:
                    yield from flatten(stmt)


def _render(node: Any) -> str:
    """A resource expression as text, with references written ``${Name}``."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " | ".join(_render(v) for v in node)
    if isinstance(node, dict):
        if "Ref" in node:
            return "${%s}" % node["Ref"]
        if "Fn::GetAtt" in node:
            return "${%s}" % ".".join(node["Fn::GetAtt"])
        if "Fn::If" in node:
            return _render(node["Fn::If"][1])
        if "Fn::Sub" in node:
            sub = node["Fn::Sub"]
            if isinstance(sub, str):
                return sub
            text, variables = sub
            for name, value in variables.items():
                text = text.replace("${%s}" % name, _render(value))
            return text
    return repr(node)


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else [value]


def _grants(logical_id: str, template: str) -> List[Tuple[str, str]]:
    properties = _load(template)["Resources"][logical_id].get("Properties") or {}
    out = []
    for stmt in _statements(properties.get("Policies") or []):
        for action in _as_list(stmt.get("Action") or []):
            for resource in _as_list(stmt.get("Resource") or []):
                out.append((action, _render(resource)))
    return out


_BUCKET_REF = re.compile(r"\$\{(\w*Bucket)(?:Name)?(?:\.Arn)?\}")


def _object_access(grants: List[Tuple[str, str]]) -> Set[Tuple[str, str, str]]:
    found = set()
    for action, resource in grants:
        if not action.startswith("s3:") and action != "*":
            continue
        match = _BUCKET_REF.search(resource)
        if not match:
            continue
        bucket = match.group(1)
        key = resource[match.end() :]
        if not key.startswith("/"):
            continue  # bucket-level resource; object actions do not apply to it
        for kind, ops in (
            ("read", READ_ACTIONS),
            ("write", WRITE_ACTIONS),
            ("delete", DELETE_ACTIONS),
        ):
            if any(fnmatch.fnmatchcase(op, action) for op in ops):
                if kind == "read" and bucket not in DOCUMENT_BUCKETS:
                    continue
                found.add((kind, bucket, key))
    return found


ROLES = sorted(MEASURED_AGENT_FUNCTIONS)


@pytest.mark.unit
def test_the_derivation_finds_every_function_that_runs_the_agent():
    derived = _agent_functions()
    assert derived, "no function in any template constructs an agent"
    missing = sorted(MEASURED_AGENT_FUNCTIONS - derived)
    assert not missing, f"no longer derived as running an agent: {missing}"
    undeclared = sorted(f for _, f in derived if f not in DECLARED_OBJECT_ACCESS)
    assert not undeclared, (
        f"these functions run an agent and have no DECLARED_OBJECT_ACCESS entry: "
        f"{undeclared}. Add one listing the S3 object access their code uses."
    )


def _all_roles() -> List[Tuple[str, str]]:
    """The functions that run the analytics agent, plus every role the capability
    manifest names for an entry that takes document-derived input."""
    return sorted(
        _agent_functions()
        | MEASURED_AGENT_FUNCTIONS
        | capabilities.document_input_roles()
    )


@pytest.mark.unit
def test_every_manifest_role_is_a_function_in_its_template():
    for role, decl in capabilities.manifest()["roles"].items():
        resources = _load(decl["template"]).get("Resources") or {}
        assert resources.get(role, {}).get("Type") == "AWS::Serverless::Function", (
            f"{role} is not a function in {decl['template']}"
        )


@pytest.mark.unit
def test_every_function_that_runs_the_agent_is_named_in_the_manifest():
    named = {f for _, f in capabilities.document_input_roles()}
    missing = sorted(f for _, f in _agent_functions() if f not in named)
    assert not missing, (
        f"these functions run an agent and no capability manifest entry that takes "
        f"document-derived input names them in `roles`: {missing}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("template, function", _all_roles())
def test_code_interpreter_is_granted_on_the_managed_interpreter_only(
    template, function
):
    grants = _grants(function, template)
    interpreter = [
        (a, r)
        for a, r in grants
        if a.startswith("bedrock-agentcore:") and "CodeInterpreter" in a
    ]
    if function in capabilities.roles_with_output("code_interpreter_sandbox"):
        assert interpreter, (
            f"{function} runs an agent whose manifest entry sends output to the Code "
            "Interpreter, and holds no Code Interpreter grant"
        )
    wide = [(a, r) for a, r in grants if a in ("bedrock-agentcore:*", "*")]
    assert not wide, f"{function} grants every AgentCore action: {wide}"
    for action, resource in interpreter:
        assert resource.endswith(":aws:code-interpreter/*"), (
            f"{function} grants {action} on {resource!r}; scope it to "
            "arn:${AWS::Partition}:bedrock-agentcore:*:aws:code-interpreter/*"
        )


@pytest.mark.unit
@pytest.mark.parametrize("template, function", _all_roles())
def test_lambda_invoke_names_functions(template, function):
    invokes = [
        (a, r)
        for a, r in _grants(function, template)
        if fnmatch.fnmatchcase("lambda:InvokeFunction", a)
    ]
    declared = DECLARED_WILDCARD_INVOKES.get(function, {})
    for action, resource in invokes:
        assert "*" not in resource or resource in declared, (
            f"{function} grants {action} on {resource!r}. Name the function its code "
            "invokes (the document lookup is !GetAtt LookupFunction.Arn), or declare "
            "the pattern under the role's `invoke` in "
            "scripts/tests/agent_capabilities.yaml with the code path that needs it."
        )
    held = {r for _, r in invokes}
    unused = sorted(set(declared) - held)
    assert not unused, (
        f"the manifest declares invoke grants {function} no longer holds: {unused}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("template, function", _all_roles())
def test_s3_object_access_is_declared(template, function):
    granted = _object_access(_grants(function, template))
    declared = set(DECLARED_OBJECT_ACCESS.get(function, {}))
    undeclared = sorted(granted - declared)
    assert not undeclared, (
        f"{function} holds S3 object access no code path is declared to need: "
        f"{undeclared}"
    )
    unused = sorted(declared - granted)
    assert not unused, (
        f"DECLARED_OBJECT_ACCESS lists access {function} no longer holds: {unused}. "
        "Remove the entry."
    )
