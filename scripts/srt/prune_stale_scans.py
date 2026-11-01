#!/usr/bin/env python3
"""Delete `.srt/<slug>/` scan dirs whose scanned template no longer exists.

Why this is needed at all: `srt assess` writes one scan dir per template it
finds, and never removes one. `scanner_health.failed_checkov_scans` then reports
any scan dir without a *fresh* `checkov-summary.json` as "checkov did not
complete" — which is the right rule for a live template, but a permanent false
positive for a template that has since been deleted or rebuilt away:

* **build artifacts** — `.aws-sam/idp-main.yaml` and friends. `make srt-clean`
  deletes the `.aws-sam` trees, so the next scan cannot refresh their scan dirs.
* **deleted source templates** — `nested/alb-hosting/template.yaml` and
  `scripts/alb-test-vpc.yaml` were removed when ALB hosting was replaced by API
  Gateway hosting, but their scan dirs survived and kept being counted.

Either way the printed warning is "this scan cannot prove the tree is clean" on a
run that is in fact clean, and the curated public security snapshot publishes it.
Pruning by *does the scanned path still exist* fixes the whole class, rather than
chasing the naming convention of whichever artifact produced it — the previous
attempt matched `*-.aws-sam-packaged` and `.aws-sam-*` by glob and still missed
the two deleted ALB templates.

⚠️ **A clean `.aws-sam` template leaves no path to read.** SRT writes
`security-matrix.json` as `[]` for a template with no findings, so the scanned path
cannot be recovered from it, and a scan dir whose path is unknown was left alone. That
is the right default for a source template — `failed_checkov_scans` treats an
unresolvable path as CI-visible, failing closed — but for a build artifact it meant
`make srt-clean` deleted the template and kept its scan dir, and the next scan under
`CI=1` reported INCOMPLETE over a file that no longer exists. So an unresolvable scan
dir whose name carries a `.aws-sam` component is matched the other way round: its name
is compared against the names SRT would give every template still present whose
path mentions `.aws-sam`, and it is stale when none matches. Unresolvable dirs of any other
shape are still left alone.

Nothing CI scans is affected: a path that exists is never pruned, so a genuine
checkov crash on a tracked template still fails the gate. A CI checkout has no
`.aws-sam` tree at all, so the second rule cannot fire there.

Usage: python3 scripts/srt/prune_stale_scans.py [--srt-dir .srt] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scanner_health import _resolve_scanned_path  # noqa: E402

BUILD_DIR = ".aws-sam"
# What `srt assess` treats as a template. A file of another kind under `.aws-sam`
# cannot be what a scan dir was named after, so it is not a candidate.
TEMPLATE_SUFFIXES = (".yaml", ".yml", ".json", ".template")
# Trees `make srt-clean` does not descend into, so it does not delete their
# `.aws-sam` either; walking them here would only cost time.
UNWALKED_DIRS = {"node_modules", ".venv", ".git", ".srt"}


def scan_dir_name(template_path):
    """The name `srt assess` gives the scan dir for a repo-relative template path.

    '/' becomes '-' and the extension is dropped: `nested/x/.aws-sam/packaged.yaml`
    is scanned into `.srt/nested-x-.aws-sam-packaged`.
    """
    path = Path(template_path)
    return "-".join(path.with_suffix("").parts)


def names_a_build_artifact(name):
    """True when a scan dir's name carries a `.aws-sam` path component."""
    return name.startswith(BUILD_DIR + "-") or f"-{BUILD_DIR}-" in name


def build_artifact_scan_names(project_root="."):
    """Scan-dir names of every template present whose path mentions `.aws-sam`.

    Not only templates under a `.aws-sam` directory: the name rule cannot tell
    `nested/x/.aws-sam/packaged.yaml` from `nested/x/.aws-sam-packaged.yaml`, so
    any present template whose name could carry the `.aws-sam` component keeps
    its scan dir. That is what keeps "a path that exists is never pruned" true.
    """
    root = Path(project_root)
    names = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in UNWALKED_DIRS]
        rel = Path(dirpath).relative_to(root)
        for filename in filenames:
            path = rel / filename
            if BUILD_DIR in path.as_posix() and filename.endswith(TEMPLATE_SUFFIXES):
                names.add(scan_dir_name(path))
    return names


def stale_scan_dirs(srt_dir, project_root="."):
    """[(scan_dir, scanned_path)] for scan dirs whose template is gone.

    A scan dir whose `security-matrix.json` yields no path is left alone —
    failing closed, the same way `failed_checkov_scans` treats an unresolvable
    path as CI-visible — unless its name says it scanned a `.aws-sam` build
    artifact and no template present still carries that name.
    Those are reported with the scanned path `None`.
    """
    srt_dir = Path(srt_dir)
    root = Path(project_root)
    stale = []
    if not srt_dir.is_dir():
        return stale
    live_artifacts = None
    for scan_dir in sorted(srt_dir.iterdir()):
        if not scan_dir.is_dir() or not (scan_dir / "security-matrix.json").exists():
            continue
        scanned = _resolve_scanned_path(scan_dir)
        if scanned:
            if not (root / scanned).exists():
                stale.append((scan_dir, scanned))
            continue
        if not names_a_build_artifact(scan_dir.name):
            continue
        if live_artifacts is None:
            live_artifacts = build_artifact_scan_names(root)
        if scan_dir.name not in live_artifacts:
            stale.append((scan_dir, None))
    return stale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--srt-dir", default=".srt")
    ap.add_argument("--project-root", default=".")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stale = stale_scan_dirs(args.srt_dir, args.project_root)
    for scan_dir, scanned in stale:
        if scanned is None:
            print(
                f"  stale scan dir {scan_dir} (a {BUILD_DIR} build artifact no "
                f"template present carries the name of)"
            )
        else:
            print(
                f"  stale scan dir {scan_dir} (scanned path {scanned} no longer exists)"
            )
        if not args.dry_run:
            shutil.rmtree(scan_dir, ignore_errors=True)
    if not stale:
        print("  no stale SRT scan dirs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
