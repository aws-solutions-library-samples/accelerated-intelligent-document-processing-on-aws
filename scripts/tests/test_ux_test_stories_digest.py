# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/story_digest.py, the nightly digest's pure rules.

The regression rules are a truth table over synthetic histories: the baseline, the
3-of-4 completion window, the last-night permission rule, what makes a night or an
observation comparable, a series reset, and a fix reported once. The stack state is
closed over ``codebuild_deployment._UX_NIGHTLY_ROW_TEXT``: ``ROW_TEMPLATES`` must be
that table, and every row ``build_consolidated_summary`` renders must classify back
to its own state. That module is imported inside the closure tests only.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import story_digest, story_verdict  # noqa: E402
from ux_runner.story_digest import History, Night, NightFlow  # noqa: E402

KEY = "route-folder-to-profile@platform-admin"
OTHER_KEY = "find-extraction-model@team-lead"
SERIES = "0123456789ab"
OTHER_SERIES = "ba9876543210"
DEVELOP = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
MODEL = "us.anthropic.claude-opus-5-5"
TONIGHT = 1000
SUB_KINDS = {
    "not_run": "budget",
    "error": "journal",
    "gave_up": "max_turns",
    "stuck": "no_route",
}
GROUPS = {"platform-admin": "Admin", "team-lead": "Viewer", "new-analyst": "Author"}


def _codebuild_deployment() -> ModuleType:
    sdlc = str(SCRIPTS / "sdlc")
    if sdlc not in sys.path:
        sys.path.insert(0, sdlc)
    import codebuild_deployment

    return codebuild_deployment


def _flow(
    verdict: str = "done",
    *,
    key: str = KEY,
    state: str | None = None,
    series: str = SERIES,
    regressed: bool = False,
) -> NightFlow:
    if state is None:
        state = "unconfirmed" if verdict in story_verdict.BAD_VERDICTS else "single"
    return NightFlow(
        key=key, series=series, verdict=verdict, state=state, regressed=regressed
    )


def _night(
    pipeline_id: int,
    *flows: NightFlow,
    comparable: bool = True,
    audits_clean: bool = True,
    stack_state: str = "updated",
) -> Night:
    return Night(
        pipeline_id=pipeline_id,
        develop_sha=DEVELOP,
        stack_state=stack_state,
        comparable=comparable,
        audits_clean=audits_clean,
        flows=tuple(flows),
    )


def _history(*verdicts: str | NightFlow | None, start: int = TONIGHT - 1) -> History:
    """Prior nights, newest first, one flow each; None is a night the flow was absent."""
    nights = []
    for offset, item in enumerate(verdicts):
        pipeline_id = start - offset
        if item is None:
            nights.append(_night(pipeline_id))
        elif isinstance(item, NightFlow):
            nights.append(_night(pipeline_id, item))
        else:
            nights.append(_night(pipeline_id, _flow(item)))
    return History(nights=tuple(nights), gaps=())


def _tonight(verdict: str, state: str = "confirmed", **night: Any) -> Night:
    return _night(TONIGHT, _flow(verdict, state=state), **night)


@pytest.mark.unit
def test_row_templates_are_exactly_the_nightly_row_table() -> None:
    module = _codebuild_deployment()
    table = {
        key: template for key, (_, template) in module._UX_NIGHTLY_ROW_TEXT.items()
    }
    table[""] = module._UX_NIGHTLY_UNREPORTED_ROW[1]
    ours = {key: template for key, _, template in story_digest.ROW_TEMPLATES}
    assert len(ours) == len(story_digest.ROW_TEMPLATES)
    assert ours == table
    states = [state for _, state, _ in story_digest.ROW_TEMPLATES]
    assert len(set(states)) == len(states)
    assert story_digest.STACK_STATES == (*states, "unrecognised", "missing")
    assert story_digest.COMPARABLE_STACK_STATES == {"updated", "up_to_date"}


def _outcome(key: str, **slots: Any) -> dict[str, Any]:
    if key.startswith("skipped/"):
        outcome: dict[str, Any] = {"state": "skipped", "reason": key.split("/", 1)[1]}
    else:
        outcome = {"state": key}
    outcome.update(slots)
    return outcome


SLOT_VALUES = (
    {"status": "UPDATE_COMPLETE", "minutes": 12.34, "phase": "stack"},
    {"status": "UPDATE_ROLLBACK_COMPLETE", "minutes": -0.04, "phase": "iam"},
    {"status": "lower case", "minutes": True, "phase": "not-a-phase"},
    {"minutes": 999999.9},
)


@pytest.mark.unit
@pytest.mark.parametrize("slots", SLOT_VALUES)
def test_every_rendered_row_classifies_back_to_its_state(slots: dict[str, Any]) -> None:
    module = _codebuild_deployment()
    for key, state, _ in story_digest.ROW_TEMPLATES:
        outcome = _outcome(key, **slots) if key else {"state": "something else"}
        summary = module.build_consolidated_summary(
            "idp-ci-x", {"success": True}, [], True, ux_nightly_update=outcome
        )
        assert story_digest.classify_stack_state(summary) == state, (key, summary)


@pytest.mark.unit
def test_a_summary_without_the_stack_section_is_missing() -> None:
    module = _codebuild_deployment()
    summary = module.build_consolidated_summary("idp-ci-x", {"success": True}, [], True)
    assert story_digest.STACK_HEADER_PREFIX not in summary
    assert story_digest.classify_stack_state(summary) == "missing"
    assert story_digest.classify_stack_state(None) == "missing"
    assert story_digest.classify_stack_state("") == "missing"


