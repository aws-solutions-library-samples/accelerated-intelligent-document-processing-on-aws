#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``report.html``: one story run as a page a person browses, made from its files.

A run directory holds what a person needs to see what happened: the verdicts in
``results.json``, the run's context in ``story-run.json``, and for each flow
attempt its journal, its screenshots, the model's own ``report.md`` and, for a
few flows, a video. :func:`render` turns those files into one self-contained HTML
page: a header naming the run, an index of every flow with the worst verdict
first, a section for each flow, and the setup, the cost and the caveats at the
end. :func:`write_report` writes it as ``report.html`` beside them. The
orchestrator does that wherever it finishes a run directory, and
``make ai-ux-test-stories-report RUN=<dir>`` does it for any run directory, a
downloaded CI artifact or an older local run included.

Every string in those files can carry text from a page under test or from the
model, so the page is built on four rules:

* every value is HTML-escaped, in attributes too, and none is passed through as
  markup;
* no link is made from page or model text: an address the screen showed, or a
  documentation reference a note gave, is shown as text;
* every ``src`` and ``href`` is an anchor on the page or a relative path to a
  regular file inside the run directory, built from a flow key, a fixed file
  name, and a screenshot or video name of a closed shape, with every directory on
  the way checked to be a real one and no part a symbolic link; a name of any
  other shape is never linked;
* the page loads nothing from anywhere else and holds no script: its Content
  Security Policy allows images and media of its own origin and inline style, and
  nothing more.

Navigating a large run needs no script either. Radio inputs placed before the
page's content, with ``:checked ~`` rules, show only the flows of one verdict, one
persona or one story; the flows are listed by story as well, and each attempt's
screenshots sit in a closed ``<details>``, whose lazily loaded images are not
fetched until it is opened.

The text fields of ``results.json`` were sanitised for a GitLab note, which writes
``<`` as ``&lt;`` and puts zero-width spaces into whatever GitLab would act on.
The page shows such a field as the note reads, then escapes it for HTML.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import html
import json
import math
import os
import re
import stat
import sys
import urllib.parse
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import TypeGuard

_HERE = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0] or ".").resolve() == _HERE:
    sys.path.pop(0)
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from ux_runner import python_floor  # noqa: E402

python_floor.require(__file__)

from ux_runner import section, stories, story_verdict  # noqa: E402

REPORT_FILE = "report.html"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; img-src 'self'; media-src 'self'; "
    "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
)
ATTEMPT_FILES: tuple[str, ...] = (
    "journal.jsonl",
    "report.md",
    "audit.json",
    "refusals.jsonl",
)

_RECORD_MAX_BYTES = 8 * 1024 * 1024
_REPORT_MD_MAX_BYTES = 512 * 1024
_REPORT_MD_CHARS = 60_000
_SCREENS_MAX = 400
_NOTES_PER_FLOW = 60
_FIELD_CHARS = 2000
_NAME_CHARS = 160
_EPOCH_MAX = 4_102_444_800.0
_LAST = 10**9
_SCREENS_DIR = "screens"
_ALL = "_all"
_DASH = "–"
_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,159}")
_SCREEN_NAME_RE = re.compile(r"[0-9]{3,6}\.jpg")
_JOURNAL_SCREEN_RE = re.compile(r"screens/([0-9]{3,6}\.jpg)")
_VIDEO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp4")
_CSS_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,100}")
_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_TEXT_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_TRUNC
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
_EXPECTATION_WORDS = {
    "allowed": "allowed",
    "refused": "refused",
    "not_offered": "not offered",
}
_EXPECTATION_MEANING = {
    "allowed": "the docs say this role can do it",
    "refused": "the docs say the UI offers it to this role and refuses it",
    "not_offered": "the docs say the UI does not offer it to this role",
}
_KIND_WORDS = {
    "read": "a read story",
    "commit_point": "a story that ends at a commit point, where it stops before saving",
}
_BLOCKED_WORDS = {
    "no_route": "no route",
    "wrong_outcome": "the wrong outcome",
    "refused": "refused although allowed",
    "precondition": "a precondition missing",
}
_GAVE_UP_WORDS = {
    "budget_exhausted": "the step budget ran out",
    "refusal_limit": "too many of its calls were refused",
    "max_turns": "the turn cap was reached",
    "dollar_cap": "the dollar cap was reached",
    "wall_clock": "the wall clock ran out",
}
_NOT_RUN_WORDS = {
    "persona": "no persona was set for its Cognito group",
    "budget": "the run's budget could not cover it",
    "time": "the run's time could not cover it",
    "sign_in_refused": "an earlier sign-in for its group was refused",
    "since": "the stack is older than the story",
    "precondition": "a fixture it needs is missing",
}
_STATE_WORDS = {
    "single": "One attempt, which nothing contradicts.",
    "unconfirmed": "Unconfirmed: one attempt, not run again.",
    "confirmed": "Confirmed: a second attempt agreed.",
    "mixed": "Mixed: a second attempt reached another verdict.",
}
_USAGE_WORDS = (
    ("input_tokens", "in"),
    ("output_tokens", "out"),
    ("cache_read_input_tokens", "cache read"),
    ("cache_creation_input_tokens", "cache write"),
)
_VERSION_KEYS = (
    ("claude_code", "Claude Code"),
    ("chrome_devtools_mcp", "chrome-devtools-mcp"),
    ("chrome", "Chrome"),
    ("chrome_platform", "Chrome platform"),
    ("node", "Node.js"),
    ("ffmpeg", "ffmpeg"),
)
_LIMIT_KEYS = (
    ("step_budget_multiple", "Steps a documented step"),
    ("step_budget_floor", "Least step budget"),
    ("documented_steps_max", "Most documented steps"),
    ("flow_usd", "Dollars a flow"),
    ("flow_minutes", "Minutes a flow"),
)
_SECTIONS = (
    ("index", "Flows, worst first"),
    ("findings", "Findings"),
    ("by-story", "By story"),
    ("flows", "Each flow"),
    ("setup", "Setup and cost"),
    ("caveats", "Caveats"),
)
_STYLE = """
:root{color-scheme:light dark;--fg:#1f2328;--bg:#fff;--muted:#59636e;--line:#d1d9e0;
--panel:#f6f8fa;--bad:#cf222e;--good:#1a7f37;--unknown:#9a6700;--error:#8250df;
--accent:#0969da}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--bg:#0d1117;--muted:#9198a1;
--line:#3d444d;--panel:#151b23;--bad:#ff7b72;--good:#3fb950;--unknown:#d29922;
--error:#ab7df8;--accent:#4493f8}}
*{box-sizing:border-box}
body{margin:0 auto;max-width:1320px;padding:0 20px 64px;color:var(--fg);
background:var(--bg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,"Helvetica Neue",Arial,sans-serif}
h1{font-size:24px;margin:24px 0 4px}
h2{font-size:19px;margin:32px 0 8px;padding-bottom:4px;
border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:0 0 6px}
h4{font-size:14px;margin:16px 0 6px}
a{color:var(--accent)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:12.5px}
.muted,small{color:var(--muted)}
dl.facts{display:grid;grid-template-columns:max-content 1fr;gap:2px 16px;margin:8px 0}
dl.facts dt{color:var(--muted)}
dl.facts dd{margin:0}
nav.sections a{margin-right:14px}
.filter{position:absolute;opacity:0;width:1px;height:1px;margin:0;
pointer-events:none}
.chips{margin:4px 0}
.chips .name{display:inline-block;min-width:6em;color:var(--muted)}
.chips small{color:inherit;opacity:.8}
.chips label{display:inline-block;margin:2px 4px 2px 0;padding:1px 10px;
border:1px solid var(--line);border-radius:999px;background:var(--panel);
cursor:pointer}
table{border-collapse:collapse;width:100%}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;
vertical-align:top}
thead th{background:var(--panel);font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tone-bad{--tone:var(--bad)}
.tone-good{--tone:var(--good)}
.tone-unknown{--tone:var(--unknown)}
.tone-error{--tone:var(--error)}
tr.row>td:first-child{border-left:4px solid var(--tone)}
.verdict{font-weight:600}
article.flow{margin:18px 0;padding:12px 16px;border:1px solid var(--line);
border-left:6px solid var(--tone);border-radius:6px}
article.flow:target{display:block!important;outline:2px solid var(--accent);
outline-offset:2px}
blockquote{margin:6px 0;padding:4px 12px;border-left:3px solid var(--line);
background:var(--panel)}
.tag{display:inline-block;margin-right:6px;padding:0 6px;border:1px solid var(--line);
border-radius:4px;font-size:12px}
ol.struggles li{margin:6px 0}
table.metrics th{width:16em;font-weight:normal;color:var(--muted)}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
gap:10px;margin:8px 0}
.gallery figure{margin:0}
.gallery img{display:block;width:100%;height:auto;border:1px solid var(--line);
border-radius:4px;background:var(--panel)}
.gallery figcaption{font-size:12px;color:var(--muted)}
video{display:block;max-width:100%;max-height:70vh;border-radius:4px;background:#000}
details{margin:8px 0}
summary{cursor:pointer;font-weight:600}
pre{max-height:60vh;overflow:auto;padding:10px;border-radius:4px;white-space:pre-wrap;
overflow-wrap:anywhere;background:var(--panel)}
ul.jump>li{margin:6px 0}
footer{margin-top:40px;font-size:12px;color:var(--muted)}
@media print{.chips,nav.sections{display:none}}
"""


