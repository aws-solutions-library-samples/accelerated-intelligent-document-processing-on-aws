# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Make this suite hermetic with respect to the ambient AWS environment.

``index.py`` constructs its Cognito client at module scope, which is the right
thing for a Lambda handler — the client is reused across warm invocations, and the
runtime always provides ``AWS_REGION``. It does mean that importing the handler
needs a resolvable region, and with none botocore raises ``NoRegionError``. Before
this file the suite therefore passed on a developer machine, whose region comes
from the shared AWS config file, and failed on a CI runner, which has neither that
file nor the environment variables: 41 of its 49 tests errored in their fixture
setup. Issue #988 has the measurements.

Pinning the values here rather than in the caller is what makes the suite
self-contained: ``pytest`` run directly in this directory, ``make test`` via
``scripts/run_all_tests.py``, and ``make test-packages-cicd`` all behave the same.
The credentials are fake and no AWS call is made — every Cognito call the handler
makes is against a ``MagicMock`` assigned to ``index.cognito``.

One set of tests is deliberately *not* covered by this file, and cannot be.
``TestGroupMapping`` reloads ``index`` under ``patch.dict(os.environ, ..., clear=True)``
to check how ``GROUP_MAPPING`` is built from the ``*_GROUP_NAME`` variables.
``clear=True`` empties the environment this file populates before the reload
re-runs the module-scope client construction, so those three tests carry
``AWS_DEFAULT_REGION`` inside their own env dictionaries. That is explained again
at the site, because reading either one alone would otherwise suggest the region is
being set twice for no reason.

``setdefault`` throughout, so a real environment still wins if someone
deliberately points this suite at an account.
"""

from __future__ import annotations

import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
