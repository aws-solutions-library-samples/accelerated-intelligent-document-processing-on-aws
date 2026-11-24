# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The callers that invoke an API resolver directly can find it and may invoke it.

Two resolvers are invoked directly, with no identity in the event, as well as
through the REST API: the reprocess resolver (by the SDK, the CLI and the MCP
``reprocess`` tool) and the review queues resolver (by the SDK and the CLI). Each
caller finds the function a different way, and a wrong name on either side fails
only at run time, on a deployed stack:

* the SDK reads the function's ARN from an output of the nested API resolver
  stack, by a name it holds as a constant;
* the MCP handler is handed the ARN in its environment and its role names the
  function it may invoke.

These checks read the templates and the code and hold the names together.
"""

from __future__ import annotations

import ast
from typing import Any

import gate_premises
import pytest
import test_analytics_role_grants as role_grants

pytestmark = pytest.mark.unit

REPO = gate_premises.REPO_ROOT
PARENT = "template.yaml"
NESTED = "nested/api-resolvers/template.yaml"
NESTED_STACK = "APIRESOLVERSTACK"
MCP_HANDLER = "AgentCoreMCPHandlerFunction"
REPROCESS_OUTPUT = "ReprocessDocumentResolverFunctionArn"

#: SDK module, the constant that names a nested-stack output, and the function
#: the output has to be the ARN of.
SDK_OUTPUTS = (
    (
        "lib/idp_sdk/idp_sdk/_core/reprocess.py",
        "REPROCESS_OUTPUT",
        "ReprocessDocumentResolverFunction",
    ),
    (
        "lib/idp_sdk/idp_sdk/operations/review_queues.py",
        "REVIEW_QUEUES_OUTPUT",
        "ReviewQueuesResolverFunction",
    ),
)


def _constant(path: str, name: str) -> str:
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            assert isinstance(node.value, ast.Constant), f"{path}: {name}"
            assert isinstance(node.value.value, str), f"{path}: {name}"
            return node.value.value
    raise AssertionError(f"{path} does not assign {name}")


def _get_att(value: Any) -> str:
    """``!GetAtt A.B`` as ``A.B``; anything else as an empty string."""
    if not isinstance(value, dict):
        return ""
    target = value.get("Fn::GetAtt")
    if isinstance(target, list):
        return ".".join(target)
    return target if isinstance(target, str) else ""


@pytest.mark.parametrize(("path", "constant", "function"), SDK_OUTPUTS)
def test_the_output_the_sdk_asks_for_is_the_resolvers_arn(path, constant, function):
    output_key = _constant(path, constant)
    nested = gate_premises.load_template(NESTED)

    outputs = nested.get("Outputs") or {}
    assert output_key in outputs, (
        f"{path} asks the API resolver stack for the output '{output_key}', which "
        f"{NESTED} does not declare"
    )
    assert _get_att(outputs[output_key].get("Value")) == f"{function}.Arn"
    assert function in nested["Resources"]


def test_the_sdk_names_the_same_output_in_its_missing_output_message():
    """The message that tells an operator to update the stack is keyed by the
    output's name; a renamed output would lose it."""
    source = (REPO / "lib/idp_sdk/idp_sdk/_core/resolver_invoke.py").read_text()

    for path, constant, _ in SDK_OUTPUTS:
        assert f'"{_constant(path, constant)}"' in source


def _mcp_handler() -> dict:
    return gate_premises.load_template(PARENT)["Resources"][MCP_HANDLER]


def test_the_mcp_handler_is_handed_the_reprocess_resolvers_arn():
    variables = _mcp_handler()["Properties"]["Environment"]["Variables"]

    assert _get_att(variables.get("REPROCESS_FUNCTION_ARN")) == (
        f"{NESTED_STACK}.Outputs.{REPROCESS_OUTPUT}"
    )
    tool = (
        REPO / "src/lambda/agentcore_mcp_handler/tools/batch_reprocess.py"
    ).read_text()
    assert 'os.environ["REPROCESS_FUNCTION_ARN"]' in tool


def test_the_mcp_handler_may_invoke_the_reprocess_resolver_and_no_other_wildcard():
    invokes = [
        resource
        for action, resource in role_grants._grants(MCP_HANDLER, PARENT)
        if action == "lambda:InvokeFunction"
    ]

    assert any(REPROCESS_OUTPUT in resource for resource in invokes), invokes
    assert not [resource for resource in invokes if "*" in resource]


def test_the_mcp_handler_deletes_no_object():
    """Its reprocess tool used to clear a document's output itself. The resolver
    does that under its own role, so the handler's role holds no delete."""
    deletes = [
        (action, resource)
        for action, resource in role_grants._grants(MCP_HANDLER, PARENT)
        if action.startswith("s3:Delete") or action in ("s3:*", "*")
    ]

    assert deletes == []


def test_the_reprocess_resolver_reads_the_acting_caller_only_without_an_identity():
    """What makes the MCP handler's invoke safe to trust: the resolver holds the
    request to the caller it names, and ignores that name on the REST route."""
    source = (
        REPO / "nested/api-resolvers/src/lambda/reprocess_document_resolver/index.py"
    ).read_text()
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_allowed_versions"
    )
    body = ast.get_source_segment(source, function) or ""

    assert body.index("is_direct_invoke(event)") < body.index(
        'event.get("actingCaller")'
    )
    assert "Caller.from_payload" in body
