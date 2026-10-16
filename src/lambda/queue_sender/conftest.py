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

# --- No outbound AWS from this suite -----------------------------------------
# Measured in #1398: this suite issued requests that reached botocore's real send,
# signed with whatever credentials the machine could resolve, and nothing failed or
# reported it — each call sits inside a production `except Exception`. Stripping the
# AWS environment (which `make test` now does) stops a request only while no suite
# reinstates placeholder credentials and no test clears the environment; the refusal
# below is the half that cannot be undone from inside a test. One definition, walked
# up to rather than copied: scripts/tests/no_outbound_aws.py carries the reasoning,
# the seam and the measurements, and the ``integration``-marker carve-out.
def _no_outbound_aws_fixture():
    import sys as _sys
    from pathlib import Path as _Path

    for ancestor in _Path(__file__).resolve().parents:
        helper = ancestor / "scripts" / "tests"
        if not (helper / "no_outbound_aws.py").is_file():
            continue
        _sys.path.insert(0, str(helper))
        try:
            from no_outbound_aws import fixture

            return fixture()
        finally:
            _sys.path.pop(0)
    raise RuntimeError(
        "scripts/tests/no_outbound_aws.py not found above this file; it carries the "
        "control that stops this suite transacting with a real AWS account (#1398)"
    )


no_outbound_aws = _no_outbound_aws_fixture()
