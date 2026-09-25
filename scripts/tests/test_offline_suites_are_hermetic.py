# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert the offline test suites do not depend on an ambient AWS environment.

``make test-packages-cicd`` and ``make test-cicd -C lib/idp_common_pkg`` are the two
recipes both CIs use to run this repository's offline suites, and every suite they
run is offline by contract: no AWS call, no credentials, no region. Nothing checked
that contract, and both ways it broke are invisible on the machine where the code is
written, because a developer machine has AWS sources a CI runner does not.

A Lambda handler builds its boto3 client at module scope — which is correct, because
the runtime always sets ``AWS_REGION`` and a warm invocation then reuses the client —
and the suite that imports the handler inherits the handler's requirements:

* **a region.** A developer machine satisfies it from the shared AWS config file
  without anyone noticing; a runner has no such file and no ``AWS_*`` variables, so
  botocore raises ``NoRegionError`` during collection. Three suites were in that
  state, and the workaround was to pin ``AWS_DEFAULT_REGION`` on their recipe lines,
  which fixed the symptom in the caller and left every future suite free to inherit
  the same trap (#988);
* **credentials.** botocore freezes the session's credentials object into a client
  when the client is constructed, so a client built at import time with none
  resolvable can never sign, however many credentials appear later. A developer box
  on EC2 resolves them from the instance metadata service; a runner cannot reach it.
  Five tests in ``lib/idp_common_pkg`` failed on CI only, for exactly that reason.

The control that replaces the pins is ``HERMETIC_AWS`` in ``make/hermetic_aws.mk``,
included by both Makefiles so there is one definition rather than a copy per
consumer. It strips the AWS environment from every pytest invocation in both
recipes, so the local run is the CI run and a suite that needs a region or
credentials fails for everyone, immediately. This file is what keeps that control
honest, and everything it asserts is DERIVED from those makefiles at test time
rather than restated here:

* the wrapper is parsed out of ``make/hermetic_aws.mk`` and its effect is
  **measured**: a subprocess launched under it must be unable to resolve either a
  region or credentials. The probe environment is deliberately *polluted* first —
  supplying both through every source, including a real shared config file — because
  a probe built from the ambient environment measures nothing on a runner, where
  there was never anything to strip. A stripping wrapper that has silently stopped
  stripping turns the whole gate into a no-op, and that is precisely the "control
  that exists but is never consulted" failure this repository keeps rediscovering.
  Measurement cannot cover everything, and the gaps are named rather than assumed
  away: several sources are invisible to a probe on the very machine that has them,
  so they are held to a floor list instead — see ``REQUIRED_UNSET_FLOOR``;
* every *runnable line* in both recipes is checked to go through the wrapper, so a
  line added later without it fails here rather than in six months on a runner. The
  check is not a search for the word "pytest": ``$(PYTHON) -m $(PYTEST_MOD)`` runs
  pytest and contains no such word, so a substring match would skip the line and drop
  its per-suite probe silently. The parsed invocation count is also required to equal
  the runnable line count, so a parse that quietly saw one line out of 34 fails;
* every invocation ``test-packages-cicd`` names is then collected under that
  stripped environment, which is what actually catches the defect class in a new
  suite.

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
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"
RECIPE_TARGET = "test-packages-cicd"
# The wrapper lives in its own file because two Makefiles include it: this one's
# recipe and lib/idp_common_pkg's unit targets, which CI invokes separately as
# `make test-cicd -C lib/idp_common_pkg`. One definition, two consumers.
WRAPPER_MAKEFILE = REPO_ROOT / "make" / "hermetic_aws.mk"
LIBRARY_MAKEFILE = REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile"
# The idp_common_pkg targets that must run hermetically, and the one that must
# not: test-integration needs the machine's real credentials and region.
LIBRARY_HERMETIC_TARGETS = ("test-unit", "test-unit-cicd")
LIBRARY_NON_HERMETIC_TARGET = "test-integration"
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

# Values the probes put INTO the environment before the wrapper runs, so that the
# wrapper has something to take away. See _polluted_env.
SENTINEL_REGION = "eu-west-3"
SENTINEL_VALUE = "hermetic-probe-sentinel"
_SHARED_CONFIG_DIR: str | None = None

# The floor the wrapper must keep removing. This is deliberately a MINIMUM rather
# than a copy of the definition: adding a variable to the wrapper needs no change
# here, and removing one of these is the regression that put five tests on CI-only
# failure and three suites on a pinned region (#988).
#
# The floor is not redundant with the two measured probes below, because a probe can
# only see a source the machine running it actually has:
#
#  * ``AWS_DEFAULT_REGION`` and the three credential variables are measured.
#  * ``AWS_REGION`` is NOT. botocore's default-region environment source is
#    ``AWS_DEFAULT_REGION`` alone, so with only ``AWS_REGION`` set a client still
#    raises ``NoRegionError`` and the region probe cannot distinguish a wrapper that
#    removes it from one that does not. It stays in the wrapper because handlers and
#    conftests read it directly, and it is held here.
#  * ``AWS_PROFILE``, ``AWS_ROLE_ARN``, ``AWS_WEB_IDENTITY_TOKEN_FILE`` and the two
#    container credential URIs cannot be probed safely — a regressed wrapper would
#    send the probe off to fetch credentials over the network, or to read a profile
#    that may not exist.
#  * ``AWS_EC2_METADATA_DISABLED`` is measurable only ON EC2. Off it, or with the
#    metadata endpoint unreachable, deleting that assignment changes nothing the
#    probe can see — so on a CI runner the probe stays green while every developer
#    box on EC2 silently resolves its instance-role credentials again. That is the
#    exact machine class the five-test failure came from, so it is floored too.
REQUIRED_UNSET_FLOOR = {
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
}
REQUIRED_ASSIGNED_FLOOR = {
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_EC2_METADATA_DISABLED",
}
# Values the two neutralised file variables may take. Both must name something that
# cannot supply a region or credentials; `make` expands nothing here, so a value
# containing `$(...)` reaches botocore as a literal string that happens not to be a
# path — which silences both probes while the real recipe reads the expanded file.
NEUTRALISED_FILE_VARS = ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE")

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
    """The right-hand side of a ``name := ...`` assignment in the wrapper file."""
    pattern = re.compile(rf"^{re.escape(name)}\s*:?=\s*(.*)$")
    for line in _logical_lines(WRAPPER_MAKEFILE.read_text(encoding="utf-8")):
        match = pattern.match(line)
        if match:
            return match.group(1).strip()
    raise AssertionError(
        f"{WRAPPER_MAKEFILE.relative_to(REPO_ROOT)} no longer defines {name}. It is "
        "the wrapper that strips the AWS environment from the offline suites; if it "
        "was renamed, update this test, and if it was removed, the suites are back "
        "to depending on whatever region and credentials the machine happens to "
        "provide (#988)."
    )


def _recipe_lines(makefile: Path, target: str) -> list[str]:
    """The tab-indented lines of one target's recipe, continuations joined."""
    lines = makefile.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(target + ":")]
    assert len(starts) == 1, (
        f"expected exactly one '{target}:' rule in "
        f"{makefile.relative_to(REPO_ROOT)}, found {len(starts)}"
    )
    body: list[str] = []
    for ln in lines[starts[0] + 1 :]:
        if ln.startswith("\t"):
            body.append(ln.lstrip("\t"))
        elif ln.strip() == "":
            continue
        else:
            break
    assert body, (
        f"parsed an empty recipe body for {target} in {makefile.relative_to(REPO_ROOT)}"
    )
    return [ln for ln in _logical_lines("\n".join(body)) if ln.strip()]


