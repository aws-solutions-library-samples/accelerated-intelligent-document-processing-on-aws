# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/story_report.py, a run's ``report.html``.

The run directories here are real ones: each journal is written with the story
server's own ``JournalWriter``, judged by the real judge, and ``results.json`` is
what ``story_verdict.py judge`` writes. What is pinned:

* every value is shown escaped and only as text, whatever a field holds, and the
  page holds no script, loads nothing else and carries its Content Security Policy;
* every ``src`` and ``href`` is an anchor on the page or a relative path to a
  regular file inside the run directory, and a crafted, absolute or linked path is
  never linked;
* the index is worst first and links each flow's section, the filters and the
  story list need no script, and the same files always make the same page;
* a run directory missing every optional file still makes a page, and the CLI
  writes it and says where.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
REPORT_PY = SCRIPTS / "ux_runner" / "story_report.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ux_story_mcp  # noqa: E402
from ux_runner import stories, story_report, story_verdict  # noqa: E402

SERIES = "5d0c1e9a2b7f"
HARNESS = "f23669def653f7c3a9606841cfc178c5456c1e8f"
MODEL = "us.anthropic.claude-opus-5-5"
EPOCH = 1760961234.5
CATALOGUE_SHA = "c" * 64
CATALOGUE: dict[str, Any] = {
    "version": 1,
    "personas": {
        "new-analyst": {"group": "Author", "background": "Joined the team this week."},
        "platform-admin": {"group": "Admin", "background": "Runs the platform."},
    },
    "stories": [
        {
            "id": "route-one-folder",
            "title": "Make one input folder use one configuration",
            "intent": "Have documents that land in one folder use one configuration.",
            "kind": "commit_point",
            "documented_steps": 2,
            "as": {"platform-admin": "allowed", "new-analyst": "not_offered"},
            "tier": "p0",
            "since": "0.6.0",
            "origin": "smoke",
        },
        {
            "id": "find-the-model",
            "title": "Find which model reads the fields",
            "intent": "Find out which model extracts the fields of a document.",
            "kind": "read",
            "documented_steps": 1,
            "as": {"new-analyst": "allowed"},
            "tier": "p0",
            "since": "0.6.0",
            "origin": "smoke",
        },
    ],
}
SCRIPT_TAG = "<script>alert('one')</script>"
ATTRIBUTE_BREAK = '"><img src=x onerror=alert(2)>'
JAVASCRIPT_URL = "javascript:alert(3)"
STYLE_BREAK = "</style><b>bold</b>"
SINGLE_QUOTE_BREAK = "' onmouseover='alert(5)"
HOSTILE = (SCRIPT_TAG, ATTRIBUTE_BREAK, JAVASCRIPT_URL, STYLE_BREAK, SINGLE_QUOTE_BREAK)
PAGE_TAGS = frozenset(
    {
        "html",
        "head",
        "meta",
        "title",
        "style",
        "body",
        "input",
        "header",
        "h1",
        "h2",
        "h3",
        "h4",
        "dl",
        "dt",
        "dd",
        "nav",
        "a",
        "main",
        "section",
        "div",
        "span",
        "label",
        "small",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "ul",
        "ol",
        "li",
        "code",
        "strong",
        "br",
        "article",
        "p",
        "blockquote",
        "figure",
        "figcaption",
        "video",
        "img",
        "details",
        "summary",
        "pre",
        "footer",
    }
)


