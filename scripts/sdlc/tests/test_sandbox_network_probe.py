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
    statuses=None,
    home="/nonexistent-home-for-probe-test",
    calls=None,
    empty_dns=(),
):
    """Execute the session program with the network replaced.

    ``reachable`` names hosts whose DNS, TCP and HTTP calls succeed; every other
    call raises the error the live interpreter returned for a blocked target.
    ``bodies`` overrides the response body per URL prefix, ``statuses`` the
    success status per host, and ``http_errors`` makes a host answer with that
    HTTP error status. ``empty_dns`` hosts resolve to no address. Every network
    call is appended to ``calls`` so a test can assert what was sent.

    The fake behaves like the metadata service: the credentials listing answers
    401 unless the request carries the token the token endpoint issued.
    """
    bodies = bodies or {}
    http_errors = http_errors or {}
    statuses = statuses or {}
    calls = [] if calls is None else calls
    for name in list(os.environ):
        if name.startswith("AWS_"):
            monkeypatch.delenv(name)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HOME", str(home))

    def getaddrinfo(host, port, *a, **k):
        calls.append({"kind": "dns", "host": host, "port": port})
        if host in empty_dns:
            return []
        if host in reachable:
            return [(2, 1, 6, "", ("192.0.2.1", port))]
        raise socket.gaierror(-2, "Name or service not known")

    class _Sock:
        def close(self):
            pass

    def create_connection(addr, timeout=None):
        calls.append({"kind": "tcp", "host": addr[0], "port": addr[1]})
        if addr[0] in reachable:
            return _Sock()
        raise OSError(101, "Network is unreachable")

    class _Resp:
        def __init__(self, body, status):
            self._body = body
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def urlopen(req, timeout=None):
        if isinstance(req, str):
            req = urllib.request.Request(req)
        url = req.full_url
        host = url.split("/")[2]
        headers = {k.lower(): v for k, v in req.header_items()}
        calls.append(
            {
                "kind": "http",
                "host": host,
                "url": url,
                "method": req.get_method(),
                "data": req.data,
                "headers": headers,
            }
        )
        if host in http_errors:
            raise urllib.error.HTTPError(url, http_errors[host], "err", {}, None)  # type: ignore[arg-type]
        if host not in reachable:
            raise urllib.error.URLError(f"unreachable {url}")
        if "/meta-data/iam/security-credentials/" in url and (
            headers.get("x-aws-ec2-metadata-token") != "imds-token"
        ):
            raise urllib.error.HTTPError(url, 401, "err", {}, None)  # type: ignore[arg-type]
        if url.endswith("/latest/api/token"):
            return _Resp(bodies.get(url, b"imds-token"), 200)
        body = next((b for prefix, b in bodies.items() if url.startswith(prefix)), b"x")
        return _Resp(body, statuses.get(host, 200))

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
            bodies={"http://169.254.169.254/latest/api/token": b""},
        )
    )
    assert parsed["imds_reachable"]["ok"] is False


# --- credential sources reported present, by presence only ----------------------


def _botocore_credential_env_names():
    """Every environment variable botocore's credential chain reads.

    Static keys and container credentials come from their providers; web
    identity from its provider's mapping; profile and file selection from the
    session variables. ``AWS_ROLE_SESSION_NAME`` is left out: it names a
    session and grants nothing on its own.
    """
    import botocore.credentials as bc
    from botocore.configprovider import BOTOCORE_DEFAUT_SESSION_VARIABLES as session

    names = {bc.EnvProvider.ACCESS_KEY, bc.EnvProvider.SECRET_KEY}
    names |= set(bc.EnvProvider.TOKENS)
    names |= {bc.ContainerProvider.ENV_VAR, bc.ContainerProvider.ENV_VAR_FULL}
    names.add(bc.ContainerProvider.ENV_VAR_AUTH_TOKEN)
    web = bc.AssumeRoleWithWebIdentityProvider._CONFIG_TO_ENV_VAR
    names |= {web["web_identity_token_file"], web["role_arn"]}
    names |= set(session["profile"][1])
    names |= {session["credentials_file"][1], session["config_file"][1]}
    return sorted(names)


