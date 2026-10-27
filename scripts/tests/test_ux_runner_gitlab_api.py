# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/gitlab_api.py, the UX job's GitLab client.

Every method runs against an in-process ``http.server`` on port 0, so what is
asserted is the request GitLab would actually receive: the method, the path
(project-scoped or, for ``/user``, not), the ``PRIVATE-TOKEN`` header, the JSON
or multipart body, and pagination by ``X-Next-Page``. The error mapping is pinned
by status, because it decides whether a run reads as a skip (nothing broke) or a
failure (the tooling did). Note bodies are checked defused on the wire, since
GitLab executes a line starting with ``/`` in a note created through the API.

What ``http.server`` cannot be made to send is sent by a raw socket instead: a
status line that is not HTTP, too many or too long headers, a broken chunk, a
body shorter than its Content-Length, and a body trickled out past the timeout.
Each must leave the module as a skip or a failure, never as an ``http.client``
exception the orchestrator does not catch, and every answer is bounded in size.
The fake server can echo the token it was sent back into a body or a header, the
way a gateway or a debugging proxy that echoes a request does, so the claim that
no message quotes the token is checked against an answer that carries it.

The subclass couples to ``ai_mr_review.Gitlab`` by inheritance, so the parts of
that class it depends on are pinned too, and so is the review footer that
``section`` reads a note's own text from, by composing a real review note.
Every ``isinstance`` check goes through ``gitlab_api.review``:
``test_ai_mr_review.py`` re-executes that module under xdist, and a separately
imported copy can hold different exception classes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import json
import re
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import gitlab_api, section  # noqa: E402

review = gitlab_api.review
CREDENTIAL = "test-credential-value"
PROJECT_PATH = "/api/v4/projects/group%2Fproj"
HEAD = "0123456789abcdef" * 2 + "01234567"
OTHER_HEAD = "fedcba9876543210" * 2 + "fedcba98"
UPLOAD_URL = "/uploads/0123456789abcdef0123456789abcdef/ux-test-mr812-01234567.mp4"


class _Handler(BaseHTTPRequestHandler):
    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        server = self.server
        assert isinstance(server, _FakeGitlab)
        server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": self.rfile.read(length) if length else b"",
            }
        )
        status, headers, payload = server.routes.get(
            (self.command, self.path), (404, {}, b'{"message": "404 Not Found"}')
        )
        echoed = self.headers.get("PRIVATE-TOKEN", "")
        payload = payload.replace(b"{echo}", echoed.encode())
        self.send_response(status)
        headers.setdefault("Content-Type", "application/json")
        for name, value in headers.items():
            self.send_header(name, value.replace("{echo}", echoed))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def do_PUT(self) -> None:
        self._serve()

    def do_DELETE(self) -> None:
        self._serve()

    def log_message(self, format: str, *args: Any) -> None:
        return None


