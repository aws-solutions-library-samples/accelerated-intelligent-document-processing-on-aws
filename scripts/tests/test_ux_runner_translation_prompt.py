# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the screen review's prompt and report, ``ux_runner/translation_prompt.py``.

The glossary is a fixture written here in the shape of
``src/ui/src/i18n/glossary.fr-CA.csv`` (``en,fr-CA,note``), which the reader takes
as data through its path. The screens are held to the routes of
``src/ui/src/routes/constants.ts``, and the flows come from the real
``scripts/ux_flows.yaml``. The report parser is fed hostile answers: the checks
pin that every one of them ends as a dropped item, recorded with the rule it
broke, or an ``unreadable`` outcome, never as an exception or as text that was not
redacted, flattened and cut.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import flows, signin, translation_prompt  # noqa: E402

ROUTES_TS = REPO / "src" / "ui" / "src" / "routes" / "constants.ts"
FLOWS_YAML = SCRIPTS / "ux_flows.yaml"
BASE = "http://localhost:3000/"
GLOSSARY_CSV = (
    "en,fr-CA,note\n"
    "access key,clé d’accès,Noun.\n"
    "\n"
    'upload,téléverser,"Verb; Quebec usage, never télécharger."\n'
    "Review,Révision,Noun; the verb is réviser.\n"
)
SHOTS = {"fr-CA-documents.png", "fr-CA-upload.png", "fr-CA-other-1.png"}


def _glossary(tmp_path: Path, text: str = GLOSSARY_CSV, **kw: Any) -> Path:
    path = tmp_path / "glossary.fr-CA.csv"
    path.write_bytes(text.encode(kw.get("encoding", "utf-8")))
    return path


def _flows() -> list[flows.Flow]:
    return flows.load_flows(FLOWS_YAML)


def _prompt(locale: str, tmp_path: Path, base: str = BASE) -> str:
    glossary = translation_prompt.read_glossary(_glossary(tmp_path), "fr-CA")
    return translation_prompt.build_prompt(
        locale, base_url=base, flow_list=_flows(), glossary=glossary
    )


# The glossary ---------------------------------------------------------------


@pytest.mark.unit
def test_the_glossary_is_read_as_terms_with_their_notes(tmp_path: Path) -> None:
    terms = translation_prompt.read_glossary(_glossary(tmp_path), "fr-CA")
    assert terms == (
        translation_prompt.Term("access key", "clé d’accès", "Noun."),
        translation_prompt.Term(
            "upload", "téléverser", "Verb; Quebec usage, never télécharger."
        ),
        translation_prompt.Term("Review", "Révision", "Noun; the verb is réviser."),
    )


@pytest.mark.unit
def test_a_byte_order_mark_and_non_breaking_spaces_are_accepted(
    tmp_path: Path,
) -> None:
    text = "\ufeffen,fr-CA,note\nquote,« citation »,\u202fnarrow and\u00a0plain\n"
    (term,) = translation_prompt.read_glossary(_glossary(tmp_path, text), "fr-CA")
    assert term.translation == "« citation »"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("term,translation\n", r"glossary\.fr-CA\.csv:1: the header must be"),
        ("en,fr-CA,note\nlonely,cell\n", r":2: a row has three cells"),
        ("en,fr-CA,note\nterm,,note\n", r":2: a term needs its English and its fr-CA"),
        ("en,fr-CA,note\n,terme,\n", r":2: a term needs its English"),
        ("en,fr-CA,note\nterm,ter\x1bme,\n", r":2: a cell is not one line"),
        ('en,fr-CA,note\n\nterm,"two\nlines",\n', r":4: a cell is not one line"),
        ("en,fr-CA,note\nterm,terme," + "x" * 301 + "\n", r"of at most 300"),
    ],
    ids=[
        "header",
        "two-cells",
        "no-translation",
        "no-english",
        "control",
        "newline",
        "too-long",
    ],
)
def test_a_glossary_that_breaks_a_rule_is_refused_naming_its_line(
    tmp_path: Path, text: str, message: str
) -> None:
    with pytest.raises(translation_prompt.PromptError, match=message):
        translation_prompt.read_glossary(_glossary(tmp_path, text), "fr-CA")


