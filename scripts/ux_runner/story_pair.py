#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One first-time-user story flow, made ready for a pair session you start.

``run_in_docker.py stories --pair`` runs this inside the browser container it has
started and signed in, through ``docker exec``, and then prints the ``claude``
command for a session of the developer's own. Everything that command and its one
MCP server need is written here, under ``--run`` (the run directory, ``/out``):

* ``pair/index.json``, the docs index of the checkout's working tree, which the
  ``ux-story`` server searches and reads;
* ``pair/start.json``, the server's start record, mode ``pair``;
* ``ux-test-stories/story-run.json``, the run record with the one flow, mode
  ``pair``, whose journal the server writes to
  ``ux-test-stories/flows/<story@persona>/journal.jsonl``, so the run is judged
  as any other is (``story_verdict.py judge``);
* ``pair-prompt.md``, the flow's prompt exactly as ``story_prompt.flow_prompt``
  builds it for every mode: the brief, the unattended contract, the
  untrusted-input paragraph, the persona, the task and its bounds. The command
  passes it with ``--append-system-prompt-file``, because a session with no
  built-in tools has no Read or Skill tool to open the skill with.

The server checks the start record when it starts. Of what is outside
``scripts/ux_runner`` this imports only what the MR UX test's harness carries
(``check_markdown_links``, through the docs index), because that harness pins this
directory whole, so the server is named by its path and never imported.

It creates the server's private ``HOME`` and prints one JSON line: the flow's key
and budget, the prompt's path, the server definition ``agent.story_server`` gives
(``/usr/bin/env -i`` and an empty ``env`` map, so the server inherits nothing of
the container's environment, which holds the personas and ``UX_TEST_UI_ENV``) and
the STORY profile's granted tools. The host checks that line before it writes ``pair-mcp.json`` and
prints anything.

A pair session has no model or effort the harness chose, so both are recorded as
``unknown``, and it is never compared or posted. Its harness label and whether the
working tree's docs differ from ``HEAD`` come from the host's git, because a
checkout mounted into a container need not be a repository there.

Usage (inside the image; ``run_in_docker.py stories --pair`` builds this)::

    python3 -I -B /repo/scripts/ux_runner/story_pair.py prepare --story ID \\
        --as PERSONA --run /out --stack NAME --harness SHA[+dirty]|unknown \\
        --docs-dirty 0|1 --home /tmp/ux-pair [--build]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0] or ".").resolve() == _HERE:
    sys.path.pop(0)
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from ux_runner import (  # noqa: E402
    agent,
    docs_index,
    paths,
    stories,
    story_prompt,
    story_relay,
    story_verdict,
    tool_profiles,
)

PAIR_DIR = "pair"
ARTIFACTS = "ux-test-stories"
PROMPT_FILE = "pair-prompt.md"
INDEX_FILE = "index.json"
START_FILE = "start.json"
JOURNAL_FILE = "journal.jsonl"
MODE = "pair"
UNKNOWN = "unknown"
STORY_SERVER_SCRIPT = _HERE.parent / "ux_story_mcp.py"
REPO_ROOT = _HERE.parents[1]
HARNESS_RE = re.compile(r"^(?:[0-9a-f]{40}(?:\+dirty)?|unknown)$")
STACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
VERSIONS_MAX_BYTES = 64 * 1024
CATALOGUE_MAX_BYTES = 4 * 1024 * 1024
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


class PairError(Exception):
    """The flow cannot be made ready for a pair session; the message says why."""


def _read_regular(path: Path, limit: int, what: str) -> bytes:
    """A regular file of at most ``limit`` bytes, never through a symbolic link."""
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise PairError(
            f"{what} cannot be read ({error.strerror or type(error).__name__})"
        ) from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise PairError(f"{what} is not a regular file")
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise PairError(f"{what} is larger than {limit} bytes")
    return data