class _FakeGitlab(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes: dict[tuple[str, str], tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[dict[str, Any]] = []

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/api/v4"

    def answer(
        self,
        method: str,
        path: str,
        payload: object = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        raw: bytes | None = None,
    ) -> None:
        body = raw if raw is not None else json.dumps(payload).encode()
        self.routes[(method, path)] = (status, dict(headers or {}), body)


@pytest.fixture
def gitlab() -> Iterator[_FakeGitlab]:
    server = _FakeGitlab()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _forge(server: _FakeGitlab) -> gitlab_api.UxGitlab:
    return gitlab_api.UxGitlab(server.api_url, "group/proj", CREDENTIAL)


def _note(ident: int, author: int, body: str, **extra: Any) -> dict[str, Any]:
    return {"id": ident, "author": {"id": author}, "body": body, **extra}


JSON_HEAD = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"


def _read_request(connection: socket.socket) -> None:
    connection.settimeout(5)
    received = b""
    while b"\r\n\r\n" not in received:
        chunk = connection.recv(65536)
        if not chunk:
            return
        received += chunk


@contextlib.contextmanager
def _raw_server(
    script: Callable[[socket.socket, threading.Event], None],
) -> Iterator[str]:
    """A one-connection server that runs ``script`` on the request it accepts.

    Yields the API URL to point a client at. ``script`` is given an event that is
    set when the test is done with the server, so one that waits or trickles
    stops as soon as the client has its answer.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(10)
    done = threading.Event()

    def serve() -> None:
        with contextlib.suppress(OSError):
            connection, _ = listener.accept()
            with connection:
                _read_request(connection)
                script(connection, done)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/api/v4"
    finally:
        done.set()
        listener.close()
        thread.join(timeout=10)


def _sender(answer: bytes) -> Callable[[socket.socket, threading.Event], None]:
    def script(connection: socket.socket, done: threading.Event) -> None:
        connection.sendall(answer)

    return script


def _section_for(head: str) -> str:
    report = section.parse_report(
        '```json\n{"flows": [{"id": "change", "title": "t", "outcome": "works"}], '
        '"visible_change_found": false}\n```\n'
    )
    return section.compose_section(
        head_sha=head,
        persona="Admin",
        stack=section.StackFacts("idp-ux-nightly", "us-east-1"),
        report=report,
        video=None,
        video_note="_No user-visible change found, so no video is attached._",
        chapters=[],
        caveats=[],
        model="m",
        upload_id=None,
    )


@pytest.mark.unit
def test_the_coupling_to_ai_mr_review_is_what_the_subclass_relies_on() -> None:
    assert list(inspect.signature(review.Gitlab._request).parameters) == [
        "self",
        "method",
        "path",
        "body",
    ]
    assert hasattr(review, "_NO_REDIRECT_OPENER")
    forge = gitlab_api.UxGitlab(
        "https://gitlab.example/api/v4", "group/proj", CREDENTIAL
    )
    assert forge._encoded == "group%2Fproj"
    assert "upload_timeout" in {
        field.name for field in dataclasses.fields(gitlab_api.UxGitlab)
    }
    assert forge.upload_timeout == 600
    assert CREDENTIAL not in repr(forge)


@pytest.mark.unit
def test_current_user_asks_for_user_without_the_project_prefix(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer("GET", "/api/v4/user", {"id": 7, "username": "ux-bot"})
    assert _forge(gitlab).current_user()["id"] == 7
    (request,) = gitlab.requests
    assert request["path"] == "/api/v4/user"
    assert request["headers"]["private-token"] == CREDENTIAL


@pytest.mark.unit
def test_current_user_without_an_id_is_a_failure(gitlab: _FakeGitlab) -> None:
    gitlab.answer("GET", "/api/v4/user", {"username": "ux-bot"})
    with pytest.raises(review.Failure, match="user id"):
        _forge(gitlab).current_user()


@pytest.mark.unit
def test_notes_follows_x_next_page_and_asks_oldest_first(gitlab: _FakeGitlab) -> None:
    base = f"{PROJECT_PATH}/merge_requests/812/notes?sort=asc&order_by=created_at"
    gitlab.answer(
        "GET",
        f"{base}&per_page=100&page=1",
        [_note(1, 7, "a")],
        headers={"X-Next-Page": "2"},
    )
    gitlab.answer(
        "GET",
        f"{base}&per_page=100&page=2",
        [_note(2, 7, "b")],
        headers={"X-Next-Page": ""},
    )
    assert [note["id"] for note in _forge(gitlab).notes(812)] == [1, 2]
    assert [r["path"] for r in gitlab.requests] == [
        f"{base}&per_page=100&page=1",
        f"{base}&per_page=100&page=2",
    ]
    assert all(r["headers"]["private-token"] == CREDENTIAL for r in gitlab.requests)


@pytest.mark.unit
def test_a_next_page_that_does_not_advance_is_a_failure(gitlab: _FakeGitlab) -> None:
    base = f"{PROJECT_PATH}/merge_requests/812/notes?sort=asc&order_by=created_at"
    gitlab.answer(
        "GET", f"{base}&per_page=100&page=1", [], headers={"X-Next-Page": "1"}
    )
    with pytest.raises(review.Failure, match="X-Next-Page"):
        _forge(gitlab).notes(812)


@pytest.mark.unit
@pytest.mark.parametrize("following", ["\u00b2", "\u00b9\u00b2", "2.0", "1" * 10])
def test_a_next_page_that_is_not_a_plain_page_number_is_a_failure(
    gitlab: _FakeGitlab, following: str
) -> None:
    """``str.isdigit`` admits superscript and other scripts' digits, which ``int``
    then refuses, so the header is matched as ASCII digits of bounded length."""
    base = f"{PROJECT_PATH}/merge_requests/812/notes?sort=asc&order_by=created_at"
    gitlab.answer(
        "GET", f"{base}&per_page=100&page=1", [], headers={"X-Next-Page": following}
    )
    with pytest.raises(review.Failure, match="X-Next-Page"):
        _forge(gitlab).notes(812)


@pytest.mark.unit
def test_the_inherited_listings_the_job_never_calls_refuse_rather_than_assert() -> None:
    forge = gitlab_api.UxGitlab(
        "https://gitlab.example/api/v4", "group/proj", CREDENTIAL
    )
    with pytest.raises(review.Failure, match="does not list merge requests"):
        forge.open_merge_requests("develop")
    with pytest.raises(review.Failure, match="does not read reviewed heads"):
        forge.reviewed_shas(812)


@pytest.mark.unit
def test_the_clients_upload_url_check_quotes_no_token(gitlab: _FakeGitlab) -> None:
    """The refusal quotes GitLab's own answer, which a gateway can make echo the
    request, so the token is replaced before the quote is cut."""
    forge = _forge(gitlab)
    echoed = "https://sso.example/" + "x" * 40 + CREDENTIAL + "/x.mp4"
    with pytest.raises(review.Failure) as refused:
        forge.checked_upload_url({"url": echoed})
    message = str(refused.value)
    assert CREDENTIAL[:10] not in message, message
    assert gitlab_api.CREDENTIAL_SHOWN_AS in message
    assert forge.checked_upload_url({"url": UPLOAD_URL}) == UPLOAD_URL


@pytest.mark.unit
def test_note_get_and_put_use_the_note_path_and_put_defuses_the_body(
    gitlab: _FakeGitlab,
) -> None:
    path = f"{PROJECT_PATH}/merge_requests/812/notes/55"
    gitlab.answer("GET", path, _note(55, 7, "body"))
    gitlab.answer("PUT", path, _note(55, 7, "edited"))
    forge = _forge(gitlab)
    assert forge.note(812, 55)["body"] == "body"
    assert forge.update_note(812, 55, "/merge\n  /approve\nkeep /this")["id"] == 55
    put = gitlab.requests[-1]
    assert put["method"] == "PUT"
    assert put["headers"]["content-type"] == "application/json"
    assert json.loads(put["body"]) == {"body": "\\/merge\n  \\/approve\nkeep /this"}


@pytest.mark.unit
def test_create_note_and_the_inherited_post_note_both_defuse(
    gitlab: _FakeGitlab,
) -> None:
    path = f"{PROJECT_PATH}/merge_requests/812/notes"
    gitlab.answer("POST", path, _note(60, 7, "x"), status=201)
    forge = _forge(gitlab)
    assert forge.create_note(812, "/close")["id"] == 60
    forge.post_note(812, "/merge")
    bodies = [json.loads(r["body"])["body"] for r in gitlab.requests]
    assert bodies == ["\\/close", "\\/merge"]
    assert {r["path"] for r in gitlab.requests} == {path}


@pytest.mark.unit
def test_discussion_lookup_pages_and_the_reply_is_posted_in_it(
    gitlab: _FakeGitlab,
) -> None:
    base = f"{PROJECT_PATH}/merge_requests/812/discussions"
    gitlab.answer(
        "GET",
        f"{base}?per_page=100&page=1",
        [{"id": "aaa", "notes": [{"id": 1}]}],
        headers={"X-Next-Page": "2"},
    )
    gitlab.answer(
        "GET",
        f"{base}?per_page=100&page=2",
        [{"id": "d1sc", "notes": [{"id": 54}, {"id": 55}]}],
    )
    gitlab.answer("POST", f"{base}/d1sc/notes", _note(70, 7, "reply"), status=201)
    forge = _forge(gitlab)
    assert forge.discussion_id_for(812, 55) == "d1sc"
    assert forge.discussion_id_for(812, 99) is None
    assert forge.reply(812, "d1sc", "/merge it")["id"] == 70
    assert json.loads(gitlab.requests[-1]["body"]) == {"body": "\\/merge it"}


@pytest.mark.unit
def test_upload_posts_one_multipart_file_with_a_safe_name(
    gitlab: _FakeGitlab, tmp_path: Path
) -> None:
    content = b"\x00\x01fake mp4\r\n--not-a-boundary\r\n\xff"
    video = tmp_path / "video.mp4"
    video.write_bytes(content)
    gitlab.answer(
        "POST", f"{PROJECT_PATH}/uploads", {"id": 9, "url": UPLOAD_URL}, status=201
    )
    payload = _forge(gitlab).upload(video, filename='ux test"/../mr 812.mp4')
    assert payload["id"] == 9
    (request,) = gitlab.requests
    content_type = request["headers"]["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()
    parts = request["body"].split(b"--" + boundary)
    assert parts[0] == b""
    assert parts[2] == b"--\r\n"
    head, _, rest = parts[1].partition(b"\r\n\r\n")
    assert (
        b'Content-Disposition: form-data; name="file"; filename="ux-test--..-mr-812.mp4"'
        in head
    )
    assert b"Content-Type: video/mp4" in head
    assert rest == content + b"\r\n"


@pytest.mark.unit
def test_multipart_refuses_header_values_that_could_break_out() -> None:
    body, content_type = gitlab_api.multipart(
        "file", "a.mp4", b"x", "video/mp4", boundary="b0und"
    )
    assert content_type == "multipart/form-data; boundary=b0und"
    assert body == (
        b'--b0und\r\nContent-Disposition: form-data; name="file"; filename="a.mp4"'
        b"\r\nContent-Type: video/mp4\r\n\r\nx\r\n--b0und--\r\n"
    )
    for name in ('a".mp4', "a\r\n.mp4"):
        with pytest.raises(ValueError):
            gitlab_api.multipart("file", name, b"x", "video/mp4")


@pytest.mark.unit
def test_a_redirect_is_a_skip_naming_the_gateway(gitlab: _FakeGitlab) -> None:
    gitlab.answer(
        "GET",
        f"{PROJECT_PATH}/merge_requests/812/notes/1",
        raw=b"",
        status=302,
        headers={"Location": "https://sso.example/login"},
    )
    with pytest.raises(review.Skip, match="authentication gateway"):
        _forge(gitlab).note(812, 1)


@pytest.mark.unit
def test_401_and_403_are_skips_and_403_names_the_role(gitlab: _FakeGitlab) -> None:
    gitlab.answer("GET", "/api/v4/user", {"message": "401"}, status=401)
    with pytest.raises(review.Skip, match=r"rejected the token \(401\)"):
        _forge(gitlab).current_user()
    path = f"{PROJECT_PATH}/merge_requests/812/notes/1"
    gitlab.answer("PUT", path, {"message": "403 Forbidden"}, status=403)
    with pytest.raises(review.Skip, match="role") as refused:
        _forge(gitlab).update_note(812, 1, "x")
    assert "(403)" in str(refused.value)
    assert CREDENTIAL not in str(refused.value)


@pytest.mark.unit
def test_the_inherited_merge_request_goes_through_the_same_mapping(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer("GET", f"{PROJECT_PATH}/merge_requests/5", {}, status=403)
    with pytest.raises(review.Skip, match="role"):
        _forge(gitlab).merge_request(5)
    gitlab.answer(
        "GET",
        f"{PROJECT_PATH}/merge_requests/6",
        {"iid": 6, "title": "t", "draft": True, "sha": HEAD},
    )
    merge_request = _forge(gitlab).merge_request(6)
    assert merge_request.draft is True
    assert merge_request.head_sha == HEAD


@pytest.mark.unit
def test_other_errors_are_failures_with_bounded_detail(gitlab: _FakeGitlab) -> None:
    gitlab.answer(
        "GET", f"{PROJECT_PATH}/merge_requests/812/notes/1", raw=b"x" * 5000, status=404
    )
    with pytest.raises(review.Failure, match="404") as failed:
        _forge(gitlab).note(812, 1)
    assert len(str(failed.value)) < 600


@pytest.mark.unit
def test_204_with_empty_ok_returns_none(gitlab: _FakeGitlab) -> None:
    gitlab.answer("DELETE", f"{PROJECT_PATH}/thing", raw=b"", status=204)
    forge = _forge(gitlab)
    payload, headers = forge._send(
        "DELETE", forge._project_url("/thing"), empty_ok=True
    )
    assert payload is None
    assert isinstance(headers, dict)


@pytest.mark.unit
def test_html_or_broken_json_from_a_2xx_is_a_skip(gitlab: _FakeGitlab) -> None:
    gitlab.answer(
        "GET",
        "/api/v4/user",
        raw=b"<html>sign in</html>",
        headers={"Content-Type": "text/html"},
    )
    with pytest.raises(review.Skip, match="not JSON"):
        _forge(gitlab).current_user()
    gitlab.answer("GET", "/api/v4/user", raw=b"{broken")
    with pytest.raises(review.Skip, match="did not"):
        _forge(gitlab).current_user()


@pytest.mark.unit
def test_an_unreachable_host_is_a_skip_and_a_silent_one_a_failure() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    refused = gitlab_api.UxGitlab(
        f"http://127.0.0.1:{closed_port}/api/v4", "group/proj", CREDENTIAL
    )
    with pytest.raises(review.Skip, match="cannot reach"):
        refused.current_user()
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        forge = gitlab_api.UxGitlab(
            f"http://127.0.0.1:{silent.getsockname()[1]}/api/v4",
            "group/proj",
            CREDENTIAL,
        )
        with pytest.raises(review.Failure, match="did not answer GET /user within 1s"):
            forge._send("GET", f"{forge.api_url}/user", timeout=1)


@pytest.mark.unit
def test_review_note_for_wants_my_newest_note_for_the_full_head() -> None:
    marker = f"<!-- ai-review: sha={HEAD} rev=1 -->\nreview"
    notes = [
        _note(10, 7, marker),
        _note(11, 8, marker),
        _note(12, 7, f"<!-- ai-review: sha={HEAD[:8]} rev=1 -->"),
        _note(13, 7, f"<!-- ai-review: sha={OTHER_HEAD} rev=1 -->"),
        _note(14, 7, marker + " again"),
        _note(15, 7, marker, system=True),
    ]
    found = gitlab_api.review_note_for(notes, HEAD, 7)
    assert found is not None
    assert found["id"] == 14
    assert gitlab_api.review_note_for(notes, HEAD, 9) is None
    assert gitlab_api.review_note_for(notes[1:2], HEAD, 7) is None


@pytest.mark.unit
def test_my_notes_are_the_authors_own_newest_first() -> None:
    notes = [
        _note(3, 7, "c"),
        _note(1, 7, "a"),
        _note(2, 8, "b"),
        {"id": 4, "author": {"id": True}, "body": "d"},
        _note(5, 7, "e", system=True),
    ]
    assert [note["id"] for note in gitlab_api.my_notes(notes, 7)] == [3, 1]
    assert gitlab_api.my_notes(notes, 1) == []


@pytest.mark.unit
def test_safe_upload_url_accepts_only_the_upload_shape() -> None:
    assert gitlab_api.safe_upload_url({"url": UPLOAD_URL, "id": 9}) == UPLOAD_URL
    for bad in (
        "https://evil/x.mp4",
        "/uploads/../x",
        "/uploads/0123456789abcdef0123456789abcdef/..",
        "/uploads/0123456789abcdef/x.mp4/extra",
        UPLOAD_URL + "\n",
        "//evil.example/uploads/0123456789abcdef0123456789abcdef/x.mp4",
        None,
        42,
    ):
        with pytest.raises(review.Failure):
            gitlab_api.safe_upload_url({"url": bad})
    with pytest.raises(review.Failure):
        gitlab_api.safe_upload_url({})


@pytest.mark.unit
def test_gitlab_api_is_the_only_module_importing_ai_mr_review_here() -> None:
    """D17: one import site, so one set of Skip and Failure classes per process."""
    import ast

    importers = []
    for path in sorted((SCRIPTS / "ux_runner").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "ai_mr_review" for name in names):
                importers.append(path.name)
    assert importers == ["gitlab_api.py"]


@pytest.mark.unit
def test_a_dropped_connection_is_a_skip() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def accept_and_hang_up() -> None:
            connection, _ = listener.accept()
            connection.recv(65536)
            connection.close()

        hangup = threading.Thread(target=accept_and_hang_up, daemon=True)
        hangup.start()
        forge = gitlab_api.UxGitlab(
            f"http://127.0.0.1:{listener.getsockname()[1]}/api/v4",
            "group/proj",
            CREDENTIAL,
        )
        with pytest.raises(review.Skip, match="cannot reach"):
            forge.current_user()
        hangup.join(timeout=5)


@pytest.mark.unit
def test_a_listing_that_never_ends_is_a_failure(
    gitlab: _FakeGitlab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitlab_api, "MAX_PAGES", 2)
    base = f"{PROJECT_PATH}/merge_requests/812/discussions"
    for page in (1, 2):
        gitlab.answer(
            "GET",
            f"{base}?per_page=100&page={page}",
            [],
            headers={"X-Next-Page": str(page + 1)},
        )
    with pytest.raises(review.Failure, match="past 2 pages"):
        _forge(gitlab).discussion_id_for(812, 1)


@pytest.mark.unit
def test_no_message_quotes_the_token_when_an_answer_echoes_it(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer(
        "GET",
        "/api/v4/user",
        raw=b"x" * 185 + b"{echo} and the rest of the sign-in page",
        headers={"Content-Type": "text/html"},
    )
    notes = f"{PROJECT_PATH}/merge_requests/812/notes"
    gitlab.answer(
        "GET", f"{notes}/1", raw=b"<pre>PRIVATE-TOKEN: {echo}</pre>", status=500
    )
    gitlab.answer(
        "GET",
        f"{notes}/2",
        raw=b"",
        status=302,
        headers={"Location": "https://sso.example/login?echo={echo}"},
    )
    forge = _forge(gitlab)
    messages = []
    with pytest.raises(review.Skip, match="not JSON") as page:
        forge.current_user()
    messages.append(str(page.value))
    with pytest.raises(review.Failure, match="returned 500") as failed:
        forge.note(812, 1)
    messages.append(str(failed.value))
    with pytest.raises(review.Skip, match="authentication gateway") as redirected:
        forge.note(812, 2)
    messages.append(str(redirected.value))
    assert all(r["headers"]["private-token"] == CREDENTIAL for r in gitlab.requests)
    for message in messages:
        assert CREDENTIAL[:15] not in message, message
        assert gitlab_api.CREDENTIAL_SHOWN_AS in message, message


@pytest.mark.unit
@pytest.mark.parametrize(
    ("answer", "shown"),
    [
        (b"NOT HTTP\r\n\r\n", "is not well-formed HTTP (BadStatusLine)"),
        (
            b"HTTP/1.1 200 OK\r\n"
            + b"".join(b"X-%d: y\r\n" % n for n in range(101))
            + b"\r\n",
            "is not well-formed HTTP (HTTPException)",
        ),
        (
            b"HTTP/1.1 200 OK\r\nX-Long: " + b"y" * 70000 + b"\r\n\r\n",
            "is not well-formed HTTP (LineTooLong)",
        ),
        (
            JSON_HEAD + b"Transfer-Encoding: chunked\r\n\r\nzz\r\n",
            "broke off (IncompleteRead)",
        ),
        (
            JSON_HEAD + b'Content-Length: 100\r\n\r\n{"a"',
            "broke off after 4 of its 100 bytes",
        ),
    ],
)
def test_an_answer_that_is_not_whole_http_is_a_skip(answer: bytes, shown: str) -> None:
    with _raw_server(_sender(answer)) as api_url:
        forge = gitlab_api.UxGitlab(api_url, "group/proj", CREDENTIAL)
        with pytest.raises(review.Skip, match=re.escape(shown)):
            forge._send("GET", f"{api_url}/user", timeout=5)


@pytest.mark.unit
def test_json_nested_past_the_recursion_limit_is_a_skip(gitlab: _FakeGitlab) -> None:
    gitlab.answer("GET", "/api/v4/user", raw=b"[" * 100_000 + b"]" * 100_000)
    with pytest.raises(review.Skip, match="claimed to return JSON but did not"):
        _forge(gitlab).current_user()


@pytest.mark.unit
def test_an_answer_over_the_size_bound_is_a_failure_and_is_read_no_further(
    gitlab: _FakeGitlab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitlab_api, "MAX_ANSWER_BYTES", 1024)
    gitlab.answer("GET", "/api/v4/user", raw=b" " * 2048)
    with pytest.raises(review.Failure, match="is 2048 bytes, more than the 1024"):
        _forge(gitlab).current_user()
    unmeasured = JSON_HEAD + b"Connection: close\r\n\r\n" + b" " * 4096
    with _raw_server(_sender(unmeasured)) as api_url:
        forge = gitlab_api.UxGitlab(api_url, "group/proj", CREDENTIAL)
        with pytest.raises(review.Failure, match="runs past the 1024 bytes"):
            forge._send("GET", f"{api_url}/user", timeout=5)


@pytest.mark.unit
def test_an_answer_trickled_out_past_the_timeout_is_a_failure() -> None:
    def trickle(connection: socket.socket, done: threading.Event) -> None:
        connection.sendall(JSON_HEAD + b"Connection: close\r\n\r\n")
        for _ in range(150):
            if done.wait(0.1):
                return
            connection.sendall(b" ")

    with _raw_server(trickle) as api_url:
        forge = gitlab_api.UxGitlab(api_url, "group/proj", CREDENTIAL)
        with pytest.raises(review.Failure, match="within 1s"):
            forge._send("GET", f"{api_url}/user", timeout=1)


@pytest.mark.unit
def test_a_refusal_is_read_only_as_far_as_the_detail_it_shows() -> None:
    filler = b"x" * (gitlab_api.REFUSAL_BYTES + 100)

    def endless(connection: socket.socket, done: threading.Event) -> None:
        connection.sendall(
            b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n"
            b"Content-Length: 10000000\r\n\r\n" + filler
        )
        done.wait(20)

    with _raw_server(endless) as api_url:
        forge = gitlab_api.UxGitlab(api_url, "group/proj", CREDENTIAL)
        began = time.monotonic()
        with pytest.raises(review.Failure, match="returned 500") as failed:
            forge._send("GET", f"{api_url}/user", timeout=30)
        assert time.monotonic() - began < 10
    assert len(str(failed.value)) < 600


@pytest.mark.unit
def test_a_refusal_whose_body_breaks_off_is_still_reported_by_its_status() -> None:
    broken = (
        b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n\r\nzz\r\n"
    )
    with _raw_server(_sender(broken)) as api_url:
        forge = gitlab_api.UxGitlab(api_url, "group/proj", CREDENTIAL)
        with pytest.raises(review.Failure, match=r"returned 500 for GET /user: $"):
            forge._send("GET", f"{api_url}/user", timeout=5)


@pytest.mark.unit
def test_a_request_url_urllib_cannot_build_is_a_skip_naming_the_request(
    gitlab: _FakeGitlab,
) -> None:
    with pytest.raises(review.Skip, match="GET relative/path: the request URL"):
        _forge(gitlab)._send("GET", "relative/path")
    assert gitlab.requests == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "api_url",
    [
        "gitlab.example/api/v4",
        "ftp://gitlab.example/api/v4",
        "https:///api/v4",
        "https://gitlab.example:abc/api/v4",
        "https://gitlab.example:0/api/v4",
        "https://user:pw@gitlab.example/api/v4",
        "https://gitlab.example/api/v4?x=1",
        "https://gitlab.example/api/v4#here",
        "https://gitlab .example/api/v4",
        "https://gitläb.example/api/v4",
        "https://gitlab.example/api/v4\n",
    ],
)
def test_an_api_url_no_request_could_reach_is_refused_when_the_client_is_made(
    api_url: str,
) -> None:
    with pytest.raises(review.Skip, match="not an absolute http") as refused:
        gitlab_api.UxGitlab(api_url, "group/proj", CREDENTIAL)
    assert "pw@" not in str(refused.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "shown"),
    [
        ([], "other than an object"),
        ({"title": "t", "sha": HEAD}, "lacks a usable field (KeyError)"),
        ({"iid": "seven"}, "lacks a usable field (ValueError)"),
        ({"iid": 7, "author": "jdoe"}, "lacks a usable field (AttributeError)"),
        ({"iid": 7, "title": None, "sha": HEAD}, "a text field that is not text"),
    ],
)
def test_a_merge_request_answer_of_the_wrong_shape_is_a_failure(
    gitlab: _FakeGitlab, payload: object, shown: str
) -> None:
    gitlab.answer("GET", f"{PROJECT_PATH}/merge_requests/7", payload)
    with pytest.raises(review.Failure, match=re.escape(shown)):
        _forge(gitlab).merge_request(7)


@pytest.mark.unit
def test_a_token_that_cannot_be_a_header_fails_without_being_shown(
    gitlab: _FakeGitlab,
) -> None:
    broken = gitlab_api.UxGitlab(
        gitlab.api_url, "group/proj", CREDENTIAL + "\nX-Injected: 1"
    )
    with pytest.raises(review.Failure, match=r"could not be sent \(ValueError\)") as e:
        broken.current_user()
    assert CREDENTIAL not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert gitlab.requests == []


@pytest.mark.unit
def test_an_upload_whose_file_cannot_be_read_is_a_failure(
    gitlab: _FakeGitlab, tmp_path: Path
) -> None:
    with pytest.raises(review.Failure, match="could not be read"):
        _forge(gitlab).upload(tmp_path / "absent.mp4", filename="absent.mp4")
    assert gitlab.requests == []


@pytest.mark.unit
def test_review_note_for_reads_the_review_marker_only_as_the_first_line() -> None:
    quoting = (
        f"<!-- ai-review: sha={OTHER_HEAD} rev=1 -->\n"
        f"The diff adds <!-- ai-review: sha={HEAD} rev=1 --> to a page.\n"
    )
    notes = [
        _note(20, 7, quoting),
        _note(21, 7, f"A comment.\n<!-- ai-review: sha={HEAD} rev=1 -->"),
    ]
    assert gitlab_api.review_note_for(notes, HEAD, 7) is None
    found = gitlab_api.review_note_for(notes, OTHER_HEAD, 7)
    assert found is not None and found["id"] == 20


@pytest.mark.unit
def test_the_review_footer_section_reads_from_is_the_one_ai_mr_review_writes() -> None:
    merge_request = review.MergeRequest(
        iid=812,
        title="t",
        author="a",
        source_branch="s",
        target_branch="develop",
        web_url="",
        head_sha=HEAD,
        draft=False,
    )
    quoted = (
        f"It quotes <!-- ux-test: sha={OTHER_HEAD} rev=1 upload=none -->\n\n---\n"
        f"<!-- ux-test: sha={OTHER_HEAD} rev=1 upload=none -->\n"
    )
    body = review.compose_note(merge_request, quoted, (1, 2, 3), False, "m")
    assert body.rstrip("\n").splitlines()[-1].startswith(section.REVIEW_FOOTER_PREFIX)
    assert section.sections_in(body) == []
    appended = section.append_section(body, _section_for(HEAD))
    assert section.sections_in(appended) == [(HEAD, section.UX_PROMPT_REVISION)]
    note = _note(30, 7, appended)
    found = gitlab_api.review_note_for([note], HEAD, 7)
    assert found is not None and found["id"] == 30
    assert gitlab_api.review_note_for([note], OTHER_HEAD, 7) is None


# --- waiting for the code-review note ----------------------------------------------

ME = 7
STRANGER = 8
REVIEW_BODY = f"<!-- ai-review: sha={HEAD} rev=1 -->\n## PR/MR Review: !812\n"


class _WaitForge:
    """The three calls the wait makes, answered in turn, each one recorded.

    ``listings`` are the successive answers to ``notes``; the last one repeats.
    """

    def __init__(
        self, listings: list[list[dict[str, Any]]], *, head: str = HEAD
    ) -> None:
        self.listings = listings
        self.head = head
        self.calls: list[str] = []

    def merge_request(self, iid: int) -> Any:
        self.calls.append(f"merge_request {iid}")
        return review.MergeRequest(
            iid=iid,
            title="t",
            author="a",
            source_branch="s",
            target_branch="develop",
            web_url="",
            head_sha=self.head,
            draft=False,
        )

    def current_user(self) -> dict[str, Any]:
        self.calls.append("current_user")
        return {"id": ME, "username": "ux-review-bot"}

    def notes(self, iid: int) -> list[dict[str, Any]]:
        listed = sum(1 for call in self.calls if call.startswith("notes"))
        self.calls.append(f"notes {iid}")
        return self.listings[min(listed, len(self.listings) - 1)]


class _Clock:
    """Monotonic time that moves only when the wait sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _wait(
    forge: _WaitForge,
    clock: _Clock,
    *,
    poll_minutes: float = 10,
    deadline: float = 1e9,
    posted: Callable[[list[dict[str, Any]]], bool] = lambda mine: False,
    **extra: Any,
) -> gitlab_api.ReviewWait:
    return gitlab_api.wait_for_review_note(
        forge,
        812,
        HEAD,
        already_posted=posted,
        poll_minutes=poll_minutes,
        deadline=deadline,
        clock=clock.clock,
        sleep=clock.sleep,
        **extra,
    )


@pytest.mark.unit
def test_the_wait_reports_what_it_ended_on_in_five_frozen_fields() -> None:
    names = [f.name for f in dataclasses.fields(gitlab_api.ReviewWait)]
    assert names == ["outcome", "review_note", "mine", "me", "moved_to"]
    assert gitlab_api.POLL_SECONDS == 30.0
    default = inspect.signature(gitlab_api.wait_for_review_note).parameters
    assert default["poll_seconds"].default == gitlab_api.POLL_SECONDS


@pytest.mark.unit
def test_the_wait_finds_the_review_note_for_the_head_when_it_appears() -> None:
    """The MR, then the token's user, then a listing every 30 s until it is there."""
    review_note = _note(41, ME, REVIEW_BODY)
    forge = _WaitForge([[], [], [_note(40, STRANGER, "hi"), review_note]])
    clock = _Clock()
    waited = _wait(forge, clock)
    assert waited.outcome == "found"
    assert waited.review_note == review_note
    assert waited.mine == [review_note]
    assert (waited.me, waited.moved_to) == (ME, None)
    assert clock.sleeps == [30.0, 30.0]
    assert forge.calls == [
        "merge_request 812",
        "current_user",
        "notes 812",
        "notes 812",
        "notes 812",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("poll_minutes", "deadline", "sleeps"),
    [(10, 1e9, 20), (1, 1e9, 2), (10, 1095.0, 4), (0, 1e9, 0)],
    ids=["ten-minutes", "one-minute", "deadline-first", "no-wait"],
)
def test_the_wait_ends_at_the_earlier_of_its_minutes_and_the_deadline(
    poll_minutes: float, deadline: float, sleeps: int
) -> None:
    """Ten minutes of 30 s sleeps is twenty, and the run's deadline cuts it short."""
    forge = _WaitForge([[_note(50, STRANGER, REVIEW_BODY)]])
    clock = _Clock()
    waited = _wait(forge, clock, poll_minutes=poll_minutes, deadline=deadline)
    assert waited.outcome == "timeout"
    assert waited.review_note is None and waited.mine == []
    assert clock.sleeps == [30.0] * sleeps
    assert forge.calls.count("notes 812") == sleeps + 1


@pytest.mark.unit
def test_a_head_that_moved_returns_before_the_user_or_the_notes_are_read() -> None:
    forge = _WaitForge([[_note(41, ME, REVIEW_BODY)]], head=OTHER_HEAD)
    clock = _Clock()
    waited = _wait(forge, clock)
    assert waited == gitlab_api.ReviewWait("moved", None, [], None, OTHER_HEAD)
    assert forge.calls == ["merge_request 812"]
    assert clock.sleeps == []


@pytest.mark.unit
def test_a_merge_request_that_names_no_head_is_not_a_move() -> None:
    forge = _WaitForge([[_note(41, ME, REVIEW_BODY)]], head="")
    assert _wait(forge, _Clock()).outcome == "found"


@pytest.mark.unit
def test_my_own_post_for_the_head_ends_the_wait_before_the_review_is_looked_for() -> (
    None
):
    """``already_posted`` is asked of the token's own notes alone, newest first, on
    every listing, and is asked before the review note is: a section another run
    posted for this head stops this one even with the review note right there."""
    review_note = _note(41, ME, REVIEW_BODY)
    posted = _note(42, ME, "<!-- ux-test: posted -->")
    forge = _WaitForge([[_note(43, STRANGER, "x")], [review_note, posted]])
    asked: list[list[int]] = []

    def already(mine: list[dict[str, Any]]) -> bool:
        asked.append([note["id"] for note in mine])
        return any(note["id"] == 42 for note in mine)

    clock = _Clock()
    waited = _wait(forge, clock, posted=already)
    assert waited.outcome == "posted"
    assert waited.review_note is None
    assert [note["id"] for note in waited.mine] == [42, 41]
    assert asked == [[], [42, 41]]
    assert clock.sleeps == [30.0]


@pytest.mark.unit
def test_a_review_note_another_author_posted_is_never_the_one_found() -> None:
    forge = _WaitForge([[_note(41, STRANGER, REVIEW_BODY)]])
    waited = _wait(forge, _Clock(), poll_minutes=1)
    assert waited.outcome == "timeout"
    assert waited.mine == []


@pytest.mark.unit
def test_the_wait_sleeps_poll_seconds_between_listings() -> None:
    forge = _WaitForge([[]])
    clock = _Clock()
    _wait(forge, clock, poll_minutes=1, poll_seconds=20.0)
    assert clock.sleeps == [20.0, 20.0, 20.0]


@pytest.mark.unit
def test_the_wait_drives_the_real_client_over_the_wire(gitlab: _FakeGitlab) -> None:
    """The three requests the real client sends, in order, and nothing written."""
    notes = f"{PROJECT_PATH}/merge_requests/812/notes?sort=asc&order_by=created_at"
    gitlab.answer(
        "GET",
        f"{PROJECT_PATH}/merge_requests/812",
        {"iid": 812, "title": "t", "draft": False, "sha": HEAD},
    )
    gitlab.answer("GET", "/api/v4/user", {"id": ME, "username": "ux-review-bot"})
    gitlab.answer("GET", f"{notes}&per_page=100&page=1", [_note(41, ME, REVIEW_BODY)])
    clock = _Clock()
    waited = gitlab_api.wait_for_review_note(
        _forge(gitlab),
        812,
        HEAD,
        already_posted=lambda mine: False,
        poll_minutes=10,
        deadline=1e9,
        clock=clock.clock,
        sleep=clock.sleep,
    )
    assert waited.outcome == "found" and waited.review_note is not None
    assert waited.review_note["id"] == 41
    assert [(r["method"], r["path"]) for r in gitlab.requests] == [
        ("GET", f"{PROJECT_PATH}/merge_requests/812"),
        ("GET", "/api/v4/user"),
        ("GET", f"{notes}&per_page=100&page=1"),
    ]


# --- earlier pipelines: what the nightly digest compares with ---------------------

DIGEST_JOB = "ai_ux_test_stories_digest"
DIGEST_FILE = "ux-test-stories-digest/digest.json"
PIPELINES = (
    f"{PROJECT_PATH}/pipelines?ref=develop&source=schedule&order_by=id&sort=desc"
    "&per_page=20"
)


def _jobs(pipeline: int) -> str:
    return f"{PROJECT_PATH}/pipelines/{pipeline}/jobs?per_page=100&page=1"


def _artifact(job: int) -> str:
    return f"{PROJECT_PATH}/jobs/{job}/artifacts/{DIGEST_FILE}"


def _pipeline(ident: object, **extra: Any) -> dict[str, Any]:
    return {"id": ident, "ref": "develop", "source": "schedule", **extra}


def _job(ident: object, name: str = DIGEST_JOB, status: str = "success") -> object:
    return {"id": ident, "name": name, "status": status}


def _digests(server: _FakeGitlab, **overrides: Any) -> list[tuple[int, bytes | None]]:
    values: dict[str, Any] = {
        "ref": "develop",
        "job": DIGEST_JOB,
        "artifact": DIGEST_FILE,
        "max_bytes": 1024,
        "deadline": 1e9,
        **overrides,
    }
    return gitlab_api.scheduled_digests(_forge(server), **values)


@pytest.mark.unit
def test_the_history_reads_each_scheduled_nights_digest_by_job_id(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer(
        "GET", PIPELINES, [_pipeline(30), _pipeline(29), _pipeline(28), _pipeline(27)]
    )
    gitlab.answer(
        "GET",
        _jobs(29),
        [
            _job(2904, name="ai_ux_test_stories_nightly"),
            _job(2902),
            _job(2903, status="failed"),
            _job(2905, status="running"),
            _job(True),
        ],
    )
    gitlab.answer(
        "GET",
        _artifact(2903),
        headers={"Content-Type": "application/octet-stream"},
        raw=b'{"night": 29}',
    )
    gitlab.answer("GET", _jobs(28), [_job(2801, status="manual"), "not a job"])
    gitlab.answer("GET", _jobs(27), [_job(2701)])
    found = _digests(gitlab, own_pipeline=30)
    assert found == [(29, b'{"night": 29}'), (28, None), (27, None)]
    assert [(r["method"], r["path"]) for r in gitlab.requests] == [
        ("GET", PIPELINES),
        ("GET", _jobs(29)),
        ("GET", _artifact(2903)),
        ("GET", _jobs(28)),
        ("GET", _jobs(27)),
        ("GET", _artifact(2701)),
    ]
    assert all(r["headers"]["private-token"] == CREDENTIAL for r in gitlab.requests)


@pytest.mark.unit
def test_a_night_that_cannot_be_read_is_a_gap_and_the_history_goes_on(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer("GET", PIPELINES, [_pipeline(29), _pipeline(28), _pipeline(27)])
    gitlab.answer("GET", _jobs(29), {"message": "boom"}, status=500)
    gitlab.answer("GET", _jobs(28), [_job(2801)])
    gitlab.answer("GET", _artifact(2801), {"message": "too big"}, status=403)
    gitlab.answer("GET", _jobs(27), [_job(2701)])
    gitlab.answer("GET", _artifact(2701), raw=b"{}")
    assert _digests(gitlab) == [(29, None), (28, None), (27, b"{}")]


@pytest.mark.unit
def test_a_digest_past_its_bound_is_a_gap_and_is_read_no_further(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer("GET", PIPELINES, [_pipeline(29)])
    gitlab.answer("GET", _jobs(29), [_job(2901)])
    gitlab.answer("GET", _artifact(2901), raw=b" " * 1025)
    assert _digests(gitlab) == [(29, None)]
    with pytest.raises(review.Failure, match="is 1025 bytes, more than the 1024"):
        _forge(gitlab).job_artifact(2901, DIGEST_FILE, limit=1024)
    unmeasured = JSON_HEAD + b"Connection: close\r\n\r\n" + b" " * 4096
    with _raw_server(_sender(unmeasured)) as api_url:
        forge = gitlab_api.UxGitlab(api_url, "group/proj", CREDENTIAL)
        with pytest.raises(review.Failure, match="runs past the 1024 bytes"):
            forge.job_artifact(2901, DIGEST_FILE, limit=1024)


@pytest.mark.unit
def test_nights_not_reached_by_the_deadline_are_gaps_and_are_not_asked_for(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer("GET", PIPELINES, [_pipeline(29), _pipeline(28), _pipeline(27)])
    gitlab.answer("GET", _jobs(29), [_job(2901)])
    gitlab.answer("GET", _artifact(2901), raw=b"{}")
    times = iter([0.0, 10.0, 11.0])
    found = _digests(gitlab, deadline=10.0, clock=lambda: next(times))
    assert found == [(29, b"{}"), (28, None), (27, None)]
    assert not [r for r in gitlab.requests if r["path"] in (_jobs(28), _jobs(27))]


@pytest.mark.unit
def test_a_listing_that_cannot_be_read_raises_as_any_request_does(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer("GET", PIPELINES, {"message": "403 Forbidden"}, status=403)
    with pytest.raises(review.Skip, match="403"):
        _digests(gitlab)
    gitlab.answer("GET", PIPELINES, {"pipelines": []})
    with pytest.raises(review.Failure, match="no list"):
        _digests(gitlab)


@pytest.mark.unit
def test_the_listing_returns_only_this_refs_scheduled_pipelines(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer(
        "GET",
        PIPELINES,
        [
            _pipeline(33, ref="feature/x"),
            _pipeline(32, source="push"),
            _pipeline(True),
            _pipeline(0),
            _pipeline("31"),
            "not a pipeline",
            {"id": 29},
            _pipeline(29),
            _pipeline(30),
        ],
    )
    assert _forge(gitlab).scheduled_pipelines("develop") == [30, 29]


@pytest.mark.unit
def test_the_listing_is_one_page_of_at_most_a_hundred(gitlab: _FakeGitlab) -> None:
    listing = PIPELINES.replace("per_page=20", "per_page=2")
    gitlab.answer("GET", listing, [_pipeline(n) for n in (30, 29, 28)])
    assert _forge(gitlab).scheduled_pipelines("develop", limit=2) == [30, 29]
    for limit in (0, 101):
        with pytest.raises(ValueError, match="1 to 100"):
            _forge(gitlab).scheduled_pipelines("develop", limit=limit)
    assert [r["path"] for r in gitlab.requests] == [listing]


@pytest.mark.unit
@pytest.mark.parametrize(
    "kind", ["application/json", "application/octet-stream", "text/html"]
)
def test_an_artifact_is_returned_as_stored_whatever_type_gitlab_names(
    gitlab: _FakeGitlab, kind: str
) -> None:
    gitlab.answer("GET", _artifact(7), headers={"Content-Type": kind}, raw=b"[not json")
    assert _forge(gitlab).job_artifact(7, DIGEST_FILE, limit=64) == b"[not json"
    gitlab.answer("GET", _artifact(8), raw=b"")
    assert _forge(gitlab).job_artifact(8, DIGEST_FILE, limit=64) == b""


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    ["", "../digest.json", "/etc/passwd", "a//b", "a/./b", "a/../b", "a b", "x" * 129],
)
def test_an_artifact_path_is_a_path_inside_the_artifacts(
    gitlab: _FakeGitlab, path: str
) -> None:
    with pytest.raises(ValueError, match="not an artifact path"):
        _forge(gitlab).job_artifact(7, path, limit=64)
    assert gitlab.requests == []


@pytest.mark.unit
def test_a_bound_a_request_names_never_raises_the_clients_own(
    gitlab: _FakeGitlab, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitlab_api, "MAX_ANSWER_BYTES", 1024)
    gitlab.answer("GET", _artifact(7), raw=b" " * 2048)
    with pytest.raises(review.Failure, match="is 2048 bytes, more than the 1024"):
        _forge(gitlab).job_artifact(7, DIGEST_FILE, limit=4096)


MR_PIPELINES = f"{PROJECT_PATH}/merge_requests/812/pipelines?per_page=100"
MR = "ai_ux_test_stories"
PROCEEDED = re.compile(rb"PROCEED: [0-9]+ flow")


def _trace(job: int) -> str:
    return f"{PROJECT_PATH}/jobs/{job}/trace"


@pytest.mark.unit
def test_a_jobs_log_is_read_only_as_far_as_its_head(gitlab: _FakeGitlab) -> None:
    gitlab.answer(
        "GET",
        _trace(7),
        headers={"Content-Type": "text/plain"},
        raw=b"PROCEED: 3 flow(s)" + b" " * 4096,
    )
    head = _forge(gitlab).job_trace_head(7, limit=64)
    assert head == (b"PROCEED: 3 flow(s)" + b" " * 4096)[:64]
    unmeasured = JSON_HEAD + b"Connection: close\r\n\r\n" + b"x" * 4096
    with _raw_server(_sender(unmeasured)) as api_url:
        forge = gitlab_api.UxGitlab(api_url, "group/proj", CREDENTIAL)
        assert forge.job_trace_head(7, limit=100) == b"x" * 100


@pytest.mark.unit
def test_runs_started_counts_the_mrs_story_jobs_whose_log_says_they_went_ahead(
    gitlab: _FakeGitlab,
) -> None:
    gitlab.answer("GET", MR_PIPELINES, [{"id": n} for n in (40, 39, 38, 37, 36)])
    gitlab.answer("GET", _jobs(39), [_job(391, MR, "canceled"), _job(392, "lint")])
    gitlab.answer("GET", _jobs(38), [_job(381, MR, "skipped"), _job(382, MR, "failed")])
    gitlab.answer("GET", _jobs(37), {"message": "boom"}, status=500)
    gitlab.answer("GET", _jobs(36), [_job(361, MR, "success")])
    for job, text in ((391, b"PROCEED: 2 flow(s)"), (382, b"SKIPPED: a draft")):
        gitlab.answer("GET", _trace(job), raw=text)
    gitlab.answer("GET", _trace(361), {"message": "gone"}, status=404)
    counted = gitlab_api.runs_started(
        _forge(gitlab),
        812,
        job=MR,
        marker=PROCEEDED,
        own_pipeline=40,
        enough=4,
        deadline=1e9,
    )
    assert counted == 1
    asked = [r["path"] for r in gitlab.requests]
    assert _jobs(40) not in asked and _trace(381) not in asked
    assert _trace(392) not in asked


@pytest.mark.unit
def test_runs_started_stops_at_enough_and_at_the_deadline(gitlab: _FakeGitlab) -> None:
    gitlab.answer("GET", MR_PIPELINES, [{"id": n} for n in (3, 2, 1)])
    for pipeline in (3, 2, 1):
        gitlab.answer("GET", _jobs(pipeline), [_job(pipeline * 10, MR, "success")])
        gitlab.answer("GET", _trace(pipeline * 10), raw=b"PROCEED: 1 flow(s)")
    values: dict[str, Any] = {
        "job": MR,
        "marker": PROCEEDED,
        "own_pipeline": None,
        "deadline": 1e9,
    }
    assert gitlab_api.runs_started(_forge(gitlab), 812, enough=2, **values) == 2
    assert _jobs(1) not in [r["path"] for r in gitlab.requests]
    gitlab.requests.clear()
    times = iter([0.0, 10.0])
    late = {**values, "deadline": 10.0, "clock": lambda: next(times)}
    assert gitlab_api.runs_started(_forge(gitlab), 812, enough=4, **late) == 1
    gitlab.answer("GET", MR_PIPELINES, {"message": "403 Forbidden"}, status=403)
    with pytest.raises(review.Skip, match="403"):
        gitlab_api.runs_started(_forge(gitlab), 812, enough=4, **values)
