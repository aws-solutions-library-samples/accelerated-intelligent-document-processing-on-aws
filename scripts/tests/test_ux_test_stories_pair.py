# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for ``story_pair.py``: a pair session's files, judged as any run is.

``run_in_docker.py stories --pair`` runs the preparer inside the browser container,
then writes the session's MCP configuration from the line it prints. What is pinned
here is that everything the session and its one server read is consistent with the
frozen modules that read it:

* the start record is one ``ux_story_mcp`` accepts, and the run record one
  ``story_verdict`` reads, naming the same flow, series and journal;
* the prompt is ``story_prompt.flow_prompt``'s, byte for byte, so a pair session
  is told exactly what an unattended one is;
* the server definition is ``agent.story_server``'s, under ``env -i``;
* a session served by the real story server from those files is judged from its
  journal, end to end.

The docs index is built from a one-page tree with the real ``docs-site/setup.sh``,
so nothing reads the checkout's whole corpus.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ux_story_mcp  # noqa: E402
from ux_runner import (  # noqa: E402
    agent,
    paths,
    stories,
    story_pair,
    story_prompt,
    story_relay,
    story_verdict,
    tool_profiles,
)

HEAD = "a7" * 20
STORY = "route-folder-to-profile"
PERSONA = "platform-admin"
KEY = f"{STORY}@{PERSONA}"
VERSIONS = {
    "chrome": "155.0.8059.84",
    "chrome_platform": "linux-arm64",
    "node": "v22.14.0",
    "chrome_devtools_mcp": "1.10.1",
    "claude_code": "2.1.281",
}


