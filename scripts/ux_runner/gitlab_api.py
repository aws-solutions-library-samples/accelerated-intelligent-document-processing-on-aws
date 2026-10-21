# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The GitLab calls the MR UX test makes, as a subclass of ai_mr_review's client.

``ai_mr_review.Gitlab`` lists merge requests and posts a note. The UX job also
needs to upload the video (multipart, which that client cannot send), to ask who
the token is (``GET /user``, which is not project-scoped), to read notes with
their authors, to read and edit one note, and to find a note's discussion and
reply in it. Those live here, in a subclass in a new module, so
``ai_mr_review.py`` stays byte-for-byte untouched.

The single import point
-----------------------
This is the **only** module of the UX test that imports ``ai_mr_review``; every
other module reaches it as ``gitlab_api.review``. ``test_ai_mr_review.py``
re-executes that script and replaces ``sys.modules['ai_mr_review']`` under xdist,
so two import sites in one process could hold two different ``Skip`` classes,
and an ``except review.Skip`` in one would not catch a ``Skip`` raised by the
other.

One request path, one error mapping
-----------------------------------
Every request, including the inherited ``merge_request``, goes through
:meth:`UxGitlab._send`, and whatever goes wrong leaves it as ``review.Skip`` or
``review.Failure``, never as a raw ``urllib``, ``http.client`` or ``json``
exception, so the caller's ``except`` decides the outcome. The two inherited
listings this job never calls, ``open_merge_requests`` and ``reviewed_shas``,
assert on their answers and page without a bound, so they refuse outright here
rather than sit outside that promise. A skip means GitLab's
API could not be reached or did not answer as GitLab's API: a redirect, which is
refused through ``ai_mr_review``'s opener because on an instance behind an
authenticating gateway it leads to an HTML login page, not JSON; a 401 or 403,
whose message says the token's role is the likely cause, because the least role
that can create a note is what this token is kept at; a refused or dropped
connection, or an answer that is not well-formed HTTP; an answer that is not
JSON. A failure means the tooling or the answer is wrong: any other error status,
a timeout, a request ``urllib`` cannot send, and a merge request whose fields are
not the types the job reads. An API URL no request could reach is refused when
the client is made, as a skip, which is how the orchestrator already treats a
missing one.

Bounded answers
---------------
An answer is read in pieces, up to :data:`MAX_ANSWER_BYTES` or the smaller bound
a request names, and for no longer in total than the request's timeout. The socket timeout bounds each read alone, so
without the total a peer could trickle a body a byte at a time; and the notes
and discussions listings carry note bodies that anyone who can comment on the MR
writes, at up to a million characters each. An answer over the size, by its
Content-Length or by what arrives, is a failure and is read no further. A body
shorter than its own Content-Length is a dropped connection, not an answer. A
refusal's body is read only for its first :data:`REFUSAL_BYTES`, which is more
than its message shows.

