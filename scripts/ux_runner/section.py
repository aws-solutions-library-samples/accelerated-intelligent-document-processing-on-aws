# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Everything the MR note says about a UX test, composed by the harness alone.

The model writes a report; it does not write the note. Its final message must end
with one fenced JSON block (D-REPORT), and :func:`parse_report` reads that block,
drops every key it does not know, bounds every list and every string, and
refuses an outcome outside :data:`OUTCOME_ICONS`. :func:`compose_section` then
builds the note text from those fields and a fixed template (D-SECTION). Prose
the model wrote around the block reaches the job's ``report.md`` artifact, never
the note.

Why model text is sanitised as well as bounded
----------------------------------------------
The note is posted with ``GITLAB_REVIEW_TOKEN``, and the text being posted is
derived from a session that read the MR's diff and the MR's running UI, both
written by the MR's author. So every model-derived string goes through
:func:`sanitize`, which leaves one line of plain text that GitLab cannot act on.
GitLab renders a note as CommonMark and then runs its mention, reference and
autolink filters over the rendered text, so the rules are written against what
the renderer will see, not against one spelling of it. In the order they apply:

* control characters, line breaks and runs of whitespace collapse, so a field
  cannot start a line of its own and become a quick action or a heading;
* an ``&`` that could start a character reference or an epic reference gets a
  zero-width space, so GitLab decodes nothing out of model text: a mention or a
  reference spelled as an entity (``&commat;all``, ``&#x23;12``) stays the text
  it was written as, and so does a registered value the redactor would have
  matched in its plain spelling, rather than reaching the note decoded.
  ``&lt;`` is the one reference left, whoever wrote it, and it decodes to text;
* ``<`` becomes ``&lt;``, so there is no raw HTML, no ``<url>`` autolink and no
  ``<!--`` marker. That matters twice over: ``ai_mr_review`` treats any
  ``<!-- ai-review: ... -->`` as "already reviewed", and :data:`UX_MARKER_RE`
  keys this job's own idempotency;
* a zero-width space follows every ``[``, every ``!`` before one, and every
  ``]`` before ``(``, ``[`` or ``:``, so no link, image, link definition,
  footnote, task-list box or bracketed GitLab reference can form;
* ``@``, and ``#`` or ``!`` before a digit, get one too, so the note mentions
  nobody and refers to no issue or merge request;
* so do ``~``, ``%`` and ``$`` wherever whitespace does not follow them, the end
  of the text included, ``*`` before ``iteration:``, and the hyphen of an
  uppercase issue key such as ``ABC-`` before a digit, so there is no label,
  milestone, snippet or iteration reference and nothing a Jira-style external
  tracker would take for one of its issues. The sigils take any character after
  them because GitLab's own patterns are wider than a word: an unquoted label
  name may start with ``-``, ``?``, ``.`` or ``&``, a milestone name with
  anything but whitespace, a quote or ``<``, and ``~5`` and ``%5`` are
  references by id. The end of the text counts because the note's own text
  follows a field there, and this function cannot see what it is. The sigil
  stays on screen, so ``$5`` and ``~3 min`` still read as written, and one before
  a space, as in ``50% off``, is left alone, since every reference needs a name
  or an id right after it. The iteration and issue-key rules also match where a character they
  look at is written with a CommonMark backslash escape (``*iteration\\:3``,
  ``ABC\\-12``, ``AB\\_C-1``), which renders as the character itself, and the
  sigil rule covers an escape after a sigil (``~\\"bug\\"``, ``$\\_12``)
  because a backslash is not whitespace. A backslash before a letter or a digit
  is no escape: it stays on screen and already breaks the reference, so the
  issue-key rule does not match across one;
* so does every ``\\`` before an ASCII letter. GitLab renders ``$...$``,
  ``$`...`$`` and ``$$...$$`` as math with KaTeX, whose ``\\href`` and ``\\url``
  make links, and a backslash followed by a zero-width space starts no command;
  Markdown shows that backslash as written;
* so do a scheme's ``://`` and a leading ``www.``, including a ``:`` or ``/``
  written backslash-escaped, so no bare URL is autolinked. A URL naming an issue
  or merge request on the same instance would otherwise be a reference as well;
* a leading ``/`` is escaped, belt and braces with the gitlab_api layer's
  ``defuse_quick_actions``;
* the result is cut to its bound, and a cut drops the whole run of whitespace,
  ``@``, ``[``, ``~``, ``%``, ``$`` and partial ``&lt;`` that would otherwise end
  it before the ellipsis. So it never leaves half an escape behind, nor a sigil
  the ellipsis would follow unescaped, including one that whitespace had left
  without an escape (``costs 50% [see]`` cut just after the ``[`` reads
  ``costs 50…``), which is what keeps :func:`sanitize` idempotent.

A bare commit SHA is the one reference form left as written: GitLab links it only
when the project has that commit, and then records a cross-reference on that
commit's page, and breaking it would break copying it. An external tracker
configured with an issue pattern of its own, other than the uppercase-key shape,
is not covered either.

Text that changes after sanitising
----------------------------------
The orchestrator redacts the composed section once more before it is posted, and
a redaction writes a placeholder such as ``[token]``: brackets, which a link needs.
So whatever is redacted after :func:`sanitize` goes through :func:`inert_markup`,
which breaks the brackets a placeholder brings in exactly as :func:`sanitize`
breaks the model's own. And the shape rules a redactor applies before sanitising
read :func:`flatten`'s output, the text with its control and bidi characters
already turned into spaces, so that a ``Bearer`` token sanitising would separate
from its keyword is caught before the field is cut rather than after.

What a section looks like, and why it is appended
--------------------------------------------------
The section is appended to the ``ai_mr_review`` note for the same head, so a
reader finds the code review and the UX test in one place. :func:`append_section`
always leaves a blank line before the section's ``---``: the review's footer is a
paragraph, and ``---`` directly under a paragraph is a setext heading underline,
which would turn the whole footer into a heading. :func:`compose_standalone` is
the fallback when no review note exists, and it never carries an ``ai-review:``
marker, because one would tell ``ai_mr_review`` this head was already reviewed.

The marker records the full 40-character head, the prompt revision and the upload
id, so :func:`has_section` is exact rather than a prefix match and a later,
separately authorised change can find the uploads to delete (D9). When a newer
head lands, :func:`supersede_video` replaces the older note's video line; the
upload itself is kept.

Where a marker counts
---------------------
A UX marker is recognised only where the harness writes one. The code review a
section is appended to is model output, and it can quote anything, a marker
included: one quoted there would otherwise mark a later head as already tested.
So a note is read from where this job's own text starts: its first character
when it opens with a UX marker or a section, the end of
:data:`STANDALONE_PREFIX`, or the end of the code review's last footer line
(:data:`REVIEW_FOOTER_PREFIX`), the line ``ai_mr_review`` writes last and the one
a section is appended below. A note that is none of those holds no section.
Below that point every character is the harness's, because no sanitised field
can contain ``<``.

The version and build shown come from the MR's own UI, so the MR can choose
them. They are shown only when they have the expected shape and length, and the
caveats say they are as the UI reports them. A version's shape still admits an
uppercase issue key and a ``www.`` (``0.ABC-1.www.e``), so the heading shows it
through the same rules as the model's text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ux_runner import ui_change

#: Bumped when the prompt or the note's contract changes in a way that makes an
#: existing section stale. It is part of the marker, so a bump re-tests open MRs.
UX_PROMPT_REVISION = 1

UX_MARKER_RE = re.compile(
    r"<!--\s*ux-test:\s*sha=(?P<sha>[0-9a-f]{7,40})\s+rev=(?P<rev>\d+)"
    r"\s+upload=(?P<upload>\d+|none)\s*-->"
)
VIDEO_LINE_RE = re.compile(
    r"^!\[UX test\]\(/uploads/[0-9a-f]{16,64}/[A-Za-z0-9._-]{1,128}\)$", re.M
)

#: One icon per outcome. An outcome outside this table is refused at parse time.
OUTCOME_ICONS = {"works": "✅", "broken": "❌", "blocked": "⏭️", "not_covered": "⚪"}

MAX_FLOWS = 4
MAX_FINDINGS = 10
MAX_BREAKAGE = 5
MAX_NOT_COVERED = 5
MAX_INJECTIONS = 5
TITLE_CHARS = 120
SUMMARY_CHARS = 160
TEXT_CHARS = 300
QUOTE_CHARS = 200

#: Flow ids, and the ``where`` of an injection attempt: short labels.
ID_CHARS = 40
#: Chapter labels, the same bound the recorder adapter puts on a mark label.
CHAPTER_CHARS = 80
#: A chapter's ``m:ss`` time.
CHAPTER_TIME_CHARS = 12
#: At most one chapter per mark the recorder adapter accepts.
MAX_CHAPTERS = 40
#: One caveat line; the API-contract caveat names up to three paths.
CAVEAT_CHARS = 600
#: The persona, stack and model names the harness supplies.
NAME_CHARS = 120
#: The longest version the heading shows. A release version is far shorter; the
#: bound is this module's own, whatever the reader of the page cut it to.
VERSION_CHARS = 40

STANDALONE_PREFIX = (
    "_The code review for this head was not found, so the UX test is posted on "
    "its own._"
)
#: How ``ai_mr_review``'s footer line starts. It is the last line that tool
#: writes, so a section is appended below it, and only text below its last
#: occurrence in a review note is this job's.
REVIEW_FOOTER_PREFIX = "🤖 Automated review of "
UI_FACTS_CAVEAT = "backend facts as the UI reports them"

ZERO_WIDTH_SPACE = "\u200b"
ELLIPSIS = "\u2026"

_CONTROL_RE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"
)
_AMPERSAND_RE = re.compile(r"&(?!lt;)(?=[#\w])")
_OPENING_BRACKET_RE = re.compile(r"\[(?!\u200b)")
_IMAGE_BANG_RE = re.compile(r"!(?=\[)")
_CLOSING_BRACKET_RE = re.compile(r"\](?=[(\[:])")
_MENTION_RE = re.compile(r"@(?!\u200b)")
_REFERENCE_RE = re.compile(r"([#!])(?=\d)")
_PREFIX_REFERENCE_RE = re.compile(r"([~%$])(?![\s\u200b])")
_ITERATION_RE = re.compile(r"\*(?=iteration\\?:)")
_ISSUE_KEY_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z](?:[A-Z0-9_]|\\_)+\\?-)(?=\d)")
_COMMAND_RE = re.compile(r"\\(?=[A-Za-z])")
_SCHEME_RE = re.compile(r"(?<=[A-Za-z0-9+.-])(?=\\?:\\?/\\?/)")
_WWW_RE = re.compile(r"(?<![A-Za-z0-9])([Ww]{3})(?=\\?\.)")
#: The run a cut drops before its ellipsis: whitespace, ``@``, ``[``, ``~``, ``%``,
#: ``$`` and a partial ``&lt;``. The lookbehind lets a match start only where such a
#: run starts, so a long run of sigils and spaces is read once rather than once for
#: every character in it.
_DANGLING_RE = re.compile(r"(?<![@\[~%$\s&])(?:[@\[~%$\s]|&(?:lt?)?)+$")
_BACKSLASH_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_CARD_LT_RE = re.compile(r"&lt;(?!!--)")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHORT_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_VERSION_RE = re.compile(r"[0-9]+(\.[0-9A-Za-z-]+){1,4}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_VIDEO_RE = re.compile(r"!\[UX test\]\([^\s()<>]+\)")
_FENCE_OPEN_RE = re.compile(r"^[ \t]*```[ \t]*json[ \t]*\r?$", re.M | re.I)
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*```[ \t]*\r?$", re.M)
_AI_REVIEW_MARKER_RE = re.compile(r"<!--\s*ai-review:")
_OWN_OPENING_RE = re.compile(r"(?:---[ \t]*\r?\n)?" + UX_MARKER_RE.pattern)
_FOOTER_LINE_RE = re.compile(r"^" + re.escape(REVIEW_FOOTER_PREFIX) + r".*$", re.M)
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,40}")


class ReportError(Exception):
    """The model's report is missing or unusable, so nothing can be posted."""


@dataclass(frozen=True)
class StackFacts:
    """The review stack as the note describes it.

    ``version`` and ``build`` are read from the MR's own UI, so they are shown
    only in the shapes :attr:`shown_version` and :attr:`shown_build` accept.
    """

    name: str
    region: str
    version: str | None = None
    build: str | None = None

    @property
    def shown_version(self) -> str | None:
        """The version, if it has a release version's shape and length."""
        version = self.version.strip() if isinstance(self.version, str) else ""
        if len(version) <= VERSION_CHARS and _VERSION_RE.fullmatch(version):
            return version
        return None

    @property
    def shown_build(self) -> str | None:
        """The first YYYY-MM-DD date in the build string, if there is one."""
        found = _DATE_RE.search(self.build) if isinstance(self.build, str) else None
        return found.group(0) if found else None


@dataclass(frozen=True)
class FlowResult:
    id: str
    title: str
    outcome: str
    summary: str


@dataclass(frozen=True)
class Finding:
    flow: str
    text: str


@dataclass(frozen=True)
class Breakage:
    flow: str
    steps: str
    observed: str
    expected: str


@dataclass(frozen=True)
class NotCovered:
    flow: str
    reason: str


@dataclass(frozen=True)
class InjectionAttempt:
    where: str
    quote: str


@dataclass(frozen=True)
class Report:
    """The model's JSON report, bounded and sanitised by :func:`parse_report`."""

    flows: tuple[FlowResult, ...]
    findings: tuple[Finding, ...]
    breakage: tuple[Breakage, ...]
    not_covered: tuple[NotCovered, ...]
    visible_change_found: bool
    injection_attempts: tuple[InjectionAttempt, ...]


def _spaced(match: re.Match[str]) -> str:
    return match.group(1) + ZERO_WIDTH_SPACE


def inert_markup(text: str) -> str:
    """``text`` with every ``[``, every ``!`` before one and every ``]`` before
    ``(``, ``[`` or ``:`` followed by a zero-width space, and nothing else changed.

    These are :func:`sanitize`'s bracket rules alone, for text a redactor changed
    after it was sanitised: a placeholder such as ``[token]`` brings brackets in,
    and the text around it may hold the ``(`` that would make them a link. Every
    other construct :func:`sanitize` breaks stays broken, because a redaction only
    removes text and puts a placeholder in its place. Idempotent.
    """
    text = _OPENING_BRACKET_RE.sub("[" + ZERO_WIDTH_SPACE, text)
    text = _IMAGE_BANG_RE.sub("!" + ZERO_WIDTH_SPACE, text)
    return _CLOSING_BRACKET_RE.sub("]" + ZERO_WIDTH_SPACE, text)


def flatten(text: str) -> str:
    """``text`` on one line: control and bidi characters and whitespace runs as one
    space, ends stripped. The first thing :func:`sanitize` does, and what a
    redactor reads before sanitising, so both see the same words."""
    return " ".join(_CONTROL_RE.sub(" ", text).split())


def _neutralised(flat: str) -> str:
    """``flat`` with every construct GitLab acts on broken; see the module docstring."""
    flat = _AMPERSAND_RE.sub("&" + ZERO_WIDTH_SPACE, flat)
    flat = flat.replace("<", "&lt;")
    flat = inert_markup(flat)
    flat = _MENTION_RE.sub("@" + ZERO_WIDTH_SPACE, flat)
    flat = _REFERENCE_RE.sub(_spaced, flat)
    flat = _PREFIX_REFERENCE_RE.sub(_spaced, flat)
    flat = _ITERATION_RE.sub("*" + ZERO_WIDTH_SPACE, flat)
    flat = _ISSUE_KEY_RE.sub(_spaced, flat)
    flat = _COMMAND_RE.sub("\\\\" + ZERO_WIDTH_SPACE, flat)
    flat = _SCHEME_RE.sub(ZERO_WIDTH_SPACE, flat)
    return _WWW_RE.sub(_spaced, flat)


def sanitize(text: object, limit: int) -> str:
    """One line of plain text GitLab cannot act on, at most ``limit`` characters.

    The rules are listed in the module docstring, in the order they apply.
    Applying it twice gives the same result as applying it once, so composing
    from already-sanitised fields is safe: every rule leaves what it matches
    unmatchable, the ``&`` rule passes over an ``&lt;`` because the ``<`` rule
    writes one, and a cut drops whatever run of whitespace, ``@``, ``[``, ``~``,
    ``%``, ``$`` and partial ``&lt;`` it would otherwise end on: an ``@``, ``[``
    or partial ``&lt;`` would be separated from its escape, and the ellipsis
    would follow a ``~``, ``%`` or ``$`` unescaped, whether the cut took its
    escape or whitespace after it had meant it needed none.
    """
    if not isinstance(text, str) or limit < 1:
        return ""
    flat = _neutralised(flatten(text))
    if flat.startswith("/"):
        flat = "\\" + flat
    if len(flat) > limit:
        head = _DANGLING_RE.sub("", flat[: limit - 1])
        flat = head + ELLIPSIS
    return flat


def _last_json_block(text: str) -> str | None:
    openers = list(_FENCE_OPEN_RE.finditer(text))
    if not openers:
        return None
    start = openers[-1].end()
    closer = _FENCE_CLOSE_RE.search(text, start)
    return None if closer is None else text[start : closer.start()]


def _entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _flow_result(item: dict[str, Any]) -> FlowResult:
    outcome = item.get("outcome")
    normalised = outcome.strip().lower() if isinstance(outcome, str) else ""
    if normalised not in OUTCOME_ICONS:
        raise ReportError(
            f"a flow's outcome is {sanitize(outcome, ID_CHARS)!r}, not one of "
            + ", ".join(OUTCOME_ICONS)
        )
    ident = sanitize(item.get("id"), ID_CHARS)
    return FlowResult(
        id=ident,
        title=sanitize(item.get("title"), TITLE_CHARS) or ident or "untitled",
        outcome=normalised,
        summary=sanitize(item.get("summary"), SUMMARY_CHARS),
    )


def parse_report(text: str) -> Report:
    """Read the last fenced JSON block of the model's final message.

    Unknown keys are dropped, lists are cut to their ``MAX_*`` bounds, and every
    string is sanitised to its bound. Entries with nothing left to show are
    dropped. A missing block, an empty flows list, an outcome outside
    :data:`OUTCOME_ICONS`, or a ``visible_change_found`` that is not a boolean
    raises :class:`ReportError`.
    """
    block = _last_json_block(text) if isinstance(text, str) else None
    if block is None:
        raise ReportError("the session did not end with a JSON report")
    try:
        data = json.loads(block)
    except (ValueError, RecursionError) as error:
        raise ReportError(f"the JSON report does not parse: {error}") from None
    if not isinstance(data, dict):
        raise ReportError("the JSON report is not an object")
    flows = tuple(
        _flow_result(item) for item in _entries(data.get("flows"))[:MAX_FLOWS]
    )
    if not flows:
        raise ReportError("the JSON report names no flows")
    visible = data.get("visible_change_found")
    if not isinstance(visible, bool):
        raise ReportError("visible_change_found must be true or false")
    findings = [
        Finding(
            flow=sanitize(item.get("flow"), ID_CHARS),
            text=sanitize(item.get("text"), TEXT_CHARS),
        )
        for item in _entries(data.get("findings"))
    ]
    breakage = [
        Breakage(
            flow=sanitize(item.get("flow"), ID_CHARS),
            steps=sanitize(item.get("steps"), TEXT_CHARS),
            observed=sanitize(item.get("observed"), TEXT_CHARS),
            expected=sanitize(item.get("expected"), TEXT_CHARS),
        )
        for item in _entries(data.get("breakage"))
    ]
    not_covered = [
        NotCovered(
            flow=sanitize(item.get("flow"), ID_CHARS),
            reason=sanitize(item.get("reason"), TEXT_CHARS),
        )
        for item in _entries(data.get("not_covered"))
    ]
    attempts = [
        InjectionAttempt(
            where=sanitize(item.get("where"), ID_CHARS),
            quote=sanitize(item.get("quote"), QUOTE_CHARS),
        )
        for item in _entries(data.get("injection_attempts"))
    ]
    return Report(
        flows=flows,
        findings=tuple(f for f in findings if f.text)[:MAX_FINDINGS],
        breakage=tuple(b for b in breakage if b.steps or b.observed or b.expected)[
            :MAX_BREAKAGE
        ],
        not_covered=tuple(n for n in not_covered if n.flow or n.reason)[
            :MAX_NOT_COVERED
        ],
        visible_change_found=visible,
        injection_attempts=tuple(a for a in attempts if a.where or a.quote)[
            :MAX_INJECTIONS
        ],
    )


def _code(text: object, limit: int) -> str:
    return sanitize(text, limit).replace("`", "'")


def caveats_for(change: ui_change.UiChange, stack: StackFacts) -> list[str]:
    """What a reader must know to weigh the section, in note order."""
    first = (
        f"backend is `develop` as of the last nightly update of "
        f"`{_code(stack.name, NAME_CHARS)}`"
    )
    if change.backend:
        first += "; this MR's own backend changes are not deployed there"
    caveats = [first]
    if change.api_contract:
        shown = ", ".join(
            f"`{_code(path, TITLE_CHARS)}`" for path in change.api_contract[:3]
        )
        if len(change.api_contract) > 3:
            shown += f" and {len(change.api_contract) - 3} more"
        caveats.append(
            f"this MR changes the API contract ({shown}), so screens that depend "
            "on it are reported as blocked, not broken"
        )
    if stack.shown_version is None:
        caveats.append("the stack version could not be read from the UI")
    return caveats


def refusal_caveat(
    refusals: Iterable[object], *, background: Callable[[str], bool] | None = None
) -> str | None:
    """The caveat for what the egress proxy refused the browser, or None.

    ``refusals`` are the proxy's refusal records, as ``egress.read_refusals``
    returns them. ``background`` is given each record's target and says whether
    it is one the MR has no part in: the browser's own background traffic, and
    the harness's own proxy self-test, both refused by design. Those are left
    out, so the caveat appears only when something the page needed was refused,
    which is what a reader weighs a broken-looking screen against.

    Only a count per reason reaches the note, never a target. The page chooses
    what it asks for, so a target is text the MR's author wrote, and a host name
    has room for anything the page can read. ``refusals.jsonl`` in the job's
    artifacts lists the targets, redacted. A reason outside the proxy's
    vocabulary shape is counted as ``other``.
    """
    counts: dict[str, int] = {}
    for record in refusals:
        if not isinstance(record, Mapping):
            continue
        target = record.get("target")
        if background is not None and background(
            target if isinstance(target, str) else ""
        ):
            continue
        reason = record.get("reason")
        named = isinstance(reason, str) and _REASON_RE.fullmatch(reason)
        key = reason if named and isinstance(reason, str) else "other"
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    shown = ", ".join(f"`{reason}` ×{count}" for reason, count in ranked)
    return (
        f"the egress proxy refused {sum(counts.values())} of the browser's "
        f"requests ({shown}), so a screen that needed one can look broken; "
        "`refusals.jsonl` in the job's artifacts lists them"
    )


def _breakage_line(item: Breakage) -> str:
    sequence = " → ".join(
        part
        for part in (
            sanitize(item.steps, TEXT_CHARS),
            sanitize(item.observed, TEXT_CHARS),
        )
        if part
    )
    expected = sanitize(item.expected, TEXT_CHARS)
    line = f"{sanitize(item.flow, ID_CHARS)}: {sequence}" if item.flow else sequence
    return f"{line} (expected: {expected})" if expected else line


def compose_section(
    *,
    head_sha: str,
    persona: str,
    stack: StackFacts,
    report: Report,
    video: str | None,
    video_note: str | None,
    chapters: Sequence[tuple[str, str]],
    caveats: Sequence[str],
    model: str,
    upload_id: int | None,
) -> str:
    """The UX section of the note, by the D-SECTION template.

    ``video`` is the whole image line, ``![UX test](<url>)``; ``video_note`` is
    the italic sentence shown instead when there is no video. Chapters are shown
    only with a video.
    """
    if not isinstance(head_sha, str) or not _FULL_SHA_RE.fullmatch(head_sha):
        raise ValueError("head_sha must be the full 40-character head SHA")
    if upload_id is not None and (
        isinstance(upload_id, bool) or not isinstance(upload_id, int) or upload_id < 0
    ):
        raise ValueError("upload_id must be a non-negative integer or None")
    if video is not None and not _VIDEO_RE.fullmatch(video):
        raise ValueError("video must be a single ![UX test](<url>) image line")
    sha8 = head_sha[:8]
    stack_name = _code(stack.name, NAME_CHARS)
    heading = f"## 🖱️ UX test — {sanitize(persona, NAME_CHARS)}, {stack_name}"
    if stack.shown_version:
        heading += f" v{_neutralised(stack.shown_version)}"
    if stack.shown_build:
        heading += f" (built {stack.shown_build})"
    heading += f", MR UI build `{sha8}`"
    upload = "none" if upload_id is None else str(upload_id)
    lines = [
        "---",
        f"<!-- ux-test: sha={head_sha} rev={UX_PROMPT_REVISION} upload={upload} -->",
        heading,
        "",
        "**Looked at**",
    ]
    for flow in report.flows:
        icon = OUTCOME_ICONS.get(flow.outcome, OUTCOME_ICONS["not_covered"])
        title = sanitize(flow.title, TITLE_CHARS) or sanitize(flow.id, ID_CHARS)
        summary = sanitize(flow.summary, SUMMARY_CHARS)
        lines.append(f"- {icon} {title}" + (f" — {summary}" if summary else ""))
    lines += ["", "**Findings** (ranked; suggestion, not a demand)"]
    texts = [sanitize(f.text, TEXT_CHARS) for f in report.findings[:MAX_FINDINGS]]
    lines += [f"- {text}" for text in texts if text] or ["- none"]
    lines.append("")
    breakage = [_breakage_line(b) for b in report.breakage[:MAX_BREAKAGE]]
    breakage = [line for line in breakage if line]
    if breakage:
        lines.append("**Functional breakage**")
        lines += [f"- {line}" for line in breakage]
    else:
        lines.append("**Functional breakage** — none")
    if video is not None:
        lines += ["", video]
        marks = [
            f"{sanitize(at, CHAPTER_TIME_CHARS)} {sanitize(label, CHAPTER_CHARS)}"
            for at, label in list(chapters)[:MAX_CHAPTERS]
        ]
        marks = [mark for mark in marks if mark.strip()]
        if marks:
            lines += ["", "**Chapters:** " + " · ".join(marks)]
    elif video_note:
        lines += ["", sanitize(video_note, TEXT_CHARS)]
    notes = [sanitize(c, CAVEAT_CHARS) for c in caveats if isinstance(c, str)]
    notes = [note for note in notes if note]
    if stack.shown_version or stack.shown_build:
        notes.append(UI_FACTS_CAVEAT)
    if notes:
        lines += ["", "**Caveats:** " + "; ".join(notes)]
    if report.injection_attempts:
        lines += [
            "",
            f"⚠️ **The MR's content tried to steer this test** "
            f"({len(report.injection_attempts)} attempt(s)); the quotes are in "
            "the job's report.md.",
        ]
    lines += [
        "",
        f"🤖 Automated UX test of `{sha8}` by `{_code(model, NAME_CHARS)}` in a "
        f"confined headless browser against `{stack_name}`, following "
        "`.claude/skills/ux-test-ci.md`. **Advisory only** — it approves nothing "
        "and gates nothing.",
    ]
    return "\n".join(lines) + "\n"


def append_section(note_body: str, section: str) -> str:
    """``note_body`` with ``section`` appended after one blank line."""
    if not note_body.strip():
        return section.strip("\n") + "\n"
    return note_body.rstrip("\r\n") + "\n\n" + section.strip("\n") + "\n"


def _own_start(note_body: str) -> int | None:
    """Where this job's own text in ``note_body`` starts, or None if it has none.

    The positions are listed in the module docstring under "Where a marker
    counts". A code review without its footer line has none, so a section can
    never be read out of the review text above where the footer should be.
    """
    if _OWN_OPENING_RE.match(note_body):
        return 0
    if note_body.startswith(STANDALONE_PREFIX):
        return len(STANDALONE_PREFIX)
    if not _AI_REVIEW_MARKER_RE.match(note_body):
        return None
    footers = list(_FOOTER_LINE_RE.finditer(note_body))
    return footers[-1].end() if footers else None


def sections_in(note_body: str) -> list[tuple[str, int]]:
    """The (head SHA, revision) pair of every UX marker this job wrote in a note."""
    start = _own_start(note_body)
    if start is None:
        return []
    return [
        (match.group("sha"), int(match.group("rev")))
        for match in UX_MARKER_RE.finditer(note_body, start)
    ]


def has_section(note_body: str, head_sha: str, rev: int = UX_PROMPT_REVISION) -> bool:
    """Whether ``note_body`` already carries the section for this head and rev."""
    return (head_sha, rev) in sections_in(note_body)


def supersede_video(note_body: str, new_sha8: str) -> str | None:
    """``note_body`` with its UX video lines superseded, or None if it had none.

    Only lines after the first marker this job wrote are touched, so a line in
    the code review above it that happens to look like a video, or that follows
    a marker the review quotes, is left alone.
    """
    if not _SHORT_SHA_RE.fullmatch(new_sha8):
        raise ValueError("new_sha8 must be an abbreviated head SHA")
    start = _own_start(note_body)
    first = None if start is None else UX_MARKER_RE.search(note_body, start)
    if first is None:
        return None
    replacement = f"_Video superseded by `{new_sha8}`._"
    tail, count = VIDEO_LINE_RE.subn(replacement, note_body[first.start() :])
    return note_body[: first.start()] + tail if count else None


def compose_reply(head_sha: str, report: Report) -> str:
    """The one reply posted in the review note's thread, which is what notifies."""
    if not _FULL_SHA_RE.fullmatch(head_sha):
        raise ValueError("head_sha must be the full 40-character head SHA")
    text = (
        f"🖱️ UX test of `{head_sha[:8]}` added to the review above: "
        f"{len(report.flows)} flow(s) looked at, {len(report.findings)} finding(s)"
    )
    if report.breakage:
        text += ", functional breakage reported"
    return text + "."


def compose_standalone(section: str) -> str:
    """The note posted when no code review note exists for the head."""
    body = STANDALONE_PREFIX + "\n\n" + section.strip("\n") + "\n"
    if _AI_REVIEW_MARKER_RE.search(body):
        raise ValueError("a standalone UX note must not carry an ai-review marker")
    return body


def _card(text: object, limit: int) -> str:
    """One line for the video's end card: sanitised, then shown as GitLab shows it.

    The card is drawn as an image, so nothing on it is parsed; what has to hold is
    that each entry stays on one line, which is what the recorder's parser keys on,
    and that no entry spells the recorder's own ``<!--`` chapter markers, which its
    ``replace_chapters`` would take for the real ones.
    """
    plain = sanitize(text, limit).replace(ZERO_WIDTH_SPACE, "")
    return _CARD_LT_RE.sub("<", _BACKSLASH_ESCAPE_RE.sub(r"\1", plain))


def review_md(
    report: Report, *, stack: StackFacts, persona: str, date: str, flows: Sequence[str]
) -> str:
    """The session's ``review.md``, in the recorder's skeleton format.

    ``ux_recorder_render`` reads the two-space-indented entries under
    ``Findings`` for the end card, so each section heading starts a line and
    each entry is indented and on one line. Every entry starts with an icon or
    the entry text itself, so none is mistaken for a heading.
    """
    shown = [sanitize(flow, ID_CHARS) for flow in flows]
    flow_note = f" — flows {', '.join(f for f in shown if f)}" if any(shown) else ""
    lines = [
        f"🖱️  UX review — {sanitize(stack.name, NAME_CHARS)}, "
        f"{sanitize(persona, NAME_CHARS)}, {sanitize(date, ID_CHARS)}{flow_note}",
        "",
        "Looked at",
    ]
    for flow in report.flows:
        icon = OUTCOME_ICONS.get(flow.outcome, OUTCOME_ICONS["not_covered"])
        summary = _card(flow.summary, SUMMARY_CHARS)
        lines.append(
            f"  {icon} {_card(flow.id, ID_CHARS)}  {_card(flow.title, TITLE_CHARS)}"
            + (f" — {summary}" if summary else "")
        )
    lines += [
        "",
        "Findings                                    "
        "(ranked; suggestion, not a demand)",
    ]
    findings = [_card(f.text, TEXT_CHARS) for f in report.findings[:MAX_FINDINGS]]
    lines += [f"  {text}" for text in findings if text] or ["  none"]
    lines += ["", "Functional breakage"]
    breakage = [_card(_breakage_line(b), 4 * TEXT_CHARS) for b in report.breakage]
    lines += [f"  {line}" for line in breakage if line] or ["  none"]
    lines += ["", "Not covered"]
    gaps = [
        ": ".join(
            part
            for part in (_card(n.flow, ID_CHARS), _card(n.reason, TEXT_CHARS))
            if part
        )
        for n in report.not_covered
    ]
    lines += [f"  {line}" for line in gaps if line] or ["  none"]
    lines += [
        "",
        "Chapters",
        "<!-- ux-recorder:chapters -->",
        "<!-- /ux-recorder:chapters -->",
    ]
    return "\n".join(lines) + "\n"