class Page(HTMLParser):
    """Every tag with its attributes, and the text a reader sees, decoded."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.data: list[str] = []
        self._style = False
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._style = tag == "style"

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        self._style = False

    def handle_data(self, data: str) -> None:
        if not self._style:
            self.data.append(data)

    @property
    def text(self) -> str:
        return "".join(self.data)

    def attr(self, name: str) -> list[str]:
        return [
            value
            for _, attrs in self.tags
            for key, value in attrs.items()
            if key == name and value is not None
        ]

    def ids(self) -> set[str]:
        return set(self.attr("id"))

    def of(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


def known_catalogue() -> story_report.CatalogueText:
    return story_report.CatalogueText(stories.parse_catalogue(CATALOGUE), CATALOGUE_SHA)


def story_hash(story: str) -> str:
    catalogue = stories.parse_catalogue(CATALOGUE)
    return stories.story_hash(catalogue, catalogue.story(story))


class Flow:
    """One flow attempt of the synthetic run: what its journal says it did."""

    def __init__(
        self,
        story: str,
        persona: str,
        *,
        attempt: int = 1,
        outcome: str = "done",
        actions: int = 3,
        budget: int = 30,
        video: str | None = None,
        notes: bool = True,
        help_link: bool = False,
        what: str = "Where is the upload?",
        why: str = "Two controls look alike.",
        evidence: str = "The page says the mapping was saved.",
        story_hash_value: str | None = None,
        scale: int = 1,
        session: bool = True,
    ) -> None:
        self.scale = scale
        self.session = session
        self.story = story
        self.persona = persona
        self.key = f"{story}@{persona}"
        self.attempt = attempt
        self.outcome = outcome
        self.actions = actions
        self.budget = budget
        self.video = video
        self.notes = notes
        self.help_link = help_link
        self.what = what
        self.why = why
        self.evidence = evidence
        self.story_hash = story_hash_value or story_hash(story)

    @property
    def catalogue_story(self) -> stories.Story:
        return stories.parse_catalogue(CATALOGUE).story(self.story)

    @property
    def group(self) -> str:
        return CATALOGUE["personas"][self.persona]["group"]

    @property
    def expectation(self) -> str:
        return self.catalogue_story.expectation_for(self.persona)

    @property
    def folder(self) -> str:
        return story_verdict.journal_path(self.key, self.attempt).rsplit("/", 1)[0]

    def start(self) -> dict[str, Any]:
        return {
            "story": self.story,
            "story_hash": self.story_hash,
            "series": SERIES,
            "persona": self.persona,
            "group": self.group,
            "expectation": self.expectation,
            "kind": self.catalogue_story.kind,
            "attempt": self.attempt,
            "docs_ref": "worktree",
            "corpus_hash": "abcdef012345",
            "harness": HARNESS,
            "model": MODEL,
            "effort": "high",
            "mode": "unattended",
        }

    def write(self, run_dir: Path) -> None:
        folder = run_dir / self.folder
        (folder / "screens").mkdir(parents=True)
        writer = ux_story_mcp.JournalWriter(
            folder / "journal.jsonl",
            self.start(),
            relay=True,
            budget=self.budget,
            wall=lambda: EPOCH,
        )
        tools = ("navigate_page", "click", "fill", "click", "click", "press_key")
        used = 0
        for index in range(self.actions):
            used += 1
            name = f"{used:03d}.jpg"
            (folder / "screens" / name).write_bytes(b"\xff\xd8\xff" + bytes(64))
            writer.call(
                tool=tools[index % len(tools)],
                cls="action",
                ok=True,
                counted=True,
                actions_used=used,
                images_sent=used,
                screen=f"screens/{name}",
            )
            if self.notes and used == 2:
                writer.call(
                    tool="note_confusion",
                    cls="note",
                    ok=True,
                    args={
                        "what": self.what,
                        "why": self.why,
                        "docs_ref": "web-ui.md#upload-documents",
                    },
                    actions_used=used,
                    images_sent=used,
                )
        if self.notes:
            writer.call(
                tool="note_dead_end",
                cls="note",
                ok=True,
                args={"what": "The Settings page", "why": "It holds no mapping."},
                actions_used=used,
                images_sent=used,
            )
        if self.help_link:
            writer.call(
                tool="read_documentation",
                cls="doc",
                ok=False,
                label="help_link_unresolved",
                args={"ref": "/no-such-page/", "part": 1},
                actions_used=used,
                images_sent=used,
            )
        if self.outcome == "done":
            writer.call(
                tool="report_done",
                cls="report",
                ok=True,
                args={"evidence": self.evidence, "stopped_at_commit_point": True},
                actions_used=used,
                images_sent=used,
                terminal="done",
                after_budget=False,
            )
        elif self.outcome in ("blocked", "offered"):
            writer.call(
                tool="report_blocked",
                cls="report",
                ok=True,
                args={
                    "reason": "Nothing on the page does it.",
                    "kind": "no_route",
                    "offered_a_control": self.outcome == "offered",
                },
                actions_used=used,
                images_sent=used,
                terminal="blocked",
                after_budget=False,
            )
        writer.end("terminal" if self.outcome != "silent" else "eof")
        (folder / "report.md").write_text(
            f"# What I did\n\nI tried {self.story}.\n", encoding="utf-8"
        )
        (folder / "audit.json").write_text('{"state": "clean"}\n', encoding="utf-8")
        (folder / "refusals.jsonl").write_text("", encoding="utf-8")
        if self.video is not None:
            (run_dir / self.video).write_bytes(b"\x00\x00\x00\x18ftypmp42")

    def context(self) -> story_verdict.FlowContext:
        story = self.catalogue_story
        return story_verdict.FlowContext(
            key=self.key,
            story=self.story,
            persona=self.persona,
            group=self.group,
            title=story.title,
            expectation=self.expectation,
            kind=story.kind,
            documented_steps=story.documented_steps,
            needs=(),
            declared_docs=("web-ui.md#upload-documents",),
            series=SERIES,
            budget=self.budget,
            attempt=self.attempt,
            budget_scale=self.scale,
            journal=story_verdict.journal_path(self.key, self.attempt),
            audit_state="clean",
            session=None
            if not self.session
            else story_verdict.SessionOutcome(
                started_epoch=EPOCH,
                ended_epoch=EPOCH + 245.0,
                timed_out=False,
                returncode=0,
                stop="end_turn",
                is_error=False,
                turns=21,
                duration_s=240.0,
                cost_usd=1.25,
                cost_usd_pinned=1.31,
                counted_usd=1.31,
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 2000,
                    "cache_read_input_tokens": 100000,
                    "cache_creation_input_tokens": 10000,
                },
            ),
            video=self.video,
            chapters=(("0:00", "Opened the application"),) if self.video else (),
        )


def record(flows: list[Flow], **changes: Any) -> dict[str, Any]:
    contexts = sorted(
        (flow.context() for flow in flows), key=lambda c: (c.key, c.attempt)
    )
    value: dict[str, Any] = {
        "schema": story_verdict.RUN_SCHEMA,
        "version": story_verdict.RUN_VERSION,
        "mode": "local",
        "shard": None,
        "harness_sha": HARNESS,
        "harness_dirty": False,
        "catalogue_sha256": CATALOGUE_SHA,
        "prompt_revision": 3,
        "judgement_revision": story_verdict.JUDGEMENT_REVISION,
        "relay_revision": 2,
        "model": MODEL,
        "effort": "high",
        "region": "us-east-1",
        "confinement": "full",
        "versions": {"claude_code": "2.1.0", "chrome": "155.0.0.1", "node": "v22"},
        "stack": {
            "name": "idp-ux-nightly",
            "version": "0.6.12.dev1",
            "build": "2026-10-19",
            "review_stack": True,
        },
        "stack_state": None,
        "stack_changed_mid_run": False,
        "ui_source": {"kind": "deployed"},
        "docs": {
            "ref": None,
            "corpus_hash": "abcdef012345",
            "source": "worktree",
            "dirty": False,
        },
        "limits": {
            "step_budget_multiple": 10,
            "step_budget_floor": 30,
            "documented_steps_max": 6,
            "flow_usd": 4.0,
            "flow_minutes": 12,
        },
        "flows": [context.to_json() for context in contexts],
        "credential_scope": {"claude": "scoped", "render": None},
        "video_withheld": None,
        "video_withheld_flows": [],
        "spend_usd": round(1.31 * len(flows), 6),
        "reruns_not_run": [],
        "reply": {"caveats": ["one machine's attempt per story"], "video_notes": []},
    }
    value.update(changes)
    return value


def build(run_dir: Path, flows: list[Flow], **changes: Any) -> Path:
    """A judged run directory holding ``flows``, as a run leaves one."""
    run_dir.mkdir(parents=True)
    for flow in flows:
        flow.write(run_dir)
    (run_dir / story_verdict.RUN_FILE).write_text(
        json.dumps(record(flows, **changes), indent=2), encoding="utf-8"
    )
    results, found = story_verdict.judge_run(run_dir)
    (run_dir / story_verdict.RESULTS_FILE).write_text(
        story_verdict.dump_json(story_verdict.results_document(results, found)),
        encoding="utf-8",
    )
    return run_dir


def three_flows() -> list[Flow]:
    return [
        Flow("route-one-folder", "platform-admin", video="ux-story-route--admin-1.mp4"),
        Flow("route-one-folder", "new-analyst", outcome="offered", help_link=True),
        Flow("route-one-folder", "new-analyst", attempt=2, outcome="offered"),
        Flow("find-the-model", "new-analyst", outcome="blocked", notes=False),
    ]


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return build(tmp_path / "ux-test-stories", three_flows())


def _render(run_dir: Path, catalogue: Any = None) -> str:
    return story_report.render(run_dir, catalogue=catalogue or known_catalogue())


def _results(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / story_verdict.RESULTS_FILE).read_text("utf-8"))


def _write_results(run_dir: Path, document: dict[str, Any]) -> None:
    (run_dir / story_verdict.RESULTS_FILE).write_text(
        json.dumps(document), encoding="utf-8"
    )


def _references(page: Page) -> list[str]:
    return page.attr("href") + page.attr("src")


def _section(text: str, anchor: str) -> str:
    """One flow's article, as the page writes it."""
    start = text.index(f'id="{anchor}"')
    return text[start : text.index("</article>", start)]