def _hermetic_spec() -> tuple[list[str], dict[str, str]]:
    """Parse the wrapper into (variables it unsets, variables it assigns).

    Deriving this from the Makefile rather than keeping a copy is the point: the
    environment this test probes suites under is, by construction, the environment
    the recipe runs them in.
    """
    # The loop variable below is a shell word, deliberately not named ``token``:
    # bandit's B105 reads any ``token == "..."`` comparison as a hardcoded
    # credential check and reports it HIGH, which gates the SRT job. Nothing here
    # is a secret — the wrapper contains only variable names — so the name is the
    # thing to change rather than the finding the thing to suppress. ``word`` is
    # also the more accurate name next to ``AWS_SESSION_TOKEN``, which IS a
    # credential and appears a few lines away in the Makefile this parses.
    words = shlex.split(_variable_definition(WRAPPER_VAR))
    assert words and words[0] == "env", (
        f"{WRAPPER_VAR} is expected to be an `env` invocation so that it can both "
        f"unset and assign variables; got {words[:1]}"
    )
    unset: list[str] = []
    assigned: dict[str, str] = {}
    rest = words[1:]
    index = 0
    while index < len(rest):
        word = rest[index]
        if word == "-u":
            index += 1
            assert index < len(rest), f"trailing `-u` in {WRAPPER_VAR}"
            unset.append(rest[index])
        elif word.startswith("-u"):
            unset.append(word[2:])
        elif "=" in word:
            key, _, value = word.partition("=")
            assigned[key] = value
        else:
            raise AssertionError(
                f"unrecognised token {word!r} in {WRAPPER_VAR}. This test builds "
                "the probe environment from that definition, so it must stay a "
                "plain list of `-u NAME` and `NAME=VALUE` items."
            )
        index += 1
    return unset, assigned


