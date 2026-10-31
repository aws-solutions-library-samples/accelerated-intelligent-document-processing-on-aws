# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pick the catalogue flows an MR's UI change touches, from ``scripts/ux_flows.yaml``.

The unattended UX test always looks at the change itself first. What it adds
after that comes from the same catalogue the interactive skill uses, so a finding
is filed against the same story id whichever way the review ran. The link from a
flow to the code that implements it is the optional ``paths:`` field, a list of
``src/ui/`` globs: a flow is a candidate for an MR when one of the MR's
user-visible files matches one of its globs.

Selection is a pure ranking, because the run has a fixed budget and the order is
the only thing a reader can check afterwards. A flow scores one point per
user-visible file its globs match; ties go to the higher priority and then to the
lower id, so equal inputs always choose the same flows. Only flows for the run's
persona are considered (the session is signed in as one group), and a flow with
no ``paths:`` is never chosen, which keeps the catalogue's other uses unaffected
by this one.

:func:`load_flows` validates what the prompt relies on and names the flow that
breaks it, because the file is hand-edited and a malformed flow found in CI is an
MR that gets no UX test for a reason nobody can see. ``yaml`` is imported inside
it, so importing this module costs nothing on a bare python3.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ux_runner import ui_change

#: Lower ranks first. A priority outside this table ranks after all of them.
PRIORITY_RANK = {"p0": 0, "p1": 1, "p2": 2}


class FlowsError(Exception):
    """The flows file is missing or malformed."""


@dataclass(frozen=True)
class Flow:
    """One catalogue flow. ``raw`` is the whole mapping, as the prompt shows it."""

    id: str
    title: str
    persona: str
    priority: str
    paths: tuple[str, ...]
    raw: Mapping[str, Any]


def _string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) for item in value)
    )


def _flow(entry: object) -> Flow:
    if not isinstance(entry, dict):
        raise FlowsError("a flow entry is not a mapping")
    ident = entry.get("id")
    label = ident if isinstance(ident, str) else repr(ident)
    for key in ("id", "title", "persona", "priority"):
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            raise FlowsError(f"flow {label}: {key} must be a non-empty string")
    for key in ("steps", "expect"):
        if not _string_list(entry.get(key)):
            raise FlowsError(f"flow {label}: {key} must be a non-empty list of text")
    globs: tuple[str, ...] = ()
    if "paths" in entry:
        value = entry["paths"]
        if not _string_list(value):
            raise FlowsError(
                f"flow {label}: paths, when present, must be a non-empty list of globs"
            )
        globs = tuple(value)
        stray = next((g for g in globs if not g.startswith(ui_change.UI_PREFIX)), None)
        if stray is not None:
            raise FlowsError(
                f"flow {label}: path {stray!r} is not under {ui_change.UI_PREFIX}"
            )
    return Flow(
        id=entry["id"],
        title=entry["title"],
        persona=entry["persona"],
        priority=entry["priority"],
        paths=globs,
        raw=dict(entry),
    )


def load_flows(path: Path) -> list[Flow]:
    """Read and validate the catalogue. Any problem raises :class:`FlowsError`."""
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise FlowsError(f"cannot read {path.name}: {error}") from error
    entries = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise FlowsError(f"{path.name} has no top-level 'flows' list")
    loaded: list[Flow] = []
    seen: set[str] = set()
    for entry in entries:
        flow = _flow(entry)
        if flow.id in seen:
            raise FlowsError(f"flow {flow.id} appears twice")
        seen.add(flow.id)
        loaded.append(flow)
    return loaded


def select_flows(
    flows: Sequence[Flow], user_visible: Sequence[str], *, persona: str, limit: int
) -> list[Flow]:
    """The ``limit`` best-matching flows for ``persona``, best first."""
    if limit <= 0:
        return []
    ranked: list[tuple[int, int, str, Flow]] = []
    for flow in flows:
        if flow.persona != persona or not flow.paths:
            continue
        score = sum(1 for path in user_visible if ui_change.matches(path, flow.paths))
        if score:
            ranked.append((-score, PRIORITY_RANK.get(flow.priority, 9), flow.id, flow))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:limit]]


def flow_yaml(flow: Flow) -> str:
    """The flow as YAML, keys in file order, for the prompt to carry verbatim."""
    import yaml

    return yaml.safe_dump(dict(flow.raw), sort_keys=False, allow_unicode=True)
