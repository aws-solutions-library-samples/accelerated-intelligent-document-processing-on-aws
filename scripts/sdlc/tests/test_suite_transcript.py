"""Unit tests for the suite transcript and the log source the analysis reads.

⚠️ **A build cannot read its own output back from CloudWatch reliably.**
CodeBuild batches stdout to CloudWatch and the suite's parallel steps interleave,
so a `get_log_events` walk performed while the build is still running can return
a prefix that is missing lines already written. Measured on one nightly run: the
failure analysis captured 1942 lines, stopped ~50 short of the three lines that
identified the failure, reported "root cause not fully determined", and
hypothesised a 3600s timeout that had not happened. Read again after the build,
the same stream held all of it.

So the suite tees its own output to a local file and the analysis prefers that.
These tests pin the two halves: the tee captures everything without changing
what the console sees, and `fetch_full_build_log` prefers the transcript while
still falling back to CloudWatch for the cases the transcript cannot cover (a
failure before the tee was installed, or a caller outside the harness).
"""

import io

import pytest

pytestmark = pytest.mark.unit


class _Recorder(io.StringIO):
    """A stand-in for the real stream, so write-through is observable."""

    def __init__(self):
        super().__init__()
        self.is_a_tty = False

    def isatty(self):
        return self.is_a_tty

    def fileno(self):
        return 99


class TestTee:
    def test_output_reaches_both_the_stream_and_the_transcript(self, cbd):
        console, sink = _Recorder(), io.StringIO()

        tee = cbd._Tee(console, sink)
        tee.write("hello\n")
        tee.flush()

        assert console.getvalue() == "hello\n"
        assert sink.getvalue() == "hello\n"

    def test_a_broken_transcript_does_not_cost_the_console(self, cbd):
        """The transcript is a diagnostic; the console log is the product.

        A full disk must not turn into missing build output, so a sink that
        raises is swallowed and the write-through still happens.
        """

        class _Broken(io.StringIO):
            def write(self, data):
                raise OSError("no space left on device")

        console = _Recorder()
        tee = cbd._Tee(console, _Broken())

        tee.write("still printed\n")
        tee.flush()

        assert console.getvalue() == "still printed\n"

    def test_stream_attributes_are_delegated(self, cbd):
        """`rich` and `subprocess` ask about these on whatever stdout is.

        A tee missing one surfaces as an AttributeError from inside an unrelated
        library, a long way from here.
        """
        console = _Recorder()
        console.is_a_tty = True
        tee = cbd._Tee(console, io.StringIO())

        assert tee.isatty() is True
        assert tee.fileno() == 99
        assert tee.writable() is True
        assert tee.encoding


class TestInstallSuiteTranscript:
    def test_everything_printed_afterwards_is_captured(self, cbd, tmp_path):
        path = tmp_path / "transcript.log"
        original_out, original_err = cbd.sys.stdout, cbd.sys.stderr
        try:
            assert cbd.install_suite_transcript(str(path)) == str(path)
            print("a line of suite output")
            print("something on stderr", file=cbd.sys.stderr)
            cbd.sys.stdout.flush()
            cbd.sys.stderr.flush()
            written = path.read_text()
        finally:
            cbd.sys.stdout, cbd.sys.stderr = original_out, original_err

        assert "a line of suite output" in written
        assert "something on stderr" in written

    def test_the_transcript_is_line_buffered(self, cbd, tmp_path):
        """The analysis reads this file from the process still writing it.

        A block-buffered transcript would be missing its most recent — and most
        relevant — lines at exactly the moment it is read, which is the failure
        this whole mechanism exists to prevent. So the line must be on disk
        with no explicit flush.
        """
        path = tmp_path / "transcript.log"
        original_out, original_err = cbd.sys.stdout, cbd.sys.stderr
        try:
            cbd.install_suite_transcript(str(path))
            print("written without an explicit flush")
            immediately = path.read_text()
        finally:
            cbd.sys.stdout, cbd.sys.stderr = original_out, original_err

        assert "written without an explicit flush" in immediately

    def test_an_unopenable_path_returns_none_and_leaves_stdout_alone(
        self, cbd, tmp_path
    ):
        """Losing the transcript must not lose the build."""
        original_out = cbd.sys.stdout
        try:
            result = cbd.install_suite_transcript(str(tmp_path / "no" / "such" / "dir"))
        finally:
            cbd.sys.stdout = original_out

        assert result is None
        assert cbd.sys.stdout is original_out


class TestFetchFullBuildLogPrefersTheTranscript:
    def test_the_transcript_is_used_when_present(self, monkeypatch, tmp_path):
        """And CloudWatch is not consulted at all — the point of the change."""
        import failure_agent

        path = tmp_path / "transcript.log"
        path.write_text("the decisive line\n")
        monkeypatch.setattr(failure_agent, "SUITE_TRANSCRIPT_PATH", str(path))
        monkeypatch.setenv("CODEBUILD_BUILD_ID", "app-sdlc:abc")

        def _must_not_be_called(*a, **k):
            raise AssertionError("CloudWatch was read despite a transcript existing")

        monkeypatch.setattr(failure_agent, "boto3", _must_not_be_called, raising=False)

        assert failure_agent.fetch_full_build_log() == "the decisive line\n"

    def test_an_absent_transcript_falls_back_to_cloudwatch(self, monkeypatch, tmp_path):
        """The fallback covers a failure before the tee was ever installed."""
        import failure_agent

        monkeypatch.setattr(
            failure_agent, "SUITE_TRANSCRIPT_PATH", str(tmp_path / "absent.log")
        )
        monkeypatch.delenv("CODEBUILD_BUILD_ID", raising=False)

        # With no build id the CloudWatch path returns "" — reaching that line
        # at all is what shows the fallback was taken rather than skipped.
        assert failure_agent.fetch_full_build_log() == ""

    def test_an_empty_transcript_is_not_preferred_over_cloudwatch(
        self, monkeypatch, tmp_path
    ):
        """A zero-byte file is the tee installed but nothing flushed yet.

        Treating it as the answer would hand the analysis an empty log and a
        confident "no evidence found".
        """
        import failure_agent

        path = tmp_path / "transcript.log"
        path.write_text("")
        monkeypatch.setattr(failure_agent, "SUITE_TRANSCRIPT_PATH", str(path))
        monkeypatch.delenv("CODEBUILD_BUILD_ID", raising=False)

        assert failure_agent.fetch_full_build_log() == ""

    def test_read_suite_transcript_tolerates_an_unreadable_file(self, tmp_path):
        import failure_agent

        assert failure_agent.read_suite_transcript(str(tmp_path / "nope.log")) == ""


def test_both_modules_agree_on_the_transcript_path(cbd):
    """The path is duplicated, so it has to be asserted equal somewhere.

    `failure_agent` keeps its own literal rather than importing the harness,
    because it is also driven from tests that never import that module. A silent
    divergence would send the analysis back to the CloudWatch race with nothing
    to show it had happened.
    """
    import failure_agent

    assert cbd.SUITE_TRANSCRIPT_PATH == failure_agent.SUITE_TRANSCRIPT_PATH
