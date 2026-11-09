# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The nightly first-time-user story digest: merged shards, history and regressions."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from ux_runner import section, story_verdict

DIGEST_SCHEMA = "ux-test-stories/digest"
#: v2 adds the night's injection notes, its shards that ran, and its confirming
#: re-runs that did not; a v1 digest reads as a gap.
DIGEST_VERSION = 2
HISTORY_NIGHTS = 14
BASELINE_NIGHTS = 4
COMPLETION_WINDOW = 4
COMPLETION_GOOD = 3
FIXED_NIGHTS = 2
DIGEST_MAX_BYTES = 512 * 1024
SLACK_SUMMARY_CHARS = 2000
SLACK_INTRO = (
    "Each story is a first-time user doing one task from the docs alone, "
    "in a real browser."
)
#: Each verdict's icon on its Slack line: passed, refused as the docs say, missed
#: the docs' expectation, did not finish, the harness failed, never started.
SLACK_ICONS: dict[str, str] = {
    "done": "✅",
    "refused_as_expected": "🚫",
    "not_refused": "❌",
    "stuck": "❌",
    "offered": "❌",
    "out_of_steps": "⚠️",
    "gave_up": "⚠️",
    "unclear": "⚠️",
    "error": "🛠️",
    "not_run": "⏭️",
}
#: What a flow that did not pass did, in a few words, by verdict.
SLACK_WHY: dict[str, str] = {
    "not_refused": "not refused, did what the docs say this role cannot",
    "stuck": "got stuck",
    "offered": "offered a control the docs say this role lacks",
    "out_of_steps": "ran out of steps",
    "gave_up": "gave up",
    "unclear": "ended without a clear result",
    "error": "harness error",
    "not_run": "not run",
}
#: The same, where the verdict's sub-kind says more.
SLACK_WHY_BY_CAUSE: dict[tuple[str, str], str] = {
    ("stuck", "no_route"): "got stuck, found no way to do it",
    ("stuck", "wrong_outcome"): "got stuck, the result was wrong",
    ("stuck", "refused"): "got stuck, refused although allowed",
    ("stuck", "precondition"): "got stuck, something it needed was missing",
    ("gave_up", "budget_exhausted"): "gave up, used all its steps",
    ("gave_up", "refusal_limit"): "gave up, too many actions refused",
    ("gave_up", "max_turns"): "gave up, hit the turn limit",
    ("gave_up", "dollar_cap"): "gave up, hit its dollar cap",
    ("gave_up", "wall_clock"): "gave up, ran out of time",
    ("not_run", "persona"): "not run, no account for this role",
    ("not_run", "budget"): "not run, the night's budget ran out",
    ("not_run", "time"): "not run, the night ran out of time",
    ("not_run", "sign_in_refused"): "not run, its sign-in was refused",
    ("not_run", "since"): "not run, the stack is older than the story",
    ("not_run", "precondition"): "not run, the data it needs is missing",
    ("error", "launch"): "harness error, the browser did not start",
    ("error", "selftest"): "harness error, the browser self-test failed",
    ("error", "sign_in"): "harness error, signing in failed",
    ("error", "viewport"): "harness error, the browser window was the wrong size",
    ("error", "recorder"): "harness error, the screen recorder failed",
    ("error", "session"): "harness error, the session did not start",
    ("error", "audit"): "harness error, the browser audit found violations",
    ("error", "is_error"): "harness error, the model session ended in an error",
    ("error", "crash"): "harness error, the model session crashed",
    ("error", "journal"): "harness error, no usable journal",
    ("error", "signed_out"): "harness error, signed out mid-run",
    ("error", "relay"): "harness error, the browser relay died",
    ("error", "hang"): "harness error, the session hung",
}
#: Why a night is not compared, by the review stack's state.
SLACK_STACK_REASONS: dict[str, str] = {
    "not_requested": "the review stack was not updated, as this is not a scheduled "
    "develop run",
    "not_deployed": "the review stack is not deployed",
    "busy": "the review stack was busy with another operation",
    "boundary": "the review stack's permissions boundary is not its own",
    "failed": "the review stack's update failed",
    "running": "the review stack was still updating",
    "not_started": "the review stack's update never started",
    "not_reported": "the deploy did not report the review stack's update",
    "unrecognised": "the review stack's update could not be confirmed",
    "missing": "the review stack's update could not be confirmed",
}
STACK_HEADER_PREFIX = "🌙 UX review stack ("
#: The line the build prints after its consolidated summary and before the
#: Bedrock narrative, which is model output and is never read for the state.
NARRATIVE_MARKER = "🤖 Generating deployment summary with Bedrock..."
STACK_STATES: tuple[str, ...] = (
    "updated",
    "up_to_date",
    "not_requested",
    "not_deployed",
    "busy",
    "boundary",
    "failed",
    "running",
    "not_started",
    "not_reported",
    "unrecognised",
    "missing",
)
COMPARABLE_STACK_STATES: frozenset[str] = frozenset({"updated", "up_to_date"})
ROW_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (
        "updated",
        "updated",
        "Updated in place to tonight's template — {status} in {minutes} min",
    ),
    ("up_to_date", "up_to_date", "Already on tonight's template — nothing to update"),
    ("not_requested", "not_requested", "Not updated — not a scheduled develop run"),
    (
        "skipped/not_deployed",
        "not_deployed",
        "Not updated — the stack is not deployed in this account",
    ),
    (
        "skipped/busy",
        "busy",
        "Not updated — another operation was in progress ({status})",
    ),
    (
        "skipped/boundary_not_its_own",
        "boundary",
        "Not updated — its permissions boundary is neither its own nor the shared one",
    ),
    (
        "failed",
        "failed",
        "Update did not complete — {status} after {minutes} min ({phase})",
    ),
    (
        "running",
        "running",
        "Still updating when the run ended — {minutes} min so far; "
        "CloudFormation carries on",
    ),
    ("not_started", "not_started", "Not started (publish failed)"),
    ("", "not_reported", "Not reported"),
)

_SLOTS = {
    "{status}": "[A-Z_]+",
    "{minutes}": r"-?[0-9]+\.[0-9]",
    "{phase}": "[a-z_]+",
}
_SLOT_RE = re.compile("(" + "|".join(re.escape(slot) for slot in _SLOTS) + ")")
_ROW_ICONS = frozenset({"✅", "❌", "⚠️", "⏭️", "⚪", "❓"})
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SERIES_RE = re.compile(r"[0-9a-f]{12}")
_ID = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
_KEY_RE = re.compile(_ID + "@" + _ID)
_FINDING_ID_RE = re.compile(r"[A-Z]{4}-[0-9a-f]{8}")
_REGRESSION_KINDS = ("completion", "permission")
_COMPLETION_VERDICTS = frozenset({"stuck", "offered"})
_UNOBSERVED_VERDICTS = frozenset({"error", "not_run"})
_DIGEST_KEYS = frozenset(
    {
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
    }
)
_FLOW_KEYS = frozenset({"key", "series", "verdict", "state", "regressed"})
_REGRESSION_KEYS = frozenset({"key", "series", "kind", "verdict", "good", "counted"})
_MAX_DIGEST_FLOWS = 1000
_MAX_LISTED = 20
_LEAD_CHARS = 120
_TITLE_CHARS = 200
_NAME_CHARS = 120
_PIN_CHARS = 60
_URL_CHARS = 500
_SLACK_LISTED = 10
_SLACK_TITLE_CHARS = 80
_SLACK_TEXT_CHARS = frozenset(" ',;:()?-’")
_SLACK_PASSES = ("done", "refused_as_expected")
_STACK_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,127}")
_CLEAN_AUDITS = frozenset({"clean", "not_run"})
_AUDIT_STATES = ("clean", "violations", "unavailable", "not_run")


