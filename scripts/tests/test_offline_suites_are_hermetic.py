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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_recipes  # noqa: E402

sys.path.pop(0)

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


def _make_variable_defaults() -> dict[str, str]:
    """``NAME``→value for the simple variable assignments in the root ``Makefile``.

    Only needed for variables that appear INSIDE a recipe's pytest arguments, which
    today is ``$(PYTEST_XDIST)``. Resolving them rather than ignoring them is what
    keeps :func:`_target_paths` honest: an unexpanded ``$(NAME)`` does not start
    with ``-``, so it would be collected as a test path and probed as one, and a
    variable whose value genuinely did name a path would be silently skipped if the
    token were merely dropped instead.
    """
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)\s*(?:\?=|:=|=)\s*(.*)$")
    defaults: dict[str, str] = {}
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            defaults.setdefault(match.group(1), match.group(2).strip())
    return defaults


def _expand_make_vars(text: str) -> str:
    """Substitute ``$(NAME)`` for its root-``Makefile`` default, except the wrapper.

    ``$(PYTEST_HERMETIC)`` is deliberately left alone: it is the marker
    :func:`_pytest_invocations` splits on, and this test supplies the wrapper and
    interpreter itself rather than reading them from the recipe.
    """
    defaults = _make_variable_defaults()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == PYTEST_VAR:
            return match.group(0)
        assert name in defaults, (
            f"recipe line references $({name}), which is not assigned in "
            f"{MAKEFILE.relative_to(REPO_ROOT)}. Either it is a typo, or it is "
            "defined somewhere this parser does not read — in which case the "
            "pytest arguments below cannot be read correctly."
        )
        return defaults[name]

    return re.sub(r"\$\(([A-Z][A-Z0-9_]*)\)", replace, text)


