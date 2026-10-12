# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards for two ways this suite silently stopped testing things.

**1. A test module must not replace a real installed package globally.**
Five modules under ``tests/unit/agents/`` did ``sys.modules["strands"] =
MagicMock()`` at import time, unconditionally and without ever restoring it.
``strands-agents`` is a real dependency of the ``[all]`` extra that both
``make dev`` and CI install, so this replaced a working package for every test
module imported *after* them. Consequences:

* 42 agentic tests guarded by ``from strands... import ...`` saw a MagicMock,
  reported "strands-agents package not installed", and skipped — permanently, in
  every environment. Skipped reads as green, so the agentic extraction path
  appeared covered while none of it executed.
* Whether the remaining agentic tests ran against the real library depended on
  **collection order**, which is why the same 18 tests passed under
  ``pytest tests/unit/extraction`` and failed under ``pytest tests/unit``.

Stubbing now happens once, in ``tests/conftest.py``, and only for packages that
are genuinely absent.

**2. A live-Bedrock test must be excluded from the default gate.** The gate runs
``-m "not integration"``. Two tests that call real Bedrock (5+1 parametrized
runs) carried only ``@pytest.mark.agentic``, which nothing filters on — so the
only thing standing between them and a billed CI run was the very stub that was
breaking everything else.
"""

from __future__ import annotations

import ast
import importlib
import os

import boto3
import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Packages a test module may never install a global stub for, because they are
#: real dependencies of an extra the dev/CI environments install.
_REAL_PACKAGES = ("strands", "bedrock_agentcore")

#: ``conftest.py`` owns the conditional stubbing and is therefore exempt.
_EXEMPT = {"conftest.py"}


def _py_files():
    for root, _dirs, files in os.walk(TESTS_DIR):
        if "__pycache__" in root:
            continue
        for fn in files:
            if fn.endswith(".py") and fn not in _EXEMPT:
                yield os.path.join(root, fn)


def _global_sysmodules_writes(path):
    """``sys.modules["<pkg>"] = ...`` at MODULE level (not inside a function).

    Module level is the part that matters: an assignment inside a test or fixture
    is at least scoped to something, while a module-level one fires at import and
    persists for the whole session. Parsed with ``ast`` so a mention in a comment
    or docstring cannot trigger it.
    """
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    hits = []
    for node in tree.body:  # module level only
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "modules"
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                name = target.slice.value
                root = name.split(".")[0]
                if root in _REAL_PACKAGES:
                    hits.append((name, node.lineno))
    return hits


@pytest.mark.unit
def test_the_file_sweep_is_not_vacuous():
    """Guard-the-guard: an empty walk would make the next test always pass."""
    files = list(_py_files())
    assert len(files) > 50, f"expected to sweep the test tree, found {len(files)} files"


@pytest.mark.unit
def test_no_module_globally_stubs_a_real_package():
    offenders = {}
    for path in _py_files():
        hits = _global_sysmodules_writes(path)
        if hits:
            offenders[os.path.relpath(path, TESTS_DIR)] = hits
    assert not offenders, (
        "these modules replace a REAL installed package in sys.modules at import "
        "time, which leaks into every module imported afterwards:\n"
        + "\n".join(f"  {f}: {h}" for f, h in offenders.items())
        + "\nLet tests/conftest.py stub conditionally instead, or scope the mock "
        "with patch.dict(sys.modules, ...) inside the test."
    )


@pytest.mark.unit
def test_strands_is_the_real_package_when_installed():
    """The outcome the guard above protects. If ``strands`` is importable at all,
    the suite must be running against it — not a mock that makes every agentic
    test skip while reporting success."""
    try:
        import importlib.util

        installed = importlib.util.find_spec is not None
        import strands
    except ImportError:
        pytest.skip("strands-agents genuinely not installed in this environment")
    assert installed
    assert not str(type(strands)).endswith("MagicMock'>"), (
        "strands is installed but the test suite replaced it with a MagicMock; "
        "42 agentic tests will report 'package not installed' and skip"
    )
    # A MagicMock answers any attribute, so a real submodule import is the check
    # that actually distinguishes them.
    from strands.types.agent import AgentInput  # noqa: F401


def _marker_names(node):
    """Marker names on a function decorator list (``pytest.mark.<name>``)."""
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        # pytest.mark.integration  ->  Attribute(attr='integration')
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _modules_that_reach_real_bedrock():
    """Modules whose moto setup lets Bedrock calls through to real AWS.

    Detected from the ``passthrough`` URL config rather than from a marker,
    because the marker is exactly the thing that goes missing.
    """
    out = []
    for path in _py_files():
        if os.path.abspath(path) == os.path.abspath(__file__):
            continue  # this module names the pattern in order to detect it
        with open(path) as fh:
            src = fh.read()
        # All three: a moto context, a URL passthrough list, and bedrock in it.
        # Requiring all three keeps a module that merely mentions "bedrock" out.
        if "mock_aws" in src and '"passthrough"' in src and "bedrock" in src:
            out.append((path, src))
    return out


@pytest.mark.unit
def test_live_bedrock_tests_are_excluded_from_the_default_gate():
    """The gate runs ``-m "not integration"``, so a live-Bedrock test without
    that marker is billed on every run.

    ``agentic`` is NOT sufficient: in this suite it means "needs the real
    ``strands`` package", which most tests under ``agentic_idp/`` legitimately do
    without calling a model. Only the ones that actually reach AWS need
    ``integration``.
    """
    offenders = {}
    for path, src in _modules_that_reach_real_bedrock():
        tree = ast.parse(src, filename=path)
        module_marks = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
            ):
                module_marks.add("pytestmark")
        bad = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name.startswith("test_")
            and "integration" not in _marker_names(node)
            and not module_marks
        ]
        if bad:
            offenders[os.path.relpath(path, TESTS_DIR)] = bad
    assert not offenders, (
        "these tests let Bedrock calls through to real AWS but are not marked "
        f"`integration`, so the default gate runs them: {offenders}"
    )


@pytest.mark.unit
def test_that_bedrock_sweep_found_the_known_live_module():
    """Guard-the-guard: if the detection stops matching, the test above passes
    for the wrong reason."""
    found = [os.path.basename(p) for p, _ in _modules_that_reach_real_bedrock()]
    assert "test_extraction.py" in found, (
        "expected to detect the live agentic extraction module; the passthrough "
        f"detection may have gone stale (found: {found})"
    )


@pytest.mark.unit
def test_only_one_pytest_ini_governs_this_package():
    """A second `pytest.ini` under `tests/` SHADOWED the package one.

    pytest picks its config by walking up from the test paths, so
    `lib/idp_common_pkg/tests/pytest.ini` won over
    `lib/idp_common_pkg/pytest.ini` for every path under `tests/`. The shadowing
    file had no `addopts`, so `-m "not integration"` never applied and the
    `agentic` marker was unregistered: a bare `pytest tests/...` collected and RAN
    the live-Bedrock tests. CI escaped only because run_all_tests.py passes `-m`
    explicitly on the command line.

    One config file, at the package root.
    """
    pkg = os.path.dirname(TESTS_DIR)
    stray = [
        os.path.relpath(os.path.join(root, fn), pkg)
        for root, _dirs, files in os.walk(TESTS_DIR)
        for fn in files
        if fn in ("pytest.ini", "setup.cfg", "tox.ini")
    ]
    assert not stray, (
        f"config file(s) under tests/ shadow the package pytest.ini: {stray}. "
        f"pytest resolves config by walking up from the test path, so these win "
        f"and silently drop addopts (including -m 'not integration')."
    )
    assert os.path.exists(os.path.join(pkg, "pytest.ini")), (
        "the package-level pytest.ini is missing"
    )


def _root_conftest_fixture(name):
    """Return the ``ast.FunctionDef`` for a fixture in ``tests/conftest.py``."""
    path = os.path.join(TESTS_DIR, "conftest.py")
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


@pytest.mark.unit
def test_integration_marked_tests_get_real_credentials(request):
    """The credential reconciliation must reach every directory, not one.

    ``tests/conftest.py`` forces sentinel credentials
    (``AWS_ACCESS_KEY_ID=testing``) so unit tests cannot reach AWS. Undoing that
    for the tests that DO need AWS used to live in
    ``tests/integration/conftest.py``, so it reached integration tests by virtue
    of the directory their file sat in. ``make test-integration`` runs
    ``pytest -m "integration"`` over the whole tree, and two integration-marked
    tests live under ``tests/unit/`` — the live-Bedrock agentic pair, which need
    the ``strands`` guard in the agentic conftest — so those two signed real
    Bedrock calls with the sentinel key and failed with ``InvalidClientTokenId``
    (#1307).

    This test is itself under ``tests/unit/``, so its own fixture closure is the
    evidence: the reconciliation is visible here, which it could not have been
    while it lived one directory over.
    """
    assert "real_aws_credentials_for_integration_tests" in request.fixturenames, (
        "the integration credential reconciliation is not in scope for tests "
        "under tests/unit/. If it moved back into a subdirectory conftest, the "
        "integration-marked tests outside that subdirectory lose it again."
    )


@pytest.mark.unit
def test_the_credential_reconciliation_is_keyed_on_the_marker():
    """It must gate on the ``integration`` marker and restore on teardown.

    Gating is what makes a suite-wide autouse fixture safe: without it, the
    session-scoped skip it replaced would skip all ~10,500 tests on a machine
    with no AWS credentials. Restoring is what makes a mixed run safe — a bare
    ``pytest`` with no ``-m`` interleaves both tiers, and the previous version
    popped the sentinels and left them popped, so every moto test after an
    integration test would sign with whatever the credential chain resolved.
    (Doing it at module scope is worse again — #988.)
    """
    fixture = _root_conftest_fixture("real_aws_credentials_for_integration_tests")
    assert fixture is not None, (
        "tests/conftest.py no longer defines the reconciliation fixture"
    )
    # `ast.unparse` normalizes string literals to single quotes, so match on
    # that spelling rather than on however the source happens to quote it.
    src = ast.unparse(fixture)
    assert "get_closest_marker('integration')" in src, (
        "the fixture is autouse over the whole suite but no longer gates on the "
        "`integration` marker, so it applies to every unit test too"
    )
    assert any(isinstance(node, ast.Try) for node in ast.walk(fixture)), (
        "the fixture no longer restores the credential environment on teardown; "
        "a bare `pytest` run would leave real credentials in force for the moto "
        "tests that follow an integration test"
    )


@pytest.mark.unit
def test_chain_resolved_credentials_are_not_frozen_into_the_environment():
    """Credentials reachable through boto3's chain must stay with the chain.

    Writing the frozen access key, secret and session token into ``os.environ``
    pins a token for the session. Under an SSO or assume-role profile a long
    ``make test-integration`` run can outlive that snapshot and start failing
    with ``ExpiredToken`` partway through, where the live chain would have
    renewed it. So the resolver reads the frozen values only to answer "are
    there credentials here" and must then discard them, returning an empty
    mapping: popping the sentinels is the whole job.

    Driven through the real resolver with a fake chain, because the distinction
    is one line and invisible in a passing integration run with a long-lived key.
    """
    conftest = importlib.import_module("tests.conftest")

    class _FakeFrozen:
        access_key = "AKIAFAKECHAINKEY"  # pragma: allowlist secret
        secret_key = "fake-chain-secret"  # pragma: allowlist secret
        token = "fake-chain-token"  # noqa: S105 # pragma: allowlist secret

    class _FakeCreds:
        def get_frozen_credentials(self):
            return _FakeFrozen()

    class _FakeSession:
        def get_credentials(self):
            return _FakeCreds()

    saved_env = conftest._REAL_AWS_ENV.copy()
    saved_memo = (conftest._CREDENTIAL_OVERRIDES, conftest._CREDENTIAL_RESOLUTION_DONE)
    saved_session = boto3.Session
    try:
        # No exported credentials, so the resolver must consult the chain.
        conftest._REAL_AWS_ENV.update(
            dict.fromkeys(conftest._AWS_CREDENTIAL_VARS, None)
        )
        conftest._CREDENTIAL_OVERRIDES = None
        conftest._CREDENTIAL_RESOLUTION_DONE = False
        boto3.Session = _FakeSession

        overrides = conftest._resolve_credential_overrides()
    finally:
        boto3.Session = saved_session
        conftest._REAL_AWS_ENV.clear()
        conftest._REAL_AWS_ENV.update(saved_env)
        conftest._CREDENTIAL_OVERRIDES, conftest._CREDENTIAL_RESOLUTION_DONE = (
            saved_memo
        )

    assert overrides == {}, (
        "chain-resolved credentials were written into the environment "
        f"({sorted(overrides or {})}), which pins a token for the session. The "
        "frozen values are for the existence check only."
    )


@pytest.mark.unit
def test_the_reconciliation_drops_cached_boto3_clients():
    """Swapping the credential environment does not reach an existing client.

    boto3 freezes credentials at construction, and `idp_common.s3`,
    `idp_common.metrics` and four other modules cache one in a process-global.
    Collection imports every test module while the sentinels are in force, so a
    client built at import time is poisoned before any test runs — measured as
    `InvalidAccessKeyId` on an S3 read in `-m integration -k payslip` with the
    whole tree collected, where the same test alone passes (#1307, and the
    import-time-environment class is #988).

    Driven here against a real cached client rather than against the source, so
    the detection is what is tested: a `boto3.client` needs no network to build.
    """
    from idp_common import s3 as idp_s3

    conftest = importlib.import_module("tests.conftest")

    saved = idp_s3._s3_client
    try:
        idp_s3._s3_client = boto3.client("s3", region_name="us-east-1")
        cleared = conftest.reset_cached_aws_clients()
        assert cleared >= 1, (
            "the reset found no cached boto3 client to clear, so its detection "
            "has stopped matching the caches it is for"
        )
        assert idp_s3._s3_client is None, (
            "idp_common.s3 still holds a client built under the previous "
            "credentials; an integration test would inherit it"
        )
    finally:
        idp_s3._s3_client = saved

    fixture = _root_conftest_fixture("real_aws_credentials_for_integration_tests")
    assert fixture is not None
    body = ast.unparse(fixture)
    assert body.count("reset_cached_aws_clients()") == 2, (
        "the fixture must drop cached clients on BOTH edges: entering, so a "
        "client built under the sentinels is not reused with real credentials, "
        "and leaving, so a client built under real credentials is not inherited "
        "by the moto tests that follow"
    )


@pytest.mark.unit
def test_unit_tests_still_run_under_sentinel_credentials():
    """The live negative for the two above: this test is not integration-marked,
    so the reconciliation must have left the sentinels alone."""
    assert os.environ.get("AWS_ACCESS_KEY_ID") == "testing", (
        "a non-integration test is running with non-sentinel AWS credentials "
        f"({os.environ.get('AWS_ACCESS_KEY_ID')!r}); moto tests could reach AWS"
    )


@pytest.mark.unit
def test_the_default_marker_filter_is_actually_in_effect(pytestconfig):
    """Guard-the-guard for the above: assert the running session really excludes
    `integration`, whatever config file was chosen. Reads the live session rather
    than the file, so it fails if `addopts` is dropped again by any route."""
    markexpr = pytestconfig.getoption("markexpr") or ""
    assert "not integration" in markexpr.replace('"', "").replace("'", ""), (
        f"the session is not excluding integration-marked tests (markexpr="
        f"{markexpr!r}); live-Bedrock tests would run"
    )
