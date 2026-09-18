# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Make this suite hermetic with respect to the ambient AWS environment.

``index.py`` constructs its S3 client and DynamoDB resource at module scope, which
is the right thing for a Lambda handler — the clients are then reused across warm
invocations, and the runtime always provides ``AWS_REGION``. It does mean that
merely importing the handler needs a resolvable region, and ``test_index.py``
imports it inside every test through ``import_index()``. With no region botocore
raises ``NoRegionError``, so before this file the suite passed on a developer
machine, whose region comes from the shared AWS config file, and failed on a CI
runner, which has neither that file nor the environment variables. Issue #988 has
the measurements.

Pinning the values here rather than in the caller is what makes the suite
self-contained: ``pytest`` run directly in this directory, ``make test`` via
``scripts/run_all_tests.py``, and ``make test-packages-cicd`` all behave the same.
The credentials are fake and no AWS call is made — the clients are constructed and
then every function that would use them is patched out.

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
