# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolve `idp_common.s3_targets` from THIS tree, and supply placeholder credentials.

Two independent jobs, both of which have to happen before `index` is imported.

**Placeholder credentials.** `create_s3_signed_post_url` calls
`generate_presigned_post`, and signing a POST policy reads
`credentials.access_key` — so with no credentials resolvable botocore raises
`AttributeError: 'NoneType' object has no attribute 'access_key'` from
`botocore/auth.py` and the two "a legitimate upload still succeeds" tests fail.
Presigning is arithmetic over a local key: nothing is sent, no account is touched,
and any syntactically valid key pair produces a signature. `$(PYTEST_HERMETIC)`
takes the machine's real credentials away deliberately — a suite here that reached
a real endpoint should fail loudly rather than transact against whoever the
developer is signed in as — so the placeholders are reinstated here, in the suite
that needs them, with `setdefault` so a caller's own values win. The suite's
`index` fixture supplies the region the same way, via `monkeypatch.setenv`.

**Module resolution.** This function carries the `idp_common` layer, so its handler
imports
`idp_common.s3_targets` by module path — the same convention `config_scope` uses. In
a developer environment the package is often an editable install pointing at a
different worktree, so a bare `pytest` here imports another checkout's copy of the
library and the module under test may simply not exist there. The failure is
confusing (`ImportError: cannot import name 's3_targets'`) and says nothing about the
code being tested.

So the module is registered for real, by path, under its canonical dotted name, and
the `idp_common` package is resolved from this repository. Same
load-the-real-thing-by-path approach as `src/lambda/batch_pre_processor/conftest.py`
and `src/lambda/chat_with_document_processor/tests/conftest.py`, and for the same
reason: the module is stdlib-only (`os`, `re`, `typing`), so loading one file costs
nothing and adds no dependency on the library being installed.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "lib" / "idp_common_pkg").is_dir():
            return parent
    raise RuntimeError("repo root not found")


_PKG = _repo_root() / "lib" / "idp_common_pkg"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

# Drop any already-imported idp_common resolved from elsewhere, so the insert above
# is what takes effect rather than a cached module from another tree.
for _name in [
    n for n in sys.modules if n == "idp_common" or n.startswith("idp_common.")
]:
    _resolved = getattr(sys.modules[_name], "__file__", "") or ""
    if str(_PKG) not in _resolved:
        del sys.modules[_name]
