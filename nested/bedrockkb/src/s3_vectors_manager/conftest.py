# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Test config for the S3 Vectors custom-resource Lambda, at directory scope.

``test_handler.py`` sits beside ``handler.py`` rather than under ``tests/``, and it
imports the handler directly. ``handler.py`` imports ``cfnresponse`` (vendored as
``cfnresponse.py`` beside it), whose ``send`` would PUT to a real URL, and it builds
boto3 clients — so the suite needs ``send`` replaced, a region and placeholder
credentials. Without them the
directory was excluded from every gate and its five tests ran nowhere.

``tests/conftest.py`` does the same three things for ``tests/test_iam_scope.py``.
It stays where it is: it is what makes ``pytest tests`` work from a directory other
than this one, and the ``setdefault`` calls and the same-named fixture make the overlap a no-op.

``handler`` binds the real vendored ``cfnresponse.py``, and the autouse
``_no_response_put`` fixture replaces its ``send`` with a fresh ``MagicMock`` in
every test, so no test can PUT to the event's ``ResponseURL``.

Do not put a ``MagicMock`` in ``sys.modules["cfnresponse"]`` instead. ``send`` on a
mock is an auto-created child that is not in the mock's ``__dict__``; from pytest
9.2.0, ``monkeypatch.setattr`` reads the old value from ``__dict__``, finds none,
and undoes the patch with ``delattr``, which on a mock marks ``send`` deleted, so
every later test that patches it fails with ``has no attribute 'send'``. A module's
``send`` is a real attribute, so patching and restoring it is exact.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_HANDLER_DIR = os.path.dirname(os.path.abspath(__file__))
if _HANDLER_DIR not in sys.path:
    sys.path.insert(0, _HANDLER_DIR)


def _bind_vendored_cfnresponse() -> ModuleType:
    """Import the ``cfnresponse.py`` beside ``handler.py``, and refuse a foreign one.

    ``handler`` binds whatever ``sys.modules["cfnresponse"]`` holds when it is first
    imported, at collection time, so a stub another test left there would decide what
    every handler test patches.
    """
    import cfnresponse

    vendored = os.path.join(_HANDLER_DIR, "cfnresponse.py")
    if os.path.abspath(getattr(cfnresponse, "__file__", "") or "") != vendored:
        raise RuntimeError(
            f"sys.modules['cfnresponse'] is {cfnresponse!r}, not {vendored}; "
            "something installed a stand-in before the S3 Vectors handler was imported"
        )
    return cfnresponse


_cfnresponse = _bind_vendored_cfnresponse()


@pytest.fixture(autouse=True)
def _no_response_put(monkeypatch):
    """A fresh recorder for ``cfnresponse.send`` in every test, so nothing PUTs."""
    monkeypatch.setattr(_cfnresponse, "send", MagicMock())
