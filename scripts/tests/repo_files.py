# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Repo-wide file discovery for the gates in this directory: ask git, do not walk.

Several gates here assert a property over *every* file in the tree — every
template declares an encrypted log group, every ``normalize_event`` consumer
catches the refusal, every Lambda-declaring template is registered. Each needs a
list of candidate files, and how that list is built decides what the gate can
prove.

Walking the filesystem builds it from whatever the machine happens to have on
disk, which is why every walking gate carries a prune set naming the gitignored
directories that hold whole copies of this repository — ``scratch/`` and
``.claude/worktrees/`` (see ``test_repo_walk_guards_prune_local_work.py``, which
asserts the names are there). Pruning is necessary: 157 of the 158 failures that
opened the 0.6.9 release validation were agent worktrees under ``.claude/``.

It is also not sufficient, and this module exists because of the direction a prune
set fails in. A prune set matched against ``path.parts`` matches the directory name
anywhere in the **absolute** path, including *above* the repository root. An agent
worktree is a real ``git worktree`` checkout at
``<main checkout>/.claude/worktrees/agent-*``, so from inside one, every file's
absolute path contains ``.claude`` and the prune set discards the entire checkout.
Discovery returns zero files, and the gate then either trips its own
"discovery is broken, so this proves nothing" self-guard — which is what
``test_api_adapter_identity_provenance.py`` and ``test_log_group_encryption.py``
did, in a way that cost time on every task in a batch — or, with no self-guard,
passes **vacuously**, which is the worse outcome and is what
``test_lambda_log_groups.py``'s registration sweep did.

Asking git removes the whole question. ``--cached --others --exclude-standard``
yields exactly the files that are, or could be, committed: ``.gitignore`` is
honoured, so build output (``.aws-sam/``, ``node_modules/``, ``dist/``), vendored
library copies and local work (``scratch/``, ``.claude/worktrees/``) are excluded
by the same mechanism CI uses, and a brand-new template is gated before it is
``git add``ed. Crucially the paths are resolved *relative to the checkout*, so a
checkout's own location cannot exclude it. This is the convention
``scripts/discover_templates.sh`` already uses for ``make cfn-lint`` and
``make check-arn-partitions``, and that ``test_alarm_description_length.py`` and
``test_scope_lookup_fail_closed.py`` adopted directly.

The fallback matters for the gates' own meta-tests. Each one drives its discovery
against a synthetic tree in ``tmp_path``, which is not a checkout at all, so
``tracked_paths`` walks instead — with the same prune list, matched against the
**repo-relative** path. ``git rev-parse --show-toplevel`` has to name ``root``
itself for the git path to be taken: a directory *inside* a checkout falls back to
the walk, so a probe written under the repository is still found rather than
silently missing from ``ls-files`` output rooted elsewhere.

Not converted here: the gates that already match their prune set against the
repo-relative path (``test_asl_placeholder_substitution.py``,
``test_well_architected_doc.py``), the one that prunes during an ``os.walk``
descent from the repository root (``test_iam_privilege_escalation.py``), and the
one that already asks git (``test_classification_prompt_copies_in_sync.py``).
None of those has the defect above; churning them would be a behaviour change in
four working gates for no gain.
"""

from __future__ import annotations

import fnmatch
import functools
import os
import subprocess
from pathlib import Path

#: Directories pruned by the non-git fallback. Every one of these is gitignored, so
#: on the git path they are excluded by ``--exclude-standard`` and this set is never
#: consulted. It is matched against the **repo-relative** path, which is the whole
#: point of this module — see the module docstring.
FALLBACK_PRUNE_DIRS = frozenset(
    {
        ".git",
        ".aws-sam",
        ".venv",
        "node_modules",
        "build",
        "dist",
        "__pycache__",
        "site-packages",
        "scratch",
        ".claude",
    }
)


@functools.lru_cache(maxsize=None)
def _git_toplevel(root: Path) -> Path | None:
    """The checkout root containing ``root``, or ``None`` if there is not one.

    Cached because several gates call ``tracked_paths`` repeatedly at import time
    and each call would otherwise fork git twice.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    return Path(out).resolve() if out else None


def _git_listing(root: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    """Deliberately **not** cached, unlike ``_git_toplevel``.

    Whether a directory is a checkout root cannot change during a test session;
    what is in it can. Two gates in this directory write a synthetic probe
    template into the repository and delete it in a ``finally``, so a cached
    listing would answer from before or after the write depending on call order.
    ``git ls-files`` over this tree costs a few tens of milliseconds and the gates
    call it a handful of times per run, so there is nothing to buy.
    """
    out = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *patterns,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    found = {root / rel for rel in out.split("\0") if rel}
    # ``ls-files --cached`` still lists a staged file that has been deleted from the
    # working tree; a gate that tried to read one would raise instead of failing.
    return tuple(sorted(p for p in found if p.is_file()))


def _walked(root: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    found: set[Path] = set()
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in FALLBACK_PRUNE_DIRS]
        for name in files:
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                found.add(Path(current) / name)
    return tuple(sorted(found))


def tracked_paths(root: Path, *patterns: str) -> tuple[Path, ...]:
    """Absolute paths under ``root`` matching any of ``patterns``, sorted.

    Reads the checkout through git when ``root`` is the top of one, and walks the
    filesystem otherwise. ``patterns`` are ``fnmatch``/pathspec globs on the file
    name, e.g. ``"*.py"`` or ``"*.yaml", "*.yml"``.
    """
    if not patterns:
        raise ValueError("tracked_paths needs at least one glob pattern")
    root = root.resolve()
    if _git_toplevel(root) == root:
        return _git_listing(root, tuple(patterns))
    return _walked(root, tuple(patterns))
