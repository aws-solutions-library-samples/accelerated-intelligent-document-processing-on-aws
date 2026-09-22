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
    _path = _repo_root() / "lib/idp_common_pkg/idp_common/utils/log_sanitizer.py"
    _spec = importlib.util.spec_from_file_location(_CANONICAL_SANITIZER, _path)
    assert _spec is not None and _spec.loader is not None, f"cannot load {_path}"
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    sys.modules[_CANONICAL_SANITIZER] = _module


FIRST_PARTY_UNDER_TEST = ("idp_common",)


# Fail fast if a first-party package resolves outside this checkout. These tests import
# the library as an external dependency, so nothing puts its source on `sys.path` and the
# editable-install pointer alone decides which revision runs. On a machine sharing one
# interpreter between checkouts that pointer is rewritten by whoever last ran
# `make test-cicd`, and the wrong tree produces a green run describing other code.
#
# The imports sit inside the function deliberately: this block is appended after a
# module's own imports, and module-level imports there are an E402 lint failure.
#
# Rationale, and the identity-vs-ancestry distinction:
# scripts/tests/first_party_provenance.py
def _assert_first_party_provenance(packages):
    import pathlib as _pathlib
    import sys as _sys

    for ancestor in _pathlib.Path(__file__).resolve().parents:
        gate = ancestor / "scripts" / "tests" / "first_party_provenance.py"
        if not gate.is_file():
            continue
        _sys.path.insert(0, str(gate.parent))
        try:
            from first_party_provenance import assert_resolves_in

            for package in packages:
                assert_resolves_in(package, __file__)
        finally:
            _sys.path.pop(0)
        return


_assert_first_party_provenance(FIRST_PARTY_UNDER_TEST)
