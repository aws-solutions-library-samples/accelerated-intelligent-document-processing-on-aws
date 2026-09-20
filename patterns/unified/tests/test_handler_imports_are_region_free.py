# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A handler module that a test suite imports must import without an AWS region.

A Lambda handler that builds its boto3 clients at module scope is *correct* in
production — the runtime always sets ``AWS_REGION``, and a warm invocation then
reuses the client. That is the deliberate convention across every handler tree in
this repository, and nothing in this file asks for those to change. (No count is
quoted, because a count of them is a number nothing checks: the last one written
down here was measured on one release and was already wrong on the next.)

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

Two spellings count as "names", and both have to, because the repository uses
both. A test may name the file (``.../bda_processresults_function/index.py``,
loaded with ``spec_from_file_location``) or the **package directory**
(``.../pipeline_hooks_function``, put on ``sys.path`` and then imported by module
name). A pattern that required a ``.py`` suffix read as a census and was a
sample: it found 5 modules and missed two handlers that a suite loads by
directory, both of which raised ``NoRegionError`` on import. So a directory
reference expands to every runtime ``.py`` file directly inside it, and
``test_discovery_matches_a_deliberately_crude_second_pass`` cross-checks the
result against a second extraction that looks only for the marker string and
knows nothing about suffixes — a non-empty assertion cannot tell partial
discovery from complete discovery, and that is the failure this whole file exists
to make impossible elsewhere.
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

#: Any path-like token naming something under ``patterns/unified/src`` — a file
#: **or** a package directory. Deliberately does NOT require a ``.py`` suffix:
#: two suites name the directory and then import by module name after a
#: ``sys.path`` insert, and a suffix-requiring pattern silently dropped both.
#: ``_expand`` decides which of the two a match is by asking the filesystem.
_SRC_REF = re.compile(r"patterns/unified/src/([A-Za-z0-9_./-]+)")

#: The same thing found a second, deliberately stupider way: every occurrence of
#: the marker, taken to the end of the quoted string it sits in. It knows nothing
#: about suffixes or directories, so it cannot share a bug with ``_SRC_REF``, and
#: ``test_discovery_matches_a_deliberately_crude_second_pass`` fails if the two
#: disagree about which paths exist in the tree.
_SRC_MARKER = re.compile(r"patterns/unified/src/([^\"'\s]*)")

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
    """Every ``test_*.py`` the repository would commit.

    ``--others --exclude-standard`` as well as ``--cached``, matching
    ``scripts/tests/repo_files.tracked_paths`` and ``scripts/discover_templates.sh``:
    a brand-new test that loads a handler should be covered before it is
    ``git add``ed, and ``.gitignore`` still keeps build output and local worktrees
    out. (Not imported from ``repo_files`` — that module lives in ``scripts/tests``,
    which is not on ``sys.path`` for this suite.)
    """
    out = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "*.py",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        REPO_ROOT / rel
        for rel in (p for p in out.split("\0") if p)
        if Path(rel).name.startswith("test_")
    ]


#: Files inside a referenced package directory that are not deployed handler code.
_NON_RUNTIME = re.compile(r"(^test_|^conftest\.py$|_test\.py$)")


def _expand(reference: str) -> set[str]:
    """One matched reference -> the handler module paths it stands for.

    A file reference is itself. A **directory** reference is every runtime
    ``*.py`` directly inside it, because that is what the suites which name a
    directory actually do: insert it on ``sys.path`` and then import by module
    name, so any module in it can be the one that runs at import time. Anything
    that is neither (a trimmed path, a prose mention) contributes nothing.
    """
    target = REPO_ROOT / reference
    if target.is_file() and target.suffix == ".py":
        return {reference}
    if target.is_dir():
        return {
            f"{reference}/{child.name}"
            for child in target.iterdir()
            if child.is_file()
            and child.suffix == ".py"
            and not _NON_RUNTIME.search(child.name)
        }
    return set()


