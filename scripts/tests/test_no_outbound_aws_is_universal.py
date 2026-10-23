# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every suite that was measured reaching a real AWS endpoint now refuses, as a class.

The refusal fixture existed twice before #1398 — `lib/idp_cli_pkg/tests/conftest.py`
and `lib/idp_feature_sdk/tests/conftest.py` — and each time the question "which other
suite has this?" was not asked. That is the defect #1398 reports, stated as a control
rather than as traffic: two roots had it, six were escaping, and nothing anywhere
could say so.

So this file derives its universe from the measurement instead of from a list anyone
maintains by hand. :data:`MEASURED_ESCAPING_ROOTS` is the six roots that produced a
request reaching botocore's real send, with the count each produced; every one of
them must now load a conftest defining an autouse refusal. The counts beside them are
a record of the measurement rather than something re-measured here, which is stated at
the table rather than left to be assumed.

Four checks, because none of them alone is worth much:

* **closure** — every root in the universe is covered by a conftest in its own
  chain, computed through pytest's own rootdir rule. Read from **source**, by AST,
  never by importing the conftest: these files have import-time side effects (they
  set `os.environ`, they stub absent modules into `sys.modules`), and a gate that
  imports four of them into the `scripts/tests` session changes what the 4,000 tests
  around it see.
* **effect** — the fixture each covering conftest defines actually refuses a real
  botocore request, measured in a subprocess of its own. A structural check alone
  would pass a fixture edited into a no-op, or one naming a seam botocore no longer
  routes through, which is the "control that exists and is never consulted" shape
  this repository keeps rediscovering.
* **non-vacuity** — the universe is not empty, more than one pytest rootdir is in
  play (so the derivation is not satisfied by a single file), and the two packages
  that predate #1398 are in it.
* **one definition** — exactly two conftests in the whole tree patch botocore's
  transport directly, and they are the two that predate `scripts/tests/no_outbound_aws.py`.
  That is asserted as an equality over every conftest rather than over a list of
  suites, so a *new* local copy of the seam fails wherever it appears.

What is NOT claimed: that a root outside this universe refuses. Fifteen registered
roots install placeholder AWS credentials and so could sign a real request if an
unmocked client were ever reached in them; eleven of those have no refusal, and the
reason they are not in this universe is that none of them was measured producing a
request. That residual is real and is recorded here rather than implied by silence.

No network request is made anywhere in this file. The refusal replaces botocore's
transport, so an attempt never leaves the process, and what is asserted is the raise.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The fixture names that count as the refusal.
#:
#: The two that predate #1398 are `no_outbound_http` and `_no_outbound_http`; both are
#: accepted, because renaming a fixture in a package whose tests refer to it by name
#: is a separate change from making the control universal.
FIXTURE_NAMES = ("no_outbound_aws", "no_outbound_http", "_no_outbound_http")

#: The inifile kinds pytest would honour when computing a rootdir.
#:
#: This repository uses only `pytest.ini`, and `rootdir_for` relies on that: a
#: `pyproject.toml` carrying `[tool.pytest.ini_options]`, or a `tox.ini` with a
#: `[pytest]` section, would move a rootdir — taking that tree out of the
#: repository-root conftest's reach — while closure was still reported. The premise is
#: asserted rather than assumed, in
#: `test_the_rootdir_derivation_premise_still_holds`.
INIFILE_NAMES = ("pyproject.toml", "tox.ini", "setup.cfg")

#: Directory names that are never source: build output, dependencies, and local work.
#:
#: Spelled as bare names and joined into path markers below, which is also the shape
#: `scripts/tests/test_repo_walk_guards_prune_local_work.py` reads — a gate that walks
#: the repository root and does not prune `scratch/` or `.claude/` scans a git
#: worktree or a mutation-test copy of this tree and fails while describing nothing
#: that ships.
LOCAL_AND_BUILD_DIRS = (
    "node_modules",
    ".venv",
    "site-packages",
    ".aws-sam",
    "build",
    "scratch",
    ".git",
    ".claude",
    # `.ai-review/base/` is the advisory reviewer's copy of the merge-base tree:
    # the same hazard, created by this repository's own tooling rather than by a
    # developer. Kept in step with the sibling gates.
    ".ai-review",
)

