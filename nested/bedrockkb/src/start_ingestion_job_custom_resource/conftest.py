# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Supply the region that ``handler.py`` needs at import time.

``start_ingestion_job_custom_resource/handler.py`` builds its bedrock-agent client
at module scope (``CLIENT = boto3.client('bedrock-agent')``), which is correct for
a Lambda — the runtime always sets ``AWS_REGION``. The suite's ``mock_client``
fixture imports that module, so it inherits the requirement: with no region
resolvable botocore raises ``NoRegionError`` and all nine tests error at setup.

The region is supplied here rather than on the ``make test-packages-cicd`` recipe
line so that no caller has to know. ``$(PYTEST_HERMETIC)`` strips the machine's
AWS environment precisely so a suite that depends on an ambient one fails locally
instead of only on a CI runner; a pin in the caller would defeat that. See #988.

A conftest is the route here rather than a lazily-built client because building the
client lazily would be a change to a deployed Lambda handler, and what is wrong is
the test harness's assumption rather than the handler: the module-level client is
correct in production and a warm invocation reuses it. The region is what this suite
needs, so this suite is where it belongs. Nothing reaches AWS — ``handler.CLIENT`` is
replaced with a ``MagicMock`` before any call.

Scope note: this ``setdefault`` re-supplies a region for **everything** this
directory imports, not just ``handler``. ``handler`` is the only module here, and
it builds exactly one client.
"""

from __future__ import annotations

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
