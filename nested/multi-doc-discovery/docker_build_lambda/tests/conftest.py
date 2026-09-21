# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Supply the region that ``index.py`` needs at import time.

``docker_build_lambda/index.py`` builds its CodeBuild client at module scope
(``codebuild = boto3.client("codebuild")``), which is correct for a Lambda — the
runtime always sets ``AWS_REGION`` and a warm invocation reuses the client. The
suite imports that module, so it inherits the requirement: with no region
resolvable botocore raises ``NoRegionError`` during collection and the whole
directory errors out before a single test runs.

The region is supplied here rather than on the ``make test-packages-cicd`` recipe
line so that no caller has to know. ``$(PYTEST_HERMETIC)`` strips the machine's
AWS environment precisely so a suite that depends on an ambient one fails locally
instead of only on a CI runner; a pin in the caller would defeat that. See #988.

``setdefault`` rather than assignment: a developer running with a real region
keeps it, and nothing here reaches AWS — ``index.codebuild`` is replaced with a
``MagicMock`` by the suite's own ``harness`` fixture before any call is made.

Scope note: this ``setdefault`` re-supplies a region for **everything** this
directory imports, not just ``index``. ``index`` is the only module on that import
path that builds a client, and it builds exactly one.
"""

from __future__ import annotations

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
