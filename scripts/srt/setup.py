#!/usr/bin/env python3
"""SRT setup script to download and configure the Sample Security Review Tool."""

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


def run_command(cmd, cwd=None, interactive=False):
    """Run shell command and return result."""
    try:
        if interactive:
            result = subprocess.run(cmd, shell=True, cwd=cwd, text=True)  # nosec B602 nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true - hardcoded commands, no user input
        else:
            result = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True, text=True
            )  # nosec B602 nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true - hardcoded commands, no user input
        if result.returncode != 0:
            if not interactive:
                print(f"Error running command: {cmd}")
                print(f"Error: {result.stderr}")
            return False
        return True
    except Exception as e:
        print(f"Exception running command {cmd}: {e}")
        return False


def get_platform_suffix():
    """Get SRT platform suffix based on system."""
    system = platform.system().lower()
    arch = platform.machine().lower()

    if system == "linux":
        if "x86_64" in arch or "amd64" in arch:
            return "linux-x64"
        elif "arm" in arch or "aarch64" in arch:
            return "linux-arm64"
    elif system == "darwin":  # macOS
        if "arm" in arch or "aarch64" in arch:
            return "macos-arm64"
        else:
            return "macos-x64"
    elif system == "windows":
        return "windows-x64"

    raise ValueError(f"Unsupported platform: {system} {arch}")


def get_latest_release():
    """Fetch latest release information from GitHub.

    Authenticates when a token is available. This call runs once per setup, and
    unauthenticated GitHub API is limited to 60 requests/hour **per IP** — which
    hosted CI runners share, so on a busy runner pool the anonymous request
    returns 403 and the whole scan fails with "Failed to fetch latest release".
    A token raises the limit to 5,000/hour for that token. GitHub Actions always
    provides GITHUB_TOKEN; GitLab CI has no equivalent, so there the request
    stays anonymous exactly as before.
    """
    url = "https://api.github.com/repos/aws-samples/sample-security-review-tool/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:  # nosec B310 - GitHub API URL is trusted
            data = json.loads(response.read().decode())
            return data["tag_name"], data["assets"]
    except Exception as e:
        print(f"Failed to fetch latest release: {e}")
        if not token:
            print(
                "   Hint: unauthenticated GitHub API allows 60 requests/hour per "
                "IP, which CI runners share. Set GITHUB_TOKEN to raise it."
            )
        return None, None


def write_aws_profile(profile, region):
    """Ensure `~/.aws/config` names `profile`, without needing the AWS CLI.

    Equivalent to `aws configure set region <region> --profile <profile>`, for
    images that have no awscli. Appends rather than overwrites, and leaves an
    existing entry for the profile alone, so this can never clobber a developer's
    real configuration.
    """
    aws_dir = Path.home() / ".aws"
    config = aws_dir / "config"
    # `default` is spelled `[default]`; every other profile is `[profile name]`.
    header = "[default]" if profile == "default" else f"[profile {profile}]"

    existing = config.read_text() if config.exists() else ""
    if header in existing:
        print(f"   ℹ️  {config} already has {header} - leaving it alone")
        return

    aws_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    separator = "" if (not existing or existing.endswith("\n")) else "\n"
    with config.open("a", encoding="utf-8") as fh:
        fh.write(f"{separator}{header}\nregion = {region}\noutput = json\n")
    config.chmod(0o600)
    print(f"   ✅ wrote {header} (region={region}) to {config}")