def _assert_confined(page: Page, run_dir: Path) -> None:
    ids = page.ids()
    root = run_dir.resolve()
    for value in _references(page):
        if value.startswith("#"):
            assert value[1:] in ids, value
            continue
        parts = urlsplit(value)
        assert not parts.scheme and not parts.netloc, value
        assert not parts.query and not parts.fragment, value
        path = unquote(parts.path)
        assert path and not path.startswith("/") and "\\" not in path, value
        assert ".." not in path.split("/"), value
        current = run_dir
        for part in path.split("/"):
            current = current / part
            assert not current.is_symlink(), value
        assert current.is_file(), value
        assert current.resolve().is_relative_to(root), value


# --- the page and its links ----------------------------------------------------


@pytest.mark.unit
def test_the_page_has_its_policy_and_no_script_or_outside_resource(
    run_dir: Path,
) -> None:
    text = _render(run_dir)
    page = Page(text)
    (policy,) = [
        attrs
        for attrs in page.of("meta")
        if attrs.get("http-equiv") == "Content-Security-Policy"
    ]
    assert policy["content"] == story_report.CONTENT_SECURITY_POLICY
    for directive in (
        "default-src 'none'",
        "img-src 'self'",
        "media-src 'self'",
        "style-src 'unsafe-inline'",
        "base-uri 'none'",
        "form-action 'none'",
    ):
        assert directive in story_report.CONTENT_SECURITY_POLICY
    assert "<script" not in text.lower()
    assert {name for name, _ in page.tags} <= PAGE_TAGS
    assert not [
        key for _, attrs in page.tags for key in attrs if key.lower().startswith("on")
    ]
    assert not re.search(
        r"(?i)\b(?:https?:|data:|javascript:|//)", " ".join(_references(page))
    )
    assert text.count("<style>") == 1 and text.count("</style>") == 1
    assert text.startswith("<!DOCTYPE html>\n")
    escaped = html.escape(story_report.CONTENT_SECURITY_POLICY, quote=True)
    assert f'<meta http-equiv="Content-Security-Policy" content="{escaped}">' in text


@pytest.mark.unit
def test_every_src_and_href_is_an_anchor_or_a_file_inside_the_run(
    run_dir: Path,
) -> None:
    page = Page(_render(run_dir))
    _assert_confined(page, run_dir)
    sources = set(page.attr("src"))
    assert "ux-story-route--admin-1.mp4" in sources
    assert "flows/route-one-folder@platform-admin/screens/001.jpg" in sources
    assert "flows/route-one-folder@new-analyst/attempt-2/screens/003.jpg" in sources
    links = set(page.attr("href"))
    for name in story_report.ATTEMPT_FILES:
        assert f"flows/find-the-model@new-analyst/{name}" in links


@pytest.mark.unit
def test_the_index_is_worst_first_and_every_flow_has_its_anchor(run_dir: Path) -> None:
    page = Page(_render(run_dir))
    rows = [
        attrs["class"] or ""
        for attrs in page.of("tr")
        if "row" in (attrs.get("class") or "").split()
    ]
    verdicts = [
        next(name[2:] for name in row.split() if name.startswith("v-")) for row in rows
    ]
    assert verdicts == ["stuck", "offered", "done"]
    order = story_verdict.VERDICT_ORDER
    assert verdicts == sorted(verdicts, key=order.index)
    articles = [attrs["id"] for attrs in page.of("article")]
    assert articles == [
        "flow-find-the-model--new-analyst",
        "flow-route-one-folder--new-analyst",
        "flow-route-one-folder--platform-admin",
    ]
    index_links = [value for value in page.attr("href") if value.startswith("#flow-")]
    for anchor in articles:
        assert f"#{anchor}" in index_links


@pytest.mark.unit
def test_a_verdict_is_never_shown_by_colour_alone(run_dir: Path) -> None:
    text = _render(run_dir)
    page = Page(text)
    assert "OFFERED a control the docs say this role lacks (2 of 2)" in (page.text)
    assert "STUCK: no route (unconfirmed (1 of 1))" in page.text
    assert "DONE, stopped before saving" in page.text
    badges = re.findall(
        r'<span class="verdict tone-[a-z]+"><span aria-hidden="true">([^<]+)</span> ([^<]+)</span>',
        text,
    )
    assert badges and all(icon.strip() and label.strip() for icon, label in badges)


@pytest.mark.unit
def test_the_filters_are_radios_and_css_with_a_rule_for_each_choice(
    run_dir: Path,
) -> None:
    text = _render(run_dir)
    page = Page(text)
    radios = [attrs for attrs in page.of("input") if attrs.get("type") == "radio"]
    names = {attrs["id"]: attrs["name"] for attrs in radios}
    assert names == {
        "fv-_all": "verdict",
        "fv-offered": "verdict",
        "fv-stuck": "verdict",
        "fv-done": "verdict",
        "fp-_all": "persona",
        "fp-new-analyst": "persona",
        "fp-platform-admin": "persona",
        "fs-_all": "story",
        "fs-find-the-model": "story",
        "fs-route-one-folder": "story",
    }
    assert [attrs["id"] for attrs in radios if "checked" in attrs] == [
        "fv-_all",
        "fp-_all",
        "fs-_all",
    ]
    labels = {attrs.get("for") for attrs in page.of("label")}
    assert labels == set(names)
    for ident in names:
        if not ident.endswith("_all"):
            cls = ident.replace("fv-", "v-").replace("fp-", "p-").replace("fs-", "s-")
            assert f"#{ident}:checked~main .f:not(.{cls}){{display:none}}" in text
    flows = [attrs["class"] or "" for attrs in page.of("article")]
    assert all({"f"} <= set(c.split()) for c in flows)
    assert text.index('class="filter"') < text.index("<main>")


@pytest.mark.unit
def test_the_flows_are_listed_by_story_as_well(run_dir: Path) -> None:
    page = Page(_render(run_dir))
    text = page.text
    by_story = text[text.index("By story") :]
    assert by_story.index("Find which model reads the fields") < by_story.index(
        "Make one input folder use one configuration"
    )


