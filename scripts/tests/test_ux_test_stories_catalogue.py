# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/stories.py and the catalogue scripts/ux_stories.toml."""

from __future__ import annotations

import ast
import builtins
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from repo_files import tracked_paths

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_markdown_links  # noqa: E402
from ux_runner import stories, ui_change  # noqa: E402

CATALOGUE_FILE = "scripts/ux_stories.toml"
RBAC_DOC = REPO_ROOT / "docs" / "rbac.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
MATRIX_HEADING = "## Permission Matrix"
MATRIX_HEADER = "Feature / API"
MATRIX_ROW_RE = re.compile(
    r"^  (?P<label>\S(?:.*?\S)?) {2,}(?P<cells>\S+(?: +\S+){3})\s*$"
)
TICK = "✅"
CROSS = "❌"
TICK_MARKS = frozenset("†*")
BULLET_RE = re.compile(r"^[-*] ")
SECTION_RE = re.compile(r"^## \[(?P<label>[^\]]+)\]")
LEAD_RE = re.compile(r"\A(?:⚠️?\s*)?\*\*(?P<lead>.+?)\*\*")
LIST_LINE_RE = re.compile(
    r"^(?P<tier>p0|p1)\t(?P<key>[a-z0-9-]+@[a-z0-9-]+)\t(?P<kind>read|commit_point)\t"
    r"(?P<expectation>allowed|refused|not_offered)\tbudget=(?P<budget>[0-9]+)\t"
    r"story_hash=(?P<hash>[0-9a-f]{12})$"
)
STORY_SCHEMA_KEYS = (
    "id",
    "title",
    "intent",
    "kind",
    "documented_steps",
    "as",
    "mr_as",
    "needs",
    "first_visit",
    "tier",
    "since",
    "origin",
    "claims",
    "rbac",
    "match",
)

FIXTURE_TOML = """\
version = 1

[limits]
step_budget_multiple = 10
step_budget_floor = 30
documented_steps_max = 6
flow_usd = 4
flow_minutes = 12

[runs]
mr_stories = 2

[personas.analyst]
group = "Author"
background = "You started this week and have read the release notes only."

[personas.lead]
group = "Viewer"
background = "You check results, and you were told your access is read-only."

[fixtures.done-document]
description = "a shipped sample processed to COMPLETE"

[[stories]]
id = "read-results"
title = "Read what was extracted from a document"
intent = "Open a processed document and find out what was extracted from it."
kind = "read"
documented_steps = 3
as = { analyst = "allowed", lead = "allowed" }
needs = ["done-document"]
first_visit = true
tier = "p0"
since = "0.6.0"
origin = "smoke"

[stories.match]
docs = ["web-ui.md#document-list"]
terms = ["document details"]

[[stories]]
id = "set-folder-configuration"
title = "Make one folder use one configuration"
intent = "Make every document that arrives in one folder use one configuration."
kind = "commit_point"
documented_steps = 5
as = { lead = "not_offered", analyst = "allowed" }
mr_as = ["analyst"]
tier = "p1"
since = "0.6.12"
origin = "changelog"
claims = [{ lead = "Configurations can be chosen by folder." }]
rbac = "View/Edit configuration"

[stories.match]
docs = ["configuration-profiles.md#prefix-mappings-assigning-a-profile-by-where-a-document-lands"]
issues = [1455]
terms = ["prefix mapping"]
ui_paths = ["src/ui/src/components/**"]
"""

GOLDEN_STORY_HASHES = {
    "read-results": "a10042a9a027",
    "set-folder-configuration": "3d597c70b110",
}
GOLDEN_SERIES_HASH = "35d653556052"


def _fixture() -> dict[str, Any]:
    return tomllib.loads(FIXTURE_TOML)


def _parsed(data: dict[str, Any]) -> stories.Catalogue:
    return stories.parse_catalogue(data, source="fixture.toml")


def _refused(data: dict[str, Any]) -> str:
    with pytest.raises(stories.CatalogueError) as caught:
        _parsed(data)
    return str(caught.value)


def _inputs(**overrides: Any) -> stories.SeriesInputs:
    values: dict[str, Any] = {
        "model": "us.anthropic.claude-opus-5-5",
        "effort": "high",
        "prompt_revision": 1,
        "judgement_revision": 1,
        "relay_revision": 0,
        "claude_code": "2.1.281",
        "chrome_devtools_mcp": "1.10.1",
        "chrome": "142.0.7444.59",
        "brief_sha256": "a" * 64,
        "ci_contract_sha256": "b" * 64,
    }
    values.update(overrides)
    return stories.SeriesInputs(**values)


@pytest.fixture(scope="module")
def catalogue() -> stories.Catalogue:
    return stories.load_catalogue()


@pytest.fixture(scope="module")
def published() -> set[str]:
    return check_markdown_links.published_pages(REPO_ROOT)


def _section_anchors(text: str) -> set[str]:
    """The fragment ids of H1-H3 ATX headings, deduplicated as ``anchors`` does."""
    body = check_markdown_links.strip_fences(
        check_markdown_links.strip_front_matter(text)
    )
    lines = [
        check_markdown_links.BLOCKQUOTE.sub("", line, count=1)
        for line in body.split("\n")
    ]
    seen: dict[str, int] = {}
    found: set[str] = set()
    for index, line in enumerate(lines):
        level = 0
        heading: str | None = None
        atx = check_markdown_links.ATX_HEADING.match(line)
        if atx:
            heading, level = atx.group(2), len(atx.group(1))
        elif (
            index > 0
            and check_markdown_links.SETEXT_RULE.match(line)
            and lines[index - 1].strip()
            and not check_markdown_links.ATX_HEADING.match(lines[index - 1])
        ):
            heading = lines[index - 1].strip()
        if heading is None:
            continue
        base = check_markdown_links.slugify(heading)
        repeat = seen.get(base, 0)
        seen[base] = repeat + 1
        if 1 <= level <= 3:
            found.add(base if repeat == 0 else f"{base}-{repeat}")
    return found


def _rbac_matrix(text: str) -> tuple[tuple[str, ...], dict[str, dict[str, str]]]:
    """The Permission Matrix of docs/rbac.md: its group columns and its rows."""
    lines = text.split("\n")
    start = lines.index(MATRIX_HEADING)
    opening = next(
        i for i in range(start + 1, len(lines)) if lines[i].startswith("```")
    )
    closing = next(
        i for i in range(opening + 1, len(lines)) if lines[i].startswith("```")
    )
    block = lines[opening + 1 : closing]
    header = next(line for line in block if line.startswith(MATRIX_HEADER))
    columns = tuple(header[len(MATRIX_HEADER) :].split())
    rows: dict[str, dict[str, str]] = {}
    for line in block:
        found = MATRIX_ROW_RE.match(line)
        if found:
            cells = found.group("cells").split()
            rows[found.group("label")] = dict(zip(columns, cells, strict=True))
    return columns, rows


def _cell_allows(cell: str) -> bool | None:
    """True for a tick, with or without its footnote marks; False for a cross."""
    if cell.startswith(TICK) and set(cell[len(TICK) :]) <= TICK_MARKS:
        return True
    if cell == CROSS:
        return False
    return None


def _bold_leads(text: str) -> list[tuple[str, str]]:
    """(section, lead) for every CHANGELOG entry that opens on a bold lead."""
    entries: list[tuple[str, list[str]]] = []
    section = ""
    current: list[str] | None = None
    for line in text.split("\n"):
        heading = SECTION_RE.match(line)
        if heading:
            section = heading.group("label")
        if line.startswith("#"):
            current = None
        elif BULLET_RE.match(line):
            current = [line[2:]]
            entries.append((section, current))
        elif current is not None:
            current.append(line)
    leads: list[tuple[str, str]] = []
    for label, parts in entries:
        found = LEAD_RE.match(" ".join(" ".join(parts).split()))
        if found:
            leads.append((label, " ".join(found.group("lead").split())))
    return leads