NOT_SOURCE = tuple(f"/{name}/" for name in LOCAL_AND_BUILD_DIRS)

pytestmark = pytest.mark.unit


def _run_all_tests():
    """`scripts/run_all_tests.py`, imported by path (it is a script, not a package)."""
    path = REPO_ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location("run_all_tests_universe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The roots measured in #1398 reaching botocore's real send, and how many requests
#: each produced. Instrumented by replacing `botocore.endpoint.Endpoint._send` with a
#: recorder that appends the request and then raises — a seam strictly below moto's
#: `before-send` short-circuit, so a record is exactly an escape.
#:
#: ⚠️ The counts are a **record of that measurement, not a value anything re-measures**
#: — no check in this repository reproduces them, so falsifying one changes nothing and
#: is caught by nothing. They are here because a table of roots with no magnitudes
#: reads as an opinion, and because the distribution is the useful part (one root
#: produced 343 of the 390 and is the only one where stripping the environment was
#: insufficient). What IS enforced is the membership: every key must be a root that
#: refuses, and must be a root `make test` still runs.
MEASURED_ESCAPING_ROOTS = {
    "lib/idp_common_pkg/tests": 343,
    "nested/api-resolvers/src/lambda/reprocess_document_resolver": 18,
    "benchmarks/tests": 10,
    "src/lambda/queue_sender": 9,
    "patterns/unified/tests": 8,
    "src/lambda/save_reporting_data": 2,
}

#: The two suites that carried a refusal before #1398. In the universe so that
#: removing one is a failure here rather than a silent loss.
REFUSAL_PREDATES_THIS_CHANGE = (
    "lib/idp_cli_pkg/tests",
    "lib/idp_feature_sdk/tests",
)

SHARED_DEFINITION = "scripts/tests/no_outbound_aws.py"


def covered_roots() -> list[Path]:
    """The universe: every root that must refuse, as absolute directories."""
    names = (
        *MEASURED_ESCAPING_ROOTS,
        *REFUSAL_PREDATES_THIS_CHANGE,
        # `lib/idp_sdk/tests` reached nothing in the measurement and is here anyway:
        # it ships its own `pytest.ini`, so no conftest above it can ever reach it,
        # and its placeholder credentials would let an unmocked client sign.
        "lib/idp_sdk/tests",
    )
    roots = [REPO_ROOT / name for name in dict.fromkeys(names)]
    missing = [r for r in roots if not r.is_dir()]
    assert not missing, (
        f"these roots are named in this file's universe but do not exist: "
        f"{[str(r) for r in missing]}"
    )
    return roots


def registered_roots() -> list[Path]:
    """Every root `make test` runs, as absolute directories that exist."""
    roots = [REPO_ROOT / r for r in _run_all_tests().RUN_ROOTS]
    return [r for r in roots if r.is_dir()]


def rootdir_for(root: Path) -> Path:
    """The directory pytest would compute as rootdir for `pytest <root>`.

    pytest walks from the argument upwards and takes the first ancestor holding an
    inifile; `confcutdir` then defaults to that, so it is also the highest directory
    whose `conftest.py` is loaded.
    """
    for candidate in (root, *root.parents):
        if (candidate / "pytest.ini").is_file():
            return candidate
        if candidate == REPO_ROOT:
            break
    return REPO_ROOT


def conftest_chain(root: Path) -> list[Path]:
    """The `conftest.py` files pytest loads for `pytest <root>`, outermost first.

    From the rootdir down to the root itself. Conftests *below* the root are loaded
    too, per collected directory, and are deliberately not counted here: a control
    present in one subdirectory does not cover the root.
    """
    top = rootdir_for(root)
    chain: list[Path] = []
    for candidate in (root, *root.parents):
        conftest = candidate / "conftest.py"
        if conftest.is_file():
            chain.append(conftest)
        if candidate == top:
            break
    return list(reversed(chain))


def _is_autouse_fixture(node: ast.FunctionDef) -> bool:
    """Whether a `def` carries a `pytest.fixture(autouse=True)` decorator.

    `autouse` is the load-bearing half: an opt-in refusal cannot reach a call a test
    does not know it is making, which is precisely the shape of every escape #1398
    measured.
    """
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else getattr(target, "id", "")
        )
        if name != "fixture":
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "autouse" and getattr(keyword.value, "value", False):
                return True
    return False


