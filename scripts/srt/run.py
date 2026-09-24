#!/usr/bin/env python3
"""SRT run script to execute security assessment."""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ci_paths import (  # noqa: E402
    is_gating_status,
    is_in_ci_checkout,
    partition_by_ci_visibility,
    partition_by_name_heuristic_scope,
    tracked_files,
)
from register import (  # noqa: E402
    WHOLE_REPO_SUMMARIES,
    describe_unsynced,
    describe_vacuous,
    restore_committed_register,
    vacuous_suppressions,
)
from scanner_health import (  # noqa: E402
    failed_checkov_scans,
    missing_whole_repo_scanners,
)


def warn_about_build_artifacts(project_root):
    """Warn up front if the tree carries SAM build output.

    Scanning a built tree is the single biggest source of confusion with this
    gate: the artifacts add ~30 phantom HIGH findings (non-blocking now, but
    still noise) AND crash checkov, because `sam package` re-serializes YAML and
    drops the `# checkov:skip=` comments that make the source templates clean —
    the resulting report blows past node's 1 MiB stdout buffer. Better to say so
    before spending 15 minutes than to explain it afterwards.
    """
    artifacts = sorted(
        p.relative_to(project_root).as_posix()
        for p in project_root.glob("**/.aws-sam")
        if p.is_dir() and "node_modules" not in p.parts
    )
    if not artifacts:
        return

    print(
        f"\n⚠️  {len(artifacts)} .aws-sam build directory(ies) present. These are "
        "gitignored, so\n"
        "   CI never scans them. Locally they add phantom findings (reported "
        "separately as\n"
        "   LOCAL-ONLY below) and crash checkov on the largest templates.\n"
        "   Run 'make srt-clean' first to match CI exactly."
    )


def nested_checkouts(project_root):
    """Return git checkouts nested inside the tree, as repo-relative paths.

    Finds both worktrees git still has registered and directories under
    `.claude/worktrees/` that hold a `.git` but are no longer registered — an
    orphaned copy costs the scanner exactly as much as a live one.
    """
    root = project_root.resolve()
    found = set()

    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            path = Path(line[len("worktree ") :]).resolve()
            if path != root and root in path.parents:
                found.add(path.relative_to(root).as_posix())

    agent_dir = root / ".claude" / "worktrees"
    if agent_dir.is_dir():
        for child in agent_dir.iterdir():
            if child.is_dir() and (child / ".git").exists():
                found.add(child.resolve().relative_to(root).as_posix())

    return sorted(found)


def abort_on_nested_checkouts(project_root):
    """Refuse to scan a tree that contains nested git checkouts.

    `srt assess` walks the whole path it is given and has no `--exclude` option,
    so every nested checkout multiplies the scan: one checkov process per
    template per copy, all concurrent. A tree holding 74 agent worktrees (49 GB)
    spawned 2,200 checkov children, exhausted 123 GiB of RAM plus 8 GiB of swap,
    drove memory pressure to a sustained 79% full stall that blocked new SSH
    logins, and had to be killed after 2h26m without producing a report. The
    findings would have duplicated the root tree's in any case, so there is
    nothing to gain by scanning them.

    Set `SRT_ALLOW_NESTED_CHECKOUTS=1` to scan anyway.
    """
    if os.getenv("SRT_ALLOW_NESTED_CHECKOUTS"):
        return

    nested = nested_checkouts(project_root)
    if not nested:
        return

    shown = "\n".join(f"     {path}" for path in nested[:10])
    more = f"\n     ... and {len(nested) - 10} more" if len(nested) > 10 else ""
    print(
        f"\n❌ {len(nested)} nested git checkout(s) inside the tree. `srt assess` has\n"
        "   no --exclude option, so it would scan every copy concurrently — one\n"
        "   checkov process per template per checkout — for findings that merely\n"
        "   duplicate this tree's.\n"
        f"{shown}{more}\n\n"
        "   Remove or relocate them, then re-run. To scan anyway (it can exhaust\n"
        "   RAM and swap on a large host), set SRT_ALLOW_NESTED_CHECKOUTS=1."
    )
    sys.exit(1)