HEADER = (
    "🌙 UX review stack (idp-ux-nightly, updated in place — never part of the verdict)"
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "state"),
    [
        (
            f"{HEADER}\n  ✅ Already on tonight's template — nothing to update\n",
            "up_to_date",
        ),
        (
            f"\t{HEADER}\r\n\r\n\t  ✅ Already on tonight's template — nothing to update\r\n",
            "up_to_date",
        ),
        (
            f"{HEADER}\n  ⚠️ Still updating when the run ended — 4.0 min so far; CloudFormation carries on",
            "running",
        ),
        (f"{HEADER}\n\n\n", "unrecognised"),
        (
            f"{HEADER}\n  🟢 Already on tonight's template — nothing to update\n",
            "unrecognised",
        ),
        (
            f"{HEADER}\n  ✅ Already on tonight's template — nothing to update — x\n",
            "unrecognised",
        ),
        (
            f"{HEADER}\n  ✅ Updated in place to tonight's template — update_complete in 1.0 min\n",
            "unrecognised",
        ),
        (
            f"{HEADER}\n  ✅ Updated in place to tonight's template — UPDATE_COMPLETE in 12 min\n",
            "unrecognised",
        ),
        (
            f"{HEADER}\n  ⚠️ Update did not complete — FAILED after 1.0 min (Stack)\n",
            "unrecognised",
        ),
        (
            f"{HEADER}\n  ✅ Updated in place to tonight's template — UPDATE_COMPLETE in 1.0 min\n{HEADER}\n  ⚪ Not reported\n",
            "updated",
        ),
        (
            "🌙 UX review stack\n  ✅ Already on tonight's template — nothing to update\n",
            "missing",
        ),
    ],
)
def test_classify_reads_only_the_first_row_under_the_header(
    text: str, state: str
) -> None:
    assert story_digest.classify_stack_state(text) == state


def _digest_of(night: Night, **overrides: Any) -> dict[str, Any]:
    document = story_digest.digest_document(
        night, History((), ()), [], [], spend_usd=3.5
    )
    document.update(overrides)
    return document


@pytest.mark.unit
def test_a_digest_round_trips_through_the_history() -> None:
    night = _night(
        900,
        _flow("stuck", state="confirmed", regressed=False),
        _flow("done", key=OTHER_KEY, series=OTHER_SERIES),
    )
    document = story_digest.digest_document(
        night,
        History((), (880,)),
        [story_digest.Regression(KEY, SERIES, "completion", "stuck", 3, 4)],
        [OTHER_KEY],
        spend_usd=18.4,
    )
    assert list(document) == [
        "schema",
        "version",
        "judgement_revision",
        "pipeline_id",
        "develop_sha",
        "stack_state",
        "stack_changed_mid_run",
        "comparable",
        "audits_clean",
        "spend_usd",
        "flows",
        "regressions",
        "fixed",
        "gaps",
        "injections",
        "shards_ran",
        "reruns_not_run",
    ]
    assert document["flows"] == [
        {
            "key": OTHER_KEY,
            "series": OTHER_SERIES,
            "verdict": "done",
            "state": "single",
            "regressed": False,
        },
        {
            "key": KEY,
            "series": SERIES,
            "verdict": "stuck",
            "state": "confirmed",
            "regressed": True,
        },
    ]
    assert document["regressions"] == [
        {
            "key": KEY,
            "series": SERIES,
            "kind": "completion",
            "verdict": "stuck",
            "good": 3,
            "counted": 4,
        }
    ]
    assert (document["fixed"], document["gaps"]) == ([OTHER_KEY], [880])
    parsed = json.loads(json.dumps(document))
    history = History.from_digests([(900, parsed)])
    assert history.gaps == ()
    (read,) = history.nights
    assert read.flow(KEY) == _flow("stuck", state="confirmed", regressed=True)
    assert read.flow(OTHER_KEY) == _flow("done", key=OTHER_KEY, series=OTHER_SERIES)
    assert read.flow("absent@nobody") is None
    assert (read.pipeline_id, read.develop_sha, read.comparable) == (900, DEVELOP, True)


@pytest.mark.unit
def test_a_digest_records_a_stack_change_as_a_comparable_state_on_an_incomparable_night() -> (
    None
):
    changed = _digest_of(_night(5, comparable=False, stack_state="updated"))
    assert changed["stack_changed_mid_run"] is True
    failed = _digest_of(_night(5, comparable=False, stack_state="failed"))
    assert failed["stack_changed_mid_run"] is False


@pytest.mark.unit
def test_history_is_newest_first_and_bounded_and_gaps_do_not_end_it() -> None:
    digests: list[tuple[int, Mapping[str, Any] | None]] = [
        (pipeline_id, _digest_of(_night(pipeline_id, _flow())))
        for pipeline_id in range(100, 120)
    ]
    digests[15] = (115, None)
    digests[18] = (118, _digest_of(_night(118), schema="something/else"))
    history = History.from_digests(list(reversed(digests)) + [(119, None)])
    assert [night.pipeline_id for night in history.nights] == [
        119,
        117,
        116,
        114,
        113,
        112,
        111,
        110,
        109,
        108,
        107,
        106,
        105,
        104,
    ]
    assert len(history.nights) == story_digest.HISTORY_NIGHTS
    assert history.gaps == (118, 115)


INVALID = (
    {"schema": "ux-test-stories/other"},
    {"version": 1},
    {"version": True},
    {"shards_ran": 0},
    {"injections": -1},
    {"reruns_not_run": "2"},
    {"pipeline_id": 77},
    {"develop_sha": DEVELOP.upper()},
    {"stack_state": "mostly fine"},
    {"comparable": "yes"},
    {"stack_state": "failed"},
    {"stack_changed_mid_run": True},
    {"spend_usd": -1},
    {"spend_usd": float("nan")},
    {"flows": "none"},
    {"flows": [{"key": KEY, "series": SERIES, "verdict": "done", "state": "single"}]},
    {
        "flows": [
            {
                "key": "Not A Key",
                "series": SERIES,
                "verdict": "done",
                "state": "single",
                "regressed": False,
            }
        ]
    },
    {
        "flows": [
            {
                "key": KEY,
                "series": SERIES,
                "verdict": "fine",
                "state": "single",
                "regressed": False,
            }
        ]
    },
    {
        "flows": [
            {
                "key": KEY,
                "series": SERIES,
                "verdict": "done",
                "state": "single",
                "regressed": False,
            }
        ]
        * 2
    },
    {"regressions": [{"key": KEY}]},
    {"fixed": [7]},
    {"gaps": ["7"]},
    {"extra": 1},
    {
        "flows": [
            {
                "key": KEY,
                "series": SERIES,
                "verdict": "done",
                "state": "single",
                "regressed": False,
                "note": "x" * story_digest.DIGEST_MAX_BYTES,
            }
        ]
    },
)


