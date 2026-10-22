# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/flows.py and the ``paths:`` globs in ux_flows.yaml.

A glob that matches nothing is the quiet failure here: the flow is simply never
chosen again, every run stays green, and the regression memory the catalogue
exists for is lost on the first file rename. So the real catalogue is checked
against the files git tracks, and each glob must still name at least one
component rather than only its tests. The ranking is pinned with synthetic
flows, because it is what decides what a fixed budget is spent on.

``lib/idp_common_pkg/tests/unit/test_ux_test_session.py::TestFlowsFile`` keeps
the catalogue's other contract, the fields the interactive skill reads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from repo_files import tracked_paths

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
FLOWS_FILE = SCRIPTS / "ux_flows.yaml"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import flows, ui_change  # noqa: E402


def _tracked_ui_files() -> list[str]:
    tracked = [
        p.relative_to(REPO_ROOT).as_posix() for p in tracked_paths(REPO_ROOT, "*")
    ]
    return [path for path in tracked if path.startswith(ui_change.UI_PREFIX)]


def _flow(
    ident: str,
    *,
    persona: str = "Admin",
    priority: str = "p0",
    paths: tuple[str, ...] = (),
) -> flows.Flow:
    raw = {
        "id": ident,
        "title": f"Flow {ident}",
        "persona": persona,
        "priority": priority,
    }
    return flows.Flow(
        id=ident,
        title=f"Flow {ident}",
        persona=persona,
        priority=priority,
        paths=paths,
        raw=raw,
    )


def _write_catalogue(tmp_path: Path, entry: dict[str, object]) -> Path:
    path = tmp_path / "flows.yaml"
    path.write_text(
        yaml.safe_dump({"flows": [entry]}, sort_keys=False), encoding="utf-8"
    )
    return path


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": "9.9",
        "title": "A flow",
        "persona": "Admin",
        "priority": "p1",
        "steps": ["Open it."],
        "expect": ["It opens."],
    }
    entry.update(overrides)
    return entry


@pytest.mark.unit
def test_the_real_catalogue_loads_and_every_declared_glob_is_under_src_ui() -> None:
    loaded = flows.load_flows(FLOWS_FILE)
    with_paths = [flow for flow in loaded if flow.paths]
    assert len(loaded) >= 5
    assert with_paths, "no flow declares paths:, so the MR selector can choose nothing"
    for flow in with_paths:
        for glob in flow.paths:
            assert glob.startswith("src/ui/"), f"flow {flow.id}: {glob!r}"


@pytest.mark.unit
def test_every_flow_glob_still_matches_a_tracked_ui_file() -> None:
    ui_files = _tracked_ui_files()
    assert len(ui_files) > 100, "git listed almost nothing under src/ui/"
    rotted = [
        f"flow {flow.id}: {glob}"
        for flow in flows.load_flows(FLOWS_FILE)
        for glob in flow.paths
        if not any(ui_change.matches(path, [glob]) for path in ui_files)
    ]
    assert not rotted, (
        "these paths: globs in scripts/ux_flows.yaml match no tracked file under "
        "src/ui/, so the flow can no longer be chosen for an MR (a rename or a "
        f"deletion?): {rotted}"
    )


@pytest.mark.unit
def test_no_flow_glob_matches_only_test_files() -> None:
    ui_files = _tracked_ui_files()
    only_tests = []
    for flow in flows.load_flows(FLOWS_FILE):
        for glob in flow.paths:
            matched = [path for path in ui_files if ui_change.matches(path, [glob])]
            if matched and all(ui_change.is_test_path(path) for path in matched):
                only_tests.append(f"flow {flow.id}: {glob}")
    assert not only_tests, (
        "a test-only change never reaches the selector, so these globs can never "
        f"choose their flow: {only_tests}"
    )


@pytest.mark.unit
def test_a_flow_matching_more_changed_files_ranks_first() -> None:
    one = _flow("1.1", priority="p0", paths=("src/ui/src/a/**",))
    two = _flow("2.2", priority="p2", paths=("src/ui/src/b/**", "src/ui/src/a/X.tsx"))
    chosen = flows.select_flows(
        [one, two],
        ["src/ui/src/a/X.tsx", "src/ui/src/b/Y.tsx"],
        persona="Admin",
        limit=2,
    )
    assert [flow.id for flow in chosen] == ["2.2", "1.1"]


