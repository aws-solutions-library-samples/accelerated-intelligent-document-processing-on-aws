# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp_common.chat_session_agents`` and the session flag it relies on.

The flag functions run against **moto**'s DynamoDB, which evaluates the
``#owner = :owner`` condition and ``if_not_exists`` with its own engine.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common import chat_session_agents as sa
from idp_common.chat_session_owner import (
    DATA_RESULTS_ATTRIBUTE,
    claim_chat_session,
    holds_data_results,
    record_data_results,
)

AGENTS = [
    {"agent_id": "Analytics-Agent", "opt_in": None},
    {"agent_id": "Error-Analyzer-Agent", "opt_in": None},
    {"agent_id": "Code-Intelligence-Agent", "opt_in": "code_intelligence"},
    {"agent_id": "remote-tools", "opt_in": "external_mcp"},
    {"agent_id": "Quick-Start-Agent", "opt_in": None},
]
OPT_IN = {"Code-Intelligence-Agent", "remote-tools"}


def _select(*, ci=False, mcp=False, holds=False):
    return set(
        sa.select_orchestrator_agent_ids(
            AGENTS,
            opt_ins=sa.requested_opt_ins(
                enable_code_intelligence=ci, enable_external_mcp_agents=mcp
            ),
            holds_data_results=holds,
        )
    )


@pytest.mark.unit
class TestSelection:
    def test_no_opt_in_means_no_opt_in_agent(self):
        assert _select() == {"Analytics-Agent", "Error-Analyzer-Agent"}

    def test_each_opt_in_admits_only_its_kind(self):
        assert _select(ci=True) & OPT_IN == {"Code-Intelligence-Agent"}
        assert _select(mcp=True) & OPT_IN == {"remote-tools"}

    def test_a_flagged_session_gets_no_opt_in_agent_whatever_was_asked(self):
        assert not _select(ci=True, mcp=True, holds=True) & OPT_IN
        assert _select(ci=True, mcp=True, holds=True) == {
            "Analytics-Agent",
            "Error-Analyzer-Agent",
        }

    def test_code_intelligence_is_opt_in_even_without_a_registry_marker(self):
        agents = [{"agent_id": "Code-Intelligence-Agent"}]
        assert (
            sa.select_orchestrator_agent_ids(
                agents, opt_ins=frozenset(), holds_data_results=False
            )
            == []
        )

    def test_quick_start_is_never_in_the_orchestrator(self):
        assert "Quick-Start-Agent" not in _select(ci=True, mcp=True)

    def test_needs_new_session_only_for_an_opt_in_on_a_flagged_session(self):
        assert sa.needs_new_session(holds_data_results=True, opt_ins={"external_mcp"})
        assert not sa.needs_new_session(holds_data_results=True, opt_ins=set())
        assert not sa.needs_new_session(
            holds_data_results=False, opt_ins={"code_intelligence"}
        )


@pytest.mark.unit
class TestGate:
    def test_first_data_result_records_once_and_closes_opt_in_agents(self):
        recorded = []
        gate = sa.SessionAgentGate(
            holds_data_results=False,
            opt_in_agent_ids=OPT_IN,
            record=lambda: recorded.append(1),
        )
        assert gate.allows("Code-Intelligence-Agent")
        gate.note_data_result("Error-Analyzer-Agent")
        gate.note_data_result("Analytics-Agent")
        assert recorded == [1]
        assert not gate.allows("Code-Intelligence-Agent")
        assert not gate.allows("remote-tools")
        assert gate.allows("Analytics-Agent")

    def test_other_agents_do_not_flag(self):
        recorded = []
        gate = sa.SessionAgentGate(
            holds_data_results=False,
            opt_in_agent_ids=OPT_IN,
            record=lambda: recorded.append(1),
        )
        gate.note_data_result("Code-Intelligence-Agent")
        assert recorded == []
        assert gate.allows("Code-Intelligence-Agent")

    def test_a_failed_record_raises_and_still_closes_the_turn(self):
        def fail():
            raise RuntimeError("write failed")

        gate = sa.SessionAgentGate(
            holds_data_results=False, opt_in_agent_ids=OPT_IN, record=fail
        )
        with pytest.raises(RuntimeError):
            gate.note_data_result("Analytics-Agent")
        assert not gate.allows("Code-Intelligence-Agent")

    def test_an_already_flagged_session_records_nothing_again(self):
        recorded = []
        gate = sa.SessionAgentGate(
            holds_data_results=True,
            opt_in_agent_ids=OPT_IN,
            record=lambda: recorded.append(1),
        )
        gate.note_data_result("Analytics-Agent")
        assert recorded == []


@pytest.fixture
def owners():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        yield ddb.create_table(
            TableName="owners",
            KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "sessionId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


def _own(owners, sid, sub):
    assert claim_chat_session(
        owners,
        sid,
        sub,
        expires_after=100,
        allow_new=True,
        has_stored_messages=lambda: False,
        holds_earlier_record=lambda: False,
    )


@pytest.mark.unit
class TestFlag:
    def test_unflagged_and_unknown_sessions_hold_nothing(self, owners):
        _own(owners, "s1", "sub-a")
        assert not holds_data_results(owners, "s1")
        assert not holds_data_results(owners, "nobody")

    def test_the_owner_flags_their_session_and_the_first_time_is_kept(self, owners):
        _own(owners, "s1", "sub-a")
        assert record_data_results(owners, "s1", "sub-a", 10)
        assert record_data_results(owners, "s1", "sub-a", 20)
        assert holds_data_results(owners, "s1")
        item = owners.get_item(Key={"sessionId": "s1"})["Item"]
        assert item[DATA_RESULTS_ATTRIBUTE] == 10

    def test_nobody_else_can_flag_it_and_no_row_is_created(self, owners):
        _own(owners, "s1", "sub-a")
        assert not record_data_results(owners, "s1", "sub-b", 10)
        assert not record_data_results(owners, "unowned", "sub-b", 10)
        assert not holds_data_results(owners, "s1")
        assert "Item" not in owners.get_item(Key={"sessionId": "unowned"})

    def test_extending_ownership_keeps_the_flag(self, owners):
        _own(owners, "s1", "sub-a")
        record_data_results(owners, "s1", "sub-a", 10)
        _own(owners, "s1", "sub-a")
        assert holds_data_results(owners, "s1")
