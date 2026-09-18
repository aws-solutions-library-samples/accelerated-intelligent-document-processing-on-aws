# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert the offline test suites do not depend on an ambient AWS environment.

``make test-packages-cicd`` is the recipe both CIs use to run the package and
per-Lambda suites, and every suite it runs is offline by contract: no AWS call, no
credentials, no region. Nothing checked that contract, and the way it broke is
invisible on the machine where the code is written. A Lambda handler builds its
boto3 client at module scope — which is correct, because the runtime always sets
``AWS_REGION`` and a warm invocation then reuses the client — and the suite that
imports the handler inherits a region requirement. A developer machine satisfies it
from the shared AWS config file without anyone noticing; a CI runner has no such
file and no ``AWS_*`` variables, so botocore raises ``NoRegionError``. Three suites
were in that state, and the workaround was to pin ``AWS_DEFAULT_REGION`` on their
recipe lines, which fixed the symptom in the caller and left every future suite
free to inherit the same trap (#988).

The control that replaces those pins is in the Makefile: ``HERMETIC_AWS`` strips
the AWS environment from every pytest invocation in the recipe, so the local run is
the CI run and a suite that needs a region fails for everyone, immediately. This
file is what keeps that control honest, and everything it asserts is DERIVED from
the Makefile at test time rather than restated here:

* the wrapper is parsed out of the Makefile and its effect is **measured** — a
  subprocess launched under it must be unable to resolve a region. A stripping
  wrapper that has silently stopped stripping turns the whole gate into a no-op,
  and that is precisely the "control that exists but is never consulted" failure
  this repository keeps rediscovering;
* every pytest invocation in the recipe is checked to go through the wrapper, so a
  line added later without it fails here rather than in six months on a runner;
* every invocation the recipe names is then collected under that stripped
  environment, which is what actually catches the defect class in a new suite.

Scope of the collection probe, stated plainly because it is not total. Collecting a
suite executes its ``conftest.py`` and imports its test modules, so it catches a
client built at import time — which is the shape all three known instances had, and
the only shape that can take a whole suite out with a collection error. A client
built inside a test body (``test_file_copier`` imports its handler from a helper
called per test) is *not* caught here; it is caught by the recipe running the suite
under the same wrapper, which costs nothing extra because the suite has to run
anyway. Collecting all of them takes a few seconds; running all of them twice would
take minutes, which is the only reason the probe stops at collection.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
RECIPE_TARGET = "test-packages-cicd"
WRAPPER_VAR = "HERMETIC_AWS"
PYTEST_VAR = "PYTEST_HERMETIC"

# pytest options in the recipe that take their value as a separate argument. Their
# values (`-m "not integration"`, `-p no:cacheprovider`) must not be mistaken for
# test paths. If the recipe starts using another such option the consequence is a
# loud, obvious failure — its value is reported below as a path that does not
# exist — rather than a silently weakened check.
OPTIONS_TAKING_A_VALUE = {"-m", "-k", "-p", "-o", "-n", "--deselect", "--ignore"}

# Probing with S3 would not work: botocore keeps a legacy global default for it and
# resolves us-east-1 with no region configured at all, so an S3 client cannot show
# whether a region is available. A regional-only service is required, and SSM is
# one. Nothing is called on the client — construction alone is where the region is
# resolved.
PROBE_SERVICE = "ssm"

pytestmark = pytest.mark.unit


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations so each Makefile statement is one string."""
    out: list[str] = []
    acc = ""
    for raw in text.splitlines():
        acc += raw
        if acc.rstrip().endswith("\\"):
            acc = acc.rstrip()[:-1] + " "
            continue
        out.append(acc)
        acc = ""
    if acc:
        out.append(acc)
    return out


def _variable_definition(name: str) -> str:
    """The right-hand side of a ``name := ...`` assignment in the Makefile."""
    pattern = re.compile(rf"^{re.escape(name)}\s*:?=\s*(.*)$")
    for line in _logical_lines(MAKEFILE.read_text(encoding="utf-8")):
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    raise AssertionError(
        f"the Makefile no longer defines {name}. It is the wrapper that strips the "
        "AWS environment from the offline suites; if it was renamed, update this "
        "test, and if it was removed, the suites are back to depending on whatever "
        "region the machine happens to provide (#988)."
    )


def _hermetic_spec() -> tuple[list[str], dict[str, str]]:
    """Parse the wrapper into (variables it unsets, variables it assigns).

    Deriving this from the Makefile rather than keeping a copy is the point: the
    environment this test probes suites under is, by construction, the environment
    the recipe runs them in.
    """
    tokens = shlex.split(_variable_definition(WRAPPER_VAR))
    assert tokens and tokens[0] == "env", (
        f"{WRAPPER_VAR} is expected to be an `env` invocation so that it can both "
        f"unset and assign variables; got {tokens[:1]}"
    )
    unset: list[str] = []
    assigned: dict[str, str] = {}
    rest = tokens[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "-u":
            index += 1
            assert index < len(rest), f"trailing `-u` in {WRAPPER_VAR}"
            unset.append(rest[index])
        elif token.startswith("-u"):
            unset.append(token[2:])
        elif "=" in token:
            key, _, value = token.partition("=")
            assigned[key] = value
        else:
            raise AssertionError(
                f"unrecognised token {token!r} in {WRAPPER_VAR}. This test builds "
                "the probe environment from that definition, so it must stay a "
                "plain list of `-u NAME` and `NAME=VALUE` items."
            )
        index += 1
    return unset, assigned


def _sanitized_env() -> dict[str, str]:
    """This process's environment, put through the Makefile's wrapper."""
    unset, assigned = _hermetic_spec()
    env = dict(os.environ)
    for name in unset:
        env.pop(name, None)
    env.update(assigned)
    return env


def _recipe_body() -> list[str]:
    """The tab-indented lines of the test-packages-cicd recipe, continuations joined."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(RECIPE_TARGET + ":")]
    assert len(starts) == 1, (
        f"expected exactly one '{RECIPE_TARGET}:' rule in the Makefile, "
        f"found {len(starts)}"
    )
    body: list[str] = []
    for ln in lines[starts[0] + 1 :]:
        if ln.startswith("\t"):
            body.append(ln.lstrip("\t"))
        elif ln.strip() == "":
            continue
        else:
            break
    assert body, f"parsed an empty recipe body for {RECIPE_TARGET}"
    return [ln for ln in _logical_lines("\n".join(body)) if ln.strip()]


def _command_lines() -> list[str]:
    """Recipe lines that run something, with progress echoes and comments dropped.

    A path named in an ``@echo`` message or an ``@#`` comment must not be able to
    satisfy any assertion below.
    """
    return [
        ln
        for ln in _recipe_body()
        if not ln.lstrip("@").startswith("#") and not ln.startswith("@echo")
    ]


def _pytest_invocations() -> list[tuple[str, list[str]]]:
    """Every pytest run in the recipe, as (directory relative to the repo root, args).

    ``$(PYTEST_HERMETIC)`` expands to the wrapper plus ``python -m pytest``; this
    test supplies those itself, so only the arguments after it are extracted.
    """
    invocations: list[tuple[str, list[str]]] = []
    for line in _command_lines():
        if f"$({PYTEST_VAR})" not in line:
            continue
        workdir = "."
        command = line
        if command.startswith("cd "):
            head, _, tail = command.partition("&&")
            workdir = head[len("cd ") :].strip()
            command = tail.strip()
        _, _, args = command.partition(f"$({PYTEST_VAR})")
        invocations.append((workdir, shlex.split(args)))
    return invocations


def _target_paths(args: list[str]) -> list[str]:
    """The test paths in a pytest argument list, with option values excluded."""
    paths: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            skip_next = token in OPTIONS_TAKING_A_VALUE
            continue
        paths.append(token)
    return paths


def _invocation_id(workdir: str, args: list[str]) -> str:
    where = workdir if workdir != "." else ""
    return " ".join(filter(None, [where, *_target_paths(args)])) or workdir


_INVOCATIONS = _pytest_invocations()


def test_wrapper_definition_is_parseable_and_not_empty():
    """A wrapper that parses to nothing would make every assertion below vacuous."""
    unset, assigned = _hermetic_spec()
    assert unset, (
        f"{WRAPPER_VAR} unsets no variables, so it cannot remove a region supplied "
        "through AWS_REGION or AWS_DEFAULT_REGION"
    )
    assert assigned, (
        f"{WRAPPER_VAR} assigns no variables, so it cannot neutralise the shared "
        "AWS config file — the source that makes a developer machine disagree "
        "with a CI runner in the first place"
    )
    definition = _variable_definition(PYTEST_VAR)
    assert f"$({WRAPPER_VAR})" in definition, (
        f"{PYTEST_VAR} is what the recipe actually calls, and it no longer goes "
        f"through $({WRAPPER_VAR}): {definition!r}. Every suite in the recipe would "
        "silently regain access to the ambient AWS environment."
    )


def test_wrapper_really_leaves_no_resolvable_region():
    """Measure the wrapper rather than trusting its variable list.

    This is the check that keeps the gate from becoming decorative. If someone
    drops ``AWS_CONFIG_FILE`` from the wrapper, or a future botocore grows another
    region source, the collection probe below would keep passing while no longer
    proving anything — on a developer machine, which is where it would be noticed.
    So the probe environment is verified empirically: a client built under it must
    fail to resolve a region.
    """
    probe = (
        "import boto3\n"
        "try:\n"
        f"    client = boto3.client({PROBE_SERVICE!r})\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__)\n"
        "else:\n"
        "    print('RESOLVED', client.meta.region_name)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=_sanitized_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"the region probe itself failed to run: {result.stderr[-2000:]}"
    )
    assert result.stdout.strip() == "NoRegionError", (
        f"under $({WRAPPER_VAR}) a boto3 client resolved a region "
        f"(probe said {result.stdout.strip()!r}), so the wrapper is no longer "
        "removing every source botocore consults. Until that is fixed, the "
        "collection probe in this file proves nothing and the offline suites can "
        "again depend on the machine they run on. See #988."
    )


def test_recipe_parse_is_not_vacuous():
    """A broken parse would leave nothing to probe, and pass for the wrong reason."""
    assert _INVOCATIONS, (
        f"no $({PYTEST_VAR}) invocations were found in the {RECIPE_TARGET} recipe. "
        "Either the recipe stopped using the wrapper or this parse is broken; "
        "either way the per-suite probe below runs against nothing."
    )
    missing: list[str] = []
    for workdir, args in _INVOCATIONS:
        base = REPO_ROOT / workdir
        if not base.is_dir():
            missing.append(workdir)
            continue
        missing.extend(
            str(Path(workdir) / target)
            for target in _target_paths(args)
            if not (base / target).exists()
        )
    assert not missing, (
        f"{RECIPE_TARGET} names paths that do not exist:\n  " + "\n  ".join(missing)
    )


def test_no_recipe_line_supplies_its_own_aws_environment():
    """The recipe must not hand a suite a region, only take one away.

    Pinning ``AWS_DEFAULT_REGION=`` on a recipe line is how #988 was worked around,
    and it makes CI pass while leaving the suite dependent on its caller: anyone
    running pytest in that directory, or ``make test`` through
    ``scripts/run_all_tests.py``, still gets ``NoRegionError``. The region belongs
    in the suite's own ``conftest.py``.
    """
    offenders = [ln for ln in _command_lines() if re.search(r"\bAWS_[A-Z_]+=", ln)]
    # The wrapper's own assignments arrive through $(PYTEST_HERMETIC), not as text
    # on these lines, so anything matched here was written by hand.
    assert not offenders, (
        "these lines set an AWS_* variable for the suite they run:\n  "
        + "\n  ".join(offenders)
        + "\n\nPut the value in that suite's conftest.py instead, so the suite is "
        "self-contained and `pytest` run directly in its directory behaves the "
        "same way CI does."
    )


def test_every_pytest_invocation_goes_through_the_wrapper():
    unwrapped = [
        ln
        for ln in _command_lines()
        if "pytest" in ln and f"$({PYTEST_VAR})" not in ln
    ]
    assert not unwrapped, (
        f"these {RECIPE_TARGET} lines run pytest without $({PYTEST_VAR}):\n  "
        + "\n  ".join(unwrapped)
        + f"\n\nUse $({PYTEST_VAR}) instead of $(PYTHON) -m pytest. It is the same "
        "interpreter with the ambient AWS region, credentials and profile removed, "
        "which is what a CI runner gives the suite. Without it the suite is tested "
        "under an environment no runner has, and a module-scope boto3 client can "
        "reach CI undetected (#988)."
    )


@pytest.mark.parametrize(
    ("workdir", "args"),
    _INVOCATIONS,
    ids=[_invocation_id(w, a) for w, a in _INVOCATIONS],
)
def test_suite_collects_without_an_aws_region(workdir: str, args: list[str]):
    """Collect each suite with the AWS environment stripped.

    Collection runs the suite's ``conftest.py`` and imports its test modules, so a
    boto3 client constructed at import time — by the test module, by the handler it
    imports, or by any library on that import path — fails here, naming the suite.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=REPO_ROOT / workdir,
        env=_sanitized_env(),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return
    output = (result.stdout + result.stderr)[-4000:]
    hint = ""
    if "NoRegionError" in output:
        hint = (
            "\n\nThis is the #988 defect class: something on this suite's import "
            "path builds a boto3 client with no region. The Lambda runtime always "
            "supplies AWS_REGION, so production is unaffected and the handler does "
            "not need changing — add a conftest.py to the suite directory doing "
            "`os.environ.setdefault(\"AWS_DEFAULT_REGION\", \"us-east-1\")` before "
            "collection (see src/lambda/test_file_copier/conftest.py), or, if the "
            "client sits in a library that many callers import, build it lazily "
            "(see idp_common/utils/settings_helper.py)."
        )
    pytest.fail(
        f"`pytest --collect-only {' '.join(args)}` in {workdir} exited "
        f"{result.returncode} with no AWS region, credentials or profile in the "
        f"environment — the environment a CI runner has.{hint}\n\n{output}"
    )