def _claim_line(story_id: str) -> int:
    lines = stories.CATALOGUE_PATH.read_text(encoding="utf-8").splitlines()
    start = lines.index(f'id = "{story_id}"')
    return 1 + next(
        i for i in range(start, len(lines)) if lines[i].startswith("claims")
    )


def _unmatched_globs(globs: list[str], tracked: list[str]) -> list[str]:
    return [
        glob for glob in globs if not any(ui_change.matches(p, [glob]) for p in tracked)
    ]


@pytest.mark.unit
def test_the_catalogue_loads_and_every_title_and_intent_states_intent_only(
    catalogue: stories.Catalogue,
) -> None:
    assert catalogue.version == stories.CATALOGUE_VERSION
    assert catalogue.stories
    for story in catalogue.stories:
        assert stories.lint_text(story.title) == [], story.id
        assert stories.lint_text(story.intent) == [], story.id


@pytest.mark.unit
def test_the_loaded_catalogue_is_the_toml_in_file_order(
    catalogue: stories.Catalogue,
) -> None:
    raw = tomllib.loads(stories.CATALOGUE_PATH.read_text(encoding="utf-8"))
    assert list(catalogue.personas) == list(raw["personas"])
    assert list(catalogue.fixtures) == list(raw["fixtures"])
    assert [story.id for story in catalogue.stories] == [
        s["id"] for s in raw["stories"]
    ]
    for story, entry in zip(catalogue.stories, raw["stories"], strict=True):
        assert story.expectations == tuple(entry["as"].items())
        assert story.mr_as == tuple(entry.get("mr_as", list(entry["as"])[:1]))
        assert story.needs == tuple(entry.get("needs", ()))
        assert story.first_visit is entry.get("first_visit", False)
        assert story.claims == tuple(
            stories.Claim(lead=claim["lead"]) for claim in entry.get("claims", ())
        )
        assert story.rbac == entry.get("rbac")
        assert story.match == stories.Match(
            **{key: tuple(value) for key, value in entry.get("match", {}).items()}
        )
    assert catalogue.limits == stories.Limits(**raw["limits"])
    assert catalogue.runs == stories.Runs(**raw["runs"])


@pytest.mark.unit
def test_story_ids_and_flow_keys_are_unique_in_the_catalogue(
    catalogue: stories.Catalogue,
) -> None:
    ids = [story.id for story in catalogue.stories]
    assert len(ids) == len(set(ids))
    keys = stories.flow_keys(catalogue, tiers=stories.TIERS)
    assert len(keys) == len(set(keys))
    assert len(keys) == sum(len(story.expectations) for story in catalogue.stories)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("place", "key"),
    [
        ("top level", "owner"),
        ("[limits]", "flow_dollars"),
        ("[runs]", "nightly_shards"),
        ("[personas.analyst]", "email"),
        ("[fixtures.done-document]", "bucket"),
        ("story 'read-results'", "notes"),
        ("story 'read-results': match", "pages"),
        ("story 'set-folder-configuration': claim #1", "section"),
    ],
)
def test_every_level_of_the_catalogue_is_closed(place: str, key: str) -> None:
    data = _fixture()
    tables: dict[str, dict[str, Any]] = {
        "top level": data,
        "[limits]": data["limits"],
        "[runs]": data["runs"],
        "[personas.analyst]": data["personas"]["analyst"],
        "[fixtures.done-document]": data["fixtures"]["done-document"],
        "story 'read-results'": data["stories"][0],
        "story 'read-results': match": data["stories"][0]["match"],
        "story 'set-folder-configuration': claim #1": data["stories"][1]["claims"][0],
    }
    tables[place][key] = "x"
    message = _refused(data)
    assert f"unknown key {key!r}" in message
    if place != "top level":
        assert place in message


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("steps", "stories state intent only"),
        ("expect", "stories state intent only"),
        ("setup", "stories state intent only"),
        ("ux_watch", "stories state intent only"),
        ("persona", "use `as`"),
        ("priority", "use `tier`"),
        ("paths", "use `match.ui_paths`"),
        ("story", "use `intent`"),
        ("stages", "deferred"),
        ("verify", "no verify step"),
    ],
)
def test_flow_keys_and_older_spellings_are_refused_by_name(
    key: str, reason: str
) -> None:
    data = _fixture()
    data["stories"][0][key] = ["Open the page."]
    message = _refused(data)
    assert f"story 'read-results': key {key!r} is refused:" in message
    assert reason in message


@pytest.mark.unit
def test_a_top_level_flows_list_is_refused_by_name() -> None:
    data = _fixture()
    data["flows"] = []
    assert "key 'flows' is refused: stories are [[stories]] tables" in _refused(data)


@pytest.mark.unit
def test_every_ux_flows_key_is_a_story_key_or_refused_by_name() -> None:
    flows = yaml.safe_load((SCRIPTS / "ux_flows.yaml").read_text(encoding="utf-8"))
    used = {key for flow in flows["flows"] for key in flow}
    assert {"id", "title", "steps", "expect"} <= used
    shared = used & set(STORY_SCHEMA_KEYS)
    assert shared == {"id", "title"}, (
        f"scripts/ux_flows.yaml now uses {sorted(shared - {'id', 'title'})}, which a "
        "story also uses; decide in scripts/ux_runner/stories.py whether the meaning "
        "is the same"
    )
    for key in sorted(used - shared):
        data = _fixture()
        data["stories"][0][key] = "x"
        assert f"key {key!r} is refused:" in _refused(data), (
            f"scripts/ux_flows.yaml's flow key {key!r} reaches the generic unknown-key "
            "error; give it a refusal of its own in scripts/ux_runner/stories.py that "
            "names what a story uses instead"
        )


@pytest.mark.unit
def test_the_story_keys_are_exactly_the_schema() -> None:
    data = _fixture()
    assert set(data["stories"][1]) == set(STORY_SCHEMA_KEYS) - {"needs", "first_visit"}
    data["stories"][1]["needs"] = []
    data["stories"][1]["first_visit"] = False
    assert set(data["stories"][1]) == set(STORY_SCHEMA_KEYS)
    _parsed(data)


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "id",
        "title",
        "intent",
        "kind",
        "documented_steps",
        "as",
        "tier",
        "since",
        "origin",
    ],
)
def test_a_missing_required_story_key_is_named(key: str) -> None:
    data = _fixture()
    del data["stories"][0][key]
    message = _refused(data)
    assert message.endswith(f": {key} is required")
    assert "fixture.toml: story" in message


@pytest.mark.unit
@pytest.mark.parametrize("key", ["version", "personas", "stories"])
def test_a_missing_required_table_is_named(key: str) -> None:
    data = _fixture()
    del data[key]
    assert _refused(data) == f"fixture.toml: {key} is required"


@pytest.mark.unit
@pytest.mark.parametrize("version", [2, 0, "1", 1.0, True])
def test_the_version_must_be_one(version: object) -> None:
    data = _fixture()
    data["version"] = version
    assert "version must be 1" in _refused(data)


@pytest.mark.unit
def test_omitted_limits_and_runs_take_their_defaults() -> None:
    data = _fixture()
    del data["limits"]
    del data["runs"]
    loaded = _parsed(data)
    assert loaded.limits == stories.Limits()
    assert loaded.runs == stories.Runs()
    data = _fixture()
    data["limits"] = {"flow_minutes": 20}
    data["runs"] = {"mr_runs": 6}
    loaded = _parsed(data)
    assert loaded.limits == replace(stories.Limits(), flow_minutes=20)
    assert loaded.runs == replace(stories.Runs(), mr_runs=6)