@pytest.mark.unit
def test_a_glossary_too_large_unreadable_or_not_utf8_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(translation_prompt.PromptError, match="cannot be read"):
        translation_prompt.read_glossary(tmp_path / "missing.csv", "fr-CA")
    latin = _glossary(tmp_path, "en,fr-CA,note\nterm,thé,\n", encoding="latin-1")
    with pytest.raises(translation_prompt.PromptError, match="is not UTF-8"):
        translation_prompt.read_glossary(latin, "fr-CA")
    monkeypatch.setattr(translation_prompt, "MAX_GLOSSARY_BYTES", 64)
    with pytest.raises(translation_prompt.PromptError, match="larger than 0 KiB"):
        translation_prompt.read_glossary(_glossary(tmp_path), "fr-CA")


@pytest.mark.unit
def test_a_cell_past_the_csv_field_limit_is_refused_naming_its_line(
    tmp_path: Path,
) -> None:
    text = 'en,fr-CA,note\nterm,"' + "x" * 200_000 + '",\n'
    with pytest.raises(translation_prompt.PromptError, match=r":2: field larger"):
        translation_prompt.read_glossary(_glossary(tmp_path, text), "fr-CA")


@pytest.mark.unit
def test_a_glossary_with_more_terms_than_the_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(translation_prompt, "MAX_TERMS", 2)
    with pytest.raises(translation_prompt.PromptError, match="more than 2 terms"):
        translation_prompt.read_glossary(_glossary(tmp_path), "fr-CA")


@pytest.mark.unit
@pytest.mark.parametrize("locale", ["fr", "en", "fr-FR", ""])
def test_only_a_review_locale_has_a_glossary_or_a_prompt(
    tmp_path: Path, locale: str
) -> None:
    with pytest.raises(translation_prompt.PromptError, match="one of fr-CA, en-XA"):
        translation_prompt.read_glossary(_glossary(tmp_path), locale)
    with pytest.raises(translation_prompt.PromptError, match="one of fr-CA, en-XA"):
        translation_prompt.build_prompt(
            locale, base_url=BASE, flow_list=[], glossary=()
        )


# The screens ----------------------------------------------------------------


def _redirects() -> set[str]:
    """Each child route the router answers with a redirect, by its last segment."""
    found: set[str] = set()
    for source in (REPO / "src" / "ui" / "src" / "routes").glob("*Routes.tsx"):
        text = source.read_text(encoding="utf-8")
        found |= set(re.findall(r'<Route path="([\w-]+)" element=\{<Navigate', text))
    return found


def _routes() -> set[str]:
    """Every route ``constants.ts`` defines, its ``${OTHER}`` prefixes resolved."""
    values: dict[str, str] = {}
    source = ROUTES_TS.read_text(encoding="utf-8")
    pattern = re.compile(r"export const (\w+) = (?:'([^']*)'|`([^`]*)`);")
    for name, plain, template in pattern.findall(source):
        value = plain or template
        value = re.sub(r"\$\{(\w+)\}", lambda m: values.get(m.group(1), "?"), value)
        values[name] = value
    return {value for value in values.values() if value.startswith("/")}


@pytest.mark.unit
def test_no_screen_is_a_route_the_router_redirects_elsewhere() -> None:
    """``#/review-queues/supervise`` and ``#/test-studio/annotate`` both redirect
    to the review queues, so a review would call the wrong page reached."""
    redirects = _redirects()
    assert {"supervise", "annotate"} <= redirects
    for screen in translation_prompt.SCREENS:
        last = screen.path.partition("?")[0].rsplit("/", 1)[-1]
        assert last not in redirects, screen.path


@pytest.mark.unit
def test_every_screen_is_a_route_the_ui_defines() -> None:
    routes = _routes()
    source = ROUTES_TS.read_text(encoding="utf-8")
    ids = [screen.id for screen in translation_prompt.SCREENS]
    assert len(ids) == len(set(ids)), "a screen id twice"
    for screen in translation_prompt.SCREENS:
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", screen.id), screen.id
        assert screen.path.startswith("#/"), screen.path
        route, _, query = screen.path[1:].partition("?")
        if screen.id == "document":
            assert "documentDetailHref" in source
            assert "`#${DOCUMENTS_PATH}/${encodeURIComponent(" in source
            continue
        assert route in routes, f"{screen.path} is not a route of constants.ts"
        if query:
            tab = query.removeprefix("tab=")
            assert f"?tab={tab}" in source or f"'{tab}'" in source, screen.path


