"""Test bootstrap for the SDLC CodeBuild harness tests.

`scripts/sdlc/codebuild_deployment.py` is a standalone script (not an installed
package), so put its directory on sys.path and import it once as a module the
tests can monkeypatch. Importing it has no side effects — all AWS/subprocess
work lives inside functions guarded by `if __name__ == "__main__"`.
"""

import sys
from pathlib import Path

import pytest

_SDLC_DIR = Path(__file__).resolve().parent.parent
if str(_SDLC_DIR) not in sys.path:
    sys.path.insert(0, str(_SDLC_DIR))

# Several tests reuse a helper from a sibling test module (the CFN short-form
# loader in test_config_schema_order, the loader registry in
# test_cfn_loader_safety). pytest imports test modules under their bare basename
# but does NOT put this directory on sys.path, so those imports only resolved by
# luck of collection order — and failed outright when a single file was run on
# its own. Put the directory on the path so the imports are order-independent.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# The Step 14 tests build the hook Lambda's zip, which imports idp_common. In
# CodeBuild `make setup` has installed it, but the gate runs pytest from the repo
# root with no PYTHONPATH — so add the in-repo package for a local/dev run.
# Without this the whole Step 14 hook-package suite silently SKIPS via
# importorskip, and a skipped test protects nothing.
_IDP_COMMON_DIR = _SDLC_DIR.parent.parent / "lib" / "idp_common_pkg"
if _IDP_COMMON_DIR.is_dir() and str(_IDP_COMMON_DIR) not in sys.path:
    sys.path.append(str(_IDP_COMMON_DIR))


@pytest.fixture
def cbd():
    """Import (and reset per-test global state on) the harness module."""
    import codebuild_deployment as module

    # These module-level primitives are process-global; clear them so a test
    # that sets ABORT_TESTS / never_abort can't leak into the next test.
    module.ABORT_TESTS.clear()
    if hasattr(module._thread_local, "never_abort"):
        del module._thread_local.never_abort
    yield module
    module.ABORT_TESTS.clear()
    if hasattr(module._thread_local, "never_abort"):
        del module._thread_local.never_abort


FIRST_PARTY_UNDER_TEST = (
    "idp_common",
    "idp_sdk",
)


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