@pytest.mark.unit
def test_a_dollar_amount_is_stored_as_a_float() -> None:
    loaded = _parsed(_fixture())
    assert loaded.limits.flow_usd == 4.0
    assert isinstance(loaded.limits.flow_usd, float)
    data = _fixture()
    data["runs"] = {"job_usd": 12, "extended_flow_usd": 7}
    loaded = _parsed(data)
    assert (loaded.runs.job_usd, loaded.runs.extended_flow_usd) == (12.0, 7.0)
    assert isinstance(loaded.runs.job_usd, float)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("step_budget_multiple", 0),
        ("step_budget_multiple", True),
        ("step_budget_multiple", 2.5),
        ("step_budget_multiple", "10"),
        ("step_budget_floor", -1),
        ("documented_steps_max", 0),
        ("documented_steps_max", 11),
        ("flow_usd", 0),
        ("flow_usd", -1.0),
        ("flow_usd", 20.01),
        ("flow_usd", float("nan")),
        ("flow_usd", float("inf")),
        ("flow_usd", True),
        ("flow_usd", "4"),
        ("flow_usd", 10**400),
        ("flow_minutes", 0),
        ("flow_minutes", 61),
    ],
)
def test_limits_are_bounded(key: str, value: object) -> None:
    data = _fixture()
    data["limits"][key] = value
    message = _refused(data)
    assert message.startswith(f"fixture.toml: [limits]: {key} must be")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("documented_steps_max", 10),
        ("flow_usd", 20),
        ("flow_usd", 0.01),
        ("flow_minutes", 60),
        ("step_budget_multiple", 1),
    ],
)
def test_limits_accept_their_boundaries(key: str, value: object) -> None:
    data = _fixture()
    data["limits"][key] = value
    assert getattr(_parsed(data).limits, key) == value


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("mr_stories", 0),
        ("mr_flows", True),
        ("mr_runs", 1.5),
        ("job_usd", 0),
        ("job_usd", float("nan")),
        ("extended_flow_usd", -6.0),
        ("deadline_minutes", 0),
        ("setup_minutes", "6"),
        ("reserve_minutes", -1),
    ],
)
def test_runs_are_bounded(key: str, value: object) -> None:
    data = _fixture()
    data["runs"][key] = value
    assert _refused(data).startswith(f"fixture.toml: [runs]: {key} must be")


@pytest.mark.unit
def test_runs_have_no_dollar_ceiling() -> None:
    data = _fixture()
    data["runs"]["job_usd"] = 250
    assert _parsed(data).runs.job_usd == 250.0


@pytest.mark.unit
def test_a_persona_signs_in_as_a_supported_group_and_annotator_says_why_not() -> None:
    data = _fixture()
    data["personas"]["analyst"]["group"] = "Annotator"
    assert _refused(data) == (
        "fixture.toml: [personas.analyst]: group Annotator stays refused until "
        "`--long-lived` supports it"
    )
    for group in ("admin", "Operator", "", 3):
        data = _fixture()
        data["personas"]["analyst"]["group"] = group
        message = _refused(data)
        assert message.startswith(
            "fixture.toml: [personas.analyst]: group must be one of"
        )
        assert "Admin, Author, Reviewer, Viewer" in message


@pytest.mark.unit
@pytest.mark.parametrize(
    "ident", ["Analyst", "new_analyst", "-x", "x-", "x--y", "1x", "a" * 61, "a b"]
)
def test_persona_fixture_and_story_ids_are_lowercase_hyphenated(ident: str) -> None:
    data = _fixture()
    data["personas"][ident] = data["personas"].pop("lead")
    assert "fixture.toml: [personas]: persona id" in _refused(data)
    data = _fixture()
    data["fixtures"][ident] = data["fixtures"].pop("done-document")
    assert "fixture.toml: [fixtures]: fixture id" in _refused(data)
    data = _fixture()
    data["stories"][0]["id"] = ident
    message = _refused(data)
    assert message.startswith("fixture.toml: story #1: id")
    assert "lowercase words joined by hyphens" in message


@pytest.mark.unit
def test_a_story_id_is_used_once() -> None:
    data = _fixture()
    data["stories"][1]["id"] = "read-results"
    assert _refused(data) == (
        "fixture.toml: story #2: id 'read-results' is also the id of story #1"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("table", "key", "limit"),
    [
        ("story", "title", stories.TITLE_CHARS),
        ("story", "intent", stories.INTENT_CHARS),
        ("persona", "background", stories.BACKGROUND_CHARS),
        ("fixture", "description", 200),
        ("story", "rbac", 80),
    ],
)
def test_text_fields_are_bounded(table: str, key: str, limit: int) -> None:
    def target(data: dict[str, Any]) -> dict[str, Any]:
        if table == "story":
            return data["stories"][0]
        if table == "persona":
            return data["personas"]["analyst"]
        return data["fixtures"]["done-document"]

    data = _fixture()
    target(data)[key] = "a" * limit
    _parsed(data)
    for text in ("a" * (limit + 1), ""):
        data = _fixture()
        target(data)[key] = text
        assert f"{key} must be 1 to {limit} characters" in _refused(data)


@pytest.mark.unit
@pytest.mark.parametrize("text", ["Find it.\nThen stop.", "Find\tit", "Find it", "   "])
def test_text_fields_are_one_non_blank_line(text: str) -> None:
    for key in ("title", "intent", "rbac"):
        data = _fixture()
        data["stories"][0][key] = text
        message = _refused(data)
        assert f"story 'read-results': {key} must" in message
        assert "one line" in message or "blank" in message
    data = _fixture()
    data["personas"]["lead"]["background"] = text
    assert "[personas.lead]: background must" in _refused(data)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "text", "problem"),
    [
        ("title", "Click through to the results", "control word: Click"),
        ("intent", "Open the Settings tab and change nothing.", "control word: tab"),
        ("intent", 'Find the "Save" control.', 'quote mark: "'),
        ("title", "Find the 'Upload' page", "quoted label: 'Upload'"),
        ("intent", "Read docs/web-ui.md first.", "docs path or URL: docs/"),
    ],
)
def test_a_title_or_intent_that_is_not_intent_only_is_refused(
    key: str, text: str, problem: str
) -> None:
    data = _fixture()
    data["stories"][0][key] = text
    message = _refused(data)
    assert message.startswith(f"fixture.toml: story 'read-results': {key} must state")
    assert problem in message


@pytest.mark.unit
def test_documented_steps_is_bounded_by_the_catalogue_limit() -> None:
    for steps in (0, 7, True, 3.0, "3"):
        data = _fixture()
        data["stories"][0]["documented_steps"] = steps
        assert "documented_steps must be" in _refused(data)
    data = _fixture()
    data["stories"][0]["documented_steps"] = 6
    assert _parsed(data).stories[0].documented_steps == 6
    data["limits"]["documented_steps_max"] = 8
    data["stories"][0]["documented_steps"] = 7
    assert _parsed(data).stories[0].documented_steps == 7


@pytest.mark.unit
def test_as_names_defined_personas_with_one_of_the_three_expectations() -> None:
    for value, phrase in (
        ({}, "as must be a non-empty table"),
        ("analyst", "as must be a non-empty table"),
        ({"nobody": "allowed"}, "as names persona 'nobody', which [personas] does not"),
        (
            {"analyst": "maybe"},
            "as.analyst must be one of allowed, refused, not_offered",
        ),
        ({"analyst": ""}, "as.analyst must be one of"),
    ):
        data = _fixture()
        data["stories"][0]["as"] = value
        assert phrase in _refused(data)
    data = _fixture()
    data["stories"][0]["as"] = {"lead": "refused", "analyst": "not_offered"}
    story = _parsed(data).stories[0]
    assert story.expectations == (("lead", "refused"), ("analyst", "not_offered"))
    assert story.personas == ("lead", "analyst")


@pytest.mark.unit
def test_a_commit_point_story_cannot_expect_a_refusal() -> None:
    data = _fixture()
    data["stories"][1]["as"]["lead"] = "refused"
    message = _refused(data)
    assert (
        "as.lead is refused, but a commit_point story declares only allowed or"
        in message
    )
    assert "a refusal is invisible when nothing is pressed" in message