The token
---------
The token is sent as a header and appears in no message. GitLab's answers are
quoted into messages (a refusal's detail, the start of a body that is not JSON,
a redirect's target), and a gateway or a debugging proxy that echoes a request
echoes its headers with it, so every quote has the token replaced before it is
cut, which leaves no half of it either. An error ``urllib`` raises about a
header value carries the value, so that one is reported by its class alone.

What it posts
-------------
Every note body goes through ``defuse_quick_actions`` before it is sent: GitLab
executes a line starting with ``/`` in a note created or edited through the API,
with the token's permissions. It is idempotent, so passing a whole existing note
body back through it on an edit is safe. Only one upload URL shape is ever put in
a note, checked by :func:`safe_upload_url`. Notes are attributed by author id
from ``GET /user``, so a marker another user pasted into a comment neither
suppresses a run nor gets edited. The token's own notes include
``ai_mr_review``'s reviews, whose text quotes MR content, so within them a marker
counts only where it is written: a review's as the note's first line
(:func:`review_note_for`), and a UX section's where ``section.sections_in``
reads it.

What it reads of earlier pipelines
----------------------------------
:func:`scheduled_digests` is how the nightly story digest finds the nights
before it: one listing of a branch's newest scheduled pipelines, bounded to one
page, then each pipeline's jobs, then one file of one job's artifacts by job id,
read only up to the caller's size and returned as it arrived, for the caller to
validate. Each of those is a GET, so the token it is given is used to read and
nothing else. The ref-based artifact endpoint is not used: it answers from the
latest successful pipeline on the ref, which on a day with a merge is a push
pipeline whose nightly jobs never ran.

:func:`runs_started` is how the MR story job counts the runs an MR has had,
posted or not: one listing of the MR's pipelines, each pipeline's jobs, and the
first bytes of each story job's log, which says whether its precheck went ahead.
A log is kept when a job is cancelled or times out, which its artifacts are not,
so a run cancelled by a newer push counts as the run it was.
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SDLC_DIR = str(Path(__file__).resolve().parents[1] / "sdlc")
if _SDLC_DIR not in sys.path:
    sys.path.insert(0, _SDLC_DIR)

import ai_mr_review as review  # noqa: E402

#: The only upload URL shape that may reach a note (D-SECTION).
UPLOAD_URL_RE = re.compile(r"/uploads/[0-9a-f]{16,64}/[A-Za-z0-9._-]{1,128}")

#: How many pages of 100 a listing may run to before it is treated as runaway.
MAX_PAGES = 50

#: The most of any one answer that is read: a page of 100 notes or discussions,
#: or a merge request whose description runs to GitLab's million characters.
MAX_ANSWER_BYTES = 16 * 1024 * 1024

#: How much of a refused request's body is read for the detail its message shows.
REFUSAL_BYTES = 4096

#: How the token is shown when an answer quoted into a message carries it.
CREDENTIAL_SHOWN_AS = "[redacted]"

#: Seconds between two looks for the code-review note.
POLL_SECONDS = 30.0

#: How many of a branch's newest scheduled pipelines one history listing reads.
SCHEDULED_PIPELINES = 20
#: The most of an MR's pipelines :func:`runs_started` reads, newest first.
MR_PIPELINES = 100
#: How much of a job's log :func:`runs_started` reads; a story job's precheck
#: prints its decision before anything is installed, so it is near the start.
TRACE_HEAD_BYTES = 512 * 1024

_CHUNK_BYTES = 64 * 1024
_DETAIL_CHARS = 400
_EXCERPT_CHARS = 200
_LOCATION_CHARS = 120
_BOUNDARY_RE = re.compile(r"[0-9A-Za-z'()+_,./:=?-]{1,70}")
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
_LENGTH_RE = re.compile(r"[0-9]{1,18}")
_PAGE_RE = re.compile(r"[0-9]{1,9}")
_ARTIFACT_PATH_RE = re.compile(
    r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}(?:/[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}){0,7}"
)
_FINISHED_JOB_STATES = frozenset({"success", "failed"})
_STARTED_JOB_STATES = frozenset({"running", "success", "failed", "canceled"})


def _header(headers: Mapping[str, str], name: str) -> str:
    lowered = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lowered), "")


def _note_body(body: str) -> dict[str, str]:
    return {"body": review.defuse_quick_actions(body)}


def _object(payload: object, what: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise review.Failure(
            f"GitLab answered {what} with something other than an object"
        )
    return payload


def _usable_api_url(url: object) -> bool:
    """Whether ``url`` is an absolute http(s) URL a request can be sent to.

    Printable ASCII, a host, a port that parses, and no user name, password,
    query or fragment: each of those would be quoted into every message that
    names the URL, or land in the middle of every path built from it.
    """
    if not isinstance(url, str) or not url.isascii() or not url.isprintable():
        return False
    if " " in url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
        and port != 0
    )


def _declared_length(headers: Any) -> int | None:
    value = headers.get("Content-Length") if headers is not None else None
    text = value.strip() if isinstance(value, str) else ""
    return int(text) if _LENGTH_RE.fullmatch(text) else None


def _read_capped(stream: Any, cap: int, seconds: float) -> tuple[bytes, str]:
    """At most ``cap`` bytes of ``stream``, and why reading stopped.

    ``"done"`` at the end of the body; ``"over"`` when it holds more than ``cap``
    bytes, of which the first ``cap`` are returned; ``"slow"`` when it is still
    arriving ``seconds`` after the first read. Each read is one call into the
    socket, so the socket timeout bounds a read and this bounds them all.
    """
    read = getattr(stream, "read1", None) or stream.read
    deadline = time.monotonic() + seconds
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = read(min(_CHUNK_BYTES, cap + 1 - size))
        if not chunk:
            return b"".join(chunks), "done"
        chunks.append(chunk)
        size += len(chunk)
        if size > cap:
            return b"".join(chunks)[:cap], "over"
        if time.monotonic() > deadline:
            return b"".join(chunks), "slow"


