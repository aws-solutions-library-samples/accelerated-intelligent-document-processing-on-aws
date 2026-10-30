# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards for four ways this suite silently stopped testing things.

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

**3. A unit test must not wait out a real retry or poll ladder.** Two tests in
``tests/unit/lambdas/test_data_mart_rollup.py`` each patched one boundary of
``_run_reconcile`` and left the other real, so each reached ``_wait_for_athena``
against a MagicMock Athena client that never reports a terminal state and slept
1 s per poll to that function's 300 s default. The ``TimeoutError`` that
followed was caught by the production code's own ``except Exception``, so both
tests passed. 300.1 s each, measured serially, against 1.2 s for the other 197
tests in that module put together (#1380). ``tests/conftest.py``'s
``bounded_real_sleep`` now fails a unit test on the sleep that would carry it
past a one-second budget, before that sleep is served.

**4. The guard for (1) was here, and looked at a two-name list.** Lesson 1's
check shipped with ``_REAL_PACKAGES = ("strands", "bedrock_agentcore")`` — the
two packages that had already caused a problem. ``munkres``, the
Hungarian-assignment library the pinned ``stickler-eval`` depends on, was being
stubbed at module scope in ``tests/unit/evaluation/test_evaluation_service.py``,
and the check never looked at it. Six Stickler tests failed whenever that module
and the two Stickler suites landed in the same process — four reporting ``fd: 0``
where four false discoveries were expected, two as golden drift — which made CI
red on **every** branch while several local configurations passed, because
whether the poisoned process is the one that runs the Stickler files depends on
pytest's rootdir and on how xdist distributes them.

So the universe is now **derived**: any module-level ``sys.modules["X"]`` where
``X`` resolves in this environment. A name that resolves to nothing (``index``,
``cfnresponse``, ``idp_common_pkg`` — handler siblings and paths that are not
packages) is a stub standing in for something absent, which is the legitimate use.
The two original names are asserted to still be *covered*, from distribution
metadata rather than from the derivation itself, so a derived rule that narrowed
what the hand-written list reached fails rather than reading as a pass. The
general lesson is the one this file is accumulating: a control whose scope is a
list of the instances that have already bitten is a control over the past.
"""

from __future__ import annotations

import ast
import importlib
import os
import time

import boto3
import pytest

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The two packages this guard was written for. They are asserted to still be
#: *covered* rather than used as the universe: the universe is derived below, because
#: a hand-written list is only as good as whoever last remembered to extend it, and
#: this one was not extended. ``munkres`` — the Hungarian-assignment library the
#: pinned ``stickler-eval`` depends on — was stubbed at module scope in
#: ``tests/unit/evaluation/test_evaluation_service.py`` for as long as that file has
#: existed, and took six Stickler tests down with it whenever the two landed in the
#: same process. This guard was already here and did not look at it.
_GUARD_WAS_WRITTEN_FOR = ("strands", "bedrock_agentcore")


def _is_importable(name: str) -> bool:
    """Whether ``name``'s top-level package resolves in THIS environment.

    This is the derived universe: any name that resolves is a real module some other
    test can legitimately import, so replacing it process-wide is the defect. A name
    that does not resolve (``index``, ``cfnresponse``, ``idp_common_pkg`` — handler
    siblings and paths that are not packages) is a stub standing in for something
    absent, which is the legitimate use and is left alone.

    ``find_spec`` imports parent packages, so it is called on the top-level name only
    and its failures are swallowed: an environment where the probe itself raises must
    not decide the verdict.
    """
    import importlib.util

    root = name.split(".")[0]
    try:
        return importlib.util.find_spec(root) is not None
    except (ImportError, ValueError):
        return False


def _distribution_provides(name: str) -> bool:
    """Whether an installed distribution ships ``name`` as a top-level package.

    Read from ``importlib.metadata``, which is an independent source from
    ``find_spec`` — this exists so the coverage assertion below is not the derivation
    confirming itself. ``packages_distributions`` maps import name to distribution
    names and so answers exactly the question asked.
    """
    import importlib.metadata

    try:
        mapping = importlib.metadata.packages_distributions()
    except Exception:  # pragma: no cover - metadata unreadable
        return False
    return bool(mapping.get(name.split(".")[0]))


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
                if _is_importable(name):
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
def test_the_guard_covers_what_it_was_written_for():
    """The universe above is derived, so this pins that the derivation did not
    *narrow* it. Both original names must still be recognised as real packages when
    they are installed — a derived rule that stopped resolving them would read as a
    pass while covering less than the hand-written list it replaced.

    Installedness is read from the **distribution metadata**, not from
    ``find_spec``, so this is not the derivation checking itself. That also sidesteps
    a trap: ``tests/conftest.py`` stubs these two into ``sys.modules`` when they are
    genuinely absent, and ``find_spec`` on a module already in ``sys.modules`` with
    no ``__spec__`` raises ``ValueError`` rather than answering — so asking
    ``find_spec`` here would fail on precisely the environment where there is nothing
    to protect.
    """
    for name in _GUARD_WAS_WRITTEN_FOR:
        if not _distribution_provides(name):
            pytest.skip(f"no installed distribution provides {name} here")
        assert _is_importable(name), (
            f"an installed distribution provides the top-level package {name!r}, but "
            "the derived universe does not recognise it, so a module-level "
            "sys.modules stub over it would no longer be reported. This guard named "
            "it explicitly before the universe was derived; the derivation must "
            "cover at least as much."
        )


@pytest.mark.unit
def test_a_stub_over_an_installed_package_is_detected():
    """Guard-the-guard, positively: the sweep above reports nothing today, so
    without this the whole check is indistinguishable from one that cannot fire.

    The probe is written against a package that IS installed here and is reached by
    another suite — ``munkres``, which Stickler uses to pair list items — because
    that is the exact shape that went undetected: a module-level stub over a real
    transitive dependency, in a file whose own tests pass either way.
    """
    import importlib.util
    import tempfile

    if importlib.util.find_spec("munkres") is None:
        pytest.skip("munkres not installed; the probe needs a real package to shadow")

    source = (
        "import sys\n"
        "from unittest.mock import MagicMock\n"
        'sys.modules["munkres"] = MagicMock()\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(source)
        probe = fh.name
    try:
        hits = _global_sysmodules_writes(probe)
        assert hits == [("munkres", 3)], (
            "the module-level sys.modules detector did not report a stub over an "
            f"installed package; got {hits}. Every other assertion in this group is "
            "vacuous while that is true."
        )
        # And the opposite direction: a stub over a name that resolves to nothing is
        # the legitimate case and must stay unreported, or the guard would fail every
        # Lambda handler suite that stands in for its own `index` sibling.
        absent = source.replace("munkres", "a_module_that_does_not_exist_anywhere")
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(absent)
            probe_absent = fh.name
        try:
            assert _global_sysmodules_writes(probe_absent) == []
        finally:
            os.unlink(probe_absent)
    finally:
        os.unlink(probe)


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
        access_key = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret
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


# ---------------------------------------------------------------------------
# 3. A unit test must not wait out a real retry or poll ladder.
# ---------------------------------------------------------------------------
# `tests/conftest.py`'s `bounded_real_sleep` is the tripwire. The tests below
# pin the properties that make it work at all — it is in force for unit tests,
# it refuses an oversized sleep without serving it, it accumulates across a
# ladder of small sleeps, its failure escapes an `except Exception`, it stands
# aside for `integration`-marked tests, and the budget itself stays small
# enough to catch a ladder — because a tripwire that is installed and silently
# inert is worse than none: it reads as protection while the next incomplete
# mock sleeps for five minutes and passes.


@pytest.mark.unit
def test_the_sleep_budget_is_in_scope_for_unit_tests(request):
    """Its own fixture closure is the evidence, the same way the credential
    reconciliation above is pinned. If the fixture moves into a subdirectory
    conftest, every unit test outside that subdirectory loses it."""
    assert "bounded_real_sleep" in request.fixturenames, (
        "the sleep budget is not in scope for tests under tests/unit/; a test "
        "that reaches an unmocked retry ladder will wait out its timeout and "
        "then pass"
    )


@pytest.mark.unit
def test_an_oversized_sleep_is_refused_before_it_is_served(bounded_real_sleep):
    """The refusal has to happen BEFORE the sleep, not after.

    Deciding afterwards would make the failure loud and leave the cost exactly
    where it was — the 300 s this exists to stop would still be spent, and the
    suite would go from slow-and-green to slow-and-red.
    """
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="sleep budget"):
        time.sleep(bounded_real_sleep.limit + 10)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, (
        f"the oversized sleep was served before the budget refused it "
        f"({elapsed:.2f}s elapsed)"
    )


@pytest.mark.unit
def test_the_sleep_budget_accumulates_across_one_test(bounded_real_sleep):
    """A poll ladder's individual sleeps are small — `_wait_for_athena` asks
    for 1 s at a time — so a per-call limit would never fire on the case this
    was written for. The budget is per test, over the sum."""
    bounded_real_sleep.limit = 0.06
    time.sleep(0.02)
    time.sleep(0.02)
    assert bounded_real_sleep.spent == pytest.approx(0.04)
    with pytest.raises(pytest.fail.Exception):
        time.sleep(0.03)


@pytest.mark.unit
def test_the_sleep_budget_failure_escapes_an_except_exception(bounded_real_sleep):
    """The property that makes the tripwire loud rather than merely fast.

    The call path it was written for raises inside a production
    `except Exception` that files the error in a result dict and returns
    normally (`_run_reconcile`'s daily self-heal arm). An `Exception` raised
    from the sleep would be caught right there: the test would get five
    minutes faster and stay exactly as silent, which is the half of #1380 that
    was expensive. `pytest.fail` raises from `BaseException` instead.
    """
    assert not issubclass(pytest.fail.Exception, Exception), (
        "the sleep-budget failure is now an Exception subclass, so production "
        "code under test that catches Exception will swallow it"
    )
    caught = None
    try:
        time.sleep(bounded_real_sleep.limit + 10)
    except Exception as exc:  # noqa: BLE001 - the point of the test
        caught = exc
    except pytest.fail.Exception:
        pass
    else:
        pytest.fail("the sleep budget did not fire at all")
    assert caught is None, f"an `except Exception` swallowed the refusal ({caught!r})"


@pytest.mark.unit
def test_the_sleep_budget_stands_aside_for_integration_tests():
    """Keyed on the marker, like the credential reconciliation it sits beside.

    An `integration`-marked test polls real AWS — a stack update, an Athena
    query, a Step Functions execution — and those waits are the test. Keying on
    the directory instead would miss the integration-marked tests that live
    under `tests/unit/`.
    """
    fixture = _root_conftest_fixture("bounded_real_sleep")
    assert fixture is not None, (
        "tests/conftest.py no longer defines the sleep budget fixture"
    )
    src = ast.unparse(fixture)
    assert "get_closest_marker('integration')" in src, (
        "the sleep budget is autouse over the whole package but no longer keys "
        "on the `integration` marker, so a real AWS poll now fails at 5s"
    )
    assert "monkeypatch.setattr(time, 'sleep'" in src, (
        "the fixture no longer intercepts time.sleep, so it cannot see a sleep at all"
    )


@pytest.mark.unit
def test_the_sleep_budget_is_too_small_to_let_a_ladder_through():
    """A budget raised far enough reads as installed and refuses nothing.

    Found by mutation sampling: of the mutations aimed at
    ``bounded_real_sleep``, every one that removed or inverted part of the
    decision was killed by the tests above, and the single survivor was
    ``_SLEEP_BUDGET_SEC`` set to a million — the tripwire still present, still
    autouse, still intercepting every sleep, and permissive enough to serve the
    300 s poll it exists to stop. The tests above all phrase their oversized
    sleep as ``limit + 10``, so they follow the budget wherever it goes and
    cannot see that.

    The band is deliberately wide, because which number inside it is a
    judgement about this suite and the conftest is where that judgement is
    recorded. What is pinned is the order of magnitude: the ladders in this
    repository time out in minutes, so putting the budget on that scale has to
    be a deliberate edit with a reason beside it rather than a one-character
    change nothing notices.
    """
    path = os.path.join(TESTS_DIR, "conftest.py")
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_SLEEP_BUDGET_SEC"
            for t in node.targets
        )
    ]
    assert len(found) == 1, (
        f"expected exactly one _SLEEP_BUDGET_SEC assignment in tests/conftest.py, "
        f"found {len(found)}"
    )
    budget = ast.literal_eval(found[0])
    assert 0 < budget <= 10, (
        f"the unit-test sleep budget is {budget}s. Above ~10s it stops being a "
        f"tripwire on an unmocked retry ladder: the ladders in this repository "
        f"time out in minutes, and a test that waits one out passes while "
        f"reporting nothing (#1380). If a unit test genuinely needs to sleep "
        f"longer than this, patch `time.sleep` in that test and assert on the "
        f"delays it was asked for instead."
    )


@pytest.mark.unit
def test_a_sleep_in_a_forked_child_is_served_rather_than_refused(bounded_real_sleep):
    """A forked child inherits the wrapper, and must not be budgeted by it.

    `tests/unit/rule_validation/test_rule_validation_chunking.py` provokes a
    child that never finishes by asking it to sleep 30 s and killing it after
    1, so that 30 s is never spent and the parent's `join(timeout)` is the real
    bound. `pytest.fail` inside a child is not a test failure — it is a
    non-zero exit, which that harness reads as the child having *died* rather
    than having *hung*, which is a distinction it exists to draw. Budgeting the
    child broke it, and did so only when the whole package ran, which is the
    expensive way to find out.
    """
    bounded_real_sleep.limit = 0.01
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            time.sleep(0.05)
        except BaseException:  # noqa: BLE001 - reported to the parent as a status
            code = 17
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status), f"the forked child did not exit cleanly ({status})"
    assert os.WEXITSTATUS(status) == 0, (
        "the sleep budget refused a sleep in a forked child; a harness that "
        "distinguishes a hung child from a dead one now gets the wrong answer"
    )
    # ... and the parent is still budgeted, so the pid check narrows the scope
    # rather than turning the tripwire off.
    with pytest.raises(pytest.fail.Exception):
        time.sleep(0.05)