@pytest.mark.unit
@pytest.mark.parametrize("overrides", INVALID)
def test_an_invalid_digest_is_a_gap(overrides: dict[str, Any]) -> None:
    document = _digest_of(_night(42, _flow()), **overrides)
    history = History.from_digests([(43, _digest_of(_night(43))), (42, document)])
    assert [night.pipeline_id for night in history.nights] == [43]
    assert history.gaps == (42,)


@pytest.mark.unit
def test_a_digest_past_the_byte_bound_is_a_gap_and_read_digest_bounds_bytes() -> None:
    big = _digest_of(_night(42), develop_sha=DEVELOP)
    big["flows"] = [
        {
            "key": f"s{n}@p",
            "series": SERIES,
            "verdict": "done",
            "state": "single",
            "regressed": False,
        }
        for n in range(story_digest.DIGEST_MAX_BYTES // 60)
    ]
    assert History.from_digests([(42, big)]).gaps == (42,)
    raw = json.dumps(_digest_of(_night(42)))
    assert story_digest.read_digest(raw) == json.loads(raw)
    assert story_digest.read_digest(raw.encode("utf-8")) == json.loads(raw)
    assert story_digest.read_digest(b"[1, 2]") is None
    assert story_digest.read_digest(b"{not json") is None
    assert story_digest.read_digest(b"\xff\xfe") is None
    assert story_digest.read_digest(b" " * (story_digest.DIGEST_MAX_BYTES + 1)) is None


@pytest.mark.unit
def test_pipeline_ids_that_are_not_integers_are_left_out_and_duplicates_read_once() -> (
    None
):
    digests: list[tuple[Any, Any]] = [
        (True, _digest_of(_night(1))),
        ("7", _digest_of(_night(7))),
        (8, _digest_of(_night(8))),
        (8, None),
    ]
    history = History.from_digests(digests)
    assert [night.pipeline_id for night in history.nights] == [8]
    assert history.gaps == ()


@pytest.mark.unit
def test_nothing_is_flagged_before_four_baseline_nights() -> None:
    assert (
        story_digest.regressions(_tonight("stuck"), _history("done", "done", "done"))
        == []
    )
    (found,) = story_digest.regressions(
        _tonight("stuck"), _history("done", "done", "done", "done")
    )
    assert found == story_digest.Regression(KEY, SERIES, "completion", "stuck", 4, 4)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("prior", "good"),
    [
        (("done", "done", "done", "stuck"), 3),
        (("refused_as_expected", "done", "gave_up", "done"), 3),
        (("done", "done", "stuck", "stuck", "done", "done"), None),
        (("stuck", "done", "done", "done", "stuck"), 3),
        (("done", "unclear", "out_of_steps", "done"), None),
    ],
)
def test_a_completion_regression_needs_three_good_of_the_last_four(
    prior: tuple[str, ...], good: int | None
) -> None:
    for verdict in ("stuck", "offered"):
        found = story_digest.regressions(_tonight(verdict), _history(*prior))
        if good is None:
            assert found == []
        else:
            assert found == [
                story_digest.Regression(KEY, SERIES, "completion", verdict, good, 4)
            ]


@pytest.mark.unit
def test_a_permission_regression_needs_the_last_comparable_night_good() -> None:
    (found,) = story_digest.regressions(
        _tonight("not_refused"), _history("done", "stuck", "stuck", "stuck")
    )
    assert found == story_digest.Regression(
        KEY, SERIES, "permission", "not_refused", 1, 1
    )
    assert (
        story_digest.regressions(
            _tonight("not_refused"), _history("not_refused", "done", "done", "done")
        )
        == []
    )
    assert (
        story_digest.regressions(
            _tonight("not_refused"), _history("done", "done", "done")
        )
        == []
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("verdict", "state"),
    [
        ("stuck", "unconfirmed"),
        ("stuck", "mixed"),
        ("not_refused", "mixed"),
        ("offered", "single"),
        ("out_of_steps", "confirmed"),
        ("gave_up", "confirmed"),
        ("unclear", "confirmed"),
        ("not_run", "confirmed"),
        ("error", "confirmed"),
        ("done", "confirmed"),
        ("refused_as_expected", "confirmed"),
    ],
)
def test_only_a_confirmed_bad_verdict_is_flagged_when_every_rerun_ran(
    verdict: str, state: str
) -> None:
    history = _history("done", "done", "done", "done", "done")
    assert story_digest.regressions(_tonight(verdict, state), history) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("verdict", "kind", "good", "counted"),
    [
        ("stuck", "completion", 4, 4),
        ("offered", "completion", 4, 4),
        ("not_refused", "permission", 1, 1),
    ],
)
def test_a_bad_result_whose_confirming_rerun_did_not_fit_is_an_unconfirmed_regression(
    verdict: str, kind: str, good: int, counted: int
) -> None:
    """The re-run that would have confirmed it was planned and did not fit in the
    night's budget or time, so the result is reported, as unconfirmed, rather than
    passed over as though nothing had been seen."""
    tonight = _tonight(verdict, "unconfirmed")
    history = _history("done", "done", "done", "done")
    assert story_digest.regressions(tonight, history, rerun_did_not_fit={KEY}) == [
        story_digest.Regression(
            KEY, SERIES, kind, verdict, good, counted, confirmed=False
        )
    ]
    assert story_digest.regressions(tonight, history) == []
    assert (
        story_digest.regressions(
            tonight, history, rerun_did_not_fit={"other-story@platform-admin"}
        )
        == []
    )
    mixed = _tonight(verdict, "mixed")
    assert story_digest.regressions(mixed, history, rerun_did_not_fit={KEY}) == []


