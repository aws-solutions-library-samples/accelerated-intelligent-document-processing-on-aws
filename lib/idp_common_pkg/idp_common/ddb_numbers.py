# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Numeric coercion for values read back out of DynamoDB.

``boto3``'s resource-level interface deserialises every DynamoDB ``N`` attribute
to ``decimal.Decimal``, so a value that was written as an ``int`` does not compare
equal to one and cannot be used where an ``int`` is expected. Every optimistic-
locking version counter in this package reads a number back and compares or
increments it, which is why this lives in one place rather than beside each of
them.

It is a top-level leaf module on purpose. The natural-looking home,
``idp_common.dynamodb``, costs about 140 ms to import because its package
``__init__`` pulls in the client, the document service and ``idp_common.models``;
paying that in ``idp_common.config.revisions`` and in the agent Lambdas for one
coercion would be a poor trade, and ``idp_common.utils`` imports
``idp_common.config.models``, which would make ``revisions`` importing it a cycle.
"""

from typing import Any

__all__ = ["coerce_int"]


def coerce_int(value: Any, default: int = 0) -> int:
    """
    Read a DynamoDB number as an ``int``.

    Args:
        value: The value read from a DynamoDB item, typically a ``Decimal``.
            ``None`` is expected and normal: it is what ``dict.get`` returns for
            a version attribute that an item written before the guard existed
            does not carry.
        default: The value to return when ``value`` is absent or not numeric.

    Returns:
        ``value`` as an ``int``, or ``default`` if it is ``None`` or cannot be
        converted.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
