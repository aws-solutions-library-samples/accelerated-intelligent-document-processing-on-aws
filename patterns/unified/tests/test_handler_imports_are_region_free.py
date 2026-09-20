# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A handler module that a test suite imports must import without an AWS region.

A Lambda handler that builds its boto3 clients at module scope is *correct* in
production — the runtime always sets ``AWS_REGION``, and a warm invocation then
reuses the client. That is the deliberate convention here: 12 module-scope
constructions across 6 files under ``patterns/unified/src/``, and 76 across 41
files under ``src/lambda/``. Nothing in this file asks for those to change.

The rule this file enforces is narrower, and it is about *tests*. As soon as a
suite loads a handler module, that module's import-time side effects run during
**collection**, before any fixture or mock can intervene. ``ssm`` and
``bedrock-data-automation`` are regional-only services, so
``boto3.client("ssm")`` at module scope raises ``NoRegionError`` the moment the
suite is collected with no region configured — and
``make test-packages-cicd`` collects every offline suite with the AWS
environment stripped precisely so that an import-time AWS dependency is caught
(``scripts/tests/test_offline_suites_are_hermetic.py``, issue #988).

``bda_processresults_function/index.py`` was in that state and did not fail,
because the suite that loads it —
``lib/idp_common_pkg/tests/unit/assessment/test_bda_lambda_functions.py``, via
``spec_from_file_location`` at module scope — sits under a ``conftest.py`` that
``os.environ.setdefault``s ``AWS_DEFAULT_REGION``. That ``setdefault`` is a
legitimate escape hatch for the library suite, but it also undoes the stripping
for everything that suite imports, so the handler's import-time clients were
invisible to the gate that exists to find them. This file closes that hole from
the other side: it strips the environment itself, in a subprocess, for the
handler modules a test actually loads.

Why a subprocess. ``patterns/unified/tests`` already runs under
``$(PYTEST_HERMETIC)`` from ``make``, so the ambient environment is stripped
there — but a bare ``pytest patterns/unified/tests`` on a developer machine has
a region, and the check would pass without testing anything. Scrubbing
explicitly in a child process makes the result the same either way. Region is
scrubbed from every source botocore consults, including ``AWS_CONFIG_FILE`` and
``~/.aws/config``, since a configured profile supplies one just as an env var
does.

The list of modules is **derived**, not written down: it is every file under
``patterns/unified/src/`` that some ``test_*.py`` in the repository names. A
handler nobody imports keeps the module-scope convention and is not checked
here; one that gains a test is covered the moment the test lands.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PATTERN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PATTERN_ROOT.parents[1]
SRC_REL = "patterns/unified/src"

#: Environment variables that can supply a region or credentials. Mirrors the
#: floor in ``scripts/tests/test_offline_suites_are_hermetic.py`` — kept as a
#: local list rather than imported, because that module is a *gate on the
#: Makefile* and importing it here would couple a runtime check to a text check.
_AWS_ENV_VARS = (
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_EC2_METADATA_DISABLED",
)

#: Any path-like token naming a file under ``patterns/unified/src``. Matches the
#: two spellings the tests in this repo use — a slash-joined literal in a
#: ``Path`` / ``os.path.join`` expression, and the ``../../`` relative form in
#: ``lib/idp_common_pkg``'s loader.
_SRC_REF = re.compile(r"patterns/unified/src/([A-Za-z0-9_./-]+\.py)")

#: How many third-party wheels the child may stub before giving up. A Lambda
#: handler declares its runtime dependencies in its own ``requirements.txt``
#: (``pypdfium2``, ``mlflow``, …) and those are not test dependencies of this
#: repository, so a missing one is a fact about the developer's environment, not
#: about the handler. The child stubs each as it is reported missing and retries,
#: which keeps the question this file asks — "does importing it need a region?" —
#: separate from "is every wheel installed here?". The cap only stops a runaway
#: loop; 20 is far above the 1-2 any handler needs.
_MAX_STUBS = 20

#: First-party prefixes the child must NOT stub. Stubbing ``idp_common`` would
#: hide a broken ``PYTHONPATH`` and turn this into a check that always passes.
_NEVER_STUB = ("idp_", "patterns", "scripts")

_CHILD = """
import sys, importlib.util
from unittest.mock import MagicMock

NEVER_STUB = {never_stub!r}
stubbed = []
for _ in range({max_stubs} + 1):
    spec = importlib.util.spec_from_file_location("_under_test", {path!r})
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as exc:
        name = exc.name or ""
        if not name or name.startswith(NEVER_STUB):
            raise
        # MagicMock, not types.ModuleType: a handler may call into the dependency
        # at import time, and a bare module object raises AttributeError there.
        sys.modules[name] = MagicMock(name=name)
        stubbed.append(name)
        continue
    print("imported; stubbed=" + ",".join(stubbed))
    break
else:
    raise SystemExit("gave up after stubbing " + ",".join(stubbed))
"""


def _test_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        REPO_ROOT / rel
        for rel in (p for p in out.split("\0") if p)
        if Path(rel).name.startswith("test_")
    ]


def _imported_handler_modules() -> list[str]:
    """Repo-relative paths under ``patterns/unified/src`` that a test file names.

    Derived from the tree rather than listed, so a handler that gains its first
    test is covered without editing this file — and so a handler whose test is
    deleted drops out instead of leaving a check that proves nothing.
    """
    found: set[str] = set()
    for path in _test_files():
        if path.resolve() == Path(__file__).resolve():
            continue  # this module names the directory in prose, not as an import
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in _SRC_REF.finditer(text):
            candidate = f"{SRC_REL}/{match.group(1)}"
            if (REPO_ROOT / candidate).is_file():
                found.add(candidate)
    return sorted(found)


IMPORTED_HANDLER_MODULES = _imported_handler_modules()


def _scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _AWS_ENV_VARS}
    # A profile in ~/.aws/config supplies a region just as an env var does, so
    # point botocore at a path that does not exist rather than at the real file.
    env["AWS_CONFIG_FILE"] = "/nonexistent/aws/config"
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/nonexistent/aws/credentials"
    # The library under test is imported from this checkout, not from whatever an
    # editable install happens to point at.
    lib = str(REPO_ROOT / "lib" / "idp_common_pkg")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{lib}{os.pathsep}{existing}" if existing else lib
    return env


@pytest.mark.unit
def test_discovery_finds_the_handler_modules_tests_import() -> None:
    """A silent empty list would make the parametrised check below vacuous."""
    assert IMPORTED_HANDLER_MODULES, (
        f"no file under {SRC_REL} is named by any test_*.py in the repository, "
        f"which cannot be right — the reference pattern in _SRC_REF no longer "
        f"matches how the suites spell these paths, so this module checks nothing."
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", IMPORTED_HANDLER_MODULES)
def test_handler_module_imports_without_an_aws_region(rel_path: str) -> None:
    """Import the module in a child process with no region available anywhere."""
    program = _CHILD.format(
        never_stub=_NEVER_STUB,
        max_stubs=_MAX_STUBS,
        path=str(REPO_ROOT / rel_path),
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=_scrubbed_env(),
        cwd=REPO_ROOT,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"{rel_path} cannot be imported without an AWS region, and a test suite in "
        f"this repository imports it — so that suite depends on a region being "
        f"configured, and an import-time AWS call in this handler is invisible to "
        f"the hermetic-collection gate. Move the client construction into the "
        f"function that uses it, or delete it if nothing uses it.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr[-3000:]}"
    )