def test_credential_env_names_are_botocores_credential_chain():
    names = _botocore_credential_env_names()
    assert len(names) >= 10
    assert set(names) == set(snp.CREDENTIAL_ENV_NAMES)


@pytest.mark.parametrize("name", snp.CREDENTIAL_ENV_NAMES)
def test_each_credential_env_name_is_detected(monkeypatch, capsys, name):
    out = _run_program(monkeypatch, capsys, env={name: "VALUEEXAMPLE"})
    parsed = snp.parse_session_output(out)
    assert parsed["creds_env"]["ok"] is True
    assert name in parsed["creds_env"]["detail"]
    assert "VALUEEXAMPLE" not in out


@pytest.mark.parametrize("filename", ["credentials", "config"])
def test_a_credential_file_under_home_is_detected(
    monkeypatch, capsys, tmp_path, filename
):
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / filename).write_text("[default]\nSECRETEXAMPLE\n")
    out = _run_program(monkeypatch, capsys, home=tmp_path)
    parsed = snp.parse_session_output(out)
    assert parsed["creds_files"]["ok"] is True
    assert "SECRETEXAMPLE" not in out


@pytest.mark.parametrize("variable", ["AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"])
def test_a_credential_file_named_by_the_environment_is_detected(
    monkeypatch, capsys, tmp_path, variable
):
    f = tmp_path / "elsewhere"
    f.write_text("x")
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, env={variable: str(f)})
    )
    assert parsed["creds_files"]["ok"] is True


def test_a_metadata_role_is_detected_without_printing_its_name(monkeypatch, capsys):
    calls = []
    out = _run_program(
        monkeypatch,
        capsys,
        reachable={"169.254.169.254"},
        bodies={
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/": (
                b"ROLENAMEEXAMPLE\n"
            )
        },
        calls=calls,
    )
    parsed = snp.parse_session_output(out)
    assert parsed["creds_imds"]["ok"] is True
    assert "ROLENAMEEXAMPLE" not in out
    listing = [c for c in calls if "/security-credentials/" in c.get("url", "")]
    assert listing
    assert all(
        c["headers"].get("x-aws-ec2-metadata-token") == "imds-token" for c in listing
    )


def test_a_metadata_listing_with_no_role_reads_as_absent(monkeypatch, capsys):
    parsed = snp.parse_session_output(
        _run_program(
            monkeypatch,
            capsys,
            reachable={"169.254.169.254"},
            bodies={
                "http://169.254.169.254/latest/meta-data/iam/security-credentials/": b""
            },
        )
    )
    assert parsed["imds_reachable"]["ok"] is True
    assert parsed["creds_imds"]["ok"] is False


@pytest.mark.parametrize(
    "env, url",
    [
        (
            {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/creds"},
            "http://169.254.170.2/v2/creds",
        ),
        (
            {"AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:9/creds"},
            "http://127.0.0.1:9/creds",
        ),
    ],
)
def test_a_container_credential_document_is_detected(monkeypatch, capsys, env, url):
    doc = json.dumps({"AccessKeyId": "AKIAIOSFODNN7EXAMPLE"}).encode()
    host = url.split("/")[2]
    out = _run_program(
        monkeypatch, capsys, env=env, reachable={host}, bodies={url: doc}
    )
    parsed = snp.parse_session_output(out)
    assert parsed["creds_container_endpoint"]["ok"] is True
    assert "AKIAIOSFODNN7EXAMPLE" not in out


# --- what the program sends ------------------------------------------------------


def test_the_presigned_request_is_a_put_with_a_body(monkeypatch, capsys):
    calls = []
    host = "ab-sandbox-probe-x.s3.us-west-2.amazonaws.com"
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, reachable={host}, calls=calls)
    )
    assert parsed["s3_presigned_put"]["ok"] is True
    [put] = [c for c in calls if c.get("host") == host]
    assert put["method"] == "PUT"
    assert put["url"] == _PUT_URL
    assert put["data"] == snp.PROBE_BODY.encode()


def test_tcp_checks_use_port_443(monkeypatch, capsys):
    calls = []
    _run_program(monkeypatch, capsys, calls=calls)
    tcp = [c for c in calls if c["kind"] == "tcp"]
    assert {c["host"] for c in tcp} == {"1.1.1.1", "s3.us-east-1.amazonaws.com"}
    assert {c["port"] for c in tcp} == {443}


