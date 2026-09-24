# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ``IDPPublisher``'s subprocess wrapper and component build steps.

This covers the part of ``idp_sdk._core.publish`` that actually invokes ``sam``:
the subprocess wrapper both build steps go through, the two-command
build-then-package sequence for one component directory, the type filter that
selects which components get built, the thread pool that builds them, and the two
small helpers that enumerate config files and map a source directory back to its
CloudFormation logical id.

What shaped these tests
-----------------------
``run_subprocess_with_logging`` is tested against **real** child processes
(``sys.executable -c ...``) rather than a patched ``subprocess``. That is what
makes the interesting assertions possible: that ``cwd`` is genuinely honoured
(the child reports its own working directory), that a non-zero exit is turned
into a ``(False, message)`` pair carrying the real stderr, and that the two
branches — captured and real-time — differ in how they handle a command that
cannot be executed at all. A patched ``subprocess.run`` would have accepted any
of those being wrong.

``build_and_package_template`` composes two ``sam`` command lines, and a wrong
argument there is not a crash — it is an artifact published to the wrong prefix or
a template whose ``ImageUri`` references nothing. Its subprocess call is
therefore replaced by a recorder, and the tests assert the **full argv list and
the working directory** of each of the two commands. The syntax-validation and
checksum-deletion paths use real files on disk, so the failure path is observed as
a deleted ``.checksum``, not as a call.

``_build_components_concurrently`` is driven with a real ``ThreadPoolExecutor``
and a thread-safe recorder, so the tests can assert which components were built
and that one component's failure or exception does not stop the others.

