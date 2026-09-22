# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for `DynamoDBMemoryHookProvider`, the multi-turn conversation memory for
the agent chat system.

Three properties carry the weight here, and all three are quiet when they break.

**Chronological order.** History is queried newest-first (`ScanIndexForward=False`)
and then re-reversed to read oldest-first. Get that wrong and the agent is handed a
conversation running backwards — which it will answer coherently, because a reversed
transcript still reads as a conversation. This is the same class of defect as
[#1081](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1081)
in the Step Functions tool, so the ordering is asserted directly rather than
inferred from a round trip.

**Item splitting at the 400KB DynamoDB limit.** `_store_message_to_dynamodb`
speculatively builds the item it is about to write, measures it, and starts a new
item if it would exceed `max_item_size_kb`. Both sides of that boundary are
exercised with a real size threshold, because the failure mode on the wrong side is
a `ValidationException` that loses the message.

**Turn grouping.** A turn starts at a `user` message and absorbs everything after
it, and `max_history_turns` truncates from the end. Truncating from the wrong end
would hand the agent the oldest turns and drop the ones it needs.

**Appending without losing a concurrent append.** Adding a message rewrites the
whole newest item, so two writers serving one session — a resubmitted request, or a
retried Lambda — would each drop the other's message. The write is conditional on a
version attribute read alongside the item, and `TestConcurrentAppendGuard` drives
that mechanism by injecting the conflict rather than racing for it.

`boto3.resource` is stubbed throughout. Nothing here reaches AWS.
"""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import boto3.dynamodb.conditions  # noqa: F401
import botocore.exceptions
import pytest

from idp_common.agents.utils.memory_provider import DynamoDBMemoryHookProvider

# The import above is load-bearing. `memory_provider` builds its key conditions as
# `boto3.dynamodb.conditions.Key(...)`, and `boto3.dynamodb` is only populated as a
# side effect of a real `boto3.resource("dynamodb")` call. These tests patch
# `boto3.resource`, so that side effect never happens and the attribute lookup fails
# with `module 'boto3' has no attribute 'dynamodb'` — importing the submodule here
# restores it.

MODULE = "idp_common.agents.utils.memory_provider"


def _provider(**overrides: Any):
    """A provider with a stubbed table. Returns (provider, table)."""
    table = MagicMock()
    with patch(f"{MODULE}.boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        kwargs = {"table_name": "memory", "session_id": "sess-1"}
        kwargs.update(overrides)
        provider = DynamoDBMemoryHookProvider(**kwargs)
    return provider, table


def _item(sk: str, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    item = {
        "PK": "conversation#sess-1",
        "SK": sk,
        "conversation_history": json.dumps(messages),
        "session_id": "sess-1",
        "message_count": len(messages),
    }
    item.update(extra)
    return item


def _message(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": {"text": text}, "timestamp": "t"}


def _client_error(code: str):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "m"}}, "Query"
    )


def _written_item(table: MagicMock) -> dict[str, Any]:
    return table.put_item.call_args.kwargs["Item"]


def _written_messages(table: MagicMock) -> list[dict[str, Any]]:
    return json.loads(_written_item(table)["conversation_history"])


@pytest.mark.unit
class TestConstructionAndKeys:
    """__init__, the partition key and the size helper."""

    def test_the_table_is_bound_in_the_requested_region(self):
        with patch(f"{MODULE}.boto3.resource") as resource:
            DynamoDBMemoryHookProvider(
                table_name="memory", session_id="s", region_name="eu-west-1"
            )
        assert resource.call_args.kwargs["region_name"] == "eu-west-1"
        resource.return_value.Table.assert_called_once_with("memory")

    def test_the_defaults_are_the_documented_ones(self):
        provider, _ = _provider()
        assert provider.max_message_size_kb == 8.5
        assert provider.max_history_turns == 20
        assert provider.max_item_size_kb == 350.0

    def test_the_partition_key_namespaces_the_session(self):
        # Every read and write is scoped by this, so a key that did not include the
        # session id would leak one user's conversation into another's.
        provider, _ = _provider(session_id="abc")
        assert provider._get_conversation_pk() == "conversation#abc"

    def test_the_sort_key_is_an_iso_timestamp(self):
        provider, _ = _provider()
        sk = provider._generate_timestamp_sk()
        assert sk.count("-") >= 2 and "T" in sk

    def test_successive_sort_keys_sort_chronologically_as_strings(self):
        # The sort key is compared lexicographically by DynamoDB, and ISO-8601 is
        # chosen precisely because that ordering matches time order.
        provider, _ = _provider()
        first = provider._generate_timestamp_sk()
        second = provider._generate_timestamp_sk()
        assert first <= second

    def test_the_size_helper_measures_encoded_json_bytes(self):
        provider, _ = _provider()
        assert provider._get_item_size_bytes({"a": "b"}) == len(b'{"a":"b"}')

    def test_the_size_helper_counts_multibyte_characters_by_byte(self):
        # DynamoDB's limit is in bytes, so measuring characters would under-count a
        # conversation containing non-ASCII text and write an oversized item.
        provider, _ = _provider()
        assert provider._get_item_size_bytes(
            {"a": "é"}
        ) > provider._get_item_size_bytes({"a": "e"})


@pytest.mark.unit
class TestLatestConversationItem:
    """_get_latest_conversation_item: newest-first, one item."""

    def test_the_query_asks_for_the_newest_item_only(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        provider._get_latest_conversation_item()
        kwargs = table.query.call_args.kwargs
        assert kwargs["ScanIndexForward"] is False
        assert kwargs["Limit"] == 1

    def test_the_newest_item_is_returned(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t2", [])]}
        assert provider._get_latest_conversation_item()["SK"] == "t2"

    def test_no_items_yields_nothing(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        assert provider._get_latest_conversation_item() is None

    def test_a_query_failure_yields_nothing_rather_than_propagating(self):
        # The caller treats None as "no history yet" and creates a first item, so a
        # transient query failure costs the earlier history rather than the message.
        provider, table = _provider()
        table.query.side_effect = _client_error(
            "ProvisionedThroughputExceededException"
        )
        assert provider._get_latest_conversation_item() is None


@pytest.mark.unit
class TestConcurrentAppendGuard:
    """The version attribute that stops one append from overwriting another.

    The conflict is injected, not raced for, so every assertion here is as
    deterministic as the rest of the module: no threads, no sleeps, no dependence
    on the scheduler.
    """

    def _put_kwargs(self, table: MagicMock) -> dict[str, Any]:
        return table.put_item.call_args.kwargs

    def test_an_append_is_conditional_on_the_version_it_read(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [_item("t1", [_message("user", "a")], conversation_version=4)]
        }
        provider._store_message_to_dynamodb({"text": "b"}, "assistant")
        kwargs = self._put_kwargs(table)
        assert (
            "conversation_version = :expected_version" in kwargs["ConditionExpression"]
        )
        assert kwargs["ExpressionAttributeValues"][":expected_version"] == 4

    def test_an_append_advances_the_stored_version(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [_item("t1", [_message("user", "a")], conversation_version=4)]
        }
        provider._store_message_to_dynamodb({"text": "b"}, "assistant")
        assert _written_item(table)["conversation_version"] == 5

    def test_an_item_written_before_the_guard_existed_is_still_appendable(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t1", [_message("user", "a")])]}
        provider._store_message_to_dynamodb({"text": "b"}, "assistant")
        kwargs = self._put_kwargs(table)
        assert (
            "attribute_not_exists(conversation_version)"
            in kwargs["ConditionExpression"]
        )
        assert len(_written_messages(table)) == 2

    def test_a_newly_created_item_carries_the_guard_from_the_start(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        provider._store_message_to_dynamodb({"text": "hello"}, "user")
        assert _written_item(table)["conversation_version"] == 1

    def test_a_rejected_append_is_retried_and_both_messages_survive(self):
        # The point of the retry: the other writer's message has to be picked up
        # by the re-read, not overwritten by the array the first attempt built.
        provider, table = _provider()
        table.query.side_effect = [
            {"Items": [_item("t1", [_message("user", "a")])]},
            {
                "Items": [
                    _item(
                        "t1",
                        [_message("user", "a"), _message("assistant", "other-writer")],
                        conversation_version=1,
                    )
                ]
            },
        ]
        table.put_item.side_effect = [
            _client_error("ConditionalCheckFailedException"),
            None,
        ]
        provider._store_message_to_dynamodb({"text": "mine"}, "assistant")
        assert table.query.call_count == 2
        stored = _written_messages(table)
        assert len(stored) == 3
        assert stored[-1]["content"] == {"text": "mine"}
        assert any(m["content"] == {"text": "other-writer"} for m in stored)

    def test_the_retry_is_bounded_rather_than_looping_forever(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t1", [_message("user", "a")])]}
        table.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        provider._store_message_to_dynamodb({"text": "b"}, "assistant")
        assert table.put_item.call_count == 10

    def test_exhausting_the_retries_is_reported(self, caplog):
        # Dropping one message beats overwriting the conversation, but it must not
        # be silent -- silence is what makes this class of defect invisible.
        import logging

        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t1", [_message("user", "a")])]}
        table.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        with caplog.at_level(logging.ERROR):
            provider._store_message_to_dynamodb({"text": "b"}, "assistant")
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("sess-1" in m and "not persisted" in m for m in errors)

    def test_a_non_conflict_write_error_is_not_retried(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t1", [_message("user", "a")])]}
        table.put_item.side_effect = _client_error("ValidationException")
        provider._store_message_to_dynamodb({"text": "b"}, "assistant")
        assert table.put_item.call_count == 1

    def test_rolling_over_to_a_new_item_needs_no_guard(self):
        # The rollover write lands on a fresh sort key, so it cannot overwrite the
        # item it rolled over from and must not be rejected by a stale version.
        provider, table = _provider(max_item_size_kb=0.1)
        table.query.return_value = {
            "Items": [_item("t1", [_message("user", "x" * 500)])]
        }
        provider._store_message_to_dynamodb({"text": "next"}, "assistant")
        assert "ConditionExpression" not in self._put_kwargs(table)
        assert len(_written_messages(table)) == 1


@pytest.mark.unit
class TestStoreMessage:
    """_store_message_to_dynamodb: append, or split at the size limit."""

    def test_a_first_message_creates_the_first_item(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        provider._store_message_to_dynamodb({"text": "hello"}, "user")
        item = _written_item(table)
        assert item["PK"] == "conversation#sess-1"
        assert item["message_count"] == 1
        assert _written_messages(table)[0]["role"] == "user"

    def test_a_stored_message_carries_role_content_and_a_sequence_number(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        provider._store_message_to_dynamodb({"text": "hello"}, "assistant")
        entry = _written_messages(table)[0]
        assert entry["role"] == "assistant"
        assert entry["content"] == {"text": "hello"}
        assert entry["timestamp"]
        assert isinstance(entry["sequence_number"], int)

    def test_a_later_message_is_appended_to_the_existing_item(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t1", [_message("user", "first")])]}
        provider._store_message_to_dynamodb({"text": "second"}, "assistant")
        written = _written_messages(table)
        assert len(written) == 2
        assert written[0]["content"]["text"] == "first"
        assert written[1]["role"] == "assistant"

    def test_an_append_reuses_the_existing_sort_key(self):
        # Writing under a new sort key would leave the old item in place and
        # duplicate every earlier message on the next read.
        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t1", [_message("user", "a")])]}
        provider._store_message_to_dynamodb({"text": "b"}, "assistant")
        assert _written_item(table)["SK"] == "t1"

    def test_a_message_that_would_exceed_the_limit_starts_a_new_item(self):
        # 400KB is a hard DynamoDB limit; exceeding it raises ValidationException and
        # the message is lost, so the split has to happen on the near side.
        provider, table = _provider(max_item_size_kb=1.0)
        big = [_message("user", "x" * 2000)]
        table.query.return_value = {"Items": [_item("t1", big)]}
        provider._store_message_to_dynamodb({"text": "next"}, "assistant")
        item = _written_item(table)
        assert item["SK"] != "t1"
        assert item["message_count"] == 1
        assert _written_messages(table)[0]["content"] == {"text": "next"}

    def test_a_message_that_fits_does_not_start_a_new_item(self):
        provider, table = _provider(max_item_size_kb=350.0)
        table.query.return_value = {"Items": [_item("t1", [_message("user", "small")])]}
        provider._store_message_to_dynamodb({"text": "also small"}, "assistant")
        assert _written_item(table)["SK"] == "t1"
        assert len(_written_messages(table)) == 2

    def test_the_configured_limit_is_honoured_rather_than_a_hardcoded_one(self):
        provider, table = _provider(max_item_size_kb=0.05)
        table.query.return_value = {"Items": [_item("t1", [_message("user", "a")])]}
        provider._store_message_to_dynamodb({"text": "b"}, "assistant")
        assert _written_item(table)["SK"] != "t1"

    def test_malformed_existing_history_starts_fresh_rather_than_raising(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [{"PK": "p", "SK": "t1", "conversation_history": "not json"}]
        }
        provider._store_message_to_dynamodb({"text": "hello"}, "user")
        assert len(_written_messages(table)) == 1

    def test_a_json_scalar_where_an_array_was_expected_starts_fresh(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [
                {"PK": "p", "SK": "t1", "conversation_history": json.dumps({"a": 1})}
            ]
        }
        provider._store_message_to_dynamodb({"text": "hello"}, "user")
        assert len(_written_messages(table)) == 1

    def test_an_item_with_no_history_attribute_starts_fresh(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [{"PK": "p", "SK": "t1"}]}
        provider._store_message_to_dynamodb({"text": "hello"}, "user")
        assert len(_written_messages(table)) == 1

    def test_a_write_failure_does_not_propagate(self):
        # Memory is best-effort: losing a turn of history is acceptable, aborting
        # the user's chat request is not.
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        table.put_item.side_effect = _client_error("ValidationException")
        provider._store_message_to_dynamodb({"text": "hello"}, "user")

    def test_an_unexpected_failure_does_not_propagate(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        table.put_item.side_effect = RuntimeError("boom")
        provider._store_message_to_dynamodb({"text": "hello"}, "user")


@pytest.mark.unit
class TestLoadConversationHistory:
    """_load_conversation_history: ordering, turn grouping, truncation."""

    def test_no_items_gives_no_turns(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        assert provider._load_conversation_history() == []

    def test_the_query_asks_newest_first_with_a_page_of_items(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        provider._load_conversation_history()
        kwargs = table.query.call_args.kwargs
        assert kwargs["ScanIndexForward"] is False
        assert kwargs["Limit"] == 10

    def test_items_are_re_reversed_so_messages_read_oldest_first(self):
        # The query returns newest-first; the messages handed to the agent must be
        # oldest-first or the conversation is replayed backwards.
        provider, table = _provider()
        table.query.return_value = {
            "Items": [
                _item("t2", [_message("user", "second")]),
                _item("t1", [_message("user", "first")]),
            ]
        }
        turns = provider._load_conversation_history()
        assert [turn[0]["content"]["text"] for turn in turns] == ["first", "second"]

    def test_a_turn_starts_at_a_user_message_and_absorbs_what_follows(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [
                _item(
                    "t1",
                    [
                        _message("user", "q1"),
                        _message("assistant", "a1"),
                        _message("tool", "t1"),
                        _message("user", "q2"),
                        _message("assistant", "a2"),
                    ],
                )
            ]
        }
        turns = provider._load_conversation_history()
        assert len(turns) == 2
        assert len(turns[0]) == 3
        assert turns[1][0]["content"]["text"] == "q2"

    def test_an_assistant_message_with_no_preceding_user_message_still_forms_a_turn(
        self,
    ):
        # Dropping it would silently lose the opening of a conversation that began
        # with a system or assistant message.
        provider, table = _provider()
        table.query.return_value = {
            "Items": [_item("t1", [_message("assistant", "unprompted")])]
        }
        turns = provider._load_conversation_history()
        assert len(turns) == 1
        assert turns[0][0]["role"] == "assistant"

    def test_a_message_with_an_unrecognised_role_is_dropped(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [_item("t1", [_message("user", "q"), _message("banana", "?")])]
        }
        turns = provider._load_conversation_history()
        assert len(turns[0]) == 1

    def test_only_the_most_recent_turns_are_kept(self):
        # Truncating from the wrong end would hand the agent the beginning of a long
        # conversation and drop what the user just said.
        provider, table = _provider(max_history_turns=2)
        messages = []
        for i in range(5):
            messages.append(_message("user", f"q{i}"))
            messages.append(_message("assistant", f"a{i}"))
        table.query.return_value = {"Items": [_item("t1", messages)]}
        turns = provider._load_conversation_history()
        assert len(turns) == 2
        assert [turn[0]["content"]["text"] for turn in turns] == ["q3", "q4"]

    def test_fewer_turns_than_the_limit_are_all_returned(self):
        provider, table = _provider(max_history_turns=20)
        table.query.return_value = {"Items": [_item("t1", [_message("user", "q")])]}
        assert len(provider._load_conversation_history()) == 1

    def test_one_malformed_item_is_skipped_and_the_others_read(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [
                {"PK": "p", "SK": "t2", "conversation_history": "not json"},
                _item("t1", [_message("user", "survived")]),
            ]
        }
        turns = provider._load_conversation_history()
        assert len(turns) == 1
        assert turns[0][0]["content"]["text"] == "survived"

    def test_a_missing_table_gives_no_turns_rather_than_raising(self):
        provider, table = _provider()
        table.query.side_effect = _client_error("ResourceNotFoundException")
        assert provider._load_conversation_history() == []

    def test_another_dynamodb_error_gives_no_turns(self):
        provider, table = _provider()
        table.query.side_effect = _client_error(
            "ProvisionedThroughputExceededException"
        )
        assert provider._load_conversation_history() == []

    def test_an_unexpected_error_gives_no_turns(self):
        provider, table = _provider()
        table.query.side_effect = RuntimeError("boom")
        assert provider._load_conversation_history() == []


@pytest.mark.unit
class TestOnAgentInitialized:
    """on_agent_initialized: history becomes system-prompt context."""

    def _agent_event(self, system_prompt):
        event = MagicMock()
        event.agent.system_prompt = system_prompt
        return event

    def test_history_is_appended_to_an_existing_system_prompt(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t1", [_message("user", "hello")])]}
        event = self._agent_event("You are helpful.")
        provider.on_agent_initialized(event)
        assert event.agent.system_prompt.startswith("You are helpful.")
        assert "hello" in event.agent.system_prompt

    def test_history_becomes_the_system_prompt_when_there_was_none(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [_item("t1", [_message("user", "hello")])]}
        event = self._agent_event(None)
        provider.on_agent_initialized(event)
        assert event.agent.system_prompt.startswith("Recent conversation:")

    def test_each_message_is_rendered_with_its_role(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [
                _item(
                    "t1",
                    [_message("user", "question"), _message("assistant", "answer")],
                )
            ]
        }
        event = self._agent_event(None)
        provider.on_agent_initialized(event)
        assert "user: question" in event.agent.system_prompt
        assert "assistant: answer" in event.agent.system_prompt

    def test_a_non_dict_content_is_stringified_rather_than_dropped(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [_item("t1", [{"role": "user", "content": [{"text": "listy"}]}])]
        }
        event = self._agent_event(None)
        provider.on_agent_initialized(event)
        assert "listy" in event.agent.system_prompt

    def test_no_history_leaves_the_system_prompt_untouched(self):
        # Appending an empty "Recent conversation:" heading would tell the model
        # there was a prior conversation with no content in it.
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        event = self._agent_event("You are helpful.")
        provider.on_agent_initialized(event)
        assert event.agent.system_prompt == "You are helpful."

    def test_a_load_failure_leaves_the_agent_usable(self):
        provider, table = _provider()
        table.query.side_effect = RuntimeError("boom")
        event = self._agent_event("You are helpful.")
        provider.on_agent_initialized(event)
        assert event.agent.system_prompt == "You are helpful."


@pytest.mark.unit
class TestOnMessageAdded:
    """on_message_added: size check, truncation, and the store call."""

    def _event(self, messages):
        event = MagicMock()
        event.agent.messages = messages
        return event

    def test_the_last_message_is_the_one_stored(self):
        provider, _ = _provider()
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(
                self._event(
                    [
                        {"role": "user", "content": [{"text": "old"}]},
                        {"role": "assistant", "content": [{"text": "new"}]},
                    ]
                )
            )
        content, role = store.call_args.args
        assert role == "assistant"
        assert content == [{"text": "new"}]

    def test_a_small_message_is_stored_unaltered(self):
        provider, _ = _provider(max_message_size_kb=8.5)
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(
                self._event([{"role": "user", "content": [{"text": "hi"}]}])
            )
        assert store.call_args.args[0] == [{"text": "hi"}]

    def test_an_oversized_message_is_truncated_rather_than_dropped(self):
        # A 400KB document pasted into chat must not silently vanish from the
        # transcript; the head is kept so the turn is still recognisable.
        provider, _ = _provider(max_message_size_kb=0.5)
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(
                self._event([{"role": "user", "content": [{"text": "x" * 5000}]}])
            )
        stored = store.call_args.args[0]
        assert "too large" in stored[0]["text"]
        assert len(stored[0]["text"]) < 5000

    def test_the_truncated_head_is_bounded(self):
        provider, _ = _provider(max_message_size_kb=0.1)
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(
                self._event([{"role": "user", "content": [{"text": "y" * 10000}]}])
            )
        preserved = store.call_args.args[0][0]["text"]
        assert len(preserved) < 700
        # Bounded from below too: the sibling test's rationale is that "the head is
        # kept so the turn is still recognisable", and only an upper bound would let
        # the head shrink to nothing while the test stayed green.
        assert len(preserved) > 400

    def test_a_message_exactly_at_the_size_limit_is_not_truncated(self):
        # Strictly greater than, so the limit itself is allowed through. Pinned
        # because flipping it to >= would truncate a message that fits.
        provider, _ = _provider(max_message_size_kb=1.0)
        exact = "y" * (1024 - len('[{"text": ""}]'))
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(
                self._event([{"role": "user", "content": [{"text": exact}]}])
            )
        assert "too large" not in str(store.call_args.args[0])

    def test_the_role_survives_truncation(self):
        provider, _ = _provider(max_message_size_kb=0.1)
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(
                self._event([{"role": "assistant", "content": [{"text": "z" * 5000}]}])
            )
        assert store.call_args.args[1] == "assistant"

    def test_a_store_failure_on_the_normal_path_does_not_propagate(self):
        provider, _ = _provider()
        with patch.object(
            provider, "_store_message_to_dynamodb", side_effect=RuntimeError("boom")
        ):
            provider.on_message_added(
                self._event([{"role": "user", "content": [{"text": "hi"}]}])
            )

    def test_a_store_failure_on_the_truncated_path_does_not_propagate(self):
        provider, _ = _provider(max_message_size_kb=0.1)
        with patch.object(
            provider, "_store_message_to_dynamodb", side_effect=RuntimeError("boom")
        ):
            provider.on_message_added(
                self._event([{"role": "user", "content": [{"text": "x" * 5000}]}])
            )

    def test_a_message_with_no_content_key_is_stored_as_empty(self):
        provider, _ = _provider()
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(self._event([{"role": "user"}]))
        assert store.call_args.args[0] == ""

    def test_a_message_with_no_role_is_dropped_rather_than_aborting_the_turn(self):
        # Memory is best-effort: every other failure in this module is logged and
        # swallowed, because losing a turn of history is acceptable and aborting
        # the user's chat turn is not. A message with no role is unusable — the
        # role is what turn grouping keys on when the history is read back — so it
        # is dropped, not stored under a substituted role.
        provider, _ = _provider()
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(self._event([{"content": [{"text": "hi"}]}]))
        store.assert_not_called()

    def test_an_empty_message_list_does_not_abort_the_turn(self):
        # messages[-1] on an empty list raises IndexError by the same route as the
        # missing role, so it is covered by the same guard.
        provider, _ = _provider()
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(self._event([]))
        store.assert_not_called()

    def test_content_that_cannot_be_serialised_does_not_abort_the_turn(self):
        # The size check serialises the content with json.dumps, which raises
        # TypeError on anything json does not know. That read sits on the same
        # lines as the two above and needs the same guard.
        provider, _ = _provider()
        unserialisable = [{"text": object()}]
        with patch.object(provider, "_store_message_to_dynamodb") as store:
            provider.on_message_added(
                self._event([{"role": "user", "content": unserialisable}])
            )
        store.assert_not_called()

    def test_a_malformed_message_is_logged_at_error_level(self):
        # Dropping it silently would make a lost turn of history undiagnosable.
        provider, _ = _provider()
        with patch(f"{MODULE}.logger") as log:
            provider.on_message_added(self._event([{"content": [{"text": "hi"}]}]))
        assert log.error.called


@pytest.mark.unit
class TestHousekeeping:
    """register_hooks, clear_conversation_history, get_conversation_stats."""

    def test_both_hooks_are_registered(self):
        provider, _ = _provider()
        registry = MagicMock()
        provider.register_hooks(registry)
        assert registry.add_callback.call_count == 2

    def test_clearing_deletes_every_returned_item(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [
                {"PK": "conversation#sess-1", "SK": "t1"},
                {"PK": "conversation#sess-1", "SK": "t2"},
            ]
        }
        batch = table.batch_writer.return_value.__enter__.return_value
        assert provider.clear_conversation_history() is True
        assert batch.delete_item.call_count == 2

    def test_clearing_projects_only_the_keys_it_needs(self):
        # The history attribute can be 350KB per item; reading it back to delete it
        # would spend read capacity on data that is about to be discarded.
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        provider.clear_conversation_history()
        assert table.query.call_args.kwargs["ProjectionExpression"] == "PK, SK"

    def test_clearing_an_empty_conversation_succeeds(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        assert provider.clear_conversation_history() is True

    def test_a_clear_failure_reports_false(self):
        provider, table = _provider()
        table.query.side_effect = RuntimeError("boom")
        assert provider.clear_conversation_history() is False

    def test_clearing_does_not_paginate_and_reports_success_regardless(self):
        # The query is unpaginated: only the first 1MB page of keys is deleted, and
        # True is returned either way. With key-only projection a page holds many
        # thousands of keys, so this is unreachable for any realistic conversation
        # -- but "cleared" is reported without having verified it, which is why the
        # behaviour is pinned rather than assumed.
        provider, table = _provider()
        table.query.return_value = {
            "Items": [{"PK": "conversation#sess-1", "SK": "t1"}],
            "LastEvaluatedKey": {"PK": "conversation#sess-1", "SK": "t1"},
        }
        batch = table.batch_writer.return_value.__enter__.return_value
        assert provider.clear_conversation_history() is True
        assert batch.delete_item.call_count == 1

    def test_stats_report_item_and_message_counts(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [
                _item("t1", [], message_count=3, last_updated="2026-01-01T00:00:00"),
                _item("t2", [], message_count=4, last_updated="2026-01-02T00:00:00"),
            ]
        }
        stats = provider.get_conversation_stats()
        assert stats["session_id"] == "sess-1"
        assert stats["item_count"] == 2
        assert stats["total_message_count"] == 7
        assert stats["exists"] is True

    def test_stats_report_the_latest_update_across_items(self):
        provider, table = _provider()
        table.query.return_value = {
            "Items": [
                _item("t2", [], message_count=1, last_updated="2026-01-02T00:00:00"),
                _item("t1", [], message_count=1, last_updated="2026-01-01T00:00:00"),
            ]
        }
        assert (
            provider.get_conversation_stats()["last_updated"] == "2026-01-02T00:00:00"
        )

    def test_stats_for_a_conversation_that_does_not_exist(self):
        provider, table = _provider()
        table.query.return_value = {"Items": []}
        stats = provider.get_conversation_stats()
        assert stats["exists"] is False
        assert stats["item_count"] == 0
        assert stats["total_message_count"] == 0

    def test_an_item_with_no_message_count_contributes_zero(self):
        provider, table = _provider()
        table.query.return_value = {"Items": [{"PK": "p", "SK": "t1"}]}
        assert provider.get_conversation_stats()["total_message_count"] == 0

    def test_a_stats_failure_reports_the_error_and_not_existing(self):
        # `exists: False` on an error is a deliberate fail-closed: a caller deciding
        # whether to show a "resume conversation" affordance should not offer one it
        # could not confirm.
        provider, table = _provider()
        table.query.side_effect = RuntimeError("boom")
        stats = provider.get_conversation_stats()
        assert stats["exists"] is False
        assert "boom" in stats["error"]