@dataclass(frozen=True)
class NightFlow:
    key: str
    series: str
    verdict: str
    state: str
    regressed: bool


@dataclass(frozen=True)
class Night:
    pipeline_id: int
    develop_sha: str
    stack_state: str
    comparable: bool
    audits_clean: bool
    flows: tuple[NightFlow, ...]

    def flow(self, key: str) -> NightFlow | None:
        """The flow with ``key`` this night, or None when it did not appear."""
        return next((flow for flow in self.flows if flow.key == key), None)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _night_flow(data: object) -> NightFlow | None:
    if not isinstance(data, Mapping) or set(data) != _FLOW_KEYS:
        return None
    key, series = data["key"], data["series"]
    verdict, state, regressed = data["verdict"], data["state"], data["regressed"]
    if not (
        isinstance(key, str)
        and _KEY_RE.fullmatch(key)
        and isinstance(series, str)
        and _SERIES_RE.fullmatch(series)
        and verdict in story_verdict.VERDICT_ORDER
        and state in story_verdict.CONFIRMATION_STATES
        and isinstance(regressed, bool)
    ):
        return None
    return NightFlow(
        key=key, series=series, verdict=verdict, state=state, regressed=regressed
    )


def _regression_shape(data: object) -> bool:
    return (
        isinstance(data, Mapping)
        and set(data) == _REGRESSION_KEYS
        and isinstance(data["key"], str)
        and _KEY_RE.fullmatch(data["key"]) is not None
        and isinstance(data["series"], str)
        and _SERIES_RE.fullmatch(data["series"]) is not None
        and data["kind"] in _REGRESSION_KINDS
        and data["verdict"] in story_verdict.VERDICT_ORDER
        and _is_int(data["good"])
        and _is_int(data["counted"])
    )


def _digest_size(data: Mapping[str, Any]) -> int | None:
    try:
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    return len(encoded.encode("utf-8"))


def _night_from_digest(pipeline_id: int, data: object) -> Night | None:
    """A prior night from its digest.json, or None when it is not a valid v1 digest."""
    if not isinstance(data, Mapping) or set(data) != _DIGEST_KEYS:
        return None
    size = _digest_size(data)
    if size is None or size > DIGEST_MAX_BYTES:
        return None
    flows = data["flows"]
    lists = (flows, data["regressions"], data["fixed"], data["gaps"])
    if not all(isinstance(item, list) for item in lists):
        return None
    stack_state = data["stack_state"]
    mid_run, comparable = data["stack_changed_mid_run"], data["comparable"]
    counts = (data["injections"], data["shards_ran"], data["reruns_not_run"])
    if not all(_is_int(count) and count >= 0 for count in counts):
        return None
    if data["shards_ran"] == 0:
        return None
    if not (
        data["schema"] == DIGEST_SCHEMA
        and _is_int(data["version"])
        and data["version"] == DIGEST_VERSION
        and _is_int(data["judgement_revision"])
        and _is_int(data["pipeline_id"])
        and data["pipeline_id"] == pipeline_id
        and isinstance(data["develop_sha"], str)
        and _SHA_RE.fullmatch(data["develop_sha"])
        and stack_state in STACK_STATES
        and isinstance(mid_run, bool)
        and isinstance(comparable, bool)
        and isinstance(data["audits_clean"], bool)
        and _is_number(data["spend_usd"])
        and data["spend_usd"] >= 0
        and len(flows) <= _MAX_DIGEST_FLOWS
        and all(_regression_shape(item) for item in data["regressions"])
        and all(isinstance(k, str) and _KEY_RE.fullmatch(k) for k in data["fixed"])
        and all(_is_int(gap) for gap in data["gaps"])
    ):
        return None
    if comparable and (stack_state not in COMPARABLE_STACK_STATES or mid_run):
        return None
    parsed = [_night_flow(item) for item in flows]
    night_flows = [flow for flow in parsed if flow is not None]
    if len(night_flows) != len(parsed):
        return None
    if len({flow.key for flow in night_flows}) != len(night_flows):
        return None
    return Night(
        pipeline_id=pipeline_id,
        develop_sha=data["develop_sha"],
        stack_state=stack_state,
        comparable=comparable,
        audits_clean=data["audits_clean"],
        flows=tuple(sorted(night_flows, key=lambda flow: flow.key)),
    )


@dataclass(frozen=True)
class History:
    nights: tuple[Night, ...]
    gaps: tuple[int, ...]

    @classmethod
    def from_digests(
        cls, digests: Sequence[tuple[int, Mapping[str, Any] | None]]
    ) -> History:
        """Prior nights, newest first, from (pipeline id, digest.json or None) pairs.

        A missing or invalid digest is a gap and the history carries on past it,
        up to HISTORY_NIGHTS nights, and so is the digest of a night on which no
        shard ran. Pipelines are ordered by id, newest first.
        """
        listed = [
            (pipeline_id, data) for pipeline_id, data in digests if _is_int(pipeline_id)
        ]
        listed.sort(key=lambda item: item[0], reverse=True)
        nights: list[Night] = []
        gaps: list[int] = []
        seen: set[int] = set()
        for pipeline_id, data in listed:
            if len(nights) >= HISTORY_NIGHTS:
                break
            if pipeline_id in seen:
                continue
            seen.add(pipeline_id)
            night = _night_from_digest(pipeline_id, data)
            if night is None:
                gaps.append(pipeline_id)
            else:
                nights.append(night)
        return cls(nights=tuple(nights), gaps=tuple(gaps))