@pytest.mark.unit
def test_a_flow_shows_its_story_its_notes_by_step_and_its_screens(
    run_dir: Path,
) -> None:
    text = _render(run_dir)
    page = Page(text)
    shown = page.text
    assert "Have documents that land in one folder use one configuration." in shown
    assert "Joined the team this week." in shown
    assert "Where is the upload?" in shown and "Two controls look alike." in shown
    assert "web-ui.md#upload-documents" in shown
    assert "step 2 · screen 002" in shown
    assert "Screen 001 · step 1 · navigate_page" in shown
    assert "The page says the mapping was saved." in shown
    assert "/no-such-page/" in shown
    assert "in 3 of 30 steps" in shown
    assert "Opened the application" in shown
    assert "# What I did" in shown
    images = page.of("img")
    assert images and all(attrs.get("loading") == "lazy" for attrs in images)
    assert (
        '<a href="flows/route-one-folder@platform-admin/screens/002.jpg">screen 002</a>'
        in text
    )
    assert not [href for href in page.attr("href") if "no-such-page" in href]


@pytest.mark.unit
def test_the_header_and_the_footer_hold_the_runs_facts_and_cost(
    run_dir: Path,
) -> None:
    shown = Page(_render(run_dir)).text
    totals = story_verdict.cost_totals(
        [story_verdict.FlowResult.from_json(f) for f in _results(run_dir)["flows"]]
    )
    assert story_verdict.cost_text(totals) in shown
    assert "2025-10-20 11:53 to 11:57 UTC" in shown
    assert "idp-ux-nightly, version 0.6.12.dev1, build 2026-10-19" in shown
    assert MODEL in shown and "effort high" in shown
    assert "3 flows, 4 attempts" in shown
    assert "one machine's attempt per story" in shown
    assert "4 sessions" not in shown and "0 sessions without usage" in shown


@pytest.mark.unit
def test_a_watched_sessions_cost_is_a_row_of_its_own_and_in_the_totals(
    tmp_path: Path,
) -> None:
    watched = {
        "assisted": True,
        "counted_usd": 2.5,
        "cost_usd_pinned": 2.25,
        "cli_cost_usd": 2.5,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "started_epoch": EPOCH - 600.0,
        "ended_epoch": EPOCH - 60.0,
    }
    run = build(
        tmp_path / "run", [Flow("find-the-model", "new-analyst")], interactive=watched
    )
    shown = Page(story_report.render(run)).text
    assert "the watched session, assisted" in shown
    assert "$3.81 counted, $3.56 at the pinned rates" in shown
    assert "2025-10-20 11:43 to 11:57 UTC" in shown
    record = json.loads((run / story_verdict.RUN_FILE).read_text("utf-8"))
    record["interactive"]["assisted"] = None
    (run / story_verdict.RUN_FILE).write_text(json.dumps(record), encoding="utf-8")
    assert "the watched session, typing unknown" in Page(story_report.render(run)).text


@pytest.mark.unit
def test_the_intent_is_shown_only_from_the_runs_own_catalogue(
    run_dir: Path,
) -> None:
    intent = "Have documents that land in one folder use one configuration."
    assert intent in Page(_render(run_dir)).text
    other = story_report.CatalogueText(stories.parse_catalogue(CATALOGUE), "d" * 64)
    assert intent in Page(_render(run_dir, other)).text
    changed = json.loads(json.dumps(CATALOGUE))
    changed["stories"][0]["intent"] = "Something else to do with a folder."
    reworded = story_report.CatalogueText(stories.parse_catalogue(changed), "d" * 64)
    shown = Page(_render(run_dir, reworded)).text
    assert "Something else to do with a folder." not in shown
    assert "words the story differently from the run's" in shown
    same = story_report.CatalogueText(stories.parse_catalogue(changed), CATALOGUE_SHA)
    assert "Something else to do with a folder." in Page(_render(run_dir, same)).text
    plain = story_report.render(run_dir)
    assert "The catalogue could not be read, so no intent is shown." in plain