# The prompt -----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("locale", signin.REVIEW_LOCALES)
def test_the_prompt_is_the_same_for_the_same_inputs(
    tmp_path: Path, locale: str
) -> None:
    assert _prompt(locale, tmp_path) == _prompt(locale, tmp_path)
    assert _prompt(locale, tmp_path, "http://localhost:3000") == _prompt(
        locale, tmp_path
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "base", ["localhost:3000", "http://localhost:3000/#/", "https://x.example/?a=1"]
)
def test_a_base_url_that_would_corrupt_the_screen_urls_is_refused(base: str) -> None:
    with pytest.raises(translation_prompt.PromptError, match="origin and path only"):
        translation_prompt.build_prompt(
            "en-XA", base_url=base, flow_list=[], glossary=()
        )


@pytest.mark.unit
def test_the_prompt_asks_to_look_at_a_screenshot_before_saving_one(
    tmp_path: Path,
) -> None:
    """chrome-devtools-mcp saves a screenshot given a filePath instead of showing
    it, so looking and saving are two calls."""
    text = _prompt("fr-CA", tmp_path)
    assert "take_screenshot with no filePath to see its layout" in text
    assert "A screenshot with a filePath is saved, not shown to you." in text


@pytest.mark.unit
def test_no_persona_credential_account_or_stack_is_an_input_of_the_prompt() -> None:
    assert list(inspect.signature(translation_prompt.build_prompt).parameters) == [
        "locale",
        "base_url",
        "flow_list",
        "glossary",
        "screens",
    ]


@pytest.mark.unit
def test_the_french_prompt_carries_the_glossary_the_house_style_and_every_screen(
    tmp_path: Path,
) -> None:
    text = _prompt("fr-CA", tmp_path)
    assert "- access key → clé d’accès (Noun.)" in text
    assert "- upload → téléverser (Verb; Quebec usage, never télécharger.)" in text
    assert "vous, never tu" in text
    assert "A non-breaking space before a colon" in text
    assert "{count}" in text and "{{" not in text
    for screen in translation_prompt.SCREENS:
        assert f"| {screen.id} | {BASE}{screen.path} |" in text
    kinds = text.split("- kind: one of ", 1)[1].split(".\n", 1)[0].split(", ")
    assert kinds == list(translation_prompt.kinds_for("fr-CA"))
    assert "not_externalized" not in text
    assert f"Never open {BASE}#/logout" in text


@pytest.mark.unit
def test_the_pseudo_locale_prompt_has_no_glossary_and_its_own_rules(
    tmp_path: Path,
) -> None:
    text = _prompt("en-XA", tmp_path)
    assert "Glossary" not in text and "clé d’accès" not in text
    assert "House style" not in text
    assert "[Ŀȧȧƞɠŭŭȧȧɠḗḗ]" in text
    assert "not_externalized" in text and "english_left" not in text
    assert '"kind": "not_externalized"' in text


@pytest.mark.unit
def test_every_flow_goes_in_with_its_setup_and_steps(tmp_path: Path) -> None:
    text = _prompt("fr-CA", tmp_path)
    loaded = _flows()
    assert len(loaded) == 8
    for flow in loaded:
        assert f"### {flow.id}: {flow.title}" in text
        for step in flow.raw["steps"]:
            assert f"- {' '.join(step.split())}" in text
        for item in flow.raw.get("setup") or []:
            assert f"- {' '.join(item.split())}" in text


@pytest.mark.unit
@pytest.mark.parametrize("locale", signin.REVIEW_LOCALES)
def test_the_answer_the_prompt_shows_is_one_the_parser_reads(
    tmp_path: Path, locale: str
) -> None:
    """The schema in the prompt and the parser's rules are one contract."""
    text = _prompt(locale, tmp_path)
    example = text.split("```json\n", 1)[1].split("\n```", 1)[0]
    report = translation_prompt.parse_report(
        f"Done.\n```json\n{example}\n```\n",
        locale,
        flow_ids=[flow.id for flow in _flows()],
        screenshots={f"{locale}-documents.png"},
        field=str,
    )
    assert (report.outcome, report.dropped, len(report.findings)) == ("read", 0, 1)
    assert report.visits[0].reached is True


# The report -----------------------------------------------------------------


def _finding(**override: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "screen": "upload",
        "path": "#/documents/upload",
        "flow": None,
        "kind": "english_left",
        "severity": "medium",
        "shown": "Upload documents",
        "expected": "Téléverser des documents",
        "screenshot": "fr-CA-upload.png",
        "note": "page title",
    }
    item.update(override)
    return item