class ReportError(Exception):
    """The run directory cannot be shown; the message names a file, never its text."""


@dataclass(frozen=True)
class Screen:
    """One screenshot of an attempt, and the step and tool the journal saved it at."""

    name: str
    url: str
    step: int | None
    tool: str | None


@dataclass(frozen=True)
class Place:
    """Where a note was written: its journal line, the step count then, and the
    screen saved last before it."""

    seq: int
    step: int | None
    screen: str | None
    screen_url: str | None


@dataclass(frozen=True)
class AttemptView:
    """One attempt of a flow, with what its own folder holds."""

    result: story_verdict.FlowResult
    context: story_verdict.FlowContext | None
    journal: story_verdict.Journal | None
    screens: tuple[Screen, ...]
    unshown_screens: int
    files: tuple[tuple[str, str], ...]
    report_md: str | None
    report_md_cut: bool
    video_url: str | None
    hesitations: tuple[Place | None, ...]
    dead_ends: tuple[Place | None, ...]


@dataclass(frozen=True)
class FlowView:
    """One flow: its confirmation, the attempt that shows it, and every attempt."""

    key: str
    confirmation: story_verdict.Confirmation
    shown: story_verdict.FlowResult
    attempts: tuple[AttemptView, ...]
    findings: tuple[story_verdict.Finding, ...]

    @property
    def anchor(self) -> str:
        """The flow's section id; a key holds no ``--``, so the split is exact."""
        return "flow-" + self.key.replace("@", "--")

    @property
    def classes(self) -> tuple[str, ...]:
        """The classes the page's filters select the flow by."""
        return (
            f"v-{self.confirmation.verdict}",
            f"p-{self.shown.persona}",
            f"s-{self.shown.story}",
        )


@dataclass(frozen=True)
class RunView:
    """Everything the page shows, read from one run directory."""

    record: Mapping[str, Any]
    record_problem: str | None
    revision: object
    flows: tuple[FlowView, ...]
    found: tuple[story_verdict.Finding, ...]
    results: tuple[story_verdict.FlowResult, ...]


@dataclass(frozen=True)
class CatalogueText:
    """A catalogue and the sha256 of its bytes, for the intents it can vouch for."""

    catalogue: stories.Catalogue
    sha256: str


@dataclass(frozen=True)
class Option:
    """One choice of a filter: its radio's id, the class it shows, its label."""

    ident: str
    cls: str
    label: str
    count: int


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + section.ELLIPSIS


def _text(value: object, limit: int = _FIELD_CHARS) -> str:
    """One line of text, escaped; anything but a string shows as nothing."""
    if not isinstance(value, str):
        return ""
    return _escape(_cut(section.flatten(value), limit))


def _as_read(text: str) -> str:
    """A field ``section.sanitize`` made for a GitLab note, as the note reads: its
    zero-width spaces gone, a backslash escape as the character, ``&lt;`` as ``<``."""
    shown = _ESCAPE_RE.sub(r"\1", text.replace(section.ZERO_WIDTH_SPACE, ""))
    return shown.replace("&lt;", "<")


def _field(value: object, limit: int = _FIELD_CHARS) -> str:
    """A field the judge sanitised, as the note reads it, escaped."""
    if not isinstance(value, str):
        return ""
    return _text(_as_read(value), limit)