_OUTCOMES = [
    (
        "not_refused",
        None,
        [],
        "Reported done although the docs say this role cannot do it in 3 of 30 steps.",
    ),
    (
        "stuck",
        "no_route",
        [],
        "Reported blocked (no route) in 3 of 30 steps, where the docs say this role can do it.",
    ),
    (
        "offered",
        None,
        ["unproven"],
        "nothing was pressed, so whether it is refused was not tested.",
    ),
    (
        "offered",
        None,
        [],
        "Reported blocked, and that the screen offered a control the docs say this role lacks.",
    ),
    (
        "out_of_steps",
        None,
        [],
        "Reported blocked once the step budget had run out in 3 of 30 steps.",
    ),
    (
        "error",
        "launch",
        [],
        "A harness error (launch), which says nothing about the UI.",
    ),
    ("gave_up", "wall_clock", [], "Ended without a report: the wall clock ran out."),
    ("unclear", None, [], "Ended with no report, for no reason the journal records."),
    ("not_run", "since", [], "Not run: the stack is older than the story."),
    (
        "refused_as_expected",
        None,
        [],
        "Reported blocked, as the docs say this role is in 3 of 30 steps.",
    ),
    (
        "done",
        None,
        ["done_after_budget", "stopped_at_commit_point"],
        "Reported done in 3 of 30 steps, stopping before it saved anything, after the step budget had run out.",
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize(("verdict", "sub_kind", "flags", "sentence"), _OUTCOMES)
def test_every_verdict_reads_as_its_own_outcome(
    run_dir: Path, verdict: str, sub_kind: str | None, flags: list[str], sentence: str
) -> None:
    """The sentence under each flow's heading is made from the verdict's closed-set
    values alone, one for every verdict there is."""
    document = _results(run_dir)
    (flow,) = [f for f in document["flows"] if f["key"] == "find-the-model@new-analyst"]
    flow.update(
        verdict=verdict,
        sub_kind=sub_kind,
        flags=flags,
        met_expectation=story_verdict.VERDICT_MEETS[verdict],
    )
    _write_results(run_dir, document)
    text = story_report.render(run_dir)
    article = Page(_section(text, "flow-find-the-model--new-analyst")).text
    assert sentence in article
    result = story_verdict.FlowResult.from_json(flow)
    label = story_verdict.verdict_label(result, story_verdict.confirm([result]))
    assert label in article


@pytest.mark.unit
def test_every_verdict_of_the_judge_has_an_outcome_here() -> None:
    assert {verdict for verdict, *_ in _OUTCOMES} == set(story_verdict.VERDICT_ORDER)


@pytest.mark.unit
def test_the_header_says_what_the_record_says_of_its_run(tmp_path: Path) -> None:
    flows = [Flow("find-the-model", "new-analyst")]
    run = build(
        tmp_path / "run",
        flows,
        shard={"index": 2, "total": 5},
        replayed="earlier-run",
        confinement="dev-unconfined",
        harness_dirty=True,
        stack_state="updated",
        stack_changed_mid_run=True,
        ui_source={"kind": "build", "head_sha": "a" * 40, "src_ui_tree": "b" * 40},
        docs={
            "ref": None,
            "corpus_hash": "abcdef012345",
            "source": "worktree",
            "dirty": True,
        },
    )
    record = json.loads((run / story_verdict.RUN_FILE).read_text("utf-8"))
    record["stack"]["review_stack"] = False
    session = record["flows"][0]["session"]
    session["ended_epoch"] = session["started_epoch"] + 7260.0
    (run / story_verdict.RUN_FILE).write_text(json.dumps(record), encoding="utf-8")
    document = _results(run)
    document["judgement_revision"] = story_verdict.JUDGEMENT_REVISION + 1
    _write_results(run, document)
    shown = Page(story_report.render(run)).text
    for words in (
        "local, shard 2 of 5, a replay of earlier-run, confinement dev-unconfined",
        "the working tree, with uncommitted changes, corpus abcdef012345",
        f"a UI build of {'b' * 40}",
        f"{HARNESS} with uncommitted changes; judged under revision "
        f"{story_verdict.JUDGEMENT_REVISION + 1}, which this checkout is not: it "
        f"judges under {story_verdict.JUDGEMENT_REVISION}",
        "not confirmed as the review stack, state updated, its build changed during "
        "the run",
        "2 h 1 min",
    ):
        assert words in shown, words
    docs = dict(record["docs"], ref="1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d")
    record.update(docs=docs, ui_source={"kind": "deployed"})
    (run / story_verdict.RUN_FILE).write_text(json.dumps(record), encoding="utf-8")
    shown = Page(story_report.render(run)).text
    assert "git at 1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d" in shown
    assert "the stack's own deployed UI" in shown


@pytest.mark.unit
@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("[]", "story-run.json is not a JSON object"),
        (
            json.dumps({"schema": story_verdict.RUN_SCHEMA, "version": 1}),
            "story-run.json: 'judgement_revision' must be a whole number",
        ),
    ],
)
def test_a_run_record_that_is_not_v1_is_named_and_the_page_is_still_made(
    run_dir: Path, contents: str, message: str
) -> None:
    (run_dir / story_verdict.RUN_FILE).write_text(contents, encoding="utf-8")
    page = Page(story_report.render(run_dir))
    assert message in page.text
    assert "3 flows, 4 attempts" in page.text
    _assert_confined(page, run_dir)


@pytest.mark.unit
def test_an_unreadable_journal_costs_its_steps_and_nothing_else(run_dir: Path) -> None:
    folder = run_dir / "flows" / "route-one-folder@platform-admin"
    (folder / "journal.jsonl").write_text("not a journal\n", encoding="utf-8")
    article = Page(
        _section(story_report.render(run_dir), "flow-route-one-folder--platform-admin")
    ).text
    assert "Screen 001" in article and "Screen 001 · step" not in article
    assert "Where is the upload?" in article and "step 2 · screen" not in article


@pytest.mark.unit
def test_notes_the_journal_does_not_account_for_claim_no_place(run_dir: Path) -> None:
    document = _results(run_dir)
    (flow,) = [
        f for f in document["flows"] if f["key"] == "route-one-folder@platform-admin"
    ]
    flow["notes"]["confusion"].append(
        {"what": "A second hesitation", "why": "Not in the journal.", "docs_ref": None}
    )
    _write_results(run_dir, document)
    article = Page(
        _section(story_report.render(run_dir), "flow-route-one-folder--platform-admin")
    ).text
    assert "A second hesitation" in article and "Where is the upload?" in article
    assert "step 2 · screen" not in article
    assert "step 3 · screen 003" in article


@pytest.mark.unit
def test_the_videos_reruns_and_withheld_flows_of_the_record_are_caveats(
    tmp_path: Path,
) -> None:
    flows = [Flow("find-the-model", "new-analyst")]
    run = build(
        tmp_path / "run",
        flows,
        video_withheld_flows=[
            {
                "key": "find-the-model@new-analyst",
                "attempt": 1,
                "reason": "it is not the review stack",
            }
        ],
        reruns_not_run=[{"key": "find-the-model@new-analyst", "reason": "budget"}],
        reply={
            "caveats": [],
            "video_notes": [
                {"key": "a@b", "kind": "upload_failed", "detail": ""},
                {"key": "c@d", "kind": "over_limit", "detail": "120"},
                {"key": "e@f", "kind": "not_rendered", "detail": ""},
                {"key": "g@h", "kind": "local", "detail": "ux-story-g--h-1.mp4"},
            ],
        },
    )
    shown = Page(story_report.render(run)).text
    for words in (
        "the video of find-the-model@new-analyst was withheld: it is not the review stack",
        "find-the-model@new-analyst was not run again (budget), so its bad result "
        "stays unconfirmed",
        "the video of a@b could not be uploaded",
        "the video of c@d, 120 MB, was over the upload limit",
        "the video of e@f could not be rendered",
    ):
        assert words in shown, words
    assert "g@h" not in shown


