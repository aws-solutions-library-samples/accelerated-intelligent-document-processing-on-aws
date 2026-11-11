# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Base class for IDP MCP tools, and the configuration-scope lookups they share."""

import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

#: Message for a request naming a batch none of whose documents the caller may see,
#: identical to the one for a batch that does not exist, so a refusal does not say
#: whether the batch exists.
BATCH_NOT_FOUND = "Batch not found: {batch_id}"


class IDPTool(ABC):
    """Base class for IDP MCP tools.

    ``execute`` receives the verified caller (``idp_common.mcp_access.Caller``) as
    ``caller`` alongside the tool's own arguments. The handler refuses a call with
    no verified caller before any tool runs.
    """

    @abstractmethod
    def execute(self, caller: Any = None, **kwargs) -> Dict[str, Any]:
        """Execute tool logic"""
        pass


def config_version_lookup(
    tracking_table: Optional[Any] = None,
) -> Callable[[str], Any]:
    """``key -> profile`` from the tracking table, in ``document_scope``'s
    three-way form (a name, ``None`` for no profile, ``ABSENT`` for no row).

    Memoized for one request: a batch can name a document more than once."""
    from idp_common.document_scope import tracked_config_version

    if tracking_table is None:
        import boto3

        tracking_table = boto3.resource("dynamodb").Table(
            os.environ["TRACKING_TABLE_NAME"]
        )
    memo: Dict[str, Any] = {}

    def lookup(key: str) -> Any:
        if key not in memo:
            memo[key] = tracked_config_version(tracking_table, key)
        return memo[key]

    return lookup


def document_id_from_ref(document_id: str) -> str:
    """A document reference (object key, or ``s3://bucket/<key>[/]``) as its key.

    The same rule the SDK applies before it reads a document, so the key checked
    against the caller's scope is the key whose results are returned. A plain split,
    never ``urlparse``, which reads ``#`` in a key as a fragment.
    """
    if document_id.startswith("s3://"):
        parts = document_id[len("s3://") :].split("/", 1)
        if len(parts) < 2 or not parts[1]:
            raise ValueError(
                f"Invalid document reference: {document_id}. "
                "Expected s3://bucket/<document_id>"
            )
        document_id = parts[1]
    return document_id.rstrip("/")
