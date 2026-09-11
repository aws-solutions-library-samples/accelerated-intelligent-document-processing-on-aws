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
