#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The first-time-user story catalogue, ``scripts/ux_stories.toml``."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, TypeVar

_HERE = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0] or ".").resolve() == _HERE:
    sys.path.pop(0)
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from ux_runner import python_floor  # noqa: E402

python_floor.require(__file__)

import tomllib  # noqa: E402

CATALOGUE_VERSION = 1
CATALOGUE_PATH = Path(__file__).resolve().parents[1] / "ux_stories.toml"
EXPECTATIONS: tuple[str, ...] = ("allowed", "refused", "not_offered")
KINDS: tuple[str, ...] = ("read", "commit_point")
TIERS: tuple[str, ...] = ("p0", "p1")
ORIGINS: tuple[str, ...] = ("changelog", "smoke", "finding")
SIGN_IN_GROUPS: tuple[str, ...] = ("Admin", "Author", "Reviewer", "Viewer")
CONTROL_WORDS: tuple[str, ...] = (
    "click",
    "press",
    "tap",
    "button",
    "menu",
    "tab",
    "link",
    "dropdown",
    "checkbox",
    "sidebar",
    "modal",
    "select the",
)
ID_CHARS = 60
TITLE_CHARS = 80
INTENT_CHARS = 600
BACKGROUND_CHARS = 400
HASH_CHARS = 12
ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
SINCE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
DOC_REF_RE = re.compile(r"[A-Za-z0-9._/-]+\.md(?:#[^\s#]+)?")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOP_KEYS = ("version", "limits", "runs", "personas", "fixtures", "stories")
_TOP_REQUIRED = ("version", "personas", "stories")
_PERSONA_KEYS = ("group", "background")
_FIXTURE_KEYS = ("description",)
_STORY_KEYS = (
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
_STORY_REQUIRED = (
    "id",
    "title",
    "intent",
    "kind",
    "documented_steps",
    "as",
    "tier",
    "since",
    "origin",
)
_MATCH_KEYS = ("docs", "issues", "terms", "ui_paths")
_CLAIM_KEYS = ("lead",)
_INTENT_ONLY = "stories state intent only"
_FOREIGN_STORY_KEYS: dict[str, str] = {
    "steps": _INTENT_ONLY,
    "expect": _INTENT_ONLY,
    "setup": _INTENT_ONLY,
    "ux_watch": _INTENT_ONLY,
    "persona": "use `as`",
    "priority": "use `tier`",
    "paths": "use `match.ui_paths`",
    "story": "use `intent`",
    "stages": "a story is one intent; stages are deferred",
    "verify": "a story has no verify step; the journal is what is judged",
}
_FOREIGN_TOP_KEYS: dict[str, str] = {
    "flows": (
        "stories are [[stories]] tables; scripts/ux_flows.yaml holds the UX "
        "test's flows"
    ),
}
_ANNOTATOR = "Annotator"
_LIMIT_CEILINGS: dict[str, float] = {
    "documented_steps_max": 10,
    "flow_usd": 20.0,
    "flow_minutes": 60,
}
_DESCRIPTION_CHARS = 200
_LEAD_CHARS = 300
_RBAC_CHARS = 80
_TERM_CHARS = 60
_GLOB_CHARS = 200
_TURN_CEILING = 200
_SHOWN_CHARS = 60
_CONTROL_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_CONTROL_WORD_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(word) for word in CONTROL_WORDS if " " not in word)
    + r")(?:s|es|ed|ing)?(?!\w)",
    re.IGNORECASE,
)
_CONTROL_PHRASE_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(word) for word in CONTROL_WORDS if " " in word)
    + r")(?!\w)",
    re.IGNORECASE,
)
_QUOTE_MARK_RE = re.compile(r"[\"“”`]")
_QUOTED_SPAN_RE = re.compile(
    r"(?<![A-Za-z0-9])['‘’][^'‘’\n]{1,80}" r"['‘’](?![A-Za-z0-9])"
)
_DOCS_MENTION_RE = re.compile(
    r"(?<!\w)docs/|\.md(?!\w)|https?://|(?<!\w)www\.", re.IGNORECASE
)
_LINT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("control word", _CONTROL_WORD_RE),
    ("control word", _CONTROL_PHRASE_RE),
    ("quote mark", _QUOTE_MARK_RE),
    ("quoted label", _QUOTED_SPAN_RE),
    ("docs path or URL", _DOCS_MENTION_RE),
)


