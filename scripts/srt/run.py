#!/usr/bin/env python3
"""SRT run script to execute security assessment."""

import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ci_paths import is_gating_status, partition_by_ci_visibility  # noqa: E402


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


def main():
    """Run SRT security assessment."""
    import os

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
        import json

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

    if gating_issues:
        print_issue_table("🔴 OPEN HIGH PRIORITY SECURITY ISSUES", gating_issues)

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

    if gating_issues:
        if is_ci:
            # In CI/CD: fail the build
            sys.exit(1)
        else:
            # In local dev: continue to fix prompt (exit 0)
            print("💡 Run 'make srt-fix' to interactively review and suppress issues.")
            sys.exit(0)

    print("\n✅ SRT scan complete - no high-priority security issues found!")


if __name__ == "__main__":
    main()