@pytest.mark.unit
def test_an_unconfirmed_regression_says_so_in_the_digest_and_on_slack() -> None:
    results = _results(_result(KEY, verdict="stuck"))
    run = {**_run(_context(KEY)), "reruns_not_run": [{"key": KEY, "reason": "time"}]}
    night = dataclasses.replace(
        story_digest.night_from_results(
            results, run, pipeline_id=TONIGHT, develop_sha=DEVELOP
        ),
        comparable=True,
        stack_state="updated",
    )
    (flow,) = night.flows
    assert flow.state == "unconfirmed"
    history = _history("done", "done", "done", "done")
    found = story_digest.regressions(night, history, rerun_did_not_fit={KEY})
    digest = story_digest.digest_document(
        night, history, found, [], spend_usd=4.0, reruns_not_run=1
    )
    assert digest["flows"][0]["regressed"] is True
    assert digest["regressions"][0]["key"] == KEY
    text = story_digest.compose_digest_md(digest, results, run, uncovered=[])
    assert (
        f"- `{KEY}` — stuck, unconfirmed tonight, as its confirming re-run did not "
        "fit; good on 4 of the last 4 comparable nights (completion)"
    ) in _section_of(text, "## Regressions")
    payload = story_digest.slack_payload(digest, job_url="")
    assert payload["status"] == "regressions"
    assert (
        f"Regressions: {KEY} (completion, stuck, unconfirmed, good 4 of 4)."
        in payload["summary"]
    )
    assert "Confirming re-runs not run: 1." in payload["summary"]
    confirmed = {**digest, "flows": [{**digest["flows"][0], "state": "confirmed"}]}
    assert "confirmed tonight;" in story_digest.compose_digest_md(
        confirmed, results, run, uncovered=[]
    )
    assert (
        "(completion, stuck, good 4 of 4)"
        in (story_digest.slack_payload(confirmed, job_url="")["summary"])
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "night",
    [
        {"comparable": False},
        {"audits_clean": False},
        {"comparable": False, "stack_state": "failed"},
    ],
)
def test_nothing_is_flagged_on_a_night_that_is_not_comparable(
    night: dict[str, Any],
) -> None:
    history = _history("done", "done", "done", "done")
    assert story_digest.regressions(_tonight("stuck", **night), history) == []
    assert story_digest.regressions(_tonight("not_refused", **night), history) == []


@pytest.mark.unit
def test_prior_nights_count_only_when_comparable_clean_in_series_and_observed() -> None:
    four_good = ("done", "done", "done", "done")
    uncounted = [
        _night(TONIGHT - 1, _flow("done"), comparable=False),
        _night(TONIGHT - 1, _flow("done"), audits_clean=False),
        _night(TONIGHT - 1, _flow("done", series=OTHER_SERIES)),
        _night(TONIGHT - 1, _flow("error")),
        _night(TONIGHT - 1, _flow("not_run")),
        _night(TONIGHT - 1),
        _night(TONIGHT, _flow("done")),
        _night(TONIGHT + 5, _flow("done")),
    ]
    three_good = _history(*four_good[:3], start=TONIGHT - 2)
    for night in uncounted:
        history = History(nights=(night, *three_good.nights), gaps=())
        assert story_digest.regressions(_tonight("stuck"), history) == [], night
    assert story_digest.regressions(
        _tonight("stuck"), _history(*four_good, start=TONIGHT - 2)
    )


@pytest.mark.unit
def test_a_not_run_night_does_not_hide_a_permission_hole() -> None:
    history = _history("not_run", "done", "done", "done", "done")
    (found,) = story_digest.regressions(_tonight("not_refused"), history)
    assert found.kind == "permission"


@pytest.mark.unit
def test_a_new_series_starts_its_baseline_again() -> None:
    history = _history("done", "done", "done", "done", "done")
    tonight = _night(TONIGHT, _flow("stuck", state="confirmed", series=OTHER_SERIES))
    assert story_digest.regressions(tonight, history) == []


@pytest.mark.unit
def test_history_order_is_by_pipeline_not_by_position() -> None:
    history = _history("stuck", "stuck", "done", "done", "done", "done")
    shuffled = History(nights=tuple(reversed(history.nights)), gaps=())
    assert story_digest.regressions(_tonight("stuck"), shuffled) == []


def _regressed(verdict: str = "stuck") -> NightFlow:
    return _flow(verdict, state="confirmed", regressed=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("prior", "is_fixed"),
    [
        ((_regressed(), "done"), False),
        (("done", _regressed()), True),
        (("done", "done", _regressed()), False),
        (("done", "stuck", _regressed()), True),
        (("done", _regressed(), "done", "done", _regressed()), True),
        (("done", "done", "done"), False),
        (("done", None, _regressed()), True),
        (("done", "not_run", _regressed()), True),
    ],
)
def test_a_fix_is_two_good_comparable_nights_after_a_regression_reported_once(
    prior: tuple[str | NightFlow | None, ...], is_fixed: bool
) -> None:
    tonight = _night(TONIGHT, _flow("done"))
    assert story_digest.fixed(tonight, _history(*prior)) == ([KEY] if is_fixed else [])


@pytest.mark.unit
def test_a_fix_needs_tonight_comparable_good_and_in_the_regressions_series() -> None:
    history = _history("done", _regressed())
    assert story_digest.fixed(_night(TONIGHT, _flow("stuck")), history) == []
    assert (
        story_digest.fixed(_night(TONIGHT, _flow("done"), comparable=False), history)
        == []
    )
    other = _night(TONIGHT, _flow("done", series=OTHER_SERIES))
    assert story_digest.fixed(other, history) == []


def _result(
    key: str = KEY,
    *,
    attempt: int = 1,
    verdict: str = "done",
    series: str = SERIES,
    audit: str = "clean",
    flags: Sequence[str] = (),
    help_links: Sequence[str] = (),
    confusion: Sequence[str] = (),
    dead_end: Sequence[str] = (),
    refusals: Mapping[str, int] | None = None,
    actions_used: int | None = None,
    budget: int | None = None,
    overrun: float | None = None,
    sub_kind: str | None = None,
    commit_points: Sequence[str] = (),
    injections: int = 0,
) -> story_verdict.FlowResult:
    story, persona = key.split("@")
    expectation = "not_offered" if verdict in ("not_refused", "offered") else "allowed"
    return story_verdict.FlowResult(
        key=key,
        story=story,
        persona=persona,
        group=GROUPS[persona],
        title="Make one input folder use one configuration",
        expectation=expectation,
        kind="read",
        series=series,
        attempt=attempt,
        budget_scale=1,
        verdict=verdict,
        sub_kind=sub_kind or SUB_KINDS.get(verdict),
        flags=tuple(flags),
        met_expectation=story_verdict.VERDICT_MEETS[verdict],
        findings=(),
        documented_steps=5,
        actions_used=actions_used,
        budget=budget,
        overrun=overrun,
        budget_exhausted=None,
        looks=None,
        searches=0,
        reads=0,
        notes=story_verdict.Notes(
            confusion=tuple(story_verdict.Note(what=w, why="w") for w in confusion),
            dead_end=tuple(story_verdict.Note(what=w, why="w") for w in dead_end),
            injections=injections,
            injection_places={place: 0 for place in story_verdict.INJECTION_PLACES},
        ),
        help_links_unresolved=tuple(help_links),
        doc_addresses_guessed=0,
        commit_points=tuple(commit_points),
        declared_docs_found=False,
        terminal=None,
        claude=None,
        audit=audit,
        refusals_by_origin=dict(refusals or {}),
        video=None,
        chapters=(),
    )