@pytest.mark.unit
def test_mr_as_defaults_to_the_first_as_persona_and_must_be_a_subset() -> None:
    loaded = _parsed(_fixture())
    assert loaded.stories[0].mr_as == ("analyst",)
    assert loaded.stories[1].mr_as == ("analyst",)
    for value, phrase in (
        ([], "mr_as must be a non-empty array"),
        ("analyst", "mr_as must be a non-empty array"),
        (["nobody"], "mr_as names 'nobody', which as does not hold"),
        (["analyst", "analyst"], "mr_as lists 'analyst' twice"),
    ):
        data = _fixture()
        data["stories"][1]["mr_as"] = value
        assert phrase in _refused(data)


@pytest.mark.unit
def test_needs_names_each_defined_fixture_once() -> None:
    for value, phrase in (
        (["missing"], "needs names 'missing', which [fixtures] does not hold"),
        (["done-document", "done-document"], "needs lists 'done-document' twice"),
        ("done-document", "needs must be an array"),
    ):
        data = _fixture()
        data["stories"][0]["needs"] = value
        assert phrase in _refused(data)
    data = _fixture()
    del data["stories"][0]["needs"]
    assert _parsed(data).stories[0].needs == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value", "phrase"),
    [
        ("first_visit", "yes", "first_visit must be true or false"),
        ("first_visit", 1, "first_visit must be true or false"),
        ("kind", "write", "kind must be one of read, commit_point"),
        ("tier", "p2", "tier must be one of p0, p1"),
        ("origin", "manual", "origin must be one of changelog, smoke, finding"),
        ("since", "0.6", "since must be a release segment"),
        ("since", "v0.6.12", "since must be a release segment"),
        ("since", "0.6.12.dev1", "since must be a release segment"),
        ("since", "０.６.１２", "since must be a release segment"),
        ("since", 0.6, "since must be a release segment"),
    ],
)
def test_the_story_metadata_is_checked(key: str, value: object, phrase: str) -> None:
    data = _fixture()
    data["stories"][0][key] = value
    assert phrase in _refused(data)


@pytest.mark.unit
def test_a_changelog_story_needs_a_claim_and_claims_are_well_formed() -> None:
    data = _fixture()
    del data["stories"][1]["claims"]
    assert "origin changelog needs at least one claim" in _refused(data)
    for value, phrase in (
        ([], "origin changelog needs at least one claim"),
        ("A lead.", "claims must be an array"),
        ([{"lead": "A."}, {"lead": "A."}], "claim #2: lead repeats an earlier claim"),
        ([{"lead": ""}], "claim #1: lead must be 1 to 300 characters"),
        ([{"lead": "a" * 301}], "claim #1: lead must be 1 to 300 characters"),
        ([{}], "claim #1: lead is required"),
        (["A lead."], "claim #1 must be a table"),
    ):
        data = _fixture()
        data["stories"][1]["claims"] = value
        assert phrase in _refused(data)
    data = _fixture()
    data["stories"][0]["claims"] = [{"lead": "A smoke story may carry a claim."}]
    assert _parsed(data).stories[0].claims == (
        stories.Claim(lead="A smoke story may carry a claim."),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "value", "phrase"),
    [
        ("docs", ["web-ui"], "match.docs entry 'web-ui' must be page.md or"),
        ("docs", ["web-ui.md#a b"], "must be page.md or page.md#anchor"),
        ("docs", ["web-ui.md##x"], "must be page.md or page.md#anchor"),
        ("docs", ["docs/web-ui.md"], "must be relative to docs/"),
        ("docs", ["../web-ui.md"], "must be relative to docs/"),
        ("docs", ["/web-ui.md"], "must be relative to docs/"),
        ("docs", ["a//web-ui.md"], "must be relative to docs/"),
        ("docs", ["web-ui.md", "web-ui.md"], "match.docs lists 'web-ui.md' twice"),
        ("docs", "web-ui.md", "match.docs must be an array"),
        ("issues", [0], "must be a positive issue number"),
        ("issues", [True], "must be a positive issue number"),
        ("issues", ["12"], "must be a positive issue number"),
        ("issues", [12, 12], "match.issues lists 12 twice"),
        ("terms", [""], "each match.terms entry must be 1 to 60 characters"),
        ("terms", ["a" * 61], "each match.terms entry must be 1 to 60 characters"),
        ("terms", ["x", "x"], "match.terms lists 'x' twice"),
        ("ui_paths", ["a" * 201], "each match.ui_paths entry must be 1 to 200"),
        ("ui_paths", [3], "each match.ui_paths entry must be text"),
    ],
)
def test_the_match_block_is_checked(key: str, value: object, phrase: str) -> None:
    data = _fixture()
    data["stories"][1]["match"][key] = value
    message = _refused(data)
    assert message.startswith("fixture.toml: story 'set-folder-configuration':")
    assert phrase in message


@pytest.mark.unit
def test_an_absent_match_or_rbac_reads_as_empty() -> None:
    data = _fixture()
    del data["stories"][1]["match"]
    del data["stories"][1]["rbac"]
    story = _parsed(data).stories[1]
    assert story.match == stories.Match()
    assert story.rbac is None
    data = _fixture()
    data["stories"][1]["match"] = {"docs": ["web-ui.md"]}
    assert _parsed(data).stories[1].match == stories.Match(docs=("web-ui.md",))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "where"),
    [
        ([], "stories must be a non-empty array"),
        ({}, "stories must be a non-empty array"),
        (["x"], "story #1 must be a table"),
    ],
)
def test_stories_are_a_non_empty_array_of_tables(value: object, where: str) -> None:
    data = _fixture()
    data["stories"] = value
    assert where in _refused(data)


@pytest.mark.unit
def test_tables_must_be_tables() -> None:
    for mutate, phrase in (
        (lambda d: d.update(limits=3), "[limits] must be a table"),
        (lambda d: d.update(personas=[]), "[personas] must be a table"),
        (
            lambda d: d["personas"].update(lead="Viewer"),
            "[personas.lead] must be a table",
        ),
        (
            lambda d: d["stories"][0].update(match=[]),
            "story 'read-results': match must be",
        ),
    ):
        data = _fixture()
        mutate(data)
        assert phrase in _refused(data)
    with pytest.raises(stories.CatalogueError, match="the catalogue must be a table"):
        stories.parse_catalogue([], source="x")


@pytest.mark.unit
def test_errors_name_the_file_the_story_and_the_key(tmp_path: Path) -> None:
    path = tmp_path / "ux_stories.toml"
    path.write_text(
        FIXTURE_TOML.replace('kind = "read"', 'kind = "write"'), encoding="utf-8"
    )
    with pytest.raises(stories.CatalogueError) as caught:
        stories.load_catalogue(path)
    assert str(caught.value) == (
        f"{path}: story 'read-results': kind must be one of read, commit_point, "
        "not 'write'"
    )


@pytest.mark.unit
def test_the_catalogue_is_named_relative_to_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tomllib.loads(stories.CATALOGUE_PATH.read_text(encoding="utf-8"))
    data["stories"][0]["kind"] = "write"
    monkeypatch.setattr(stories.tomllib, "loads", lambda text: data)
    with pytest.raises(stories.CatalogueError) as caught:
        stories.load_catalogue()
    first = data["stories"][0]["id"]
    assert str(caught.value).startswith(f"{CATALOGUE_FILE}: story {first!r}: kind")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "phrase"),
    [
        (None, "cannot be read"),
        (b"version = \n", "is not valid TOML"),
        (b"version = 1\nversion = 1\n", "is not valid TOML"),
        (b"\xff\xfe\x00v", "is not UTF-8 text"),
    ],
)
def test_an_unreadable_or_malformed_file_is_a_catalogue_error(
    tmp_path: Path, content: bytes | None, phrase: str
) -> None:
    path = tmp_path / "ux_stories.toml"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(stories.CatalogueError) as caught:
        stories.load_catalogue(path)
    assert str(caught.value).startswith(f"{path}: {phrase}")