def _lines(value: str, limit: int) -> str:
    """Text of many lines, escaped, its control and bidi characters made visible."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    return _escape(_cut(_TEXT_CONTROL_RE.sub("\ufffd", text), limit))


def _attrs(pairs: Iterable[tuple[str, str | None]]) -> str:
    return "".join(
        f' {name}="{_escape(value)}"' for name, value in pairs if value is not None
    )


def _tag(name: str, inner: str, *pairs: tuple[str, str | None]) -> str:
    return f"<{name}{_attrs(pairs)}>{inner}</{name}>"


def _css_name(text: str) -> str:
    if not _CSS_NAME_RE.fullmatch(text):
        raise ReportError("a class or id the page needs is not of its closed shape")
    return text


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _usd(value: object) -> str:
    return f"${float(value):.2f}" if _is_number(value) else _DASH


def _count(value: object) -> str:
    return f"{value:,}" if _is_int(value) else _DASH


def _seconds(value: float | None) -> str:
    if value is None or not math.isfinite(value) or value < 0:
        return _DASH
    whole = int(round(value))
    if whole < 60:
        return f"{whole} s"
    minutes, rest = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def _when(epoch: object) -> datetime | None:
    if not _is_number(epoch) or not 0 < float(epoch) < _EPOCH_MAX:
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc)


def _spoken(identifier: str) -> str:
    return identifier.replace("-", " ").replace("_", " ")


def _yes(value: object) -> str:
    if value is True:
        return "yes"
    return "no" if value is False else _DASH


def _icon(verdict: str) -> str:
    key = story_verdict.VERDICT_ICON_KEYS.get(verdict, "not_covered")
    return section.OUTCOME_ICONS.get(key, section.OUTCOME_ICONS["not_covered"])


def _tone(verdict: str) -> str:
    """The colour family a verdict is drawn in, always beside its icon and words."""
    if verdict in story_verdict.BAD_VERDICTS:
        return "bad"
    if verdict in story_verdict.GOOD_VERDICTS:
        return "good"
    return "error" if verdict == "error" else "unknown"


def _badge(verdict: str, label: str) -> str:
    icon = _tag("span", _escape(_icon(verdict)), ("aria-hidden", "true"))
    return _tag(
        "span", f"{icon} {_escape(label)}", ("class", f"verdict tone-{_tone(verdict)}")
    )


def _flow_badge(flow: FlowView) -> str:
    label = story_verdict.verdict_label(flow.shown, flow.confirmation)
    return _badge(flow.confirmation.verdict, label)


def _attempt_badge(result: story_verdict.FlowResult) -> str:
    alone = story_verdict.Confirmation(
        result.verdict, "single", 1, 1, result.attempt, False
    )
    return _badge(result.verdict, story_verdict.verdict_label(result, alone))


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("a key appears twice in one object")
        found[key] = value
    return found


def _no_constant(name: str) -> Any:
    raise ValueError("JSON holds no NaN or Infinity")


def _why_not(error: OSError) -> str:
    if error.errno in (errno.ELOOP, errno.EMLINK):
        return "it is a symbolic link, which is never followed"
    if error.errno == errno.ENOENT:
        return "it does not exist"
    return error.strerror or type(error).__name__


def _read(path: Path, limit: int, what: str) -> bytes:
    """At most ``limit`` bytes of the regular file at ``path``, never through a link."""
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as failure:
        raise ReportError(f"{what} cannot be opened: {_why_not(failure)}") from None
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode):
            raise ReportError(f"{what} is not a regular file")
        if found.st_size > limit:
            raise ReportError(f"{what} is over its bound of {limit} bytes")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise ReportError(f"{what} is over its bound of {limit} bytes")
        return bytes(data)
    except OSError as failure:
        raise ReportError(f"{what} cannot be read: {_why_not(failure)}") from None
    finally:
        os.close(descriptor)


def _load_json(path: Path, what: str) -> Any:
    data = _read(path, _RECORD_MAX_BYTES, what)
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_keys,
            parse_constant=_no_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReportError(f"{what} is not valid JSON") from None


def confined(run_dir: Path, parts: Sequence[str], *, directory: bool = False) -> bool:
    """Whether ``run_dir/parts`` is a regular file, or with ``directory`` a
    directory, inside ``run_dir``, with every part a plain name and none a link.

    A plain name starts with a letter or a digit and holds only letters, digits,
    ``.``, ``_``, ``@`` and ``-``, so neither ``..``, nor an absolute path, nor a
    separator can be one.
    """
    if not parts or not all(
        isinstance(part, str) and _SEGMENT_RE.fullmatch(part) for part in parts
    ):
        return False
    current = run_dir
    for index, part in enumerate(parts):
        current = current / part
        try:
            found = os.lstat(current)
        except OSError:
            return False
        last = index == len(parts) - 1
        wanted = stat.S_ISDIR if directory or not last else stat.S_ISREG
        if stat.S_ISLNK(found.st_mode) or not wanted(found.st_mode):
            return False
    root = os.path.realpath(run_dir).rstrip(os.sep) + os.sep
    return os.path.realpath(current).startswith(root)


def link(run_dir: Path, parts: Sequence[str]) -> str | None:
    """The relative URL of a file :func:`confined` accepts, else None."""
    if not confined(run_dir, parts):
        return None
    return "/".join(urllib.parse.quote(part, safe="@") for part in parts)


def _attempt_folder(key: str, attempt: int) -> tuple[str, ...]:
    folder = (story_verdict.FLOWS_DIR, key)
    return folder if attempt == 1 else (*folder, f"attempt-{attempt}")


def _journal(run_dir: Path, folder: tuple[str, ...]) -> story_verdict.Journal | None:
    parts = (*folder, "journal.jsonl")
    if not confined(run_dir, parts):
        return None
    try:
        return story_verdict.read_journal(run_dir.joinpath(*parts))
    except story_verdict.JournalError:
        return None


def _screen_of(call: story_verdict.JournalCall) -> str | None:
    match = _JOURNAL_SCREEN_RE.fullmatch(call.screen or "")
    return None if match is None else match.group(1)


def _screen_names(run_dir: Path, folder: tuple[str, ...]) -> list[str]:
    parts = (*folder, _SCREENS_DIR)
    if not confined(run_dir, parts, directory=True):
        return []
    try:
        with os.scandir(run_dir.joinpath(*parts)) as entries:
            names = [
                entry.name
                for entry in entries
                if _SCREEN_NAME_RE.fullmatch(entry.name)
                and entry.is_file(follow_symlinks=False)
            ]
    except OSError:
        return []
    return sorted(names, key=lambda name: (len(name), name))[:_SCREENS_MAX]


def _screens(
    run_dir: Path, folder: tuple[str, ...], journal: story_verdict.Journal | None
) -> tuple[tuple[Screen, ...], int]:
    """The attempt's screenshots in order, and how many more the journal names
    than the folder holds."""
    taken: dict[str, tuple[int | None, str]] = {}
    for call in () if journal is None else journal.calls:
        name = _screen_of(call)
        if name is not None and name not in taken:
            taken[name] = (call.actions_used, call.tool)
    shown: list[Screen] = []
    for name in _screen_names(run_dir, folder):
        url = link(run_dir, (*folder, _SCREENS_DIR, name))
        if url is not None:
            step, tool = taken.get(name, (None, None))
            shown.append(Screen(name, url, step, tool))
    held = {screen.name for screen in shown}
    return tuple(shown), sum(1 for name in taken if name not in held)


def _places(
    run_dir: Path,
    folder: tuple[str, ...],
    journal: story_verdict.Journal | None,
    tool: str,
    notes: int,
) -> tuple[Place | None, ...]:
    """Where each of an attempt's ``notes`` notes made with ``tool`` was written.

    The judge keeps an attempt's accepted notes in journal order, up to
    :data:`_NOTES_PER_FLOW`, so the n-th note in ``results.json`` is the journal's
    n-th. When the counts do not agree, no place is claimed for any of them.
    """
    if journal is None:
        return (None,) * notes
    places: list[Place] = []
    screen: str | None = None
    for call in journal.calls:
        screen = _screen_of(call) or screen
        if call.cls == "note" and call.tool == tool and call.ok:
            url = (
                None
                if screen is None
                else link(run_dir, (*folder, _SCREENS_DIR, screen))
            )
            places.append(Place(call.seq, call.actions_used, screen, url))
    if len(places) < notes or (len(places) > notes and notes < _NOTES_PER_FLOW):
        return (None,) * notes
    return tuple(places[:notes])


def _report_md(run_dir: Path, folder: tuple[str, ...]) -> tuple[str | None, bool]:
    parts = (*folder, "report.md")
    if not confined(run_dir, parts):
        return None, False
    try:
        data = _read(run_dir.joinpath(*parts), _REPORT_MD_MAX_BYTES, "report.md")
    except ReportError:
        return None, False
    text = data.decode("utf-8", errors="replace")
    return text, len(text) > _REPORT_MD_CHARS


def _video_url(run_dir: Path, name: str | None) -> str | None:
    if not isinstance(name, str) or not _VIDEO_NAME_RE.fullmatch(name):
        return None
    return link(run_dir, (name,))


def _attempt_view(
    run_dir: Path,
    result: story_verdict.FlowResult,
    context: story_verdict.FlowContext | None,
) -> AttemptView:
    folder = _attempt_folder(result.key, result.attempt)
    journal = _journal(run_dir, folder)
    screens, unshown = _screens(run_dir, folder, journal)
    files = []
    for name in ATTEMPT_FILES:
        url = link(run_dir, (*folder, name))
        if url is not None:
            files.append((name, url))
    report_md, cut = _report_md(run_dir, folder)
    notes = result.notes
    return AttemptView(
        result=result,
        context=context,
        journal=journal,
        screens=screens,
        unshown_screens=unshown,
        files=tuple(files),
        report_md=report_md,
        report_md_cut=cut,
        video_url=_video_url(run_dir, result.video),
        hesitations=_places(
            run_dir, folder, journal, "note_confusion", len(notes.confusion)
        ),
        dead_ends=_places(
            run_dir, folder, journal, "note_dead_end", len(notes.dead_end)
        ),
    )


def _results(
    run_dir: Path,
) -> tuple[list[story_verdict.FlowResult], list[story_verdict.Finding], object]:
    document = _load_json(run_dir / story_verdict.RESULTS_FILE, "results.json")
    if (
        not isinstance(document, Mapping)
        or document.get("schema") != story_verdict.RESULTS_SCHEMA
        or document.get("version") != story_verdict.RESULTS_VERSION
    ):
        raise ReportError(
            f"results.json is not {story_verdict.RESULTS_SCHEMA} "
            f"v{story_verdict.RESULTS_VERSION}"
        )
    flows, findings = document.get("flows"), document.get("findings")
    if not isinstance(flows, list) or not isinstance(findings, list):
        raise ReportError("results.json holds no list of flows and of findings")
    try:
        results = [story_verdict.FlowResult.from_json(item) for item in flows]
        found = [story_verdict.Finding.from_json(item) for item in findings]
    except story_verdict.RunRecordError as error:
        raise ReportError(f"results.json: {error}") from None
    return results, found, document.get("judgement_revision")


def _record(
    run_dir: Path,
) -> tuple[Mapping[str, Any], dict[tuple[str, int], Any], str | None]:
    """``story-run.json`` and its flows by key and attempt, and why not, if not."""
    try:
        record = _load_json(run_dir / story_verdict.RUN_FILE, "story-run.json")
    except ReportError as error:
        return {}, {}, str(error)
    if not isinstance(record, Mapping):
        return {}, {}, "story-run.json is not a JSON object"
    try:
        contexts = story_verdict.run_record(record)
    except story_verdict.RunRecordError as error:
        return record, {}, str(error)
    return record, {(item.key, item.attempt): item for item in contexts}, None


def read_run(run_dir: Path) -> RunView:
    """The run directory as the page shows it.

    ReportError when ``results.json`` cannot be read as v1, the one file the page
    cannot do without; every other file may be missing.
    """
    results, found, revision = _results(run_dir)
    record, contexts, problem = _record(run_dir)
    try:
        confirmed = story_verdict.flow_confirmations(results)
    except ValueError:
        raise ReportError(
            "results.json holds a flow of more than two attempts, or two of one number"
        ) from None
    flows: list[FlowView] = []
    for key, confirmation, shown in confirmed:
        attempts = sorted(
            (result for result in results if result.key == key),
            key=lambda result: result.attempt,
        )
        flows.append(
            FlowView(
                key=key,
                confirmation=confirmation,
                shown=shown,
                attempts=tuple(
                    _attempt_view(run_dir, result, contexts.get((key, result.attempt)))
                    for result in attempts
                ),
                findings=tuple(item for item in found if key in item.scope),
            )
        )
    order = story_verdict.VERDICT_ORDER
    flows.sort(key=lambda flow: (order.index(flow.confirmation.verdict), flow.key))
    return RunView(
        record=record,
        record_problem=problem,
        revision=revision,
        flows=tuple(flows),
        found=tuple(found),
        results=tuple(results),
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _attempt_seconds(attempt: AttemptView) -> float | None:
    session = attempt.context.session if attempt.context is not None else None
    if session is not None:
        span = session.ended_epoch - session.started_epoch
        if math.isfinite(span) and span >= 0:
            return span
    claude = attempt.result.claude
    if claude is not None and claude.duration_s is not None:
        return float(claude.duration_s)
    return None


def _flow_seconds(flow: FlowView) -> float | None:
    spans = [_attempt_seconds(attempt) for attempt in flow.attempts]
    known = [span for span in spans if span is not None]
    return sum(known) if known else None


def _flow_cost(flow: FlowView) -> float | None:
    costs = [
        attempt.result.claude.counted_usd
        for attempt in flow.attempts
        if attempt.result.claude is not None
    ]
    return round(sum(costs), 6) if costs else None


def _run_span(run: RunView) -> str:
    epochs: list[object] = []
    for flow in run.flows:
        for attempt in flow.attempts:
            session = attempt.context.session if attempt.context else None
            if session is not None:
                epochs += [session.started_epoch, session.ended_epoch]
            elif attempt.journal is not None:
                epochs.append(attempt.journal.start.epoch)
    watched = _mapping(run.record.get("interactive"))
    epochs += [watched.get("started_epoch"), watched.get("ended_epoch")]
    moments = [moment for moment in map(_when, epochs) if moment is not None]
    if not moments:
        return "not recorded"
    first, last = min(moments), max(moments)
    end = "%H:%M" if first.date() == last.date() else "%Y-%m-%d %H:%M"
    return f"{first.strftime('%Y-%m-%d %H:%M')} to {last.strftime(end)} UTC"


def _code(value: object, limit: int = _NAME_CHARS) -> str:
    shown = _text(value, limit)
    return f"<code>{shown}</code>" if shown else ""


def _stack_line(record: Mapping[str, Any]) -> str:
    stack = _mapping(record.get("stack"))
    parts = [_code(stack.get("name")) or "not recorded"]
    for key in ("version", "build"):
        value = _code(stack.get(key))
        if value:
            parts.append(f"{key} {value}")
    review = stack.get("review_stack")
    if review is True:
        parts.append("the review stack")
    elif review is False:
        parts.append("not confirmed as the review stack")
    state = _code(record.get("stack_state"))
    if state:
        parts.append(f"state {state}")
    if record.get("stack_changed_mid_run") is True:
        parts.append("its build changed during the run")
    return ", ".join(parts)


def _docs_line(record: Mapping[str, Any]) -> str:
    docs = _mapping(record.get("docs"))
    if not docs:
        return "not recorded"
    ref = _code(docs.get("ref"), 40)
    if ref:
        where = f"git at {ref}"
    elif docs.get("dirty") is True:
        where = "the working tree, with uncommitted changes"
    else:
        where = "the working tree"
    corpus = _code(docs.get("corpus_hash"), 64)
    return where + (f", corpus {corpus}" if corpus else "")


def _ui_line(record: Mapping[str, Any]) -> str:
    source = _mapping(record.get("ui_source"))
    if source.get("kind") == "build":
        tree = _code(source.get("src_ui_tree"), 40)
        return "a UI build" + (f" of {tree}" if tree else "")
    if source.get("kind") == "deployed":
        return "the stack's own deployed UI"
    return "not recorded"


def _mode_line(record: Mapping[str, Any]) -> str:
    mode = _text(record.get("mode"), 40) or "not recorded"
    shard = _mapping(record.get("shard"))
    if _is_int(shard.get("index")) and _is_int(shard.get("total")):
        mode += f", shard {shard['index']} of {shard['total']}"
    replayed = _code(record.get("replayed"), 200)
    if replayed:
        mode += f", a replay of {replayed}"
    confinement = record.get("confinement")
    if isinstance(confinement, str) and confinement != "full":
        mode += f", confinement {_code(confinement, 40)}"
    return mode


def _harness_line(record: Mapping[str, Any], revision: object) -> str:
    line = _code(record.get("harness_sha"), 48) or "not recorded"
    if record.get("harness_dirty") is True:
        line += " with uncommitted changes"
    if _is_int(revision):
        line += f"; judged under revision {revision}"
        if revision != story_verdict.JUDGEMENT_REVISION:
            line += (
                f", which this checkout is not: it judges under "
                f"{story_verdict.JUDGEMENT_REVISION}"
            )
    return line


def _verdict_counts(run: RunView) -> list[tuple[str, int]]:
    counts = dict.fromkeys(story_verdict.VERDICT_ORDER, 0)
    for flow in run.flows:
        counts[flow.confirmation.verdict] += 1
    return [(verdict, count) for verdict, count in counts.items() if count]


def _cost_totals(run: RunView) -> dict[str, Any]:
    return story_verdict.cost_totals(run.results, run.record.get("interactive"))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _header(run: RunView) -> str:
    record = run.record
    attempts = sum(len(flow.attempts) for flow in run.flows)
    verdicts = " · ".join(
        f"{_escape(_icon(verdict))} "
        f"{_escape(story_verdict.VERDICT_LABELS[verdict])}: {count}"
        for verdict, count in _verdict_counts(run)
    )
    findings: dict[str, int] = {}
    for item in run.found:
        name = f"{item.category}/{item.severity}"
        findings[name] = findings.get(name, 0) + 1
    model = _code(record.get("model"))
    effort = _text(record.get("effort"), 40)
    facts = [
        ("When", _run_span(run)),
        ("Stack", _stack_line(record)),
        (
            "Model",
            (model + (f", effort {effort}" if effort else "")) or "not recorded",
        ),
        ("Mode", _mode_line(record)),
        ("Docs", _docs_line(record)),
        ("UI", _ui_line(record)),
        ("Harness", _harness_line(record, run.revision)),
        (
            "Flows",
            f"{_plural(len(run.flows), 'flow')}, {_plural(attempts, 'attempt')}",
        ),
        ("Verdicts", verdicts or "none"),
        (
            "Findings",
            ", ".join(f"{_escape(name)}: {count}" for name, count in findings.items())
            or "none",
        ),
        ("Cost", _escape(story_verdict.cost_text(_cost_totals(run)))),
    ]
    if run.record_problem is not None:
        facts.append(("Run record", _escape(run.record_problem)))
    rows = "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in facts)
    links = " ".join(
        _tag("a", _escape(words), ("href", f"#{anchor}")) for anchor, words in _SECTIONS
    )
    return (
        "<header><h1>First-time-user stories</h1>"
        f'<dl class="facts">{rows}</dl>'
        f'<nav class="sections" aria-label="Sections">{links}</nav></header>'
    )


def _by_story_order(run: RunView) -> list[tuple[str, list[FlowView]]]:
    """The run's flows grouped by story, the stories in the order of their titles."""
    grouped: dict[str, list[FlowView]] = {}
    for flow in run.flows:
        grouped.setdefault(flow.shown.story, []).append(flow)
    return sorted(
        grouped.items(), key=lambda item: (_as_read(item[1][0].shown.title), item[0])
    )


