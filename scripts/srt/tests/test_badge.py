"""Tests for the SRT status badge: run.py's verdict file and badge.py's rendering.

The property that matters is that the badge never shows a pass the gate did not
reach: every outcome other than a clean pass, and every missing or unreadable
verdict, must render as something other than green.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

SRT_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SRT_DIR.parent.parent
sys.path.insert(0, str(SRT_DIR))

import badge  # noqa: E402
import run as srt_run  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("args", "outcome"),
    [
        ((0, False, False), "pass"),
        ((3, False, False), "open_high"),
        ((3, True, True), "open_high"),
        ((0, True, False), "incomplete"),
        ((0, True, True), "incomplete"),
        ((0, False, True), "stale_register"),
    ],
)
def test_write_verdict_records_what_the_gate_concluded(tmp_path, args, outcome):
    verdict = srt_run.write_verdict(tmp_path, *args)
    on_disk = json.loads((tmp_path / "verdict.json").read_text(encoding="utf-8"))
    assert verdict == on_disk
    assert on_disk["outcome"] == outcome
    assert on_disk["open_high"] == args[0]


def test_the_verdict_outcome_agrees_with_the_exit_decision():
    """run.py gates on open HIGH first, then incomplete scanners, then a stale
    register; the verdict must rank them the same way or the badge and the job
    status disagree."""
    source = (SRT_DIR / "run.py").read_text(encoding="utf-8")
    main = source[source.index("def main(") :]
    positions = [
        main.index("if gating_issues:\n        if is_ci:"),
        main.index("if scanners_incomplete:"),
        main.index("if register_is_stale:"),
    ]
    assert positions == sorted(positions)
    assert main.index("write_verdict(") < positions[0]


@pytest.mark.parametrize(
    ("verdict", "message", "colour"),
    [
        ({"outcome": "pass", "open_high": 0}, "0 open high", badge.GREEN),
        ({"outcome": "open_high", "open_high": 2}, "2 open high", badge.RED),
        ({"outcome": "incomplete"}, "scan incomplete", badge.ORANGE),
        ({"outcome": "stale_register"}, "stale suppressions", badge.ORANGE),
        ({"outcome": "something-new"}, "no result", badge.GREY),
        ({}, "no result", badge.GREY),
        (None, "no result", badge.GREY),
    ],
)
def test_message_and_colour(verdict, message, colour):
    assert badge.message_and_colour(verdict) == (message, colour)


@pytest.mark.parametrize("content", [None, "", "not json", "[1, 2]"])
def test_a_missing_or_unreadable_verdict_is_no_result(tmp_path, content):
    path = tmp_path / "verdict.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    out = tmp_path / "badge.svg"
    assert badge.main(["badge.py", str(path), str(out)]) == 0
    svg = out.read_text(encoding="utf-8")
    assert "no result" in svg
    assert badge.GREEN not in svg


def test_render_is_well_formed_svg_and_escapes_text():
    from xml.etree import ElementTree

    svg = badge.render("a<b>&c", badge.RED, label="x&y")
    root = ElementTree.fromstring(svg)
    assert root.tag.endswith("svg")
    texts = [el.text for el in root.iter() if el.tag.endswith("text")]
    assert texts == ["x&y", "a<b>&c"]


def test_the_pages_job_publishes_the_badge_the_srt_job_renders():
    ci = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    srt = ci["srt_security_review"]
    assert "srt-badge.svg" in srt["artifacts"]["paths"]
    assert srt["artifacts"]["when"] == "always"
    assert any("scripts/srt/badge.py" in line for line in srt["after_script"])

    pages = ci["pages"]
    assert pages["needs"][0]["job"] == "srt_security_review"
    assert pages["needs"][0]["artifacts"] is True
    # Without `when: always` the job is skipped whenever the gate fails, and the
    # site keeps the last green badge.
    assert all(rule.get("when") == "always" for rule in pages["rules"])
    assert all("CI_DEFAULT_BRANCH" in rule["if"] for rule in pages["rules"])
    assert pages["artifacts"]["paths"] == ["public"]