def download_srt(tag_name, assets, srt_dir):
    """Download SRT binary for current platform."""
    platform_suffix = get_platform_suffix()

    # Construct expected filename pattern: srt-cli-v{version}-{platform}.{ext}
    # e.g., srt-cli-v1.0.2-linux-x64.tar.gz
    extension = ".zip" if "windows" in platform_suffix else ".tar.gz"
    expected_pattern = f"srt-cli-{tag_name}-{platform_suffix}{extension}"

    # Find matching asset
    asset = None
    for a in assets:
        if a["name"] == expected_pattern:
            asset = a
            break

    if not asset:
        print(f"No release found for platform: {expected_pattern}")
        print(f"Available assets: {[a['name'] for a in assets]}")
        return False

    download_url = asset["browser_download_url"]
    archive_path = srt_dir / expected_pattern

    print(f"Downloading SRT {tag_name} for {platform_suffix}...")
    try:
        urllib.request.urlretrieve(download_url, archive_path)  # nosec B310 - GitHub release URL is trusted
        print(f"Downloaded: {archive_path.name}")
        return archive_path
    except Exception as e:
        print(f"Download failed: {e}")
        return False


def extract_srt(archive_path, srt_dir):
    """Extract SRT archive."""
    filename = archive_path.name

    print(f"Extracting: {filename}")

    success = False
    if filename.endswith(".tar.gz"):
        # Properly quote the filename to prevent command injection
        quoted_filename = shlex.quote(filename)
        success = run_command(f"tar -xzf {quoted_filename}", cwd=srt_dir)
    else:
        print(f"Unsupported archive format: {filename}")
        return False

    # Remove macOS quarantine attribute if on macOS
    if success and platform.system().lower() == "darwin":
        srt_executable = srt_dir / "srt"
        if srt_executable.exists():
            print("Removing macOS quarantine attribute...")
            run_command("xattr -d com.apple.quarantine ./srt", cwd=srt_dir)

    return success