def _filters(run: RunView) -> list[tuple[str, str, list[Option]]]:
    """The page's three filters, each a radio name, a legend and its options; the
    first option of each shows everything and is checked."""
    total = len(run.flows)
    verdicts = [
        Option(
            f"fv-{verdict}",
            f"v-{verdict}",
            f"{_escape(_icon(verdict))} "
            f"{_escape(story_verdict.VERDICT_LABELS[verdict])}",
            count,
        )
        for verdict, count in _verdict_counts(run)
    ]
    by_persona: dict[str, list[FlowView]] = {}
    for flow in run.flows:
        by_persona.setdefault(flow.shown.persona, []).append(flow)
    personas = [
        Option(
            f"fp-{name}",
            f"p-{name}",
            f"{_escape(_spoken(name))} <small>{_escape(flows[0].shown.group)}</small>",
            len(flows),
        )
        for name, flows in sorted(by_persona.items())
    ]
    titles = [
        Option(f"fs-{name}", f"s-{name}", _title(flows[0].shown), len(flows))
        for name, flows in _by_story_order(run)
    ]
    return [
        ("verdict", "Verdict", [Option(f"fv-{_ALL}", "", "All", total), *verdicts]),
        ("persona", "Persona", [Option(f"fp-{_ALL}", "", "All", total), *personas]),
        ("story", "Story", [Option(f"fs-{_ALL}", "", "All", total), *titles]),
    ]


