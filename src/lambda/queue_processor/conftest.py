# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Make a few real ``idp_common`` leaf modules importable in this suite's
hermetic tests.

The tests in this directory import ``index`` with ``idp_common`` replaced by a
``MagicMock`` in ``sys.modules``, because the real library is delivered by a Lambda
layer at deploy time and pulling it in would make a unit test depend on the whole
package. A stubbed parent is not a package, so each of the handler's
``from idp_common.<sub>.<mod> import ...`` lines — the log redactor, so that it no
longer writes its unredacted invocation event to CloudWatch, and the transient-error
classifier the circuit-breaker check consults — fails at import with
``'idp_common' is not a package``.

Each is loaded for REAL, by path, rather than stubbed. Two reasons. They are
stdlib-plus-botocore only, so loading a handful of files costs nothing and adds no
dependency on the library being installed. And a ``MagicMock`` would not work
anyway: the handler passes the redactor's result straight to ``json.dumps``, which
cannot serialize one — so a mock there would turn a redaction bug into a confusing
serialization error, and an identity stub would quietly make these tests pass
whatever the handler logs.

Each is registered under its canonical dotted name so the handler's import resolves
from ``sys.modules`` without the stubbed parent being consulted. The same
load-the-real-thing-by-path approach is used for ``idp_common.config_scope`` in
``src/lambda/chat_with_document_processor/tests/conftest.py``, for the same reason.
"""

import importlib.util
import sys
from pathlib import Path

#: Dotted name -> path, relative to the checkout root, of every ``idp_common``
#: module the handler imports that must be the REAL implementation here.
#:
#: ``transient_errors`` is loaded for the same reasons as the sanitizer, and one
#: more: ``check_circuit_breaker``'s decision on a failed DynamoDB read IS this
#: classifier's verdict, so a ``MagicMock`` in its place would make the tests
#: that assert the transient/terminal split pass whichever way the real
#: classifier judges the error. It pulls in ``bedrock_utils`` for the retryable
#: vocabulary, which is why that module is listed FIRST — loading it registers
#: the dotted name, and ``transient_errors``'s own
#: ``from idp_common.utils.bedrock_utils import ...`` then resolves out of
#: ``sys.modules`` without the stubbed ``idp_common`` parent being consulted.
#: Both are stdlib + botocore only, so loading them costs nothing.
_REAL_MODULES: tuple[tuple[str, str], ...] = (
    (
        "idp_common.utils.log_sanitizer",
        "lib/idp_common_pkg/idp_common/utils/log_sanitizer.py",
    ),
    (
        "idp_common.utils.bedrock_utils",
        "lib/idp_common_pkg/idp_common/utils/bedrock_utils.py",
    ),
    (
        "idp_common.utils.transient_errors",
        "lib/idp_common_pkg/idp_common/utils/transient_errors.py",
    ),
)


def _repo_root() -> Path:
    """Walk up to the checkout root, rather than counting ``parents[n]``.

    A hardcoded index breaks silently if this file is ever moved a level, and the
    failure mode is an unhelpful ``FileNotFoundError`` deep inside collection.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "lib" / "idp_common_pkg").is_dir():
            return parent
    raise RuntimeError("could not locate the repository root from this test file")


for _dotted, _relative in _REAL_MODULES:
    if _dotted in sys.modules:
        continue
    _path = _repo_root() / _relative
    _spec = importlib.util.spec_from_file_location(_dotted, _path)
    assert _spec is not None and _spec.loader is not None, f"cannot load {_path}"
    _module = importlib.util.module_from_spec(_spec)
    # Registered BEFORE execution so a module that imports a sibling already
    # registered above resolves it from sys.modules, and so a circular import
    # would fail loudly rather than re-entering the loader.
    sys.modules[_dotted] = _module
    _spec.loader.exec_module(_module)
