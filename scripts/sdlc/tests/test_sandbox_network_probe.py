"""Offline tests for scripts/sdlc/sandbox_network_probe.py.

The live part (starting an interpreter session, the bucket) is exercised only by
``make stacktest-sandbox-network``. These tests cover everything that decides the
verdict: the program the session runs, how its output is read back, and how the
observations are compared with the recorded posture.

The session program is executed here, in-process, with the network functions it
calls replaced, so the names it reports and the shape it prints are measured
rather than assumed.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import socket
import sys
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.unit

_SDLC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SDLC not in sys.path:
    sys.path.insert(0, _SDLC)

import sandbox_network_probe as snp  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(_SDLC))
_TOOL = os.path.join(
    _REPO,
    "lib/idp_common_pkg/idp_common/agents/analytics/tools/code_interpreter_tools.py",
)
_PUT_URL = (
    "https://ab-sandbox-probe-x.s3.us-west-2.amazonaws.com/probe/k"
    "?X-Amz-Signature=SIGNATUREVALUE&X-Amz-Security-Token=TOKENVALUE"
)


# --- the interpreter the probe starts is the one the tool starts ---------------


def test_probe_starts_the_sdk_default_interpreter():
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    default = inspect.signature(CodeInterpreter.start).parameters["identifier"].default
    assert default == snp.INTERPRETER_ID


def test_the_analytics_tool_relies_on_the_sdk_default_interpreter():
    """The tool calls ``start()`` with no identifier, so it gets the SDK default.

    If the tool ever names an interpreter of its own, this probe would be
    measuring a different one, and this test says so.
    """
    tree = ast.parse(open(_TOOL, encoding="utf-8").read())
    starts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "start"
    ]
    assert starts, "the tool no longer calls start() on the interpreter client"
    for call in starts:
        assert not call.args and not call.keywords


# --- reading the executeCode stream --------------------------------------------


def _model_result_members():
    import botocore.session

    model = botocore.session.get_session().get_service_model("bedrock-agentcore")
    stream = model.operation_model("InvokeCodeInterpreter").output_shape.members[
        "stream"
    ]
    result = stream.members["result"]
    return result, stream


def test_stream_field_names_come_from_the_service_model():
    result, stream = _model_result_members()
    assert "result" in stream.members
    assert {"content", "structuredContent"} <= set(result.members)
    assert "stdout" in result.members["structuredContent"].members
    assert {"type", "text"} <= set(result.members["content"].member.members)


def test_collect_stdout_prefers_structured_stdout():
    events = [
        {
            "result": {
                "content": [{"type": "text", "text": "content-text"}],
                "structuredContent": {"stdout": "structured-out"},
            }
        }
    ]
    assert snp.collect_stdout(events) == "structured-out"


def test_collect_stdout_falls_back_to_content_text():
    events = [{"result": {"content": [{"type": "text", "text": "only-text"}]}}]
    assert snp.collect_stdout(events) == "only-text"


def test_collect_stdout_ignores_non_text_content():
    events = [
        {
            "result": {
                "content": [
                    {"type": "image", "text": "not-this"},
                    {"type": "text", "text": "this"},
                ]
            }
        }
    ]
    assert snp.collect_stdout(events) == "this"


def test_collect_stdout_refuses_an_exception_event():
    _, stream = _model_result_members()
    exception_events = sorted(set(stream.members) - {"result"})
    assert exception_events
    for name in exception_events:
        with pytest.raises(ValueError, match=name):
            snp.collect_stdout([{name: {"message": "x"}}])


# --- parsing the result line ----------------------------------------------------


def _all_results(ok: bool = False) -> dict:
    return {n: {"ok": ok, "detail": "d"} for n in snp.CHECK_NAMES}


def _line(data) -> str:
    return "noise\n" + snp.RESULT_MARKER + json.dumps(data) + "\nmore noise"


def test_parse_reads_the_marker_line():
    data = _all_results()
    data["s3_presigned_put"]["ok"] = True
    parsed = snp.parse_session_output(_line(data))
    assert parsed["s3_presigned_put"]["ok"] is True
    assert parsed["dns_example_com"]["ok"] is False


@pytest.mark.parametrize("count", [0, 2])
def test_parse_refuses_other_than_one_marker_line(count):
    text = "\n".join(
        snp.RESULT_MARKER + json.dumps(_all_results()) for _ in range(count)
    )
    with pytest.raises(ValueError, match="one result line"):
        snp.parse_session_output(text)


def test_parse_refuses_a_missing_check():
    data = _all_results()
    del data["s3_presigned_put"]
    with pytest.raises(ValueError, match="s3_presigned_put"):
        snp.parse_session_output(_line(data))


def test_parse_refuses_an_unknown_check():
    data = _all_results()
    data["surprise"] = {"ok": True}
    with pytest.raises(ValueError, match="surprise"):
        snp.parse_session_output(_line(data))


@pytest.mark.parametrize("bad", [{"ok": "yes"}, {"ok": 1}, {"detail": "x"}, True])
def test_parse_refuses_a_non_boolean_ok(bad):
    data = _all_results()
    data["creds_env"] = bad
    with pytest.raises(ValueError, match="creds_env"):
        snp.parse_session_output(_line(data))


def test_parse_refuses_a_non_object():
    with pytest.raises(ValueError, match="not a JSON object"):
        snp.parse_session_output(snp.RESULT_MARKER + "[]")


# --- the verdict ----------------------------------------------------------------


def _results_matching(expected: dict) -> dict:
    return {n: {"ok": expected[n], "detail": ""} for n in snp.CHECK_NAMES}


def test_recorded_posture_covers_every_check_and_the_arrival_row():
    """Universe closure: every reported row has a recorded value and vice versa.

    A row with no recorded value, or a recorded value no row reports, is
    unaccounted for and fails here rather than at the live run.
    """
    rows = set(snp.CHECK_NAMES) | {"object_arrived"}
    unaccounted = rows ^ set(snp.EXPECTED_POSTURE)
    assert not unaccounted


def test_matching_posture_passes():
    exp = snp.EXPECTED_POSTURE
    rows = snp.evaluate(_results_matching(exp), exp["object_arrived"])
    assert [r.check for r in rows] == [*snp.CHECK_NAMES, "object_arrived"]
    assert snp.exit_code(rows) == 0


@pytest.mark.parametrize("name", snp.CHECK_NAMES)
def test_any_check_flipping_fails(name):
    exp = snp.EXPECTED_POSTURE
    results = _results_matching(exp)
    results[name]["ok"] = not exp[name]
    rows = snp.evaluate(results, exp["object_arrived"])
    failed = [r.check for r in rows if not r.passed]
    assert failed == [name]
    assert snp.exit_code(rows) == 1


def test_arrival_flipping_fails():
    exp = snp.EXPECTED_POSTURE
    rows = snp.evaluate(_results_matching(exp), not exp["object_arrived"])
    assert [r.check for r in rows if not r.passed] == ["object_arrived"]
    assert snp.exit_code(rows) == 1


def test_no_rows_is_not_a_pass():
    assert snp.exit_code([]) == 1


def test_evaluate_uses_the_expected_mapping_it_is_given():
    exp = {n: True for n in [*snp.CHECK_NAMES, "object_arrived"]}
    rows = snp.evaluate(_results_matching(exp), True, expected=exp)
    assert snp.exit_code(rows) == 0


def test_table_marks_pass_and_fail_per_row():
    exp = snp.EXPECTED_POSTURE
    results = _results_matching(exp)
    results["tcp_1_1_1_1_443"]["ok"] = not exp["tcp_1_1_1_1_443"]
    table = snp.render_table(snp.evaluate(results, exp["object_arrived"]))
    lines = {ln.split()[0]: ln for ln in table.splitlines()[2:]}
    assert lines["tcp_1_1_1_1_443"].split()[3] == "FAIL"
    assert lines["dns_example_com"].split()[3] == "PASS"
    assert len(lines) == len(snp.CHECK_NAMES) + 1


# --- the session program, executed offline --------------------------------------


def _run_program(
    monkeypatch,
    capsys,
    region="us-west-2",
    reachable=(),
    env=None,
    bodies=None,
    http_errors=None,
):
    """Execute the session program with the network replaced.

    ``reachable`` names hosts whose DNS, TCP and HTTP calls succeed; every other
    call raises the error the live interpreter returned for a blocked target.
    ``bodies`` overrides the response body per host, and ``http_errors`` makes a
    host answer with that HTTP error status.
    """
    bodies = bodies or {}
    http_errors = http_errors or {}
    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HOME", "/nonexistent-home-for-probe-test")

    def getaddrinfo(host, port, *a, **k):
        if host in reachable:
            return [(2, 1, 6, "", ("192.0.2.1", port))]
        raise socket.gaierror(-2, "Name or service not known")

    class _Sock:
        def close(self):
            pass

    def create_connection(addr, timeout=None):
        if addr[0] in reachable:
            return _Sock()
        raise OSError(101, "Network is unreachable")

    class _Resp:
        status = 200

        def __init__(self, body=b"x"):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        host = url.split("/")[2]
        if host in http_errors:
            raise urllib.error.HTTPError(url, http_errors[host], "err", {}, None)
        if host in reachable:
            return _Resp(bodies.get(host, b"x"))
        raise urllib.error.URLError(f"unreachable {url}")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    exec(compile(snp.build_session_program(_PUT_URL, region), "<s>", "exec"), {})
    return capsys.readouterr().out


def test_program_has_no_unfilled_placeholder():
    program = snp.build_session_program(_PUT_URL, "us-west-2")
    assert "__" + "PUT_URL" not in program
    for token in ("__S3_HOST__", "__S3_OTHER_HOST__", "__STS_HOST__", "__PUT_BODY__"):
        assert token not in program
    assert "__MARKER__" not in program
    ast.parse(program)


def test_program_reports_exactly_the_checked_names(monkeypatch, capsys):
    out = _run_program(monkeypatch, capsys)
    parsed = snp.parse_session_output(out)
    assert set(parsed) == set(snp.CHECK_NAMES)


def test_program_reports_blocked_network_as_not_ok(monkeypatch, capsys):
    parsed = snp.parse_session_output(_run_program(monkeypatch, capsys))
    for name in (
        "dns_example_com",
        "dns_s3_endpoint",
        "https_example_com",
        "s3_presigned_put",
        "tcp_1_1_1_1_443",
        "tcp_s3_other_region",
        "https_sts_regional",
        "imds_reachable",
    ):
        assert parsed[name]["ok"] is False, name


def test_program_reports_reachable_targets_as_ok(monkeypatch, capsys):
    reachable = {
        "example.com",
        "s3.us-west-2.amazonaws.com",
        "ab-sandbox-probe-x.s3.us-west-2.amazonaws.com",
        "1.1.1.1",
        "s3.us-east-1.amazonaws.com",
        "sts.us-west-2.amazonaws.com",
        "169.254.169.254",
    }
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, reachable=reachable)
    )
    for name in (
        "dns_example_com",
        "dns_s3_endpoint",
        "https_example_com",
        "s3_presigned_put",
        "tcp_1_1_1_1_443",
        "tcp_s3_other_region",
        "https_sts_regional",
        "imds_reachable",
    ):
        assert parsed[name]["ok"] is True, name


def test_program_targets_follow_the_region(monkeypatch, capsys):
    reachable = {"s3.eu-central-1.amazonaws.com", "s3.us-east-1.amazonaws.com"}
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, region="eu-central-1", reachable=reachable)
    )
    assert parsed["dns_s3_endpoint"]["ok"] is True
    assert parsed["tcp_s3_other_region"]["ok"] is True
    assert snp.other_region("us-east-1") != "us-east-1"


def test_credentials_are_reported_by_presence_only(monkeypatch, capsys):
    out = _run_program(
        monkeypatch,
        capsys,
        env={
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SESSION_TOKEN": "SESSIONTOKENEXAMPLE",
        },
    )
    parsed = snp.parse_session_output(out)
    assert parsed["creds_env"]["ok"] is True
    assert "AWS_ACCESS_KEY_ID" in parsed["creds_env"]["detail"]
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "SESSIONTOKENEXAMPLE" not in out


def test_no_credentials_reads_as_absent(monkeypatch, capsys):
    parsed = snp.parse_session_output(_run_program(monkeypatch, capsys))
    for name in ("creds_env", "creds_files", "creds_imds", "creds_container_endpoint"):
        assert parsed[name]["ok"] is False, name


def test_error_text_never_carries_the_presigned_query(monkeypatch, capsys):
    out = _run_program(monkeypatch, capsys)
    assert "unreachable https://ab-sandbox-probe-x" in out
    assert "SIGNATUREVALUE" not in out
    assert "TOKENVALUE" not in out


def test_only_the_home_region_s3_reachable_reads_as_such(monkeypatch, capsys):
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, reachable={"s3.us-west-2.amazonaws.com"})
    )
    assert parsed["dns_s3_endpoint"]["ok"] is True
    assert parsed["tcp_s3_other_region"]["ok"] is False


def test_an_http_error_status_from_sts_still_reads_as_reachable(monkeypatch, capsys):
    """A server that answers at all is reachable, whatever status it returns."""
    parsed = snp.parse_session_output(
        _run_program(
            monkeypatch, capsys, http_errors={"sts.us-west-2.amazonaws.com": 403}
        )
    )
    assert parsed["https_sts_regional"]["ok"] is True
    assert "403" in parsed["https_sts_regional"]["detail"]


def test_an_empty_metadata_token_is_not_reachable(monkeypatch, capsys):
    parsed = snp.parse_session_output(
        _run_program(
            monkeypatch,
            capsys,
            reachable={"169.254.169.254"},
            bodies={"169.254.169.254": b""},
        )
    )
    assert parsed["imds_reachable"]["ok"] is False