@pytest.mark.unit
def test_the_models_account_is_cut_at_its_bound_and_skipped_past_it(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = run_dir / "flows" / "find-the-model@new-analyst"
    (folder / "report.md").write_text("x" * 70_000, encoding="utf-8")
    article = _section(story_report.render(run_dir), "flow-find-the-model--new-analyst")
    assert "Cut here; the whole of it is in report.md." in article
    monkeypatch.setattr(story_report, "_REPORT_MD_MAX_BYTES", 10)
    article = _section(story_report.render(run_dir), "flow-find-the-model--new-analyst")
    assert "the model's own account" not in article
    assert 'href="flows/find-the-model@new-analyst/report.md"' in article


@pytest.mark.unit
def test_a_rerun_at_twice_the_budget_and_a_flow_without_report_or_session(
    tmp_path: Path,
) -> None:
    flows = [
        Flow("route-one-folder", "platform-admin", outcome="silent"),
        Flow("route-one-folder", "platform-admin", attempt=2, scale=2, budget=60),
        Flow("find-the-model", "new-analyst", outcome="silent", session=False),
    ]
    text = story_report.render(build(tmp_path / "run", flows))
    admin = Page(_section(text, "flow-route-one-folder--platform-admin")).text
    assert "Judged from its re-run at twice the step budget." in admin
    assert "Attempt 2, budget ×2" in admin and "3 of 60" in admin
    assert "No report: the session ended without report_done or report_blocked" in (
        admin
    )
    analyst = Page(_section(text, "flow-find-the-model--new-analyst")).text
    assert "Ended with no report" in analyst and "not reported" in analyst
    assert "no session" in Page(text).text


@pytest.mark.unit
def test_a_run_with_no_flow_still_makes_a_page(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    _write_results(
        run,
        story_verdict.results_document([], []),
    )
    page = Page(story_report.render(run))
    assert "0 flows, 0 attempts" in page.text and "No flow." in page.text
    assert not page.of("article")


# --- hostile text --------------------------------------------------------------


def _hostile_results(run_dir: Path) -> None:
    """Every text field of results.json set to raw markup the judge never wrote."""
    document = _results(run_dir)
    for index, flow in enumerate(document["flows"]):
        text = HOSTILE[index % len(HOSTILE)]
        flow["title"] = f"{text} title"
        flow["help_links_unresolved"] = [JAVASCRIPT_URL, ATTRIBUTE_BREAK]
        flow["commit_points"] = [SCRIPT_TAG]
        flow["chapters"] = [[STYLE_BREAK, SCRIPT_TAG]]
        flow["video"] = ATTRIBUTE_BREAK
        for kind in ("confusion", "dead_end"):
            for note in flow["notes"][kind]:
                note["what"], note["why"] = SCRIPT_TAG, ATTRIBUTE_BREAK
                if kind == "confusion":
                    note["docs_ref"] = JAVASCRIPT_URL
        if flow["terminal"] is not None:
            flow["terminal"]["evidence"] = STYLE_BREAK
            flow["terminal"]["reason"] = SINGLE_QUOTE_BREAK
    for finding in document["findings"]:
        finding["title"], finding["body"] = SCRIPT_TAG, ATTRIBUTE_BREAK
    _write_results(run_dir, document)


def _hostile_record(run_dir: Path) -> None:
    path = run_dir / story_verdict.RUN_FILE
    value = json.loads(path.read_text("utf-8"))
    value["model"] = ATTRIBUTE_BREAK
    value["mode"] = SCRIPT_TAG
    value["harness_sha"] = STYLE_BREAK
    value["stack"] = {
        "name": SCRIPT_TAG,
        "version": ATTRIBUTE_BREAK,
        "build": JAVASCRIPT_URL,
        "review_stack": True,
    }
    value["docs"]["ref"] = SINGLE_QUOTE_BREAK
    value["versions"] = {"claude_code": STYLE_BREAK}
    value["reply"]["caveats"] = list(HOSTILE)
    value["video_withheld_flows"] = [{"key": SCRIPT_TAG, "reason": ATTRIBUTE_BREAK}]
    value["reruns_not_run"] = [{"key": STYLE_BREAK, "reason": SCRIPT_TAG}]
    path.write_text(json.dumps(value), encoding="utf-8")
    for report in run_dir.rglob("report.md"):
        report.write_text("\n".join(HOSTILE) + "\n\u202ereversed\x1b[31m", "utf-8")


def _assert_only_text(text: str) -> Page:
    page = Page(text)
    assert "<script" not in text.lower()
    assert "<b>bold</b>" not in text
    assert text.count("</style>") == 1
    assert {name for name, _ in page.tags} <= PAGE_TAGS
    for _, attrs in page.tags:
        for key, value in attrs.items():
            assert not key.lower().startswith("on"), key
            assert "javascript:" not in (value or "").lower(), value
            assert "onerror" not in (value or "").lower(), value
    assert not [attrs for attrs in page.of("img") if attrs.get("src") == "x"]
    return page


@pytest.mark.unit
def test_raw_markup_in_any_results_field_is_only_ever_text(run_dir: Path) -> None:
    _hostile_results(run_dir)
    text = _render(run_dir)
    page = _assert_only_text(text)
    for value in HOSTILE:
        assert value in page.text, value
        assert html.escape(value, quote=True) in text, value
    _assert_confined(page, run_dir)


@pytest.mark.unit
def test_raw_markup_in_the_run_record_and_the_models_report_is_only_text(
    run_dir: Path,
) -> None:
    _hostile_record(run_dir)
    text = _render(run_dir)
    page = _assert_only_text(text)
    for value in HOSTILE:
        assert value in page.text, value
    assert "\u202e" not in text and "\x1b" not in text
    _assert_confined(page, run_dir)


@pytest.mark.unit
def test_page_text_the_judge_sanitised_reads_as_written_and_stays_text(
    tmp_path: Path,
) -> None:
    """The judge writes ``<`` as ``&lt;`` and breaks links with zero-width spaces
    for GitLab; the page shows what the model wrote, escaped."""
    flows = [
        Flow(
            "route-one-folder",
            "platform-admin",
            what=SCRIPT_TAG,
            why=f"See [the docs]({JAVASCRIPT_URL}) and @all",
            evidence=ATTRIBUTE_BREAK,
        )
    ]
    run = build(tmp_path / "run", flows)
    document = _results(run)
    note = document["flows"][0]["notes"]["confusion"][0]
    assert "&lt;script>" in note["what"] and "\u200b" in note["why"]
    page = _assert_only_text(_render(run))
    assert SCRIPT_TAG in page.text
    assert f"See [the docs]({JAVASCRIPT_URL}) and @all" in page.text
    assert ATTRIBUTE_BREAK in page.text
    assert "\u200b" not in page.text and "&lt;" not in page.text


# --- paths -----------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "../outside.mp4",
        "../../outside.mp4",
        "/etc/outside.mp4",
        "sub/inner.mp4",
        "inner.mp4/",
        ".hidden.mp4",
        "inner.mp4\x00.mp4",
        "inner.MP4",
    ],
)
def test_a_crafted_video_name_is_never_linked(tmp_path: Path, name: str) -> None:
    run = build(tmp_path / "run", three_flows())
    (tmp_path / "outside.mp4").write_bytes(b"\0")
    (run / "sub").mkdir()
    (run / "sub" / "inner.mp4").write_bytes(b"\0")
    document = _results(run)
    for flow in document["flows"]:
        flow["video"] = name
    _write_results(run, document)
    page = Page(_render(run))
    assert not [value for value in _references(page) if "mp4" in value.lower()]
    assert not page.of("video")
    _assert_confined(page, run)
    assert "that is not in this directory" in page.text