def report_scanner_health(srt_dir, project_root, scan_started, is_ci):
    """Print any silently-skipped scanner; return True if the gate should fail.

    A crashed scanner contributes zero findings, so a clean table for that
    source is meaningless. Failures on gitignored files are noise for the same
    reason their findings are (CI never scans them), so only tracked-file
    losses can fail the build.
    """
    missing = missing_whole_repo_scanners(srt_dir, scan_started)
    checkov_failures = failed_checkov_scans(srt_dir, scan_started)

    if not missing and not checkov_failures:
        return False

    tracked = tracked_files(project_root)
    blocking_checkov = [
        (name, path)
        for name, path in checkov_failures
        # An unresolvable path (None) fails closed — treat it as CI-visible.
        if is_in_ci_checkout(path, tracked)
    ]
    local_only_checkov = len(checkov_failures) - len(blocking_checkov)

    print("\n" + "=" * 120)
    print("⚠️  SCANNERS THAT DID NOT COMPLETE (findings below are incomplete)")
    print("=" * 120)

    for name in missing:
        print(f"  ❌ {name}: no summary written — scanner produced NO findings at all")

    for name, path in blocking_checkov:
        print(f"  ❌ checkov: no result for {path or f'<scan dir {name}>'}")

    if local_only_checkov:
        print(
            f"  ℹ️  checkov: {local_only_checkov} failure(s) on gitignored build "
            "artifacts (not in CI; run 'make srt-clean')"
        )

    print("=" * 120)

    if not missing and not blocking_checkov:
        return False

    print(
        "A skipped scanner means this scan cannot prove the tree is clean.\n"
        "Common causes: node's 1 MiB stdout buffer (checkov duplicates its whole\n"
        "JSON report to stdout despite --quiet), or a shadowed pysemgrep on PATH.\n"
        "See .srt/logs/srt-tool.log.* and scripts/srt/scanner_health.py."
    )
    return is_ci