@pytest.mark.unit
def test_equal_scores_go_to_the_higher_priority_then_the_lower_id() -> None:
    globs = ("src/ui/src/a/**",)
    low = _flow("1.1", priority="p2", paths=globs)
    high = _flow("3.3", priority="p0", paths=globs)
    tied = _flow("2.2", priority="p0", paths=globs)
    unknown = _flow("0.1", priority="urgent", paths=globs)
    chosen = flows.select_flows(
        [low, high, tied, unknown], ["src/ui/src/a/X.tsx"], persona="Admin", limit=4
    )
    assert [flow.id for flow in chosen] == ["2.2", "3.3", "1.1", "0.1"]


@pytest.mark.unit
def test_annotator_flows_are_never_chosen_for_an_admin_run() -> None:
    loaded = flows.load_flows(FLOWS_FILE)
    every_glob_target = _tracked_ui_files()
    chosen = flows.select_flows(
        loaded, every_glob_target, persona="Admin", limit=len(loaded)
    )
    assert chosen, "nothing matched at all, so the persona check proved nothing"
    assert {flow.persona for flow in chosen} == {"Admin"}
    assert any(flow.persona == "Annotator" and flow.paths for flow in loaded)


@pytest.mark.unit
def test_the_limit_is_honoured_and_flows_without_paths_are_never_chosen() -> None:
    globs = ("src/ui/src/a/**",)
    candidates = [_flow(f"{n}.1", paths=globs) for n in range(5)]
    candidates.append(_flow("9.1", paths=()))
    user_visible = ["src/ui/src/a/X.tsx"]
    assert (
        len(flows.select_flows(candidates, user_visible, persona="Admin", limit=2)) == 2
    )
    assert flows.select_flows(candidates, user_visible, persona="Admin", limit=0) == []
    every = flows.select_flows(candidates, user_visible, persona="Admin", limit=10)
    assert "9.1" not in [flow.id for flow in every]
    assert len(every) == 5


@pytest.mark.unit
def test_an_empty_paths_list_is_refused_naming_the_flow(tmp_path: Path) -> None:
    path = _write_catalogue(tmp_path, _entry(paths=[]))
    with pytest.raises(flows.FlowsError, match=r"9\.9.*paths"):
        flows.load_flows(path)


@pytest.mark.unit
def test_a_glob_outside_src_ui_is_refused_naming_the_flow(tmp_path: Path) -> None:
    path = _write_catalogue(tmp_path, _entry(paths=["template.yaml"]))
    with pytest.raises(flows.FlowsError, match=r"9\.9.*template\.yaml"):
        flows.load_flows(path)


@pytest.mark.unit
def test_an_unquoted_numeric_id_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "flows.yaml"
    path.write_text(
        "flows:\n  - id: 5.1\n    title: t\n    persona: Admin\n    priority: p0\n"
        "    steps: [s]\n    expect: [e]\n",
        encoding="utf-8",
    )
    with pytest.raises(flows.FlowsError, match="id must be a non-empty string"):
        flows.load_flows(path)


@pytest.mark.unit
def test_missing_steps_and_a_missing_flows_list_are_refused(tmp_path: Path) -> None:
    entry = _entry()
    del entry["steps"]
    with pytest.raises(flows.FlowsError, match=r"9\.9.*steps"):
        flows.load_flows(_write_catalogue(tmp_path, entry))
    bare = tmp_path / "bare.yaml"
    bare.write_text("nothing: here\n", encoding="utf-8")
    with pytest.raises(flows.FlowsError, match="'flows' list"):
        flows.load_flows(bare)


@pytest.mark.unit
def test_a_flow_without_paths_loads_with_an_empty_tuple(tmp_path: Path) -> None:
    (flow,) = flows.load_flows(_write_catalogue(tmp_path, _entry()))
    assert flow.paths == ()
    assert flow.id == "9.9"


@pytest.mark.unit
def test_flow_yaml_round_trips_every_real_flow_id_as_a_string() -> None:
    for flow in flows.load_flows(FLOWS_FILE):
        parsed = yaml.safe_load(flows.flow_yaml(flow))
        assert parsed["id"] == flow.id
        assert isinstance(parsed["id"], str)
        assert list(parsed)[:4] == ["id", "title", "persona", "priority"]


@pytest.mark.unit
def test_importing_flows_does_not_import_yaml() -> None:
    """The precheck runs on a bare python3, so yaml must stay a lazy import."""
    import ast

    tree = ast.parse((SCRIPTS / "ux_runner" / "flows.py").read_text(encoding="utf-8"))
    module_level = [
        alias.name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert "yaml" not in module_level