def _results(*attempts: story_verdict.FlowResult) -> dict[str, Any]:
    found = story_verdict.findings(attempts)
    return story_verdict.results_document(
        story_verdict.attach_findings(attempts, found), found
    )


def _context(
    key: str = KEY, *, attempt: int = 1, needs: Sequence[str] = ()
) -> story_verdict.FlowContext:
    story, persona = key.split("@")
    return story_verdict.FlowContext(
        key=key,
        story=story,
        persona=persona,
        group=GROUPS[persona],
        title="Make one input folder use one configuration",
        expectation="allowed",
        kind="read",
        documented_steps=5,
        needs=tuple(needs),
        declared_docs=(),
        series=SERIES,
        budget=50,
        attempt=attempt,
    )


def _run(
    *contexts: story_verdict.FlowContext,
    spend: float = 1.5,
    build: str | None = "2026-10-19",
    stack_state: str | None = "updated",
    mid_run: bool = False,
    scope: dict[str, Any] | None = None,
    index: int = 1,
) -> dict[str, Any]:
    return {
        "schema": story_verdict.RUN_SCHEMA,
        "version": story_verdict.RUN_VERSION,
        "mode": "nightly",
        "shard": {"index": index, "total": 2},
        "harness_sha": "f" * 40,
        "harness_dirty": False,
        "catalogue_sha256": "a" * 64,
        "prompt_revision": 1,
        "judgement_revision": story_verdict.JUDGEMENT_REVISION,
        "relay_revision": 1,
        "model": MODEL,
        "effort": "high",
        "region": "us-east-1",
        "versions": {
            "claude_code": "2.1.281",
            "chrome_devtools_mcp": "1.10.1",
            "chrome": "141.0.7390.54",
        },
        "stack": {"name": "idp-ux-nightly", "version": "0.6.12.dev1", "build": build},
        "stack_state": stack_state,
        "stack_changed_mid_run": mid_run,
        "ui_source": {"kind": "deployed"},
        "docs": {
            "ref": DEVELOP,
            "corpus_hash": "9e8d7c6b5a41",
            "source": "git",
            "dirty": None,
        },
        "limits": {"step_budget_multiple": 10, "step_budget_floor": 30},
        "flows": [context.to_json() for context in contexts],
        "credential_scope": scope or {"claude": "scoped", "render": "scoped"},
        "spend_usd": spend,
    }


@pytest.mark.unit
def test_tonight_is_one_flow_per_key_by_its_confirmation() -> None:
    results = _results(
        _result(verdict="stuck"),
        _result(attempt=2, verdict="stuck"),
        _result(OTHER_KEY, series=OTHER_SERIES),
        _result(
            "find-extraction-model@new-analyst", verdict="not_run", audit="not_run"
        ),
    )
    night = story_digest.night_from_results(
        results, _run(), pipeline_id=TONIGHT, develop_sha=DEVELOP
    )
    assert night == Night(
        pipeline_id=TONIGHT,
        develop_sha=DEVELOP,
        stack_state="updated",
        comparable=True,
        audits_clean=True,
        flows=(
            NightFlow(
                "find-extraction-model@new-analyst", SERIES, "not_run", "single", False
            ),
            NightFlow(OTHER_KEY, OTHER_SERIES, "done", "single", False),
            NightFlow(KEY, SERIES, "stuck", "confirmed", False),
        ),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("run", "audit", "comparable", "clean", "state"),
    [
        ({"stack_state": "up_to_date"}, "clean", True, True, "up_to_date"),
        ({"stack_state": "failed"}, "clean", False, True, "failed"),
        ({"stack_state": None}, "clean", False, True, "missing"),
        ({"stack_state": "half way"}, "clean", False, True, "unrecognised"),
        ({"mid_run": True}, "clean", False, True, "updated"),
        ({}, "unavailable", True, False, "updated"),
        ({}, "violations", True, False, "updated"),
    ],
)
def test_tonight_is_comparable_only_on_an_unchanged_comparable_stack(
    run: dict[str, Any], audit: str, comparable: bool, clean: bool, state: str
) -> None:
    results = _results(
        _result(audit=audit, verdict="error" if audit == "violations" else "done")
    )
    night = story_digest.night_from_results(
        results, _run(**run), pipeline_id=TONIGHT, develop_sha=DEVELOP
    )
    assert (night.comparable, night.audits_clean, night.stack_state) == (
        comparable,
        clean,
        state,
    )


@pytest.mark.unit
def test_tonight_refuses_a_bad_sha_pipeline_or_results_shape() -> None:
    results = _results(_result())
    with pytest.raises(ValueError):
        story_digest.night_from_results(
            results, _run(), pipeline_id=1, develop_sha="abc"
        )
    with pytest.raises(ValueError):
        story_digest.night_from_results(
            results, _run(), pipeline_id=True, develop_sha=DEVELOP
        )
    with pytest.raises(story_verdict.RunRecordError):
        story_digest.night_from_results(
            {**results, "schema": "x"}, _run(), pipeline_id=1, develop_sha=DEVELOP
        )