def _answer(**override: Any) -> str:
    value: dict[str, Any] = {
        "locale": "fr-CA",
        "screens": [
            {
                "id": "upload",
                "path": "#/documents/upload",
                "reached": True,
                "screenshot": "fr-CA-upload.png",
            },
            {"id": "users", "reached": False, "screenshot": None},
        ],
        "findings": [_finding()],
    }
    value.update(override)
    return "Reviewed.\n\n```json\n" + json.dumps(value, ensure_ascii=False) + "\n```\n"


def _parse(text: str | None, locale: str = "fr-CA") -> translation_prompt.Report:
    return translation_prompt.parse_report(
        text,
        locale,
        flow_ids=["5.1", "QS.1"],
        screenshots=SHOTS,
        field=lambda value: value.replace("SECRET", "[redacted]"),
    )


@pytest.mark.unit
def test_a_good_report_is_read_into_visits_and_findings() -> None:
    report = _parse(_answer())
    assert (report.locale, report.outcome, report.problem) == ("fr-CA", "read", "")
    assert report.visits == (
        translation_prompt.Visit(
            "upload", "#/documents/upload", True, "fr-CA-upload.png"
        ),
        translation_prompt.Visit("users", "", False, None),
    )
    assert report.findings == (
        translation_prompt.Finding(
            screen="upload",
            path="#/documents/upload",
            flow=None,
            kind="english_left",
            severity="medium",
            shown="Upload documents",
            expected="Téléverser des documents",
            screenshot="fr-CA-upload.png",
            note="page title",
        ),
    )
    assert (report.dropped, report.unconfirmed) == (0, 0)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (None, "the session ended without a final message"),
        ("", "the session ended without a final message"),
        ("All good, no findings.", "the final message holds no fenced json block"),
        ("```json\n{}\n", "the final message holds no fenced json block"),
        ("```json\n{not json}\n```", "the report is not valid JSON"),
        ("```json\n[]\n```", "the report is not a JSON object"),
        (_answer(locale="en-XA"), "the report is not for fr-CA"),
        (_answer(screens={}), "the report's screens or findings is not a list"),
        (_answer(findings="none"), "the report's screens or findings is not a list"),
    ],
    ids=[
        "none",
        "empty",
        "no-block",
        "unclosed",
        "not-json",
        "not-object",
        "other-locale",
        "screens-not-list",
        "findings-not-list",
    ],
)
def test_a_report_that_cannot_be_read_is_an_outcome_not_an_error(
    text: str | None, problem: str
) -> None:
    report = _parse(text)
    assert (report.outcome, report.problem) == ("unreadable", problem)
    assert (report.visits, report.findings) == ((), ())


@pytest.mark.unit
def test_a_report_larger_than_its_bound_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(translation_prompt, "MAX_REPORT_BYTES", 1024)
    report = _parse(_answer(findings=[_finding(note="n" * 290)] * 5))
    assert (report.outcome, report.problem) == (
        "unreadable",
        "the report is larger than 1 KiB",
    )


@pytest.mark.unit
def test_a_report_nested_past_the_parsers_depth_is_unreadable() -> None:
    report = _parse("```json\n" + "[" * 30_000 + "\n```\n")
    assert (report.outcome, report.problem) == (
        "unreadable",
        "the report is not valid JSON",
    )


@pytest.mark.unit
def test_a_lone_surrogate_in_a_field_cannot_break_writing_the_page() -> None:
    report = _parse(_answer(findings=[_finding(shown="ab\ud800cd")]))
    assert report.findings[0].shown == "ab?cd"
    translation_prompt.report_markdown(report).encode("utf-8")


@pytest.mark.unit
def test_the_last_json_block_is_the_report() -> None:
    text = 'An example:\n```json\n{"locale": "en-XA"}\n```\n' + _answer()
    assert _parse(text).outcome == "read"


