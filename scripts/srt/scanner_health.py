#!/usr/bin/env python3
"""Detect scanners that crashed and were silently skipped by `srt assess`.

`srt assess` runs each scanner in a child process and swallows failures:

    async run($, J, Z) {
      try { ... } catch (D) { S4.logError("Error during Checkov scan", D); return null }
    }

So a crashed scanner contributes zero findings and the printed table looks clean
for that source. Two ways this happens in practice, both observed:

1. **`stdout maxBuffer length exceeded`.** SRT's command wrapper calls node's
   `child_process.exec` with NO `maxBuffer` option, so it takes node's 1 MiB
   default. Checkov duplicates its entire JSON report to stdout even though SRT
   passes `--quiet --output-file-path`, so any template with a large enough
   report kills its own scan. On this repo the SAM-packaged artifacts do it:
   `sam package` re-serializes YAML and **drops comments**, so the 218
   `# checkov:skip=` annotations that make `template.yaml` clean (459 passed /
   0 failed / 211 skipped) are absent from `.aws-sam/idp-main.yaml`, whose
   report is then 2.6 MB. The source templates are nowhere near the limit.
2. **A shadowed interpreter.** The venv `semgrep` re-execs `pysemgrep` from
   `PATH`; a stray `~/.local/bin/pysemgrep` makes every scan die with
   `ModuleNotFoundError` and produce no `semgrep-summary.json`.

Neither shows up in the findings table, so this module makes the omission
explicit instead of leaving it to a manual `ls` of the summary files.

Detection is by **output presence, freshness-checked**: SRT writes
`<scanner>-summary.json` per scanner (whole-repo scanners at the `.srt/` root,
checkov once per scanned template under `.srt/<slug>/`), and writes `[]` rather
than nothing when a scan succeeds with no findings — so a missing file means the
scan did not complete. Files older than the current scan are treated as missing
so a previous run's output cannot mask a fresh crash. The `srt-tool.log` is
deliberately NOT parsed: it is a per-day file that also holds earlier runs'
errors, which would produce stale false positives.
"""

import json
from pathlib import Path

# Scanners that run once over the whole repo and write one summary at .srt/ root.
WHOLE_REPO_SCANNERS = {
    "bandit": "bandit-summary.json",
    "semgrep": "semgrep-summary.json",
    "syft": "syft-summary.json",
}

# Written per scanned template into .srt/<slug>/ alongside checkov-scan.json.
CHECKOV_SUMMARY = "checkov-summary.json"

# Not per-template scan dirs.
_NON_SCAN_DIRS = {"logs", ".venv"}


def _is_fresh(path, since):
    """True if `path` exists and was written at or after `since` (epoch seconds).

    `since` of None disables the freshness check (report presence only).
    """
    try:
        if since is None:
            return path.exists()
        # 1s of slack: mtime granularity varies by filesystem.
        return path.stat().st_mtime >= since - 1
    except OSError:
        return False


def missing_whole_repo_scanners(srt_dir, since=None):
    """Return the names of whole-repo scanners that produced no fresh summary."""
    srt_dir = Path(srt_dir)
    return sorted(
        name
        for name, filename in WHOLE_REPO_SCANNERS.items()
        if not _is_fresh(srt_dir / filename, since)
    )


def _resolve_scanned_path(scan_dir):
    """Best-effort source path for a per-template scan dir.

    SRT names the dir after the template with '/' replaced by '-' and the
    extension dropped, which is not reversibly parseable. But it also writes
    `security-matrix.json` there, whose records carry the exact repo-relative
    `path` — so read it rather than guessing. Returns None if unavailable.
    """
    matrix = scan_dir / "security-matrix.json"
    try:
        with open(matrix, encoding="utf-8") as f:
            records = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(records, list):
        return None
    for record in records:
        if isinstance(record, dict) and record.get("path"):
            return record["path"]
    return None


def failed_checkov_scans(srt_dir, since=None):
    """Return [(scan_dir_name, scanned_path_or_None)] for crashed checkov runs.

    A per-template scan dir with no fresh `checkov-summary.json` means checkov
    did not complete for that template — SRT writes `[]` on a clean result, so
    absence is a failure, not an empty pass.
    """
    srt_dir = Path(srt_dir)
    if not srt_dir.is_dir():
        return []

    failures = []
    for scan_dir in sorted(srt_dir.iterdir()):
        if not scan_dir.is_dir() or scan_dir.name in _NON_SCAN_DIRS:
            continue
        # Only dirs SRT created for a template scan; skip unrelated subdirs.
        if not (scan_dir / "security-matrix.json").exists():
            continue
        if not _is_fresh(scan_dir / CHECKOV_SUMMARY, since):
            failures.append((scan_dir.name, _resolve_scanned_path(scan_dir)))
    return failures