@pytest.mark.unit
def test_merged_results_recompute_findings_across_shards() -> None:
    first = _results(
        _result(KEY, verdict="stuck", help_links=["/missing/"]),
        _result(KEY, attempt=2, verdict="stuck"),
    )
    second = _results(_result(OTHER_KEY, help_links=["/missing/"]))
    merged = story_digest.merge_results([second, first])
    assert [(f["key"], f["attempt"]) for f in merged["flows"]] == [
        (OTHER_KEY, 1),
        (KEY, 1),
        (KEY, 2),
    ]
    docs = [f for f in merged["findings"] if f["category"] == "docs"]
    assert len(docs) == 1
    assert docs[0]["scope"] == sorted([KEY, OTHER_KEY])
    assert merged["summary"]["flows"] == 2
    assert merged["summary"]["attempts"] == 3
    assert merged["summary"]["verdicts"] == {"stuck": 1, "done": 1}
    flow_ids = {f["key"]: f["findings"] for f in merged["flows"]}
    assert docs[0]["id"] in flow_ids[OTHER_KEY]
    assert merged == story_verdict.results_document(
        [story_verdict.FlowResult.from_json(f) for f in merged["flows"]],
        [story_verdict.Finding.from_json(f) for f in merged["findings"]],
    )


@pytest.mark.unit
def test_merging_results_refuses_overlap_mixed_revisions_and_bad_shapes() -> None:
    part = _results(_result())
    for parts in (
        [],
        [part, part],
        [part, {**part, "schema": "ux-test-stories/other"}],
        [{**part, "judgement_revision": story_verdict.JUDGEMENT_REVISION + 1}],
        [{**part, "judgement_revision": True}],
        [{**part, "version": True}],
        [{**part, "flows": [{"key": KEY}]}],
    ):
        with pytest.raises(story_verdict.RunRecordError):
            story_digest.merge_results(parts)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("renders", "merged_render"),
    [
        (("scoped", "ambient"), "ambient"),
        (("unavailable", "ambient"), "ambient"),
        (("scoped", "unavailable"), "unavailable"),
        (("scoped", "scoped"), "scoped"),
    ],
)
def test_a_merged_runs_render_scope_is_its_weakest_with_own_credentials_weakest(
    renders: tuple[str, str], merged_render: str
) -> None:
    """Narrating with the run's own credentials is the broadest render scope, so a
    shard that did makes the merged run's render scope ``ambient``."""
    first = _run(
        _context(KEY), index=1, scope={"claude": "scoped", "render": renders[0]}
    )
    second = _run(
        _context(OTHER_KEY), index=2, scope={"claude": "scoped", "render": renders[1]}
    )
    merged = story_digest.merge_runs([first, second])
    assert merged["credential_scope"]["render"] == merged_render


@pytest.mark.unit
def test_merged_runs_concatenate_flows_sum_spend_and_keep_the_first_shards_facts() -> (
    None
):
    first = _run(_context(KEY), spend=1.25, index=1)
    second = _run(
        _context(OTHER_KEY),
        _context(OTHER_KEY, attempt=2),
        spend=2.5,
        index=2,
        scope={"claude": "ambient", "render": "unavailable"},
    )
    merged = story_digest.merge_runs([second, first])
    assert [(f["key"], f["attempt"]) for f in merged["flows"]] == [
        (OTHER_KEY, 1),
        (OTHER_KEY, 2),
        (KEY, 1),
    ]
    assert merged["spend_usd"] == 3.75
    assert merged["shard"] is None
    assert merged["stack_changed_mid_run"] is False
    assert merged["credential_scope"] == {"claude": "ambient", "render": "unavailable"}
    assert merged["model"] == MODEL
    assert len(story_verdict.run_record(merged)) == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    ("first", "second", "changed"),
    [
        ({}, {}, False),
        ({}, {"build": "2026-10-20"}, True),
        ({}, {"build": None}, False),
        ({"build": None}, {"build": None}, False),
        ({}, {"mid_run": True}, True),
    ],
)
def test_the_stack_changed_mid_run_when_the_shards_read_different_builds(
    first: dict[str, Any], second: dict[str, Any], changed: bool
) -> None:
    merged = story_digest.merge_runs(
        [_run(_context(KEY), **first), _run(_context(OTHER_KEY), index=2, **second)]
    )
    assert merged["stack_changed_mid_run"] is changed


@pytest.mark.unit
def test_a_merged_stack_names_the_first_version_and_build_any_shard_read() -> None:
    first = _run(_context(KEY), build=None)
    first["stack"]["version"] = None
    merged = story_digest.merge_runs([first, _run(_context(OTHER_KEY), index=2)])
    assert (merged["stack"]["version"], merged["stack"]["build"]) == (
        "0.6.12.dev1",
        "2026-10-19",
    )
    assert merged["stack_changed_mid_run"] is False


@pytest.mark.unit
def test_the_shards_reruns_that_did_not_run_are_merged_in_key_order() -> None:
    first = {**_run(_context(KEY)), "reruns_not_run": [{"key": KEY, "reason": "time"}]}
    second = {
        **_run(_context(OTHER_KEY), index=2),
        "reruns_not_run": [{"key": OTHER_KEY, "reason": "budget"}],
    }
    merged = story_digest.merge_runs([second, first])
    assert [item["key"] for item in merged["reruns_not_run"]] == sorted(
        [KEY, OTHER_KEY]
    )


@pytest.mark.unit
def test_merging_runs_refuses_overlap_bad_spend_and_bad_shapes() -> None:
    part = _run(_context(KEY))
    for parts in (
        [],
        [part, part],
        [part, {**part, "version": 2}],
        [{**part, "version": True}],
        [{**part, "spend_usd": "free"}],
        [{**part, "spend_usd": -1}],
        [{**part, "flows": [{"key": KEY}]}],
    ):
        with pytest.raises(story_verdict.RunRecordError):
            story_digest.merge_runs(parts)


HOSTILE = "@all [x](http://evil.invalid) <!-- note --> #12"


