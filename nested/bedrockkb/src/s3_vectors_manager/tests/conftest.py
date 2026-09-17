# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test config for the S3 Vectors custom-resource Lambda.

Stubs ``cfnresponse`` (delivered via the ``cfnresponse`` pip dep at deploy
time), sets fake AWS credentials, and puts the handler directory on
``sys.path`` so ``import handler`` works from this ``tests/`` package.
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

_HANDLER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HANDLER_DIR not in sys.path:
    sys.path.insert(0, _HANDLER_DIR)