@pytest.fixture
def docs_root(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "docs").mkdir(parents=True)
    (root / "images").mkdir()
    (root / "docs-site").mkdir()
    shutil.copyfile(
        REPO_ROOT / "docs-site" / "setup.sh", root / "docs-site" / "setup.sh"
    )
    (root / "docs" / "web-ui.md").write_text(
        "# Web UI\n\n## Upload documents\n\nUse the upload page to send a document.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def toolchain(tmp_path: Path) -> paths.Toolchain:
    prefix = tmp_path / "toolchain"
    prefix.mkdir()
    chosen = paths.Toolchain(prefix)
    chosen.versions_file.write_text(json.dumps(VERSIONS), encoding="utf-8")
    return chosen


def _prepare(
    tmp_path: Path, docs_root: Path, toolchain: paths.Toolchain, **overrides: Any
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "story_id": STORY,
        "persona_id": PERSONA,
        "run": tmp_path / "run",
        "stack": "idp-ux-nightly",
        "harness": HEAD,
        "docs_dirty": False,
        "home": tmp_path / "pair-home",
        "repo": docs_root,
        "toolchain": toolchain,
        "python": sys.executable,
    }
    arguments.update(overrides)
    (tmp_path / "run").mkdir(exist_ok=True)
    return story_pair.prepare(**arguments)


def _series(catalogue: stories.Catalogue, versions: dict[str, str]) -> str:
    story = catalogue.story(STORY)
    texts = story_prompt.read_texts()
    return stories.series_hash(
        stories.story_hash(catalogue, story),
        PERSONA,
        stories.SeriesInputs(
            model="unknown",
            effort="unknown",
            prompt_revision=story_prompt.STORY_PROMPT_REVISION,
            judgement_revision=story_verdict.JUDGEMENT_REVISION,
            relay_revision=story_relay.RELAY_REVISION,
            claude_code=versions.get("claude_code", "unknown"),
            chrome_devtools_mcp=versions.get("chrome_devtools_mcp", "unknown"),
            chrome=versions.get("chrome", "unknown"),
            brief_sha256=texts.brief_sha256,
            ci_contract_sha256=texts.ci_contract_sha256,
        ),
    )


@pytest.mark.unit
def test_prepare_writes_the_files_the_session_reads_and_reports_its_server(
    tmp_path: Path, docs_root: Path, toolchain: paths.Toolchain
) -> None:
    reply = _prepare(tmp_path, docs_root, toolchain)
    run = tmp_path / "run"
    catalogue = stories.load_catalogue()
    budget = stories.budget(catalogue.limits, catalogue.story(STORY))
    assert reply["key"] == KEY and reply["budget"] == budget
    assert reply["allowed_tools"] == list(tool_profiles.STORY_ALLOWED_TOOLS)
    assert reply["prompt"] == str(run / story_pair.PROMPT_FILE)
    flow_dir = run / story_pair.ARTIFACTS / "flows" / KEY
    assert not flow_dir.exists(), "the server creates the flow directory itself"
    expected = agent.story_server(
        toolchain=toolchain,
        story_adapter=story_pair.STORY_SERVER_SCRIPT,
        index=run / story_pair.PAIR_DIR / story_pair.INDEX_FILE,
        out=flow_dir,
        start=run / story_pair.PAIR_DIR / story_pair.START_FILE,
        budget=budget,
        python=sys.executable,
        home=tmp_path / "pair-home",
        search_path=story_pair.search_path(),
    )[tool_profiles.STORY_SERVER]
    assert reply["server"] == expected
    assert reply["server"]["command"] == "/usr/bin/env"
    assert reply["server"]["args"][0] == "-i" and reply["server"]["env"] == {}
    assert ["--budget", str(budget)] == reply["server"]["args"][-4:-2]
    if hasattr(os, "getuid"):
        assert (tmp_path / "pair-home").stat().st_mode & 0o777 == 0o700


@pytest.mark.unit
def test_the_prompt_is_the_one_every_mode_gets(
    tmp_path: Path, docs_root: Path, toolchain: paths.Toolchain
) -> None:
    _prepare(tmp_path, docs_root, toolchain)
    written = (tmp_path / "run" / story_pair.PROMPT_FILE).read_text(encoding="utf-8")
    catalogue = stories.load_catalogue()
    assert written == story_prompt.flow_prompt(
        catalogue, stories.FlowKey(STORY, PERSONA), texts=story_prompt.read_texts()
    )


@pytest.mark.unit
def test_the_start_and_run_records_name_the_same_flow_and_series(
    tmp_path: Path, docs_root: Path, toolchain: paths.Toolchain
) -> None:
    _prepare(tmp_path, docs_root, toolchain)
    run = tmp_path / "run"
    start = ux_story_mcp.start_record(run / story_pair.PAIR_DIR / story_pair.START_FILE)
    catalogue = stories.load_catalogue()
    assert start["series"] == _series(catalogue, VERSIONS)
    assert (start["mode"], start["model"], start["effort"]) == (
        "pair",
        "unknown",
        "unknown",
    )
    assert start["docs_ref"] == "worktree" and start["harness"] == HEAD
    record = json.loads(
        (run / story_pair.ARTIFACTS / story_verdict.RUN_FILE).read_text()
    )
    (context,) = story_verdict.run_record(record)
    assert (context.story, context.persona, context.group) == (STORY, PERSONA, "Admin")
    assert (context.expectation, context.kind) == (start["expectation"], start["kind"])
    assert context.series == start["series"]
    assert context.journal == f"flows/{KEY}/journal.jsonl"
    assert context.audit_state == "not_run" and context.session is None
    assert record["mode"] == "pair" and record["shard"] is None
    assert (record["harness_sha"], record["harness_dirty"]) == (HEAD, False)
    assert record["docs"] == {
        "ref": None,
        "corpus_hash": start["corpus_hash"],
        "source": "worktree",
        "dirty": False,
    }
    assert record["ui_source"] == {"kind": "deployed"}
    assert record["versions"] == VERSIONS
    assert record["stack"]["review_stack"] is None


@pytest.mark.unit
def test_a_dirty_tree_a_local_build_and_no_toolchain_are_recorded_as_such(
    tmp_path: Path, docs_root: Path
) -> None:
    bare = paths.Toolchain(tmp_path / "no-toolchain")
    _prepare(
        tmp_path,
        docs_root,
        bare,
        harness=f"{HEAD}+dirty",
        docs_dirty=True,
        build=True,
    )
    run = tmp_path / "run"
    start = ux_story_mcp.start_record(run / story_pair.PAIR_DIR / story_pair.START_FILE)
    assert start["docs_ref"] == "worktree+dirty"
    assert start["series"] == _series(stories.load_catalogue(), {})
    record = json.loads(
        (run / story_pair.ARTIFACTS / story_verdict.RUN_FILE).read_text()
    )
    assert (record["harness_sha"], record["harness_dirty"]) == (HEAD, True)
    assert record["docs"]["dirty"] is True
    assert record["ui_source"]["kind"] == "build"
    assert record["versions"] == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"story_id": "no-such-story"}, "no story 'no-such-story'"),
        ({"persona_id": "team-lead"}, "not 'team-lead'"),
        ({"harness": "main"}, "--harness"),
        ({"stack": "my stack"}, "--stack"),
    ],
)
def test_prepare_refuses_what_it_cannot_record(
    tmp_path: Path,
    docs_root: Path,
    toolchain: paths.Toolchain,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    with pytest.raises(story_pair.PairError, match=reason):
        _prepare(tmp_path, docs_root, toolchain, **overrides)


@pytest.mark.unit
def test_prepare_writes_no_file_over_another_runs(
    tmp_path: Path, docs_root: Path, toolchain: paths.Toolchain
) -> None:
    _prepare(tmp_path, docs_root, toolchain)
    with pytest.raises(story_pair.PairError, match="already exists"):
        _prepare(tmp_path, docs_root, toolchain)
    flow_dir = tmp_path / "again" / story_pair.ARTIFACTS / "flows" / KEY
    flow_dir.mkdir(parents=True)
    with pytest.raises(story_pair.PairError, match="fresh one"):
        _prepare(tmp_path, docs_root, toolchain, run=tmp_path / "again")


def _rpc(identifier: int, method: str, params: dict[str, Any]) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
    )