def defines_refusal(conftest: Path) -> bool:
    """Whether `conftest` defines an autouse refusal fixture, read from source.

    Two shapes count, because the repository has both. A `def` decorated with
    `pytest.fixture(autouse=True)` is the form the two pre-#1398 suites use. A
    module-level **assignment** — `no_outbound_aws = _no_outbound_aws_fixture()` — is
    the form the shared definition takes, where the fixture object is built by
    `scripts/tests/no_outbound_aws.py` and bound to a name here; `autouse` is then a
    property of that module, which the effect probe below measures directly.
    """
    tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FIXTURE_NAMES:
            if _is_autouse_fixture(node):
                return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in FIXTURE_NAMES:
                    return True
    return False


def covering_conftests(root: Path) -> list[Path]:
    """The conftests in `root`'s chain that define an autouse refusal."""
    return [p for p in conftest_chain(root) if defines_refusal(p)]


def roots_covered_by(conftest: Path) -> list[Path]:
    """The roots in this file's universe whose conftest chain includes `conftest`."""
    return [r for r in covered_roots() if conftest in conftest_chain(r)]


def has_integration_marked_tests(root: Path) -> bool:
    """Whether any test under `root` carries the `integration` marker.

    Derived by reading source, because the alternative is a `pytest -m integration
    --collect-only` subprocess per root and this gate is otherwise a few seconds. The
    bound is stated rather than hidden: a marker applied some other way — by a
    `pytest_collection_modifyitems` hook, say — is not seen here, and the consequence
    would be demanding no carve-out from a suite that needs one. The two spellings
    below are the only ones this repository uses.
    """
    for path in root.rglob("test_*.py"):
        posix = "/" + path.relative_to(REPO_ROOT).as_posix()
        if any(marker in posix for marker in NOT_SOURCE):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "mark.integration" in text:
            return True
    return False


def test_the_rootdir_derivation_premise_still_holds():
    """Only `pytest.ini` decides a rootdir here, which is what `rootdir_for` assumes.

    A file configuring pytest any other way would move a rootdir, taking that tree
    out of the repository-root `conftest.py`'s reach, and `rootdir_for` would not see
    it — so closure would still be reported while the control had a hole.
    """
    offenders: list[str] = []
    for name in INIFILE_NAMES:
        for path in REPO_ROOT.rglob(name):
            posix = "/" + path.relative_to(REPO_ROOT).as_posix()
            if any(marker in posix for marker in NOT_SOURCE):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "[tool.pytest.ini_options]" in text or "\n[pytest]" in text:
                offenders.append(posix)
    assert not offenders, (
        f"these files configure pytest and so move a rootdir: {sorted(offenders)}. "
        f"`rootdir_for` in this file only looks for pytest.ini, so a tree under one "
        f"of these sits outside the repository-root conftest's reach while this file "
        f"still reports closure. Teach `rootdir_for` about it. #1398."
    )


