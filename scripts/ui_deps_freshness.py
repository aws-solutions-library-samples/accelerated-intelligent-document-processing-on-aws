# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Say whether an npm project's ``node_modules`` matches its committed lockfile.

The UI gates that run a tool out of ``src/ui/node_modules`` used to ask only whether
the tool's binary existed. A ``node_modules`` installed before ``package.json`` gained
a dependency passes that question and then fails inside the tool, for example as an
ESLint ``ERR_MODULE_NOT_FOUND`` stack trace for a plugin nobody installed. That is the
ordinary state of a checkout after ``git pull``, so the gates ask this instead and
say what to run.

The comparison is between ``package-lock.json`` and the hidden lockfile npm writes on
every install, ``node_modules/.package-lock.json``. A dependency is out of date when
the committed lockfile lists it and the install either lacks it or holds a different
version. An entry marked ``optional`` that is absent is not counted, because npm
leaves out the optional packages built for other platforms (esbuild and rollup ship
one per platform), and a fresh ``npm ci`` leaves dozens of those absent.

Two states mean "cannot judge" and are reported as fresh, so the caller falls back to
its own check: no ``package-lock.json`` at all, and no ``node_modules`` directory (the
caller's missing-binary check reports that case). A ``node_modules`` that exists
without the hidden lockfile was not written by a recent npm and is reported as out of
date.
"""

from __future__ import annotations

import json
from pathlib import Path

HIDDEN_LOCKFILE = Path("node_modules") / ".package-lock.json"


def stale_dependencies(project_root: Path) -> list[str] | None:
    """Return the out-of-date package paths, an empty list if current, None if unjudgeable."""
    committed = project_root / "package-lock.json"
    modules = project_root / "node_modules"
    if not committed.is_file() or not modules.is_dir():
        return None
    hidden = project_root / HIDDEN_LOCKFILE
    if not hidden.is_file():
        return [str(HIDDEN_LOCKFILE)]
    try:
        wanted = json.loads(committed.read_text(encoding="utf-8")).get("packages", {})
        installed = json.loads(hidden.read_text(encoding="utf-8")).get("packages", {})
    except (OSError, ValueError):
        return [str(HIDDEN_LOCKFILE)]
    stale = []
    for path, entry in wanted.items():
        if not path:  # "" is the project itself
            continue
        have = installed.get(path)
        if have is None:
            if not entry.get("optional"):
                stale.append(path)
        elif have.get("version") != entry.get("version"):
            stale.append(path)
    return stale


def describe(label: str, stale: list[str], shown: int = 3) -> str:
    """One line naming a few of the stale packages, for a gate's SKIP message."""
    names = [p.removeprefix("node_modules/") for p in stale[:shown]]
    more = f" and {len(stale) - shown} more" if len(stale) > shown else ""
    return (
        f"{label}/node_modules does not match {label}/package-lock.json "
        f"({', '.join(names)}{more} missing or at another version)"
    )