``_extract_function_name`` is fed real YAML templates on disk, including the
CloudFormation short-form intrinsics that plain ``yaml.safe_load`` rejects.
"""

from __future__ import annotations

import io
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from rich.console import Console

from idp_sdk._core.publish import IDPPublisher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_publisher(verbose: bool = False) -> tuple[IDPPublisher, io.StringIO]:
    pub = IDPPublisher(verbose=verbose)
    buf = io.StringIO()
    pub.console = Console(file=buf, width=300, no_color=True, highlight=False)
    return pub, buf


def write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def py(code: str) -> list[str]:
    """A runnable argv that executes ``code`` in this interpreter."""
    return [sys.executable, "-c", code]


def py_script(tmp_path: Path, name: str, code: str) -> list[str]:
    """An argv running ``code`` from a file, so the argv does not contain it.

    The wrapper matters for every assertion about what reached the console or the
    error message: ``run_subprocess_with_logging`` echoes and embeds the command
    line, so a ``-c`` inline program makes its own output text appear in the
    argv and an assertion on that text passes whether or not the child's output
    was read at all. Putting the program in a file removes that confound.

    The file *name* is chosen by the caller, which is also how the ``npm``
    keyword is put into the command line without putting it into the output.
    """
    script = tmp_path / name
    script.write_text(code)
    return [sys.executable, str(script)]


class CommandRecorder:
    """Stands in for ``run_subprocess_with_logging``, recording every call.

    Records the argv list, the component label and the working directory of each
    invocation so a test can assert on the composed command rather than on the
    fact that something was run. ``fail_on`` makes the Nth call (0-based) report
    failure, which is how the two ``sam`` steps are failed independently.
    """

    def __init__(self, fail_on: int | None = None):
        self.calls: list[dict] = []
        self.fail_on = fail_on

    def __call__(self, cmd, component_name, cwd=None, realtime=False):
        index = len(self.calls)
        self.calls.append(
            {
                "cmd": list(cmd),
                "component": component_name,
                "cwd": cwd,
                "realtime": realtime,
            }
        )
        if self.fail_on == index:
            return False, "simulated failure"
        return True, subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    @property
    def argvs(self) -> list[list[str]]:
        return [call["cmd"] for call in self.calls]


# ---------------------------------------------------------------------------
# run_subprocess_with_logging — captured mode
# ---------------------------------------------------------------------------


def test_a_successful_command_returns_its_completed_process_with_stdout(tmp_path):
    """The second element of the tuple is the caller's handle on the output.

    Several call sites parse ``result.stdout`` (the SAM version check is one), so
    returning ``True, None`` on success would break them silently.
    """
    pub, _ = make_publisher()
    ok, result = pub.run_subprocess_with_logging(
        py_script(tmp_path, "probe.py", "print('built 3 resources')"), "probe"
    )

    assert ok is True
    assert result.returncode == 0
    assert "built 3 resources" in result.stdout
    assert pub.build_errors == []


def test_the_working_directory_is_honoured(tmp_path):
    """``cwd`` decides which component ``sam build`` builds.

    Every pattern build passes the pattern directory here, and ``sam`` reads
    ``template.yaml`` relative to it. The child process reports its own working
    directory, so this asserts the real effect rather than a forwarded keyword.
    """
    workdir = tmp_path / "patterns" / "unified"
    workdir.mkdir(parents=True)

    pub, _ = make_publisher()
    ok, result = pub.run_subprocess_with_logging(
        py("import os; print(os.path.realpath(os.getcwd()))"), "probe", cwd=str(workdir)
    )

    assert ok is True
    assert result.stdout.strip() == str(workdir.resolve())


def test_a_failing_command_reports_the_return_code_stdout_stderr_and_cwd(tmp_path):
    """The error message is the only diagnostic a non-verbose run keeps.

    It has to carry enough to act on: the command, where it ran, the exit code and
    both streams. A build failure whose message omits stderr sends the reader to
    re-run the command by hand.
    """
    pub, _ = make_publisher()
    ok, message = pub.run_subprocess_with_logging(
        py_script(
            tmp_path,
            "failing_build.py",
            "import sys\n"
            "sys.stdout.write('partial output\\n')\n"
            "sys.stderr.write('Error: template not found\\n')\n"
            "sys.exit(3)\n",
        ),
        "SAM build for patterns/unified",
        cwd=str(tmp_path),
    )

    assert ok is False
    assert "Return code: 3" in message
    # Neither of these strings is in the argv, so they can only have come from
    # the child's own streams.
    assert "partial output" in message
    assert "Error: template not found" in message
    assert str(tmp_path) in message
    assert "STDOUT:" in message and "STDERR:" in message


def test_a_failure_is_recorded_for_the_error_summary():
    """The failure must reach ``build_errors`` under the component's own name.

    ``print_error_summary`` is what the operator reads at the end of a failed
    publish; a failure that returns ``False`` without recording itself leaves that
    summary empty while the run exits non-zero.
    """
    pub, _ = make_publisher()
    ok, message = pub.run_subprocess_with_logging(
        py("import sys; sys.exit(1)"), "SAM package for nested/bedrockkb"
    )

    assert ok is False
    assert len(pub.build_errors) == 1
    assert pub.build_errors[0]["component"] == "SAM package for nested/bedrockkb"
    assert pub.build_errors[0]["error"] == message


def test_the_reported_working_directory_falls_back_to_the_process_cwd():
    """With no ``cwd``, the message names the directory the command really used."""
    import os

    pub, _ = make_publisher()
    _, message = pub.run_subprocess_with_logging(py("import sys; sys.exit(1)"), "probe")

    assert f"Working directory: {os.getcwd()}" in message


def test_an_unexecutable_command_raises_in_captured_mode(tmp_path):
    """DEFECT, pinned as current behaviour: ``publish.py:339``.

    The captured branch calls ``subprocess.run`` with no ``try``, so a command
    that cannot be executed at all — a missing binary, a directory in place of an
    executable — raises ``FileNotFoundError`` straight out of the method. The
    real-time branch wraps the equivalent call and returns ``(False,
    "Failed to execute command: ...")`` instead, so the two branches of the same
    method behave differently for the same input.

    The observable consequence is an unhandled traceback where every other
    failure in this class produces a recorded, summarised build error. The
    prerequisite check makes it unlikely for ``sam`` itself, but not for the other
    executables this wrapper is handed (``npm``, ``docker``, ``uv``), and
    ``build_and_package_template`` catches ``Exception`` broadly enough that the
    traceback surfaces as a mislabelled "Build failed" with no recorded error.
    """
    pub, _ = make_publisher()

    with pytest.raises(FileNotFoundError):
        pub.run_subprocess_with_logging([str(tmp_path / "no-such-executable")], "probe")

    assert pub.build_errors == []


# ---------------------------------------------------------------------------
# run_subprocess_with_logging — real-time mode
# ---------------------------------------------------------------------------


def test_realtime_mode_streams_and_reports_success(tmp_path):
    """Real-time mode returns no result object, by design.

    It is used for ``npm install``, whose output is streamed rather than
    captured, so the second tuple element is ``None`` on success and callers must
    not read it.
    """
    pub, buf = make_publisher()
    ok, result = pub.run_subprocess_with_logging(
        py_script(tmp_path, "quiet.py", "print('hello from the child')"),
        "ui build",
        realtime=True,
    )

    assert ok is True
    assert result is None
    # The command itself is echoed so the operator can see what is running.
    assert "Running:" in buf.getvalue()


def test_realtime_mode_classifies_npm_progress_warnings_and_errors(tmp_path):
    """The npm filter only engages when ``npm`` appears in the command.

    Three classes of line get three different styles, and the classification is
    by lower-cased substring — so ``npm WARN`` and ``npm warn`` both count. The
    child really prints these lines from a script file, and ``npm`` reaches the
    command line through the script's *name*, which is what lets the fourth
    assertion mean something: the unclassified line is absent from the console
    because it was filtered out, not merely because it was never in the argv.
    """
    pub, buf = make_publisher()
    ok, _ = pub.run_subprocess_with_logging(
        py_script(
            tmp_path,
            "npm_install_probe.py",
            "print('added 412 packages')\n"
            "print('WARN deprecated inflight@1.0.6')\n"
            "print('ERROR could not resolve dependency')\n"
            "print('an unremarkable line nobody classifies')\n",
        ),
        "ui build",
        realtime=True,
    )
    out = buf.getvalue()

    assert ok is True
    assert "added 412 packages" in out
    assert "WARN deprecated inflight@1.0.6" in out
    assert "ERROR could not resolve dependency" in out
    # A line matching none of the keyword sets is not echoed.
    assert "an unremarkable line nobody classifies" not in out


def test_realtime_output_is_not_echoed_for_a_non_npm_command(tmp_path):
    """Only npm commands get their output mirrored to the console.

    Everything else is collected silently and surfaces only if the command fails,
    which is what keeps a successful build's log readable. The same program as the
    test above, under a name that does not contain ``npm``, must produce no echoed
    output at all.
    """
    pub, buf = make_publisher()
    pub.run_subprocess_with_logging(
        py_script(tmp_path, "docker_probe.py", "print('added 412 packages')\n"),
        "docker build",
        realtime=True,
    )

    assert "added 412 packages" not in buf.getvalue()


def test_a_realtime_failure_returns_the_collected_output(tmp_path):
    """Output is only captured for the error path, so it must all be there.

    The failure message is assembled from the lines read during streaming; a
    reader that dropped lines would produce a build error missing the one line
    that explains the failure.
    """
    pub, _ = make_publisher()
    ok, message = pub.run_subprocess_with_logging(
        py_script(
            tmp_path,
            "two_steps.py",
            "import sys\nprint('step one ok')\nprint('step two failed')\nsys.exit(2)\n",
        ),
        "ui build",
        realtime=True,
    )

    assert ok is False
    assert "Return code: 2" in message
    assert "step one ok" in message
    assert "step two failed" in message
    assert pub.build_errors[0]["component"] == "ui build"


def test_realtime_mode_merges_stderr_into_the_collected_output(tmp_path):
    """``stderr=STDOUT`` is what makes a failing npm run diagnosable.

    Were stderr left unredirected it would go to the real terminal and be absent
    from the recorded build error.
    """
    pub, _ = make_publisher()
    ok, message = pub.run_subprocess_with_logging(
        py_script(
            tmp_path,
            "stderr_only.py",
            "import sys\nsys.stderr.write('fatal: ENOSPC\\n')\nsys.exit(1)\n",
        ),
        "ui build",
        realtime=True,
    )

    assert ok is False
    assert "fatal: ENOSPC" in message


def test_an_unexecutable_command_is_caught_in_realtime_mode(tmp_path):
    """The real-time branch does handle an unexecutable command.

    Contrast with the captured branch, where the same input raises; that
    asymmetry is pinned above.
    """
    pub, _ = make_publisher()
    ok, message = pub.run_subprocess_with_logging(
        [str(tmp_path / "no-such-executable")], "ui build", realtime=True
    )

    assert ok is False
    assert "Failed to execute command" in message
    assert pub.build_errors[0]["component"] == "ui build"


# ---------------------------------------------------------------------------
# log_error_details
# ---------------------------------------------------------------------------


def test_error_details_are_appended_in_order_with_component_and_text():
    """The list order is the order the summary prints, so it is asserted."""
    pub, _ = make_publisher()
    pub.log_error_details("first", "boom one")
    pub.log_error_details("second", "boom two")

    assert pub.build_errors == [
        {"component": "first", "error": "boom one"},
        {"component": "second", "error": "boom two"},
    ]


def test_error_details_are_withheld_unless_verbose():
    """Non-verbose mode must still say *what* failed and how to see more.

    A failure line with neither the component name nor the ``--verbose`` hint
    leaves the operator with nothing to act on.
    """
    quiet, quiet_buf = make_publisher(verbose=False)
    quiet.log_error_details("SAM build for patterns/unified", "a very long traceback")
    quiet_out = quiet_buf.getvalue()

    assert "SAM build for patterns/unified build failed" in quiet_out
    assert "use --verbose for details" in quiet_out
    assert "a very long traceback" not in quiet_out

    loud, loud_buf = make_publisher(verbose=True)
    loud.log_error_details("SAM build for patterns/unified", "a very long traceback")
    assert "a very long traceback" in loud_buf.getvalue()


# ---------------------------------------------------------------------------
# build_and_package_template
# ---------------------------------------------------------------------------


def _prepare_build_dir(tmp_path: Path, name: str = "patterns/unified") -> Path:
    """A component directory with a template and valid Python, plus a checksum."""
    directory = tmp_path / name
    write(directory / "template.yaml", "Resources: {}\n")
    write(
        directory / "src" / "handler.py",
        "def handler(event, context):\n    return {}\n",
    )
    write(directory / ".checksum", "a-previous-checksum")
    return directory


def _configure(pub: IDPPublisher) -> None:
    pub.bucket = "idp-artifacts-us-west-2"
    pub.prefix_and_version = "idp/0.6.8"
    pub.region = "us-west-2"
    pub.account_id = "111122223333"


def test_the_build_and_package_commands_are_composed_exactly(tmp_path, monkeypatch):
    """Both ``sam`` command lines in full, plus their working directories.

    Four of these arguments decide where the artifacts end up and are not
    recoverable from a later step: ``--s3-bucket``, ``--s3-prefix``, the built
    template that ``sam package`` reads, and the packaged template it writes.
    ``sam build`` must run *in* the component directory while ``sam package``
    must run from the project root with absolute-ish paths, because the paths it
    is given already include the directory.
    """
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")
    recorder = CommandRecorder()

    pub, _ = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)

    assert pub.build_and_package_template(str(directory)) is True

    build_call, package_call = recorder.calls

    assert build_call["cmd"] == ["sam", "build", "--template-file", "template.yaml"]
    assert build_call["cwd"] == str(directory)

    assert package_call["cmd"] == [
        "sam",
        "package",
        "--template-file",
        str(directory / ".aws-sam" / "build" / "template.yaml"),
        "--output-template-file",
        str(directory / ".aws-sam" / "packaged.yaml"),
        "--s3-bucket",
        "idp-artifacts-us-west-2",
        "--s3-prefix",
        "idp/0.6.8",
    ]
    # sam package is given full paths, so it must not be run from the component.
    assert package_call["cwd"] is None


def test_the_component_labels_name_the_directory_for_the_error_summary(
    tmp_path, monkeypatch
):
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")
    recorder = CommandRecorder()

    pub, _ = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)
    pub.build_and_package_template(str(directory))

    assert recorder.calls[0]["component"] == f"SAM build for {directory}"
    assert recorder.calls[1]["component"] == f"SAM package for {directory}"


@pytest.mark.parametrize(
    "component", ["patterns/unified", "nested/multi-doc-discovery"]
)
def test_container_components_get_an_image_repository(tmp_path, monkeypatch, component):
    """Only these two build container images, and only they need the flag.

    ``sam package`` uses ``--image-repository`` to write the ``ImageUri`` values
    into the packaged template even under ``SkipBuild: True``. Omitting it for a
    container component yields a template whose functions reference no image;
    passing it for a zip component is a spurious ECR reference.

    The test runs with the component directory as the *relative* path the source
    compares against, because the comparison is a literal membership test on the
    directory string.
    """
    _prepare_build_dir(tmp_path, component)
    recorder = CommandRecorder()

    pub, _ = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)
    monkeypatch.chdir(tmp_path)

    pub.build_and_package_template(component)

    package_cmd = recorder.argvs[1]
    assert "--image-repository" in package_cmd
    assert package_cmd[package_cmd.index("--image-repository") + 1] == (
        "111122223333.dkr.ecr.us-west-2.amazonaws.com/placeholder"
    )


def test_a_zip_component_gets_no_image_repository(tmp_path, monkeypatch):
    _prepare_build_dir(tmp_path, "nested/bedrockkb")
    recorder = CommandRecorder()

    pub, _ = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)
    monkeypatch.chdir(tmp_path)

    pub.build_and_package_template("nested/bedrockkb")

    assert "--image-repository" not in recorder.argvs[1]


def test_the_placeholder_ecr_host_is_hardcoded_to_the_commercial_suffix(
    tmp_path, monkeypatch
):
    """Pinned: the ECR host is built with a literal ``amazonaws.com``.

    ``publish.py:823`` composes
    ``{account}.dkr.ecr.{region}.amazonaws.com/placeholder`` rather than using the
    region's own DNS suffix. GovCloud ECR endpoints do use ``amazonaws.com``, so
    the ``--govcloud`` transform this repository supports is unaffected; the
    partition where this is wrong is China (``amazonaws.com.cn``), which the
    solution does not target. Recorded rather than reported as a defect, so that
    a future China-partition requirement finds the one line to change.
    """
    _prepare_build_dir(tmp_path, "patterns/unified")
    recorder = CommandRecorder()

    pub, _ = make_publisher()
    _configure(pub)
    pub.region = "us-gov-west-1"
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)
    monkeypatch.chdir(tmp_path)

    pub.build_and_package_template("patterns/unified")

    package_cmd = recorder.argvs[1]
    assert package_cmd[package_cmd.index("--image-repository") + 1] == (
        "111122223333.dkr.ecr.us-gov-west-1.amazonaws.com/placeholder"
    )


def test_verbose_mode_adds_debug_to_both_sam_commands(tmp_path, monkeypatch):
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")
    recorder = CommandRecorder()

    pub, _ = make_publisher(verbose=True)
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)
    pub.build_and_package_template(str(directory))

    assert recorder.argvs[0][-1] == "--debug"
    assert "--debug" in recorder.argvs[1]


def test_a_container_flag_is_appended_to_the_build_command(tmp_path, monkeypatch):
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")
    recorder = CommandRecorder()

    pub, _ = make_publisher()
    _configure(pub)
    pub.use_container_flag = "--use-container"
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)
    pub.build_and_package_template(str(directory))

    assert recorder.argvs[0] == [
        "sam",
        "build",
        "--template-file",
        "template.yaml",
        "--use-container",
    ]


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_container_flag_is_not_appended(tmp_path, monkeypatch, blank):
    """An empty or whitespace-only flag must not become an argv entry.

    ``sam build ""`` is not the same command as ``sam build``; SAM reads the empty
    string as a positional resource name and builds nothing, which would look
    like a successful no-op build.
    """
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")
    recorder = CommandRecorder()

    pub, _ = make_publisher()
    _configure(pub)
    pub.use_container_flag = blank
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)
    pub.build_and_package_template(str(directory))

    assert recorder.argvs[0] == ["sam", "build", "--template-file", "template.yaml"]


def test_a_python_syntax_error_aborts_before_sam_runs_and_clears_the_checksum(
    tmp_path, monkeypatch
):
    """The real ``py_compile`` gate, observed through a real deleted checksum.

    Two things must happen together: ``sam`` must not be invoked at all (building
    a syntactically invalid function wastes minutes and fails confusingly later),
    and the component's ``.checksum`` must be removed so the *next* run rebuilds
    rather than reporting the broken component up to date. The second is the one
    that would be a lasting bug — a retained checksum after a failure means the
    failure is never retried.
    """
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")
    write(directory / "src" / "broken.py", "def handler(:\n    pass\n")
    recorder = CommandRecorder()

    pub, buf = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)

    with pytest.raises(SystemExit) as exc:
        pub.build_and_package_template(str(directory))

    assert exc.value.code == 1
    assert recorder.calls == []
    assert not (directory / ".checksum").exists()
    out = buf.getvalue()
    assert "syntax error" in out
    assert "Python syntax validation failed" in out


def test_a_failed_sam_build_exits_and_clears_the_checksum(tmp_path, monkeypatch):
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")
    recorder = CommandRecorder(fail_on=0)

    pub, buf = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)

    with pytest.raises(SystemExit) as exc:
        pub.build_and_package_template(str(directory))

    assert exc.value.code == 1
    # The package step must not run after a failed build.
    assert len(recorder.calls) == 1
    assert not (directory / ".checksum").exists()
    assert "SAM build failed" in buf.getvalue()


def test_a_failed_sam_package_exits_and_clears_the_checksum(tmp_path, monkeypatch):
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")
    recorder = CommandRecorder(fail_on=1)

    pub, buf = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", recorder)

    with pytest.raises(SystemExit) as exc:
        pub.build_and_package_template(str(directory))

    assert exc.value.code == 1
    assert len(recorder.calls) == 2
    assert not (directory / ".checksum").exists()
    assert "SAM package failed" in buf.getvalue()


def test_a_successful_build_leaves_the_checksum_alone(tmp_path, monkeypatch):
    """Only the failure path deletes the checksum.

    Deleting it on success would make every component rebuild on every run,
    which is the opposite of what the cache is for.
    """
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")

    pub, _ = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", CommandRecorder())

    assert pub.build_and_package_template(str(directory)) is True
    assert (directory / ".checksum").read_text() == "a-previous-checksum"


def test_the_upload_destination_is_reported_on_success(tmp_path, monkeypatch):
    """The console line has to name the prefix artifacts actually went to.

    It is what an operator copies into a ``TemplateURL`` or an ``aws s3 ls``, so a
    line naming a different bucket or prefix than the command used is worse than
    no line.
    """
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")

    pub, buf = make_publisher()
    _configure(pub)
    monkeypatch.setattr(pub, "run_subprocess_with_logging", CommandRecorder())
    pub.build_and_package_template(str(directory))

    assert "s3://idp-artifacts-us-west-2/idp/0.6.8/" in buf.getvalue()


def test_the_force_rebuild_argument_is_accepted_and_changes_nothing(
    tmp_path, monkeypatch
):
    """Pinned: ``force_rebuild`` is declared but never read.

    ``_build_components_concurrently`` passes ``force_rebuild=True`` for every
    component it submits, and ``build_and_package_template`` ignores the value —
    the rebuild decision has already been made by the caller that filtered the
    component list. Recorded because the parameter reads like a switch, and a
    change that starts honouring it would alter behaviour for every existing
    caller.
    """
    directory = _prepare_build_dir(tmp_path, "nested/bedrockkb")

    forced = CommandRecorder()
    pub_forced, _ = make_publisher()
    _configure(pub_forced)
    monkeypatch.setattr(pub_forced, "run_subprocess_with_logging", forced)
    pub_forced.build_and_package_template(str(directory), force_rebuild=True)

    unforced = CommandRecorder()
    pub_unforced, _ = make_publisher()
    _configure(pub_unforced)
    monkeypatch.setattr(pub_unforced, "run_subprocess_with_logging", unforced)
    pub_unforced.build_and_package_template(str(directory), force_rebuild=False)

    assert forced.argvs == unforced.argvs


# ---------------------------------------------------------------------------
# build_components_with_smart_detection
# ---------------------------------------------------------------------------


def test_only_components_matching_the_type_are_built(monkeypatch):
    """The filter is what keeps the pattern and nested builds separate.

    They run as separate concurrent groups, so a component leaking into the wrong
    group is built twice — or, worse, counted in a group whose worker budget was
    sized for something else.
    """
    pub, _ = make_publisher()
    captured = {}

    def fake_concurrent(components, component_type, max_workers):
        captured["components"] = list(components)
        captured["type"] = component_type
        captured["workers"] = max_workers
        return True

    monkeypatch.setattr(pub, "_build_components_concurrently", fake_concurrent)

    needing_rebuild = [
        {"component": "patterns/unified"},
        {"component": "nested/bedrockkb"},
        {"component": "nested/api-resolvers"},
    ]
    assert (
        pub.build_components_with_smart_detection(needing_rebuild, "nested", 2) is True
    )

    assert captured == {
        "components": ["nested/bedrockkb", "nested/api-resolvers"],
        "type": "nested",
        "workers": 2,
    }


def test_nothing_to_build_reports_up_to_date_without_starting_a_pool(monkeypatch):
    """The early return is what makes a no-op publish fast.

    It must also return ``True``: returning a falsy value for "nothing to do"
    would fail the whole publish whenever the cache was warm.
    """
    pub, buf = make_publisher()

    def must_not_run(*args, **kwargs):  # pragma: no cover - asserted unreachable
        raise AssertionError("no pool should be started when nothing needs rebuilding")

    monkeypatch.setattr(pub, "_build_components_concurrently", must_not_run)

    result = pub.build_components_with_smart_detection(
        [{"component": "patterns/unified"}], "nested", 4
    )

    assert result is True
    assert "All nested are up to date" in buf.getvalue()


def test_an_empty_rebuild_list_is_also_up_to_date(monkeypatch):
    pub, buf = make_publisher()
    monkeypatch.setattr(
        pub,
        "_build_components_concurrently",
        lambda *a, **k: pytest.fail("should not be called"),
    )

    assert pub.build_components_with_smart_detection([], "patterns", 4) is True
    assert "All patterns are up to date" in buf.getvalue()


def test_the_type_filter_is_a_substring_match_not_a_path_segment_match(monkeypatch):
    """Pinned: the filter is ``component_type in item["component"]``.

    That is a bare substring test against the component path, so a component type
    matches any component whose path contains those characters anywhere — the
    directory ``config/patterns-archive`` is selected by type ``patterns``, and
    ``deeply/nested/thing`` by type ``nested``. Today's two component types
    (``patterns`` and ``nested``) happen to correspond to top-level directories so
    nothing is miscategorised, but the filter carries no such rule: a new
    component directory whose name merely contains one of those words joins that
    group and is built with it.
    """
    pub, _ = make_publisher()
    captured = {}
    monkeypatch.setattr(
        pub,
        "_build_components_concurrently",
        lambda components, component_type, max_workers: (
            captured.update(components=list(components)) or True
        ),
    )

    pub.build_components_with_smart_detection(
        [
            {"component": "patterns/unified"},
            {"component": "config/patterns-archive"},
            {"component": "nested/bedrockkb"},
        ],
        "patterns",
        1,
    )

    assert captured["components"] == ["patterns/unified", "config/patterns-archive"]


# ---------------------------------------------------------------------------
# _build_components_concurrently
# ---------------------------------------------------------------------------


class BuildRecorder:
    """A thread-safe stand-in for ``build_and_package_template``.

    Records each component and the ``force_rebuild`` value it was given, and can
    be told to fail or raise for specific components.
    """

    def __init__(self, fail: set[str] | None = None, raise_for: set[str] | None = None):
        self.lock = threading.Lock()
        self.calls: list[tuple[str, bool]] = []
        self.fail = fail or set()
        self.raise_for = raise_for or set()

    def __call__(self, component, force_rebuild=False):
        with self.lock:
            self.calls.append((component, force_rebuild))
        if component in self.raise_for:
            raise RuntimeError(f"docker daemon unreachable while building {component}")
        return component not in self.fail

    @property
    def components(self) -> set[str]:
        with self.lock:
            return {component for component, _ in self.calls}


def test_every_component_is_built_once_with_force_rebuild_set(monkeypatch):
    """The grouping assertion: all of them, each exactly once, all forced.

    The caller has already decided these components are stale, so each must be
    submitted once and with ``force_rebuild=True``. A duplicate submission doubles
    a multi-minute ``sam build``; a missing one ships a stale artifact.
    """
    recorder = BuildRecorder()
    pub, _ = make_publisher()
    monkeypatch.setattr(pub, "build_and_package_template", recorder)

    components = [
        "patterns/unified",
        "nested/bedrockkb",
        "nested/api-resolvers",
        "nested/multi-doc-discovery",
    ]
    assert pub._build_components_concurrently(components, "nested", 3) is True

    assert sorted(component for component, _ in recorder.calls) == sorted(components)
    assert all(forced is True for _, forced in recorder.calls)


def test_a_single_worker_still_builds_everything(monkeypatch):
    """``--max-workers 1`` is the documented sequential mode.

    A pool of one must not drop or deadlock on the remaining submissions.
    """
    recorder = BuildRecorder()
    pub, _ = make_publisher()
    monkeypatch.setattr(pub, "build_and_package_template", recorder)

    components = ["a", "b", "c"]
    assert pub._build_components_concurrently(components, "patterns", 1) is True
    assert [component for component, _ in recorder.calls] == components


def test_one_falsy_result_fails_the_group_without_stopping_the_others(monkeypatch):
    """A failure must be reported *and* the rest must still be attempted.

    Aborting the group on the first failure would leave the other components
    unbuilt but their checksums untouched, so a re-run would report them up to
    date and publish stale artifacts for components that were never built.
    """
    recorder = BuildRecorder(fail={"nested/bedrockkb"})
    pub, buf = make_publisher()
    monkeypatch.setattr(pub, "build_and_package_template", recorder)

    result = pub._build_components_concurrently(
        ["patterns/unified", "nested/bedrockkb", "nested/api-resolvers"], "nested", 3
    )

    assert result is False
    assert recorder.components == {
        "patterns/unified",
        "nested/bedrockkb",
        "nested/api-resolvers",
    }
    assert "Build failed!" in buf.getvalue()


def test_an_exception_in_one_build_is_recorded_and_does_not_stop_the_group(monkeypatch):
    """A raising build must become a recorded build error, not a lost thread.

    ``future.result()`` re-raises inside the coordinator, so without the handler
    the exception would escape ``_build_components_concurrently`` and the
    remaining futures' results would never be read. The recorded error is what
    ``print_error_summary`` shows, and it must name the component and carry the
    traceback.
    """
    recorder = BuildRecorder(raise_for={"patterns/unified"})
    pub, _ = make_publisher()
    monkeypatch.setattr(pub, "build_and_package_template", recorder)

    result = pub._build_components_concurrently(
        ["patterns/unified", "nested/bedrockkb"], "patterns", 2
    )

    assert result is False
    assert recorder.components == {"patterns/unified", "nested/bedrockkb"}

    (error,) = pub.build_errors
    assert error["component"] == "Patterns patterns/unified build exception"
    assert "docker daemon unreachable" in error["error"]
    assert "Traceback" in error["error"]


def test_several_failures_are_each_recorded(monkeypatch):
    recorder = BuildRecorder(raise_for={"a", "c"})
    pub, _ = make_publisher()
    monkeypatch.setattr(pub, "build_and_package_template", recorder)

    assert pub._build_components_concurrently(["a", "b", "c"], "nested", 2) is False
    assert {e["component"] for e in pub.build_errors} == {
        "Nested a build exception",
        "Nested c build exception",
    }


def test_the_progress_count_reaches_the_total(monkeypatch):
    """Completion lines are numbered ``n/total``.

    The count is incremented per completed future, so the last line must read
    ``3/3``; an off-by-one here is the only progress signal a concurrent build
    gives.
    """
    pub, buf = make_publisher()
    monkeypatch.setattr(pub, "build_and_package_template", BuildRecorder())

    pub._build_components_concurrently(["a", "b", "c"], "nested", 3)
    out = buf.getvalue()

    assert "Complete (1/3)" in out
    assert "Complete (2/3)" in out
    assert "Complete (3/3)" in out


def test_an_empty_component_list_succeeds_without_building(monkeypatch):
    pub, _ = make_publisher()
    recorder = BuildRecorder()
    monkeypatch.setattr(pub, "build_and_package_template", recorder)

    assert pub._build_components_concurrently([], "nested", 4) is True
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# generate_config_file_list
# ---------------------------------------------------------------------------


def test_the_config_file_list_is_relative_recursive_and_sorted(tmp_path, monkeypatch):
    """Paths are relative to ``config_library`` and sorted deterministically.

    The list drives an explicit copy, so an absolute or repo-relative path would
    place files one directory too deep, and a non-deterministic order would make
    the artifact differ between otherwise identical builds.
    """
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "config_library" / "pricing.yaml", "pricing")
    write(tmp_path / "config_library" / "unified" / "rvl-cdip" / "config.yaml", "a")
    write(
        tmp_path
        / "config_library"
        / "unified"
        / "bank-statement-sample"
        / "config.yaml",
        "b",
    )
    write(tmp_path / "config_library" / "model_config_limits.yaml", "limits")
    write(tmp_path / "outside_the_library.yaml", "must not appear")

    assert IDPPublisher().generate_config_file_list() == [
        "model_config_limits.yaml",
        "pricing.yaml",
        "unified/bank-statement-sample/config.yaml",
        "unified/rvl-cdip/config.yaml",
    ]


def test_a_missing_config_library_yields_an_empty_list(tmp_path, monkeypatch):
    """Pinned: absence is silent.

    ``os.walk`` on a missing directory yields nothing, so a publish run from a
    tree with no ``config_library`` produces an empty list and no warning. The
    consequence is a deployment with no default configuration presets, which
    surfaces as an empty configuration table rather than as a build error.
    """
    monkeypatch.chdir(tmp_path)
    assert IDPPublisher().generate_config_file_list() == []


def test_the_config_file_list_excludes_nothing(tmp_path, monkeypatch):
    """Pinned: there is no exclusion filter here.

    Unlike ``get_directory_checksum``, this walk has no exclusion set, so an
    editor backup, a ``.DS_Store`` or a stray ``__pycache__`` entry inside
    ``config_library`` is listed and copied alongside the real presets.
    """
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "config_library" / "config.yaml", "real")
    write(tmp_path / "config_library" / ".DS_Store", "junk")
    write(tmp_path / "config_library" / "__pycache__" / "x.pyc", "bytecode")
    write(tmp_path / "config_library" / "config.yaml.bak", "backup")

    assert IDPPublisher().generate_config_file_list() == [
        ".DS_Store",
        "__pycache__/x.pyc",
        "config.yaml",
        "config.yaml.bak",
    ]


# ---------------------------------------------------------------------------
# _extract_function_name
# ---------------------------------------------------------------------------


TEMPLATE_WITH_INTRINSICS = """
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Resources:
  OCRFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/ocr_function/
      Handler: index.handler
      Role: !GetAtt OCRFunctionRole.Arn
      Environment:
        Variables:
          BUCKET: !Ref WorkingBucket
  ClassificationFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: !Sub '${ProjectRoot}/src/classification_function'
      Handler: index.handler
  OCRFunctionRole:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument: {}
  WorkingBucket:
    Type: AWS::S3::Bucket
