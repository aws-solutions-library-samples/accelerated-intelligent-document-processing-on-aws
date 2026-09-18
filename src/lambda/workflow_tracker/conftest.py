# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Make the canonical log redactor importable in this suite's hermetic tests.

The tests in this directory import ``index`` with ``idp_common`` replaced by a
``MagicMock`` in ``sys.modules``, because the real library is delivered by a Lambda
layer at deploy time and pulling it in would make a unit test depend on the whole
package. A stubbed parent is not a package, so the handler's
``from idp_common.utils.log_sanitizer import sanitize_event_for_logging`` — added so
that it no longer writes its unredacted invocation event to CloudWatch — fails at
import with ``'idp_common' is not a package``.

The redactor is loaded for REAL, by path, rather than stubbed. Two reasons. It is
stdlib-only (``copy``, ``re``, ``typing``), so loading one file costs nothing and
adds no dependency on the library being installed. And a ``MagicMock`` would not
work anyway: the handler passes the result straight to ``json.dumps``, which cannot
serialize one — so a mock here would turn a redaction bug into a confusing
serialization error, and an identity stub would quietly make these tests pass
whatever the handler logs.

Registered under the canonical dotted name so the handler's import resolves from
``sys.modules`` without the stubbed parent being consulted. The same
load-the-real-thing-by-path approach is used for ``idp_common.config_scope`` in
``src/lambda/chat_with_document_processor/tests/conftest.py``, for the same reason.
"""

import importlib.util
import sys
from pathlib import Path

_CANONICAL_SANITIZER = "idp_common.utils.log_sanitizer"


def _repo_root() -> Path:
    """Walk up to the checkout root, rather than counting ``parents[n]``.

    A hardcoded index breaks silently if this file is ever moved a level, and the
    failure mode is an unhelpful ``FileNotFoundError`` deep inside collection.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "lib" / "idp_common_pkg").is_dir():
            return parent
    raise RuntimeError("could not locate the repository root from this test file")


if _CANONICAL_SANITIZER not in sys.modules:
    _path = (
        _repo_root() / "lib/idp_common_pkg/idp_common/utils/log_sanitizer.py"
    )
    _spec = importlib.util.spec_from_file_location(_CANONICAL_SANITIZER, _path)
    assert _spec is not None and _spec.loader is not None, f"cannot load {_path}"
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    sys.modules[_CANONICAL_SANITIZER] = _module