def _digest_fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    analyst = "find-extraction-model@new-analyst"
    results = _results(
        _result(
            KEY,
            verdict="stuck",
            confusion=[HOSTILE],
            actions_used=9,
            budget=50,
            overrun=1.8,
        ),
        _result(KEY, attempt=2, verdict="stuck"),
        _result(OTHER_KEY, verdict="not_refused", dead_end=[HOSTILE]),
        _result(OTHER_KEY, attempt=2, verdict="not_refused"),
        _result(analyst, verdict="not_run", sub_kind="precondition", audit="not_run"),
        _result(
            "route-folder-to-profile@new-analyst", verdict="offered", flags=["unproven"]
        ),
        _result("route-folder-to-profile@new-analyst", attempt=2, verdict="done"),
        _result(
            "tell-production-from-test@team-lead",
            verdict="error",
            refusals={"browser": 3, "selftest": 1},
        ),
        _result(
            "tell-production-from-test@new-analyst",
            verdict="gave_up",
            refusals={"browser": 1},
        ),
    )
    run = _run(
        _context(KEY),
        _context(KEY, attempt=2),
        _context(OTHER_KEY),
        _context(OTHER_KEY, attempt=2),
        _context(analyst, needs=["complete-document"]),
        scope={"claude": "scoped", "render": "unavailable"},
    )
    night = story_digest.night_from_results(
        results, run, pipeline_id=TONIGHT, develop_sha=DEVELOP
    )
    found = [story_digest.Regression(KEY, SERIES, "completion", "stuck", 3, 4)]
    digest = story_digest.digest_document(
        night,
        History((), (990,)),
        found,
        ["judge-field-accuracy@new-analyst"],
        spend_usd=18.4,
    )
    return digest, results, run


SECTIONS = (
    "# First-time-user stories — nightly digest",
    "## Regressions",
    "## Permission and visibility findings",
    "## Still failing",
    "## Unstable",
    "## Friction trend",
    "## Coverage",
    "## Harness health",
)


def _section_of(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    rest = text[start:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


@pytest.mark.unit
def test_the_digest_markdown_has_the_eight_sections_in_order() -> None:
    digest, results, run = _digest_fixture()
    text = story_digest.compose_digest_md(digest, results, run, uncovered=["A lead"])
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert tuple(headings) == SECTIONS


@pytest.mark.unit
def test_the_digest_header_names_develop_stack_docs_model_pins_and_spend() -> None:
    digest, results, run = _digest_fixture()
    text = story_digest.compose_digest_md(
        digest, results, run, uncovered=[], cap_usd=50
    )
    header = text.split("\n## ", 1)[0]
    for part in (
        "`1a2b3c4d` (pipeline 1000)",
        "`idp-ux-nightly` v0.6.12.dev1 (built 2026-10-19), state `updated`",
        "**docs:** `1a2b3c4d`",
        f"`{MODEL}`, effort `high`",
        "claude-code `2.1.281`, chrome-devtools-mcp `1.10.1`, Chrome `141.0.7390.54`",
        "$18.40 of a $50.00 cap",
    ):
        assert part in header, part
    plain = story_digest.compose_digest_md(digest, results, run, uncovered=[])
    assert "**spend:** $18.40\n" in plain


@pytest.mark.unit
def test_each_flow_lands_in_one_section_by_its_verdict() -> None:
    digest, results, run = _digest_fixture()
    text = story_digest.compose_digest_md(digest, results, run, uncovered=["A lead"])
    regressions = _section_of(text, "## Regressions")
    assert f"`{KEY}` — stuck, confirmed tonight; good on 3 of the last 4" in regressions
    assert "Fixed: `judge-field-accuracy@new-analyst`" in regressions
    permission = _section_of(text, "## Permission and visibility findings")
    assert f"`{OTHER_KEY}` — NOT REFUSED" in permission
    assert KEY not in permission
    unstable = _section_of(text, "## Unstable")
    assert "`route-folder-to-profile@new-analyst` — OFFERED" in unstable
    assert "mixed (1 of 2)" in unstable
    assert "`tell-production-from-test@new-analyst` — GAVE UP: max turns" in unstable
    assert _section_of(text, "## Still failing").strip() == "None."
    coverage = _section_of(text, "## Coverage")
    assert "- not run (precondition): `find-extraction-model@new-analyst`" in coverage
    assert "- not covered by any story: `A lead`" in coverage
    health = _section_of(text, "## Harness health")
    assert "harness error (journal): `tell-production-from-test@team-lead`" in health
    assert "- audits: 8 clean, 0 violations, 0 unavailable, 1 not_run" in health
    assert "- refusals by origin: browser 4, selftest 1" in health
    assert "- credential scope: claude `scoped`, render `unavailable`" in health
    assert "- history gaps: 990" in health
    assert (
        "`complete-document` missing for `find-extraction-model@new-analyst`" in health
    )
    friction = _section_of(text, "## Friction trend")
    assert f"| `{KEY}` | 1 | 0 | 0 | 1.8 | 9 of 50 |" in friction


@pytest.mark.unit
def test_the_digest_markdown_shows_no_model_prose_and_says_when_nothing_compared() -> (
    None
):
    digest, results, run = _digest_fixture()
    text = story_digest.compose_digest_md(digest, results, run, uncovered=[HOSTILE])
    assert "evil.invalid" in _section_of(text, "## Coverage")
    assert "evil.invalid" not in text.replace(_section_of(text, "## Coverage"), "")
    assert "@all" not in text and "http://" not in text and "<!--" not in text
    incomparable = {**digest, "comparable": False, "stack_state": "failed"}
    assert "Tonight is not comparable (the review stack's state is `failed`)" in (
        story_digest.compose_digest_md(incomparable, results, run, uncovered=[])
    )


@pytest.mark.unit
def test_the_slack_payload_has_four_plain_text_keys() -> None:
    digest, _, _ = _digest_fixture()
    payload = story_digest.slack_payload(
        digest, job_url="https://gitlab.invalid/-/jobs/1\n"
    )
    assert sorted(payload) == ["commit", "job_url", "status", "summary"]
    assert all(isinstance(value, str) for value in payload.values())
    assert payload["status"] == "regressions"
    assert payload["commit"] == "1a2b3c4d"
    assert payload["job_url"] == "https://gitlab.invalid/-/jobs/1"
    assert f"Regressions: {KEY} (completion, stuck, good 3 of 4)." in payload["summary"]
    assert "Fixed: judge-field-accuracy@new-analyst." in payload["summary"]
    assert "History gaps: 1 night(s)." in payload["summary"]
    assert "Spend: $18.40." in payload["summary"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"regressions": []}, "degraded"),
        ({"regressions": [], "flows": []}, "no_run"),
        (
            {
                "regressions": [],
                "flows": [
                    {
                        "key": KEY,
                        "series": SERIES,
                        "verdict": "not_run",
                        "state": "single",
                        "regressed": False,
                    }
                ],
            },
            "no_run",
        ),
        (
            {
                "regressions": [],
                "flows": [
                    {
                        "key": KEY,
                        "series": SERIES,
                        "verdict": "done",
                        "state": "single",
                        "regressed": False,
                    }
                ],
            },
            "clean",
        ),
        (
            {
                "regressions": [],
                "flows": [
                    {
                        "key": KEY,
                        "series": SERIES,
                        "verdict": "done",
                        "state": "single",
                        "regressed": False,
                    }
                ],
                "comparable": False,
            },
            "degraded",
        ),
        (
            {
                "regressions": [],
                "flows": [
                    {
                        "key": KEY,
                        "series": SERIES,
                        "verdict": "done",
                        "state": "single",
                        "regressed": False,
                    }
                ],
                "audits_clean": False,
            },
            "degraded",
        ),
    ],
)
def test_the_slack_status_says_regressions_clean_degraded_or_no_run(
    overrides: dict[str, Any], status: str
) -> None:
    digest, _, _ = _digest_fixture()
    assert (
        story_digest.slack_payload({**digest, **overrides}, job_url="")["status"]
        == status
    )


