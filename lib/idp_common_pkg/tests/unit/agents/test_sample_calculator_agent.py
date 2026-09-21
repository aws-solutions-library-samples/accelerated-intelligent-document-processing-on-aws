# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the sample calculator agent.

This agent is a developer reference rather than a production path: its
registration in `agents/factory/registry.py` is commented out on purpose. It is
tested anyway because a reference that does not construct is worse than no
reference — the first thing anyone does with it is copy it, and a broken copy
teaches the wrong shape.

What is asserted is construction, not behaviour: that it reads the declared
default model id rather than a literal of its own (the class of defect that
[#1049] was about, where seven agent modules each carried a private copy of a
retired model id), that the caller's boto3 session is the one the model is built
with, and that the calculator tool is attached. No Bedrock call is made.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.agents.common.config import DEFAULT_AGENT_MODEL_ID
from idp_common.agents.sample_calculator.agent import create_sample_calculator_agent


@pytest.mark.unit
class TestCreateSampleCalculatorAgent:
    def _build(self, session=None):
        session = session or MagicMock(name="boto3-session")
        with (
            patch(
                "idp_common.agents.sample_calculator.agent.create_strands_bedrock_model"
            ) as make_model,
            patch(
                "idp_common.agents.sample_calculator.agent.strands.Agent"
            ) as agent_cls,
        ):
            result = create_sample_calculator_agent(session)
        return result, make_model, agent_cls, session

    def test_it_returns_the_constructed_agent(self):
        result, _, agent_cls, _ = self._build()
        assert result is agent_cls.return_value

    def test_the_model_comes_from_the_declared_default_not_a_local_literal(self):
        # A literal here would be a model id no gate can see, which is how a
        # retired model survived in seven fallback paths.
        _, make_model, _, _ = self._build()
        assert make_model.call_args.kwargs["model_id"] == DEFAULT_AGENT_MODEL_ID

    def test_the_callers_session_is_used_to_build_the_model(self):
        # The session carries the region and credentials, so building the model
        # from anything else would silently target another account.
        session = MagicMock(name="caller-session")
        _, make_model, _, _ = self._build(session)
        assert make_model.call_args.kwargs["session"] is session

    def test_the_calculator_tool_is_attached(self):
        from strands_tools import calculator

        _, _, agent_cls, _ = self._build()
        assert agent_cls.call_args.kwargs["tools"] == [calculator]

    def test_extra_keyword_arguments_are_accepted_and_ignored(self):
        # The signature takes **kwargs to match the factory's calling convention;
        # passing one must not reach strands.Agent as an unexpected argument.
        with (
            patch(
                "idp_common.agents.sample_calculator.agent.create_strands_bedrock_model"
            ),
            patch(
                "idp_common.agents.sample_calculator.agent.strands.Agent"
            ) as agent_cls,
        ):
            create_sample_calculator_agent(MagicMock(), config={"unused": True})
        assert set(agent_cls.call_args.kwargs) == {"model", "tools"}
