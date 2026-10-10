# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every caller of ``resolve_config_assignment`` injects the same seams.

``resolve_config_assignment`` is a pure function with three injected callables —
``active_profile``, ``published_revision`` and ``profile_exists``. It is
deterministic, so two callers asking about the same object can only disagree if
their *inputs* differ, and the inputs that differ are the seams. Four artifacts
call it, and three of them answer a question the fourth will answer again a moment
later about the same S3 object:

* ``upload_resolver`` decides whether to mint a presigned POST,
* the configuration resolver's dry run tells the UI what will happen,
* ``queue_sender`` does it for real at ingest,
* the reprocess resolver does it for a re-run.

**The resolver's own suite cannot see any of this**, by construction: it tests the
function given its arguments, and the defect is in which arguments get passed. That
is why this gate parses the call sites instead.

Two divergences this is written against, both measured on a tree where
``upload_resolver`` passed only ``mappings``:

* **No ``published_revision``** made an unpinned request's effective revision
  ``None``, which then "disagreed" with a mapping pinned to the revision that
  profile had in fact published. A ``reject`` mapping refused an upload the preview
  had just said was fine and that ingest would have accepted — a 400 on a
  legitimate upload, reachable with no adversary.
* **No ``active_profile``** left the resolved profile ``None`` whenever no mapping
  matched and no profile was named, and a ``None`` profile skipped the scope guard.
  That is the single most common upload shape, so the scope check the function
  exists for did not run on it: the control was reported closed with its widest
  route open.

``queue_sender`` is the reference, because it is the path that actually decides what
a document is processed under.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from repo_files import tracked_paths

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)

#: The three injected callables. A caller supplying a strict subset is answering a
#: different question from the one ingest will answer.
SEAMS = ("active_profile", "published_revision", "profile_exists")

#: Every artifact that resolves a configuration assignment, and why it is here.
CALL_SITES = {
    "src/lambda/queue_sender/index.py": "ingest — the reference answer",
    "nested/api-resolvers/src/lambda/upload_resolver/index.py": (
        "decides whether to mint a presigned POST for the same object ingest will "
        "then resolve"
    ),
    "nested/api-resolvers/src/lambda/configuration_resolver/index.py": (
        "the dry run the upload panel shows the user before they upload"
    ),
    "nested/api-resolvers/src/lambda/reprocess_document_resolver/index.py": (
        "re-resolves on a re-run"
    ),
}


def _resolve_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_config_assignment"
    ]


@pytest.mark.unit
def test_the_call_site_inventory_is_complete():
    """A derived gate that silently finds nothing is the failure mode to avoid.

    Discovered from the tree rather than trusted, so a fifth caller added anywhere
    fails here instead of quietly answering a different question.
    """
    # Discovered through `repo_files.tracked_paths`, which asks git. An `rglob`
    # here would also walk `scratch/` and `.claude/worktrees/`, both gitignored and
    # both routinely holding whole checkouts of this repository -- a stale copy
    # parses exactly like the real thing, so the gate would fail on code that ships
    # nothing. See scripts/tests/test_repo_walk_guards_prune_local_work.py.
    found = {
        str(p.relative_to(REPO_ROOT))
        for p in tracked_paths(REPO_ROOT, "*.py")
        if "resolve_config_assignment("
        in p.read_text(encoding="utf-8", errors="ignore")
        and not p.name.startswith("test_")
        and p.name != "prefix_mappings.py"
    }
    unregistered = found - set(CALL_SITES)
    assert not unregistered, (
        "these call sites resolve a configuration assignment and are not registered "
        f"in CALL_SITES: {sorted(unregistered)}. Add each one, and make sure it "
        f"injects every seam in {SEAMS} — see this module's docstring."
    )
    missing = set(CALL_SITES) - found
    assert not missing, f"registered call sites that no longer exist: {sorted(missing)}"


@pytest.mark.unit
@pytest.mark.parametrize("rel", sorted(CALL_SITES))
def test_every_call_site_injects_every_seam(rel):
    calls = _resolve_calls(REPO_ROOT / rel)
    assert calls, f"{rel} is registered as a call site but calls nothing"
    for call in calls:
        supplied = {kw.arg for kw in call.keywords if kw.arg}
        missing = [seam for seam in SEAMS if seam not in supplied]
        assert not missing, (
            f"{rel}:{call.lineno} resolves a configuration assignment without "
            f"{missing}. {CALL_SITES[rel]}, so a seam it omits is a question it "
            "answers differently from the ingest path. See this module's docstring "
            "for the two divergences that produced this gate."
        )