def _radios(groups: Sequence[tuple[str, str, list[Option]]]) -> str:
    return "".join(
        "<input"
        + _attrs(
            (
                ("type", "radio"),
                ("class", "filter"),
                ("name", name),
                ("id", _css_name(option.ident)),
                ("checked", "checked" if index == 0 else None),
            )
        )
        + ">"
        for name, _, options in groups
        for index, option in enumerate(options)
    )


def _chips(groups: Sequence[tuple[str, str, list[Option]]]) -> str:
    rows = []
    for _, legend, options in groups:
        labels = "".join(
            _tag("label", f"{option.label} ({option.count})", ("for", option.ident))
            for option in options
        )
        rows.append(
            f'<div class="chips" role="group" aria-label="{_escape(legend)}">'
            f'<span class="name">{_escape(legend)}</span>{labels}</div>'
        )
    return (
        '<section id="filters" aria-label="Filters"><h2>Show only</h2>'
        + "".join(rows)
        + "</section>"
    )


def _style(groups: Sequence[tuple[str, str, list[Option]]]) -> str:
    """The page's stylesheet: the fixed rules, then each filter option's own."""
    rules = []
    for _, _, options in groups:
        for option in options:
            ident = _css_name(option.ident)
            rules.append(
                f"#{ident}:checked~main label[for={ident}]"
                "{background:var(--fg);color:var(--bg);border-color:var(--fg)}"
            )
            rules.append(
                f"#{ident}:focus-visible~main label[for={ident}]"
                "{outline:2px solid var(--accent)}"
            )
            if option.cls:
                rules.append(
                    f"#{ident}:checked~main .f:not(.{_css_name(option.cls)})"
                    "{display:none}"
                )
    text = _STYLE + "\n".join(rules) + "\n"
    if "<" in text:
        raise ReportError("the stylesheet would hold a tag")
    return text


def _persona_cell(result: story_verdict.FlowResult) -> str:
    return (
        f"{_escape(_spoken(result.persona))}<br><small>{_escape(result.group)}</small>"
    )


def _steps(result: story_verdict.FlowResult) -> str:
    if _is_int(result.actions_used) and _is_int(result.budget):
        return f"{result.actions_used} of {result.budget}"
    return _DASH


def _documented(result: story_verdict.FlowResult) -> str:
    shown = str(result.documented_steps)
    if _is_number(result.overrun):
        shown += f" (×{float(result.overrun):.1f})"
    return shown


def _video_cell(flow: FlowView) -> str:
    if any(attempt.video_url for attempt in flow.attempts):
        return "yes"
    if any(attempt.result.video for attempt in flow.attempts):
        return "not here"
    return _DASH


def _title(result: story_verdict.FlowResult) -> str:
    return _field(result.title, _NAME_CHARS) or _escape(result.story)


def _index(run: RunView) -> str:
    if not run.flows:
        return (
            '<section id="index"><h2>Flows, worst first</h2><p>No flow.</p></section>'
        )
    heads = (
        ("Story", ""),
        ("As", ""),
        ("Expected", ""),
        ("Verdict", ""),
        ("Met", ""),
        ("Steps", "n"),
        ("Documented (overrun)", "n"),
        ("Hesitations", "n"),
        ("Dead ends", "n"),
        ("Findings", "n"),
        ("Cost, all attempts", "n"),
        ("Time, all attempts", "n"),
        ("Video", ""),
    )
    head = "".join(
        _tag("th", _escape(name), ("scope", "col"), ("class", cls or None))
        for name, cls in heads
    )
    rows = []
    for flow in run.flows:
        shown = flow.shown
        cells = (
            (_tag("a", _title(shown), ("href", f"#{flow.anchor}")), ""),
            (_persona_cell(shown), ""),
            (_escape(_EXPECTATION_WORDS[shown.expectation]), ""),
            (_flow_badge(flow), ""),
            (_yes(story_verdict.VERDICT_MEETS[flow.confirmation.verdict]), ""),
            (_steps(shown), "n"),
            (_documented(shown), "n"),
            (str(len(shown.notes.confusion)), "n"),
            (str(len(shown.notes.dead_end)), "n"),
            (str(len(shown.findings)), "n"),
            (_usd(_flow_cost(flow)), "n"),
            (_seconds(_flow_seconds(flow)), "n"),
            (_video_cell(flow), ""),
        )
        row = "".join(_tag("td", value, ("class", cls or None)) for value, cls in cells)
        classes = (
            "row",
            "f",
            f"tone-{_tone(flow.confirmation.verdict)}",
            *flow.classes,
        )
        rows.append(_tag("tr", row, ("class", " ".join(classes))))
    return (
        '<section id="index"><h2>Flows, worst first</h2>'
        f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody>"
        "</table></section>"
    )


def _finding_line(item: story_verdict.Finding, flows: Mapping[str, FlowView]) -> str:
    scope = ", ".join(
        _tag("a", _escape(key), ("href", f"#{flows[key].anchor}"))
        if key in flows
        else _escape(key)
        for key in item.scope
    )
    state = f" ({_escape(item.confirmation)})" if item.confirmation else ""
    return (
        f'<li><span class="tag">{_escape(item.severity)} · '
        f"{_escape(item.category)}</span><code>{_escape(item.id)}</code> "
        f"<strong>{_field(item.title)}</strong>{state}<br>{_field(item.body)}"
        f"<br><small>{scope}</small></li>"
    )


def _findings(run: RunView, flows: Mapping[str, FlowView]) -> str:
    if not run.found:
        body = "<p>None.</p>"
    else:
        body = "<ul>" + "".join(_finding_line(i, flows) for i in run.found) + "</ul>"
    return f'<section id="findings"><h2>Findings</h2>{body}</section>'


def _by_story(run: RunView) -> str:
    items = []
    for _, flows in _by_story_order(run):
        entries = "".join(
            _tag(
                "li",
                _tag(
                    "a",
                    f"{_escape(_spoken(flow.shown.persona))}: {_flow_badge(flow)}",
                    ("href", f"#{flow.anchor}"),
                ),
                ("class", " ".join(("f", *flow.classes))),
            )
            for flow in sorted(flows, key=lambda flow: flow.shown.persona)
        )
        classes = sorted({name for flow in flows for name in flow.classes})
        items.append(
            _tag(
                "li",
                f"<strong>{_title(flows[0].shown)}</strong><ul>{entries}</ul>",
                ("class", " ".join(("f", *classes))),
            )
        )
    body = f'<ul class="jump">{"".join(items)}</ul>' if items else "<p>No flow.</p>"
    return f'<section id="by-story"><h2>By story</h2>{body}</section>'