def test_a_non_2xx_put_answer_is_not_a_successful_put(monkeypatch, capsys):
    host = "ab-sandbox-probe-x.s3.us-west-2.amazonaws.com"
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, reachable={host}, statuses={host: 302})
    )
    assert parsed["s3_presigned_put"]["ok"] is False
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, http_errors={host: 403})
    )
    assert parsed["s3_presigned_put"]["ok"] is False
    assert "403" in parsed["s3_presigned_put"]["detail"]


def test_an_http_error_from_example_com_still_reads_as_reachable(monkeypatch, capsys):
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, http_errors={"example.com": 500})
    )
    assert parsed["https_example_com"]["ok"] is True


def test_a_name_resolving_to_no_address_is_not_ok(monkeypatch, capsys):
    parsed = snp.parse_session_output(
        _run_program(monkeypatch, capsys, empty_dns={"example.com"})
    )
    assert parsed["dns_example_com"]["ok"] is False


def test_other_region_is_never_the_region_itself():
    for region in ("us-east-1", "us-west-2", "eu-central-1"):
        assert snp.other_region(region) != region
    program = snp.build_session_program(_PUT_URL, "us-east-1")
    assert 'S3_OTHER_HOST = "s3.us-east-1.amazonaws.com"' not in program


def test_detail_text_is_bounded(monkeypatch, capsys):
    """A long error message is cut to 300 characters, not carried whole."""
    long_host = "h" * 1000 + ".example"
    _run_program(monkeypatch, capsys)

    def urlopen(req, timeout=None):
        raise urllib.error.URLError(long_host)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    exec(compile(snp.build_session_program(_PUT_URL, "us-west-2"), "<s>", "exec"), {})
    parsed = snp.parse_session_output(capsys.readouterr().out)
    assert parsed["https_example_com"]["detail"].startswith("URLError")
    assert len(parsed["https_example_com"]["detail"]) == 300


# --- reading the stream: chunking and the marker anchor --------------------------


def test_a_marker_line_split_across_stdout_chunks_is_reassembled():
    line = snp.RESULT_MARKER + json.dumps(_all_results())
    events = [
        {"result": {"structuredContent": {"stdout": line[:20]}}},
        {"result": {"structuredContent": {"stdout": line[20:] + "\n"}}},
    ]
    assert set(snp.parse_session_output(snp.collect_stdout(events))) == set(
        snp.CHECK_NAMES
    )


def test_a_marker_line_split_across_content_blocks_is_reassembled():
    line = snp.RESULT_MARKER + json.dumps(_all_results())
    events = [
        {
            "result": {
                "content": [
                    {"type": "text", "text": line[:20]},
                    {"type": "text", "text": line[20:]},
                ]
            }
        }
    ]
    assert set(snp.parse_session_output(snp.collect_stdout(events))) == set(
        snp.CHECK_NAMES
    )


def test_only_a_line_starting_with_the_marker_counts():
    data = _all_results()
    echoed = "print(" + snp.RESULT_MARKER + json.dumps(data) + ")"
    text = echoed + "\n" + snp.RESULT_MARKER + json.dumps(data)
    assert set(snp.parse_session_output(text)) == set(snp.CHECK_NAMES)


# --- the live driver, with S3 on moto and the interpreter replaced ---------------


class _FakeInterpreter:
    """Stands in for the SDK client: records calls, returns a canned stream.

    When ``deliver`` is true the fake performs the PUT the session would have
    made, through the same S3 API, so the arrival check has an object to find.
    """

    instances: list = []

    def __init__(self, region, session=None, **kw):
        self.region = region
        self.session = session
        self.started_with = None
        self.invocations = []
        self.stopped = False
        _FakeInterpreter.instances.append(self)

    def start(self, identifier=None, **kw):
        self.started_with = identifier

    def invoke(self, method, params=None):
        self.invocations.append((method, params))
        code = params["code"]
        put_url = json.loads(code.split("PUT_URL = ", 1)[1].split("\n", 1)[0])
        self.put_url = put_url
        if _FakeInterpreter.deliver:
            bucket = put_url.split("//", 1)[1].split(".", 1)[0]
            self.session.client("s3", region_name=self.region).put_object(
                Bucket=bucket, Key=snp.PROBE_KEY, Body=b"x"
            )
        results = _results_matching(snp.EXPECTED_POSTURE)
        out = snp.RESULT_MARKER + json.dumps(results)
        return {"stream": [{"result": {"structuredContent": {"stdout": out}}}]}

    def stop(self):
        self.stopped = True