_KIND = "its kind is not one a fr-CA review reports"
_SCREEN = "its screen is not one of the run's screens"
_FLOW = "its flow is not one of the run's flows"
_PATH = "its path is not a route of the UI"
_SHOT = "its screenshot is not one the harness copied"
_TEXT = "its shown, expected or note is not text"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"kind": "not_externalized"}, _KIND),
        ({"kind": "typo"}, _KIND),
        ({"kind": ["english_left"]}, _KIND),
        ({"severity": "critical"}, "its severity is not high, medium or low"),
        ({"screen": "admin"}, _SCREEN),
        ({"screen": ["upload"]}, _SCREEN),
        ({"flow": "9.9"}, _FLOW),
        ({"flow": "none"}, _FLOW),
        ({"flow": {"id": "5.1"}}, _FLOW),
        ({"path": "javascript:alert(1)"}, _PATH),
        ({"path": "#/documents\n"}, _PATH),
        ({"screenshot": "fr-CA-users.png"}, _SHOT),
        ({"screenshot": "en-XA-upload.png"}, _SHOT),
        ({"screenshot": "../fr-CA-upload.png"}, _SHOT),
        ({"screenshot": ["fr-CA-upload.png"]}, _SHOT),
        ({"shown": 42}, _TEXT),
        ({"note": ["a"]}, _TEXT),
    ],
)
def test_a_finding_that_breaks_a_rule_is_dropped_counted_and_recorded(
    override: dict[str, Any], reason: str
) -> None:
    report = _parse(_answer(findings=[_finding(), _finding(**override)]))
    assert report.outcome == "read"
    assert len(report.findings) == 1
    assert report.dropped == 1
    (refused,) = report.refused
    assert (refused.part, refused.index, refused.reason) == ("findings", 1, reason)


@pytest.mark.unit
@pytest.mark.parametrize("flow", ["", "  \t"], ids=["empty", "blank"])
def test_a_blank_flow_is_read_as_no_flow(flow: str) -> None:
    report = _parse(_answer(findings=[_finding(flow=flow)]))
    assert [finding.flow for finding in report.findings] == [None]
    assert (report.dropped, report.refused) == (0, ())


@pytest.mark.unit
def test_a_refused_item_keeps_its_own_fields_redacted_and_cut() -> None:
    item = _finding(
        flow="9.9",
        shown="SECRET\n" + "é" * 400,
        severity=7,
        path=["#/x"],
        extra="SECRET",
    )
    (refused,) = _parse(_answer(findings=[item])).refused
    assert (refused.part, refused.index, refused.reason) == ("findings", 0, _FLOW)
    kept = dict(refused.item or {})
    assert kept.pop("shown").startswith("[redacted] éé")
    assert kept == {
        "screen": "upload",
        "path": "(list)",
        "flow": "9.9",
        "kind": "english_left",
        "severity": "7",
        "expected": "Téléverser des documents",
        "screenshot": "fr-CA-upload.png",
        "note": "page title",
    }
    (cut,) = _parse(_answer(findings=[_finding(flow="9.9", note="n" * 400)])).refused
    assert len((cut.item or {})["note"]) == translation_prompt.FIELD_CHARS


@pytest.mark.unit
def test_a_finding_that_is_not_an_object_is_dropped_counted_and_recorded() -> None:
    report = _parse(_answer(findings=[_finding(), "english_left", None, 7, [1]]))
    assert (len(report.findings), report.dropped) == (1, 4)
    assert [(r.index, r.reason, r.item) for r in report.refused] == [
        (1, "it is not an object", {"value": "english_left"}),
        (2, "it is not an object", {"value": None}),
        (3, "it is not an object", {"value": "7"}),
        (4, "it is not an object", {"value": "(list)"}),
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("upload", "it is not an object"),
        ({"id": "admin", "reached": True}, "its id is not one of the run's screens"),
        ({"id": ["upload"], "reached": True}, "its id is not one of the run's screens"),
        ({"id": "upload", "reached": "yes"}, "its reached is not true or false"),
        ({"id": "upload", "reached": True, "screenshot": "fr-CA-users.png"}, _SHOT),
        (
            {"id": "upload", "reached": True, "path": "https://elsewhere.example/"},
            _PATH,
        ),
    ],
)
def test_a_screen_entry_that_breaks_a_rule_is_dropped_counted_and_recorded(
    entry: Any, reason: str
) -> None:
    report = _parse(_answer(screens=[entry]))
    assert (report.outcome, report.visits, report.dropped) == ("read", (), 1)
    (refused,) = report.refused
    assert (refused.part, refused.index, refused.reason) == ("screens", 0, reason)