"""


def test_a_function_is_found_by_its_code_uri_directory(tmp_path):
    """The logical id is what later steps name the layer and log group after.

    Returning the wrong one, or ``None``, means a Lambda that never gets its
    layer attached.
    """
    template = write(tmp_path / "template.yaml", TEMPLATE_WITH_INTRINSICS)
    pub, _ = make_publisher()

    assert pub._extract_function_name("ocr_function", str(template)) == "OCRFunction"


def test_a_trailing_slash_on_the_code_uri_does_not_hide_the_match(tmp_path):
    """``CodeUri: src/ocr_function/`` and ``src/ocr_function`` must both match.

    Both spellings are valid SAM and both occur in this repository's templates; a
    missed trailing slash would silently skip the function.
    """
    template = write(
        tmp_path / "template.yaml",
        """
Resources:
  WithSlash:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/alpha/
  WithoutSlash:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/beta
""",
    )
    pub, _ = make_publisher()

    assert pub._extract_function_name("alpha", str(template)) == "WithSlash"
    assert pub._extract_function_name("beta", str(template)) == "WithoutSlash"


def test_an_intrinsic_code_uri_still_resolves_to_its_last_path_segment(tmp_path):
    """A ``!Sub`` ``CodeUri`` collapses to its raw scalar, keeping the directory.

    Plain ``yaml.safe_load`` rejects ``!Sub`` outright, so without the custom
    loader this function would resolve *no* names in a template that uses one
    anywhere. The source comment records that a hand-maintained tag list used to
    cause exactly that; this asserts the general case works.
    """
    template = write(tmp_path / "template.yaml", TEMPLATE_WITH_INTRINSICS)
    pub, _ = make_publisher()

    assert (
        pub._extract_function_name("classification_function", str(template))
        == "ClassificationFunction"
    )


def test_an_unknown_intrinsic_tag_elsewhere_does_not_break_resolution(tmp_path):
    """Any ``!Tag``, not just a known list, must be tolerated.

    The loader accepts arbitrary short-form tags, so an intrinsic nobody
    anticipated — in a property this function does not read — must not cost it
    every answer in the template.
    """
    template = write(
        tmp_path / "template.yaml",
        """