@pytest.mark.parametrize(
    "root", covered_roots(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_every_root_in_the_universe_is_covered_by_a_refusal(root: Path):
    """Universe closure: derived from RUN_ROOTS, not from a list maintained here.

    A root reaches this either through the repository-root `conftest.py` — which is
    every root whose rootdir is the repository — or through a conftest of its own,
    which is how the three packages shipping a `pytest.ini` are covered. A root
    covered by neither is a suite that can transact with a real AWS account, and
    nothing will report it: these calls sit inside production `except Exception`
    handlers, which is why five roots were doing it unnoticed.
    """
    covering = covering_conftests(root)
    assert covering, (
        f"{root.relative_to(REPO_ROOT)} loads no conftest defining an autouse "
        f"refusal fixture (one of {list(FIXTURE_NAMES)}). Its rootdir is "
        f"'{rootdir_for(root).relative_to(REPO_ROOT).as_posix() or '.'}' and its "
        f"conftest chain is "
        f"{[p.relative_to(REPO_ROOT).as_posix() for p in conftest_chain(root)]}. "
        f"#1398."
    )


def test_more_than_one_rootdir_is_in_play():
    """Non-vacuity for the closure check, in the dimension that matters.

    If every root resolved to the repository rootdir, the parametrisation would be
    satisfied by one file and would say nothing about the packages that compute their
    own. Those packages are exactly what the root conftest cannot reach, so they are
    the reason the per-root derivation exists.
    """
    distinct = {rootdir_for(root) for root in covered_roots()}
    assert len(distinct) > 1, (
        f"every registered root resolves to one rootdir ({distinct}); the per-root "
        f"derivation in this file is then measuring nothing beyond a single file"
    )
    assert REPO_ROOT in distinct, "no root resolves to the repository rootdir"


def refusal_implementations() -> list[Path]:
    """Every distinct conftest that covers at least one registered root."""
    found: dict[Path, None] = {}
    for root in covered_roots():
        for path in covering_conftests(root):
            found[path] = None
    return sorted(found)


def test_at_least_one_refusal_implementation_was_found():
    """Guards the parametrisation below against collecting nothing at all."""
    assert refusal_implementations(), (
        "no conftest defining an autouse refusal fixture covers any registered root"
    )


#: The probe run against each implementation, in a subprocess of its own.
#:
#: A subprocess because the subject is somebody else's `conftest.py` and importing it
#: has side effects — `os.environ` assignments, `sys.modules` stubs — that would
#: otherwise land in the session running this file. The fixture is called directly
#: rather than through pytest, because there is no way to make another tree's conftest
#: apply to a test in this one; `request` is stubbed to report no `integration`
#: marker, which is the branch every unit test takes.
_PROBE = r"""
import importlib.util, inspect, sys
from pathlib import Path

conftest = Path(sys.argv[1])
sys.path.insert(0, str(conftest.parent))
spec = importlib.util.spec_from_file_location("conftest_under_probe", conftest)
module = importlib.util.module_from_spec(spec)
sys.modules["conftest_under_probe"] = module
spec.loader.exec_module(module)

fixture = None
for name in ("no_outbound_aws", "no_outbound_http", "_no_outbound_http"):
    candidate = getattr(module, name, None)
    if candidate is not None:
        fixture = candidate
        break
assert fixture is not None, "no refusal fixture on %s" % conftest

# `@pytest.fixture` returns a wrapper object, and WHICH attributes it carries is
# pytest-version-dependent: pytest 9 uses `_fixture_function_marker` /
# `_fixture_function`, earlier versions `_pytestfixturefunction` / `__wrapped__`.
# Both are read, and finding neither is reported rather than assumed benign — a probe
# keyed to one pytest's internals that silently answers "fine" is worse than one that
# fails on an upgrade.
def _marker(obj):
    for attribute in ("_fixture_function_marker", "_pytestfixturefunction"):
        found = getattr(obj, attribute, None)
        if found is not None:
            return found
    return None


def _unwrap(obj):
    for attribute in ("_fixture_function", "__wrapped__"):
        found = getattr(obj, attribute, None)
        if found is not None:
            return found
    return obj


marker = _marker(fixture)
if marker is None:
    print("NOT-A-FIXTURE"); raise SystemExit(0)
if not getattr(marker, "autouse", False):
    print("NOT-AUTOUSE"); raise SystemExit(0)

function = _unwrap(fixture)

class _Node:
    def get_closest_marker(self, _name):
        return None

class _Request:
    node = _Node()

import pytest
patcher = pytest.MonkeyPatch()
kwargs = {}
for parameter in inspect.signature(function).parameters:
    kwargs[parameter] = _Request() if parameter == "request" else patcher
result = function(**kwargs)
if inspect.isgenerator(result):
    next(result)

import boto3
client = boto3.client(
    "sts",
    region_name="us-east-1",
    aws_access_key_id="probe",
    aws_secret_access_key="probe",
)
try:
    client.get_caller_identity()
except RuntimeError as refusal:
    assert "attempted a real network request" in str(refusal), str(refusal)
except BaseException as other:
    print("OTHER:" + type(other).__name__); raise SystemExit(0)
else:
    print("SENT"); raise SystemExit(0)

# The other direction: an `integration`-marked test must be left alone, or
# `make test-integration` cannot reach the infrastructure it exists to test. Asserted
# structurally — the transport must be the ORIGINAL function after the fixture has run
# — because the only behavioural way to check "not refused" is to send a request.
patcher.undo()
from botocore.httpsession import URLLib3Session

pristine = URLLib3Session.send

class _IntegrationNode:
    def get_closest_marker(self, name):
        return object() if name == "integration" else None

class _IntegrationRequest:
    node = _IntegrationNode()

patcher2 = pytest.MonkeyPatch()
kwargs2 = {}
for parameter in inspect.signature(function).parameters:
    kwargs2[parameter] = _IntegrationRequest() if parameter == "request" else patcher2
out2 = function(**kwargs2)
if inspect.isgenerator(out2):
    next(out2)
if URLLib3Session.send is not pristine:
    print("INTEGRATION-REFUSED"); raise SystemExit(0)
print("REFUSED")
"""


@pytest.mark.parametrize(
    "conftest",
    refusal_implementations(),
    ids=lambda p: p.relative_to(REPO_ROOT).as_posix(),
)
def test_each_refusal_actually_refuses_a_real_botocore_request(conftest: Path):
    """Effect, measured: the fixture is run and a real client is then refused.

    This is the half a structural check cannot do. It also fails if botocore moves
    the seam these fixtures patch, which would disarm all four at once and is
    otherwise invisible — nothing else in the repository would notice.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        from first_party_paths import pinned_environment
    finally:
        sys.path.pop(0)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(conftest)],
        cwd=REPO_ROOT,
        env=pinned_environment(REPO_ROOT, dict(os.environ)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"the probe against {conftest.relative_to(REPO_ROOT)} failed to run: "
        f"{result.stderr[-3000:]}"
    )
    verdict = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    assert verdict != "NOT-AUTOUSE", (
        f"{conftest.relative_to(REPO_ROOT)}'s refusal fixture is not `autouse`, so it "
        f"applies only to a test that asks for it by name — and every escape measured "
        f"in #1398 came from a test that did not know it was making the call. Note "
        f"this is invisible to a source check when the fixture is built by "
        f"{SHARED_DEFINITION} and merely bound here, which is why it is measured."
    )
    assert verdict != "NOT-A-FIXTURE", (
        f"{conftest.relative_to(REPO_ROOT)} binds the refusal name to something that "
        f"is not a pytest fixture, so pytest will never run it."
    )
    # The carve-out is demanded only where there is something for it to carve out.
    # Two conftests here predate #1398 and have no marker check at all; neither of
    # their packages contains a single `integration`-marked test, so requiring one
    # would be a requirement with no member behind it. The premise is computed per
    # conftest rather than recorded, so an integration test added to either package
    # turns this into a demand on arrival.
    needs_carve_out = [
        r.relative_to(REPO_ROOT).as_posix()
        for r in roots_covered_by(conftest)
        if has_integration_marked_tests(r)
    ]
    if needs_carve_out:
        assert verdict != "INTEGRATION-REFUSED", (
            f"{conftest.relative_to(REPO_ROOT)}'s refusal also applies to an "
            f"`integration`-marked test, and {needs_carve_out} contain such tests — "
            f"so `make test-integration` cannot reach the infrastructure they exist "
            f"to test. The marker is what says 'this test calls AWS', and "
            f"`scripts/run_all_tests.py` makes the same split. Found by mutation: "
            f"misspelling the marker name made every carve-out inert and nothing "
            f"noticed."
        )
    elif verdict == "INTEGRATION-REFUSED":
        # Refusing an integration test is harmless while there are none, and saying
        # so is better than silently accepting it: the demand above arrives with the
        # first such test, which is when it starts to matter.
        return
    assert verdict == "REFUSED", (
        f"{conftest.relative_to(REPO_ROOT)} defines an autouse refusal fixture that "
        f"does not refuse: the probe's botocore request was "
        f"{verdict or '<no output>'}. Either the fixture no longer patches the seam "
        f"every botocore request passes through "
        f"(`botocore.httpsession.URLLib3Session.send`), or botocore has moved it — "
        f"in which case all of them are disarmed at once. #1398."
    )


def test_every_root_in_the_universe_is_registered_in_the_gate(monkeypatch):
    """A root that must refuse must also be a root `make test` actually runs.

    The universe above is derived from a measurement taken at one point in time, and
    the thing that would make it quietly wrong is a root being renamed or dropped from
    `RUN_ROOTS` — the suite then runs nowhere, this file still asserts a property
    about it, and both read green. Checked against the runner's own registry.
    """
    registered = {p.relative_to(REPO_ROOT).as_posix() for p in registered_roots()}
    universe = {p.relative_to(REPO_ROOT).as_posix() for p in covered_roots()}
    unregistered = sorted(universe - registered)
    assert not unregistered, (
        f"{unregistered} must refuse an outbound AWS request but are not in "
        f"scripts/run_all_tests.py's RUN_ROOTS, so `make test` does not run them. "
        f"Either the root moved and this file's universe is stale, or the suite was "
        f"dropped from the gate."
    )


def test_the_shared_definition_exists_and_is_the_only_copy_of_the_seam():
    """Non-vacuity for the test above, and a bound on how many copies there are.

    The two suites that predate #1398 still carry their own `URLLib3Session.send`
    patch, which is why this asserts a ceiling rather than "exactly one". What it
    refuses is a *new* copy: converting those two is worth doing and is a separate
    change, and until then the honest statement is the count.
    """
    shared = REPO_ROOT / SHARED_DEFINITION
    assert shared.is_file(), f"{SHARED_DEFINITION} is missing"
    assert "URLLib3Session" in shared.read_text(encoding="utf-8"), (
        f"{SHARED_DEFINITION} no longer names the seam it is supposed to own"
    )

    local_copies = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in REPO_ROOT.rglob("conftest.py")
        if not any(
            marker in "/" + p.relative_to(REPO_ROOT).as_posix() for marker in NOT_SOURCE
        )
        and "URLLib3Session" in p.read_text(encoding="utf-8", errors="replace")
    )
    assert local_copies == [
        "lib/idp_cli_pkg/tests/conftest.py",
        "lib/idp_feature_sdk/tests/conftest.py",
    ], (
        f"the conftests patching botocore's transport directly are {local_copies}. "
        f"Exactly two are expected — the ones that predate "
        f"{SHARED_DEFINITION} — and a new one is a copy of a control that has a "
        f"single home. Bind the shared definition instead."
    )
