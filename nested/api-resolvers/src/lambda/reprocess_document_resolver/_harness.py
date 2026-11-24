# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the newer suites of this resolver share: the handler by path, and events."""

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("INPUT_BUCKET", "test-in")
os.environ.setdefault("OUTPUT_BUCKET", "test-out")
os.environ.setdefault("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/test-queue")
# index.py builds a document service at import; it needs a table name but makes
# no call, so any value does.
os.environ.setdefault("TRACKING_TABLE", "test-tracking")

REPROCESS = "reprocessDocument"
OPTIONS = "getReprocessOptions"


def load(name):
    """The handler module, loaded under ``name`` rather than as ``index``."""
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rest_event(groups, keys=("claims/a.pdf",), field=REPROCESS, **arguments):
    """A call through the REST route: the dispatcher always sends an identity."""
    return {
        "info": {"fieldName": field},
        "arguments": {"objectKeys": list(keys), **arguments},
        "identity": {
            "claims": {"cognito:groups": list(groups), "email": "someone@example.com"}
        },
    }


def direct_event(keys=("claims/a.pdf",), field=REPROCESS, acting=None, **arguments):
    """A direct Lambda invoke: no identity, and optionally the caller it acts for."""
    event = {
        "info": {"fieldName": field},
        "arguments": {"objectKeys": list(keys), **arguments},
    }
    if acting is not None:
        event["actingCaller"] = acting
    return event


def acting_caller(roles, scope=None):
    """The payload the MCP handler sends (``idp_common.mcp_access.Caller``)."""
    return {
        "v": 1,
        "kind": "user",
        "id": "user-1",
        "name": "someone@example.com",
        "roles": list(roles),
        "scope": scope,
        "tenant": None,
        "writeTools": True,
    }