class CatalogueError(Exception):
    """The catalogue is unreadable or breaks a loader rule; the message says where."""


@dataclass(frozen=True)
class Limits:
    step_budget_multiple: int = 10
    step_budget_floor: int = 30
    documented_steps_max: int = 6
    flow_usd: float = 4.0
    flow_minutes: int = 12


@dataclass(frozen=True)
class Runs:
    mr_stories: int = 2
    mr_flows: int = 3
    mr_runs: int = 4
    job_usd: float = 10.0
    extended_flow_usd: float = 6.0
    deadline_minutes: int = 55
    setup_minutes: int = 6
    reserve_minutes: int = 12


@dataclass(frozen=True)
class Persona:
    id: str
    group: str
    background: str


@dataclass(frozen=True)
class Fixture:
    id: str
    description: str


@dataclass(frozen=True)
class Claim:
    lead: str


@dataclass(frozen=True)
class Match:
    docs: tuple[str, ...] = ()
    issues: tuple[int, ...] = ()
    terms: tuple[str, ...] = ()
    ui_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Story:
    id: str
    title: str
    intent: str
    kind: str
    documented_steps: int
    expectations: tuple[tuple[str, str], ...]
    mr_as: tuple[str, ...]
    needs: tuple[str, ...]
    first_visit: bool
    tier: str
    since: str
    origin: str
    claims: tuple[Claim, ...]
    rbac: str | None
    match: Match

    @property
    def personas(self) -> tuple[str, ...]:
        return tuple(persona for persona, _ in self.expectations)

    def expectation_for(self, persona: str) -> str:
        """The expectation declared for ``persona``; KeyError when not in ``as``."""
        for name, expectation in self.expectations:
            if name == persona:
                return expectation
        raise KeyError(persona)


@dataclass(frozen=True, order=True)
class FlowKey:
    story: str
    persona: str

    def __str__(self) -> str:
        return f"{self.story}@{self.persona}"

    @classmethod
    def parse(cls, text: str) -> FlowKey:
        """``story@persona`` back into a key.

        ValueError unless both halves match ID_RE.
        """
        if not isinstance(text, str):
            raise ValueError("a flow key is text of the form story@persona")
        story, at, persona = text.partition("@")
        if not at or not ID_RE.fullmatch(story) or not ID_RE.fullmatch(persona):
            raise ValueError(
                f"{_shown(text)} is not a flow key of the form story@persona"
            )
        return cls(story=story, persona=persona)


@dataclass(frozen=True)
class Catalogue:
    version: int
    limits: Limits
    runs: Runs
    personas: Mapping[str, Persona]
    fixtures: Mapping[str, Fixture]
    stories: tuple[Story, ...]

    def story(self, story_id: str) -> Story:
        """The story with ``story_id``; KeyError when there is none."""
        for story in self.stories:
            if story.id == story_id:
                return story
        raise KeyError(story_id)

    def persona(self, persona_id: str) -> Persona:
        """The persona with ``persona_id``; KeyError when there is none."""
        return self.personas[persona_id]


@dataclass(frozen=True)
class SeriesInputs:
    model: str
    effort: str
    prompt_revision: int
    judgement_revision: int
    relay_revision: int
    claude_code: str
    chrome_devtools_mcp: str
    chrome: str
    brief_sha256: str
    ci_contract_sha256: str


_Numbers = TypeVar("_Numbers", Limits, Runs)


def _shown(value: object) -> str:
    text = repr(value)
    if len(text) <= _SHOWN_CHARS:
        return text
    return text[: _SHOWN_CHARS - 3] + "..."


