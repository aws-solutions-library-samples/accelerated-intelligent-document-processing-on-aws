# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Load a Lambda ``index.py`` by path under a unique module name.

Every handler here is a module named ``index``; loading by path under a unique
name lets one pytest invocation hold several of them without collision.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.unit._handler_loading import exec_with_stubs

REPO_ROOT = Path(__file__).resolve().parents[5]


def load_lambda(module_name: str, relative_dir: str, *, patches: dict | None = None):
    """Import ``<repo>/<relative_dir>/index.py`` as ``module_name``.

    ``patches`` maps dotted targets to replacement objects, applied only while the
    module executes (for module-scope clients and services). See
    ``tests.unit._handler_loading`` for what is left in ``sys.modules`` afterwards.
    """
    os.environ.setdefault("AWS_REGION", "us-east-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    path = REPO_ROOT / relative_dir / "index.py"
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    stubs = {"aws_xray_sdk": MagicMock(), "aws_xray_sdk.core": xray_core}
    managers = [patch(target, value) for target, value in (patches or {}).items()]
    for m in managers:
        m.start()
    try:
        return exec_with_stubs(module_name, path, stubs, taint=tuple(patches or ()))
    finally:
        for m in reversed(managers):
            m.stop()