def _shared_config_supplying_a_region() -> str:
    """Path to a throwaway AWS config file that names a region and credentials.

    The wrapper neutralises the shared config file by assignment rather than by
    unsetting, and that half of it can only be measured if the base environment
    actually points at a file with something in it.
    """
    global _SHARED_CONFIG_DIR
    if _SHARED_CONFIG_DIR is None:
        _SHARED_CONFIG_DIR = tempfile.mkdtemp(prefix="hermetic-probe-")
        path = Path(_SHARED_CONFIG_DIR) / "config"
        path.write_text(
            "[default]\n"
            f"region = {SENTINEL_REGION}\n"
            f"aws_access_key_id = {SENTINEL_VALUE}\n"
            f"aws_secret_access_key = {SENTINEL_VALUE}\n",
            encoding="utf-8",
        )
    return str(Path(_SHARED_CONFIG_DIR) / "config")


def _polluted_env() -> dict[str, str]:
    """An environment with every AWS source the wrapper claims to remove PRESENT.

    This is what makes the probes below a measurement rather than a coincidence. A
    CI runner sets no ``AWS_*`` variable and has no shared config file, so a wrapper
    that quietly stopped unsetting ``AWS_DEFAULT_REGION`` would still leave no region
    behind and a probe built from the ambient environment would still pass. Confirmed
    by mutation: dropping that ``-u`` is undetectable unless the probe supplies a
    region for it to remove.

    ``AWS_REGION`` is populated here for completeness but is **not** what the region
    probe measures: botocore's default-region environment source is
    ``AWS_DEFAULT_REGION`` alone, so with only ``AWS_REGION`` set a client still
    raises ``NoRegionError``. It is held by the floor instead, along with the profile,
    role, web-identity and container-credential variables — those are left out of the
    pollution on purpose, since pointing them at a sentinel would make a regressed
    wrapper attempt a network credential fetch and hang rather than fail. See
    ``REQUIRED_UNSET_FLOOR`` for the full division of labour.
    """
    env = dict(os.environ)
    env["AWS_REGION"] = SENTINEL_REGION
    env["AWS_DEFAULT_REGION"] = SENTINEL_REGION
    env["AWS_ACCESS_KEY_ID"] = SENTINEL_VALUE
    env["AWS_SECRET_ACCESS_KEY"] = SENTINEL_VALUE
    env["AWS_SESSION_TOKEN"] = SENTINEL_VALUE
    env.pop("AWS_PROFILE", None)
    config = _shared_config_supplying_a_region()
    env["AWS_CONFIG_FILE"] = config
    env["AWS_SHARED_CREDENTIALS_FILE"] = config
    env.pop("AWS_EC2_METADATA_DISABLED", None)
    return env


def _sanitized_env() -> dict[str, str]:
    """The polluted environment above, put through the Makefile's wrapper."""
    unset, assigned = _hermetic_spec()
    env = _polluted_env()
    for name in unset:
        env.pop(name, None)
    env.update(assigned)
    return env


def _recipe_body() -> list[str]:
    """The tab-indented lines of the test-packages-cicd recipe, continuations joined."""
    return _recipe_lines(MAKEFILE, RECIPE_TARGET)


def _runnable(lines: list[str]) -> list[str]:
    """Recipe lines that run something, with progress echoes and comments dropped.

    A path named in an ``@echo`` message or an ``@#`` comment must not be able to
    satisfy any assertion below.
    """
    return [
        ln
        for ln in lines
        if not ln.lstrip("@").startswith("#") and not ln.startswith("@echo")
    ]