def _display(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _table(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogueError(f"{where} must be a table")
    return value


def _closed(
    body: Mapping[str, Any],
    where: str,
    keys: Sequence[str],
    required: Sequence[str] = (),
    foreign: Mapping[str, str] | None = None,
) -> None:
    for key in body:
        if foreign is not None and key in foreign:
            raise CatalogueError(f"{where}: key {key!r} is refused: {foreign[key]}")
        if key not in keys:
            raise CatalogueError(
                f"{where}: unknown key {_shown(key)} (the keys are {', '.join(keys)})"
            )
    for key in required:
        if key not in body:
            raise CatalogueError(f"{where}: {key} is required")


def _bounds(low: float, high: float | None) -> str:
    if high is None:
        return f"at least {low:g}"
    return f"from {low:g} to {high:g}"


def _count(value: object, where: str, key: str, high: float | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogueError(f"{where}: {key} must be a whole number")
    if value < 1 or (high is not None and value > high):
        raise CatalogueError(
            f"{where}: {key} must be {_bounds(1, high)}, not {_shown(value)}"
        )
    return value


def _amount(value: object, where: str, key: str, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogueError(f"{where}: {key} must be a number")
    try:
        amount = float(value)
    except OverflowError:
        amount = math.inf
    if not math.isfinite(amount) or amount <= 0 or (high is not None and amount > high):
        ceiling = "" if high is None else f" and at most {high:g}"
        raise CatalogueError(
            f"{where}: {key} must be above 0{ceiling}, not {_shown(value)}"
        )
    return amount


def _numbers(
    value: object, where: str, cls: type[_Numbers], ceilings: Mapping[str, float]
) -> _Numbers:
    body = _table(value, where)
    names = tuple(item.name for item in fields(cls))
    _closed(body, where, names)
    parsed: dict[str, Any] = {}
    for item in fields(cls):
        if item.name not in body:
            continue
        reader = _amount if isinstance(item.default, float) else _count
        parsed[item.name] = reader(
            body[item.name], where, item.name, ceilings.get(item.name)
        )
    return cls(**parsed)


def _text(value: object, where: str, key: str, high: int) -> str:
    if not isinstance(value, str):
        raise CatalogueError(f"{where}: {key} must be text")
    if not 1 <= len(value) <= high:
        raise CatalogueError(
            f"{where}: {key} must be 1 to {high} characters, not {len(value)}"
        )
    if not value.strip():
        raise CatalogueError(f"{where}: {key} must not be blank")
    if any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in value):
        raise CatalogueError(f"{where}: {key} must be one line of text")
    return value


def _prose(value: object, where: str, key: str, high: int) -> str:
    text = _text(value, where, key, high)
    problems = lint_text(text)
    if problems:
        raise CatalogueError(
            f"{where}: {key} must state intent only ({'; '.join(problems)})"
        )
    return text


def _choice(value: object, where: str, key: str, choices: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise CatalogueError(
            f"{where}: {key} must be one of {', '.join(choices)}, not {_shown(value)}"
        )
    return value


def _identifier(value: object, where: str, key: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > ID_CHARS
        or not ID_RE.fullmatch(value)
    ):
        raise CatalogueError(
            f"{where}: {key} {_shown(value)} must be lowercase words joined by "
            f"hyphens, at most {ID_CHARS} characters"
        )
    return value


def _unique(
    value: object,
    where: str,
    key: str,
    item: Callable[[object, str, str], Any],
    *,
    nonempty: bool = False,
) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)) or (nonempty and not value):
        shape = "a non-empty array" if nonempty else "an array"
        raise CatalogueError(f"{where}: {key} must be {shape}")
    items: list[Any] = []
    for entry in value:
        checked = item(entry, where, key)
        if checked in items:
            raise CatalogueError(f"{where}: {key} lists {_shown(checked)} twice")
        items.append(checked)
    return tuple(items)


def _string_item(high: int) -> Callable[[object, str, str], str]:
    def read(value: object, where: str, key: str) -> str:
        return _text(value, where, f"each {key} entry", high)

    return read


def _doc_ref(value: object, where: str, key: str) -> str:
    if not isinstance(value, str) or not DOC_REF_RE.fullmatch(value):
        raise CatalogueError(
            f"{where}: {key} entry {_shown(value)} must be page.md or "
            "page.md#anchor, relative to docs/"
        )
    segments = value.split("#", 1)[0].split("/")
    if segments[0] == "docs" or any(part in ("", ".", "..") for part in segments):
        raise CatalogueError(
            f"{where}: {key} entry {_shown(value)} must be relative to docs/"
        )
    return value


def _issue(value: object, where: str, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CatalogueError(
            f"{where}: {key} entry {_shown(value)} must be a positive issue number"
        )
    return value


def _personas(value: object, source: str) -> dict[str, Persona]:
    where = f"{source}: [personas]"
    body = _table(value, where)
    personas: dict[str, Persona] = {}
    for persona_id, entry in body.items():
        _identifier(persona_id, where, "persona id")
        here = f"{source}: [personas.{persona_id}]"
        table = _table(entry, here)
        _closed(table, here, _PERSONA_KEYS, _PERSONA_KEYS)
        group = table["group"]
        if group == _ANNOTATOR:
            raise CatalogueError(
                f"{here}: group Annotator stays refused until `--long-lived` "
                "supports it"
            )
        personas[persona_id] = Persona(
            id=persona_id,
            group=_choice(group, here, "group", SIGN_IN_GROUPS),
            background=_text(table["background"], here, "background", BACKGROUND_CHARS),
        )
    return personas


def _fixtures(value: object, source: str) -> dict[str, Fixture]:
    where = f"{source}: [fixtures]"
    body = _table(value, where)
    fixtures: dict[str, Fixture] = {}
    for fixture_id, entry in body.items():
        _identifier(fixture_id, where, "fixture id")
        here = f"{source}: [fixtures.{fixture_id}]"
        table = _table(entry, here)
        _closed(table, here, _FIXTURE_KEYS, _FIXTURE_KEYS)
        fixtures[fixture_id] = Fixture(
            id=fixture_id,
            description=_text(
                table["description"], here, "description", _DESCRIPTION_CHARS
            ),
        )
    return fixtures


def _expectations(
    value: object, where: str, kind: str, personas: Mapping[str, Persona]
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping) or not value:
        raise CatalogueError(
            f"{where}: as must be a non-empty table of persona = expectation"
        )
    pairs: list[tuple[str, str]] = []
    for persona, expectation in value.items():
        if persona not in personas:
            raise CatalogueError(
                f"{where}: as names persona {_shown(persona)}, which [personas] "
                "does not define"
            )
        _choice(expectation, where, f"as.{persona}", EXPECTATIONS)
        if kind == "commit_point" and expectation == "refused":
            raise CatalogueError(
                f"{where}: as.{persona} is refused, but a commit_point story declares "
                "only allowed or not_offered: a refusal is invisible when nothing is "
                "pressed"
            )
        pairs.append((persona, expectation))
    return tuple(pairs)


def _since(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not SINCE_RE.fullmatch(value)
    ):
        raise CatalogueError(
            f"{where}: since must be a release segment such as 0.6.12, "
            f"not {_shown(value)}"
        )
    return value


def _claims(value: object, where: str) -> tuple[Claim, ...]:
    if not isinstance(value, (list, tuple)):
        raise CatalogueError(f"{where}: claims must be an array of {{ lead = ... }}")
    claims: list[Claim] = []
    for number, entry in enumerate(value, start=1):
        here = f"{where}: claim #{number}"
        table = _table(entry, here)
        _closed(table, here, _CLAIM_KEYS, _CLAIM_KEYS)
        lead = _text(table["lead"], here, "lead", _LEAD_CHARS)
        if any(claim.lead == lead for claim in claims):
            raise CatalogueError(f"{here}: lead repeats an earlier claim of this story")
        claims.append(Claim(lead=lead))
    return tuple(claims)


def _match(value: object, where: str) -> Match:
    here = f"{where}: match"
    body = _table(value, here)
    _closed(body, here, _MATCH_KEYS)
    return Match(
        docs=_unique(body.get("docs", []), where, "match.docs", _doc_ref),
        issues=_unique(body.get("issues", []), where, "match.issues", _issue),
        terms=_unique(
            body.get("terms", []), where, "match.terms", _string_item(_TERM_CHARS)
        ),
        ui_paths=_unique(
            body.get("ui_paths", []),
            where,
            "match.ui_paths",
            _string_item(_GLOB_CHARS),
        ),
    )


def _member(names: Sequence[str], owner: str) -> Callable[[object, str, str], str]:
    def read(value: object, where: str, key: str) -> str:
        if not isinstance(value, str) or value not in names:
            raise CatalogueError(
                f"{where}: {key} names {_shown(value)}, which {owner} does not hold"
            )
        return value

    return read


def _story(
    entry: object,
    source: str,
    number: int,
    limits: Limits,
    personas: Mapping[str, Persona],
    fixtures: Mapping[str, Fixture],
) -> Story:
    numbered = f"{source}: story #{number}"
    body = _table(entry, numbered)
    ident = body.get("id")
    named = isinstance(ident, str) and len(ident) <= ID_CHARS and ID_RE.fullmatch(ident)
    where = f"{source}: story {ident!r}" if named else numbered
    _closed(body, where, _STORY_KEYS, _STORY_REQUIRED, _FOREIGN_STORY_KEYS)
    story_id = _identifier(body["id"], where, "id")
    title = _prose(body["title"], where, "title", TITLE_CHARS)
    intent = _prose(body["intent"], where, "intent", INTENT_CHARS)
    kind = _choice(body["kind"], where, "kind", KINDS)
    documented_steps = _count(
        body["documented_steps"], where, "documented_steps", limits.documented_steps_max
    )
    expectations = _expectations(body["as"], where, kind, personas)
    names = tuple(persona for persona, _ in expectations)
    mr_as = names[:1]
    if "mr_as" in body:
        mr_as = _unique(
            body["mr_as"], where, "mr_as", _member(names, "as"), nonempty=True
        )
    needs = _unique(
        body.get("needs", []), where, "needs", _member(tuple(fixtures), "[fixtures]")
    )
    first_visit = body.get("first_visit", False)
    if not isinstance(first_visit, bool):
        raise CatalogueError(f"{where}: first_visit must be true or false")
    tier = _choice(body["tier"], where, "tier", TIERS)
    since = _since(body["since"], where)
    origin = _choice(body["origin"], where, "origin", ORIGINS)
    claims = _claims(body.get("claims", []), where)
    if origin == "changelog" and not claims:
        raise CatalogueError(
            f"{where}: origin changelog needs at least one claim, the CHANGELOG "
            "lead the story covers"
        )
    rbac = None
    if "rbac" in body:
        rbac = _text(body["rbac"], where, "rbac", _RBAC_CHARS)
    return Story(
        id=story_id,
        title=title,
        intent=intent,
        kind=kind,
        documented_steps=documented_steps,
        expectations=expectations,
        mr_as=mr_as,
        needs=needs,
        first_visit=first_visit,
        tier=tier,
        since=since,
        origin=origin,
        claims=claims,
        rbac=rbac,
        match=_match(body.get("match", {}), where),
    )


def load_catalogue(path: Path = CATALOGUE_PATH) -> Catalogue:
    """Read and validate the TOML catalogue.

    A CatalogueError names the file, the story or table, and the key.
    """
    source = _display(path)
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise CatalogueError(
            f"{source}: cannot be read ({error.strerror or type(error).__name__})"
        ) from error
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise CatalogueError(f"{source}: is not UTF-8 text") from error
    except tomllib.TOMLDecodeError as error:
        raise CatalogueError(f"{source}: is not valid TOML: {error}") from error
    return parse_catalogue(data, source=source)


def parse_catalogue(
    data: Mapping[str, Any], *, source: str = "<catalogue>"
) -> Catalogue:
    """The same validation over an already-parsed mapping."""
    if not isinstance(data, Mapping):
        raise CatalogueError(f"{source}: the catalogue must be a table")
    _closed(data, source, _TOP_KEYS, _TOP_REQUIRED, _FOREIGN_TOP_KEYS)
    version = data["version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != CATALOGUE_VERSION
    ):
        raise CatalogueError(
            f"{source}: version must be {CATALOGUE_VERSION}, not {_shown(version)}"
        )
    limits = _numbers(
        data.get("limits", {}), f"{source}: [limits]", Limits, _LIMIT_CEILINGS
    )
    runs = _numbers(data.get("runs", {}), f"{source}: [runs]", Runs, {})
    personas = _personas(data["personas"], source)
    fixtures = _fixtures(data.get("fixtures", {}), source)
    entries = data["stories"]
    if not isinstance(entries, (list, tuple)) or not entries:
        raise CatalogueError(f"{source}: stories must be a non-empty array of tables")
    stories: list[Story] = []
    for number, entry in enumerate(entries, start=1):
        story = _story(entry, source, number, limits, personas, fixtures)
        earlier = next(
            (index for index, seen in enumerate(stories, 1) if seen.id == story.id),
            None,
        )
        if earlier is not None:
            raise CatalogueError(
                f"{source}: story #{number}: id {story.id!r} is also the id of "
                f"story #{earlier}"
            )
        stories.append(story)
    return Catalogue(
        version=version,
        limits=limits,
        runs=runs,
        personas=personas,
        fixtures=fixtures,
        stories=tuple(stories),
    )


def lint_text(text: str) -> list[str]:
    """The intent-only problems in ``text``, as short messages; [] when clean."""
    found: list[tuple[int, str]] = []
    for label, pattern in _LINT_RULES:
        found.extend(
            (match.start(), f"{label}: {match.group(0)}")
            for match in pattern.finditer(text)
        )
    found.sort(key=lambda item: item[0])
    return list(dict.fromkeys(message for _, message in found))


def budget(limits: Limits, story: Story, *, scale: int = 1) -> int:
    """scale * max(step_budget_multiple * documented_steps, step_budget_floor)."""
    if isinstance(scale, bool) or not isinstance(scale, int) or scale < 1:
        raise ValueError(
            f"the budget scale must be a whole number of at least 1, not {scale!r}"
        )
    return scale * max(
        limits.step_budget_multiple * story.documented_steps, limits.step_budget_floor
    )


def turn_cap(budget: int) -> int:
    """min(3 * budget + 20, 200)."""
    return min(3 * budget + 20, _TURN_CEILING)


def short_hash(value: object) -> str:
    """The first HASH_CHARS hex digits of the sha256 of ``value``'s canonical JSON."""
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:HASH_CHARS]


def story_hash(catalogue: Catalogue, story: Story) -> str:
    """The short hash of what the agent is asked and how it is bounded."""
    limits = asdict(catalogue.limits)
    limits["flow_usd"] = float(catalogue.limits.flow_usd)
    return short_hash(
        {
            "story": {
                "id": story.id,
                "title": story.title,
                "intent": story.intent,
                "kind": story.kind,
                "documented_steps": story.documented_steps,
                "as": [
                    [persona, expectation]
                    for persona, expectation in story.expectations
                ],
                "needs": list(story.needs),
                "first_visit": story.first_visit,
            },
            "personas": {
                persona: {
                    "group": catalogue.persona(persona).group,
                    "background": catalogue.persona(persona).background,
                }
                for persona in story.personas
            },
            "limits": limits,
        }
    )


def series_hash(story_hash: str, persona: str, inputs: SeriesInputs) -> str:
    """The short hash naming one flow's like-for-like series across nights."""
    return short_hash({"story_hash": story_hash, "persona": persona, **asdict(inputs)})


def flow_keys(
    catalogue: Catalogue,
    *,
    tiers: Collection[str] = ("p0",),
    story: str | None = None,
    persona: str | None = None,
) -> list[FlowKey]:
    """Every (story, persona) in ``as`` for the tiers, filtered.

    Sorted by FlowKey order: story, then persona.
    """
    chosen = {tiers} if isinstance(tiers, str) else set(tiers)
    stray = sorted(repr(tier) for tier in chosen if tier not in TIERS)
    if stray:
        raise ValueError(
            f"unknown tier {', '.join(stray)}; the tiers are {', '.join(TIERS)}"
        )
    return sorted(
        FlowKey(story=entry.id, persona=name)
        for entry in catalogue.stories
        if entry.tier in chosen and (story is None or entry.id == story)
        for name in entry.personas
        if persona is None or name == persona
    )


def mr_personas(story: Story) -> tuple[str, ...]:
    """mr_as first, in order, then the rest of ``as`` in TOML order."""
    return story.mr_as + tuple(
        name for name in story.personas if name not in story.mr_as
    )


def shard(keys: Sequence[FlowKey], index: int, total: int) -> list[FlowKey]:
    """Balanced round robin over the sorted keys, with a 1-based ``index``.

    The keys whose position modulo ``total`` is ``index - 1``.
    """
    if isinstance(total, bool) or not isinstance(total, int) or total < 1:
        raise ValueError(f"a shard total must be at least 1, not {total!r}")
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= total:
        raise ValueError(f"a shard index must be from 1 to {total}, not {index!r}")
    return sorted(keys)[index - 1 :: total]


def release_segment(version: str | None) -> tuple[int, int, int] | None:
    """The leading ``M.m.p`` of a version; None when it has none.

    ``0.6.12.dev1`` gives (0, 6, 12).
    """
    if not isinstance(version, str):
        return None
    found = SINCE_RE.match(version.strip())
    if found is None:
        return None
    try:
        return int(found.group(1)), int(found.group(2)), int(found.group(3))
    except ValueError:
        return None


def since_reached(since: str, stack_version: str | None) -> bool | None:
    """release_segment(stack_version) >= since.

    None when the version is unreadable, and the gate is then skipped.
    """
    floor = (
        release_segment(since)
        if isinstance(since, str) and SINCE_RE.fullmatch(since)
        else None
    )
    if floor is None:
        raise ValueError(
            f"since must be a release segment such as 0.6.12, not {since!r}"
        )
    reached = release_segment(stack_version)
    return None if reached is None else reached >= floor


def _listing(catalogue: Catalogue, tiers: Sequence[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tier in tiers:
        for key in flow_keys(catalogue, tiers=(tier,)):
            entry = catalogue.story(key.story)
            rows.append(
                {
                    "tier": tier,
                    "key": str(key),
                    "story": key.story,
                    "persona": key.persona,
                    "group": catalogue.persona(key.persona).group,
                    "kind": entry.kind,
                    "expectation": entry.expectation_for(key.persona),
                    "budget": budget(catalogue.limits, entry),
                    "story_hash": story_hash(catalogue, entry),
                }
            )
    return rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser(
        "list", help="validate the catalogue and print one line per flow"
    )
    listing.add_argument(
        "--catalogue",
        type=Path,
        default=CATALOGUE_PATH,
        help="the catalogue to read (default: scripts/ux_stories.toml)",
    )
    listing.add_argument(
        "--tier",
        choices=(*TIERS, "all"),
        default="all",
        help="the tier to list (default: all)",
    )
    listing.add_argument(
        "--json", action="store_true", help="print one JSON object instead"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate the catalogue and list its flows; 0, or 1 on a CatalogueError."""
    args = _parser().parse_args(argv)
    try:
        catalogue = load_catalogue(args.catalogue)
    except CatalogueError as error:
        print(f"FAILED: {error}")
        return 1
    tiers = TIERS if args.tier == "all" else (args.tier,)
    rows = _listing(catalogue, tiers)
    if args.json:
        print(
            json.dumps(
                {"version": catalogue.version, "tiers": list(tiers), "flows": rows},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    for row in rows:
        print(
            f"{row['tier']}\t{row['key']}\t{row['kind']}\t{row['expectation']}\t"
            f"budget={row['budget']}\tstory_hash={row['story_hash']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