@pytest.mark.unit
def test_a_session_served_from_the_pair_files_is_judged_from_its_journal(
    tmp_path: Path, docs_root: Path, toolchain: paths.Toolchain
) -> None:
    """The real server, in its own-tools mode, from the files the preparer wrote:
    the journal it writes is the one the run record names, and the judge reads it."""
    reply = _prepare(tmp_path, docs_root, toolchain)
    args = reply["server"]["args"]
    served = [
        sys.executable,
        "-I",
        "-B",
        str(story_pair.STORY_SERVER_SCRIPT),
        *args[args.index("--index") : args.index("--budget")],
    ]
    messages = [
        _rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        _rpc(
            2,
            "tools/call",
            {
                "name": "report_done",
                "arguments": {
                    "evidence": "The mapping list shows the folder.",
                    "stopped_at_commit_point": True,
                },
            },
        ),
    ]
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        served,
        input="\n".join(messages) + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
    )
    assert done.returncode == 0, done.stderr[-600:]
    answers = [json.loads(line) for line in done.stdout.splitlines()]
    assert answers[-1]["result"]["isError"] is False
    run_dir = tmp_path / "run" / story_pair.ARTIFACTS
    results, found = story_verdict.judge_run(run_dir)
    (result,) = results
    assert (result.key, result.verdict, result.met_expectation) == (KEY, "done", True)
    assert "stopped_at_commit_point" in result.flags
    assert not found
    table = story_verdict.summary_table(run_dir, results, found)
    assert f"\n{KEY}\t1\tdone\tsingle\tyes\t" in table
    orchestrator = Path(story_pair.__file__).resolve().parents[1] / "sdlc"
    judge = [sys.executable, "-B", str(orchestrator / "ai_ux_test_stories.py")]
    environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    printed = []
    for extra in (["--summary"], ["--check"]):
        judged = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*judge, "--judge", str(run_dir), *extra],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environ,
        )
        assert judged.returncode == 0, judged.stdout[-600:] + judged.stderr[-600:]
        printed.append(judged.stdout)
    assert f"\n{KEY}\t1\tdone\tsingle\tyes\t" in printed[0]
    assert printed[1] == ""
    assert (run_dir / story_verdict.RESULTS_FILE).is_file()
    assert not (run_dir / "section.md").exists()


