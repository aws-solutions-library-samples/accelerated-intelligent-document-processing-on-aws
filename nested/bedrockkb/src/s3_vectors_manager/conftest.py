# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test config for the S3 Vectors custom-resource Lambda, at directory scope.

``test_handler.py`` sits beside ``handler.py`` rather than under ``tests/``, and it
imports the handler directly. ``handler.py`` imports ``cfnresponse``, which is
delivered by the ``cfnresponse`` pip dependency at deploy time and does not exist
in a test environment, and it builds boto3 clients — so the suite needs a stub, a
region and placeholder credentials before that import happens. Without them the
directory was excluded from every gate and its five tests ran nowhere.

``tests/conftest.py`` does the same three things for ``tests/test_iam_scope.py``.
It stays where it is: it is what makes ``pytest tests`` work from a directory other
than this one, and the ``setdefault`` calls make the overlap a no-op.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_cfnresponse = MagicMock()
_cfnresponse.SUCCESS = "SUCCESS"
_cfnresponse.FAILED = "FAILED"
sys.modules.setdefault("cfnresponse", _cfnresponse)

_HANDLER_DIR = os.path.dirname(os.path.abspath(__file__))
if _HANDLER_DIR not in sys.path:
    sys.path.insert(0, _HANDLER_DIR)
