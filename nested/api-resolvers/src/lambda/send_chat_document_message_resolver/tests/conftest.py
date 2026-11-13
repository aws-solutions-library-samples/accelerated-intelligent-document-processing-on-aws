# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test configuration for send_chat_document_message_resolver."""

from __future__ import annotations

import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

os.environ.setdefault("CHAT_DOCUMENT_SESSIONS_TABLE", "chat-sessions-table")
os.environ.setdefault("CHAT_SESSION_OWNERS_TABLE", "chat-session-owners-table")
os.environ.setdefault("CHAT_MESSAGES_TABLE", "chat-messages-table")
os.environ.setdefault(
    "CHAT_DOCUMENT_PROCESSOR_FUNCTION_ARN",
    "arn:aws:lambda:us-east-1:000000000000:function:chat-processor",
)
os.environ.setdefault("DATA_RETENTION_DAYS", "1")
