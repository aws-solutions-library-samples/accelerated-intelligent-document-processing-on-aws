# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB Memory Hook Provider for Agent System

This module provides memory persistence for conversational agents using DynamoDB.
Copied and adapted from code_intel system for use with the agent chat system.

Key Features:
- Stores conversation history in DynamoDB for multi-turn conversations
- Automatically loads recent conversation context when agent initializes
- Handles large conversations by splitting into multiple DynamoDB items
- Groups messages into turns for efficient context management
- Supports message size limits and truncation

Usage:
    from idp_common.agents.utils.memory_provider import DynamoDBMemoryHookProvider

    memory_provider = DynamoDBMemoryHookProvider(
        table_name="IdpHelperChatMemoryTable",
        session_id="user-session-123",
        region_name="us-west-2"
    )

    # Add to agent hooks
    agent.hooks.add_hook(memory_provider)
"""

import json
import logging
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError
from strands.hooks import (
    AgentInitializedEvent,
    HookProvider,
    HookRegistry,
    MessageAddedEvent,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Guards the newest conversation item against a concurrent append. Appending
# rewrites the whole item, so two writers that both read it would each drop the
# other's message; each write is conditional on the version it read instead.
# Absent on items written before the guard existed; the first append adds it.
_VERSION_ATTRIBUTE = "conversation_version"

# Every conflict round has at least one winner, so W concurrent writers need at
# most W attempts between them. Each retry re-queries, which is a round trip, so
# the loop needs no backoff to pace itself.
_MAX_APPEND_ATTEMPTS = 10


def _coerce_int(value: Any, default: int = 0) -> int:
    """DynamoDB returns numbers as Decimal; normalize to int for comparison."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DynamoDBMemoryHookProvider(HookProvider):
    """
    DynamoDB-based memory hook provider for conversational agents.

    This provider stores and retrieves conversation history from DynamoDB,
    enabling multi-turn conversations with persistent memory across sessions.

    Storage Strategy:
    - Stores messages in JSON arrays within DynamoDB items
    - Creates new items when approaching 400KB DynamoDB limit (uses 350KB threshold)
    - Uses timestamp as sort key for automatic chronological ordering
    - Efficiently retrieves latest messages using DynamoDB Query with reverse sort

    Memory Loading:
    - Automatically loads recent conversation history when agent initializes
    - Groups messages into turns (user message + assistant responses)
    - Limits history to max_history_turns to control context size
    - Adds conversation context to agent's system prompt

    Message Storage:
    - Stores each message as it's added to the conversation
    - Handles message size limits with truncation
    - Tracks message count and timestamps for debugging
    """

    def __init__(
        self,
        table_name: str,
        session_id: str,
        region_name: str = "us-west-2",
        max_message_size_kb: float = 8.5,
        max_history_turns: int = 20,
        max_item_size_kb: float = 350.0,  # 50KB buffer below 400KB DynamoDB limit
    ):
        """
        Initialize the DynamoDBMemoryHookProvider for agent system.

        Args:
            table_name: Name of the DynamoDB table to store conversations
            session_id: The session ID for this conversation
            region_name: AWS region name for DynamoDB (defaults to us-west-2)
            max_message_size_kb: Maximum message size in KB before truncation
            max_history_turns: Maximum number of conversation turns to load on initialization
            max_item_size_kb: Maximum item size in KB before creating new item (default 350KB)
        """
        self.table_name = table_name
        self.session_id = session_id
        self.max_message_size_kb = max_message_size_kb
        self.max_history_turns = max_history_turns
        self.max_item_size_kb = max_item_size_kb

        # Initialize DynamoDB client
        self.dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self.table = self.dynamodb.Table(table_name)

        logger.info(
            f"Agent Memory Provider initialized for table: {table_name}, session: {session_id}"
        )

    def _get_conversation_pk(self) -> str:
        """
        Generate DynamoDB partition key for the conversation.

        Returns:
            Partition key string in format: conversation#{session_id}
        """
        return f"conversation#{self.session_id}"

    def _generate_timestamp_sk(self) -> str:
        """
        Generate timestamp-based sort key for chronological ordering.

        Returns:
            Timestamp string in ISO format with microsecond precision
        """
        return datetime.now().isoformat()

    def _get_item_size_bytes(self, item_data: Dict[str, Any]) -> int:
        """
        Calculate the approximate size of a DynamoDB item in bytes.

        Args:
            item_data: The item data dictionary

        Returns:
            Approximate size in bytes
        """
        # Convert to JSON and measure size
        json_str = json.dumps(item_data, separators=(",", ":"))
        return len(json_str.encode("utf-8"))

    def _get_latest_conversation_item(self) -> Optional[Dict[str, Any]]:
        """
        Get the latest conversation item (most recent timestamp).

        Returns:
            Latest conversation item or None if no items exist
        """
        try:
            pk = self._get_conversation_pk()

            # Query for the latest item (reverse chronological order)
            response = self.table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(pk),
                ScanIndexForward=False,  # Descending order (latest first)
                Limit=1,
            )

            items = response.get("Items", [])
            return items[0] if items else None

        except Exception as e:
            logger.error(
                f"Error getting latest conversation item for session {self.session_id}: {e}"
            )
            return None

    def _parse_history(self, item: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        The message array stored on one conversation item.

        Args:
            item: A conversation item read from DynamoDB

        Returns:
            The stored messages, or an empty list if the attribute is absent or
            cannot be parsed as an array.
        """
        try:
            messages = json.loads(item.get("conversation_history", "[]"))
        except json.JSONDecodeError:
            logger.warning(
                f"Invalid JSON in conversation_history for session {self.session_id}, starting fresh"
            )
            return []
        return messages if isinstance(messages, list) else []

    def _put_new_item(self, pk: str, message_entry: Dict[str, Any]) -> None:
        """
        Start a new conversation item holding one message.

        Args:
            pk: The conversation partition key
            message_entry: The message to seed the new item with
        """
        self.table.put_item(
            Item={
                "PK": pk,
                "SK": self._generate_timestamp_sk(),
                "conversation_history": json.dumps([message_entry]),
                "session_id": self.session_id,
                "last_updated": datetime.now().isoformat(),
                "message_count": 1,
                _VERSION_ATTRIBUTE: 1,
            }
        )

    def _put_conditionally(self, item: Dict[str, Any], version: int) -> bool:
        """
        Write the item back, but only if nobody else wrote since the read.

        Args:
            item: The rewritten conversation item, new message included
            version: The version observed by the read this write is based on

        Returns:
            True if the write committed, False if another writer got there first
            and the append has to be rebuilt on a fresh read.
        """
        try:
            self.table.put_item(
                Item=item,
                # The first clause admits an item written before the guard
                # existed; two writers racing to add it still conflict, because
                # whichever commits first makes the attribute exist.
                ConditionExpression=(
                    f"attribute_not_exists({_VERSION_ATTRIBUTE}) "
                    f"OR {_VERSION_ATTRIBUTE} = :expected_version"
                ),
                ExpressionAttributeValues={":expected_version": version},
            )
            return True
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                return False
            raise

    def _store_message_to_dynamodb(
        self, message_content: str, message_role: str
    ) -> None:
        """
        Store a single message to DynamoDB, creating new items when size limit is approached.

        This method implements an efficient storage strategy:
        1. Try to append to the latest existing item
        2. If adding the message would exceed size limit, create a new item
        3. Track message count and timestamps for debugging

        Appending rewrites the whole newest item, so the write is conditional on
        a version attribute read alongside it: a write whose version no longer
        matches is rejected by DynamoDB and rebuilt on a fresh read, rather than
        replacing a message another writer appended in between. Two invocations
        serving one session -- a resubmitted request, or a retried Lambda -- are
        what make that overlap possible.

        Args:
            message_content: The message content to store
            message_role: The role of the message (user, assistant, system, etc.)
        """
        try:
            pk = self._get_conversation_pk()

            # Create message entry
            # Store content as-is (it's already in the correct format from Strands)
            message_entry = {
                "timestamp": datetime.now().isoformat(),
                "role": message_role,
                "content": message_content,  # Store the actual content structure
                "sequence_number": int(
                    datetime.now().timestamp() * 1000000
                ),  # Microsecond precision
            }

            for attempt in range(1, _MAX_APPEND_ATTEMPTS + 1):
                # Get the latest conversation item
                latest_item = self._get_latest_conversation_item()

                if not latest_item:
                    # Create first item
                    self._put_new_item(pk, message_entry)
                    logger.info(
                        f"Created first conversation item for session {self.session_id}"
                    )
                    break

                existing_messages = self._parse_history(latest_item)

                # Create a test item with the new message to check size
                test_messages = existing_messages + [message_entry]
                version = _coerce_int(latest_item.get(_VERSION_ATTRIBUTE))
                test_item = {
                    "PK": pk,
                    "SK": latest_item["SK"],  # Use existing timestamp
                    "conversation_history": json.dumps(test_messages),
                    "session_id": self.session_id,
                    "last_updated": datetime.now().isoformat(),
                    "message_count": len(test_messages),
                    _VERSION_ATTRIBUTE: version + 1,
                }

                # Check if adding this message would exceed size limit
                test_size_kb = self._get_item_size_bytes(test_item) / 1024

                if test_size_kb > self.max_item_size_kb:
                    # Create new item with just this message. This lands on a
                    # fresh sort key, so it needs no guard: it cannot overwrite
                    # the item it is rolling over from.
                    self._put_new_item(pk, message_entry)
                    logger.info(
                        f"Created new item for session {self.session_id} (previous item was {test_size_kb:.2f} KB)"
                    )
                    break

                if self._put_conditionally(test_item, version):
                    logger.debug(
                        f"Updated existing item for session {self.session_id}, size: {test_size_kb:.2f} KB"
                    )
                    break

                # Another writer appended between the query and the write. Loop
                # to re-read so that its message survives alongside this one.
                logger.debug(
                    f"Concurrent append detected for session {self.session_id} "
                    f"(attempt {attempt}), retrying"
                )
            else:
                logger.error(
                    f"Gave up storing message for session {self.session_id} after "
                    f"{_MAX_APPEND_ATTEMPTS} attempts; the message was not persisted"
                )
                return

            logger.debug(
                f"Successfully stored message for session {self.session_id}, role: {message_role}"
            )

        except ClientError as e:
            logger.error(
                f"DynamoDB error storing message for session {self.session_id}: {e}"
            )
        except Exception as e:
            logger.error(
                f"Unexpected error storing message for session {self.session_id}: {e}"
            )
            logger.error(f"Traceback: {traceback.format_exc()}")

    def _load_conversation_history(self) -> List[List[Dict[str, Any]]]:
        """
        Load conversation history from DynamoDB in chronological order.

        This method efficiently retrieves only the latest messages and groups them
        into turns for better context management.

        Turn Grouping:
        - A turn starts with a user message
        - Includes all subsequent assistant/system/tool messages
        - Continues until the next user message

        This grouping helps the agent understand the conversation flow and
        maintain context across multiple exchanges.

        Returns:
            List of conversation turns, where each turn is a list of messages
        """
        try:
            pk = self._get_conversation_pk()

            # Query for recent items in reverse chronological order
            response = self.table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(pk),
                ScanIndexForward=False,  # Descending order (latest first)
                Limit=10,  # Get more items than needed to ensure we have enough messages
            )

            items = response.get("Items", [])

            if not items:
                logger.info(
                    f"No conversation history found for session {self.session_id}"
                )
                return []

            # Collect all messages from all items
            all_messages = []
            for item in reversed(items):  # Reverse to get chronological order
                conversation_history_str = item.get("conversation_history", "[]")
                try:
                    messages = json.loads(conversation_history_str)
                    if isinstance(messages, list):
                        all_messages.extend(messages)
                except json.JSONDecodeError:
                    logger.warning(
                        f"Invalid JSON in conversation_history for item {item.get('SK')}"
                    )
                    continue

            # Group messages into turns
            # A turn starts with a user message and includes all subsequent assistant messages
            turns = []
            current_turn = []

            for message in all_messages:
                role = message.get("role", "")

                if role == "user":
                    # Start a new turn
                    if current_turn:  # Save previous turn if it exists
                        turns.append(current_turn)
                    current_turn = [message]  # Start new turn with user message
                elif role in ["assistant", "system", "tool"]:
                    # Add to current turn (assistant responses, tool calls, etc.)
                    if current_turn:  # Only add if we have a turn started
                        current_turn.append(message)
                    else:
                        # Edge case: assistant message without user message, create a turn
                        current_turn = [message]

            # Don't forget the last turn
            if current_turn:
                turns.append(current_turn)

            # Take only the last N turns
            recent_turns = (
                turns[-self.max_history_turns :]
                if len(turns) > self.max_history_turns
                else turns
            )

            logger.info(
                f"Loaded {len(recent_turns)} conversation turns from DynamoDB for session {self.session_id}"
            )
            return recent_turns

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                logger.info(
                    f"No conversation table found for session {self.session_id}"
                )
            else:
                logger.error(
                    f"DynamoDB error loading conversation for session {self.session_id}: {e}"
                )
            return []
        except Exception as e:
            logger.error(
                f"Unexpected error loading conversation for session {self.session_id}: {e}"
            )
            logger.error(f"Traceback: {traceback.format_exc()}")
            return []

    def on_agent_initialized(self, event: AgentInitializedEvent):
        """
        Hook called when agent is initialized.

        Loads recent conversation history and adds it to the agent's system prompt
        to provide context for the current conversation.

        Args:
            event: Agent initialization event containing the agent instance
        """
        try:
            # Load recent conversation turns from DynamoDB
            recent_turns = self._load_conversation_history()

            if recent_turns:
                # Format conversation history for context
                context_messages = []
                for turn in recent_turns:
                    for message in turn:
                        role = message.get("role", "unknown")
                        content = message.get("content", {})
                        if isinstance(content, dict):
                            text = content.get("text", str(content))
                        else:
                            text = str(content)
                        context_messages.append(f"{role}: {text}")

                context = "\n".join(context_messages)

                # Add context to agent's system prompt
                if event.agent.system_prompt is None:
                    event.agent.system_prompt = f"Recent conversation:\n{context}"
                else:
                    event.agent.system_prompt += f"\n\nRecent conversation:\n{context}"

                logger.info(
                    f"✅ Agent Memory: Loaded {len(recent_turns)} conversation turns for session {self.session_id}"
                )

        except Exception as e:
            logger.error(f"Agent Memory load error for session {self.session_id}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")

    def on_message_added(self, event: MessageAddedEvent):
        """
        Hook called when a message is added to the conversation.

        Stores the message in DynamoDB with size checking and truncation if needed.

        Args:
            event: Message added event containing the agent and message
        """
        messages = event.agent.messages

        # Extract message content and role
        message_content = messages[-1].get("content", "")
        message_role = messages[-1]["role"]

        # Calculate message size (serialize to JSON for accurate size)
        message_json = json.dumps(message_content)
        size_bytes = len(message_json.encode("utf-8"))
        size_kb = size_bytes / 1024
        logger.info(
            f"Agent Memory: Message size: {size_bytes} bytes ({size_kb:.2f} KB), role: {message_role}"
        )

        # Check if message is larger than the configured limit
        max_size_bytes = self.max_message_size_kb * 1024
        try:
            if size_bytes > max_size_bytes:
                # Truncate the message
                logger.info("Agent Memory: Message too large, truncating")
                # Create truncated content structure
                truncated_content = [
                    {
                        "text": f"This message was too large to add. Here is the truncated head: {message_json[:500]}"
                    }
                ]

                try:
                    # Store the truncated message
                    self._store_message_to_dynamodb(truncated_content, message_role)
                    logger.info("Successfully stored truncated message to DynamoDB")
                except Exception as e:
                    logger.error(
                        f"Agent Memory: Failed to store truncated message for session {self.session_id}: {e}"
                    )
            else:
                try:
                    # Store the original message content (not stringified)
                    self._store_message_to_dynamodb(message_content, message_role)
                    logger.info("Successfully stored message to DynamoDB")
                except Exception as e:
                    logger.error(
                        f"Agent Memory: Failed to store message for session {self.session_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Agent Memory: Error storing message: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")

    def register_hooks(self, registry: HookRegistry):
        """
        Register memory hooks with the agent's hook registry.

        Args:
            registry: The hook registry to register with
        """
        registry.add_callback(MessageAddedEvent, self.on_message_added)
        registry.add_callback(AgentInitializedEvent, self.on_agent_initialized)

    def clear_conversation_history(self) -> bool:
        """
        Clear the conversation history for the current session.

        Useful for testing or when user wants to start a fresh conversation.

        Returns:
            True if successful, False otherwise
        """
        try:
            pk = self._get_conversation_pk()

            # Query all items for this conversation
            response = self.table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(pk),
                ProjectionExpression="PK, SK",
            )

            # Delete all items
            with self.table.batch_writer() as batch:
                for item in response.get("Items", []):
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

            logger.info(f"Cleared conversation history for session {self.session_id}")
            return True

        except Exception as e:
            logger.error(
                f"Error clearing conversation history for session {self.session_id}: {e}"
            )
            return False

    def get_conversation_stats(self) -> Dict[str, Any]:
        """
        Get statistics about the current conversation.

        Useful for debugging and monitoring conversation size.

        Returns:
            Dictionary with conversation statistics including:
            - session_id: The session ID
            - item_count: Number of DynamoDB items
            - total_message_count: Total number of messages
            - last_updated: Timestamp of last update
            - exists: Whether conversation exists
        """
        try:
            pk = self._get_conversation_pk()

            # Query to count items and messages
            response = self.table.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(pk)
            )

            items = response.get("Items", [])
            item_count = len(items)
            total_message_count = 0
            latest_timestamp = None

            for item in items:
                total_message_count += item.get("message_count", 0)
                item_timestamp = item.get("last_updated")
                if not latest_timestamp or (
                    item_timestamp and item_timestamp > latest_timestamp
                ):
                    latest_timestamp = item_timestamp

            return {
                "session_id": self.session_id,
                "item_count": item_count,
                "total_message_count": total_message_count,
                "last_updated": latest_timestamp,
                "exists": item_count > 0,
            }

        except Exception as e:
            logger.error(
                f"Error getting conversation stats for session {self.session_id}: {e}"
            )
            return {"session_id": self.session_id, "error": str(e), "exists": False}