@pytest.mark.unit
def test_the_loader_reads_no_file_but_the_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ux_stories.toml"
    path.write_bytes(stories.CATALOGUE_PATH.read_bytes())
    opened: list[str] = []
    real_io_open = io.open
    real_os_open = os.open

    def spy_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_io_open(file, *args, **kwargs)

    def spy_os_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_os_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    loaded = stories.load_catalogue(path)
    monkeypatch.undo()
    assert loaded.stories
    assert set(opened) == {str(path)}


@pytest.mark.unit
def test_stories_imports_only_the_standard_library_and_the_python_floor() -> None:
    source = (SCRIPTS / "ux_runner" / "stories.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    first_party: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "ux_runner":
            first_party.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported - {"__future__"} <= sys.stdlib_module_names
    assert first_party == {"python_floor"}


@pytest.mark.unit
@pytest.mark.parametrize("word", [w for w in stories.CONTROL_WORDS if " " not in w])
@pytest.mark.parametrize("suffix", ["", "s", "es", "ed", "ing"])
def test_each_control_word_is_refused_in_every_inflection(
    word: str, suffix: str
) -> None:
    for spelled in (word + suffix, (word + suffix).upper(), (word + suffix).title()):
        assert stories.lint_text(f"Then {spelled} it.") == [f"control word: {spelled}"]


@pytest.mark.unit
def test_every_control_word_is_refused() -> None:
    for word in stories.CONTROL_WORDS:
        assert stories.lint_text(f"Now {word} what you need.") == [
            f"control word: {word}"
        ]
    assert stories.lint_text("Select the right one.") == ["control word: Select the"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Read the table of results.",
        "The tapestry hangs on the wall.",
        "Under pressure, find the answer.",
        "Find the unlinked records.",
        "The buttonhole is torn.",
        "It selected the right one.",
        "Select these two documents.",
        "Find the documents/ folder.",
        "Read the markdown version.",
        "Use the md5 sum.",
    ],
)
def test_control_words_and_paths_inside_other_words_are_not_refused(text: str) -> None:
    assert stories.lint_text(text) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "problems"),
    [
        ('Find the "Save" control.', ['quote mark: "']),
        ("Find the “Save” control.", ["quote mark: “", "quote mark: ”"]),
        ("Find the `Save` control.", ["quote mark: `"]),
        ("Find the 'Save' control.", ["quoted label: 'Save'"]),
        ("Find the ‘Save’ control.", ["quoted label: ‘Save’"]),
        ("Check a paid extension's subscription.", []),
        ("Read the users' results and the team's notes.", []),
        ("Rock 'n roll.", []),
    ],
)
def test_quoted_labels_are_refused_and_apostrophes_are_not(
    text: str, problems: list[str]
) -> None:
    assert stories.lint_text(text) == problems


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("Read docs/web-ui.md first.", "docs path or URL: docs/"),
        ("Read the web-ui.md page.", "docs path or URL: .md"),
        ("Open https://example.com/x.", "docs path or URL: https://"),
        ("Open HTTP://example.com/x.", "docs path or URL: HTTP://"),
        ("Visit www.example.com.", "docs path or URL: www."),
    ],
)
def test_docs_paths_and_urls_are_refused(text: str, problem: str) -> None:
    assert problem in stories.lint_text(text)


@pytest.mark.unit
def test_problems_come_in_text_order_once_each() -> None:
    assert stories.lint_text("Click the 'Save' button, then click Save.") == [
        "control word: Click",
        "quoted label: 'Save'",
        "control word: button",
        "control word: click",
    ]
    assert stories.lint_text("Click it. Click it again.") == ["control word: Click"]
    assert stories.lint_text("") == []


@pytest.mark.unit
def test_short_hash_is_the_sha256_of_canonical_json() -> None:
    value = {"b": [1, 2.0, None, True], "a": "é", "c": {"y": 1, "x": (2, 3)}}
    canonical = '{"a":"é","b":[1,2.0,null,true],"c":{"x":[2,3],"y":1}}'.encode()
    assert stories.short_hash(value) == hashlib.sha256(canonical).hexdigest()[:12]
    assert re.fullmatch(r"[0-9a-f]{12}", stories.short_hash(value))
    assert stories.HASH_CHARS == 12


@pytest.mark.unit
def test_the_story_hash_is_the_documented_canonical_form() -> None:
    loaded = _parsed(_fixture())
    story = loaded.story("set-folder-configuration")
    document = {
        "story": {
            "id": "set-folder-configuration",
            "title": story.title,
            "intent": story.intent,
            "kind": "commit_point",
            "documented_steps": 5,
            "as": [["lead", "not_offered"], ["analyst", "allowed"]],
            "needs": [],
            "first_visit": False,
        },
        "personas": {
            "analyst": {
                "group": "Author",
                "background": loaded.personas["analyst"].background,
            },
            "lead": {
                "group": "Viewer",
                "background": loaded.personas["lead"].background,
            },
        },
        "limits": {
            "step_budget_multiple": 10,
            "step_budget_floor": 30,
            "documented_steps_max": 6,
            "flow_usd": 4.0,
            "flow_minutes": 12,
        },
    }
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert (
        stories.story_hash(loaded, story) == hashlib.sha256(canonical).hexdigest()[:12]
    )


@pytest.mark.unit
def test_the_golden_hashes_of_the_fixture_catalogue_are_stable() -> None:
    loaded = _parsed(_fixture())
    hashes = {story.id: stories.story_hash(loaded, story) for story in loaded.stories}
    assert hashes == GOLDEN_STORY_HASHES
    series = stories.series_hash(hashes["read-results"], "analyst", _inputs())
    assert series == GOLDEN_SERIES_HASH


HASHED_CHANGES: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("id", lambda d: d["stories"][0].update(id="read-results-again")),
    ("title", lambda d: d["stories"][0].update(title="Read what one document holds")),
    (
        "intent",
        lambda d: d["stories"][0].update(intent="Find what one document holds."),
    ),
    ("kind", lambda d: d["stories"][0].update(kind="commit_point")),
    ("documented_steps", lambda d: d["stories"][0].update(documented_steps=4)),
    (
        "as order",
        lambda d: d["stories"][0].update(
            **{"as": {"lead": "allowed", "analyst": "allowed"}}
        ),
    ),
    ("as value", lambda d: d["stories"][0]["as"].update(lead="not_offered")),
    ("as member", lambda d: d["stories"][0]["as"].pop("lead")),
    ("needs", lambda d: d["stories"][0].update(needs=[])),
    ("first_visit", lambda d: d["stories"][0].update(first_visit=False)),
    (
        "background",
        lambda d: d["personas"]["lead"].update(background="You lead a team."),
    ),
    ("group", lambda d: d["personas"]["lead"].update(group="Reviewer")),
    ("step_budget_floor", lambda d: d["limits"].update(step_budget_floor=20)),
    ("flow_usd", lambda d: d["limits"].update(flow_usd=4.5)),
    ("flow_minutes", lambda d: d["limits"].update(flow_minutes=13)),
]