def read_digest(raw: bytes | str) -> dict[str, Any] | None:
    """A fetched digest.json parsed, or None past DIGEST_MAX_BYTES or not a JSON object."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > DIGEST_MAX_BYTES:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class Regression:
    """A bad result tonight against the flow's comparable history.

    ``confirmed`` is False for one that stands unconfirmed only because its
    confirming re-run did not fit in the night's budget or time; the digest reads
    that back from the flow's state.
    """

    key: str
    series: str
    kind: str
    verdict: str
    good: int
    counted: int
    confirmed: bool = True


def _row_pattern(template: str) -> re.Pattern[str]:
    parts = _SLOT_RE.split(template)
    return re.compile(
        "".join(_SLOTS[part] if part in _SLOTS else re.escape(part) for part in parts)
    )


_ROW_PATTERNS = tuple(
    (state, _row_pattern(template)) for _, state, template in ROW_TEMPLATES
)


def classify_stack_state(summary_text: str | None) -> str:
    """The review stack's state from the row under its header in the build's summary.

    The text is the integration build's log, or its summary. Everything from the
    last NARRATIVE_MARKER line on is the Bedrock narrative and is left out, and of
    what remains the header read is the last: the build prints the consolidated
    summary just before that marker, so a header earlier in the text is not
    tonight's final word. Text with no marker is read whole. ``missing`` for no
    text or no header, ``unrecognised`` for a row that is not exactly one of
    ROW_TEMPLATES with its slots in their fixed shapes.
    """
    if not isinstance(summary_text, str) or not summary_text:
        return "missing"
    lines = summary_text.split("\n")
    markers = [
        index for index, line in enumerate(lines) if line.strip() == NARRATIVE_MARKER
    ]
    if markers:
        lines = lines[: markers[-1]]
    headers = [
        index
        for index, line in enumerate(lines)
        if line.lstrip().startswith(STACK_HEADER_PREFIX)
    ]
    if not headers:
        return "missing"
    header = headers[-1]
    row = next((line.strip() for line in lines[header + 1 :] if line.strip()), None)
    if row is None:
        return "unrecognised"
    icon, _, text = row.partition(" ")
    if icon not in _ROW_ICONS:
        return "unrecognised"
    for state, pattern in _ROW_PATTERNS:
        if pattern.fullmatch(text):
            return state
    return "unrecognised"


def _attempts(results: Mapping[str, Any]) -> list[story_verdict.FlowResult]:
    if not isinstance(results, Mapping):
        raise story_verdict.RunRecordError("results.json is not an object")
    version = results.get("version")
    if (
        results.get("schema") != story_verdict.RESULTS_SCHEMA
        or not _is_int(version)
        or version != story_verdict.RESULTS_VERSION
    ):
        raise story_verdict.RunRecordError("results.json is not results v1")
    flows = results.get("flows")
    if not isinstance(flows, list):
        raise story_verdict.RunRecordError("results.json has no flows list")
    return [story_verdict.FlowResult.from_json(item) for item in flows]


def _stack_state(run: Mapping[str, Any]) -> str:
    state = run.get("stack_state")
    if state is None:
        return "missing"
    return state if state in STACK_STATES else "unrecognised"


def night_from_results(
    results: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    pipeline_id: int,
    develop_sha: str,
) -> Night:
    """Tonight as history will read it: one NightFlow per flow key, by confirmation.

    The night is comparable when the stack state is comparable and the stack did
    not change mid-run; audits are clean when no attempt's audit found violations
    or could not run, a flow that never started having nothing to audit.
    """
    if not _is_int(pipeline_id):
        raise ValueError("pipeline_id must be an integer")
    if not isinstance(develop_sha, str) or not _SHA_RE.fullmatch(develop_sha):
        raise ValueError("develop_sha must be a full 40-character lowercase SHA")
    attempts = _attempts(results)
    stack_state = _stack_state(run)
    mid_run = run.get("stack_changed_mid_run") is True
    return Night(
        pipeline_id=pipeline_id,
        develop_sha=develop_sha,
        stack_state=stack_state,
        comparable=stack_state in COMPARABLE_STACK_STATES and not mid_run,
        audits_clean=all(attempt.audit in _CLEAN_AUDITS for attempt in attempts),
        flows=tuple(
            NightFlow(
                key=key,
                series=shown.series,
                verdict=confirmation.verdict,
                state=confirmation.state,
                regressed=False,
            )
            for key, confirmation, shown in story_verdict.flow_confirmations(attempts)
        ),
    )


def _observed(tonight: Night, history: History, flow: NightFlow) -> list[NightFlow]:
    """This flow's prior comparable observations in its series, newest first.

    A prior night counts when it was comparable and audits-clean, the flow ran in
    the same series, and its verdict was neither ``error`` nor ``not_run``: a flow
    that never started observed nothing.
    """
    seen = []
    for night in sorted(
        history.nights, key=lambda item: item.pipeline_id, reverse=True
    ):
        if night.pipeline_id >= tonight.pipeline_id:
            continue
        if not (night.comparable and night.audits_clean):
            continue
        prior = night.flow(flow.key)
        if (
            prior is None
            or prior.series != flow.series
            or prior.verdict in _UNOBSERVED_VERDICTS
        ):
            continue
        seen.append(prior)
    return seen


def _good(flow: NightFlow) -> bool:
    return flow.verdict in story_verdict.GOOD_VERDICTS


def _tonight_counts(tonight: Night, flow: NightFlow) -> bool:
    return tonight.comparable and tonight.audits_clean and flow.verdict != "error"


def regressions(
    tonight: Night, history: History, *, rerun_did_not_fit: Collection[str] = ()
) -> list[Regression]:
    """Bad results tonight, against each flow's own comparable history.

    A result counts when two attempts confirm it, and when it stands unconfirmed
    only because its confirming re-run did not fit: ``rerun_did_not_fit`` holds the
    keys the run's ``reruns_not_run`` names. That one is reported as unconfirmed.
    """
    found: list[Regression] = []
    for flow in sorted(tonight.flows, key=lambda item: item.key):
        unconfirmed = flow.state == "unconfirmed" and flow.key in rerun_did_not_fit
        if not _tonight_counts(tonight, flow) or not (
            flow.state == "confirmed" or unconfirmed
        ):
            continue
        prior = _observed(tonight, history, flow)
        if len(prior) < BASELINE_NIGHTS:
            continue
        if flow.verdict in _COMPLETION_VERDICTS:
            window = prior[:COMPLETION_WINDOW]
            good = sum(1 for item in window if _good(item))
            if good >= COMPLETION_GOOD:
                found.append(
                    Regression(
                        key=flow.key,
                        series=flow.series,
                        kind="completion",
                        verdict=flow.verdict,
                        good=good,
                        counted=len(window),
                        confirmed=not unconfirmed,
                    )
                )
        elif flow.verdict == "not_refused" and _good(prior[0]):
            found.append(
                Regression(
                    key=flow.key,
                    series=flow.series,
                    kind="permission",
                    verdict=flow.verdict,
                    good=1,
                    counted=1,
                    confirmed=not unconfirmed,
                )
            )
    return found


def _unconfirmed_keys(digest: Mapping[str, Any]) -> set[str]:
    """The keys of the digest's flows that stand unconfirmed tonight."""
    return {
        item["key"]
        for item in _items(digest, "flows")
        if isinstance(item, Mapping)
        and isinstance(item.get("key"), str)
        and item.get("state") == "unconfirmed"
    }


def fixed(tonight: Night, history: History) -> list[str]:
    """Keys whose latest regression closes tonight: the first FIXED_NIGHTS good in a row.

    Counted over comparable observations after the flow's most recent regressed
    night, tonight included, so a fix is reported on the night it lands rather than
    on every good night after it.
    """
    keys = []
    for flow in sorted(tonight.flows, key=lambda item: item.key):
        if not _tonight_counts(tonight, flow) or not _good(flow):
            continue
        prior = _observed(tonight, history, flow)
        latest = next((i for i, item in enumerate(prior) if item.regressed), None)
        if latest is None:
            continue
        since = [*reversed(prior[:latest]), flow]
        streaks = [
            index
            for index in range(FIXED_NIGHTS - 1, len(since))
            if all(_good(item) for item in since[index - FIXED_NIGHTS + 1 : index + 1])
        ]
        if streaks and streaks[0] == len(since) - 1:
            keys.append(flow.key)
    return keys