Resources:
  Weird:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/gamma
      Layers:
        - !SomethingNobodyAnticipated [a, b]
      Tags: !FutureTag {Key: Value}
""",
    )
    pub, _ = make_publisher()

    assert pub._extract_function_name("gamma", str(template)) == "Weird"


def test_a_bare_code_uri_with_no_slash_matches_the_whole_value(tmp_path):
    template = write(
        tmp_path / "template.yaml",
        """
Resources:
  Flat:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src
""",
    )
    assert make_publisher()[0]._extract_function_name("src", str(template)) == "Flat"


def test_non_function_resources_are_never_returned(tmp_path):
    """Only ``AWS::Serverless::Function`` counts.

    A state machine or a layer can also carry a ``CodeUri``/``ContentUri``
    pointing at the same directory, and returning one of those logical ids would
    attach a layer to a resource that cannot take one.
    """
    template = write(
        tmp_path / "template.yaml",
        """
Resources:
  NotAFunction:
    Type: AWS::Serverless::LayerVersion
    Properties:
      ContentUri: src/shared
      CodeUri: src/shared
  AlsoNotAFunction:
    Type: AWS::Lambda::Function
    Properties:
      CodeUri: src/shared
""",
    )
    pub, buf = make_publisher()

    assert pub._extract_function_name("shared", str(template)) is None
    assert "Could not extract function name for shared" in buf.getvalue()


def test_no_match_warns_and_returns_none_rather_than_exiting(tmp_path):
    """A missing mapping is a skip, not a fatal error.

    The source says so explicitly, and it matters: a directory with no matching
    function would otherwise abort a whole publish over a single unused source
    tree.
    """
    template = write(tmp_path / "template.yaml", TEMPLATE_WITH_INTRINSICS)
    pub, buf = make_publisher()

    assert pub._extract_function_name("no_such_directory", str(template)) is None
    out = buf.getvalue()
    assert "Could not extract function name for no_such_directory" in out
    assert "No CloudFormation function found" in out


def test_a_missing_template_warns_and_returns_none(tmp_path):
    pub, buf = make_publisher()
    assert pub._extract_function_name("ocr", str(tmp_path / "absent.yaml")) is None
    assert "Could not extract function name for ocr" in buf.getvalue()


def test_malformed_yaml_warns_and_returns_none(tmp_path):
    template = write(tmp_path / "template.yaml", "Resources: [unclosed\n  : : :\n")
    pub, buf = make_publisher()

    assert pub._extract_function_name("ocr", str(template)) is None
    assert "Could not extract function name for ocr" in buf.getvalue()


def test_a_template_that_parses_to_a_non_mapping_warns_and_returns_none(tmp_path):
    """An empty or list-valued document is rejected explicitly.

    ``yaml`` returns ``None`` for an empty file, and ``template.get`` on that
    would raise ``AttributeError``; the guard turns it into the same warning every
    other unreadable template produces.
    """
    pub, buf = make_publisher()

    empty = write(tmp_path / "empty.yaml", "")
    assert pub._extract_function_name("ocr", str(empty)) is None

    a_list = write(tmp_path / "list.yaml", "- one\n- two\n")
    assert pub._extract_function_name("ocr", str(a_list)) is None

    assert buf.getvalue().count("Could not extract function name for ocr") == 2


def test_a_template_with_no_resources_section_returns_none(tmp_path):
    template = write(tmp_path / "template.yaml", "Description: nothing here\n")
    assert make_publisher()[0]._extract_function_name("ocr", str(template)) is None


def test_a_null_resource_body_is_skipped_rather_than_raising(tmp_path):
    """A resource key with no body appears while a template is being edited.

    ``resource_config`` is ``None`` there, and the truthiness guard is what stops
    the ``.get`` below it raising. The valid resource after it must still be
    found, which is what shows the loop continued rather than aborting.
    """
    template = write(
        tmp_path / "template.yaml",
        """
