# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pytest configuration file for the IDP Common package tests.
"""

import importlib
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

#: The AWS credential variables this file forces to sentinels for the unit suite.
#: Named once because three things below have to agree about the set: the
#: snapshot, the sentinel assignment, and the integration reconciliation.
_AWS_CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SECURITY_TOKEN",
    "AWS_SESSION_TOKEN",
)

#: Whatever real credentials the invoking environment exported, captured BEFORE
#: the sentinels below overwrite them. The `aws_credentials` fixture assigns
#: unconditionally (not `setdefault`), so by the time any test runs, exported
#: real credentials are gone — and an integration test cannot get them back from
#: the environment. Recovering them from the boto3 profile/role chain only works
#: for a machine that HAS a profile or an instance role, which is why
#: `AWS_PROFILE=...` worked and the exported `AWS_ACCESS_KEY_ID` form documented
#: alongside it did not.
_REAL_AWS_ENV = {var: os.environ.get(var) for var in _AWS_CREDENTIAL_VARS}

# Set up AWS credentials and region BEFORE any imports that might use boto3
# This must be done at module load time, not in a fixture, because fixtures
# run after module imports and some code may initialize boto3 clients at import time
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Mock external dependencies that may not be available in test environments
# These mocks need to be set up before any imports that might use these packages


def _stub_if_absent(*module_names: str) -> None:
    """Install a MagicMock for each module ONLY when it is genuinely absent.

    This used to stub unconditionally, and for ``strands`` that was actively
    harmful: ``strands-agents`` is a real dependency of the ``[all]`` extra, which
    both ``make dev`` and CI install, so the stub replaced a package that WAS
    importable. Every agentic test guards itself with
    ``pytest.importorskip``-style detection (``from strands.types.agent import
    AgentInput``), that import saw a MagicMock without the attribute, and 42 tests
    reported "strands-agents package not installed" and skipped — permanently, in
    every environment, including CI.

    Skipped tests read as green. So the agentic extraction path had 42 tests that
    could never fail, which is the same failure mode as having no tests at all
    except that it looks covered. Stubbing only what is missing keeps the suite
    runnable in a bare environment while letting a properly installed one actually
    exercise the code.
    """
    for name in module_names:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except ImportError:
            sys.modules[name] = MagicMock()


_stub_if_absent(
    "strands",
    "strands.models",
    "strands.hooks",
    "strands.hooks.events",
)

# Agent submodules some test modules used to stub for themselves; centralized
# here so no single module can replace a real installed package for every module
# imported after it.
_stub_if_absent(
    "strands.agent",
    "strands.agent.conversation_manager",
    "strands.tools",
    "strands.tools.mcp",
)

# bedrock_agentcore (secure code execution) is not a test dependency, so in
# practice these are always stubbed — routed through the same helper so the rule
# is uniform rather than a second, differently-behaved mechanism.
_stub_if_absent(
    "bedrock_agentcore",
    "bedrock_agentcore.tools",
    "bedrock_agentcore.tools.code_interpreter_client",
)

# PIL module is now used directly for document conversion functionality
# No mocking needed as PIL is a required dependency for the OCR module


# Fail fast when the idp_common being tested is not the one in this checkout. The
# rationale, and why this is an error rather than a warning, is in the shared helper.
#
# This suite usually shadows the installed package by accident rather than by design:
# tests/ and tests/unit/ carry __init__.py, so pytest's prepend import mode inserts the
# PACKAGE ROOT on sys.path and the local idp_common wins. That is luck, not a control --
# it disappears if those files go away or import mode changes -- so the check is stated
# explicitly here instead of being left to infer.
# The guard is skipped only when the helper is genuinely ABSENT -- an export with no
# scripts/ tree. A blanket `except ImportError` around the import and the call was
# broader than that claim: it also swallowed a renamed symbol, or any ImportError raised
# from inside the helper, either of which turned the guard into a no-op in this suite
# with nothing printed and every test still green.
_GATE_DIR = Path(__file__).resolve().parents[3] / "scripts" / "tests"
if (_GATE_DIR / "first_party_provenance.py").is_file():
    sys.path.insert(0, str(_GATE_DIR))
    try:
        from first_party_provenance import assert_resolves_in

        assert_resolves_in("idp_common", __file__)
    finally:
        sys.path.pop(0)


@pytest.fixture(scope="session", autouse=True)
def aws_credentials():
    """Set up AWS credentials and region for testing."""
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"  # nosec B105 - dummy moto credential  # pragma: allowlist secret
    os.environ["AWS_SECURITY_TOKEN"] = "testing"  # nosec B105 - dummy moto credential
    os.environ["AWS_SESSION_TOKEN"] = "testing"  # nosec B105 - dummy moto credential
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["AWS_REGION"] = (
        "us-east-1"  # Also set AWS_REGION for code that checks this variable
    )


# --- No outbound AWS from this suite -----------------------------------------
# The credential fixture above makes the credentials harmless; this makes the request
# not happen, and both are wanted because they fail in different directions. A
# placeholder credential still produces a *signed request to a real endpoint*, which
# is rejected rather than prevented and which the code under test then swallows.
#
# Measured in #1398 over this package alone: 343 requests reached botocore's real send
# in one `pytest -m "not integration"` — 320 CloudWatch `PutMetricData`, the rest S3,
# SSM and Secrets Manager — with no test failing and nothing reported. One of them was
# signed with a real-shaped key even under the stripped runner, because
# `patch.dict(os.environ, ..., clear=True)` erases the two pins no unset can replace.
# The placeholders this file installs are deliberate (#988) and are exactly what the
# other 342 signed with, so stripping the environment cannot reach them.
#
# One definition, walked up to rather than copied: scripts/tests/no_outbound_aws.py
# carries the seam, the measurements and the ``integration``-marker carve-out, which
# is the same split `real_aws_credentials_for_integration_tests` below makes.
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

# ---------------------------------------------------------------------------
# Integration-tier credential reconciliation
# ---------------------------------------------------------------------------
# This reconciliation is keyed on the ``integration`` MARKER, not on the
# directory, and that is the whole point of it living here. It used to be an
# autouse session fixture in ``tests/integration/conftest.py``, so it reached
# integration tests by virtue of where their file sat. Two integration-marked
# tests live under ``tests/unit/`` (the live-Bedrock agentic extraction pair,
# which belong beside the agentic conftest that guards on a real ``strands``),
# and `make test-integration` runs ``pytest -m "integration"`` over this
# PACKAGE's tree — so those two were handed the sentinel ``testing`` key, signed
# real Bedrock calls with it and failed with ``InvalidClientTokenId``. The marker
# is what says "this test calls AWS"; the directory only says where someone filed
# it. Keyed on the marker, a misplaced file inside this package is covered on
# arrival.
#
# ⚠️ **The reach is this package, not the repository.** A conftest governs its own
# directory downwards, so two other trees hold integration-marked tests outside
# it, and they are outside it for different reasons — one reason each, because a
# single reason covering both is false of one of them:
#
#   * ``lib/idp_sdk/tests/integration/`` injects no sentinel credentials at all,
#     so there is nothing there to reconcile.
#   * ``feature-platform/confbench-testset/`` DOES inject them — its
#     ``conftest.py`` sets ``AWS_ACCESS_KEY_ID=testing`` and four siblings at
#     module scope. Nothing to reconcile *yet*: its one integration-marked test
#     (``tests/test_variants.py``, gated on ``CONFBENCH_NETWORK_TESTS=1``) calls
#     HuggingFace and makes no AWS call. ⚠️ An AWS-calling integration test added
#     to that tree WOULD be handed the sentinels, and would need a reconciliation
#     of its own — this comment is where somebody would come looking.
#
# The repo-wide claim belongs to the parity test, not to this fixture.
#
# Pinned by test_integration_marked_tests_get_real_credentials in
# tests/unit/test_suite_hygiene.py. See #1307.

_SENTINEL = "testing"

#: Memo for the probe below. It answers "do real credentials exist at all",
#: which is an existence question rather than a validity one, so caching it for
#: the session is safe even when the credentials themselves rotate.
#: ``None`` is a real answer (nothing available), so "not yet computed" needs its
#: own flag.
_CREDENTIAL_OVERRIDES: Optional[Dict[str, str]] = None
_CREDENTIAL_RESOLUTION_DONE = False


def _resolve_credential_overrides() -> Optional[Dict[str, str]]:
    """Return the env mapping an integration test needs, or None to skip it.

    ⚠️ **An empty mapping is a real answer, and the common one.** It means the
    sentinels merely have to be out of the way, and boto3's own chain — a
    profile, SSO, an instance role — resolves from there *and refreshes*.

    **Why ``{}`` rather than the frozen key material.** Writing a resolved
    access key, secret and session token into ``os.environ`` pins them for the
    session, so an SSO or instance-role token that expires mid-run fails with
    ``ExpiredToken`` instead of refreshing — and a ``make test-integration`` run
    is long enough to reach that. The frozen credentials are read here to answer
    "are there credentials at all" and are deliberately discarded; signing stays
    with the chain.

    So only one case writes anything: credentials the invoking environment
    exported itself, which the sentinel assignment at the top of this module
    overwrote and which nothing else can give back.
    """
    global _CREDENTIAL_OVERRIDES, _CREDENTIAL_RESOLUTION_DONE
    if _CREDENTIAL_RESOLUTION_DONE:
        return _CREDENTIAL_OVERRIDES

    overrides: Optional[Dict[str, str]] = None
    exported = {
        k: v for k, v in _REAL_AWS_ENV.items() if v is not None and v != _SENTINEL
    }
    if exported.get("AWS_ACCESS_KEY_ID"):
        # Hand back exactly what the environment had, including the absence of a
        # session token for a long-lived key.
        overrides = exported
    else:
        # Hide the sentinels so boto3 falls through to the profile/role chain,
        # then put the environment back exactly as it was. Leaving them popped
        # here is what made the previous session-scoped version unsafe to
        # generalise: a later moto test in the same session would then sign with
        # whatever the chain resolved. (Popping at module scope is worse still —
        # #988.) The fixture does the popping per test instead, and restores.
        saved = {var: os.environ.pop(var, None) for var in _AWS_CREDENTIAL_VARS}
        # Imported here rather than at module scope: this file runs before every
        # test module, and the sentinel assignment above has to land before
        # anything builds a boto3 client.
        import boto3

        try:
            creds = boto3.Session().get_credentials()
            frozen = creds.get_frozen_credentials() if creds else None
        except Exception:
            frozen = None
        finally:
            for var, val in saved.items():
                if val is not None:
                    os.environ[var] = val

        if frozen is not None and frozen.access_key not in (None, _SENTINEL):
            # Resolvable through the chain. Nothing to write — the frozen values
            # are read only to answer "are there credentials here", and are
            # deliberately discarded so signing keeps going through the chain.
            overrides = {}

    _CREDENTIAL_OVERRIDES = overrides
    _CREDENTIAL_RESOLUTION_DONE = True
    return overrides


def reset_cached_aws_clients() -> int:
    """Drop every module-level boto3 client/session/resource ``idp_common`` holds.

    Changing the credential environment is not enough on its own, because a
    boto3 client **freezes its credentials at construction**. Several modules
    cache one in a process-global — ``idp_common.s3._s3_client``,
    ``idp_common.metrics._cloudwatch_client``, the Bedrock session cache, the
    X-Ray/DynamoDB/CloudFormation/SSM helpers — so the first construction in the
    process wins for every test afterwards.

    The construction that wins is usually one nobody intended: pytest imports
    every test module during collection, and the sentinel credentials are in
    force then, so a client built at import time is poisoned before a single
    test has run. That is measurable rather than theoretical — collecting the
    whole tree and selecting one test (``-m integration -k payslip``, 10,494
    deselected) reproduces ``InvalidAccessKeyId`` on an S3 read, where the same
    test alone passes. Issue #1307; the import-time-environment class is #988.

    Every one of these caches is ``if _x is None: _x = boto3.client(...)``, so
    setting it to ``None`` costs one reconstruction and nothing else. Detection
    is by **value type** rather than by name: a hardcoded list of module globals
    rots silently the first time somebody adds a seventh cache, and a name
    pattern would also match unrelated caches like ``_settings_cache``.

    ⚠️ **What by-value detection does not cover.** It recognises a cache whose
    value IS a ``BaseClient``, ``Session`` or ``ServiceResource``. A cache
    holding a *wrapper* around one — a service class, a dataclass, a closure —
    is invisible to it, so by-value is rot-resistant for the shape these six
    caches have and not a general guarantee. A module that grows such a cache
    needs its own reset called from here.

    Where a module publishes its own reset, that is called instead of writing the
    attribute: ``idp_common.bedrock.session`` keeps ``_cached_region`` beside
    ``_cached_session`` under a lock, and an attribute write would leave the
    region populated next to a ``None`` session and bypass the lock.

    Returns the number of caches cleared, so a caller can assert it did
    something.
    """
    import boto3
    from boto3.resources.base import ServiceResource
    from botocore.client import BaseClient

    frozen_credential_holders = (BaseClient, boto3.Session, ServiceResource)
    cleared = 0
    for module_name, module in list(sys.modules.items()):
        if module_name != "idp_common" and not module_name.startswith("idp_common."):
            continue
        if module is None:
            continue

        purpose_built_reset = getattr(module, "reset_cached_session", None)
        if callable(purpose_built_reset):
            purpose_built_reset()
            cleared += 1
            continue

        for attr, value in list(vars(module).items()):
            # Module-private by convention: these caches are all `_`-prefixed,
            # and a public boto3 client at module scope would be someone's
            # deliberate singleton rather than a lazy cache.
            if attr.startswith("_") and isinstance(value, frozen_credential_holders):
                setattr(module, attr, None)
                cleared += 1
    return cleared


@pytest.fixture(autouse=True)
def real_aws_credentials_for_integration_tests(request):
    """Give every ``integration``-marked test real credentials, and only those.

    Function-scoped and restoring on teardown, so a run that spans both tiers —
    a bare ``pytest`` with no ``-m`` — leaves the sentinels in force for the
    moto tests either side of an integration test.

    The cached-client reset happens on **both** edges, and the second one is not
    symmetry for its own sake: without it a client built under real credentials
    during an integration test would be inherited by the moto tests that follow
    it, which is the same defect pointing the other way.
    """
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    overrides = _resolve_credential_overrides()
    if overrides is None:
        pytest.skip(
            "Integration tests require real AWS credentials. Configure a profile "
            "or export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (and "
            "AWS_SESSION_TOKEN if using temporary creds)."
        )

    saved = {var: os.environ.get(var) for var in _AWS_CREDENTIAL_VARS}
    for var in _AWS_CREDENTIAL_VARS:
        os.environ.pop(var, None)
    # Usually empty: popping the sentinels is the whole job, and boto3's chain
    # signs from there so a rotating credential keeps refreshing.
    os.environ.update(overrides)
    reset_cached_aws_clients()
    try:
        yield
    finally:
        for var in _AWS_CREDENTIAL_VARS:
            os.environ.pop(var, None)
        os.environ.update({k: v for k, v in saved.items() if v is not None})
        reset_cached_aws_clients()


# ---------------------------------------------------------------------------
# How long a unit test may spend in a real `time.sleep`
# ---------------------------------------------------------------------------
# A unit test that reaches an unmocked retry or poll ladder does not fail. It
# WAITS — for as long as the ladder's own timeout — and then usually passes,
# because the caller treats the resulting timeout as a tolerable error and
# records it rather than raising. Nothing reports anything unusual; the only
# symptom is a suite that goes quiet and stays there.
#
# Two tests in `tests/unit/lambdas/test_data_mart_rollup.py` were in that state
# (#1380). Each patched one boundary of `_run_reconcile` and left the other
# real, so the call reached `_wait_for_athena` against a MagicMock Athena client
# that never reports a terminal state, slept 1 s per poll to the function's
# `timeout_sec: int = 300` default, raised `TimeoutError`, and had it caught by
# the production code's own `except Exception`. 300.1 s per test, measured
# serially, against 1.2 s for the other 197 tests in that module combined.
#
# The shape generalises past those two: a poll or backoff default measured in
# minutes is correct for the Lambda that contains it (this one has a 900 s
# timeout) and is wrong by three orders of magnitude for a unit test, and
# nothing about an incomplete mock announces which one is running. So the
# budget below is a tripwire on the SYMPTOM rather than on any one ladder: the
# first sleep that would carry a test past it fails the test, before sleeping,
# naming the remedy.
#
# It intercepts `time.sleep` on the `time` module, so it sees a caller that did
# `import time; time.sleep(...)` — which is every ladder in this repository —
# and not one that bound the function with `from time import sleep`. Sleeps on
# other threads are counted against the same budget without a lock, since the
# figure is a budget rather than an assertion about concurrency.
#
# ⚠️ **A sleep in a FORKED CHILD is served, not budgeted**, and the pid check
# that does that is not a technicality. `tests/unit/rule_validation/
# test_rule_validation_chunking.py` provokes a child that never finishes, by
# asking it to sleep for 30 s and killing it after 1 — so the 30 s is never
# spent, and the parent's own `join(timeout)` is what bounds the cost. A fork
# inherits this wrapper, and `pytest.fail` in a child is not a test failure:
# it is a non-zero exit, which that harness reads as the child having DIED
# rather than having hung. Budgeting the child therefore turned a passing
# test into a wrong answer about a distinction it exists to draw. What this
# gives up is a ladder reached inside a child the parent joins with no
# timeout at all; that waits as long as it likes, as it did before.
#
# `integration`-marked tests wait on real AWS and are keyed off this the same
# way they are keyed off the sentinel credentials above: by marker, not by
# directory, because the marker is what says a test calls AWS.

#: Seconds of real `time.sleep` one unit test may spend in total.
#:
#: Set on a measurement rather than by feel: with the budget temporarily at
#: 0.25 s, nothing in this package's ~11,200 unit tests crosses it, so 1.0 is
#: four times the largest request any test makes today and three hundred times
#: under the poll ladder that prompted this. Raise it only against a
#: measurement, and prefer patching `time.sleep` in the test that needs the
#: room — the message below says so, because that is almost always the answer.
#:
#: The accounting is on the seconds a test ASKS for, not the seconds that
#: elapse, so this cannot go flaky on a loaded machine: the verdict is the same
#: on an idle host and a saturated one.
_SLEEP_BUDGET_SEC = 1.0


@dataclass
class SleepBudget:
    """Per-test sleep accounting, yielded by :func:`bounded_real_sleep`.

    ``limit`` is read on every `time.sleep` call rather than captured, so a
    test that needs a different bound for its own duration can set it — which
    is how ``test_suite_hygiene.py`` drives the cumulative path without
    actually waiting out the budget to prove it.
    """

    limit: float
    spent: float = 0.0


@pytest.fixture(autouse=True)
def bounded_real_sleep(request, monkeypatch):
    """Fail a unit test that spends more than `_SLEEP_BUDGET_SEC` asleep.

    Fails on the sleep that would cross the budget, BEFORE that sleep runs, so
    the test costs the budget and not the ladder's timeout.

    The failure is raised with `pytest.fail`, whose exception derives from
    `BaseException` and not from `Exception`. That is load-bearing rather than
    incidental: the call path this was written for sits inside a production
    `except Exception` that files the error in a result dict and returns
    normally, so an `Exception` here would be caught by the code under test and
    the run would get faster while staying just as silent.
    """
    if request.node.get_closest_marker("integration") is not None:
        yield None
        return

    real_sleep = time.sleep
    budget = SleepBudget(limit=_SLEEP_BUDGET_SEC)
    owner_pid = os.getpid()

    def _budgeted_sleep(seconds):
        if os.getpid() != owner_pid:
            # A forked child. Its lifetime belongs to whatever joined it, and a
            # `pytest.fail` here would reach the parent as an exit status.
            real_sleep(seconds)
            return
        try:
            requested = float(seconds)
        except (TypeError, ValueError):
            # Not a number — let the real `time.sleep` raise about it.
            real_sleep(seconds)
            return
        if requested > 0 and budget.spent + requested > budget.limit:
            pytest.fail(
                f"This unit test asked to sleep {requested:g}s on top of "
                f"{budget.spent:g}s already slept, which is past the "
                f"{budget.limit:g}s sleep budget for a unit test.\n"
                f"Almost always this means the test reached a real retry or "
                f"poll ladder because a mock does not cover the whole call "
                f"graph — the test would otherwise have waited out that "
                f"ladder's timeout and then passed, reporting nothing (#1380).\n"
                f"Patch the boundary the test does not intend to exercise. If "
                f"the backoff itself is what is under test, patch `time.sleep` "
                f"in the test and assert on the delays it was asked for, "
                f"rather than spending them.",
                pytrace=False,
            )
        budget.spent += max(requested, 0.0)
        real_sleep(seconds)

    monkeypatch.setattr(time, "sleep", _budgeted_sleep)
    yield budget


# ---------------------------------------------------------------------------
# Strands / boto3 binding check (#1756)
# ---------------------------------------------------------------------------
#
# A test module that replaces a library attribute when it is imported changes
# that attribute for every module collected after it in the same process, and
# collection imports every module before the first test runs. Three modules
# once replaced ``strands.tool`` with a passthrough this way, so every Strands
# tool defined later in the process was a plain function an Agent could not
# register, and the tests built on those tools went on passing.
#
# The rule checked is about what the names REFER TO, not about how a module
# spells the replacement. After each test module is imported, and after each
# test's teardown:
#
# * every public function or class of the source modules below must be the
#   object it was when this conftest loaded;
# * every name a first-party module bound from one of them (``from strands
#   import tool``, read from that module's own import statements) must still be
#   that object;
# * every function a first-party module defines at module level or in a class
#   body, whose outermost decorator EVALUATES (in that module's namespace) to the
#   strands tool decorator, must be a Strands tool. Evaluating the decorator,
#   rather than matching how it is spelled, covers ``@tool``, ``@strands.tool``,
#   ``@tools.tool``, an alias assigned in the module, the called form
#   ``@tool(name=...)``, and definitions inside module-level ``if``/``try``
#   blocks. This catches a module that was first imported while the decorator
#   was replaced, even by a replacement that was undone afterwards.
#
# A violation is reported against the module or test that caused it, and the
# originals are put back so the report does not cascade: a source attribute or
# binding is reassigned, and a plain tool function is re-decorated, including
# where another first-party module holds the same function by name.
#
# A replacement scoped to one test (``monkeypatch``, or ``unittest.mock.patch``
# as a context manager or decorator) is undone before the teardown check runs,
# so it passes, unless the test imported a tool-defining first-party module for
# the first time while the decorator was replaced. A fixture with a wider scope
# that keeps one of these replaced across tests is reported at the first test's
# teardown: patch for the duration you need instead.
#
# Not covered: attributes of library classes (``Agent.__init__``,
# ``BaseClient._make_api_call``); non-callable module state such as
# ``boto3.DEFAULT_SESSION``, which boto3 itself assigns lazily; modules outside
# the ``idp_common`` package, such as Lambda handlers loaded by path; a tool
# built by calling the decorator rather than decorating (``x = tool(fn)``);
# functions nested inside other functions; and a decorator reached through a
# first-party name that was itself assigned from the replacement while it was
# in place, which no longer evaluates to the real decorator.

#: The library modules whose public functions and classes are snapshotted.
_BINDING_SOURCE_MODULES = (
    "strands",
    "strands.tools",
    "strands.tools.decorator",
    "boto3",
)

#: Module-name prefix of the first-party code whose imported names are checked.
_FIRST_PARTY_PREFIX = "idp_common"


def _snapshot_binding_sources() -> Dict[tuple, Any]:
    import inspect
    import types

    snapshot: Dict[tuple, Any] = {}
    for source in _BINDING_SOURCE_MODULES:
        try:
            module = importlib.import_module(source)
        except ImportError:
            continue
        if not isinstance(module, types.ModuleType):
            continue  # a conftest stub (MagicMock): there is nothing real to keep
        for attr in dir(module):
            if attr.startswith("_"):
                continue
            value = getattr(module, attr, None)
            if inspect.isfunction(value) or inspect.isclass(value):
                snapshot[(source, attr)] = value
    return snapshot


_BINDING_SNAPSHOT = _snapshot_binding_sources()

#: (first-party module name, bound name) -> (source module, source attribute),
#: read from each first-party module's own ``from <source> import <name>``.
_FIRST_PARTY_BINDINGS: Dict[tuple, tuple] = {}
#: (first-party module name, attribute path) of functions whose outermost
#: decorator evaluated to the strands tool decorator when the module was scanned.
_FIRST_PARTY_TOOLS: list = []
_SCANNED_FIRST_PARTY: set = set()
#: Modules whose imports have been read but whose decorators are not resolved yet;
#: resolution waits until every binding has been put back.
_UNRESOLVED_FIRST_PARTY: list = []


def _decorated_definitions(tree) -> list:
    """(attribute path, outermost decorator) for every function defined at module
    level or in a class body, including inside module-level ``if``/``try``/
    ``with`` blocks. A function nested in another function has no stable object
    to check and is skipped."""
    import ast

    found = []

    def visit(statements, path):
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.decorator_list:
                    found.append(((*path, node.name), node.decorator_list[0]))
            elif isinstance(node, ast.ClassDef):
                visit(node.body, (*path, node.name))
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
                for block in ("body", "orelse", "finalbody"):
                    visit(getattr(node, block, []), path)
                for handler in getattr(node, "handlers", []):
                    visit(handler.body, path)

    visit(tree.body, ())
    return found


def _evaluate_decorator(decorator, namespace: dict):
    """The object a decorator expression names, or None.

    Only a name or an attribute chain is evaluated (the function of a call, for
    ``@tool(name=...)``); nothing is called."""
    import ast

    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    parts = []
    while isinstance(decorator, ast.Attribute):
        parts.append(decorator.attr)
        decorator = decorator.value
    if not isinstance(decorator, ast.Name) or decorator.id not in namespace:
        return None
    value = namespace[decorator.id]
    try:
        for part in reversed(parts):
            value = getattr(value, part)
    except Exception:  # noqa: BLE001 - an unreadable decorator is not a tool
        return None
    return value


def _scan_first_party_imports() -> None:
    import ast

    source_roots = {src.split(".")[0] for src in _BINDING_SOURCE_MODULES}
    for name, module in list(sys.modules.items()):
        if name in _SCANNED_FIRST_PARTY or not (
            name == _FIRST_PARTY_PREFIX or name.startswith(_FIRST_PARTY_PREFIX + ".")
        ):
            continue
        _SCANNED_FIRST_PARTY.add(name)
        path = getattr(module, "__file__", None)
        if not path or not path.endswith(".py"):
            continue
        try:
            source_text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        if not any(root in source_text for root in source_roots):
            continue
        tree = ast.parse(source_text)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and not node.level
                and node.module in _BINDING_SOURCE_MODULES
            ):
                for alias in node.names:
                    key = (node.module, alias.name)
                    if key in _BINDING_SNAPSHOT:
                        bound = alias.asname or alias.name
                        _FIRST_PARTY_BINDINGS[(name, bound)] = key
        _UNRESOLVED_FIRST_PARTY.append((name, _decorated_definitions(tree)))


def _resolve_first_party_tools(real_tool) -> None:
    while _UNRESOLVED_FIRST_PARTY:
        name, definitions = _UNRESOLVED_FIRST_PARTY.pop()
        module = sys.modules.get(name)
        if module is None:
            continue
        namespace = vars(module)
        for attr_path, decorator in definitions:
            if _evaluate_decorator(decorator, namespace) is real_tool:
                _FIRST_PARTY_TOOLS.append((name, attr_path))


def _lookup(module, attr_path):
    owner = module
    for part in attr_path[:-1]:
        owner = getattr(owner, part, None)
        if owner is None:
            return None, None
    return owner, vars(owner).get(attr_path[-1]) if hasattr(owner, "__dict__") else None


def _redecorate_everywhere(plain, decorated) -> None:
    """Replace a plain tool function wherever a first-party module holds it."""
    for name, module in list(sys.modules.items()):
        if name == _FIRST_PARTY_PREFIX or name.startswith(_FIRST_PARTY_PREFIX + "."):
            namespace = getattr(module, "__dict__", None) or {}
            for attr, value in list(namespace.items()):
                if value is plain:
                    setattr(module, attr, decorated)


def binding_problems(restore: bool = True) -> list:
    """Describe every replaced source attribute or first-party binding, and every
    first-party tool that is not a Strands tool.

    With ``restore`` the original objects are put back, so one offender is
    reported once rather than by every module and test after it.
    """
    import inspect

    problems = []
    for (source, attr), original in _BINDING_SNAPSHOT.items():
        module = sys.modules.get(source)
        if module is None:
            continue
        current = getattr(module, attr, None)
        if current is not original:
            problems.append(f"{source}.{attr} is {current!r}, not {original!r}")
            if restore:
                setattr(module, attr, original)
    _scan_first_party_imports()
    for (owner, bound), key in _FIRST_PARTY_BINDINGS.items():
        module = sys.modules.get(owner)
        if module is None or not hasattr(module, bound):
            continue
        current = getattr(module, bound)
        original = _BINDING_SNAPSHOT[key]
        if current is not original:
            problems.append(
                f"{owner}.{bound} (bound by `from {key[0]} import {key[1]}`) is "
                f"{current!r}, not {original!r}"
            )
            if restore:
                setattr(module, bound, original)
    tool_class = _BINDING_SNAPSHOT.get(
        ("strands.tools.decorator", "DecoratedFunctionTool")
    )
    real_tool = _BINDING_SNAPSHOT.get(("strands.tools.decorator", "tool"))
    if tool_class is None or real_tool is None:
        return problems
    _resolve_first_party_tools(real_tool)
    for owner, attr_path in _FIRST_PARTY_TOOLS:
        module = sys.modules.get(owner)
        if module is None:
            continue
        holder, current = _lookup(module, attr_path)
        if current is None or isinstance(current, tool_class):
            continue
        problems.append(
            f"{owner}.{'.'.join(attr_path)} is decorated with the strands tool "
            f"decorator but is {current!r}, not a Strands tool: the module was "
            f"imported while the decorator was replaced"
        )
        if restore and inspect.isfunction(current):
            decorated = real_tool(current)
            setattr(holder, attr_path[-1], decorated)
            _redecorate_everywhere(current, decorated)
    return problems


def _binding_failure_text(where: str, problems: list) -> str:
    remedies = []
    if any("not a Strands tool" in p for p in problems):
        remedies.append(
            "A module imported while the strands tool decorator is replaced keeps "
            "plain functions for the rest of the process, even after the "
            "replacement is undone. Import the module before replacing the "
            "decorator, or do not replace the decorator."
        )
    if any("not a Strands tool" not in p for p in problems):
        remedies.append(
            "A replacement made at import, or inside a test without being undone, "
            "outlives it and changes every module and test that runs after it in "
            "this process. Use `monkeypatch.setattr` inside the test, or a "
            "function-scoped fixture, so the replacement ends with the test."
        )
    return (
        f"{where} left the strands/boto3 bindings changed (#1756):\n  "
        + "\n  ".join(problems)
        + "\n"
        + "\n".join(remedies)
        + "\nThe originals have been put back."
    )


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    report = yield
    if isinstance(collector, pytest.Module):
        problems = binding_problems()
        if problems and report.outcome != "failed":
            report.outcome = "failed"
            report.longrepr = _binding_failure_text(
                f"Importing {collector.nodeid}", problems
            )
    return report


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    problems = binding_problems()
    if problems:
        pytest.fail(_binding_failure_text(item.nodeid, problems), pytrace=False)
    return result
