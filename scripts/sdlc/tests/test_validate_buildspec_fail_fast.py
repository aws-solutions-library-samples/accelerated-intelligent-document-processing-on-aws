"""The buildspec gate refuses a loop that cannot report its own failure (#1310).

Three separate claims are pinned here, and they are worth keeping apart.

1. **The loop really does fail the build now.** `test_the_real_loop_*` extract the
   shell block out of each shipped buildspec, run it against a stubbed `docker`,
   and assert the behaviour: a transient failure is retried and recovers, a
   permanent one exits non-zero, and the images after the failing one are not
   attempted. This is the only test here that measures the fix rather than the
   gate, and it is the one that would have caught #1310.

2. **The gate's rule works.** A loop without errexit is an error; every spelling
   of errexit clears it; `set +e` before the loop re-opens it; a loop fed by a
   pipe or wrapped in a subshell is still a loop; a heredoc body is not shell.

3. **The gate is reached.** `make validate-buildspec` must hand it every
   buildspec in the tree, not the one the old `patterns/*/buildspec.yml` glob
   matched. Of the three files that glob skipped, two carried the #1310 defect;
   the glob did read the file the issue was filed against, so the discovery gap
   is a forward-looking fix rather than the diagnosis for #1310.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# Shared rather than a fourth hand-rolled one: `test_log_group_encryption`
# counts the copies of this in the tree, and a SafeLoader subclass is the point
# (`test_cfn_loader_safety` enforces that no loader here can execute a tag).
from test_config_schema_order import _CfnSafeLoader

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = REPO_ROOT / "scripts" / "sdlc" / "validate_buildspec.py"

# The name shape the Makefile's discovery matches, kept here independently so a
# change to one side has to be made on the other deliberately.
BUILDSPEC_NAME = re.compile(r"(?:^|/)buildspec[^/]*\.ya?ml$")


def _run(tmp_path: Path, buildspec: dict) -> subprocess.CompletedProcess:
    path = tmp_path / "buildspec.yml"
    path.write_text(yaml.safe_dump(buildspec, default_flow_style=False))
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(path)],
        capture_output=True,
        text=True,
    )


def _spec(command: str) -> dict:
    return {"version": 0.2, "phases": {"build": {"commands": [command]}}}


LOOP_BODY = "  docker buildx build --push -t $REPO:$f .\ndone\n"


def test_a_loop_without_errexit_is_an_error(tmp_path: Path):
    result = _run(tmp_path, _spec("for f in a b c; do\n" + LOOP_BODY))
    assert result.returncode == 1, result.stdout
    assert "does not enable errexit" in result.stdout
    assert "#1310" in result.stdout


def test_errexit_before_the_loop_clears_it(tmp_path: Path):
    result = _run(tmp_path, _spec("set -e\nfor f in a b c; do\n" + LOOP_BODY))
    assert result.returncode == 0, result.stdout
    assert "errexit" not in result.stdout.replace(str(tmp_path), "")


@pytest.mark.parametrize("setline", ["set -eu", "set -euo pipefail", "set -o errexit"])
def test_the_other_spellings_of_errexit_count(tmp_path: Path, setline: str):
    result = _run(tmp_path, _spec(f"{setline}\nfor f in a b; do\n" + LOOP_BODY))
    assert result.returncode == 0, result.stdout


def test_pipefail_alone_is_not_errexit(tmp_path: Path):
    """`-o pipefail` changes how a *pipeline's* status is computed and does not
    make the shell exit on one, so it must not satisfy this check."""
    result = _run(tmp_path, _spec("set -o pipefail\nfor f in a b; do\n" + LOOP_BODY))
    assert result.returncode == 1, result.stdout
    assert "does not enable errexit" in result.stdout


def test_errexit_after_the_loop_does_not_count(tmp_path: Path):
    """Ordering is the whole point: a `set -e` the loop has already run past
    protects nothing, and a check that only asked whether the string appears
    anywhere in the block would pass this."""
    result = _run(tmp_path, _spec("for f in a b; do\n" + LOOP_BODY + "set -e\n"))
    assert result.returncode == 1, result.stdout


def test_a_single_line_loop_is_left_alone(tmp_path: Path):
    """CodeBuild judges a single-line command on its own exit status, so a
    one-line loop aborts the phase without errexit and needs no flag."""
    result = _run(tmp_path, _spec("for f in a b; do echo $f; done"))
    assert result.returncode == 0, result.stdout


def test_the_word_for_in_prose_is_not_a_loop(tmp_path: Path):
    """The pattern anchors on statement position. An echo that merely contains
    the word would otherwise demand errexit of every buildspec in the tree."""
    result = _run(
        tmp_path,
        _spec('echo "building images for every function"\necho "second line"\n'),
    )
    assert result.returncode == 0, result.stdout


# --- the rule's reach: shapes a naive anchor set would miss or over-flag -------
#
# Driven through the validator's own checker rather than the CLI, so a case can be
# stated in one line. `(name, command, expect_error)`.
RULE_CASES = [
    # A `while` fed by a PIPE is the most natural way to write this loop and
    # carries the identical defect, so it has to be a match.
    ("pipe-fed while", "cat l | while read f; do\n  docker build .\ndone\n", True),
    ("subshell loop", "( for f in a b; do\n  docker build .\ndone )\n", True),
    ("brace-group loop", "{ for f in a b; do\n  docker build .\ndone ; }\n", True),
    ("after a background job", "x & for f in a; do\n  docker build .\ndone\n", True),
    # errexit genuinely IS enabled here; rejecting it would tell a contributor to
    # add a `set -e` they already have.
    ("set -o pipefail -e", "set -o pipefail -e\nfor f in a; do\n b\ndone\n", False),
    ("set -ex", "set -ex\nfor f in a; do\n b\ndone\n", False),
    ("set -o errexit", "set -o errexit\nfor f in a; do\n b\ndone\n", False),
    # `-o pipefail` changes how a PIPELINE's status is computed; it does not make
    # the shell exit on one.
    ("pipefail alone", "set -o pipefail\nfor f in a; do\n b\ndone\n", True),
    # The realistic way this protection gets removed later.
    ("set -e then set +e", "set -e\nq\nset +e\nfor f in a; do\n b\ndone\n", True),
    (
        "set -e then +o errexit",
        "set -e\nset +o errexit\nfor f in a; do\n b\ndone\n",
        True,
    ),
    ("set +e then set -e", "set +e\nq\nset -e\nfor f in a; do\n b\ndone\n", False),
    # A heredoc body is data, and often another language. `set -e` is not a
    # remedy for an embedded Python loop, so demanding it would be unanswerable.
    ("heredoc python loop", "python3 - <<'EOF'\nfor i in r(3):\n  p(i)\nEOF\n", False),
    (
        "heredoc cannot supply errexit",
        "cat <<'EOF'\nset -e\nEOF\nfor f in a; do\n b\ndone\n",
        True,
    ),
    ("indented heredoc", "cat <<-EOF\n\tfor x in y; do\n\tdone\n\tEOF\n", False),
    ("comment is not a statement", "# for each function\necho hi\necho there\n", False),
    ("elif is not a loop", "if x; then\n  :\nelif y; then\n  :\nfi\n", False),
    # ⚠️ A FALSE heredoc match blanks every line after it, which switches the
    # check off for the rest of the command -- the most expensive direction for a
    # false positive here. Arithmetic shift is the realistic case: `sleep
    # $((1 << attempt))` is the natural next edit to buildspec.yml's retry ladder,
    # and it sits above the image loop.
    (
        "arithmetic shift is not a heredoc",
        "n=$((1 << N))\nfor f in a; do\n b\ndone\n",
        True,
    ),
    ("a herestring has no body", "cat <<<WORD\nfor f in a; do\n b\ndone\n", True),
    (
        "an unterminated heredoc restores its lines",
        "cat <<EOF\nhello\nfor f in a; do\n b\ndone\n",
        True,
    ),
    # A `-e` that only appears in a trailing comment never executed.
    (
        "errexit in a trailing comment",
        "set -x  # remember -e someday\nfor f in a; do\n b\ndone\n",
        True,
    ),
    # The regression this PR's own fix created: the retry helper's `until` loop
    # sits ABOVE the image loop, so a check that stopped at the first match
    # stopped reading before the loop it exists to protect.
    (
        "a second loop is judged too",
        "set -e\nuntil q; do\n r\ndone\nset +e\nfor f in a; do\n b\ndone\n",
        True,
    ),
    (
        "two guarded loops are both fine",
        "set -e\nuntil q; do\n r\ndone\nfor f in a; do\n b\ndone\n",
        False,
    ),
]


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_buildspec", VALIDATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BuildspecValidator


@pytest.mark.parametrize(
    ("name", "command", "expect_error"),
    [pytest.param(*c, id=c[0].replace(" ", "-")) for c in RULE_CASES],
)
def test_the_rule_reaches_these_shapes(name: str, command: str, expect_error: bool):
    cls = _load_validator()
    validator = cls("<probe>")
    validator._check_one_command("build", 1, cls._strip_heredocs(command))
    assert bool(validator.errors) is expect_error, f"{name}: errors={validator.errors}"


def test_a_loop_in_a_finally_block_is_checked_too(tmp_path: Path):
    """`finally` is a sibling command list CodeBuild also executes, and
    `PHASE_FIELDS` already knows it exists, so reading only `commands` would
    leave a real command list unchecked."""
    path = tmp_path / "buildspec.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 0.2,
                "phases": {
                    "build": {
                        "commands": ["echo hi"],
                        "finally": ["for f in a b; do\n" + LOOP_BODY],
                    }
                },
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(path)], capture_output=True, text=True
    )
    assert result.returncode == 1, result.stdout
    assert "build.finally" in result.stdout


# --- the fix itself, measured by running the shipped shell --------------------

# A stub `docker` that fails only for ONE named function, which is what makes the
# exit-code assertion mean anything.
#
# ⚠️ Failing the first N calls regardless of target does NOT reproduce #1310: if
# every image fails, the LAST iteration fails too, so the block's status is
# non-zero and even the unfixed loop exits 1. The bug needs an EARLIER image to
# fail and a LATER one to succeed — then the last iteration's success is the
# block's status and the failure disappears. Measured: the pre-fix block exits 1
# under fail-everything and 0 under fail-one-early.
#
# `STUB_FAIL_FOR` is matched against the whole argument list, and
# `STUB_FAIL_TIMES` (0 = always) bounds it so the retry ladder can recover.
DOCKER_STUB = """#!/bin/bash
echo "docker-call $*"
case " $* " in
  *"$STUB_FAIL_FOR"*)
    n=$(cat "$STUB_DIR/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$STUB_DIR/count"
    if [ "${STUB_FAIL_TIMES:-0}" = "0" ] || [ "$n" -le "$STUB_FAIL_TIMES" ]; then
      echo "ERROR: failed to build"; exit 1
    fi
    ;;
esac
exit 0
"""

BUILDSPECS_WITH_LOOPS = [
    "patterns/unified/buildspec.yml",
    "patterns/unified/buildspec-bda.yml",
    "patterns/unified/buildspec-pipeline.yml",
]


def _build_block(buildspec: str) -> str:
    """The one `build` command that carries the image loop, as shipped."""
    spec = yaml.safe_load((REPO_ROOT / buildspec).read_text())
    blocks = [
        c
        for c in spec["phases"]["build"]["commands"]
        if isinstance(c, str) and "buildx build" in c
    ]
    assert len(blocks) == 1, (
        f"{buildspec}: expected one image-build command, got {len(blocks)}"
    )
    return blocks[0]


def _run_block(
    tmp_path: Path,
    buildspec: str,
    functions: list[str],
    fail_for: str,
    fail_times: int = 0,
    block: str | None = None,
):
    """Run a buildspec's image-build block over fake functions against the stub.

    The `for func_var in ...` list is replaced so the probe builds two cheap fake
    images instead of fifteen real ones, and the backoff sleeps are zeroed. Every
    other line -- the retry ladder, the `set -e`, the argument assembly -- is the
    file's own text. `block` overrides the source, which is how the pre-fix
    comparison below feeds in the version from `develop`.
    """
    stub_dir = tmp_path / "stub"
    (stub_dir / "bin").mkdir(parents=True)
    docker = stub_dir / "bin" / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)

    source = block if block is not None else _build_block(buildspec)
    source = re.sub(
        r"for func_var in .*?; do",
        f"for func_var in {' '.join(functions)}; do",
        source,
        count=1,
    )
    source = source.replace("sleep $((attempt * 15))", "sleep 0")

    env = {
        **os.environ,
        "PATH": f"{stub_dir / 'bin'}:{os.environ['PATH']}",
        "STUB_DIR": str(stub_dir),
        "STUB_FAIL_FOR": fail_for,
        "STUB_FAIL_TIMES": str(fail_times),
        "ECR_URI": "stub.ecr",
        "IMAGE_VERSION": "probe",
        **{name: f"patterns/unified/src/{name.lower()}" for name in functions},
    }
    return subprocess.run(
        ["bash", "-s"],
        input=source,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.parametrize("buildspec", BUILDSPECS_WITH_LOOPS)
def test_the_real_loop_fails_the_build_when_an_image_cannot_be_pushed(
    tmp_path: Path, buildspec: str
):
    """The #1310 assertion, and the one that measures the fix rather than the gate.

    Before the fix this block exited 0 -- the loop ran on past the failure and the
    command's status was the LAST iteration's -- so CodeBuild reported SUCCEEDED
    with an image missing from ECR. Two things are asserted: the exit status, and
    that the function AFTER the failing one is never attempted, which is what
    distinguishes an aborted loop from one that merely happened to end badly.
    """
    result = _run_block(
        tmp_path, buildspec, ["FUNCTION_AAA", "FUNCTION_ZZZ"], fail_for="aaa"
    )
    assert result.returncode != 0, f"exit 0 with a failed image\n{result.stdout}"
    assert "after 3 attempts" in result.stdout
    assert "zzz" not in result.stdout, (
        f"the loop continued past a failed image\n{result.stdout}"
    )


# The shape `patterns/unified/buildspec.yml` carried before this fix, pinned as a
# literal rather than read from git history: a `HEAD~1` lookup stops being the
# pre-fix version the moment another commit lands on the branch, and it answers
# `skip` on a shallow CI clone -- both of which retire the probe silently.
PRE_FIX_BLOCK = """\
for func_var in FUNCTION_AAA FUNCTION_ZZZ; do
  func_path="${!func_var}"
  func_name=$(basename "$func_path" | sed 's/_/-/g')
  echo "Building ${func_name} from ${func_path}..."
  docker buildx build \\
    --tag "${ECR_URI}:${func_name}-${IMAGE_VERSION}" \\
    --push \\
    .
done
"""


def test_the_unfixed_loop_really_did_report_success(tmp_path: Path):
    """Non-vacuity for the test above: the probe can see the defect at all.

    The point is narrow. Under fail-an-earlier-image the unfixed loop exits **0**
    and goes on to build the later one, which is #1310. Under fail-every-image it
    exits 1 even unfixed, because the last iteration fails too -- so a probe that
    failed everything would have certified the fix while measuring nothing, which
    is how this test was first written and what this case exists to stop.
    """
    result = _run_block(
        tmp_path,
        "patterns/unified/buildspec.yml",
        ["FUNCTION_AAA", "FUNCTION_ZZZ"],
        fail_for="aaa",
        block=PRE_FIX_BLOCK,
    )
    assert result.returncode == 0, "expected the unfixed loop to swallow the failure"
    assert "zzz" in result.stdout, (
        "expected the unfixed loop to carry on to the next image"
    )


def test_the_unfixed_loop_does_fail_when_every_image_fails(tmp_path: Path):
    """The companion half, and the reason the stub targets one function.

    This is the scenario a fail-everything stub produces, and the unfixed loop
    exits non-zero under it -- so an exit-code assertion driven that way holds
    before the fix as well as after, and proves nothing.
    """
    result = _run_block(
        tmp_path,
        "patterns/unified/buildspec.yml",
        ["FUNCTION_AAA", "FUNCTION_ZZZ"],
        fail_for="-",  # matches every invocation's argument list
        block=PRE_FIX_BLOCK,
    )
    assert result.returncode != 0


@pytest.mark.parametrize("buildspec", BUILDSPECS_WITH_LOOPS)
def test_the_real_loop_retries_a_transient_failure_and_recovers(
    tmp_path: Path, buildspec: str
):
    """#1310's own reproduction was a TLS handshake timeout against a private
    registry that succeeded on an unmodified re-run, so failing on the first
    attempt would trade an hour-long hang for a failed deploy on a blip."""
    result = _run_block(
        tmp_path, buildspec, ["FUNCTION_AAA"], fail_for="aaa", fail_times=2
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "attempt 1/3" in result.stdout and "attempt 2/3" in result.stdout
    assert result.stdout.count("docker-call") == 3, result.stdout


@pytest.mark.parametrize("buildspec", BUILDSPECS_WITH_LOOPS)
def test_post_build_does_not_claim_success_when_the_build_failed(buildspec: str):
    """#1310 names the misleading log line as part of the defect: CodeBuild runs
    post_build after a failed build phase, so an unconditional success echo is
    the log contradicting the build result."""
    spec = yaml.safe_load((REPO_ROOT / buildspec).read_text())
    commands = spec["phases"]["post_build"]["commands"]
    joined = "\n".join(c for c in commands if isinstance(c, str))
    assert "CODEBUILD_BUILD_SUCCEEDING" in joined, (
        f"{buildspec}: post_build reports success unconditionally"
    )
    for command in commands:
        if isinstance(command, str) and "successfully" in command:
            assert "CODEBUILD_BUILD_SUCCEEDING" in command, (
                f"{buildspec}: an unguarded success line survives: {command!r}"
            )


@pytest.mark.parametrize("buildspec", BUILDSPECS_WITH_LOOPS)
def test_the_image_loop_itself_is_still_inside_the_gates_reach(buildspec: str):
    """The gate must read the IMAGE loop, not merely the first loop it meets.

    This is the regression the #1310 fix created and the sharpest assertion in
    this file: `build_with_retry`'s `until` loop sits above the `for func_var`
    loop, so a checker using `search` stopped reading before the loop it exists
    to protect. Sabotaging the real file is the only way to show the gate is
    actually looking at that loop rather than being satisfied by the one in
    front of it.
    """
    source = (REPO_ROOT / buildspec).read_text()
    sabotaged = re.sub(
        r"^(\s*)(for func_var in )",
        r"\1set +e\n\1\2",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    assert sabotaged != source, "could not find the image loop to sabotage"

    block = [
        c
        for c in yaml.safe_load(sabotaged)["phases"]["build"]["commands"]
        if isinstance(c, str) and "buildx build" in c
    ][0]
    cls = _load_validator()
    validator = cls(buildspec)
    validator._check_one_command("build", 1, cls._strip_heredocs(block))
    assert validator.errors, (
        "a 'set +e' immediately before the image loop was not reported; the gate "
        "is not reading that loop"
    )
    assert "set +e" in validator.errors[0]

    # And both loops are seen, which is what makes the above a loop-coverage
    # result rather than an accident of where the sabotage landed.
    clean_block = [
        c
        for c in yaml.safe_load(source)["phases"]["build"]["commands"]
        if isinstance(c, str) and "buildx build" in c
    ][0]
    found = [m.group(1) for m in cls._LOOP.finditer(cls._strip_heredocs(clean_block))]
    assert found == ["until", "for"], found


@pytest.mark.parametrize("buildspec", BUILDSPECS_WITH_LOOPS)
def test_the_retry_is_bounded_by_a_deadline_not_only_an_attempt_count(buildspec: str):
    """An attempt count alone does not bound wall clock against the consumer.

    `DockerBuildRun` is a `Custom::CodeBuildRun`, so CloudFormation's 60-minute
    custom-resource timeout is the real budget. A build that eventually succeeds
    after burning through it produces the message #1310 is about, now in the
    recovering case — so the retry has to look at elapsed time, and the
    CodeBuild project's own timeout has to fall inside that budget rather than
    beyond it.
    """
    block = _build_block(buildspec)
    assert "RETRY_DEADLINE_SECONDS" in block
    assert '[ "$SECONDS" -ge "$RETRY_DEADLINE_SECONDS" ]' in block

    with (REPO_ROOT / "patterns/unified/template.yaml").open() as handle:
        template = yaml.load(handle, Loader=_CfnSafeLoader)  # noqa: S506
    timeout = template["Resources"]["DockerBuildProject"]["Properties"][
        "TimeoutInMinutes"
    ]
    assert timeout < 60, (
        f"DockerBuildProject TimeoutInMinutes is {timeout}, outside CloudFormation's "
        "60-minute custom-resource budget, so a slow build surfaces as the opaque "
        "'did not receive a response' message instead of CodeBuild's own timeout"
    )

    deadline = int(re.search(r"RETRY_DEADLINE_SECONDS:-(\d+)", block).group(1))
    assert deadline < timeout * 60, (
        f"the retry deadline ({deadline}s) is not inside the CodeBuild timeout "
        f"({timeout * 60}s), so the ladder can be cut off mid-sleep instead of "
        "reporting why it stopped"
    )


def _discovered_buildspecs() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return sorted(p for p in out if BUILDSPEC_NAME.search(p))


def test_every_tracked_buildspec_is_handed_to_the_gate():
    """Universe closure: the Makefile's discovery must reach every buildspec file
    in the tree. `patterns/*/buildspec.yml` matched one of four."""
    discovered = _discovered_buildspecs()
    assert discovered, "no buildspec files found; the gate would pass vacuously"

    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "BUILDSPEC_FILES = $(shell git ls-files" in makefile, (
        "validate-buildspec no longer derives its file set from git ls-files; a "
        "hardcoded glob is how three of four buildspecs went unread (#1310)"
    )

    result = subprocess.run(
        ["make", "validate-buildspec"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for path in discovered:
        assert path in result.stdout, (
            f"{path} is a tracked buildspec that `make validate-buildspec` did not read"
        )


def test_every_tracked_buildspec_actually_passes_the_new_check():
    """Non-vacuity in the other direction: the check is only worth having if the
    tree currently satisfies it, so a regression in any buildspec fails here with
    the file named rather than inside a make target's output."""
    failures = []
    for path in _discovered_buildspecs():
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append(f"{path}:\n{result.stdout}")
    assert not failures, "\n".join(failures)