Resources:
  HalfWrittenResource:
  Good:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/delta
""",
    )
    assert make_publisher()[0]._extract_function_name("delta", str(template)) == "Good"


def test_a_function_without_properties_or_code_uri_is_skipped(tmp_path):
    template = write(
        tmp_path / "template.yaml",
        """
Resources:
  NoProperties:
    Type: AWS::Serverless::Function
  NoCodeUri:
    Type: AWS::Serverless::Function
    Properties:
      Handler: index.handler
  Good:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/epsilon
""",
    )
    assert (
        make_publisher()[0]._extract_function_name("epsilon", str(template)) == "Good"
    )


def test_a_non_string_code_uri_is_skipped(tmp_path):
    """``CodeUri`` may be a ``{Bucket, Key}`` mapping for an already-packaged function.

    Those cannot be matched against a source directory, and the ``isinstance``
    guard is what stops ``.rstrip`` raising on a dict. The directory-form function
    after it must still be found.
    """
    template = write(
        tmp_path / "template.yaml",
        """
Resources:
  AlreadyPackaged:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri:
        Bucket: some-bucket
        Key: some/key.zip
  Good:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/zeta
""",
    )
    assert make_publisher()[0]._extract_function_name("zeta", str(template)) == "Good"


def test_two_functions_sharing_a_directory_name_resolve_to_the_first_in_the_document(
    tmp_path,
):
    """Pinned: an ambiguous directory name is resolved by document order.

    The match is on the **last path segment** of ``CodeUri``, so two functions in
    different parent directories that end in the same name are indistinguishable,
    and the loop returns whichever appears first in the template. Nothing warns.

    The consequence is a silent mis-mapping: a layer or log-group setting computed
    for one function is applied to the other. The tree does not currently contain
    such a pair, which is why this is a pin rather than a bug report — but the
    function offers no protection if one is added.
    """
    template = write(
        tmp_path / "template.yaml",
        """
Resources:
  FirstInDocument:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: nested/one/src/shared_handler
  SecondInDocument:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: nested/two/src/shared_handler
""",
    )
    pub, buf = make_publisher()

    assert (
        pub._extract_function_name("shared_handler", str(template)) == "FirstInDocument"
    )
    assert buf.getvalue() == ""