def _command_lines() -> list[str]:
    return _runnable(_recipe_body())


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
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            skip_next = arg in OPTIONS_TAKING_A_VALUE
            continue
        paths.append(arg)
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


def test_wrapper_still_removes_the_sources_that_matter():
    """A floor on the variable list, for the sources the probes cannot measure.

    See REQUIRED_UNSET_FLOOR for which ones those are and why each is unmeasurable:
    a probe can only see a source the machine running it actually has, so several
    entries would be silent to drop on the very machines that need them removed.
    """
    unset, assigned = _hermetic_spec()
    missing_unset = REQUIRED_UNSET_FLOOR - set(unset)
    assert not missing_unset, (
        f"{WRAPPER_VAR} no longer unsets {sorted(missing_unset)}. Each of these is "
        "a source botocore will use to resolve a region or credentials, so a suite "
        "in a gated recipe would again inherit whatever the machine provides (#988)."
    )
    missing_assigned = REQUIRED_ASSIGNED_FLOOR - set(assigned)
    assert not missing_assigned, (
        f"{WRAPPER_VAR} no longer neutralises {sorted(missing_assigned)}. Unsetting "
        "cannot reach the shared AWS config file or the instance metadata service; "
        "only an assignment can, and those two are what make a developer machine "
        "disagree with a CI runner."
    )


def test_wrapper_assignments_are_literal_and_neutral():
    """The values, not just the keys — and no unexpanded make variables.

    Two ways an assignment can be present and useless. `make` expands `$(HOME)` when
    it runs the recipe, and this test does not: it reads the makefile as text, so a
    value like ``AWS_CONFIG_FILE=$(HOME)/.aws/config`` reaches the probes as that
    literal string, which is not a path, so both probes see an absent file and pass
    while the real recipe hands the suite the developer's actual config and
    long-lived credentials. And a value can simply name a file with content in it.
    So: reject any `$(`, and require each neutralised file variable to name something
    that is absent or empty.
    """
    _, assigned = _hermetic_spec()
    unexpanded = {name: value for name, value in assigned.items() if "$(" in value}
    assert not unexpanded, (
        f"{WRAPPER_VAR} assigns an unexpanded make variable: {unexpanded}. This test "
        "reads the makefile as text and cannot expand it, so the value it probes "
        "under is not the value the recipe uses — the probes would pass while the "
        "suites ran against a real file. Use a literal path."
    )
    for name in NEUTRALISED_FILE_VARS:
        value = assigned.get(name)
        assert value, f"{WRAPPER_VAR} no longer assigns {name}"
        path = Path(value)
        # /dev/null exists and is a character device; `st_size` is 0, which is the
        # property that matters — botocore reads it and finds no profile.
        empty = not path.exists() or (path.is_file() and path.stat().st_size == 0)
        assert empty or value == os.devnull, (
            f"{WRAPPER_VAR} points {name} at {value!r}, which has content. That file "
            "can supply a region or credentials, which is exactly what this wrapper "
            "exists to prevent."
        )