@pytest.fixture
def live(monkeypatch):
    import boto3
    import moto
    from bedrock_agentcore.tools import code_interpreter_client

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"
    )
    monkeypatch.setattr(code_interpreter_client, "CodeInterpreter", _FakeInterpreter)
    _FakeInterpreter.instances = []
    _FakeInterpreter.deliver = True
    with moto.mock_aws():
        yield boto3.client("s3", region_name="us-west-2")


def _probe_buckets(s3):
    return [
        b["Name"]
        for b in s3.list_buckets().get("Buckets", [])
        if b["Name"].startswith(snp.BUCKET_PREFIX)
    ]


def test_run_live_reports_arrival_and_tears_down(live):
    results, arrived = snp._run_live("us-west-2")
    assert arrived is True
    assert set(results) == set(snp.CHECK_NAMES)
    [ci] = _FakeInterpreter.instances
    assert ci.started_with == snp.INTERPRETER_ID
    assert ci.stopped is True
    [(method, params)] = ci.invocations
    assert method == "executeCode"
    assert params["language"] == "python"
    assert params["clearContext"] is True
    assert f"X-Amz-Expires={snp.PRESIGN_EXPIRY_SECONDS}" in ci.put_url
    assert snp.PRESIGN_EXPIRY_SECONDS == 900
    assert _probe_buckets(live) == []


def test_run_live_reports_a_missing_object(live):
    _FakeInterpreter.deliver = False
    _, arrived = snp._run_live("us-west-2")
    assert arrived is False
    assert _probe_buckets(live) == []


def test_run_live_tears_down_when_the_session_output_is_unreadable(live, monkeypatch):
    monkeypatch.setattr(
        _FakeInterpreter,
        "invoke",
        lambda self, m, p=None: {"stream": [{"result": {"content": []}}]},
    )
    with pytest.raises(ValueError):
        snp._run_live("us-west-2")
    assert _FakeInterpreter.instances[0].stopped is True
    assert _probe_buckets(live) == []


def test_a_failed_bucket_deletion_is_raised():
    class _S3:
        def list_objects_v2(self, Bucket):
            return {}

        def delete_bucket(self, Bucket):
            raise RuntimeError("denied")

    with pytest.raises(RuntimeError):
        snp._delete_bucket(_S3(), "ab-sandbox-probe-x")


# --- exit codes -------------------------------------------------------------------


def _stub_live(monkeypatch, results=None, arrived=None, error=None):
    def fake(region):
        if error:
            raise error
        return results, arrived

    monkeypatch.setattr(snp, "_run_live", fake)


def test_main_exits_0_when_the_posture_matches(monkeypatch, capsys):
    exp = snp.EXPECTED_POSTURE
    _stub_live(monkeypatch, _results_matching(exp), exp["object_arrived"])
    assert snp.main([]) == 0


def test_main_exits_1_when_the_posture_differs(monkeypatch, capsys):
    exp = snp.EXPECTED_POSTURE
    _stub_live(monkeypatch, _results_matching(exp), not exp["object_arrived"])
    assert snp.main([]) == 1


def test_main_exits_2_when_the_probe_cannot_run(monkeypatch, capsys):
    _stub_live(monkeypatch, error=ValueError("no result line"))
    assert snp.main([]) == 2
    assert "could not run" in capsys.readouterr().out


def test_main_writes_the_observations(monkeypatch, capsys, tmp_path):
    exp = snp.EXPECTED_POSTURE
    _stub_live(monkeypatch, _results_matching(exp), exp["object_arrived"])
    out = tmp_path / "o.json"
    snp.main(["--json-out", str(out)])
    data = json.loads(out.read_text())
    assert data["interpreter"] == snp.INTERPRETER_ID
    assert data["object_arrived"] is exp["object_arrived"]