def _pytest_invocations() -> list[tuple[str, list[str]]]:
    """Every pytest run in the recipe, as (directory relative to the repo root, args).

    ``$(PYTEST_HERMETIC)`` expands to the wrapper plus ``python -m pytest``; this
    test supplies those itself, so only the arguments after it are extracted. Any
    OTHER make variable in the argument list is expanded first — see
    :func:`_expand_make_vars`.
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
        invocations.append((workdir, shlex.split(_expand_make_vars(args))))
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


def test_the_integration_recipe_really_resolves_the_machine_s_region():
    """The same inverse assertion, measured rather than read.

    The check above reads one spelling: it fails if the recipe names
    ``$(PYTEST_HERMETIC)``. Anything else that strips the environment satisfies it
    — ``$(HERMETIC_AWS) $(PYTHON) -m pytest``, an inlined ``env -u AWS_PROFILE``,
    or a future wrapper that merges the two — and the suite then fails to
    authenticate for a reason no test names.

    So this runs the command ``make test-integration`` would actually run, with a
    region supplied through the environment, and requires that a boto3 client
    built under it resolves that region. It is the mirror image of
    :func:`test_wrapper_really_leaves_no_resolvable_region`: the same measurement,
    the opposite expected answer, on the one target that must keep what the others
    must lose.

    This is not an argument against pinning that target's ``PYTHONPATH``, which is
    a separate wrapper and is asserted in
    ``test_first_party_pythonpath.test_the_integration_recipe_pins_this_checkout_too``.
    """
    command = make_recipes.dry_run_command(
        LIBRARY_NON_HERMETIC_TARGET, LIBRARY_MAKEFILE.parent
    )
    probe = (
        "import boto3\n"
        "try:\n"
        f"    client = boto3.client({PROBE_SERVICE!r})\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__)\n"
        "else:\n"
        "    print('RESOLVED', client.meta.region_name)\n"
    )
    result = make_recipes.run_probe(
        command, probe, cwd=LIBRARY_MAKEFILE.parent, env=_polluted_env()
    )
    assert result.returncode == 0, (
        f"the region probe failed to run under `{command}`: {result.stderr[-2000:]}"
    )
    assert result.stdout.strip() == f"RESOLVED {SENTINEL_REGION}", (
        f"`make {LIBRARY_NON_HERMETIC_TARGET}` runs `{command}`, and under it a "
        f"boto3 client answered {result.stdout.strip()!r} rather than the region "
        f"the environment supplied. Integration tests call real AWS: something in "
        f"that command is removing the region or the profile. See "
        f"$({PYTEST_VAR}) — which this target must not use — and "
        f"$(PYTEST_FIRST_PARTY), which pins PYTHONPATH and leaves the AWS "
        f"environment alone."
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


# ---------------------------------------------------------------------------
# The coverage producer: the one pytest invocation that is not a `make` recipe
# ---------------------------------------------------------------------------
#
# `scripts/coverage_all.py` runs each tree's suite from Python, so no amount of
# checking `make` recipes reaches it, and the checks above stop at
# `test-packages-cicd`. It therefore sat outside the control's universe rather than
# being carved out of it — the shape this repository keeps rediscovering — and the
# cost was not a suite that fails on a runner but something quieter: the figures it
# produces are a ratchet's **input**, and `check_coverage_debt.py --write` records
# them permanently.
#
# `boto3.DEFAULT_SESSION` is created by the first bare `boto3.client(...)` in a
# process and caches the credentials it resolved, so a `monkeypatch.setenv` applied
# afterwards is ignored by every later client in that process. Whether a
# `try: ...get_caller_identity() / except:` branch is covered then depends on which
# test ran *first in its worker*, which xdist's `--dist load` decides afresh each
# run. Measured on `idp_feature_sdk`, 461 tests and 0 skips in every cell:
# `pack.py` at 99.44% or 100.00% from one run to the next at a fixed `-n 4`, and
# 100.00% at every worker count once the AWS environment is neutralised. #1377.


def _coverage_all():
    """`scripts/coverage_all.py`, imported by path (it is a script, not a package)."""
    import importlib.util

    path = REPO_ROOT / "scripts" / "coverage_all.py"
    spec = importlib.util.spec_from_file_location("coverage_all_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_coverage_producer_neutralises_everything_the_wrapper_does():
    """Its variable list is DERIVED from the Makefile here, never compared by eye.

    Two copies of a list of environment variables drift, and the direction that
    matters is the quiet one: a variable added to `HERMETIC_AWS` because it turned
    out to be a credential source, and not added here, leaves the coverage producer
    reading that source while every `make` recipe no longer does.
    """
    unset, assigned = _hermetic_spec()
    module = _coverage_all()
    missing = sorted(set(unset) - set(module.HERMETIC_AWS_UNSET))
    assert not missing, (
        f"$({WRAPPER_VAR}) unsets {missing} and coverage_all.HERMETIC_AWS_UNSET does "
        f"not, so `scripts/coverage_all.py` measures every tree with those AWS "
        f"sources still readable while every `make` recipe strips them. Add them to "
        f"HERMETIC_AWS_UNSET in scripts/coverage_all.py."
    )
    assert module.HERMETIC_AWS_SET == assigned, (
        f"$({WRAPPER_VAR}) assigns {assigned} and coverage_all.HERMETIC_AWS_SET has "
        f"{module.HERMETIC_AWS_SET}. These neutralise the sources no unset can reach "
        f"(the shared config files, and IMDS), so a difference here is a difference "
        f"in what the coverage figures measure."
    )


def test_the_coverage_producer_really_leaves_no_resolvable_credentials():
    """Measured through the producer's own function, not inferred from its list.

    The test above compares two lists; this one requires that putting a *polluted*
    environment through `coverage_all.hermetic_aws_env` leaves botocore nothing to
    resolve. A list that looks right while the function fails to apply it — reading
    `os.environ` afresh, say, or updating a copy — passes the comparison and fails
    here.
    """
    env = _coverage_all().hermetic_aws_env(_polluted_env())
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    probe = (
        "import boto3\n"
        "creds = boto3.Session(region_name='us-east-1').get_credentials()\n"
        "print('NONE' if creds is None else 'RESOLVED '"
        " + (creds.access_key or '<empty>'))\n"
    )
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
        f"under coverage_all.hermetic_aws_env botocore still resolved credentials "
        f"(probe said {result.stdout.strip()!r}). Every tree's coverage is then "
        f"measured against whichever account this machine is signed in to, so a "
        f"branch guarding a live AWS call is covered or not according to which test "
        f"ran first in each xdist worker, and the figure moves run to run. See #1377."
    )


def test_every_coverage_tree_is_measured_through_the_neutralisation(monkeypatch):
    """The function existing is not the function being called, for every tree.

    This is the half that catches a control present and not consulted: it intercepts
    the `subprocess.run` `coverage_all.run` makes and inspects the environment that
    pytest would actually have received, for each registry tree in turn. A `run`
    that neutralises every tree but one fails here naming it.
    """
    module = _coverage_all()
    unset, assigned = _hermetic_spec()
    seen: dict[str, dict[str, str]] = {}

    class _Result:
        returncode = 0

    real_run = subprocess.run

    def _capture(cmd, cwd=None, env=None, **kwargs):
        # ⚠️ `git` passes THROUGH, and the reason is not cosmetic. `coverage_all` and
        # `check_coverage_debt` share one `subprocess` module object, so replacing `run`
        # on it also replaces the one the fan-out trees derive their pytest roots with
        # (`check_coverage_debt.fanout_roots` asks `git ls-files`). A blanket stub makes
        # that derivation see no output at all, so those trees are measured over nothing
        # and this probe captures no invocation for them -- it would pass while asserting
        # about fewer trees than the registry holds, which is the shape of vacuity this
        # whole module is about.
        if cmd and cmd[0] == "git":
            return real_run(cmd, cwd=cwd, env=env, **kwargs)
        seen[str(cwd)] = dict(env or {})
        return _Result()

    # The producer builds its environment from `os.environ`, so the pollution has to
    # be visible there: a probe built from the ambient environment measures nothing
    # on a runner, which has no AWS environment to strip, and would pass for the
    # wrong reason. Same argument as `_polluted_env`'s.
    monkeypatch.setattr(os, "environ", _polluted_env())
    monkeypatch.setattr(module.subprocess, "run", _capture)
    for tree in module._ccd.TREES:
        module.run(tree, sys.executable, parallel=False)

    assert len(seen) == len({str(REPO_ROOT / t.cwd) for t in module._ccd.TREES}), (
        f"expected one measured invocation per distinct tree directory, captured "
        f"{sorted(seen)}. A parse or a registry change has made this probe vacuous."
    )
    for cwd, env in sorted(seen.items()):
        leaked = sorted(name for name in unset if name in env)
        assert not leaked, (
            f"the pytest invocation coverage_all.py makes in {cwd} still carries "
            f"{leaked}, so that tree's coverage is measured against this machine's "
            f"AWS environment. Route it through coverage_all.hermetic_aws_env. #1377."
        )
        for name, value in assigned.items():
            assert env.get(name) == value, (
                f"the pytest invocation in {cwd} has {name}={env.get(name)!r}, "
                f"expected {value!r}. That source is one no unset can reach, so "
                f"leaving it is enough to make the figures machine-dependent. #1377."
            )


# ---------------------------------------------------------------------------
# The test runner: the other pytest caller that is not a `make` recipe
# ---------------------------------------------------------------------------
#
# `scripts/run_all_tests.py` is what `make test` invokes, so it is the command a
# developer actually types, and it sat outside this control's universe for exactly
# the same reason `coverage_all.py` did: it builds its own subprocess environment in
# Python and spawns pytest itself, so no amount of checking `make` recipes reaches
# it. Nobody decided it should be unprotected.
#
# What that cost, measured over the registered roots in a bare environment with
# `botocore.endpoint.Endpoint._send` replaced by a recorder that appends the request
# and then raises — a seam strictly below moto's `before-send` short-circuit, so a
# record is exactly an escape, and below credential resolution, so the signing
# credential can be classified by shape without transmitting it. 69 roots, 62 of
# which reach nothing; the other seven produced 391 requests that would have been
# sent, 48 of them signed with credentials of a real shape: CloudWatch
# `PutMetricData` from four roots (35 calls, all writes), S3 `GET` from
# `benchmarks/tests`, DynamoDB `Scan` from `src/lambda/save_reporting_data`, and one
# Secrets Manager `GetSecretValue` from a test whose `patch.dict(os.environ, ...,
# clear=True)` erased its own conftest's placeholder credentials mid-test. Every CI
# recipe wrapped its pytest in `$(PYTEST_HERMETIC)`, so CI never saw any of it: the
# gates were green and correct, and `make test` was simply not equivalent to what CI
# runs. Issue #1398.
#
# The three checks below are the same three the coverage producer gets, for the same
# reasons, plus the one asymmetry this caller has: `--integration` must NOT be
# stripped, because those suites exist to call real AWS.


def _run_all_tests():
    """`scripts/run_all_tests.py`, imported by path (it is a script, not a package)."""
    import importlib.util

    path = REPO_ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location("run_all_tests_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_two_python_pytest_callers_share_one_definition():
    """One list of credential sources, not one per caller.

    The defect this guards is the one that produced #1398 out of #1377: the
    neutralisation was written for `coverage_all.py`, and the second Python caller
    needing it was not noticed. A copy would have drifted the same way — so both
    read `scripts/hermetic_aws_env.py`, and identity (not equality) is what is
    asserted, because two equal copies are exactly the state this prevents.
    """
    coverage = _coverage_all()
    runner = _run_all_tests()
    assert coverage.hermetic_aws_env is runner.hermetic_aws_env, (
        "scripts/coverage_all.py and scripts/run_all_tests.py are using different "
        "`hermetic_aws_env` functions. Both must import the one in "
        "scripts/hermetic_aws_env.py; a second copy of a list of credential sources "
        "drifts, and the direction that matters is the quiet one — a source added to "
        "$(HERMETIC_AWS) and not to the copy leaves that caller reading it."
    )


def test_the_test_runner_neutralises_everything_the_wrapper_does():
    """Its variable list is DERIVED from the Makefile here, never compared by eye."""
    unset, assigned = _hermetic_spec()
    module = _run_all_tests()
    shared = sys.modules[module.hermetic_aws_env.__module__]
    missing = sorted(set(unset) - set(shared.HERMETIC_AWS_UNSET))
    assert not missing, (
        f"$({WRAPPER_VAR}) unsets {missing} and hermetic_aws_env.HERMETIC_AWS_UNSET "
        f"does not, so `make test` runs every root with those AWS sources still "
        f"readable while every CI recipe strips them. That asymmetry is #1398."
    )
    assert shared.HERMETIC_AWS_SET == assigned, (
        f"$({WRAPPER_VAR}) assigns {assigned} and hermetic_aws_env.HERMETIC_AWS_SET "
        f"has {shared.HERMETIC_AWS_SET}. These neutralise the sources no unset can "
        f"reach (the shared config files, and IMDS), and a developer machine's "
        f"`[default]` profile is a resolvable credential with no AWS_* variable set "
        f"at all."
    )


def test_the_test_runner_really_leaves_no_resolvable_credentials():
    """Measured through the function the runner calls, not inferred from its list."""
    module = _run_all_tests()
    env = module.hermetic_aws_env(_polluted_env())
    env["AWS_DEFAULT_REGION"] = "us-east-1"
    probe = (
        "import boto3\n"
        "creds = boto3.Session(region_name='us-east-1').get_credentials()\n"
        "print('NONE' if creds is None else 'RESOLVED '"
        " + (creds.access_key or '<empty>'))\n"
    )
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
        f"under run_all_tests' neutralisation botocore still resolved credentials "
        f"(probe said {result.stdout.strip()!r}). `make test` would then sign real "
        f"requests with them from unit suites that were never meant to reach a "
        f"network, most of them writes. See #1398."
    )


def _captured_root_environments(monkeypatch, integration: bool) -> dict[str, dict]:
    """Run `run_gate` over every registered root with the subprocess intercepted.

    Returns root → the environment pytest would have been given. Nothing is executed
    and nothing is written into the repository: `REPORT_DIR` is redirected and the
    summary goes with it.
    """
    module = _run_all_tests()
    seen: dict[str, dict[str, str]] = {}
    real_run = subprocess.run

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _capture(cmd, cwd=None, env=None, **kwargs):
        # Only the pytest invocations. `run_gate` also shells out to `git rev-parse`
        # for the summary it writes, and swallowing that would both add a phantom
        # entry here and make the probe's own count assertion wrong.
        if not (isinstance(cmd, (list, tuple)) and "pytest" in cmd):
            return real_run(cmd, cwd=cwd, env=env, **kwargs)
        # The root is the last argument of the pytest command line the runner builds.
        seen[str(cmd[-1])] = dict(env or {})
        return _Result()

    # The pollution has to be visible in `os.environ`, because that is what the
    # runner builds its child environment from — and a probe built from the ambient
    # environment measures nothing on a runner, which has no AWS environment to
    # strip. Same argument as `_polluted_env`'s.
    polluted = _polluted_env()
    # ⚠️ The ambient PYTHONPATH has to go, or the pin assertion below passes for the
    # wrong reason: this suite is itself run with the checkout's roots pinned, so a
    # runner that had LOST the pin entirely and merely inherited `os.environ` would
    # still hand its children a PYTHONPATH starting with this checkout. Found by
    # mutation — `child_env = dict(os.environ)` in place of
    # `pinned_environment(REPO_ROOT)` survived until this line existed.
    polluted["PYTHONPATH"] = "/nonexistent/ambient-pythonpath"
    monkeypatch.setattr(os, "environ", polluted)
    monkeypatch.setattr(module, "REPORT_DIR", Path(tempfile.mkdtemp()) / "reports")
    monkeypatch.setattr(module.subprocess, "run", _capture)
    roots = [r for r in module.RUN_ROOTS if (REPO_ROOT / r).exists()]
    assert roots, "no registered test root exists on disk; this probe is vacuous"
    module.run_gate(roots, integration=integration)
    assert len(seen) == len(roots), (
        f"expected one intercepted invocation per registered root, captured "
        f"{len(seen)} of {len(roots)}. A parse or a registry change has made this "
        f"probe vacuous."
    )
    return seen


def test_every_test_root_runs_through_the_neutralisation(monkeypatch):
    """The function existing is not the function being called, for every root.

    This is the half that catches a control present and not consulted. It intercepts
    the `subprocess.run` the runner makes and inspects the environment pytest would
    actually have received, per root. A runner that neutralises some roots and not
    others fails here naming them.
    """
    unset, assigned = _hermetic_spec()
    seen = _captured_root_environments(monkeypatch, integration=False)
    for root, env in sorted(seen.items()):
        leaked = sorted(name for name in unset if name in env)
        assert not leaked, (
            f"the pytest invocation `make test` makes for {root} still carries "
            f"{leaked}, so that suite runs against this machine's AWS environment "
            f"and an unmocked client in it transacts with whichever account the "
            f"developer is signed in to. Route it through "
            f"hermetic_aws_env.hermetic_aws_env. #1398."
        )
        for name, value in assigned.items():
            assert env.get(name) == value, (
                f"the pytest invocation for {root} has {name}={env.get(name)!r}, "
                f"expected {value!r}. That source is one no unset can reach — a "
                f"`[default]` profile in the shared credentials file needs no AWS_* "
                f"variable at all — so leaving it is enough to put the whole run "
                f"back on the machine's real credentials. #1398."
            )
        assert env.get("PYTHONPATH", "").startswith(str(REPO_ROOT)), (
            f"the pytest invocation for {root} lost this checkout's PYTHONPATH pin "
            f"(PYTHONPATH={env.get('PYTHONPATH')!r}). The neutralisation must compose "
            f"with the pin, not replace it: without the pin `import idp_common` "
            f"follows the editable-install pointer and the gate reports on another "
            f"checkout, green (#1094)."
        )


def test_the_integration_run_keeps_the_real_aws_environment(monkeypatch):
    """`--integration` must NOT be stripped: those suites exist to call real AWS.

    The same split `make/hermetic_aws.mk` draws between `$(PYTEST_HERMETIC)` and
    `$(PYTEST_FIRST_PARTY)`. Asserted in this direction too because a neutralisation
    applied to everything would not fail loudly — it would turn
    `make test-integration-all` into a suite that cannot reach the infrastructure it
    exists to test, which reads as a broken deployment rather than a broken gate.
    """
    seen = _captured_root_environments(monkeypatch, integration=True)
    region_kept = [
        root for root, env in seen.items() if env.get("AWS_DEFAULT_REGION") is None
    ]
    assert not region_kept, (
        f"the integration run strips AWS_DEFAULT_REGION for {sorted(region_kept)[:5]}"
        f". `make test-integration-all` calls real AWS; stripping its region and "
        f"credentials removes the thing those suites test."
    )
    for root, env in sorted(seen.items()):
        assert env.get("AWS_ACCESS_KEY_ID") == SENTINEL_VALUE, (
            f"the integration invocation for {root} no longer carries the machine's "
            f"credentials. Those suites exist to call real AWS."
        )
        assert env.get("PYTHONPATH", "").startswith(str(REPO_ROOT)), (
            f"the integration invocation for {root} lost the checkout's PYTHONPATH "
            f"pin. Needing real credentials is not a reason to lose track of which "
            f"tree is under test (#1094)."
        )