def _references(pattern: re.Pattern[str]) -> set[str]:
    """Every ``patterns/unified/src/...`` reference ``pattern`` finds in a test.

    Excludes this module, which names the tree in prose rather than importing it,
    and would otherwise make both passes agree by counting its own docstring.
    """
    found: set[str] = set()
    for path in _test_files():
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in pattern.finditer(text):
            found.add(f"{SRC_REL}/{match.group(1)}")
    return found


def _resolvable(references: set[str]) -> set[str]:
    """Those references that name a real file or directory in the tree."""
    return {
        ref
        for ref in references
        if (REPO_ROOT / ref).is_file() or (REPO_ROOT / ref).is_dir()
    }


def _imported_handler_modules() -> list[str]:
    """Repo-relative handler modules under ``patterns/unified/src`` a test names.

    Derived from the tree rather than listed, so a handler that gains its first
    test is covered without editing this file — and so a handler whose test is
    deleted drops out instead of leaving a check that proves nothing.
    """
    found: set[str] = set()
    for reference in _references(_SRC_REF):
        found |= _expand(reference)
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
def test_discovery_matches_a_deliberately_crude_second_pass() -> None:
    """Two extractions must agree on which references exist in the tree.

    Non-emptiness cannot distinguish partial discovery from complete discovery,
    and partial discovery is the realistic failure: the first version of
    ``_SRC_REF`` required a ``.py`` suffix, so it found 5 modules, missed the two
    handlers a suite loads by **directory**, and passed. Both of those raised
    ``NoRegionError`` on import.

    The second pass reads to the end of the quoted string and asks the filesystem
    whether the result exists. It knows nothing about suffixes, files or
    directories, so it cannot share a bug with the structured pattern — which is
    the whole value of comparing them. Only *resolvable* references are compared:
    a mention in a comment resolves to nothing on both sides and is not a
    discovery failure.

    The comparison is **one-directional** on purpose. What must not happen is the
    structured pass missing a real path the crude pass sees — that is a silently
    skipped handler. The reverse is expected and benign: the crude pass runs to
    the closing quote, so a prose mention in double backticks yields
    ``assessment_function``\\`\\`` and resolves to nothing, while ``_SRC_REF``
    stops at the backtick and resolves the same mention to a real directory.
    Requiring equality would fail on that, which is a difference in trimming, not
    in coverage.
    """
    structured = _resolvable(_references(_SRC_REF))
    crude = _resolvable(_references(_SRC_MARKER))

    missed = crude - structured
    assert not missed, (
        f"the crude second pass finds {SRC_REL} reference(s) that _SRC_REF does "
        f"not: {sorted(missed)}. Each is a handler this module silently skips, "
        f"which is what a non-empty assertion cannot tell you. Widen _SRC_REF "
        f"rather than narrowing the crude pass — that is the direction the defect "
        f"goes in."
    )


@pytest.mark.unit
def test_a_directory_reference_expands_to_the_modules_in_it() -> None:
    """The expansion, exercised rather than assumed.

    ``_expand`` is the part of discovery that a suffix-requiring pattern did not
    have at all, so it needs its own coverage: a directory reference must yield
    the handler modules inside it, a file reference must yield itself, and
    anything that resolves to neither must yield nothing rather than a path that
    cannot be imported.
    """
    package = f"{SRC_REL}/pipeline_hooks_function"
    expanded = _expand(package)
    assert f"{package}/index.py" in expanded, (
        f"a directory reference did not expand to the handler in it: {expanded}"
    )
    assert all(ref.endswith(".py") for ref in expanded), expanded
    assert not any("/test_" in ref for ref in expanded), (
        f"expansion pulled in a non-runtime file: {sorted(expanded)}"
    )

    a_file = f"{SRC_REL}/bda_processresults_function/index.py"
    assert _expand(a_file) == {a_file}
    assert _expand(f"{SRC_REL}/no_such_thing_at_all") == set()


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