@dataclass
class UxGitlab(review.Gitlab):
    """``ai_mr_review.Gitlab`` plus the calls the UX job needs."""

    upload_timeout: int = 600

    def __post_init__(self) -> None:
        """Refuse an API URL no request could reach, before any request is made.

        The URL is not quoted back, because what is wrong with it may be a user
        name or password written into it.
        """
        super().__post_init__()
        if not _usable_api_url(self.api_url):
            raise review.Skip(
                "the GitLab API URL is not an absolute http(s) URL without a user "
                "name, password, query or fragment, so GitLab cannot be reached"
            )

    def __repr__(self) -> str:
        """Without the token, which the generated dataclass repr would print."""
        return f"UxGitlab(api_url={self.api_url!r}, project={self.project!r})"

    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[object, dict[str, str]]:
        """The inherited calls' path, routed through :meth:`_send`."""
        return self._send(method, self._project_url(path), body=body)

    def _path_of(self, url: str) -> str:
        rest = url[len(self.api_url) :] if url.startswith(self.api_url) else url
        return rest.split("?", 1)[0]

    def _scrubbed(self, text: str) -> str:
        """``text`` with the token replaced, for a message that quotes GitLab."""
        return text.replace(self.token, CREDENTIAL_SHOWN_AS) if self.token else text

    def _excerpt(self, raw: bytes, chars: int) -> str:
        """The start of a body, decoded, the token replaced, then cut to ``chars``.

        Enough bytes are decoded to hold a token that starts inside the excerpt,
        so one that would run past the cut is replaced whole, not half shown.
        """
        window = raw[: 4 * (chars + len(self.token))]
        return self._scrubbed(window.decode(errors="replace"))[:chars]

    def _send(
        self,
        method: str,
        url: str,
        *,
        body: dict | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        timeout: int = 60,
        empty_ok: bool = False,
        as_bytes: bool = False,
        limit: int | None = None,
        head: bool = False,
    ) -> tuple[object, dict[str, str]]:
        """One request: JSON ``body`` or raw ``data``, the token as a header.

        Returns the parsed JSON and the response headers. With ``empty_ok``, a 204
        or an empty body returns ``(None, headers)``. With ``as_bytes``, the body
        is returned as it arrived, whatever its type, which is how a file of a
        job's artifacts is read. ``limit`` bounds the answer below
        :data:`MAX_ANSWER_BYTES`, and with ``head`` an answer past it is cut there
        rather than refused, which is how the start of a job's log is read.
        Everything else that can go wrong is a ``review.Skip`` or a
        ``review.Failure``, as the module docstring maps them.
        """
        if body is not None and data is not None:
            raise ValueError("send either a JSON body or raw data, not both")
        path = self._path_of(url)
        payload = json.dumps(body).encode() if body is not None else data
        try:
            request = urllib.request.Request(url, data=payload, method=method)
        except ValueError:
            raise review.Skip(
                f"{method} {path}: the request URL is not one a request can be sent to"
            ) from None
        request.add_header("PRIVATE-TOKEN", self.token)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        elif content_type:
            request.add_header("Content-Type", content_type)
        try:
            with review._NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
                status = response.status
                headers = dict(response.headers)
                kind = response.headers.get("Content-Type", "")
                raw = self._answer(response, method, path, timeout, limit, head=head)
        except urllib.error.HTTPError as error:
            raise self._refusal(error, method, path, timeout) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise review.Failure(
                    f"GitLab did not answer {method} {path} within {timeout}s"
                ) from error
            raise review.Skip(
                self._scrubbed(f"cannot reach {self.api_url}: {error.reason}")
            ) from error
        except TimeoutError as error:
            raise review.Failure(
                f"GitLab did not answer {method} {path} within {timeout}s"
            ) from error
        except OSError as error:
            raise review.Skip(
                self._scrubbed(f"cannot reach {self.api_url}: {error}")
            ) from error
        except http.client.HTTPException as error:
            raise review.Skip(
                f"cannot reach {self.api_url}: the answer to {method} {path} is not "
                f"well-formed HTTP ({type(error).__name__})"
            ) from None
        except ValueError as error:
            raise review.Failure(
                f"{method} {path} could not be sent ({type(error).__name__}): the "
                "API URL or the token cannot be written into a request"
            ) from None
        if empty_ok and (status == 204 or not raw):
            return None, headers
        if as_bytes:
            return raw, headers
        if "json" not in kind.lower():
            raise review.Skip(
                self._scrubbed(
                    f"{method} {path} answered {status} with {kind!r}, not JSON, so "
                    "this is not GitLab's API replying. Body starts: "
                    f"{self._excerpt(raw, _EXCERPT_CHARS)!r}"
                )
            )
        try:
            return json.loads(raw), headers
        except (ValueError, RecursionError) as error:
            raise review.Skip(
                self._scrubbed(
                    f"{method} {path} claimed to return JSON but did not: {error}"
                )
            ) from None

    def _answer(
        self,
        response: Any,
        method: str,
        path: str,
        timeout: int,
        limit: int | None = None,
        *,
        head: bool = False,
    ) -> bytes:
        """The body of a 2xx answer, bounded in size and in time.

        The size is :data:`MAX_ANSWER_BYTES`, or ``limit`` when that is smaller;
        with ``head`` the answer's first ``cap`` bytes are its body. A read error
        is mapped here, whatever ``http.client`` calls it, because here it can
        only be the answer's fault: the ``ValueError`` that :meth:`_send` maps to
        an unsendable request must not catch one from a body, such as a chunk
        size that is not a number.
        """
        cap = MAX_ANSWER_BYTES if limit is None else min(limit, MAX_ANSWER_BYTES)
        declared = _declared_length(response.headers)
        if head:
            try:
                raw, ended = _read_capped(response, cap, timeout)
            except (ValueError, http.client.HTTPException) as error:
                raise review.Skip(
                    f"cannot reach {self.api_url}: the answer to {method} {path} "
                    f"broke off ({type(error).__name__})"
                ) from None
            if ended == "slow":
                raise review.Failure(
                    f"GitLab's answer to {method} {path} was still arriving, so it "
                    f"did not finish within {timeout}s"
                )
            return raw
        if declared is not None and declared > cap:
            raise review.Failure(
                f"GitLab's answer to {method} {path} is {declared} bytes, more than "
                f"the {cap} this client reads"
            )
        try:
            raw, ended = _read_capped(response, cap, timeout)
        except (ValueError, http.client.HTTPException) as error:
            raise review.Skip(
                f"cannot reach {self.api_url}: the answer to {method} {path} broke "
                f"off ({type(error).__name__})"
            ) from None
        if ended == "over":
            raise review.Failure(
                f"GitLab's answer to {method} {path} runs past the "
                f"{cap} bytes this client reads"
            )
        if ended == "slow":
            raise review.Failure(
                f"GitLab's answer to {method} {path} was still arriving, so it did "
                f"not finish within {timeout}s"
            )
        if declared is not None and len(raw) < declared:
            raise review.Skip(
                f"cannot reach {self.api_url}: the answer to {method} {path} broke "
                f"off after {len(raw)} of its {declared} bytes"
            )
        return raw

    def _refusal(
        self, error: urllib.error.HTTPError, method: str, path: str, timeout: int
    ) -> Exception:
        code = error.code
        if 300 <= code < 400:
            location = error.headers.get("Location", "") if error.headers else ""
            shown = self._scrubbed(str(location))[:_LOCATION_CHARS]
            return review.Skip(
                f"{self.api_url} redirected {method} {path} to an authentication "
                f"gateway ({shown!r}), so the REST API is not reachable with a "
                "token alone from here."
            )
        if code == 401:
            return review.Skip("GitLab rejected the token (401)")
        if code == 403:
            return review.Skip(
                f"GitLab refused {method} {path} (403): the token's role cannot do "
                "this, or it lacks api scope"
            )
        raw = b""
        if error.fp:
            try:
                raw, _ = _read_capped(error, REFUSAL_BYTES, timeout)
            except (OSError, ValueError, http.client.HTTPException):
                raw = b""
        detail = self._excerpt(raw, _DETAIL_CHARS)
        return review.Failure(f"GitLab returned {code} for {method} {path}: {detail}")

    def _project_url(self, path: str) -> str:
        return f"{self.api_url}/projects/{self._encoded}{path}"

    def _pages(self, path: str) -> list[dict[str, Any]]:
        """Every item of a paginated project listing, following ``X-Next-Page``."""
        collected: list[dict[str, Any]] = []
        page = 1
        joiner = "&" if "?" in path else "?"
        for _ in range(MAX_PAGES):
            url = self._project_url(f"{path}{joiner}per_page=100&page={page}")
            payload, headers = self._send("GET", url)
            if not isinstance(payload, list):
                raise review.Failure(f"GitLab answered GET {path} with no list")
            collected.extend(item for item in payload if isinstance(item, dict))
            following = _header(headers, "X-Next-Page").strip()
            if not following:
                return collected
            if not _PAGE_RE.fullmatch(following) or int(following) <= page:
                raise review.Failure(f"GitLab's X-Next-Page for {path} is not ahead")
            page = int(following)
        raise review.Failure(f"GET {path} runs past {MAX_PAGES} pages")

    def open_merge_requests(self, target_branch: str) -> list[review.MergeRequest]:
        """Refused: the inherited listing asserts its answers and pages unbounded."""
        raise review.Failure(
            "UxGitlab does not list merge requests; the UX job tests one MR"
        )

    def reviewed_shas(self, iid: int) -> set[tuple[str, int]]:
        """Refused: the job finds its own and the review's notes by author id."""
        raise review.Failure(
            "UxGitlab does not read reviewed heads; see notes and review_note_for"
        )

    def checked_upload_url(self, payload: Mapping[str, Any]) -> str:
        """:func:`safe_upload_url`, with the token replaced in what a refusal quotes."""
        return safe_upload_url(payload, scrub=self._scrubbed)

    def merge_request(self, iid: int) -> review.MergeRequest:
        """The merge request, with every field the job reads checked first.

        The inherited method asserts the payload's type and indexes it, so a
        payload of the wrong shape would leave as an ``AssertionError`` or a
        ``KeyError``, and a title that is not text would reach the caller as an
        ``AttributeError`` on ``startswith``. Each of those is a failure here.
        """
        number = int(iid)
        payload, _ = self._request("GET", f"/merge_requests/{number}")
        data = _object(payload, f"GET merge request !{number}")
        try:
            found = review.MergeRequest.from_api(data)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise review.Failure(
                f"GitLab's answer for merge request !{number} lacks a usable field "
                f"({type(error).__name__})"
            ) from None
        texts = (
            found.title,
            found.author,
            found.source_branch,
            found.target_branch,
            found.web_url,
            found.head_sha,
        )
        if not all(isinstance(value, str) for value in texts):
            raise review.Failure(
                f"GitLab's answer for merge request !{number} has a text field "
                "that is not text"
            )
        return found

    def current_user(self) -> dict[str, Any]:
        """The token's own user. Not project-scoped, so it is ``/user`` itself."""
        payload, _ = self._send("GET", f"{self.api_url}/user")
        user = _object(payload, "GET /user")
        ident = user.get("id")
        if isinstance(ident, bool) or not isinstance(ident, int):
            raise review.Failure("GET /user did not name the token's user id")
        return user

    def notes(self, iid: int) -> list[dict[str, Any]]:
        """Every note on the MR, oldest first, with its author."""
        return self._pages(
            f"/merge_requests/{int(iid)}/notes?sort=asc&order_by=created_at"
        )

    def note(self, iid: int, note_id: int) -> dict[str, Any]:
        """One note, as it is now."""
        url = self._project_url(f"/merge_requests/{int(iid)}/notes/{int(note_id)}")
        payload, _ = self._send("GET", url)
        return _object(payload, "GET note")

    def update_note(self, iid: int, note_id: int, body: str) -> dict[str, Any]:
        """Replace a note's body; the body is defused first."""
        url = self._project_url(f"/merge_requests/{int(iid)}/notes/{int(note_id)}")
        payload, _ = self._send("PUT", url, body=_note_body(body))
        return _object(payload, "PUT note")

    def create_note(self, iid: int, body: str) -> dict[str, Any]:
        """Post a new note and return it; the body is defused first."""
        url = self._project_url(f"/merge_requests/{int(iid)}/notes")
        payload, _ = self._send("POST", url, body=_note_body(body))
        return _object(payload, "POST note")

    def post_note(self, iid: int, body: str) -> None:
        """The inherited writer, routed through :meth:`create_note` so it defuses."""
        self.create_note(iid, body)

    def discussion_id_for(self, iid: int, note_id: int) -> str | None:
        """The id of the discussion holding ``note_id``, or None."""
        for discussion in self._pages(f"/merge_requests/{int(iid)}/discussions"):
            thread = discussion.get("notes")
            if not isinstance(thread, list):
                continue
            if any(isinstance(n, dict) and n.get("id") == note_id for n in thread):
                ident = discussion.get("id")
                return ident if isinstance(ident, str) and ident else None
        return None

    def reply(self, iid: int, discussion_id: str, body: str) -> dict[str, Any]:
        """Reply in a discussion; the body is defused first."""
        thread = urllib.parse.quote(discussion_id, safe="")
        url = self._project_url(
            f"/merge_requests/{int(iid)}/discussions/{thread}/notes"
        )
        payload, _ = self._send("POST", url, body=_note_body(body))
        return _object(payload, "POST reply")

    def upload(
        self, path: Path, *, filename: str, content_type: str = "video/mp4"
    ) -> dict[str, Any]:
        """Upload a file to the project and return GitLab's answer.

        A file that cannot be read is a failure like any other upload that does
        not happen, so a caller that tolerates a failed upload tolerates this one.
        """
        name = _UNSAFE_NAME_RE.sub("-", filename).lstrip(".")[:128] or "upload"
        try:
            content = path.read_bytes()
        except OSError as error:
            raise review.Failure(
                f"the file to upload could not be read ({type(error).__name__})"
            ) from None
        data, multipart_type = multipart("file", name, content, content_type)
        payload, _ = self._send(
            "POST",
            self._project_url("/uploads"),
            data=data,
            content_type=multipart_type,
            timeout=self.upload_timeout,
        )
        return _object(payload, "POST /uploads")

    def scheduled_pipelines(
        self, ref: str, *, limit: int = SCHEDULED_PIPELINES
    ) -> list[int]:
        """The ids of ``ref``'s newest scheduled pipelines, newest first.

        One page of at most ``limit``, which is at most GitLab's largest page of
        100. A pipeline listed for another ref or from another source is not
        returned, so a filter the API did not apply cannot bring a push or a
        manual pipeline in.
        """
        if not 1 <= limit <= 100:
            raise ValueError("a pipeline listing reads 1 to 100 pipelines")
        query = urllib.parse.urlencode(
            {
                "ref": ref,
                "source": "schedule",
                "order_by": "id",
                "sort": "desc",
                "per_page": limit,
            }
        )
        payload, _ = self._send("GET", self._project_url(f"/pipelines?{query}"))
        if not isinstance(payload, list):
            raise review.Failure("GitLab answered GET /pipelines with no list")
        ids: list[int] = []
        for item in payload[:limit]:
            if not isinstance(item, dict):
                continue
            ident = item.get("id")
            if isinstance(ident, bool) or not isinstance(ident, int) or ident < 1:
                continue
            listed_for = (item.get("ref", ref), item.get("source", "schedule"))
            if listed_for != (ref, "schedule"):
                continue
            if ident not in ids:
                ids.append(ident)
        return sorted(ids, reverse=True)

    def pipeline_jobs(self, pipeline_id: int) -> list[dict[str, Any]]:
        """Every job of one pipeline, the latest attempt of each, as GitLab lists them."""
        return self._pages(f"/pipelines/{int(pipeline_id)}/jobs")

    def merge_request_pipelines(
        self, iid: int, *, limit: int = MR_PIPELINES
    ) -> list[int]:
        """The ids of one MR's newest pipelines, newest first, one page of ``limit``."""
        if not 1 <= limit <= 100:
            raise ValueError("a pipeline listing reads 1 to 100 pipelines")
        query = urllib.parse.urlencode({"per_page": limit})
        url = self._project_url(f"/merge_requests/{int(iid)}/pipelines?{query}")
        payload, _ = self._send("GET", url)
        if not isinstance(payload, list):
            raise review.Failure(
                "GitLab answered GET /merge_requests/:iid/pipelines with no list"
            )
        ids: list[int] = []
        for item in payload[:limit]:
            ident = item.get("id") if isinstance(item, dict) else None
            if isinstance(ident, int) and not isinstance(ident, bool) and ident > 0:
                if ident not in ids:
                    ids.append(ident)
        return sorted(ids, reverse=True)

    def job_trace_head(self, job_id: int, *, limit: int = TRACE_HEAD_BYTES) -> bytes:
        """The first ``limit`` bytes of one job's log, as GitLab stores it."""
        url = self._project_url(f"/jobs/{int(job_id)}/trace")
        payload, _ = self._send("GET", url, as_bytes=True, limit=limit, head=True)
        return payload if isinstance(payload, bytes) else b""

    def job_artifact(self, job_id: int, path: str, *, limit: int) -> bytes:
        """One file of a job's artifacts as it is stored, refused past ``limit`` bytes.

        The bytes are returned whatever type GitLab names, for the caller to
        validate; ``path`` is a path inside the artifacts, never one on disk.
        """
        if not _ARTIFACT_PATH_RE.fullmatch(path):
            raise ValueError("not an artifact path this client reads")
        quoted = urllib.parse.quote(path, safe="/")
        url = self._project_url(f"/jobs/{int(job_id)}/artifacts/{quoted}")
        payload, _ = self._send("GET", url, as_bytes=True, limit=limit)
        return payload if isinstance(payload, bytes) else b""


