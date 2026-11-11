# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the roles that run the analytics agent may reach.

The analytics agent holds an Athena tool and a Code Interpreter session, and the
functions that run it hold every grant the agent's tools can exercise. These checks
bound those roles to what their code paths use:

* the Code Interpreter actions are granted on the AWS-managed interpreter ARN pattern,
  never on ``"*"``;
* ``lambda:InvokeFunction`` names functions, never a wildcard ARN (the only function
  any of these code paths invokes is the document lookup);
* each S3 object read on the input or output bucket, and each S3 object write or
  delete anywhere, is one this file declares for that role in
  :data:`DECLARED_OBJECT_ACCESS`, with the code path that needs it.

Both halves are derived from source. The universe is every function in a template
whose code constructs an agent (a call to ``create_analytics_agent`` or to an
``agent_factory.create_*`` method), found by reading each function's ``CodeUri``; the
grants are read from the template with SAM's policy templates expanded from
``samtranslator``'s own ``policy_templates.json``.

⚠️ This reads identity policies only. A bucket policy, an SCP, a permissions boundary
and the KMS key policy also decide what a deployed role can do, and none of them is in
this repository.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set, Tuple

import pytest
import yaml

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
#: the code path that needs it. ``kind`` is ``read`` (document buckets only), ``write``
#: or ``delete``. A key pattern is the part of the resource after the bucket.
DECLARED_OBJECT_ACCESS: Dict[str, Dict[Tuple[str, str, str], str]] = {
    "AgentChatProcessorFunction": {
        ("write", "ConfigurationBucket", "/config_revisions/*"): (
            "configuration profile saves cut a revision (#1778)"
        ),
        ("delete", "ConfigurationBucket", "/config_revisions/*"): (
            "configuration profile saves cut a revision (#1778)"
        ),
        ("write", "ReportingBucket", "/athena-results/*"): (
            "Athena writes query results with the caller's credentials"
        ),
    },
    "AgentProcessorFunction": {
        ("write", "ConfigurationBucket", "/config_revisions/*"): (
            "configuration profile saves cut a revision (#1778)"
        ),
        ("delete", "ConfigurationBucket", "/config_revisions/*"): (
            "configuration profile saves cut a revision (#1778)"
        ),
        ("write", "ReportingBucket", "/athena-results/*"): (
            "Athena writes query results with the caller's credentials"
        ),
    },
    "ChatStreamProcessorFunction": {
        ("write", "ConfigurationBucket", "/config_revisions/*"): (
            "configuration profile saves cut a revision (#1778)"
        ),
        ("delete", "ConfigurationBucket", "/config_revisions/*"): (
            "configuration profile saves cut a revision (#1778)"
        ),
        ("write", "ReportingBucket", "/athena-results/*"): (
            "Athena writes query results with the caller's credentials"
        ),
        ("read", "OutputBucket", "/*"): (
            "Chat with Document reads each page's text from its TextUri "
            "(vendored/chat_with_document_processor.py, _get_full_text)"
        ),
        ("write", "OutputBucket", "/*/summary/fulltext.txt"): (
            "Chat with Document caches the assembled text at "
            "<document key>/summary/fulltext.txt"
        ),
    },
    # The MCP handler's batch tools upload, read, reprocess and clean up documents
    # through idp_sdk; its S3 scope is the batch tools' and is declared as granted.
    "AgentCoreMCPHandlerFunction": {
        ("read", "InputBucket", "/*"): "batch tools read uploaded documents",
        ("read", "OutputBucket", "/*"): "get_results reads processing output",
        ("write", "InputBucket", "/*"): "batch_run uploads documents",
        ("write", "OutputBucket", "/*"): "batch_reprocess rewrites output",
        ("write", "MCPContentBucket", "/*"): "batch_run stages content under temp/",
        ("write", "ReportingBucket", "/*"): (
            "search runs Athena, which writes results with the caller's credentials"
        ),
        ("delete", "OutputBucket", "/*"): (
            "batch_reprocess clears prior output (idp_sdk rerun_processor)"
        ),
    },
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
    return sorted(_agent_functions() | MEASURED_AGENT_FUNCTIONS)


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
    assert interpreter, f"{function} holds no Code Interpreter grant"
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
    for action, resource in invokes:
        assert "*" not in resource, (
            f"{function} grants {action} on {resource!r}. Name the function its code "
            "invokes (the document lookup is !GetAtt LookupFunction.Arn)."
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