def report_vacuous_suppressions(project_root, srt_dir, scan_started, is_ci):
    """Print committed suppressions this scan produced no finding for; gate in CI.

    THE GAP THIS CLOSES. Two checks already guard the committed register — every
    entry's path is git-tracked, and every suppressed entry carries a reason — and
    neither asks whether an entry still shields anything. Measured when this was
    written: **52 of the register's suppressed entries shielded nothing**, every one
    of the Bandit ones, because each site had since been fixed in source with an
    inline `# nosec` and the register entry was never removed. The suppression key is
    `(path, resourceType, resourceName, check_id)` and carries no line, so each dead
    entry was pre-suppressing every future finding of that check in that file.

    WHY THIS LIVES IN THE SCAN AND NOT IN THE OFFLINE SUITE. The question is "did the
    scanner report this finding", and the only thing that can answer it is the
    scanner. Bandit is not a dependency of this repository — the SRT installer puts it
    in `.srt/.venv` — so an offline test would skip wherever it is absent, which in CI
    is everywhere, and a gate that skips is the shape of defect this whole check is
    about. A re-implementation of bandit's own detection would be worse: probing
    whether the pinned line still contains the literal the entry quotes reports **25**
    of these 52 as live, because the line is still there and an inline `# nosec` is
    what silenced it. A proxy that disagrees with the scanner in the direction of
    "still needed" keeps dead suppressions alive, which is the state being fixed.

    WHAT IT DOES NOT COVER, precisely. Only the sources in `WHOLE_REPO_SUMMARIES` —
    Bandit today, which is 54 of the register's entries. The rest are checkov and
    SRT's own per-template AWS checks, where an absent finding can mean the template
    is clean or that the scanner failed on that template; reading the second as a dead
    suppression would delete a live one on a bad day. A scanner that did not complete
    is skipped here for the same reason, and `report_scanner_health` fails the build
    for it separately.
    """
    missing = missing_whole_repo_scanners(srt_dir, scan_started)
    committed_path = project_root / "scripts" / "srt" / "issues.json"
    if not committed_path.exists():
        return False
    try:
        committed = json.loads(committed_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return False
    if not isinstance(committed, list):
        return False

    dead, unmeasured = [], []
    for source, (scanner, filename) in sorted(WHOLE_REPO_SUMMARIES.items()):
        if scanner in missing:
            unmeasured.append(source)
            continue
        try:
            findings = json.loads((srt_dir / filename).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            unmeasured.append(source)
            continue
        if not isinstance(findings, list):
            unmeasured.append(source)
            continue
        dead.extend(vacuous_suppressions(committed, findings, sources={source}))

    if unmeasured:
        print(
            f"\nℹ️  Non-vacuity of the suppression register not checked for "
            f"{', '.join(unmeasured)}: that scanner wrote no readable summary for this "
            "scan."
        )

    if not dead:
        return False

    print("\n" + "=" * 120)
    print(
        f"🔴 SUPPRESSIONS THAT SHIELD NOTHING - TOTAL: {len(dead)} "
        "(scripts/srt/issues.json)"
    )
    print("=" * 120)
    print(describe_vacuous(dead))
    print("=" * 120)
    print(
        "This scan reported no finding matching these entries, so each one currently\n"
        "suppresses nothing. They are not inert: the suppression key is\n"
        "(path, resourceType, resourceName, check_id) and carries no line, so each one\n"
        "pre-suppresses the next finding of that check in that file -- a real\n"
        "hardcoded credential landing in a pre-registered file would be suppressed on\n"
        "arrival with nothing to notice.\n"
        "\n"
        "Delete them from scripts/srt/issues.json. If you just fixed a finding in\n"
        "source (an inline `# nosec`, or removing the value), deleting its entry is\n"
        "the second half of that fix.\n"
        "\n"
        "⚠️  Locally, the NEXT scan will then refuse to start: .srt/issues.json still\n"
        "   holds the dispositions you removed, and restore_committed_register cannot\n"
        "   tell a deliberate deletion from a local disposition nobody saved yet. Run\n"
        "   the next scan once with SRT_DISCARD_LOCAL=1 (or delete .srt/issues.json).\n"
        "   CI never sees this: it starts from a fresh checkout."
    )
    return is_ci


def print_issue_table(title, issues):
    """Print a numbered table of findings under a banner."""
    separator = "=" * 120
    divider = "-" * 120

    print(f"\n{separator}")
    print(f"{title} - TOTAL: {len(issues)}")
    print(separator)
    print(
        f"{'#':<4} {'SEVERITY':<10} {'SOURCE':<12} {'CHECK ID':<20} {'FILE':<50} {'LINE':<6}"
    )
    print(divider)

    for idx, issue in enumerate(issues, 1):
        priority = issue.get("priority") or "UNKNOWN"
        source = issue.get("source") or "Unknown"
        check_id = (issue.get("check_id") or "")[:19]  # Truncate long check IDs
        path = issue.get("path") or "Unknown"
        # Truncate long paths for readability
        if len(path) > 48:
            path = "..." + path[-45:]
        line = str(issue.get("line", "?"))

        print(
            f"{idx:<4} {priority:<10} {source:<12} {check_id:<20} {path:<50} {line:<6}"
        )

    print(separator)


def run_command(cmd: str, cwd=None, capture_output=False):
    """Run shell command and return result."""
    try:
        # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true - Reviewed: command input is controlled and sanitized
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, text=True, capture_output=capture_output
        )  # nosec B602 - hardcoded commands, no user input
        if capture_output:
            return result
        return result.returncode == 0
    except Exception as e:
        print(f"Exception running command {cmd}: {e}")
        return None if capture_output else False


def restore_register(project_root, srt_dir):
    """Copy scripts/srt/issues.json over .srt/issues.json; False if that would lose work.

    A HIGH disposition present locally but never saved to the committed register
    (a hand edit, or a raw `./srt fix` without fix.py's copy-back) is reported
    and blocks the scan, unless SRT_DISCARD_LOCAL=1 says to drop it.
    """
    import os

    tracked = tracked_files(project_root)
    result = restore_committed_register(
        project_root / "scripts" / "srt" / "issues.json",
        srt_dir / "issues.json",
        is_ci_visible=lambda i: is_in_ci_checkout(i.get("path"), tracked),
        discard_local=os.getenv("SRT_DISCARD_LOCAL") == "1",
    )
    if result.action == "restored":
        print(
            f"✓ Restored the committed disposition register "
            f"({result.committed_count} entries) into .srt/issues.json"
        )
        return True
    if result.action == "no-committed-register":
        print("ℹ️  No committed scripts/srt/issues.json — scanning with no baseline")
        return True
    print(
        "❌ .srt/issues.json holds HIGH dispositions that are NOT in the committed\n"
        "   register scripts/srt/issues.json. Restoring the register would discard\n"
        "   them, so the scan is refused. Either save them (`make srt-fix` copies\n"
        "   dispositions back), or drop them with SRT_DISCARD_LOCAL=1:\n"
        + describe_unsynced(result.unsynced)
    )
    return False


def main():
    """Run SRT security assessment."""
    project_root = Path(__file__).parent.parent.parent
    srt_dir = project_root / ".srt"
    srt_executable = srt_dir / "srt"

    # Check if running in CI/CD environment
    is_ci = bool(
        os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS")
    )

    if not srt_executable.exists():
        print(f"❌ SRT not found at: {srt_executable}")
        print(f"   Expected .srt directory at: {srt_dir}")
        print("   Run 'make srt-setup' first.")
        sys.exit(1)

    print("Running SRT security assessment...")
    print(f"✓ SRT binary found at: {srt_executable}")

    # Run SRT assessment on the project
    # Use -y flag to skip interactive prompts (e.g., "Open dashboard in browser?")
    # Use -p flag to specify project path
    # Use --no-diagrams and --no-threat-models to reduce memory usage in CI/CD
    # Use --no-license-update to prevent automatic license header updates
    project_path = str(project_root)
    print(f"Scanning project: {project_path}")

    abort_on_nested_checkouts(project_root)
    warn_about_build_artifacts(project_root)

    # Start every scan from the COMMITTED disposition register. `srt assess`
    # merges into whatever .srt/issues.json already holds; a stale live file
    # predating a committed suppression makes the scanner "discover" that
    # finding and open it (v0.6.8: 10 false open HIGH). CI has always done this
    # copy via `make srt-setup`; a local `make srt-scan` did not. See register.py.
    if not restore_register(project_root, srt_dir):
        sys.exit(1)

    # Recorded before the scan so a previous run's scanner summaries can't be
    # mistaken for this run's output when checking which scanners completed.
    scan_started = time.time()

    # Properly quote the project path to prevent command injection
    quoted_path = shlex.quote(project_path)
    result = run_command(
        f"./srt assess -y -p {quoted_path} --no-diagrams --no-threat-models --no-license-update",
        cwd=srt_dir,
        capture_output=True,
    )

    if result is None or result.returncode != 0:
        print("❌ SRT scan failed to run")
        if result:
            print(f"Exit code: {result.returncode}")
            if result.stdout:
                print(f"Output:\n{result.stdout}")
            if result.stderr:
                print(f"Error:\n{result.stderr}")
        sys.exit(1)

    # Print the output
    print(result.stdout)

    # Check if there are any HIGH priority open security issues by parsing issues.json
    # This is more reliable than substring matching on stdout, which can break if SRT
    # changes its output format or uses ANSI color codes
    issues_json_path = srt_dir / "issues.json"
    high_open_issues = []

    if issues_json_path.exists():
        try:
            with open(issues_json_path, encoding="utf-8") as f:
                issues = json.load(f)
            # Filter only HIGH priority issues that are not dispositioned.
            # Medium/Low issues don't block CI. See GATING_STATUSES in
            # ci_paths.py for why 'reopened' counts as undispositioned.
            high_open_issues = [
                issue
                for issue in issues
                if (issue.get("priority") or "").upper() == "HIGH"
                and is_gating_status(issue.get("status"))
            ]
        except (json.JSONDecodeError, UnicodeDecodeError, IOError) as e:
            print(f"⚠️  Warning: Could not parse issues.json: {e}")
            # Fall back to stdout check if JSON parsing fails
            if "Open: 0" not in result.stdout:
                # Create a dummy issue to indicate problems exist
                high_open_issues = [{"issue": "Unknown - check SRT output"}]

    # Only findings in files CI actually checks out can gate. A local working
    # tree that has been built carries gitignored SAM artifacts
    # (.aws-sam/packaged.yaml, .aws-sam/idp-main.yaml) and vendored third-party
    # trees that srt assess scans but CI never sees — reporting those as
    # blocking produced phantom "regressions" after every publish.py run, and
    # invited artifact-path suppressions into the committed baseline. See
    # ci_paths.py for the full rationale.
    gating_issues, local_only_issues = partition_by_ci_visibility(
        high_open_issues, project_root
    )

    # Bandit's identifier-name heuristics (B105/B106) arrive promoted to HIGH by
    # SRT regardless of what they matched, so a fixture key named `pass_count`
    # gates like a credential. In test code that no deployment artifact is built
    # from they are reported and do not gate; everywhere else — including a
    # test-shaped file inside a Lambda's CodeUri, which sam build copies into the
    # artifact — they still do. See NAME_HEURISTIC_EXEMPT in ci_paths.py for why
    # no Bandit rule can express this scope.
    gating_issues, name_scoped_issues = partition_by_name_heuristic_scope(
        gating_issues, project_root
    )

    if gating_issues:
        print_issue_table("🔴 OPEN HIGH PRIORITY SECURITY ISSUES", gating_issues)

    if name_scoped_issues:
        print_issue_table(
            "ℹ️  IDENTIFIER-NAME FINDINGS IN TEST CODE (Bandit rates these LOW, "
            "non-blocking)",
            name_scoped_issues,
        )
        print(
            "B105/B106 match an identifier's NAME against a password wordlist, not\n"
            "its value. SRT promotes them to HIGH unconditionally; in test code that\n"
            "no deployment artifact is built from, they are reported at Bandit's own\n"
            "severity instead of blocking. The same name shape still gates anywhere\n"
            "that ships — including a test file inside a Lambda's CodeUri, which sam\n"
            "build copies into the artifact. Do NOT add a per-line suppression pragma\n"
            "for one of these; that is the accretion this scope decision replaces.\n"
            "If one of them is a REAL credential, it is not a false positive: remove\n"
            "it from the fixture."
        )

    if local_only_issues:
        print_issue_table(
            "ℹ️  LOCAL-ONLY FINDINGS (gitignored files - NOT in CI, non-blocking)",
            local_only_issues,
        )
        print(
            "These files are gitignored (build artifacts, vendored deps, scratch),\n"
            "so CI's clean checkout cannot see them and they do NOT gate the build.\n"
            "Do NOT suppress them in scripts/srt/issues.json — the suppression key\n"
            "includes the path, so it would never match the real source template.\n"
            "Run 'make srt-clean' to remove them and match CI exactly."
        )

    # A scanner that crashed contributes zero findings, so an empty table above
    # is not evidence of a clean tree. Surface that before reporting a pass.
    scanners_incomplete = report_scanner_health(
        srt_dir, project_root, scan_started, is_ci
    )

    # A suppression that shields nothing pre-suppresses the next finding of that check
    # in that file, and the scan is the only thing that can tell. See issue #1149.
    register_is_stale = report_vacuous_suppressions(
        project_root, srt_dir, scan_started, is_ci
    )

    if gating_issues:
        if is_ci:
            # In CI/CD: fail the build
            sys.exit(1)
        else:
            # In local dev: continue to fix prompt (exit 0)
            print("💡 Run 'make srt-fix' to interactively review and suppress issues.")
            sys.exit(0)

    if scanners_incomplete:
        # CI only (report_scanner_health returns False locally): no HIGH findings,
        # but coverage was lost on a file CI does scan, so this is not a pass.
        print("\n❌ SRT scan INCOMPLETE - a scanner did not run; results unreliable.")
        sys.exit(1)

    if register_is_stale:
        # CI only (report_vacuous_suppressions returns False locally): no HIGH
        # findings, but the register carries suppressions over findings that no longer
        # exist, each pre-approving whatever next lands on its (path, check_id).
        print(
            "\n❌ SRT scan found no open HIGH findings, but the committed suppression\n"
            "   register carries entries that shield nothing. Delete them (above)."
        )
        sys.exit(1)

    print("\n✅ SRT scan complete - no high-priority security issues found!")


if __name__ == "__main__":
    main()