def _story_text(
    flow: FlowView, known: CatalogueText | None, record: Mapping[str, Any]
) -> tuple[str | None, str | None, str]:
    """The intent and the persona's background the catalogue gives, and a note.

    They are shown only when this catalogue is the run's own, by the sha256 the
    run recorded, or the story's hash is the one a journal of the flow started
    with; otherwise the run was asked something else, and the note says so.
    """
    if known is None:
        return None, None, "The catalogue could not be read, so no intent is shown."
    catalogue = known.catalogue
    try:
        story = catalogue.story(flow.shown.story)
        persona = catalogue.persona(flow.shown.persona)
    except KeyError:
        return None, None, "This checkout's catalogue holds no such story or persona."
    same = known.sha256 == record.get("catalogue_sha256")
    if not same:
        digest = stories.story_hash(catalogue, story)
        same = any(
            attempt.journal is not None and attempt.journal.start.story_hash == digest
            for attempt in flow.attempts
        )
    if not same:
        return (
            None,
            None,
            "This checkout's catalogue words the story differently from the run's, "
            "so its intent is not shown.",
        )
    return story.intent, persona.background, ""


def _outcome(flow: FlowView) -> str:
    """How the shown attempt ended, in words made from closed-set values alone."""
    result, verdict = flow.shown, flow.confirmation.verdict
    terminal = result.terminal
    steps = (
        f" in {result.actions_used} of {result.budget} steps"
        if _is_int(result.actions_used) and _is_int(result.budget)
        else ""
    )
    kind = _BLOCKED_WORDS.get(terminal.kind or "", "") if terminal else ""
    if verdict == "done":
        text = "Reported done" + steps
        if "stopped_at_commit_point" in result.flags:
            text += ", stopping before it saved anything"
        if "done_after_budget" in result.flags:
            text += ", after the step budget had run out"
    elif verdict == "refused_as_expected":
        text = "Reported blocked, as the docs say this role is" + steps
    elif verdict == "stuck":
        text = (
            f"Reported blocked ({kind or 'no kind given'}){steps}, where the docs "
            "say this role can do it"
        )
    elif verdict == "not_refused":
        text = "Reported done although the docs say this role cannot do it" + steps
    elif verdict == "offered" and "unproven" in result.flags:
        text = (
            "Reported done at a control the docs say this role lacks; nothing was "
            "pressed, so whether it is refused was not tested"
        )
    elif verdict == "offered":
        text = (
            "Reported blocked, and that the screen offered a control the docs say "
            "this role lacks"
        )
    elif verdict == "out_of_steps":
        text = "Reported blocked once the step budget had run out" + steps
    elif verdict == "gave_up":
        text = "Ended without a report: " + _GAVE_UP_WORDS.get(
            result.sub_kind or "", "it stopped"
        )
    elif verdict == "error":
        cause = _spoken(result.sub_kind or "unknown")
        text = f"A harness error ({cause}), which says nothing about the UI"
    elif verdict == "not_run":
        text = "Not run: " + _NOT_RUN_WORDS.get(result.sub_kind or "", "never started")
    else:
        text = "Ended with no report, for no reason the journal records"
    if flow.confirmation.extended_budget:
        tail = "Judged from its re-run at twice the step budget."
    else:
        tail = _STATE_WORDS[flow.confirmation.state]
    return f"{_escape(text)}. {_escape(tail)}"


def _place(place: Place | None) -> str:
    if place is None:
        return ""
    bits = []
    if place.step is not None:
        bits.append(f"step {place.step}")
    if place.screen is not None:
        name = _escape(place.screen.removesuffix(".jpg"))
        bits.append(
            _tag("a", f"screen {name}", ("href", place.screen_url))
            if place.screen_url
            else f"screen {name}, not in this directory"
        )
    return f'<span class="muted">{" · ".join(bits)}</span> ' if bits else ""


def _struggles(attempt: AttemptView) -> str:
    notes = attempt.result.notes
    entries: list[tuple[int, int, str]] = []
    for index, (note, place) in enumerate(zip(notes.confusion, attempt.hesitations)):
        docs = (
            f"<br>Docs: <code>{_field(note.docs_ref)}</code>" if note.docs_ref else ""
        )
        line = (
            f'<span class="tag">Hesitation</span>{_place(place)}'
            f"<strong>{_field(note.what)}</strong><br>Why: {_field(note.why)}{docs}"
        )
        entries.append((place.seq if place else _LAST, index, line))
    for index, (note, place) in enumerate(zip(notes.dead_end, attempt.dead_ends)):
        line = (
            f'<span class="tag">Dead end</span>{_place(place)}'
            f"<strong>{_field(note.what)}</strong><br>Why: {_field(note.why)}"
        )
        entries.append((place.seq if place else _LAST, _LAST + index, line))
    if not entries:
        return "<p>None noted.</p>"
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    items = "".join(f"<li>{line}</li>" for _, _, line in entries)
    return f'<ol class="struggles">{items}</ol>'


def _ending(result: story_verdict.FlowResult) -> str:
    terminal = result.terminal
    if terminal is None:
        stop = _code(result.claude.stop if result.claude else None, 40)
        return (
            "<p>No report: the session ended without report_done or report_blocked"
            + (f" (stop {stop})" if stop else "")
            + ".</p>"
        )
    if terminal.type == "done":
        facts = [
            "report_done",
            f"stopped at a commit point: {_yes(terminal.stopped_at_commit_point)}",
        ]
        quote = terminal.evidence
    else:
        kind = _BLOCKED_WORDS.get(terminal.kind or "", "not given")
        facts = [
            "report_blocked",
            f"kind: {_escape(kind)}",
            f"offered a control: {_yes(terminal.offered_a_control)}",
        ]
        quote = terminal.reason
    facts.append(f"after the step budget: {_yes(terminal.after_budget)}")
    shown = _field(quote)
    return f"<p>{' · '.join(facts)}</p>" + (
        f"<blockquote>{shown}</blockquote>" if shown else ""
    )


def _usage(usage: object) -> str:
    counts = _mapping(usage)
    if not counts:
        return "not reported"
    return " · ".join(
        f"{word} {_count(counts.get(key))}"
        for key, word in _USAGE_WORDS
        if key in counts
    )


def _claude(attempt: AttemptView, name: str) -> object:
    claude = attempt.result.claude
    return None if claude is None else getattr(claude, name)


def _refusals(attempt: AttemptView) -> str:
    counts = sorted(attempt.result.refusals_by_origin.items())
    return ", ".join(f"{_escape(k)}: {_count(v)}" for k, v in counts) or "none"