def _write_new(path: Path, data: bytes, mode: int = 0o644) -> Path:
    """Create ``path`` with ``data``, refusing an existing file or a link there."""
    try:
        descriptor = os.open(path, _CREATE_FLAGS, mode)
    except FileExistsError:
        raise PairError(
            f"{path} already exists; a pair run writes a fresh one"
        ) from None
    except OSError as error:
        raise PairError(
            f"{path} cannot be written ({error.strerror or type(error).__name__})"
        ) from None
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return path


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def toolchain_versions(versions_file: Path) -> dict[str, Any]:
    """``VERSIONS.json`` as ``install.sh`` wrote it, or ``{}`` when there is none."""
    try:
        data = json.loads(
            _read_regular(versions_file, VERSIONS_MAX_BYTES, "VERSIONS.json")
        )
    except (PairError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _version(versions: Mapping[str, Any], key: str) -> str:
    value = versions.get(key)
    return value if isinstance(value, str) and value.strip() else UNKNOWN


def search_path(base: str = agent.DEFAULT_SEARCH_PATH) -> str:
    """``base`` with the directory holding ``node`` in front when it is not in it."""
    node = shutil.which("node")
    if node is None:
        return base
    folder = str(Path(node).resolve().parent)
    return base if folder in base.split(os.pathsep) else f"{folder}{os.pathsep}{base}"


def _flow(
    catalogue: stories.Catalogue, story_id: str, persona_id: str
) -> tuple[stories.FlowKey, stories.Story, stories.Persona]:
    try:
        story = catalogue.story(story_id)
    except KeyError:
        raise PairError(f"the catalogue has no story {story_id!r}") from None
    if persona_id not in story.personas:
        raise PairError(
            f"{story.id} runs as {', '.join(story.personas)}, not {persona_id!r}"
        )
    return stories.FlowKey(story.id, persona_id), story, catalogue.persona(persona_id)


def prepare(
    *,
    story_id: str,
    persona_id: str,
    run: Path,
    stack: str,
    harness: str,
    docs_dirty: bool,
    home: Path,
    build: bool = False,
    catalogue_path: Path = stories.CATALOGUE_PATH,
    repo: Path = REPO_ROOT,
    toolchain: paths.Toolchain | None = None,
    python: str = sys.executable,
) -> dict[str, Any]:
    """Write the pair session's files under ``run``; the line the host reads.

    ``harness`` is the host's label for the checkout (``<sha>``, ``<sha>+dirty`` or
    ``unknown``) and ``docs_dirty`` whether its ``docs/`` differ from ``HEAD``.
    Raises :class:`PairError` naming what is missing or wrong; nothing it writes is
    ever written over.
    """
    if not HARNESS_RE.match(harness):
        raise PairError(f"--harness {harness!r} is not <sha>, <sha>+dirty or unknown")
    if not STACK_RE.match(stack):
        raise PairError(f"--stack {stack!r} is not a CloudFormation stack name")
    try:
        catalogue_bytes = _read_regular(
            catalogue_path, CATALOGUE_MAX_BYTES, catalogue_path.name
        )
        catalogue = stories.load_catalogue(catalogue_path)
    except stories.CatalogueError as error:
        raise PairError(str(error)) from None
    key, story, persona = _flow(catalogue, story_id, persona_id)
    flow_dir = run / ARTIFACTS / "flows" / str(key)
    if flow_dir.exists():
        raise PairError(f"{flow_dir} already exists; a pair run starts in a fresh one")
    try:
        texts = story_prompt.read_texts()
        prompt = story_prompt.flow_prompt(catalogue, key, texts=texts)
    except (story_prompt.StoryPromptError, OSError) as error:
        raise PairError(f"the flow's prompt cannot be built: {error}") from None
    budget = stories.budget(catalogue.limits, story)
    chosen = paths.toolchain() if toolchain is None else toolchain
    versions = toolchain_versions(chosen.versions_file)
    story_hash = stories.story_hash(catalogue, story)
    series = stories.series_hash(
        story_hash,
        persona.id,
        stories.SeriesInputs(
            model=UNKNOWN,
            effort=UNKNOWN,
            prompt_revision=story_prompt.STORY_PROMPT_REVISION,
            judgement_revision=story_verdict.JUDGEMENT_REVISION,
            relay_revision=story_relay.RELAY_REVISION,
            claude_code=_version(versions, "claude_code"),
            chrome_devtools_mcp=_version(versions, "chrome_devtools_mcp"),
            chrome=_version(versions, "chrome"),
            brief_sha256=texts.brief_sha256,
            ci_contract_sha256=texts.ci_contract_sha256,
        ),
    )
    try:
        index = docs_index.build(
            repo,
            source=docs_index.DocsSource(kind="worktree", ref=None, dirty=docs_dirty),
        )
        pair_dir = run / PAIR_DIR
        pair_dir.mkdir(parents=True, exist_ok=True)
        index_path = docs_index.save(index, pair_dir / INDEX_FILE)
    except docs_index.DocsIndexError as error:
        raise PairError(f"the documentation cannot be indexed: {error}") from None
    start = {
        "story": story.id,
        "story_hash": story_hash,
        "series": series,
        "persona": persona.id,
        "group": persona.group,
        "expectation": story.expectation_for(persona.id),
        "kind": story.kind,
        "attempt": 1,
        "docs_ref": "worktree+dirty" if docs_dirty else "worktree",
        "corpus_hash": index.corpus_hash,
        "harness": harness,
        "model": UNKNOWN,
        "effort": UNKNOWN,
        "mode": MODE,
    }
    start_path = _write_new(pair_dir / START_FILE, _json_bytes(start))
    context = story_verdict.FlowContext(
        key=str(key),
        story=story.id,
        persona=persona.id,
        group=persona.group,
        title=story.title,
        expectation=story.expectation_for(persona.id),
        kind=story.kind,
        documented_steps=story.documented_steps,
        needs=story.needs,
        declared_docs=story.match.docs,
        series=series,
        budget=budget,
        journal=f"{story_verdict.FLOWS_DIR}/{key}/{JOURNAL_FILE}",
    )
    sha, _, dirty = harness.partition("+")
    record = {
        "schema": story_verdict.RUN_SCHEMA,
        "version": story_verdict.RUN_VERSION,
        "mode": MODE,
        "shard": None,
        "harness_sha": sha,
        "harness_dirty": None if sha == UNKNOWN else bool(dirty),
        "catalogue_sha256": hashlib.sha256(catalogue_bytes).hexdigest(),
        "prompt_revision": story_prompt.STORY_PROMPT_REVISION,
        "judgement_revision": story_verdict.JUDGEMENT_REVISION,
        "relay_revision": story_relay.RELAY_REVISION,
        "model": UNKNOWN,
        "effort": UNKNOWN,
        "region": None,
        "versions": versions,
        "stack": {"name": stack, "version": None, "build": None, "review_stack": None},
        "stack_state": None,
        "stack_changed_mid_run": False,
        "ui_source": (
            {"kind": "build", "head_sha": None, "src_ui_tree": None}
            if build
            else {"kind": "deployed"}
        ),
        "docs": {
            "ref": None,
            "corpus_hash": index.corpus_hash,
            "source": "worktree",
            "dirty": docs_dirty,
        },
        "limits": asdict(catalogue.limits),
        "flows": [context.to_json()],
        "credential_scope": {"claude": None, "render": None},
        "video_withheld": None,
        "spend_usd": 0.0,
    }
    artifacts = run / ARTIFACTS
    artifacts.mkdir(parents=True, exist_ok=True)
    _write_new(artifacts / story_verdict.RUN_FILE, _json_bytes(record))
    prompt_path = _write_new(run / PROMPT_FILE, prompt.encode("utf-8"))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    server = agent.story_server(
        toolchain=chosen,
        story_adapter=STORY_SERVER_SCRIPT,
        index=index_path,
        out=flow_dir,
        start=start_path,
        budget=budget,
        python=python,
        home=home,
        search_path=search_path(),
    )[tool_profiles.STORY_SERVER]
    return {
        "key": str(key),
        "budget": budget,
        "prompt": str(prompt_path),
        "server": server,
        "allowed_tools": list(tool_profiles.STORY_ALLOWED_TOOLS),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="story_pair.py",
        description=(__doc__ or "").split("\n\n", 1)[0],
    )
    commands = parser.add_subparsers(dest="command", required=True)
    making = commands.add_parser("prepare", help="write a pair run's files")
    making.add_argument("--story", required=True, help="the story's id")
    making.add_argument(
        "--as", dest="persona", required=True, help="the persona it runs as"
    )
    making.add_argument("--run", type=Path, required=True, help="the run directory")
    making.add_argument("--stack", required=True, help="the stack the browser shows")
    making.add_argument(
        "--harness", required=True, help="the host's <sha>, <sha>+dirty or unknown"
    )
    making.add_argument(
        "--docs-dirty",
        choices=("0", "1"),
        required=True,
        help="1 when the checkout's docs differ from HEAD",
    )
    making.add_argument(
        "--home", type=Path, required=True, help="the story server's private HOME"
    )
    making.add_argument(
        "--build", action="store_true", help="the browser serves a local UI build"
    )
    making.add_argument(
        "--catalogue",
        type=Path,
        default=stories.CATALOGUE_PATH,
        help="the catalogue (default: scripts/ux_stories.toml)",
    )
    making.add_argument(
        "--toolchain-prefix",
        help=f"the toolchain prefix (default: ${paths.PREFIX_ENV} or "
        f"{paths.DEFAULT_PREFIX})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Prepare the flow and print its line; 1 with a ``FAILED:`` line otherwise."""
    args = _parser().parse_args(argv)
    try:
        reply = prepare(
            story_id=args.story,
            persona_id=args.persona,
            run=args.run,
            stack=args.stack,
            harness=args.harness,
            docs_dirty=args.docs_dirty == "1",
            home=args.home,
            build=args.build,
            catalogue_path=args.catalogue,
            repo=REPO_ROOT,
            toolchain=paths.toolchain(args.toolchain_prefix),
        )
    except PairError as error:
        print(f"FAILED: {error}", flush=True)
        return 1
    print(json.dumps(reply, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