def multipart(
    field: str,
    filename: str,
    content: bytes,
    content_type: str,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """A one-part ``multipart/form-data`` body and its Content-Type value."""
    for value in (field, filename, content_type):
        if any(c in value for c in '"\r\n'):
            raise ValueError("multipart headers cannot hold quotes or line breaks")
    mark = boundary if boundary is not None else uuid.uuid4().hex
    if not _BOUNDARY_RE.fullmatch(mark):
        raise ValueError("not a usable multipart boundary")
    head = (
        f"--{mark}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{mark}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={mark}"


def _authored_by(note: Mapping[str, Any], author_id: int) -> bool:
    author = note.get("author")
    ident = author.get("id") if isinstance(author, Mapping) else None
    return not isinstance(ident, bool) and isinstance(ident, int) and ident == author_id


def _newest_key(note: Mapping[str, Any]) -> tuple[int, str]:
    ident = note.get("id")
    number = ident if isinstance(ident, int) and not isinstance(ident, bool) else -1
    return number, str(note.get("created_at") or "")


def my_notes(
    notes: Sequence[Mapping[str, Any]], author_id: int
) -> list[dict[str, Any]]:
    """The notes ``author_id`` wrote, system notes left out, newest first."""
    mine = [
        dict(note)
        for note in notes
        if _authored_by(note, author_id) and not note.get("system")
    ]
    return sorted(mine, key=_newest_key, reverse=True)


def review_note_for(
    notes: Sequence[Mapping[str, Any]], head_sha: str, author_id: int
) -> dict[str, Any] | None:
    """The newest code-review note ``author_id`` posted for exactly ``head_sha``.

    The review's marker counts only as the note's first line, where
    ``ai_mr_review`` writes it. The review text below it quotes MR content, and a
    marker quoted there would otherwise make this note the review of a head it
    does not review, so the section and its reply would land under the wrong one.
    """
    for note in my_notes(notes, author_id):
        body = note.get("body")
        found = review.MARKER_RE.match(body) if isinstance(body, str) else None
        if found is not None and found.group("sha") == head_sha:
            return note
    return None


@dataclass(frozen=True)
class ReviewWait:
    """How :func:`wait_for_review_note` ended, and what it had read by then.

    ``outcome`` is ``found`` (the code-review note for the head), ``timeout`` (the
    poll reached its end without one), ``moved`` (the MR's head is ``moved_to``,
    not the head waited for) or ``posted`` (``already_posted`` held for the token's
    own notes). ``mine`` is the token's notes from the last listing, newest first,
    and ``me`` the token's user id; both are empty for ``moved``.
    """

    outcome: str
    review_note: dict[str, Any] | None
    mine: list[dict[str, Any]]
    me: int | None
    moved_to: str | None


def wait_for_review_note(
    forge: UxGitlab,
    iid: int,
    head_sha: str,
    *,
    already_posted: Callable[[list[dict[str, Any]]], bool],
    poll_minutes: float,
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float = POLL_SECONDS,
) -> ReviewWait:
    """Wait for the token's code-review note for ``head_sha``, as a post must.

    First the MR itself, so a head that moved on is never posted for, then the
    token's user. Then the notes are listed until the review note for exactly
    this head appears, every ``poll_seconds``, for at most ``poll_minutes`` and
    never past ``deadline`` (both read on ``clock``). Each listing first asks
    ``already_posted`` of the token's own notes, so a section another run posted
    for this head while this one waited stops it before anything is uploaded.
    """
    current = forge.merge_request(iid)
    if current.head_sha and current.head_sha != head_sha:
        return ReviewWait("moved", None, [], None, current.head_sha)
    me = forge.current_user()["id"]
    poll_until = min(clock() + poll_minutes * 60, deadline)
    while True:
        notes = forge.notes(iid)
        mine = my_notes(notes, me)
        if already_posted(mine):
            return ReviewWait("posted", None, mine, me, None)
        review_note = review_note_for(notes, head_sha, me)
        if review_note is not None:
            return ReviewWait("found", review_note, mine, me, None)
        if clock() >= poll_until:
            return ReviewWait("timeout", None, mine, me, None)
        sleep(poll_seconds)


def scheduled_digests(
    forge: UxGitlab,
    *,
    ref: str,
    job: str,
    artifact: str,
    max_bytes: int,
    deadline: float,
    own_pipeline: int | None = None,
    limit: int = SCHEDULED_PIPELINES,
    clock: Callable[[], float] = time.monotonic,
) -> list[tuple[int, bytes | None]]:
    """``artifact`` of ``job`` in each of ``ref``'s newest scheduled pipelines.

    Newest first, with ``own_pipeline`` left out: one listing of at most
    ``limit`` pipelines, then each pipeline's jobs, then the newest finished
    ``job``'s file, read by job id and refused past ``max_bytes``. A pipeline
    whose job is absent or unfinished, or whose jobs or file cannot be read, is
    paired with None, a gap rather than the end of the history, and so is every
    pipeline not yet reached at ``deadline`` on ``clock``. A listing that cannot
    be read raises, as any other request here does.
    """
    found: list[tuple[int, bytes | None]] = []
    for pipeline_id in forge.scheduled_pipelines(ref, limit=limit):
        if pipeline_id == own_pipeline:
            continue
        raw: bytes | None = None
        if clock() < deadline:
            try:
                ident = _finished_job(forge.pipeline_jobs(pipeline_id), job)
                if ident is not None:
                    raw = forge.job_artifact(ident, artifact, limit=max_bytes)
            except (review.Skip, review.Failure):
                raw = None
        found.append((pipeline_id, raw))
    return found


def runs_started(
    forge: UxGitlab,
    iid: int,
    *,
    job: str,
    marker: re.Pattern[bytes],
    own_pipeline: int | None,
    enough: int,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """How many of an MR's earlier ``job`` jobs went ahead, counting to ``enough``.

    A job went ahead when the first :data:`TRACE_HEAD_BYTES` of its log hold
    ``marker``, whether it then posted, failed, was cancelled or is still
    running. ``own_pipeline`` is left out. A pipeline whose jobs, or a job whose
    log, cannot be read counts nothing, and so does every pipeline not reached at
    ``deadline`` on ``clock``; a listing that cannot be read raises, as any other
    request here does.
    """
    count = 0
    for pipeline_id in forge.merge_request_pipelines(iid):
        if count >= enough or clock() >= deadline:
            break
        if pipeline_id == own_pipeline:
            continue
        try:
            jobs = forge.pipeline_jobs(pipeline_id)
        except (review.Skip, review.Failure):
            continue
        for item in jobs:
            ident = item.get("id")
            if (
                item.get("name") != job
                or item.get("status") not in _STARTED_JOB_STATES
                or not isinstance(ident, int)
                or isinstance(ident, bool)
                or ident < 1
            ):
                continue
            try:
                head = forge.job_trace_head(ident)
            except (review.Skip, review.Failure):
                continue
            if marker.search(head):
                count += 1
    return min(count, enough)


def _finished_job(jobs: Sequence[Mapping[str, Any]], name: str) -> int | None:
    """The id of the newest job called ``name`` that ran to its end, or None."""
    ids = [
        ident
        for item in jobs
        if item.get("name") == name
        and item.get("status") in _FINISHED_JOB_STATES
        and isinstance(ident := item.get("id"), int)
        and not isinstance(ident, bool)
        and ident > 0
    ]
    return max(ids) if ids else None


def safe_upload_url(
    payload: Mapping[str, Any], *, scrub: Callable[[str], str] | None = None
) -> str:
    """GitLab's upload URL, only if it has the one shape a note may carry.

    A refusal quotes the answer's ``url``, passed through ``scrub`` before it is
    cut, so a client that holds a token can have it replaced whole
    (:meth:`UxGitlab.checked_upload_url`).
    """
    url = payload.get("url") if isinstance(payload, Mapping) else None
    if (
        not isinstance(url, str)
        or not UPLOAD_URL_RE.fullmatch(url)
        or not url.rsplit("/", 1)[-1].strip(".")
    ):
        quoted = repr(url)
        shown = (scrub(quoted) if scrub is not None else quoted)[:120]
        raise review.Failure(
            f"GitLab's upload answer has no /uploads/<secret>/<file> URL: {shown}"
        )
    return url