def _metrics(flow: FlowView) -> str:
    attempts = flow.attempts
    head = "".join(
        _tag(
            "th",
            f"Attempt {a.result.attempt}"
            + (
                f", budget ×{a.result.budget_scale}"
                if a.result.budget_scale > 1
                else ""
            ),
            ("scope", "col"),
        )
        for a in attempts
    )
    rows = (
        ("Verdict of this attempt", [_attempt_badge(a.result) for a in attempts]),
        ("Steps used", [_steps(a.result) for a in attempts]),
        ("Documented steps (overrun)", [_documented(a.result) for a in attempts]),
        ("Looks", [_count(a.result.looks) for a in attempts]),
        (
            "Docs searched, read",
            [f"{a.result.searches}, {a.result.reads}" for a in attempts],
        ),
        ("Declared docs found", [_yes(a.result.declared_docs_found) for a in attempts]),
        (
            "Doc addresses guessed",
            [_count(a.result.doc_addresses_guessed) for a in attempts],
        ),
        (
            "Hesitations, dead ends",
            [
                f"{len(a.result.notes.confusion)}, {len(a.result.notes.dead_end)}"
                for a in attempts
            ],
        ),
        (
            "Injection attempts noted",
            [_count(a.result.notes.injections) for a in attempts],
        ),
        ("Socket audit", [_escape(a.result.audit) for a in attempts]),
        ("Proxy refusals by origin", [_refusals(a) for a in attempts]),
        (
            "Turns, stop",
            [
                f"{_count(_claude(a, 'turns'))}, "
                f"{_text(_claude(a, 'stop'), 40) or _DASH}"
                for a in attempts
            ],
        ),
        ("Time", [_seconds(_attempt_seconds(a)) for a in attempts]),
        ("Cost counted", [_usd(_claude(a, "counted_usd")) for a in attempts]),
        (
            "Cost at the pinned rates",
            [_usd(_claude(a, "cost_usd_pinned")) for a in attempts],
        ),
        ("Cost the CLI reported", [_usd(_claude(a, "cost_usd")) for a in attempts]),
        ("Tokens", [_usage(_claude(a, "usage")) for a in attempts]),
    )
    body = "".join(
        f'<tr><th scope="row">{_escape(name)}</th>'
        + "".join(f"<td>{value}</td>" for value in values)
        + "</tr>"
        for name, values in rows
    )
    return (
        f'<table class="metrics"><thead><tr><td></td>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _docs_vs_screen(attempt: AttemptView) -> str:
    result = attempt.result
    bits = []
    if result.help_links_unresolved:
        items = "".join(
            f"<li><code>{_field(address)}</code></li>"
            for address in result.help_links_unresolved
        )
        bits.append(
            f"<p>Help links on screen that lead to no page:</p><ul>{items}</ul>"
        )
    if result.commit_points:
        items = "".join(f"<li>{_field(label)}</li>" for label in result.commit_points)
        bits.append(
            "<p>Controls the commit guard refused, as their labels read:</p>"
            f"<ul>{items}</ul>"
        )
    return "".join(bits)


def _video(attempt: AttemptView) -> str:
    result = attempt.result
    if result.video is None:
        return ""
    if attempt.video_url is None:
        return (
            f"<p>The results name a video, {_code(result.video)}, that is not in "
            "this directory.</p>"
        )
    chapters = "".join(
        f'<li><span class="muted">{_field(at, 20)}</span> {_field(label, 200)}</li>'
        for at, label in result.chapters
    )
    player = (
        "<video"
        + _attrs(
            (("controls", "controls"), ("preload", "none"), ("src", attempt.video_url))
        )
        + "></video>"
    )
    caption = _tag(
        "a", f"Attempt {result.attempt}: the video file", ("href", attempt.video_url)
    )
    return f"<figure>{player}<figcaption>{caption}</figcaption></figure>" + (
        f'<ol class="chapters">{chapters}</ol>' if chapters else ""
    )


def _gallery(attempt: AttemptView, label: str) -> str:
    unshown = attempt.unshown_screens
    missing = (
        f"<p>The journal names {_plural(unshown, 'more screen')} than this directory "
        "holds.</p>"
        if unshown
        else ""
    )
    if not attempt.screens:
        return missing or f"<p>{_escape(label)}: no screens in this directory.</p>"
    figures = []
    for screen in attempt.screens:
        number = screen.name.removesuffix(".jpg")
        caption = f"Screen {_escape(number)}"
        if screen.step is not None:
            caption += f" · step {screen.step}"
        if screen.tool:
            caption += f" · {_text(screen.tool, 60)}"
        image = (
            "<img"
            + _attrs(
                (
                    ("src", screen.url),
                    ("loading", "lazy"),
                    ("decoding", "async"),
                    ("alt", f"Screen {number}"),
                )
            )
            + ">"
        )
        figures.append(
            f"<figure>{_tag('a', image, ('href', screen.url))}"
            f"<figcaption>{caption}</figcaption></figure>"
        )
    summary = f"{_escape(label)}: {_plural(len(attempt.screens), 'screen')}"
    return (
        f"<details><summary>{summary}</summary>"
        f'<div class="gallery">{"".join(figures)}</div></details>{missing}'
    )


def _model_report(attempt: AttemptView, label: str) -> str:
    if attempt.report_md is None:
        return ""
    cut = (
        "<p>Cut here; the whole of it is in report.md.</p>"
        if attempt.report_md_cut
        else ""
    )
    return (
        f"<details><summary>{_escape(label)}: the model's own account, which decides "
        f"nothing</summary><pre>{_lines(attempt.report_md, _REPORT_MD_CHARS)}</pre>"
        f"{cut}</details>"
    )


def _files(attempt: AttemptView, label: str) -> str:
    if not attempt.files:
        return f"<p>{_escape(label)}: no file of its own in this directory.</p>"
    links = " · ".join(
        _tag("a", _escape(name), ("href", url)) for name, url in attempt.files
    )
    return f"<p>{_escape(label)}, its files: {links}</p>"


def _story_facts(
    flow: FlowView, known: CatalogueText | None, record: Mapping[str, Any]
) -> str:
    shown = flow.shown
    intent, background, note = _story_text(flow, known, record)
    persona = (
        f"<code>{_escape(shown.persona)}</code>, Cognito group {_escape(shown.group)}"
    )
    if background:
        persona += f": {_text(background)}"
    expectation = shown.expectation
    facts = (
        ("Story", f"<code>{_escape(shown.story)}</code>"),
        ("Intent", _text(intent) if intent else _escape(note)),
        ("Persona", persona),
        (
            "Expectation",
            f"{_escape(_EXPECTATION_WORDS[expectation])}: "
            f"{_escape(_EXPECTATION_MEANING[expectation])}; "
            f"{_escape(_KIND_WORDS[shown.kind])}",
        ),
        (
            "Verdict",
            f"{_flow_badge(flow)}; meets the expectation: "
            f"{_yes(story_verdict.VERDICT_MEETS[flow.confirmation.verdict])}",
        ),
    )
    rows = "".join(f"<dt>{name}</dt><dd>{value}</dd>" for name, value in facts)
    return f'<dl class="facts">{rows}</dl>'


def _flow_section(
    flow: FlowView,
    known: CatalogueText | None,
    record: Mapping[str, Any],
    flows: Mapping[str, FlowView],
) -> str:
    shown = flow.shown
    parts = [
        f"<h3>{_title(shown)} <small>as {_escape(_spoken(shown.persona))}</small></h3>",
        _story_facts(flow, known, record),
        f'<p class="outcome">{_outcome(flow)}</p>',
        "<h4>Attempts</h4>",
        _metrics(flow),
    ]
    several = len(flow.attempts) > 1
    for attempt in flow.attempts:
        which = f", attempt {attempt.result.attempt}" if several else ""
        parts += [
            f"<h4>How it ended{_escape(which)}</h4>",
            _ending(attempt.result),
            f"<h4>Where a first-time user struggled{_escape(which)}</h4>",
            _struggles(attempt),
            _docs_vs_screen(attempt),
        ]
    if flow.findings:
        items = "".join(_finding_line(item, flows) for item in flow.findings)
        parts.append(f"<h4>Findings</h4><ul>{items}</ul>")
    videos = "".join(_video(attempt) for attempt in flow.attempts)
    if videos:
        parts.append(f"<h4>Video</h4>{videos}")
    parts.append("<h4>Screens and files</h4>")
    for attempt in flow.attempts:
        label = f"Attempt {attempt.result.attempt}"
        parts += [
            _gallery(attempt, label),
            _model_report(attempt, label),
            _files(attempt, label),
        ]
    parts.append('<p><a href="#index">Back to the index</a></p>')
    classes = ("flow", "f", f"tone-{_tone(flow.confirmation.verdict)}", *flow.classes)
    return _tag(
        "article", "".join(parts), ("class", " ".join(classes)), ("id", flow.anchor)
    )


def _rows(pairs: Iterable[tuple[str, str]]) -> str:
    return "".join(
        f'<tr><th scope="row">{_escape(name)}</th><td>{value}</td></tr>'
        for name, value in pairs
    )


def _setup_tables(record: Mapping[str, Any]) -> str:
    versions = _mapping(record.get("versions"))
    limits = _mapping(record.get("limits"))
    scope = _mapping(record.get("credential_scope"))
    tables = (
        (
            "Toolchain",
            [
                (word, _code(versions.get(key), 80))
                for key, word in _VERSION_KEYS
                if isinstance(versions.get(key), str)
            ],
        ),
        (
            "Limits",
            [
                (word, _escape(str(limits[key])))
                for key, word in _LIMIT_KEYS
                if _is_number(limits.get(key))
            ],
        ),
        (
            "Credentials",
            [
                (f"{name} credentials", _text(scope.get(name), 40) or "none")
                for name in ("claude", "render")
                if name in scope
            ],
        ),
    )
    return "".join(
        f'<h3>{name}</h3><table class="metrics"><tbody>{_rows(pairs)}</tbody></table>'
        for name, pairs in tables
        if pairs
    )


def _cost_rows(run: RunView) -> list[str]:
    rows = []
    for flow in run.flows:
        for attempt in flow.attempts:
            claude = attempt.result.claude
            cells = (
                (_tag("a", _escape(flow.key), ("href", f"#{flow.anchor}")), ""),
                (str(attempt.result.attempt), "n"),
                (_usd(_claude(attempt, "counted_usd")), "n"),
                (_usd(_claude(attempt, "cost_usd_pinned")), "n"),
                (_usd(_claude(attempt, "cost_usd")), "n"),
                (_usage(claude.usage) if claude else "no session", ""),
            )
            rows.append(
                "<tr>"
                + "".join(
                    _tag("td", value, ("class", cls or None)) for value, cls in cells
                )
                + "</tr>"
            )
    watched = _mapping(run.record.get("interactive"))
    if watched:
        assisted = watched.get("assisted")
        who = "typing unknown"
        if isinstance(assisted, bool):
            who = "assisted" if assisted else "unassisted"
        rows.append(
            f"<tr><td>the watched session, {_escape(who)}</td><td></td>"
            f'<td class="n">{_usd(watched.get("counted_usd"))}</td>'
            f'<td class="n">{_usd(watched.get("cost_usd_pinned"))}</td>'
            f'<td class="n">{_usd(watched.get("cli_cost_usd"))}</td>'
            f"<td>{_usage(watched.get('usage'))}</td></tr>"
        )
    totals = _cost_totals(run)
    without = int(totals["sessions_without_usage"])
    rows.append(
        '<tr><th scope="row">Total</th><td></td>'
        f'<td class="n"><strong>{_usd(totals["counted_usd"])}</strong></td>'
        f'<td class="n"><strong>{_usd(totals["pinned_usd"])}</strong></td><td></td>'
        f"<td>{_plural(without, 'session')} without usage</td></tr>"
    )
    return rows


def _setup(run: RunView) -> str:
    spend = run.record.get("spend_usd")
    spent = (
        f"<p>The run's budget counted {_usd(spend)}. A session counts the larger of "
        "its CLI's own cost and its usage at the pinned rates, so the pinned total is "
        "a floor whenever a session reported no usage.</p>"
        if _is_number(spend)
        else ""
    )
    head = "".join(
        _tag("th", name, ("scope", "col"), ("class", cls or None))
        for name, cls in (
            ("Flow", ""),
            ("Attempt", "n"),
            ("Counted", "n"),
            ("At the pinned rates", "n"),
            ("The CLI's own", "n"),
            ("Tokens", ""),
        )
    )
    return (
        '<section id="setup"><h2>Setup and cost</h2>'
        f"{_setup_tables(run.record)}<h3>Cost</h3>{spent}"
        f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(_cost_rows(run))}"
        "</tbody></table></section>"
    )