def test_wrapper_really_leaves_no_resolvable_region():
    """Measure the wrapper rather than trusting its variable list.

    This is the check that keeps the gate from becoming decorative. If someone
    drops ``AWS_CONFIG_FILE`` from the wrapper, or a future botocore grows another
    region source, the collection probe below would keep passing while no longer
    proving anything. The probe environment is therefore built by ``_polluted_env``,
    which supplies a region through all three sources — the two variables and a real
    shared config file — before the wrapper runs, and then requires that no region
    survives.
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


def test_wrapper_really_leaves_no_resolvable_credentials():
    """The second half, and the one a region probe cannot see.

    botocore freezes the session's credentials object into a client when the client
    is constructed, so a client a suite builds at import time is stuck with whatever
    was resolvable at that instant — for the life of the process, however many
    credentials appear afterwards. A suite that quietly took its credentials from a
    developer box's instance metadata service therefore passed locally and could not
    sign anything on a runner, which is how five tests in ``lib/idp_common_pkg``
    failed on CI only. This probe supplies credentials through the variables AND the
    shared credentials file and requires that none survive the wrapper. A region IS
    supplied, so a ``NoRegionError`` cannot masquerade as success here.
    """
    probe = (
        "import boto3\n"
        "creds = boto3.Session(region_name='us-east-1').get_credentials()\n"
        "print('NONE' if creds is None else 'RESOLVED '"
        " + (creds.access_key or '<empty>'))\n"
    )
    env = _sanitized_env()
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"the credentials probe itself failed to run: {result.stderr[-2000:]}"
    )
    assert result.stdout.strip() == "NONE", (
        f"under $({WRAPPER_VAR}) botocore still resolved credentials "
        f"(probe said {result.stdout.strip()!r}), so the wrapper is no longer "
        "removing every credential source. A suite in a gated recipe can then sign "
        "real requests against whichever account the developer is signed in to, and "
        "a suite that depends on ambient credentials passes here and fails on a "
        "runner. See #988."
    )


def test_recipe_parse_is_not_vacuous():
    """A broken parse would leave nothing to probe, and pass for the wrong reason."""
    assert _INVOCATIONS, (
        f"no $({PYTEST_VAR}) invocations were found in the {RECIPE_TARGET} recipe. "
        "Either the recipe stopped using the wrapper or this parse is broken; "
        "either way the per-suite probe below runs against nothing."
    )
    # Non-emptiness is too weak on its own: a parse that silently dropped 33 of 34
    # lines would satisfy it, and the parametrized probe would then cover one suite
    # while reading as if it covered them all. Every runnable line must become
    # exactly one probed invocation.
    runnable = _command_lines()
    assert len(_INVOCATIONS) == len(runnable), (
        f"{RECIPE_TARGET} has {len(runnable)} runnable lines but only "
        f"{len(_INVOCATIONS)} were parsed into probes. The per-suite probe below "
        "would silently skip the difference. Lines the parse cannot see:\n  "
        + "\n  ".join(ln for ln in runnable if f"$({PYTEST_VAR})" not in ln)
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


def test_every_recipe_line_goes_through_the_wrapper():
    """EVERY runnable line, not every line that mentions pytest.

    Matching on the word "pytest" is not enough, because the word need not appear:
    ``$(PYTHON) -m $(PYTEST_MOD)`` runs pytest and contains no ``pytest``, so a
    substring search skips the line entirely — and ``_pytest_invocations`` skips it
    too, quietly dropping one of the parametrized per-suite probes below. Every
    runnable line in this recipe runs a test suite, so the honest invariant is that
    all of them go through the wrapper. If a genuinely non-pytest command is ever
    needed here, add it as an ``@echo``/``@#`` line or update this test deliberately.
    """
    unwrapped = [ln for ln in _command_lines() if f"$({PYTEST_VAR})" not in ln]
    assert not unwrapped, (
        f"these {RECIPE_TARGET} lines do not go through $({PYTEST_VAR}):\n  "
        + "\n  ".join(unwrapped)
        + f"\n\nUse $({PYTEST_VAR}) instead of $(PYTHON) -m pytest. It is the same "
        "interpreter with the ambient AWS region, credentials and profile removed, "
        "which is what a CI runner gives the suite. Without it the suite is tested "
        "under an environment no runner has, and a module-scope boto3 client can "
        "reach CI undetected (#988)."
    )


def test_both_makefiles_include_the_one_wrapper_definition():
    """Two consumers, one definition — or the second one drifts silently.

    ``make test-cicd -C lib/idp_common_pkg`` is a separate make invocation from the
    root Makefile, so it cannot see a variable defined there. Copying the wrapper
    into it would give the repository two versions of "what a CI runner supplies",
    and the copy that stopped being updated would be the one gating a suite.
    """
    assert WRAPPER_MAKEFILE.exists(), (
        f"{WRAPPER_MAKEFILE.relative_to(REPO_ROOT)} is gone. It holds the single "
        f"definition of {WRAPPER_VAR}, included by both the root Makefile and "
        "lib/idp_common_pkg/Makefile."
    )
    for makefile in (MAKEFILE, LIBRARY_MAKEFILE):
        text = makefile.read_text(encoding="utf-8")
        assert "make/hermetic_aws.mk" in text, (
            f"{makefile.relative_to(REPO_ROOT)} no longer includes "
            f"make/hermetic_aws.mk, so its {PYTEST_VAR} is either undefined — "
            "which silently runs pytest with no arguments at all — or a private "
            "copy that can drift from the shared one."
        )
        assert f"{WRAPPER_VAR} :=" not in text and f"{WRAPPER_VAR}:=" not in text, (
            f"{makefile.relative_to(REPO_ROOT)} defines {WRAPPER_VAR} itself "
            "instead of including the shared definition."
        )


@pytest.mark.parametrize("target", LIBRARY_HERMETIC_TARGETS)
def test_library_unit_targets_run_hermetically(target: str):
    """The idp_common_pkg unit suite is gated by its own make target, not by
    test-packages-cicd, so it needs the wrapper applied there too.

    It was not, and the consequence was the mirror image of #988's region case: the
    suite resolved AWS *credentials* from the EC2 instance metadata service on a
    developer box and from nothing at all on a runner. botocore freezes the
    session's credentials into a client at construction, so a client a test module
    built at import time was frozen unusable on CI only — five tests failed there
    and passed everywhere else.
    """
    lines = _runnable(_recipe_lines(LIBRARY_MAKEFILE, target))
    # Match on "runs the interpreter", not on the word "pytest". A wrapped line says
    # `$(PYTEST_HERMETIC)` and never says "pytest", and an unwrapped one can avoid
    # the word too — `$(PYTHON) -m $(PYTEST_MOD)` runs pytest and contains neither —
    # so a "pytest" substring search would report a recipe that runs no tests at all
    # and pass. Unlike test-packages-cicd these recipes do have a legitimate
    # non-interpreter line (the editable install), so the filter cannot simply be
    # "every runnable line".
    interpreter_lines = [
        ln
        for ln in lines
        if "pytest" in ln or f"$({PYTEST_VAR})" in ln or "$(PYTHON)" in ln
    ]
    assert interpreter_lines, (
        f"{target} in {LIBRARY_MAKEFILE.relative_to(REPO_ROOT)} runs no interpreter "
        "command, so this check has nothing to assert against"
    )
    unwrapped = [ln for ln in interpreter_lines if f"$({PYTEST_VAR})" not in ln]
    assert not unwrapped, (
        f"these {target} lines run the interpreter without $({PYTEST_VAR}):\n  "
        + "\n  ".join(unwrapped)
        + f"\n\nUse $({PYTEST_VAR}) instead of $(PYTHON) -m pytest, so the suite "
        "runs with the ambient AWS region, credentials and profile removed — the "
        "environment a CI runner actually has (#988)."
    )


def test_library_integration_target_keeps_the_real_aws_environment():
    """The inverse assertion, so the wrapper is not applied where it would break.

    ``test-integration`` calls real AWS. Stripping its credentials would turn a
    working integration run into an authentication error, so the next person
    tempted to apply the wrapper uniformly finds out here.
    """
    lines = _runnable(_recipe_lines(LIBRARY_MAKEFILE, LIBRARY_NON_HERMETIC_TARGET))
    wrapped = [ln for ln in lines if f"$({PYTEST_VAR})" in ln]
    assert not wrapped, (
        f"{LIBRARY_NON_HERMETIC_TARGET} runs through $({PYTEST_VAR}):\n  "
        + "\n  ".join(wrapped)
        + "\n\nIntegration tests need the machine's real credentials and region; "
        "the wrapper removes both."
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
            '`os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")` before '
            "collection (see src/lambda/test_file_copier/conftest.py), or, if the "
            "client sits in a library that many callers import, build it lazily "
            "(see idp_common/utils/settings_helper.py)."
            "\n\nKnow what the conftest route costs before taking it. That "
            "`setdefault` re-supplies the region for EVERYTHING that suite imports, "
            "not just the module that needed it, so this check stops seeing "
            "import-time AWS calls anywhere under that directory. Two handler "
            "modules under patterns/unified/src sat behind one of them and built "
            "regional-only clients at import for a release. Prefer the lazy client "
            "when the suite is large or imports handler code; if you do add the "
            "conftest, say in it which module needs the region and why."
        )
    pytest.fail(
        f"`pytest --collect-only {' '.join(args)}` in {workdir} exited "
        f"{result.returncode} with no AWS region, credentials or profile in the "
        f"environment — the environment a CI runner has.{hint}\n\n{output}"
    )