UNHASHED_CHANGES: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    ("tier", lambda d: d["stories"][0].update(tier="p1")),
    ("since", lambda d: d["stories"][0].update(since="0.6.5")),
    ("origin", lambda d: d["stories"][0].update(origin="finding")),
    ("claims", lambda d: d["stories"][0].update(claims=[{"lead": "A claim."}])),
    ("rbac", lambda d: d["stories"][0].update(rbac="View document details")),
    ("match", lambda d: d["stories"][0].update(match={"issues": [7], "terms": ["x"]})),
    ("mr_as", lambda d: d["stories"][0].update(mr_as=["lead"])),
    ("runs", lambda d: d["runs"].update(mr_stories=1, job_usd=3)),
    ("fixture text", lambda d: d["fixtures"]["done-document"].update(description="x")),
    (
        "unused persona",
        lambda d: d["personas"].update(admin={"group": "Admin", "background": "x"}),
    ),
    (
        "another story",
        lambda d: d["stories"][1].update(title="Make a folder use a configuration"),
    ),
    ("integral dollars", lambda d: d["limits"].update(flow_usd=4.0)),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "change"), HASHED_CHANGES, ids=[c[0] for c in HASHED_CHANGES]
)
def test_the_story_hash_changes_with_what_the_agent_is_asked(
    label: str, change: Callable[[dict[str, Any]], None]
) -> None:
    before = _parsed(_fixture())
    data = _fixture()
    change(data)
    after = _parsed(data)
    assert stories.story_hash(after, after.stories[0]) != stories.story_hash(
        before, before.stories[0]
    ), label


@pytest.mark.unit
@pytest.mark.parametrize(
    ("label", "change"), UNHASHED_CHANGES, ids=[c[0] for c in UNHASHED_CHANGES]
)
def test_the_story_hash_ignores_when_a_story_runs(
    label: str, change: Callable[[dict[str, Any]], None]
) -> None:
    before = _parsed(_fixture())
    data = _fixture()
    change(data)
    after = _parsed(data)
    assert stories.story_hash(after, after.stories[0]) == stories.story_hash(
        before, before.stories[0]
    ), label


@pytest.mark.unit
def test_a_limits_change_resets_every_story_hash_and_a_runs_change_none(
    catalogue: stories.Catalogue,
) -> None:
    widened = replace(catalogue, limits=replace(catalogue.limits, flow_minutes=13))
    rerun = replace(catalogue, runs=replace(catalogue.runs, mr_runs=9))
    for story in catalogue.stories:
        original = stories.story_hash(catalogue, story)
        assert stories.story_hash(widened, story) != original
        assert stories.story_hash(rerun, story) == original


@pytest.mark.unit
def test_the_series_hash_is_the_documented_canonical_form() -> None:
    inputs = _inputs()
    document: dict[str, Any] = {
        "story_hash": "0f3a9c2e71b4",
        "persona": "analyst",
        "model": inputs.model,
        "effort": inputs.effort,
        "prompt_revision": 1,
        "judgement_revision": 1,
        "relay_revision": 0,
        "claude_code": inputs.claude_code,
        "chrome_devtools_mcp": inputs.chrome_devtools_mcp,
        "chrome": inputs.chrome,
        "brief_sha256": inputs.brief_sha256,
        "ci_contract_sha256": inputs.ci_contract_sha256,
    }
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert (
        stories.series_hash("0f3a9c2e71b4", "analyst", inputs)
        == hashlib.sha256(canonical).hexdigest()[:12]
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "us.anthropic.claude-opus-5"),
        ("effort", "medium"),
        ("prompt_revision", 2),
        ("judgement_revision", 2),
        ("relay_revision", 1),
        ("claude_code", "2.1.282"),
        ("chrome_devtools_mcp", "1.10.2"),
        ("chrome", "143.0.0.0"),
        ("brief_sha256", "c" * 64),
        ("ci_contract_sha256", "d" * 64),
    ],
)
def test_the_series_hash_changes_with_every_input(field: str, value: object) -> None:
    base = stories.series_hash("0f3a9c2e71b4", "analyst", _inputs())
    assert (
        stories.series_hash("0f3a9c2e71b4", "analyst", _inputs(**{field: value}))
        != base
    )


@pytest.mark.unit
def test_the_series_is_per_flow() -> None:
    base = stories.series_hash("0f3a9c2e71b4", "analyst", _inputs())
    assert stories.series_hash("0f3a9c2e71b4", "lead", _inputs()) != base
    assert stories.series_hash("0f3a9c2e71b5", "analyst", _inputs()) != base
    assert stories.series_hash("0f3a9c2e71b4", "analyst", _inputs()) == base


@pytest.mark.unit
@pytest.mark.parametrize(
    ("steps", "scale", "expected"),
    [
        (1, 1, 30),
        (3, 1, 30),
        (4, 1, 40),
        (5, 1, 50),
        (6, 1, 60),
        (3, 2, 60),
        (5, 2, 100),
    ],
)
def test_the_budget_is_ten_actions_a_documented_step_with_a_floor(
    steps: int, scale: int, expected: int
) -> None:
    story = replace(_parsed(_fixture()).stories[0], documented_steps=steps)
    assert stories.budget(stories.Limits(), story, scale=scale) == expected


@pytest.mark.unit
def test_the_budget_follows_the_limits_and_refuses_a_scale_below_one() -> None:
    story = _parsed(_fixture()).stories[0]
    limits = stories.Limits(step_budget_multiple=4, step_budget_floor=5)
    assert stories.budget(limits, story) == 12
    assert stories.budget(replace(limits, step_budget_floor=20), story) == 20
    for scale in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            stories.budget(limits, story, scale=scale)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("budget", "cap"), [(30, 110), (40, 140), (50, 170), (60, 200), (100, 200)]
)
def test_the_turn_cap(budget: int, cap: int) -> None:
    assert stories.turn_cap(budget) == cap


@pytest.mark.unit
def test_flow_keys_cover_every_as_persona_sorted_and_filtered() -> None:
    loaded = _parsed(_fixture())
    key = stories.FlowKey
    assert stories.flow_keys(loaded) == [
        key("read-results", "analyst"),
        key("read-results", "lead"),
    ]
    assert stories.flow_keys(loaded, tiers=("p1",)) == [
        key("set-folder-configuration", "analyst"),
        key("set-folder-configuration", "lead"),
    ]
    everything = stories.flow_keys(loaded, tiers=stories.TIERS)
    assert everything == sorted(everything) and len(everything) == 4
    assert stories.flow_keys(loaded, tiers="p1", persona="lead") == [
        key("set-folder-configuration", "lead")
    ]
    assert stories.flow_keys(loaded, tiers=stories.TIERS, story="read-results") == [
        key("read-results", "analyst"),
        key("read-results", "lead"),
    ]
    assert stories.flow_keys(loaded, tiers=stories.TIERS, story="nothing") == []
    assert stories.flow_keys(loaded, tiers=()) == []
    for tiers in (("p2",), ("p0", "P1")):
        with pytest.raises(ValueError, match="unknown tier"):
            stories.flow_keys(loaded, tiers=tiers)


@pytest.mark.unit
def test_mr_personas_put_mr_as_first_then_the_rest_of_as() -> None:
    story = _parsed(_fixture()).stories[1]
    assert stories.mr_personas(story) == ("analyst", "lead")
    assert stories.mr_personas(replace(story, mr_as=("lead",))) == ("lead", "analyst")
    assert stories.mr_personas(replace(story, mr_as=("lead", "analyst"))) == (
        "lead",
        "analyst",
    )


@pytest.mark.unit
def test_shards_are_a_balanced_partition_of_the_sorted_keys(
    catalogue: stories.Catalogue,
) -> None:
    keys = stories.flow_keys(catalogue, tiers=stories.TIERS)
    for total in range(1, len(keys) + 2):
        parts = [stories.shard(keys, index, total) for index in range(1, total + 1)]
        joined = [key for part in parts for key in part]
        assert sorted(joined) == sorted(keys)
        assert len(joined) == len(set(joined))
        sizes = [len(part) for part in parts]
        assert max(sizes) - min(sizes) <= 1
        assert parts == [
            stories.shard(list(reversed(keys)), index, total)
            for index in range(1, total + 1)
        ]
    assert stories.shard(keys, 2, 3) == sorted(keys)[1::3]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("index", "total"), [(0, 3), (4, 3), (1, 0), (-1, 2), (True, 2), (1, 1.0)]
)
def test_a_shard_index_is_from_one_to_total(index: object, total: object) -> None:
    with pytest.raises(ValueError):
        stories.shard([], index, total)