def _video_notes(record: Mapping[str, Any]) -> list[str]:
    lines = []
    for item in map(_mapping, _list(record.get("video_withheld_flows"))):
        reason = _text(item.get("reason"))
        if reason:
            lines.append(
                f"the video of {_code(item.get('key'))} was withheld: {reason}"
            )
    reply = _mapping(record.get("reply"))
    for item in map(_mapping, _list(reply.get("video_notes"))):
        kind, key = item.get("kind"), _code(item.get("key"))
        if kind == "upload_failed":
            lines.append(f"the video of {key} could not be uploaded")
        elif kind == "over_limit":
            size = _text(item.get("detail"), 20)
            lines.append(f"the video of {key}, {size} MB, was over the upload limit")
        elif kind == "not_rendered":
            lines.append(f"the video of {key} could not be rendered")
    return lines


def _caveats(run: RunView) -> str:
    record = run.record
    reply = _mapping(record.get("reply"))
    lines = [_text(caveat) for caveat in _list(reply.get("caveats"))]
    lines += _video_notes(record)
    for item in map(_mapping, _list(record.get("reruns_not_run"))):
        lines.append(
            f"{_code(item.get('key'))} was not run again "
            f"({_text(item.get('reason'), 40)}), so its bad result stays unconfirmed"
        )
    attempts = [attempt for flow in run.flows for attempt in flow.attempts]
    unshown = sum(attempt.unshown_screens for attempt in attempts)
    absent = sum(1 for a in attempts if a.result.video and not a.video_url)
    if unshown or absent:
        lines.append(
            f"the journals name {_plural(unshown, 'screen')} and the results "
            f"{_plural(absent, 'video')} that this directory does not hold; the "
            "nightly digest leaves both in each shard's own artifacts, beside that "
            "shard's report.html"
        )
    lines.append(
        "screens and videos are not redacted; every quoted string is page or model "
        "text, shown as text, and nothing in it is a link"
    )
    items = "".join(f"<li>{line}</li>" for line in lines if line)
    return f'<section id="caveats"><h2>Caveats</h2><ul>{items}</ul></section>'


def render(run_dir: Path, *, catalogue: CatalogueText | None = None) -> str:
    """The run directory's ``report.html``, made from its files and nothing else.

    ``catalogue`` gives each story's intent and each persona's background, shown
    only where it is the run's own (:func:`_story_text`). The same files make the
    same page, wherever the directory is.
    """
    run = read_run(run_dir)
    groups = _filters(run)
    flows = {flow.key: flow for flow in run.flows}
    sections = "".join(
        _flow_section(flow, catalogue, run.record, flows) for flow in run.flows
    )
    bad = sum(
        1
        for flow in run.flows
        if flow.confirmation.verdict in story_verdict.BAD_VERDICTS
    )
    title = (
        f"First-time-user stories: {_plural(len(run.flows), 'flow')}, "
        f"{bad} missing the expectation"
    )
    policy = _attrs(
        (
            ("http-equiv", "Content-Security-Policy"),
            ("content", CONTENT_SECURITY_POLICY),
        )
    )
    head = (
        f'<meta charset="utf-8"><meta{policy}>'
        '<meta name="referrer" content="no-referrer">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_escape(title)}</title><style>{_style(groups)}</style>"
    )
    body = (
        _radios(groups)
        + _header(run)
        + "<main>"
        + _chips(groups)
        + _index(run)
        + _findings(run, flows)
        + _by_story(run)
        + f'<section id="flows"><h2>Each flow</h2>{sections or "<p>No flow.</p>"}'
        + "</section>"
        + _setup(run)
        + _caveats(run)
        + "</main><footer><p>Made from this directory's files by "
        + "<code>scripts/ux_runner/story_report.py</code>; it holds no script and "
        + "loads nothing from anywhere else.</p></footer>"
    )
    return (
        f'<!DOCTYPE html>\n<html lang="en">\n<head>{head}</head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def write_report(
    run_dir: Path, *, catalogue: CatalogueText | None = None, out: Path | None = None
) -> Path:
    """Write :func:`render`'s page to ``out``, by default ``run_dir/report.html``,
    and return where; never through a link.

    The page's links are relative to ``run_dir``, so a page written elsewhere
    follows them only once it is beside the run's files.
    """
    data = render(run_dir, catalogue=catalogue).encode("utf-8")
    target = out if out is not None else run_dir / REPORT_FILE
    try:
        with os.fdopen(os.open(target, _WRITE_FLAGS, 0o644), "wb") as handle:
            handle.write(data)
    except OSError as error:
        raise ReportError(
            f"{target.name} cannot be written: {_why_not(error)}"
        ) from None
    return target


def load_catalogue(path: Path = stories.CATALOGUE_PATH) -> CatalogueText | None:
    """The catalogue at ``path`` with the sha256 of its bytes, or None."""
    try:
        data = path.read_bytes()
        catalogue = stories.load_catalogue(path)
    except (OSError, stories.CatalogueError):
        return None
    return CatalogueText(catalogue, hashlib.sha256(data).hexdigest())


def failure(error: BaseException) -> str:
    """What a caller says of a report it could not write, quoting no file's text."""
    return str(error) if isinstance(error, ReportError) else type(error).__name__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="story_report.py",
        description=(
            "Write RUN_DIR/report.html, the story run as one page to open in a "
            "browser, and print where. Standard library only: it reads RUN_DIR and "
            "the story catalogue, and the page holds no script and loads nothing "
            "from anywhere else."
        ),
    )
    parser.add_argument(
        "run_dir", metavar="RUN_DIR", help="the run directory, holding results.json"
    )
    parser.add_argument(
        "--out",
        type=Path,
        metavar="FILE",
        help="write the page here instead; its links stay relative to RUN_DIR",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write the report and print its path; 1 and a ``FAILED:`` line otherwise."""
    args = _parser().parse_args(argv)
    try:
        target = write_report(
            Path(args.run_dir), catalogue=load_catalogue(), out=args.out
        )
    except ReportError as error:
        print(f"FAILED: {error}")
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
