# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Supply the region that ``index.py`` needs at import time.

``list_agent_chat_sessions_resolver/index.py`` builds its DynamoDB resource at
module scope (``dynamodb = boto3.resource("dynamodb")``), which is correct for a
Lambda — the runtime always sets ``AWS_REGION`` and a warm invocation reuses the
resource. The suite imports that module, so it inherits the requirement: with no
region resolvable botocore raises ``NoRegionError`` during collection and the whole
directory errors out before a single test runs.

The region is supplied here rather than on the ``make test-packages-cicd`` recipe
line so that no caller has to know. ``$(PYTEST_HERMETIC)`` strips the machine's
AWS environment precisely so a suite that depends on an ambient one fails locally
instead of only on a CI runner; a pin in the caller would defeat that. See #988.

A conftest is the route here rather than a lazily-built client because the suite
patches the resource object itself — ``patch.object(index.dynamodb, "Table", ...)``
— so ``index.dynamodb`` must be a real resource for the tests to have anything to
patch. No AWS call is made: every table handle the tests use is a ``MagicMock``.

Scope note: this ``setdefault`` re-supplies a region for **everything** this
directory imports, not just ``index``. The only other module on that import path
is the stdlib-only ``log_sanitizer``.
"""

from __future__ import annotations

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