@pytest.mark.unit
def test_a_flow_key_prints_and_parses_as_story_at_persona() -> None:
    key = stories.FlowKey("route-folder-to-profile", "new-analyst")
    assert str(key) == "route-folder-to-profile@new-analyst"
    assert stories.FlowKey.parse(str(key)) == key
    assert sorted(
        [
            stories.FlowKey("b", "a"),
            stories.FlowKey("a", "b"),
            stories.FlowKey("a", "a"),
        ]
    ) == [
        stories.FlowKey("a", "a"),
        stories.FlowKey("a", "b"),
        stories.FlowKey("b", "a"),
    ]
    for text in (
        "route-folder-to-profile",
        "a@b@c",
        "@b",
        "a@",
        "A@b",
        "a@b c",
        "a_b@c",
        "",
    ):
        with pytest.raises(ValueError):
            stories.FlowKey.parse(text)
    with pytest.raises(ValueError):
        stories.FlowKey.parse(None)


@pytest.mark.unit
def test_lookups_raise_key_error_for_what_is_not_there() -> None:
    loaded = _parsed(_fixture())
    assert (
        loaded.story("read-results").title == "Read what was extracted from a document"
    )
    assert loaded.persona("lead").group == "Viewer"
    assert loaded.stories[1].expectation_for("lead") == "not_offered"
    with pytest.raises(KeyError):
        loaded.story("nothing")
    with pytest.raises(KeyError):
        loaded.persona("nobody")
    with pytest.raises(KeyError):
        loaded.stories[0].expectation_for("nobody")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("version", "segment"),
    [
        ("0.6.12.dev1", (0, 6, 12)),
        ("0.6.12.dev2", (0, 6, 12)),
        ("0.6.12", (0, 6, 12)),
        ("0.6.12rc1", (0, 6, 12)),
        ("10.20.300+local", (10, 20, 300)),
        (" 0.6.5 ", (0, 6, 5)),
        ("0.6", None),
        ("v0.6.12", None),
        ("dev", None),
        ("", None),
        (None, None),
        ("9" * 5000 + ".0.0", None),
    ],
)
def test_the_release_segment_is_the_leading_major_minor_patch(
    version: str | None, segment: tuple[int, int, int] | None
) -> None:
    assert stories.release_segment(version) == segment


@pytest.mark.unit
@pytest.mark.parametrize(
    ("since", "version", "reached"),
    [
        ("0.6.12", "0.6.12.dev1", True),
        ("0.6.12", "0.6.12", True),
        ("0.6.12", "0.6.13.dev1", True),
        ("0.6.12", "0.7.0", True),
        ("0.6.13", "0.6.12.dev1", False),
        ("0.6.12", "0.6.11", False),
        ("1.0.0", "0.99.99", False),
        ("0.6.12", None, None),
        ("0.6.12", "unreadable", None),
    ],
)
def test_since_is_compared_by_release_segment(
    since: str, version: str | None, reached: bool | None
) -> None:
    assert stories.since_reached(since, version) is reached


@pytest.mark.unit
@pytest.mark.parametrize("since", ["0.6", "0.6.12.dev1", "", None])
def test_since_reached_refuses_a_since_that_is_not_a_release_segment(
    since: object,
) -> None:
    with pytest.raises(ValueError):
        stories.since_reached(since, "0.6.12")


