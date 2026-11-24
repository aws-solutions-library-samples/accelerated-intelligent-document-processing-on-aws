# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the resolver's suites share: the handler loaded by path, events, a stub service."""

import functools
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from idp_common.review_queues.service import ReviewQueueService

_INDEX = Path(__file__).with_name("index.py")


def load():
    """The handler module, loaded with no review table configured."""
    name = "review_queues_resolver_index_under_test"
    with patch.dict(os.environ, {"HITL_TABLE_NAME": ""}):
        spec = importlib.util.spec_from_file_location(name, _INDEX)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def event(field, args=None, *, groups=("Admin",), email="Admin@Example.com"):
    """A call through the REST route: the dispatcher always sends an identity."""
    claims = {"email": email, "sub": "sub-1", "cognito:groups": list(groups)}
    return {
        "info": {"fieldName": field},
        "arguments": {} if args is None else args,
        "identity": {"claims": claims},
    }


def direct_event(field, args=None):
    """A direct Lambda invoke (the SDK and the CLI): no identity at all."""
    return {"info": {"fieldName": field}, "arguments": {} if args is None else args}


def stub_service():
    """A stub service whose queue-definition operations are the real ones.

    Every other method is a mock. Creating, updating and listing queues run the
    service's own code over the stub's queue store (``directory.queue_defs``), so
    a case can pin the API shape through the handler and the service together,
    down to the store call.
    """
    service = MagicMock(name="ReviewQueueService")
    for name in ("create_queue", "update_queue", "list_queues"):
        setattr(
            service,
            name,
            functools.partial(getattr(ReviewQueueService, name), service),
        )
    service._definition = ReviewQueueService._definition
    return service