@pytest.mark.unit
def test_a_screen_claimed_as_reached_without_its_screenshot_is_not_reached() -> None:
    report = _parse(
        _answer(screens=[{"id": "documents", "reached": True}], findings=[])
    )
    assert report.visits == (translation_prompt.Visit("documents", "", False, None),)
    assert report.unconfirmed == 1


@pytest.mark.unit
def test_other_screens_and_flows_are_kept_with_their_paths() -> None:
    finding = _finding(
        screen="other",
        path="#/test-studio/sets/set-1?tab=documents",
        flow="5.1",
        kind="clipped",
        screenshot="fr-CA-other-1.png",
    )
    report = _parse(_answer(findings=[finding]))
    assert (report.findings[0].screen, report.findings[0].flow) == ("other", "5.1")
    assert report.findings[0].path == "#/test-studio/sets/set-1?tab=documents"


@pytest.mark.unit
def test_every_text_field_is_redacted_then_flattened_and_cut() -> None:
    shown = "SECRET\nline\u202e two " + "é" * 400
    report = _parse(_answer(findings=[_finding(shown=shown, expected=None)]))
    (finding,) = report.findings
    assert finding.shown.startswith("[redacted] line two éé")
    assert len(finding.shown) == translation_prompt.FIELD_CHARS
    assert finding.shown.isprintable()
    assert finding.expected == ""


@pytest.mark.unit
def test_entries_past_their_bounds_are_counted_as_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(translation_prompt, "MAX_FINDINGS", 2)
    monkeypatch.setattr(translation_prompt, "MAX_SCREEN_ENTRIES", 1)
    report = _parse(_answer(findings=[_finding()] * 5))
    assert (len(report.findings), len(report.visits), report.dropped) == (2, 1, 4)
    assert report.refused == (
        translation_prompt.Refused("screens", 1, "1 item(s) past the bound of 1", None),
        translation_prompt.Refused(
            "findings", 2, "3 item(s) past the bound of 2", None
        ),
    )


# The page -------------------------------------------------------------------


@pytest.mark.unit
def test_an_unreadable_report_says_why_on_the_page() -> None:
    page = translation_prompt.report_markdown(_parse("no block"))
    assert page == (
        "### fr-CA\n\nThe report could not be read: the final message holds no "
        "fenced json block.\n"
    )


@pytest.mark.unit
def test_the_page_counts_tabulates_links_and_lists_what_was_missed() -> None:
    findings = [
        _finding(shown="Upload | now <b>@team</b> a\\|b"),
        _finding(
            kind="clipped",
            screen="documents",
            flow="5.1",
            screenshot="fr-CA-documents.png",
        ),
    ]
    screens = [
        {"id": "upload", "reached": True, "screenshot": "fr-CA-upload.png"},
        {"id": "users", "reached": False},
        {"id": "pricing", "reached": True},
        "junk",
    ]
    page = translation_prompt.report_markdown(
        _parse(_answer(findings=findings, screens=screens)), screens_href="screens"
    )
    assert "2 finding(s) (clipped 1, english_left 1); 1 screen(s) reached" in page
    assert "1 screen(s) claimed as reached without a screenshot" in page
    assert (
        "1 item(s) of the report broke its rules; `refused` in `findings.json` "
        "lists each with the rule it broke." in page
    )
    assert "These are the model's claims" in page
    assert "[fr-CA-upload.png](screens/fr-CA-upload.png)" in page
    assert "documents #/documents/upload (flow 5.1)" in page
    row = next(line for line in page.splitlines() if "Upload" in line)
    assert "Upload &#124; now" in row
    assert row.count("|") == 8, "a cell's pipe split the row"
    assert "<b>" not in row
    assert page.rstrip().endswith("Not reached: pricing, users.")


@pytest.mark.unit
def test_the_summary_is_plain_data_for_the_findings_file() -> None:
    summary = translation_prompt.summary(_parse(_answer()))
    assert json.loads(json.dumps(summary)) == summary
    assert summary["by_kind"] == {"english_left": 1}
    assert (summary["screens_reached"], summary["screens_listed"]) == (1, 2)
    assert summary["findings"][0]["screenshot"] == "fr-CA-upload.png"
    assert summary["refused"] == []
    refused = translation_prompt.summary(
        _parse(_answer(findings=[_finding(flow="9.9")]))
    )["refused"]
    assert json.loads(json.dumps(refused)) == refused
    assert refused[0]["reason"] == _FLOW
    assert refused[0]["item"]["flow"] == "9.9"