def _check_parts(
    parts: Sequence[Mapping[str, Any]], schema: str, version: int, what: str
) -> None:
    if not parts:
        raise story_verdict.RunRecordError(f"no {what} to merge")
    for part in parts:
        if (
            not isinstance(part, Mapping)
            or part.get("schema") != schema
            or not _is_int(part.get("version"))
            or part.get("version") != version
        ):
            raise story_verdict.RunRecordError(f"a shard's {what} is not v{version}")


def merge_results(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The shards' results.json as one, findings and summary recomputed over all flows."""
    _check_parts(
        parts, story_verdict.RESULTS_SCHEMA, story_verdict.RESULTS_VERSION, "results"
    )
    if any(
        not _is_int(part.get("judgement_revision"))
        or part.get("judgement_revision") != story_verdict.JUDGEMENT_REVISION
        for part in parts
    ):
        raise story_verdict.RunRecordError(
            "a shard was judged under a different judgement revision"
        )
    attempts = [attempt for part in parts for attempt in _attempts(part)]
    pairs = [(attempt.key, attempt.attempt) for attempt in attempts]
    if len(set(pairs)) != len(pairs):
        raise story_verdict.RunRecordError("two shards hold the same flow attempt")
    attempts.sort(key=lambda attempt: (attempt.key, attempt.attempt))
    found = story_verdict.findings(attempts)
    attached = story_verdict.attach_findings(attempts, found)
    return story_verdict.results_document(attached, found)


_SCOPE_ORDER = {
    "claude": ("ambient", "scoped"),
    "render": ("ambient", "unavailable", "scoped"),
}


def _merged_scope(parts: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    """Each credential scope at its weakest across the shards."""
    scopes = [part.get("credential_scope") for part in parts]
    merged: dict[str, str | None] = {}
    for name, order in _SCOPE_ORDER.items():
        values = {scope.get(name) for scope in scopes if isinstance(scope, Mapping)}
        merged[name] = next((value for value in order if value in values), None)
    return merged


def _stack_fact(part: Mapping[str, Any], name: str) -> object:
    stack = part.get("stack")
    return stack.get(name) if isinstance(stack, Mapping) else None


def merge_runs(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The shards' story-run.json as one: flows concatenated, spend summed.

    The stack changed mid-run when any shard says so or two shards read different
    builds; a shard that read none, because no page it opened showed one, read
    nothing to differ. The stack's version and build are the first any shard
    read, the confirming re-runs that did not run are every shard's, and the rest
    is the first shard's, with ``shard`` cleared and each credential scope at its
    weakest.
    """
    _check_parts(parts, story_verdict.RUN_SCHEMA, story_verdict.RUN_VERSION, "run")
    contexts = [context for part in parts for context in story_verdict.run_record(part)]
    pairs = [(context.key, context.attempt) for context in contexts]
    if len(set(pairs)) != len(pairs):
        raise story_verdict.RunRecordError("two shards planned the same flow attempt")
    spend = 0.0
    for part in parts:
        value = part.get("spend_usd")
        if not _is_number(value) or value < 0:
            raise story_verdict.RunRecordError("a shard's spend_usd is not a number")
        spend += float(value)
    merged = dict(parts[0])
    merged["shard"] = None
    merged["flows"] = [
        context.to_json()
        for context in sorted(contexts, key=lambda item: (item.key, item.attempt))
    ]
    merged["spend_usd"] = round(spend, 6)
    builds = {
        json.dumps(build)
        for part in parts
        if (build := _stack_fact(part, "build")) is not None
    }
    merged["stack_changed_mid_run"] = (
        any(part.get("stack_changed_mid_run") is True for part in parts)
        or len(builds) > 1
    )
    stack = dict(merged.get("stack") or {})
    for name in ("version", "build"):
        stack[name] = next(
            (value for part in parts if (value := _stack_fact(part, name)) is not None),
            None,
        )
    merged["stack"] = stack
    merged["reruns_not_run"] = sorted(
        (
            dict(item)
            for part in parts
            for item in (part.get("reruns_not_run") or [])
            if isinstance(item, Mapping)
        ),
        key=lambda item: str(item.get("key")),
    )
    merged["credential_scope"] = _merged_scope(parts)
    return merged


def digest_document(
    tonight: Night,
    history: History,
    found: Sequence[Regression],
    fixed_keys: Sequence[str],
    *,
    spend_usd: float,
    injections: int = 0,
    shards_ran: int = 1,
    reruns_not_run: int = 0,
) -> dict[str, Any]:
    """digest.json for tonight, which later nights read back as history.

    ``stack_changed_mid_run`` is what Night keeps of it: a comparable stack state
    on a night that is not comparable.
    """
    regressed = {regression.key for regression in found}
    return {
        "schema": DIGEST_SCHEMA,
        "version": DIGEST_VERSION,
        "judgement_revision": story_verdict.JUDGEMENT_REVISION,
        "pipeline_id": tonight.pipeline_id,
        "develop_sha": tonight.develop_sha,
        "stack_state": tonight.stack_state,
        "stack_changed_mid_run": tonight.stack_state in COMPARABLE_STACK_STATES
        and not tonight.comparable,
        "comparable": tonight.comparable,
        "audits_clean": tonight.audits_clean,
        "spend_usd": round(float(spend_usd), 6),
        "flows": [
            {
                "key": flow.key,
                "series": flow.series,
                "verdict": flow.verdict,
                "state": flow.state,
                "regressed": flow.key in regressed,
            }
            for flow in sorted(tonight.flows, key=lambda item: item.key)
        ],
        "regressions": [
            {
                "key": regression.key,
                "series": regression.series,
                "kind": regression.kind,
                "verdict": regression.verdict,
                "good": regression.good,
                "counted": regression.counted,
            }
            for regression in found
        ],
        "fixed": list(fixed_keys),
        "gaps": list(history.gaps),
        "injections": int(injections),
        "shards_ran": int(shards_ran),
        "reruns_not_run": int(reruns_not_run),
    }


def idle_digest_document(
    *, pipeline_id: int, develop_sha: str, history: History
) -> dict[str, Any]:
    """digest.json for a night on which no shard ran: its state is not known.

    It records the night, and a later night reads it as a gap rather than as a
    night with nothing in it.
    """
    if not _is_int(pipeline_id):
        raise ValueError("pipeline_id must be an integer")
    if not isinstance(develop_sha, str) or not _SHA_RE.fullmatch(develop_sha):
        raise ValueError("develop_sha must be a full 40-character lowercase SHA")
    return {
        "schema": DIGEST_SCHEMA,
        "version": DIGEST_VERSION,
        "judgement_revision": story_verdict.JUDGEMENT_REVISION,
        "pipeline_id": pipeline_id,
        "develop_sha": develop_sha,
        "stack_state": "missing",
        "stack_changed_mid_run": False,
        "comparable": False,
        "audits_clean": False,
        "spend_usd": 0.0,
        "flows": [],
        "regressions": [],
        "fixed": [],
        "gaps": list(history.gaps),
        "injections": 0,
        "shards_ran": 0,
        "reruns_not_run": 0,
    }


def compose_idle_digest_md(
    digest: Mapping[str, Any], skipped: Sequence[tuple[str, str, str]]
) -> str:
    """digest.md for a night on which no shard ran: each shard's precheck, if any.

    ``skipped`` holds each shard directory's name and the status and detail of
    the precheck it left, all of it the harness's own text.
    """
    develop = str(digest.get("develop_sha") or "")
    lines = [
        "# First-time-user stories — nightly digest",
        "",
        f"- **develop:** `{develop[:8]}` (pipeline {digest.get('pipeline_id')})",
        "",
        "No shard ran a flow tonight, so the review stack's state is not known and "
        "nothing is compared; later nights read this one as a gap.",
        "",
        "## Shards",
        "",
    ]
    shards = [
        f"- `{_code(name)}`: {_code(status)} — {section.sanitize(detail, _TITLE_CHARS)}"
        for name, status, detail in skipped
    ]
    lines += _listed(shards, "No shard left a precheck.")
    gaps = [str(gap) for gap in digest.get("gaps", []) if _is_int(gap)]
    lines += ["", "- history gaps: " + (", ".join(gaps) if gaps else "none")]
    return "\n".join(lines) + "\n"


def _code(text: object, limit: int = _NAME_CHARS) -> str:
    return section.sanitize(text, limit).replace("`", "'")


def _key(value: object) -> str:
    """A flow key verbatim, since its closed shape holds nothing GitLab acts on in code."""
    return value if isinstance(value, str) and _KEY_RE.fullmatch(value) else "?"


def _ids(values: Sequence[str]) -> str:
    shown = [f"`{value}`" for value in values if _FINDING_ID_RE.fullmatch(value)]
    return ", ".join(shown) or "no finding"


def _label(confirmation: story_verdict.Confirmation, shown: Any) -> str:
    return section.sanitize(
        story_verdict.verdict_label(shown, confirmation), _TITLE_CHARS
    )


def _listed(lines: Sequence[str], empty: str) -> list[str]:
    if not lines:
        return [empty]
    shown = list(lines)[:_MAX_LISTED]
    if len(lines) > _MAX_LISTED:
        shown.append(f"- and {len(lines) - _MAX_LISTED} more")
    return shown


def _header(
    digest: Mapping[str, Any], run: Mapping[str, Any], cap_usd: float | None
) -> list[str]:
    stack = run.get("stack") if isinstance(run.get("stack"), Mapping) else {}
    facts = section.StackFacts(
        name=str(stack.get("name") or "unknown"),
        region=str(run.get("region") or ""),
        version=stack.get("version") if isinstance(stack.get("version"), str) else None,
        build=stack.get("build") if isinstance(stack.get("build"), str) else None,
    )
    shown_stack = f"`{_code(facts.name)}`"
    if facts.shown_version:
        shown_stack += f" v{_code(facts.shown_version)}"
    if facts.shown_build:
        shown_stack += f" (built {facts.shown_build})"
    develop = str(digest.get("develop_sha") or "")
    docs = run.get("docs") if isinstance(run.get("docs"), Mapping) else {}
    ref = docs.get("ref")
    docs_text = (
        f"`{ref[:8]}`"
        if isinstance(ref, str) and _SHA_RE.fullmatch(ref)
        else "the working tree"
    )
    versions = run.get("versions") if isinstance(run.get("versions"), Mapping) else {}
    pins = ", ".join(
        f"{name} `{_code(versions.get(key), _PIN_CHARS)}`"
        for key, name in (
            ("claude_code", "claude-code"),
            ("chrome_devtools_mcp", "chrome-devtools-mcp"),
            ("chrome", "Chrome"),
        )
        if isinstance(versions.get(key), str)
    )
    spend = digest.get("spend_usd")
    spend_text = f"${float(spend):.2f}" if _is_number(spend) else "unknown"
    if cap_usd is not None and _is_number(cap_usd):
        spend_text += f" of a ${float(cap_usd):.2f} cap"
    return [
        "# First-time-user stories — nightly digest",
        "",
        f"- **develop:** `{develop[:8]}` (pipeline {digest.get('pipeline_id')})",
        f"- **stack:** {shown_stack}, state `{_code(digest.get('stack_state'))}`",
        f"- **docs:** {docs_text}",
        f"- **model:** `{_code(run.get('model'))}`, effort `{_code(run.get('effort'))}`",
        f"- **tool pins:** {pins or 'unknown'}",
        f"- **spend:** {spend_text}",
    ]


def _comparability(digest: Mapping[str, Any]) -> str | None:
    """Why tonight was not compared with earlier nights, or None when it was."""
    reasons = []
    if digest.get("stack_state") not in COMPARABLE_STACK_STATES:
        reasons.append(
            f"the review stack's state is `{_code(digest.get('stack_state'))}`"
        )
    if digest.get("stack_changed_mid_run") is True:
        reasons.append("the stack changed mid-run")
    if digest.get("audits_clean") is not True:
        reasons.append("an audit was not clean")
    if not reasons:
        return None
    return (
        "Tonight is not comparable (" + "; ".join(reasons) + "), so nothing is flagged."
    )


def _needs_by_key(run: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    needs: dict[str, tuple[str, ...]] = {}
    flows = run.get("flows")
    for item in flows if isinstance(flows, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("key"), str):
            value = item.get("needs")
            if isinstance(value, list):
                needs[item["key"]] = tuple(str(name) for name in value)
    return needs


def _prior_good(
    history: History, pipeline_id: object, key: str, series: str
) -> tuple[int, int]:
    """How many of a flow's earlier comparable observations in its series were good."""
    seen = [
        prior
        for night in history.nights
        if night.comparable
        and night.audits_clean
        and (not _is_int(pipeline_id) or night.pipeline_id < pipeline_id)
        and (prior := night.flow(key)) is not None
        and prior.series == series
        and prior.verdict not in _UNOBSERVED_VERDICTS
    ]
    return sum(1 for prior in seen if _good(prior)), len(seen)


def _failing_note(
    history: History | None, pipeline_id: object, key: str, series: str
) -> str:
    if history is None:
        return ""
    good, counted = _prior_good(history, pipeline_id, key, series)
    if not counted:
        return "; no earlier comparable night in its series"
    if not good:
        return f"; never good in its series ({counted} earlier comparable night(s))"
    return (
        f"; good on {good} of {counted} earlier comparable night(s), not flagged as "
        "a regression"
    )


def compose_digest_md(
    digest: Mapping[str, Any],
    results: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    uncovered: Sequence[str],
    cap_usd: float | None = None,
    history: History | None = None,
) -> str:
    """digest.md: the header, then its eight sections, in order.

    Only closed-set values, keys, counts, hashes and sanitised titles and leads
    are shown; no model prose is. With the ``history`` tonight was compared with,
    a flow still failing says whether it was ever good in its series.
    """
    attempts = _attempts(results)
    flows = story_verdict.flow_confirmations(attempts)
    regressed = {
        item["key"]
        for item in digest.get("regressions", [])
        if isinstance(item, Mapping) and isinstance(item.get("key"), str)
    }
    lines = _header(digest, run, cap_usd)
    lines += ["", "## Regressions", ""]
    reason = _comparability(digest)
    if reason is not None:
        lines.append(reason)
    regression_lines = []
    unconfirmed = _unconfirmed_keys(digest)
    for item in digest.get("regressions", []):
        if isinstance(item, Mapping):
            tonight = (
                "unconfirmed tonight, as its confirming re-run did not fit"
                if item.get("key") in unconfirmed
                else "confirmed tonight"
            )
            regression_lines.append(
                f"- `{_key(item.get('key'))}` — {_code(item.get('verdict'))}, "
                f"{tonight}; good on {item.get('good')} of the last "
                f"{item.get('counted')} comparable nights ({_code(item.get('kind'))})"
            )
    lines += _listed(regression_lines, "None tonight.")
    fixed_keys = [_key(key) for key in digest.get("fixed", []) if isinstance(key, str)]
    if fixed_keys:
        lines += ["", "Fixed: " + ", ".join(f"`{key}`" for key in fixed_keys)]

    permission = []
    failing = []
    unstable = []
    for key, confirmation, shown in flows:
        line = (
            f"- `{_key(key)}` — {_label(confirmation, shown)} ({_ids(shown.findings)})"
        )
        visibility = confirmation.verdict in ("not_refused", "offered") or (
            confirmation.verdict == "stuck" and shown.sub_kind == "refused"
        )
        if confirmation.state == "mixed" or confirmation.verdict in (
            "out_of_steps",
            "gave_up",
        ):
            unstable.append(f"- `{_key(key)}` — {_label(confirmation, shown)}")
        elif key in regressed:
            continue
        elif visibility:
            permission.append(line)
        elif confirmation.verdict in story_verdict.BAD_VERDICTS:
            note = _failing_note(history, digest.get("pipeline_id"), key, shown.series)
            failing.append(line + note)
    lines += ["", "## Permission and visibility findings", ""]
    lines += _listed(permission, "None.")
    lines += ["", "## Still failing", ""]
    lines += _listed(failing, "None.")
    lines += ["", "## Unstable", ""]
    lines += _listed(unstable, "None.")

    lines += ["", "## Friction trend", ""]
    if flows:
        lines += [
            "| Flow | Hesitations | Dead ends | Injections | Overrun | Steps |",
            "|---|---|---|---|---|---|",
        ]
        for key, _, shown in flows:
            overrun = f"{shown.overrun:.1f}" if _is_number(shown.overrun) else "-"
            steps = (
                f"{shown.actions_used} of {shown.budget}"
                if _is_int(shown.actions_used) and _is_int(shown.budget)
                else "-"
            )
            lines.append(
                f"| `{_key(key)}` | {len(shown.notes.confusion)} | "
                f"{len(shown.notes.dead_end)} | {shown.notes.injections} | "
                f"{overrun} | {steps} |"
            )
    else:
        lines.append("No flow ran.")

    lines += ["", "## Coverage", ""]
    by_reason: dict[str, list[str]] = {}
    for key, confirmation, shown in flows:
        if confirmation.verdict == "not_run":
            by_reason.setdefault(shown.sub_kind or "unknown", []).append(key)
    coverage = [
        f"- not run ({_code(reason_key)}): "
        + ", ".join(f"`{_key(key)}`" for key in keys)
        for reason_key, keys in sorted(by_reason.items())
    ]
    reruns: dict[str, list[str]] = {}
    for item in run.get("reruns_not_run") or []:
        if isinstance(item, Mapping):
            reruns.setdefault(str(item.get("reason") or "unknown"), []).append(
                _key(item.get("key"))
            )
    coverage += [
        f"- confirming re-run not run ({_code(reason_key)}), so the bad result stays "
        "unconfirmed: " + ", ".join(f"`{key}`" for key in keys)
        for reason_key, keys in sorted(reruns.items())
    ]
    leads = [_code(lead, _LEAD_CHARS) for lead in uncovered]
    coverage += [f"- not covered by any story: `{lead}`" for lead in leads if lead]
    lines += _listed(
        coverage,
        "Every planned flow ran, and no [Unreleased] entry is left without a story.",
    )

    lines += ["", "## Harness health", ""]
    errors = [
        f"- harness error ({_code(shown.sub_kind or 'unknown')}): `{_key(key)}`"
        for key, confirmation, shown in flows
        if confirmation.verdict == "error"
    ]
    lines += _listed(errors, "- no harness error")
    audits = {state: 0 for state in _AUDIT_STATES}
    origins: dict[str, int] = {}
    for attempt in attempts:
        audits[attempt.audit] = audits.get(attempt.audit, 0) + 1
        for origin, count in attempt.refusals_by_origin.items():
            if _is_int(count):
                origins[origin] = origins.get(origin, 0) + count
    lines.append(
        "- audits: "
        + ", ".join(f"{count} {_code(state)}" for state, count in audits.items())
    )
    injections = sum(attempt.notes.injections for attempt in attempts)
    lines.append(
        f"- injection attempts noted: {injections}"
        + (" (quotes in the shards' journals)" if injections else "")
    )
    lines.append(
        "- refusals by origin: "
        + (
            ", ".join(
                f"{_code(origin)} {count}" for origin, count in sorted(origins.items())
            )
            or "none"
        )
    )
    scope = run.get("credential_scope")
    scope = scope if isinstance(scope, Mapping) else {}
    lines.append(
        f"- credential scope: claude `{_code(scope.get('claude') or 'unknown')}`, "
        f"render `{_code(scope.get('render') or 'unknown')}`"
    )
    gaps = [str(gap) for gap in digest.get("gaps", []) if _is_int(gap)]
    lines.append("- history gaps: " + (", ".join(gaps) if gaps else "none"))
    needs = _needs_by_key(run)
    blocked: dict[str, list[str]] = {}
    for key, confirmation, shown in flows:
        if confirmation.verdict == "not_run" and shown.sub_kind == "precondition":
            for fixture in needs.get(key, ()) or ("unknown",):
                blocked.setdefault(fixture, []).append(key)
    lines.append(
        "- fixtures: "
        + (
            "; ".join(
                f"`{_code(fixture)}` missing for "
                + ", ".join(f"`{_key(key)}`" for key in keys)
                for fixture, keys in sorted(blocked.items())
            )
            or "no flow reported a missing precondition"
        )
    )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class SlackFacts:
    """What the Slack summary reads beside digest.json, none of it model output.

    ``results`` and ``run`` are the merged night's results.json and
    story-run.json, ``history`` the earlier nights it was compared with, None
    when they were not read, ``titles`` each story id's title in the catalogue,
    and ``cap_usd`` the night's dollar cap.
    """

    results: Mapping[str, Any] | None = None
    run: Mapping[str, Any] | None = None
    history: History | None = None
    titles: Mapping[str, str] = field(default_factory=dict)
    cap_usd: float | None = None


def _items(digest: Mapping[str, Any], name: str) -> list[Any]:
    value = digest.get(name)
    return value if isinstance(value, list) else []


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _slack_text(text: object, limit: int) -> str:
    """``text`` as words Slack shows as typed: letters, digits and a little punctuation.

    Every other character is a space, so nothing in it can mention, link or
    format.
    """
    if not isinstance(text, str):
        return ""
    kept = "".join(
        char if char.isalnum() or char in _SLACK_TEXT_CHARS else " "
        for char in section.flatten(text)
    )
    return " ".join(kept.split())[:limit].strip()


def _who(key: str, titles: Mapping[str, str]) -> str:
    """A flow as its story's catalogue title and its persona's id in words."""
    story, _, persona = key.partition("@")
    title = _slack_text(titles.get(story), _SLACK_TITLE_CHARS)
    if not title:
        title = story.replace("-", " ").capitalize()
    return f"{title} ({persona.replace('-', ' ')})"


def _shown_flows(
    results: Mapping[str, Any] | None,
) -> dict[str, story_verdict.FlowResult]:
    """Each flow key's shown attempt in ``results``; none when it cannot be read."""
    if results is None:
        return {}
    try:
        flows = story_verdict.flow_confirmations(_attempts(results))
    except (story_verdict.RunRecordError, ValueError):
        return {}
    return {key: shown for key, _, shown in flows}


def _why(verdict: str, shown: story_verdict.FlowResult | None) -> str:
    cause = shown.sub_kind if shown is not None and shown.verdict == verdict else None
    return SLACK_WHY_BY_CAUSE.get(
        (verdict, cause or ""), SLACK_WHY.get(verdict, verdict.replace("_", " "))
    )


def _regression_note(item: Mapping[str, Any], confirmed: bool) -> str:
    good, counted = item["good"], item["counted"]
    seen = (
        "its last compared night"
        if counted == 1
        else f"{good} of its last {counted} compared nights"
    )
    if confirmed:
        return f" (a regression: good on {seen})"
    return f" (possibly a regression, its re-run did not fit: good on {seen})"


_SLACK_CONFIRMATIONS = {
    "confirmed": " (confirmed by a re-run)",
    "unconfirmed": " (unconfirmed)",
    "mixed": " (a re-run disagreed)",
}


def _failure_lines(
    flows: Sequence[NightFlow],
    regressed: Mapping[str, Mapping[str, Any]],
    unconfirmed: Collection[str],
    shown: Mapping[str, story_verdict.FlowResult],
    titles: Mapping[str, str],
) -> list[str]:
    """Confirmed regressions, then the other flows that did not pass, worst first."""
    by_key = {flow.key: flow for flow in flows}
    confirmed_lines: list[str] = []
    other_lines: list[str] = []
    for key, item in regressed.items():
        flow = by_key.get(key)
        verdict = flow.verdict if flow is not None else item["verdict"]
        confirmed = key not in unconfirmed
        line = (
            f"{SLACK_ICONS[verdict]} {_who(key, titles)}: "
            f"{_why(verdict, shown.get(key))}{_regression_note(item, confirmed)}"
        )
        (confirmed_lines if confirmed else other_lines).append(line)
    failing = sorted(
        (
            flow
            for flow in flows
            if flow.key not in regressed
            and flow.verdict not in story_verdict.GOOD_VERDICTS
        ),
        key=lambda flow: (story_verdict.VERDICT_ORDER.index(flow.verdict), flow.key),
    )
    other_lines += [
        f"{SLACK_ICONS[flow.verdict]} {_who(flow.key, titles)}: "
        f"{_why(flow.verdict, shown.get(flow.key))}"
        f"{_SLACK_CONFIRMATIONS.get(flow.state, '')}"
        for flow in failing
    ]
    return confirmed_lines + other_lines


def _pass_lines(
    flows: Sequence[NightFlow],
    shown: Mapping[str, story_verdict.FlowResult],
    titles: Mapping[str, str],
) -> list[str]:
    """The flows that passed: done with its steps, then refused as the docs say."""
    lines: list[str] = []
    unit = " steps"
    for verdict in _SLACK_PASSES:
        for flow in flows:
            if flow.verdict != verdict:
                continue
            who = _who(flow.key, titles)
            if verdict == "refused_as_expected":
                lines.append(f"{SLACK_ICONS[verdict]} {who}: refused, as the docs say")
                continue
            result = shown.get(flow.key)
            steps = ""
            if (
                result is not None
                and _is_int(result.actions_used)
                and _is_int(result.budget)
            ):
                steps = f", {result.actions_used} of {result.budget}{unit}"
                unit = ""
            lines.append(f"{SLACK_ICONS[verdict]} {who}{steps}")
    return lines


def _uncompared_nights(digest: Mapping[str, Any], history: History | None) -> int:
    """How many nights in a row, tonight and back, could not be compared.

    A night counts as compared when it was comparable and its audits clean, and
    an earlier night whose digest could not be read, a gap, counts as one that
    was not. Without the history, only tonight is known.
    """
    if digest.get("comparable") is True and digest.get("audits_clean") is True:
        return 0
    if history is None:
        return 1
    tonight = digest.get("pipeline_id")
    earlier = sorted(
        [
            (night.pipeline_id, night.comparable and night.audits_clean)
            for night in history.nights
        ]
        + [(gap, False) for gap in history.gaps if _is_int(gap)],
        reverse=True,
    )
    count = 1
    for pipeline_id, compared in earlier:
        if _is_int(tonight) and pipeline_id >= tonight:
            continue
        if compared:
            break
        count += 1
    return count


def _slack_status(
    digest: Mapping[str, Any],
    flows: Sequence[NightFlow],
    confirmed: int,
    uncompared: int,
) -> str:
    """The night in a few words, from its stories.

    A night that could not be compared changes it only once BASELINE_NIGHTS
    nights in a row, tonight among them, could not be, because a run that long
    is one in which no regression can be flagged.
    """
    if digest.get("shards_ran") == 0 or all(
        flow.verdict == "not_run" for flow in flows
    ):
        return "⏭️ not run"
    if confirmed:
        return f"❌ {_plural(confirmed, 'regression')}"
    if digest.get("audits_clean") is not True or any(
        flow.verdict == "error" for flow in flows
    ):
        return "⚠️ harness problem"
    passed = sum(1 for flow in flows if flow.verdict in story_verdict.GOOD_VERDICTS)
    tally = f"{passed} of {len(flows)} passed"
    if uncompared >= BASELINE_NIGHTS:
        return f"⚠️ {tally}, not compared for {uncompared} nights"
    return f"{'✅' if passed == len(flows) else '⚠️'} {tally}"


def _compared_nights(history: History, pipeline_id: object) -> int:
    return sum(
        1
        for night in history.nights
        if night.comparable
        and night.audits_clean
        and (not _is_int(pipeline_id) or night.pipeline_id < pipeline_id)
    )


def _comparison_line(
    digest: Mapping[str, Any], history: History | None, confirmed: int, unsure: int
) -> str:
    """Whether tonight was compared with earlier nights, and if not, why not."""
    reasons = []
    state = digest.get("stack_state")
    state = state if isinstance(state, str) else "unrecognised"
    if state not in COMPARABLE_STACK_STATES:
        reasons.append(
            SLACK_STACK_REASONS.get(state, SLACK_STACK_REASONS["unrecognised"])
        )
    elif digest.get("stack_changed_mid_run") is True:
        reasons.append("the review stack changed during the run")
    elif digest.get("comparable") is not True:
        reasons.append(SLACK_STACK_REASONS["unrecognised"])
    if digest.get("audits_clean") is not True:
        reasons.append("a browser audit was not clean")
    if reasons:
        return "Not compared with earlier nights: " + "; ".join(reasons) + "."
    if history is None:
        return "Not compared with earlier nights: they could not be read."
    nights = _compared_nights(history, digest.get("pipeline_id"))
    gaps = sum(1 for gap in _items(digest, "gaps") if _is_int(gap))
    unread = f" ({gaps} could not be read)" if gaps else ""
    if not nights:
        return f"Not compared: no earlier night was comparable{unread}."
    compared = f"Compared with {_plural(nights, 'earlier night')}{unread}"
    if nights < BASELINE_NIGHTS:
        return (
            f"{compared}: too few yet to flag a regression ({BASELINE_NIGHTS} needed)."
        )
    found = []
    if confirmed:
        found.append(_plural(confirmed, "regression"))
    if unsure:
        found.append(
            f"{_plural(unsure, 'possible regression')} whose re-run did not fit"
        )
    return f"{compared}: {', '.join(found) or 'no regressions'}."


def _slack_head(sha8: str, run: Mapping[str, Any] | None) -> str:
    head = f"develop {sha8 or 'unknown'}"
    stack = run.get("stack") if isinstance(run, Mapping) else None
    if not isinstance(stack, Mapping):
        return head
    name = stack.get("name")
    if not isinstance(name, str) or not _STACK_NAME_RE.fullmatch(name):
        return head
    version = section.StackFacts(
        name=name, region="", version=stack.get("version")
    ).shown_version
    return f"{head} · {name}" + (f" v{version}" if version else "")


def _fit(
    head: Sequence[str],
    failing: Sequence[str],
    passing: Sequence[str],
    foot: Sequence[str],
    limit: int,
) -> str:
    """The summary within ``limit``: passes give way first, so a failure stays listed."""
    for shown in range(len(passing), -1, -1):
        more = (
            [] if shown == len(passing) else [f"and {len(passing) - shown} more passed"]
        )
        text = "\n".join([*head, *failing, *passing[:shown], *more, *foot])
        if len(text) <= limit:
            return text
    passed = [f"and {len(passing)} more passed"] if passing else []
    for shown in range(len(failing) - 1, -1, -1):
        more = [f"and {len(failing) - shown} more did not pass"]
        text = "\n".join([*head, *failing[:shown], *more, *passed, *foot])
        if len(text) <= limit:
            return text
    return _cut([*head, *foot], limit)


def _cut(lines: Sequence[str], limit: int) -> str:
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    for line in lines:
        if len("\n".join([*kept, line, "…"])) > limit:
            break
        kept.append(line)
    return "\n".join([*kept, "…"])[:limit]


def _some(items: Sequence[str], *, separator: str = "; ") -> str:
    shown = separator.join(items[:_SLACK_LISTED])
    if len(items) > _SLACK_LISTED:
        shown += f"{separator}and {len(items) - _SLACK_LISTED} more"
    return shown


def slack_payload(
    digest: Mapping[str, Any], *, job_url: str, facts: SlackFacts | None = None
) -> dict[str, str]:
    """The Slack workflow's four variables, in plain text built from closed values only.

    ``status`` is the night's outcome from its stories, and says so once no night
    has been compared for BASELINE_NIGHTS nights in a row. ``summary`` lists every
    flow, under its catalogue title and persona, confirmed regressions first,
    then the other flows that did not pass, each saying why, then the passes,
    which give way to "and N more passed" when the summary would pass
    SLACK_SUMMARY_CHARS. It closes with whether the night was compared and
    with what, its friction and its spend. Titles are the catalogue's, reduced
    to letters, digits and plain punctuation, and everything else is a closed
    value, a count or fixed text, so nothing in it can mention a channel or
    carry a link but ``job_url``, which is the job's own.
    """
    facts = facts or SlackFacts()
    develop = digest.get("develop_sha")
    sha8 = (
        develop[:8] if isinstance(develop, str) and _SHA_RE.fullmatch(develop) else ""
    )
    flows = [
        flow
        for item in _items(digest, "flows")
        if (flow := _night_flow(item)) is not None
    ]
    regressed = {
        item["key"]: item
        for item in _items(digest, "regressions")
        if _regression_shape(item)
    }
    unconfirmed = _unconfirmed_keys(digest)
    confirmed = sum(1 for key in regressed if key not in unconfirmed)
    url = section.flatten(job_url) if isinstance(job_url, str) else ""
    payload = {
        "status": _slack_status(
            digest, flows, confirmed, _uncompared_nights(digest, facts.history)
        ),
        "commit": sha8,
        "job_url": url[:_URL_CHARS],
    }
    head = _slack_head(sha8, facts.run)
    if digest.get("shards_ran") == 0:
        payload["summary"] = (
            f"{head}\nNo shard ran tonight, so no story was tried and nothing was "
            "compared."
        )
        return payload
    shown = _shown_flows(facts.results)
    titles = facts.titles if isinstance(facts.titles, Mapping) else {}
    foot = [
        "",
        _comparison_line(digest, facts.history, confirmed, len(regressed) - confirmed),
    ]
    fixed_flows = [
        _who(key, titles)
        for key in _items(digest, "fixed")
        if isinstance(key, str) and _KEY_RE.fullmatch(key)
    ]
    if fixed_flows:
        foot.append(
            "Fixed after an earlier regression: "
            + _some(fixed_flows, separator=", ")
            + "."
        )
    injections = digest.get("injections")
    if _is_int(injections) and injections > 0:
        foot.append(f"Injection attempts noted: {injections}.")
    reruns = digest.get("reruns_not_run")
    if _is_int(reruns) and reruns > 0:
        foot.append(f"Confirming re-runs that did not fit: {reruns}.")
    if shown:
        hesitations = sum(len(result.notes.confusion) for result in shown.values())
        dead_ends = sum(len(result.notes.dead_end) for result in shown.values())
        foot.append(
            f"Friction: {_plural(hesitations, 'hesitation')}, "
            f"{_plural(dead_ends, 'dead end')}."
        )
    spend = digest.get("spend_usd")
    if _is_number(spend):
        cap = facts.cap_usd
        foot.append(
            f"Spend: ${float(spend):.2f}"
            + (f" of ${float(cap):.2f}" if _is_number(cap) else "")
        )
    payload["summary"] = _fit(
        [head, SLACK_INTRO, ""],
        _failure_lines(flows, regressed, unconfirmed, shown, titles),
        _pass_lines(flows, shown, titles),
        foot,
        SLACK_SUMMARY_CHARS,
    )
    return payload
