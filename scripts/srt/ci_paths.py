#!/usr/bin/env python3
"""Classify SRT finding paths by whether CI's checkout can actually see them.

CI runs the SRT gate (`srt_security_review` in .gitlab-ci.yml) on a clean
checkout of TRACKED files only — the job declares `needs: []` in `fast_checks`,
so no build stage ever precedes it and `.aws-sam/` does not exist there.

A local working tree is different: after a `python3 publish.py ...` run it
carries gitignored build output — `.aws-sam/packaged.yaml` for every nested
stack, `.aws-sam/idp-main.yaml`, vendored Lambda `layer/python/` trees,
`scratch/` — and `srt assess` has no `--exclude` option, so it happily scans
and flags all of it. Those findings CANNOT exist in CI.

Worse, SRT matches a finding to an existing suppression on the 4-tuple
`(path, resourceType, resourceName, check_id)`, so a suppression recorded
against `template.yaml` can never match the same resource re-flagged in
`.aws-sam/packaged.yaml`. The historical result was 30+ phantom HIGH findings
after any build, "fixed" by committing artifact-path entries into the baseline
`scripts/srt/issues.json` — which then re-detect as `reopened` on the next
scan, because `resolved` is not a sticky disposition in SRT.

This module lets run.py report those separately instead of gating on them, and
lets fix.py keep them out of the committed baseline in the first place.
"""

import subprocess
from pathlib import Path

# SRT dispositions that still need attention, i.e. that gate the build.
#
# 'reopened' MUST be here. SRT assigns it when a finding it had recorded as
# resolved/suppressed is detected again, and counts it in its own "N issues need
# attention" line. 'resolved' is NOT a sticky disposition — only 'suppressed'
# is — so gating on 'open' alone let a re-detected HIGH through silently (seen on
# 0.6.5: LAMBDA-012 in nested/bedrockkb/template.yaml and the semgrep npm
# minimum-release-age finding on src/ui/.npmrc, both carrying 'resolved').
GATING_STATUSES = ("open", "reopened")


def is_gating_status(status):
    """True if this SRT status means the finding is undispositioned."""
    return (status or "").lower() in GATING_STATUSES


def tracked_files(project_root):
    """Return the set of git-tracked repo-relative paths, or None if unknown.

    None means we could not ask git (not a repo, git missing, command failed).
    Callers must treat None as "cannot classify" and fall back to gating on
    everything — never silently drop findings we failed to check.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"⚠️  Warning: could not list tracked files ({e}); "
              "treating every finding as CI-visible.")
        return None

    if result.returncode != 0:
        print("⚠️  Warning: 'git ls-files' failed "
              f"(exit {result.returncode}); treating every finding as CI-visible.")
        return None

    return {p for p in result.stdout.split("\0") if p}


def _normalize(path):
    """Normalize an SRT finding path for comparison against `git ls-files`."""
    if not path:
        return None
    # SRT emits repo-relative POSIX paths, but be defensive about "./" prefixes
    # and Windows-style separators so a mismatch can't be read as "untracked".
    return Path(str(path).replace("\\", "/")).as_posix().removeprefix("./")


def is_in_ci_checkout(path, tracked):
    """True if this finding's file exists in CI's checkout (i.e. is tracked).

    Fails closed: an unknown `tracked` set, or a finding with no path at all,
    counts as CI-visible so it still gates.
    """
    if tracked is None:
        return True

    normalized = _normalize(path)
    if normalized is None:
        # A finding with no path (repo-wide observation) always gates.
        return True

    return normalized in tracked


def partition_by_ci_visibility(issues, project_root):
    """Split issues into (ci_visible, local_only) by path trackedness.

    `local_only` findings live in gitignored files — build artifacts, vendored
    third-party trees, scratch dirs — that CI never checks out.
    """
    tracked = tracked_files(project_root)
    ci_visible = []
    local_only = []

    for issue in issues:
        if is_in_ci_checkout(issue.get("path"), tracked):
            ci_visible.append(issue)
        else:
            local_only.append(issue)

    return ci_visible, local_only
