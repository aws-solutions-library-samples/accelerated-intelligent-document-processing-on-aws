# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``EnableQuickStartWidget`` reaches every function that can run a Quick Start turn.

The resolver and both chat processors refuse a Quick Start message when
``QUICK_START_ENABLED`` is ``false``, and read an unset variable as the parameter's
default. So a function that loses the variable silently offers Quick Start again on
a stack that turned it off; this pins the wiring from the parent parameter, through
the nested API stack, to each function's environment.
"""

from __future__ import annotations

import gate_premises
import pytest

pytestmark = pytest.mark.unit

PARAMETER = "EnableQuickStartWidget"
VARIABLE = "QUICK_START_ENABLED"
PARENT = gate_premises.load_template("template.yaml")
API_STACK = gate_premises.load_template("nested/api-resolvers/template.yaml")


def _variables(template: dict, function: str) -> dict:
    properties = template["Resources"][function]["Properties"]
    return properties["Environment"]["Variables"]


@pytest.mark.parametrize(
    ("template", "function"),
    [
        (PARENT, "AgentChatProcessorFunction"),
        (PARENT, "ChatStreamProcessorFunction"),
        (API_STACK, "AgentChatResolverFunction"),
    ],
    ids=["agent-chat-processor", "chat-stream-processor", "agent-chat-resolver"],
)
def test_each_quick_start_route_reads_the_stack_parameter(template, function):
    assert _variables(template, function).get(VARIABLE) == {"Fn::Ref": PARAMETER}
    assert PARAMETER in template["Parameters"]


def test_the_parent_passes_its_parameter_to_the_api_stack():
    parameters = PARENT["Resources"]["APIRESOLVERSTACK"]["Properties"]["Parameters"]
    assert parameters.get(PARAMETER) == {"Fn::Ref": PARAMETER}


def test_the_api_stack_parameter_matches_the_parents():
    parent, nested = PARENT["Parameters"][PARAMETER], API_STACK["Parameters"][PARAMETER]
    assert nested["AllowedValues"] == parent["AllowedValues"]
    assert nested["Default"] == parent["Default"]