@pytest.mark.unit
@pytest.mark.parametrize(
    "screen",
    ["../../outside.jpg", "/etc/outside.jpg", "screens/../../outside.jpg", "x/001.jpg"],
)
def test_a_crafted_screen_in_the_journal_is_never_linked_named_or_counted(
    tmp_path: Path, screen: str
) -> None:
    """A journal is read as v1 whatever its ``screen`` holds, so only a name of the
    writer's own shape is a screen: a crafted one is never a link, never shown as
    the place a note was written, and never counted as a screen the run lacks."""
    run = build(tmp_path / "run", three_flows())
    (tmp_path / "outside.jpg").write_bytes(b"\xff\xd8\xff")
    journal = run / "flows" / "route-one-folder@platform-admin" / "journal.jsonl"
    text = journal.read_text("utf-8").replace('"screens/002.jpg"', json.dumps(screen))
    journal.write_text(text, encoding="utf-8")
    assert story_verdict.read_journal(journal).calls[1].screen == screen
    shown = _render(run)
    page = Page(shown)
    _assert_confined(page, run)
    assert not [value for value in _references(page) if "outside" in value]
    assert screen.removesuffix(".jpg") not in page.text
    assert "more screen" not in page.text and "the journals name" not in page.text
    admin = Page(_section(shown, "flow-route-one-folder--platform-admin")).text
    assert "step 2 · screen 001" in admin
    assert "Screen 002 · step" not in admin and "Screen 002" in admin


@pytest.mark.unit
def test_a_link_inside_the_run_is_never_followed(tmp_path: Path) -> None:
    run = build(tmp_path / "run", three_flows())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jpg").write_bytes(b"\xff\xd8\xff")
    (outside / "report.md").write_text("OUTSIDE-REPORT", encoding="utf-8")
    admin = run / "flows" / "route-one-folder@platform-admin"
    screens = admin / "screens"
    (screens / "001.jpg").unlink()
    os.symlink(outside / "secret.jpg", screens / "001.jpg")
    (admin / "report.md").unlink()
    os.symlink(outside / "report.md", admin / "report.md")
    (run / "ux-story-route--admin-1.mp4").unlink()
    os.symlink(outside / "secret.jpg", run / "ux-story-route--admin-1.mp4")
    analyst = run / "flows" / "find-the-model@new-analyst"
    shutil.move(str(analyst), str(outside / "moved"))
    os.symlink(outside / "moved", analyst)
    page = Page(_render(run))
    _assert_confined(page, run)
    references = _references(page)
    assert "flows/route-one-folder@platform-admin/screens/001.jpg" not in references
    assert "flows/route-one-folder@platform-admin/screens/002.jpg" in references
    assert "ux-story-route--admin-1.mp4" not in references
    assert not [
        value
        for value in references
        if "find-the-model" in value and not value.startswith("#")
    ]
    assert "OUTSIDE-REPORT" not in page.text


@pytest.mark.unit
def test_a_link_to_a_file_inside_the_run_is_not_followed_either(
    tmp_path: Path,
) -> None:
    run = build(tmp_path / "run", three_flows())
    (run / "ux-story-other.mp4").write_bytes(b"\0")
    (run / "ux-story-route--admin-1.mp4").unlink()
    os.symlink("ux-story-other.mp4", run / "ux-story-route--admin-1.mp4")
    analyst = run / "flows" / "find-the-model@new-analyst"
    (analyst / "audit.json").unlink()
    os.symlink("journal.jsonl", analyst / "audit.json")
    references = _references(Page(_render(run)))
    assert "ux-story-route--admin-1.mp4" not in references
    assert "flows/find-the-model@new-analyst/audit.json" not in references
    assert "flows/find-the-model@new-analyst/journal.jsonl" in references


@pytest.mark.unit
def test_only_screenshots_of_the_writers_own_shape_are_shown(tmp_path: Path) -> None:
    run = build(tmp_path / "run", three_flows())
    screens = run / "flows" / "find-the-model@new-analyst" / "screens"
    names = ("evil.svg", "1.jpg", "0001.png", "001.jpeg", "001.jpg.svg", "a001.jpg")
    for name in names:
        (screens / name).write_bytes(b"\0")
    (screens / "004.jpg").mkdir()
    references = _references(Page(_render(run)))
    shown = [ref for ref in references if "find-the-model@new-analyst/screens/" in ref]
    assert sorted(set(shown)) == [
        f"flows/find-the-model@new-analyst/screens/{n:03d}.jpg" for n in (1, 2, 3)
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "parts",
    [
        ("..", "x"),
        ("/etc", "hosts"),
        ("flows", "..", "x"),
        ("flows/x",),
        ("",),
        (".x",),
        ("a b",),
        (),
    ],
)
def test_confined_refuses_any_part_that_is_not_a_plain_name(
    tmp_path: Path, parts: tuple[str, ...]
) -> None:
    (tmp_path / "x").write_text("", encoding="utf-8")
    assert story_report.confined(tmp_path, parts) is False
    assert story_report.link(tmp_path, parts) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "parts",
    [
        ("..", "run", "x"),
        ("sub", "..", "x"),
        ("sub/x",),
        (".hidden",),
        ("a b",),
        ("x\\y",),
    ],
)
def test_confined_refuses_a_name_that_is_not_plain_even_inside_the_run(
    tmp_path: Path, parts: tuple[str, ...]
) -> None:
    """Every one of these names a regular file inside the run directory, so only
    the plain-name rule refuses it: a URL never carries ``..``, a separator, a
    hidden name or a space, wherever it would lead."""
    run = tmp_path / "run"
    (run / "sub").mkdir(parents=True)
    for name in ("x", "sub/x", ".hidden", "a b", "x\\y"):
        (run / name).write_text("", encoding="utf-8")
    assert story_report.confined(run, parts) is False
    assert story_report.link(run, parts) is None


@pytest.mark.unit
def test_confined_takes_a_plain_file_and_a_plain_directory(tmp_path: Path) -> None:
    (tmp_path / "flows" / "a@b").mkdir(parents=True)
    (tmp_path / "flows" / "a@b" / "x.jpg").write_bytes(b"")
    assert story_report.link(tmp_path, ("flows", "a@b", "x.jpg")) == "flows/a@b/x.jpg"
    assert story_report.confined(tmp_path, ("flows", "a@b"), directory=True)
    assert not story_report.confined(tmp_path, ("flows", "a@b"))
    assert not story_report.confined(
        tmp_path, ("flows", "a@b", "x.jpg"), directory=True
    )


# --- what may be missing ---------------------------------------------------------