@pytest.mark.unit
def test_the_slack_summary_bounds_its_lists_and_is_built_from_closed_values_only() -> (
    None
):
    digest, _, _ = _digest_fixture()
    many = [
        {
            "key": f"story-{n}@persona-{n}",
            "series": SERIES,
            "kind": "completion",
            "verdict": "stuck",
            "good": 3,
            "counted": 4,
        }
        for n in range(400)
    ]
    hostile = {
        "key": "@all <!channel>",
        "series": SERIES,
        "kind": "completion",
        "verdict": "stuck",
        "good": 3,
        "counted": 4,
    }
    payload = story_digest.slack_payload(
        {**digest, "regressions": [hostile, *many]}, job_url="x"
    )
    summary = payload["summary"]
    assert "story-9@persona-9" in summary and "story-10@persona-10" not in summary
    assert "; and 390 more." in summary
    assert "Stack updated; comparable; audits clean." in summary
    assert "@all" not in summary and "<!channel>" not in summary
    assert (
        story_digest.slack_payload({**digest, "develop_sha": "nope"}, job_url="")[
            "commit"
        ]
        == ""
    )


@pytest.mark.unit
def test_the_slack_summary_is_cut_at_its_character_cap() -> None:
    digest, _, _ = _digest_fixture()
    long_id = "a" + "-b" * 29
    regressions = [
        {
            "key": f"{long_id}@{long_id}",
            "series": SERIES,
            "kind": "completion",
            "verdict": "stuck",
            "good": 3,
            "counted": 4,
        }
    ] * 10
    fixed = [f"{long_id}@{long_id}"] * 10
    payload = story_digest.slack_payload(
        {**digest, "regressions": regressions, "fixed": fixed}, job_url=""
    )
    assert len(payload["summary"]) <= story_digest.SLACK_SUMMARY_CHARS
    assert payload["summary"].endswith("…")


@pytest.mark.unit
def test_injection_notes_and_unrun_reruns_reach_the_digest_and_slack() -> None:
    results = _results(_result(KEY, verdict="stuck", injections=3))
    run = {**_run(_context(KEY)), "reruns_not_run": [{"key": KEY, "reason": "time"}]}
    night = story_digest.night_from_results(
        results, run, pipeline_id=TONIGHT, develop_sha=DEVELOP
    )
    digest = story_digest.digest_document(
        night, History((), ()), [], [], spend_usd=4.0, injections=3, reruns_not_run=1
    )
    text = story_digest.compose_digest_md(digest, results, run, uncovered=[])
    assert f"| `{KEY}` | 0 | 0 | 3 | - | - |" in _section_of(text, "## Friction trend")
    assert "- injection attempts noted: 3 (quotes in the shards' journals)" in text
    assert (
        "- confirming re-run not run (time), so the bad result stays unconfirmed: "
        f"`{KEY}`"
    ) in _section_of(text, "## Coverage")
    summary = story_digest.slack_payload(digest, job_url="")["summary"]
    assert "Injection attempts noted: 3." in summary
    assert "Confirming re-runs not run: 1." in summary


@pytest.mark.unit
def test_a_flow_still_failing_says_whether_it_was_ever_good_in_its_series() -> None:
    results = _results(_result(KEY, verdict="stuck"))
    run = _run(_context(KEY))
    night = story_digest.night_from_results(
        results, run, pipeline_id=TONIGHT, develop_sha=DEVELOP
    )
    digest = story_digest.digest_document(night, History((), ()), [], [], spend_usd=1)
    cases = (
        (_history("done", "stuck"), "; good on 1 of 2 earlier comparable night(s)"),
        (_history("stuck", "stuck"), "; never good in its series (2 earlier"),
        (History((), ()), "; no earlier comparable night in its series"),
        (None, ""),
    )
    for history, note in cases:
        text = story_digest.compose_digest_md(
            digest, results, run, uncovered=[], history=history
        )
        (line,) = _section_of(text, "## Still failing").strip().splitlines()
        assert line.startswith(f"- `{KEY}`")
        assert (note in line) if note else line.endswith(")")


@pytest.mark.unit
def test_a_night_no_shard_ran_says_so_and_is_a_gap_to_the_nights_after() -> None:
    digest = story_digest.idle_digest_document(
        pipeline_id=TONIGHT, develop_sha=DEVELOP, history=History((), (880,))
    )
    assert digest["shards_ran"] == 0 and digest["gaps"] == [880]
    later = History.from_digests([(TONIGHT, digest)])
    assert (later.nights, later.gaps) == ((), (TONIGHT,))
    text = story_digest.compose_idle_digest_md(
        digest, [("shard-1", "skipped", HOSTILE)]
    )
    assert "No shard ran a flow tonight" in text
    assert "- `shard-1`: skipped — " in text and "<!--" not in text
    payload = story_digest.slack_payload(digest, job_url="")
    assert payload["status"] == "no_run"
    assert "No shard ran tonight; the stack's state is not known." in payload["summary"]
    assert "audits" not in payload["summary"]
    with pytest.raises(ValueError):
        story_digest.idle_digest_document(
            pipeline_id=TONIGHT, develop_sha="nope", history=History((), ())
        )
