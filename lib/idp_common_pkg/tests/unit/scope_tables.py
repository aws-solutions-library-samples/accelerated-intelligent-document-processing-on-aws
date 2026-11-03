# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Moto tables for configuration-scope tests of resolvers that act on one document.

Not a fixture module on purpose: each suite builds these inside its own
``mock_aws`` block, after the module under test has been imported, so the tables
belong to the same mock the resolver's resource talks to.

The cast, used by every suite that imports this:

* ``scoped@example.com`` — scope ``["acme-*"]``.
* ``open@example.com`` — a row with no scope (unrestricted in ``Off`` mode).
* tracking rows ``doc#a.pdf`` (``acme-invoices``), ``doc#g.pdf`` (``globex-w2``).
"""

from typing import Any, Dict, Tuple

USERS = "scope-users"
TRACKING = "scope-tracking"

SCOPED_EMAIL = "scoped@example.com"
OPEN_EMAIL = "open@example.com"
IN_SCOPE_KEY = "a.pdf"
OUT_OF_SCOPE_KEY = "g.pdf"


def claims(email: str, *groups: str) -> Dict[str, Any]:
    return {"cognito:groups": list(groups), "email": email}


def make_users_table(ddb: Any, name: str = USERS) -> Any:
    """The UsersTable shape the scope lookup reads, with the two cast rows."""
    users = ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "email", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "EmailIndex",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    users.put_item(
        Item={
            "PK": "USER#scoped",
            "SK": "USER#scoped",
            "email": SCOPED_EMAIL,
            "allowedConfigVersions": ["acme-*"],
        }
    )
    users.put_item(Item={"PK": "USER#open", "SK": "USER#open", "email": OPEN_EMAIL})
    return users


def make_tables(ddb: Any, *, tracking_name: str = TRACKING) -> Tuple[Any, Any]:
    users = make_users_table(ddb)
    tracking = ddb.create_table(
        TableName=tracking_name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    for key, profile in (
        (IN_SCOPE_KEY, "acme-invoices"),
        (OUT_OF_SCOPE_KEY, "globex-w2"),
    ):
        tracking.put_item(
            Item={
                "PK": f"doc#{key}",
                "SK": "none",
                "ObjectKey": key,
                "ConfigVersion": profile,
            }
        )
    return users, tracking