@pytest.mark.unit
def test_main_prints_one_json_line_or_one_failed_line(
    tmp_path: Path,
    docs_root: Path,
    toolchain: paths.Toolchain,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(story_pair, "REPO_ROOT", docs_root)
    run = tmp_path / "run"
    run.mkdir()
    argv = [
        "prepare",
        "--story",
        STORY,
        "--as",
        PERSONA,
        "--run",
        str(run),
        "--stack",
        "idp-ux-nightly",
        "--harness",
        "unknown",
        "--docs-dirty",
        "0",
        "--home",
        str(tmp_path / "home"),
        "--toolchain-prefix",
        str(toolchain.prefix),
    ]
    assert story_pair.main(argv) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["key"] == KEY
    record = json.loads(
        (run / story_pair.ARTIFACTS / story_verdict.RUN_FILE).read_text()
    )
    assert (record["harness_sha"], record["harness_dirty"]) == ("unknown", None)
    assert story_pair.main([*argv[:2], "no-such-story", *argv[3:]]) == 1
    assert capsys.readouterr().out.startswith("FAILED: ")


@pytest.mark.unit
def test_the_preparer_names_the_servers_journal_and_never_imports_the_server() -> None:
    assert story_pair.JOURNAL_FILE == ux_story_mcp.JOURNAL_FILE
    assert story_pair.STORY_SERVER_SCRIPT == SCRIPTS / "ux_story_mcp.py"
    source = (SCRIPTS / "ux_runner" / "story_pair.py").read_text(encoding="utf-8")
    assert "import ux_story_mcp" not in source


@pytest.mark.unit
def test_search_path_puts_nodes_directory_first_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = tmp_path / "node-bin" / "node"
    node.parent.mkdir()
    node.write_text("")
    monkeypatch.setattr(story_pair.shutil, "which", lambda name: str(node))
    folder = str(node.parent.resolve())
    assert (
        story_pair.search_path("/usr/bin:/bin") == f"{folder}{os.pathsep}/usr/bin:/bin"
    )
    assert story_pair.search_path(f"{folder}:/bin") == f"{folder}:/bin"
    monkeypatch.setattr(story_pair.shutil, "which", lambda name: None)
    assert story_pair.search_path("/usr/bin") == "/usr/bin"


@pytest.mark.unit
def test_a_catalogue_or_docs_tree_that_cannot_be_read_is_named(
    tmp_path: Path, docs_root: Path, toolchain: paths.Toolchain
) -> None:
    broken = tmp_path / "ux_stories.toml"
    broken.write_text("version = 2\n")
    with pytest.raises(story_pair.PairError, match="is required"):
        _prepare(tmp_path, docs_root, toolchain, catalogue_path=broken)
    with pytest.raises(story_pair.PairError, match="cannot be read"):
        _prepare(
            tmp_path, docs_root, toolchain, catalogue_path=tmp_path / "absent.toml"
        )
    with pytest.raises(story_pair.PairError, match="not a regular file"):
        _prepare(tmp_path, docs_root, toolchain, catalogue_path=tmp_path)
    (docs_root / "docs-site" / "setup.sh").unlink()
    with pytest.raises(story_pair.PairError, match="cannot be indexed"):
        _prepare(tmp_path, docs_root, toolchain)


@pytest.mark.unit
def test_a_prompt_the_builder_refuses_is_named(
    tmp_path: Path,
    docs_root: Path,
    toolchain: paths.Toolchain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(**kwargs: Any) -> str:
        raise story_prompt.StoryPromptError("the brief is empty")

    monkeypatch.setattr(story_prompt, "flow_prompt", lambda *a, **k: refuse())
    with pytest.raises(story_pair.PairError, match="the brief is empty"):
        _prepare(tmp_path, docs_root, toolchain)


@pytest.mark.unit
def test_a_file_is_never_written_where_its_directory_is_missing(tmp_path: Path) -> None:
    with pytest.raises(story_pair.PairError, match="cannot be written"):
        story_pair._write_new(tmp_path / "absent" / "start.json", b"{}")
    assert story_pair.toolchain_versions(tmp_path / "absent.json") == {}
    (tmp_path / "list.json").write_text("[1]")
    assert story_pair.toolchain_versions(tmp_path / "list.json") == {}
