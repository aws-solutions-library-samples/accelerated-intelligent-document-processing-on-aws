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

Nothing CI scans is affected: a path that exists is never pruned, so a genuine
checkov crash on a tracked template still fails the gate.

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


def stale_scan_dirs(srt_dir, project_root="."):
    """[(scan_dir, scanned_path)] for scan dirs whose template is gone.

    A scan dir whose `security-matrix.json` cannot be read yields no path, so it
    is left alone — failing closed, the same way `failed_checkov_scans` treats an
    unresolvable path as CI-visible.
    """
    srt_dir = Path(srt_dir)
    root = Path(project_root)
    stale = []
    if not srt_dir.is_dir():
        return stale
    for scan_dir in sorted(srt_dir.iterdir()):
        if not scan_dir.is_dir() or not (scan_dir / "security-matrix.json").exists():
            continue
        scanned = _resolve_scanned_path(scan_dir)
        if scanned and not (root / scanned).exists():
            stale.append((scan_dir, scanned))
    return stale


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--srt-dir", default=".srt")
    ap.add_argument("--project-root", default=".")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stale = stale_scan_dirs(args.srt_dir, args.project_root)
    for scan_dir, scanned in stale:
        print(f"  stale scan dir {scan_dir} (scanned path {scanned} no longer exists)")
        if not args.dry_run:
            shutil.rmtree(scan_dir, ignore_errors=True)
    if not stale:
        print("  no stale SRT scan dirs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