@pytest.mark.unit
def test_list_prints_one_tab_separated_line_per_flow(
    catalogue: stories.Catalogue, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stories.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    keys = stories.flow_keys(catalogue, tiers=stories.TIERS)
    assert len(lines) == len(keys)
    tiers_seen: list[str] = []
    for line in lines:
        found = LIST_LINE_RE.fullmatch(line)
        assert found, line
        key = stories.FlowKey.parse(found.group("key"))
        story = catalogue.story(key.story)
        assert found.group("tier") == story.tier
        assert found.group("kind") == story.kind
        assert found.group("expectation") == story.expectation_for(key.persona)
        assert int(found.group("budget")) == stories.budget(catalogue.limits, story)
        assert found.group("hash") == stories.story_hash(catalogue, story)
        tiers_seen.append(found.group("tier"))
    assert tiers_seen == sorted(tiers_seen, key=stories.TIERS.index)


@pytest.mark.unit
def test_list_json_is_one_object_holding_the_same_flows(
    catalogue: stories.Catalogue, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stories.main(["list", "--tier", "p0", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["version"] == 1
    assert document["tiers"] == ["p0"]
    keys = [str(key) for key in stories.flow_keys(catalogue, tiers=("p0",))]
    assert [flow["key"] for flow in document["flows"]] == keys
    for flow in document["flows"]:
        assert set(flow) == {
            "tier",
            "key",
            "story",
            "persona",
            "group",
            "kind",
            "expectation",
            "budget",
            "story_hash",
        }
        assert flow["key"] == f"{flow['story']}@{flow['persona']}"
        assert flow["group"] == catalogue.persona(flow["persona"]).group


@pytest.mark.unit
def test_list_reports_a_catalogue_error_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "ux_stories.toml"
    path.write_text(
        FIXTURE_TOML.replace('tier = "p1"', 'tier = "p3"'), encoding="utf-8"
    )
    assert stories.main(["list", "--catalogue", str(path)]) == 1
    out = capsys.readouterr().out
    assert out.startswith(
        f"FAILED: {path}: story 'set-folder-configuration': tier must be"
    )
    assert out.count("\n") == 1


@pytest.mark.unit
def test_list_of_a_fixture_catalogue_reads_only_that_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "ux_stories.toml"
    path.write_text(FIXTURE_TOML, encoding="utf-8")
    assert stories.main(["list", "--catalogue", str(path), "--tier", "p1"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"p1\tset-folder-configuration@analyst\tcommit_point\tallowed\tbudget=50\t"
        f"story_hash={GOLDEN_STORY_HASHES['set-folder-configuration']}",
        f"p1\tset-folder-configuration@lead\tcommit_point\tnot_offered\tbudget=50\t"
        f"story_hash={GOLDEN_STORY_HASHES['set-folder-configuration']}",
    ]


@pytest.mark.unit
def test_list_runs_by_path_on_an_isolated_interpreter(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(SCRIPTS / "ux_runner" / "stories.py"),
            "list",
            "--tier",
            "p0",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = completed.stdout.splitlines()
    assert lines and all(LIST_LINE_RE.fullmatch(line) for line in lines)
    assert {line.split("\t")[0] for line in lines} == {"p0"}
    assert completed.stderr == ""


@pytest.mark.unit
def test_every_declared_docs_reference_is_a_published_section(
    catalogue: stories.Catalogue, published: set[str]
) -> None:
    problems: list[str] = []
    for story in catalogue.stories:
        for ref in story.match.docs:
            page, _, anchor = ref.partition("#")
            if f"docs/{page}" not in published:
                problems.append(
                    f"story {story.id}: match.docs {ref!r} names docs/{page}, which "
                    "docs-site/setup.sh does not publish; edit scripts/ux_stories.toml"
                )
                continue
            if not anchor:
                continue
            text = (REPO_ROOT / "docs" / page).read_text(encoding="utf-8")
            if anchor not in check_markdown_links.anchors(text):
                problems.append(
                    f"story {story.id}: match.docs {ref!r} names no heading of "
                    f"docs/{page}; edit scripts/ux_stories.toml, or restore the heading"
                )
            elif anchor not in _section_anchors(text):
                problems.append(
                    f"story {story.id}: match.docs {ref!r} is not an H1-H3 heading of "
                    f"docs/{page}, so no documentation search returns it as a section; "
                    "edit scripts/ux_stories.toml to name the section that holds it"
                )
    assert not problems, "\n".join(problems)
    assert any(story.match.docs for story in catalogue.stories)


@pytest.mark.unit
def test_section_anchors_are_the_h1_to_h3_headings_deduplicated_like_anchors() -> None:
    text = "\n".join(
        [
            "---",
            "title: Page",
            "---",
            "# Page",
            "## Setup",
            "#### Deep",
            "```bash",
            "## not a heading",
            "```",
            "### Setup",
            "Underlined",
            "----------",
            "## Underlined",
            "> ## Quoted",
        ]
    )
    assert _section_anchors(text) == {
        "page",
        "setup",
        "setup-1",
        "underlined-1",
        "quoted",
    }
    assert _section_anchors(text) <= check_markdown_links.anchors(text)
    assert {"deep", "underlined"} <= check_markdown_links.anchors(text)


@pytest.mark.unit
def test_the_permission_matrix_parser_reads_every_row_of_docs_rbac() -> None:
    columns, rows = _rbac_matrix(RBAC_DOC.read_text(encoding="utf-8"))
    assert columns == ("Admin", "Author", "Reviewer", "Viewer")
    assert set(stories.SIGN_IN_GROUPS) <= set(columns)
    assert {
        "Upload documents",
        "View document details",
        "View/Edit configuration",
    } <= set(rows)
    unread = {
        (label, group, cell)
        for label, cells in rows.items()
        for group, cell in cells.items()
        if _cell_allows(cell) is None
    }
    assert not unread
    body = (
        RBAC_DOC.read_text(encoding="utf-8")
        .split(MATRIX_HEADING, 1)[1]
        .split("\n## ", 1)[0]
    )
    row_like = [
        line for line in body.split("\n") if line.startswith("  ") and line.strip()
    ]
    assert len(row_like) == len(rows)


@pytest.mark.unit
def test_every_rbac_row_exists_and_agrees_with_each_persona_expectation(
    catalogue: stories.Catalogue,
) -> None:
    columns, rows = _rbac_matrix(RBAC_DOC.read_text(encoding="utf-8"))
    problems: list[str] = []
    checked = 0
    for story in catalogue.stories:
        if story.rbac is None:
            refused = [
                name for name, expected in story.expectations if expected != "allowed"
            ]
            if refused:
                problems.append(
                    f"story {story.id}: expects {', '.join(refused)} to be refused but "
                    "names no rbac row; a refusal rests on a row of the Permission Matrix "
                    'in docs/rbac.md, so name it with rbac = "<row>" in '
                    "scripts/ux_stories.toml, adding the row to docs/rbac.md if it is "
                    "missing"
                )
            continue
        row = rows.get(story.rbac)
        if row is None:
            problems.append(
                f"story {story.id}: rbac = {story.rbac!r} names no row of the Permission "
                "Matrix in docs/rbac.md; edit scripts/ux_stories.toml to name an existing "
                "row, or docs/rbac.md if the row was renamed"
            )
            continue
        for persona, expectation in story.expectations:
            group = catalogue.persona(persona).group
            cell = row.get(group, "")
            allows = _cell_allows(cell)
            checked += 1
            if allows is None or allows != (expectation == "allowed"):
                problems.append(
                    f"story {story.id}: as.{persona} = {expectation} ({group}), but row "
                    f"{story.rbac!r} of docs/rbac.md's Permission Matrix has {cell!r} for "
                    f"{group}; fix scripts/ux_stories.toml or docs/rbac.md, whichever "
                    "is wrong"
                )
    assert not problems, "\n".join(problems)
    assert checked >= 2
    assert columns


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cell", "allows"),
    [
        ("✅", True),
        ("✅†", True),
        ("✅*†", True),
        ("✅*", True),
        ("❌", False),
        ("✅x", None),
        ("-", None),
    ],
)
def test_a_tick_with_its_footnote_marks_allows_and_a_cross_does_not(
    cell: str, allows: bool | None
) -> None:
    assert _cell_allows(cell) is allows


@pytest.mark.unit
def test_every_claim_is_quoted_verbatim_as_exactly_one_changelog_bold_lead(
    catalogue: stories.Catalogue,
) -> None:
    leads = _bold_leads(CHANGELOG.read_text(encoding="utf-8"))
    problems: list[str] = []
    claimed = 0
    for story in catalogue.stories:
        for claim in story.claims:
            claimed += 1
            wanted = " ".join(claim.lead.split())
            sections = [section for section, lead in leads if lead == wanted]
            if len(sections) != 1:
                problems.append(
                    f"story {story.id} ({CATALOGUE_FILE}:{_claim_line(story.id)}): claim "
                    f"{claim.lead!r} is the bold lead of {len(sections)} CHANGELOG.md "
                    "entries, not exactly one; when an entry is reworded, reword the claim"
                    " to match"
                )
    assert not problems, "\n".join(problems)
    assert claimed


@pytest.mark.unit
def test_the_claim_check_reads_bold_leads_in_any_section() -> None:
    lead = "Configurations can be chosen by folder."
    unreleased = "\n".join(
        [
            "# Changelog",
            "## [Unreleased]",
            "### Added",
            f"- **{lead}** More text",
            "  that continues.",
            "- ⚠️ **A warned entry.** Body.",
            "## [0.6.12]",
            "### Fixed",
            "- **Another entry.** Body.",
            "* **A starred",
            "  entry.** Body.",
        ]
    )
    assert _bold_leads(unreleased) == [
        ("Unreleased", lead),
        ("Unreleased", "A warned entry."),
        ("0.6.12", "Another entry."),
        ("0.6.12", "A starred entry."),
    ]
    released = unreleased.replace("## [Unreleased]", "## [0.6.13]")
    assert [s for s, found in _bold_leads(released) if found == lead] == ["0.6.13"]
    reworded = unreleased.replace(lead, "Configurations are chosen by folder.")
    assert [found for _, found in _bold_leads(reworded) if found == lead] == []
    not_a_lead = "## [Unreleased]\n- Plain text **" + lead + "**"
    assert _bold_leads(not_a_lead) == []


@pytest.mark.unit
def test_no_since_is_above_the_release_of_version(catalogue: stories.Catalogue) -> None:
    version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    ceiling = stories.release_segment(version)
    assert ceiling is not None, f"VERSION {version!r} has no release segment"
    late = [
        f"story {story.id}: since {story.since} is above VERSION's release {version}, so "
        "the story would never run; edit scripts/ux_stories.toml"
        for story in catalogue.stories
        if stories.since_reached(story.since, version) is False
    ]
    assert not late, "\n".join(late)


@pytest.mark.unit
def test_sign_in_groups_are_the_session_helper_groups_without_annotator() -> None:
    """The groups as the session helper computes them, which it may read from
    ``template.yaml`` rather than spell out, so the module is loaded by path."""
    spec = importlib.util.spec_from_file_location(
        "ux_test_session", SCRIPTS / "ux_test_session.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    groups = module.VALID_GROUPS
    assert "Annotator" in groups
    assert stories.SIGN_IN_GROUPS == tuple(
        group for group in groups if group != "Annotator"
    )


@pytest.mark.unit
def test_every_persona_group_can_sign_in(catalogue: stories.Catalogue) -> None:
    assert catalogue.personas
    assert {p.group for p in catalogue.personas.values()} <= set(stories.SIGN_IN_GROUPS)


@pytest.mark.unit
def test_every_ui_paths_glob_matches_a_tracked_file(
    catalogue: stories.Catalogue,
) -> None:
    tracked = [
        p.relative_to(REPO_ROOT).as_posix() for p in tracked_paths(REPO_ROOT, "*")
    ]
    problems = [
        f"story {story.id}: match.ui_paths glob {glob!r} matches no tracked file; "
        "edit scripts/ux_stories.toml"
        for story in catalogue.stories
        for glob in _unmatched_globs(list(story.match.ui_paths), tracked)
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.unit
def test_the_tracked_glob_check_finds_a_glob_that_matches_nothing() -> None:
    tracked = ["src/ui/src/App.tsx", "src/ui/package.json"]
    assert _unmatched_globs(["src/ui/src/*.tsx", "src/ui/src/gone/**"], tracked) == [
        "src/ui/src/gone/**"
    ]