@pytest.mark.unit
def test_a_run_without_its_optional_files_still_makes_a_page(tmp_path: Path) -> None:
    run = build(tmp_path / "run", three_flows())
    for path in sorted(run.rglob("*"), reverse=True):
        if path.name in (story_verdict.RESULTS_FILE,):
            continue
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    assert sorted(p.name for p in run.iterdir()) == [story_verdict.RESULTS_FILE]
    page = Page(story_report.render(run))
    _assert_confined(page, run)
    assert set(page.attr("href")) <= {f"#{ident}" for ident in page.ids()}
    assert not page.attr("src")
    shown = page.text
    assert "story-run.json cannot be opened: it does not exist" in shown
    assert "no screens in this directory" in shown
    assert "no file of its own in this directory" in shown
    assert "that is not in this directory" in shown
    assert "not recorded" in shown


@pytest.mark.unit
def test_screens_the_journal_names_but_the_directory_lacks_are_counted(
    tmp_path: Path,
) -> None:
    run = build(tmp_path / "run", three_flows())
    for screens in run.rglob("screens"):
        shutil.rmtree(screens)
    shown = Page(story_report.render(run)).text
    assert "The journal names 3 more screens than this directory holds." in shown
    assert "the journals name 12 screens and the results 0 videos" in shown


@pytest.mark.unit
@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "results.json cannot be opened: it does not exist"),
        ("{", "results.json is not valid JSON"),
        ("[]", "results.json is not ux-test-stories/results v1"),
        ('{"a": 1, "a": 2}', "results.json is not valid JSON"),
        ('{"schema": NaN}', "results.json is not valid JSON"),
        (
            json.dumps({"schema": story_verdict.RESULTS_SCHEMA, "version": 1}),
            "results.json holds no list of flows and of findings",
        ),
        ("", "results.json is not a regular file"),
    ],
)
def test_a_run_without_readable_results_is_refused_by_name(
    tmp_path: Path, contents: str | None, message: str
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    if contents == "":
        (run / story_verdict.RESULTS_FILE).mkdir()
    elif contents is not None:
        (run / story_verdict.RESULTS_FILE).write_text(contents, encoding="utf-8")
    with pytest.raises(story_report.ReportError) as raised:
        story_report.render(run)
    assert str(raised.value) == message


@pytest.mark.unit
def test_results_over_their_bound_are_refused_before_they_are_read(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(story_report, "_RECORD_MAX_BYTES", 64)
    with pytest.raises(story_report.ReportError) as raised:
        story_report.render(run_dir)
    assert str(raised.value) == "results.json is over its bound of 64 bytes"


@pytest.mark.unit
def test_results_of_another_shape_are_refused_naming_the_key_only(
    run_dir: Path,
) -> None:
    original = _results(run_dir)
    document = json.loads(json.dumps(original))
    document["flows"][0]["verdict"] = SCRIPT_TAG
    _write_results(run_dir, document)
    with pytest.raises(story_report.ReportError) as raised:
        story_report.render(run_dir)
    assert "'verdict'" in str(raised.value) and SCRIPT_TAG not in str(raised.value)
    document = original
    document["flows"].append(dict(document["flows"][0]))
    _write_results(run_dir, document)
    with pytest.raises(story_report.ReportError) as raised:
        story_report.render(run_dir)
    assert str(raised.value) == (
        "results.json holds a flow of more than two attempts, or two of one number"
    )


@pytest.mark.unit
def test_an_identifier_too_long_for_the_page_is_refused_naming_no_text(
    run_dir: Path,
) -> None:
    document = _results(run_dir)
    story = "a" * 120
    flow = document["flows"][0]
    flow["story"], flow["key"] = story, f"{story}@{flow['persona']}"
    _write_results(run_dir, document)
    with pytest.raises(story_report.ReportError) as raised:
        story_report.render(run_dir)
    assert str(raised.value) == (
        "a class or id the page needs is not of its closed shape"
    )


# --- the same page, every time ---------------------------------------------------


@pytest.mark.unit
def test_the_same_files_make_the_same_page_wherever_they_are(
    run_dir: Path, tmp_path: Path
) -> None:
    first = _render(run_dir)
    assert _render(run_dir) == first
    elsewhere = tmp_path / "elsewhere" / "deeper"
    shutil.copytree(run_dir, elsewhere)
    assert _render(elsewhere) == first
    assert str(tmp_path) not in first


# --- the command line ------------------------------------------------------------


@pytest.mark.unit
def test_the_cli_writes_the_report_and_prints_its_path(
    run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert story_report.main([str(run_dir)]) == 0
    target = run_dir / story_report.REPORT_FILE
    assert capsys.readouterr().out == f"{target}\n"
    assert target.read_text("utf-8") == story_report.render(
        run_dir, catalogue=story_report.load_catalogue()
    )


@pytest.mark.unit
def test_the_cli_writes_elsewhere_with_out_and_refuses_a_link(
    run_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "elsewhere.html"
    assert story_report.main([str(run_dir), "--out", str(out)]) == 0
    assert capsys.readouterr().out == f"{out}\n"
    assert not (run_dir / story_report.REPORT_FILE).exists()
    assert out.read_text("utf-8").startswith("<!DOCTYPE html>")
    target = tmp_path / "target.html"
    target.write_text("kept", encoding="utf-8")
    os.symlink(target, run_dir / story_report.REPORT_FILE)
    assert story_report.main([str(run_dir)]) == 1
    assert capsys.readouterr().out.startswith("FAILED: report.html cannot be written:")
    assert target.read_text("utf-8") == "kept"


@pytest.mark.unit
def test_the_cli_fails_in_one_line_without_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert story_report.main([str(tmp_path)]) == 1
    assert capsys.readouterr().out == (
        "FAILED: results.json cannot be opened: it does not exist\n"
    )


@pytest.mark.unit
def test_the_script_runs_by_path_on_the_standard_library(run_dir: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-B", str(REPORT_PY), str(run_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == str(run_dir / story_report.REPORT_FILE)


@pytest.mark.unit
def test_the_catalogue_is_read_with_the_sha256_of_its_bytes(tmp_path: Path) -> None:
    known = story_report.load_catalogue()
    assert known is not None
    assert (
        known.sha256 == hashlib.sha256(stories.CATALOGUE_PATH.read_bytes()).hexdigest()
    )
    assert story_report.load_catalogue(tmp_path / "missing.toml") is None
    broken = tmp_path / "broken.toml"
    broken.write_text("version = 2\n", encoding="utf-8")
    assert story_report.load_catalogue(broken) is None


@pytest.mark.unit
def test_a_failure_is_said_without_quoting_any_text() -> None:
    assert story_report.failure(story_report.ReportError("results.json x")) == (
        "results.json x"
    )
    assert story_report.failure(KeyError(SCRIPT_TAG)) == "KeyError"