def get_installed_version(srt_dir):
    """Get the currently installed SRT version, or None if not installed."""
    srt_executable = srt_dir / "srt"
    if not srt_executable.exists():
        return None
    try:
        result = subprocess.run(
            [str(srt_executable), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            # Parse version from output like "srt version v0.1.0"
            output = result.stdout.strip()
            if "version" in output:
                return output.split()[-1].lstrip("v")
        return None
    except Exception:
        return None


# The scanners `srt assess` hard-requires. SRT installs these into its OWN
# managed venv at `.srt/.venv/bin/` (NOT the system PATH) and, at assess time,
# calls checkAllInstalled() which fails with "Prerequisites not installed."
# unless EVERY one of these executables is present. `anchore_syft` installs as
# the `syft` binary. NOTE: contrary to older guidance, semgrep is NOT optional
# for this SRT version — a missing semgrep blocks the scan just like any other.
REQUIRED_SCANNERS = ["checkov", "semgrep", "bandit", "syft", "jupyter"]


def missing_scanners(srt_dir):
    """Return the REQUIRED_SCANNERS whose executable is absent from SRT's venv."""
    venv_bin = srt_dir / ".venv" / "bin"
    return [tool for tool in REQUIRED_SCANNERS if not (venv_bin / tool).exists()]


def main():
    """Setup SRT tool."""
    project_root = Path(__file__).parent.parent.parent
    srt_dir = project_root / ".srt"

    # Check if running in CI/CD environment
    # "CI" here really means "non-interactive". A detached or scripted run with
    # no terminal must take the programmatic path too: `srt config` otherwise
    # blocks on an AWS-profile prompt nobody can answer, and interrupting that
    # prompt leaves SRT with no config and no scanner venv (seen at v0.6.8).
    is_ci = bool(
        os.getenv("CI")
        or os.getenv("GITLAB_CI")
        or os.getenv("GITHUB_ACTIONS")
        or not sys.stdin.isatty()
    )

    print("Setting up SRT (Sample Security Review Tool)...")

    # Clean .srt directory to ensure fresh installation and prevent stale cache issues
    if srt_dir.exists():
        print("Cleaning existing .srt directory for fresh setup...")
        try:
            shutil.rmtree(srt_dir)
            print("✅ Removed .srt directory")
        except Exception as e:
            print(f"⚠️  Warning: Could not remove .srt directory: {e}")
            # Continue anyway - setup might still work

    # Create .srt directory
    srt_dir.mkdir(exist_ok=True)

    # Get latest release info
    tag_name, assets = get_latest_release()
    if not tag_name or not assets:
        print("Failed to fetch latest release information")
        sys.exit(1)

    desired_version = tag_name.lstrip("v")
    print(f"Latest version: v{desired_version}")

    # Check if desired version is already installed
    installed_version = get_installed_version(srt_dir)
    if installed_version == desired_version:
        print(f"SRT v{desired_version} is already installed. Skipping download.")
    else:
        if installed_version:
            print(f"Installed: v{installed_version}. Upgrading to: v{desired_version}.")

        # Remove old files
        for old_file in srt_dir.glob("srt*"):
            old_file.unlink()
            print(f"Removed old file: {old_file.name}")

        # Download SRT
        archive_path = download_srt(tag_name, assets, srt_dir)
        if not archive_path:
            print("Failed to download SRT tool")
            sys.exit(1)

        # Extract SRT tool
        if not extract_srt(archive_path, srt_dir):
            print("Failed to extract SRT tool")
            sys.exit(1)

        # Verify installed version after extraction
        installed_version = get_installed_version(srt_dir)
        if installed_version and installed_version != desired_version:
            print(
                f"Warning: Expected v{desired_version}, but got v{installed_version}."
            )

        print(f"✅ SRT v{desired_version} installed successfully!")

    # Make srt executable
    srt_executable = srt_dir / "srt"
    if srt_executable.exists():
        srt_executable.chmod(0o755)

    # Configure SRT
    config_file = srt_dir / "srtconfig.json"

    if is_ci:
        # Create config file programmatically for CI/CD
        print("\n✅ Running in CI/CD - creating non-interactive configuration")

        aws_region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        aws_profile = os.getenv("AWS_PROFILE", "default")

        # A profile must EXIST in ~/.aws before `srt config` runs. That command
        # enumerates profiles and aborts with "✗ No AWS profiles found!" if there
        # are none — and it is what installs the five scanners, so without a
        # profile the whole scan dies at setup with a message about AWS that has
        # nothing to do with the failure the user cares about.
        #
        # No credentials are needed: the scan is static analysis. Only the profile
        # entry has to be there.
        #
        # This used to be skipped entirely when the AWS CLI was absent, which made
        # the setup silently dependent on the CI image happening to ship awscli.
        # It does on the GitLab runner and does not in python:3.13-bookworm, so the
        # GitHub job failed at `srt config` with all five scanners missing. Write
        # the file directly in that case rather than skipping.
        if shutil.which("aws"):
            print(f"   Configuring AWS CLI for profile '{aws_profile}'...")
            configure_ok = True
            for args in (
                ["aws", "configure", "set", "region", aws_region],
                ["aws", "configure", "set", "output", "json"],
            ):
                result = subprocess.run(
                    [*args, "--profile", aws_profile],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    configure_ok = False
                    print(f"   ⚠️  '{' '.join(args)}' failed: {result.stderr.strip()}")
            if configure_ok:
                print(
                    f"   ✅ AWS CLI configured: profile={aws_profile}, region={aws_region}"
                )
        else:
            print("   ℹ️  AWS CLI not found - writing ~/.aws/config directly")
            write_aws_profile(aws_profile, aws_region)

        config_data = {
            "AWS_PROFILE": aws_profile,
            "AWS_REGION": aws_region,
            "TELEMETRY_ENABLED": False,
            "INSTALLATION_ID": os.getenv("CI_COMMIT_SHORT_SHA", "local-dev"),
        }
        config_file.write_text(json.dumps(config_data, indent=2))
        print(f"   AWS Profile: {config_data['AWS_PROFILE']}")
        print(f"   AWS Region: {config_data['AWS_REGION']}")
        print(f"   Installation ID: {config_data['INSTALLATION_ID']}")

        # Install prerequisites non-interactively. `srt config` builds a managed
        # venv and pip-installs all five scanners into it; a COLD install (no pip
        # cache) of checkov+semgrep+jupyter+syft+bandit routinely needs >10 min,
        # so give it a generous ceiling. Retry once with --reinstall-prerequisites
        # if any scanner is missing afterward (partial install / transient PyPI).
        # `yes ''` feeds Enter to any interactive prompt so it never blocks.
        config_timeout = (
            900  # 15 min/attempt; CI job timeout is 50 min (.gitlab-ci.yml)
        )
        attempts = [
            f"yes '' | timeout {config_timeout} ./srt config",
            # Force reinstall on the retry to repair a partially-populated venv.
            f"yes '' | timeout {config_timeout} ./srt config --reinstall-prerequisites",
        ]
        for attempt, cmd in enumerate(attempts, 1):
            print(
                f"   Installing SRT prerequisites (attempt {attempt}/{len(attempts)}, "
                "this may take several minutes)..."
            )
            print(f"   Running: {cmd}")
            result = subprocess.run(
                cmd,
                shell=True,  # nosec B602 nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true - hardcoded pipeline, no user input
                cwd=srt_dir,
                capture_output=True,
                text=True,
                check=False,
            )

            # Log detailed output for debugging (rc 124 == timeout killed it)
            print(f"   Return code: {result.returncode}")
            if result.stdout:
                print("   stdout (last 1500 chars):")
                print(f"   {result.stdout[-1500:]}")
            if result.stderr:
                print("   stderr (last 1500 chars):")
                print(f"   {result.stderr[-1500:]}")

            # The ONLY reliable success signal is that every scanner executable
            # now exists in SRT's venv — `srt config`'s exit code / banner is not
            # trustworthy (it prints a success banner even when a tool failed).
            missing = missing_scanners(srt_dir)
            if not missing:
                print(f"   ✅ All required scanners present: {REQUIRED_SCANNERS}")
                break
            print(f"   ⚠️  Missing scanners after attempt {attempt}: {missing}")
        else:
            # Exhausted retries with scanners still missing. `srt assess` will
            # abort with "Prerequisites not installed."; fail loudly HERE so the
            # setup step surfaces the real cause instead of a misleading
            # "✅ setup complete" followed by a cryptic scan failure.
            msg = (
                f"SRT prerequisite scanners still missing after {len(attempts)} "
                f"attempts: {missing_scanners(srt_dir)}. "
                "`srt assess` requires all of "
                f"{REQUIRED_SCANNERS} in .srt/.venv/bin/. See the './srt config' "
                "output above for the pip/venv failure."
            )
            print(f"   ❌ {msg}")
            # This block only runs under `if is_ci:`, and in CI a missing scanner
            # MUST fail the setup step — the scan cannot run without it. Fail here
            # rather than emit a misleading "✅ setup complete".
            sys.exit(1)
    else:
        # Interactive configuration for local development
        if not config_file.exists():
            print("\nConfiguring SRT...")
            print("Please follow the prompts to configure SRT with your AWS settings.")

            result = subprocess.run(["./srt", "config"], cwd=srt_dir, check=False)
            if result.returncode != 0:
                print(
                    "⚠️  SRT configuration incomplete. You can run 'cd .srt && ./srt config' later."
                )
            else:
                print("✅ SRT configuration complete!")
        else:
            print(
                "✅ SRT already configured (run 'cd .srt && ./srt config' to reconfigure)"
            )

    # Copy latest issues.json from scripts/srt to .srt (restore suppressions)
    issues_source = Path(__file__).parent / "issues.json"
    issues_target = srt_dir / "issues.json"

    if issues_source.exists():
        shutil.copy2(issues_source, issues_target)
        print("✅ Copied latest issues.json to .srt/ (restored suppressions)")
    else:
        print("ℹ️  No existing issues.json found - this is a fresh SRT setup")

    print("\n✅ SRT setup complete!")
    print(f"Binary location: {srt_executable}")
    if not is_ci:
        print("\nNext steps:")
        print("  - Run assessment: make srt-scan")
        print("  - Interactive fix: make srt-fix")


if __name__ == "__main__":
    main()
