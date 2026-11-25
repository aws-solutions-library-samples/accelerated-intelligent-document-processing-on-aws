#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Release develop: merge to main, tag, publish the artifacts, create the GitHub Release.

Run from the repository root, on a develop that prep-develop-for-release has
prepared (VERSION final, CHANGELOG section cut, solution profile reviewed, pushed
to both remotes).

Paste the publishing account's temporary credentials into the shell first, then

    make release CONFIRM=<x.y.z>      # everything: all checks first (including the
                                      #   credentials), then merge, tag, push, publish,
                                      #   draft release, verify, publish release,
                                      #   docs-deploy, develop to <x.y.z+1>.dev1
    make release DRY_RUN=1            # print the plan and run the read-only checks

To resume after a failure, run one phase on its own:

    make release PHASE=git            # preflight, merge, tag, push
    make release PHASE=publish        # check account, publish, draft release,
                                      #   verify, publish release, then phase post
    make release PHASE=post           # docs-deploy from the tag, then develop to
                                      #   <next>.dev1

While the artifacts upload, the script prints each Region's start and end, a
heartbeat every minute with the publisher's latest status line, and a warning if
the publisher has written nothing for five minutes.

The confirmation names the account and role the credentials belong to, so
CONFIRM=<x.y.z> also confirms the account. RELEASE_ACCOUNT_ID (or
--account), when set, makes that a strict check instead. The id is never written
into this file, because this repository is public and scripts/check_account_ids.py
refuses a committed account id.

The GitHub Release is created as a draft while the artifacts upload, and is
published only once every template URL in its notes resolves without
credentials, so a public release never points at a 404. Publishing it starts the
solution-profile-release workflow, which attaches the profile tarball.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTES = ("origin", "github")
RELEASE_SCRIPT = ROOT / "scripts" / "aws-release.sh"
LOG_DIR = ROOT / "scratch"
FINAL = re.compile(r"^\d+\.\d+\.\d+$")

DRY_RUN = False
CONFIRMED = ""


class ReleaseError(Exception):
    pass


def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], *, mutate: bool = False, check: bool = True, env=None) -> str:
    """Run a command. A mutating command is only printed under --dry-run."""
    shown = " ".join(shlex.quote(c) for c in cmd)
    if mutate:
        say(f"  $ {shown}")
        if DRY_RUN:
            return ""
    proc = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, env=env or os.environ.copy()
    )
    if check and proc.returncode != 0:
        raise ReleaseError(
            f"`{shown}` failed ({proc.returncode}):\n{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def git(*args: str, mutate: bool = False, check: bool = True) -> str:
    env = os.environ.copy()
    if mutate:
        # The shared-branch guards refuse direct writes to main and develop; a
        # release is the deliberate exception, made by this script only.
        env["ALLOW_SHARED_BRANCH"] = "1"
    return run(["git", *args], mutate=mutate, check=check, env=env)


# --------------------------------------------------------------------- inputs
def read_version() -> str:
    v = (ROOT / "VERSION").read_text().strip()
    if not FINAL.match(v):
        raise ReleaseError(
            f"VERSION is {v!r}, not a final X.Y.Z. Run prep-develop-for-release first."
        )
    return v


def release_notes(version: str) -> str:
    """The CHANGELOG section for the version, from its heading through its Templates block."""
    lines = (ROOT / "CHANGELOG.md").read_text().split("\n")
    head = f"## [{version}]"
    try:
        start = lines.index(head)
    except ValueError:
        raise ReleaseError(
            f"CHANGELOG.md has no `{head}` section. Cut it first."
        ) from None
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")),
        len(lines),
    )
    body = "\n".join(lines[start:end]).rstrip() + "\n"
    if "## Templates" not in body:
        raise ReleaseError(f"The {version} section has no `## Templates` block.")
    return body


def publish_targets() -> list[tuple[str, str, str]]:
    """(region, bucket basename, prefix) for each publish line in aws-release.sh."""
    targets = []
    for line in RELEASE_SCRIPT.read_text().splitlines():
        if not line.strip().startswith("idp-cli publish"):
            continue
        args = shlex.split(line)

        def opt(name: str) -> str:
            return args[args.index(name) + 1]

        targets.append((opt("--region"), opt("--bucket-basename"), opt("--prefix")))
    if not targets:
        raise ReleaseError(f"No `idp-cli publish` lines in {RELEASE_SCRIPT}.")
    return targets


def base_url(region: str, basename: str, prefix: str) -> str:
    return f"https://s3.{region}.amazonaws.com/{basename}-{region}/{prefix}"


# ------------------------------------------------------------------ preflight
def check_changelog_urls(version: str, notes: str) -> None:
    for region, basename, prefix in publish_targets():
        url = f"{base_url(region, basename, prefix)}/idp-main_{version}.yaml"
        if url not in notes:
            raise ReleaseError(
                f"The {version} Templates block lacks the {region} URL the publish will write:\n  {url}"
            )
    say("  ok: the Templates block names every Region aws-release.sh publishes")


def preflight_git(version: str) -> None:
    say("Preflight (git)")
    if git("status", "--porcelain"):
        if not DRY_RUN:
            raise ReleaseError("The working tree is not clean.")
        say("  WARNING: the working tree is not clean (a real run stops here)")
    for remote in REMOTES:
        git(
            "fetch", "--quiet", remote
        )  # raises if a fetch fails, so a stale ref cannot pass
    for branch in ("develop", "main"):
        shas = {
            ref: git("rev-parse", ref)
            for ref in (branch, *(f"{r}/{branch}" for r in REMOTES))
        }
        if len(set(shas.values())) != 1:
            detail = "\n".join(f"    {k}: {v}" for k, v in shas.items())
            raise ReleaseError(
                f"`{branch}` differs between local and the remotes; sync it first:\n{detail}"
            )
    say("  ok: develop and main match locally and on both remotes")
    if (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", "main", "develop"], cwd=ROOT
        ).returncode
        != 0
    ):
        raise ReleaseError(
            "main has commits develop lacks; merge main into develop first."
        )
    tag = f"v{version}"
    if git("tag", "--list", tag):
        raise ReleaseError(f"Tag {tag} already exists locally.")
    for remote in REMOTES:
        if git("ls-remote", "--tags", remote, f"refs/tags/{tag}"):
            raise ReleaseError(f"Tag {tag} already exists on {remote}.")
    say(f"  ok: {tag} exists nowhere yet")
    notes = release_notes(version)
    check_changelog_urls(version, notes)
    if (
        subprocess.run(
            ["gh", "release", "view", tag], cwd=ROOT, capture_output=True
        ).returncode
        == 0
    ):
        raise ReleaseError(f"A GitHub Release {tag} already exists.")
    profile = subprocess.run(
        ["make", "--no-print-directory", "solution-profile-check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if profile.returncode != 0:
        raise ReleaseError(
            f"make solution-profile-check fails:\n{profile.stdout[-1500:]}"
        )
    say("  ok: solution profile reviewed for this version")


def check_credentials(account: str | None) -> str:
    """Name the account and role of the credentials in the environment."""
    identity = "(dry run: no credentials checked)"
    if (
        DRY_RUN
        and not os.environ.get("AWS_ACCESS_KEY_ID")
        and not os.environ.get("AWS_PROFILE")
    ):
        say(
            "  (dry run: no credentials in the environment; the account check would run here)"
        )
    else:
        ident = json.loads(
            run(["aws", "sts", "get-caller-identity", "--output", "json"])
        )
        identity = f"account {ident['Account']} as {ident['Arn']}"
        if account and ident["Account"] != account:
            raise ReleaseError(
                f"Credentials are for account {ident['Account']}, not RELEASE_ACCOUNT_ID {account}. "
                "Paste the publishing account's temporary credentials (environment variables beat "
                "AWS_PROFILE) and rerun."
            )
        say(
            f"  ok: credentials are for the publishing account ({identity})"
            if account
            else f"  credentials: {identity} (set RELEASE_ACCOUNT_ID to check it strictly)"
        )
    return identity


def preflight_publish(version: str, account: str | None) -> str:
    say("Preflight (publish)")
    tag = f"v{version}"
    problems = []
    tag_sha = git("rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}", check=False)
    if not tag_sha:
        problems.append(f"Tag {tag} does not exist. Run phase git first.")
    else:
        problems += [
            f"Tag {tag} is not on {r}."
            for r in REMOTES
            if not git("ls-remote", "--tags", r, f"refs/tags/{tag}")
        ]
        if git("rev-parse", "HEAD") != tag_sha:
            problems.append(
                f"HEAD is not {tag}; check out main at the tag (the publisher ships the working tree)."
            )
    if git("status", "--porcelain"):
        problems.append(
            "The working tree is not clean; the publisher would ship the changes."
        )
    if problems and not DRY_RUN:
        raise ReleaseError("\n".join(problems))
    for prob in problems:
        say(f"  WARNING: {prob} (a real run stops here)")
    if not problems:
        say(f"  ok: HEAD is {tag} on both remotes, tree clean")
    cli = run(["idp-cli", "--version"], check=False)
    if not cli.endswith(version):
        raise ReleaseError(
            f"idp-cli reports {cli!r}, not {version}: it is installed from another checkout. Run `make setup`."
        )
    say(f"  ok: {cli}")
    identity = check_credentials(account)
    release_notes(version)
    return identity


# ---------------------------------------------------------------------- phases
def confirm(version: str, what: str, assume_yes: bool) -> None:
    """Require the version typed at a prompt, or given as --confirm / CONFIRM=."""
    if DRY_RUN or assume_yes or CONFIRMED == version:
        return
    if CONFIRMED:
        raise ReleaseError(
            f"CONFIRM={CONFIRMED} does not match VERSION {version}; nothing was changed."
        )
    if not sys.stdin.isatty():
        raise ReleaseError(
            f"{what}\nNo terminal to confirm at; nothing was changed. Rerun with CONFIRM={version} "
            "(make release CONFIRM=" + version + ")."
        )
    try:
        answer = input(f"\n{what}\nType {version} to proceed: ").strip()
    except EOFError:
        answer = ""
    if answer != version:
        raise ReleaseError("Not confirmed; nothing was changed by this step.")


def phase_git(version: str, assume_yes: bool, standalone: bool = True) -> None:
    if standalone:
        preflight_git(version)
    tag = f"v{version}"
    confirm(
        version,
        f"About to merge develop into main as 'Merge branch 'develop' {tag}', tag {tag}, and push both to {', '.join(REMOTES)}.",
        assume_yes,
    )
    say("Merge and tag")
    git("switch", "main", mutate=True)
    git(
        "merge",
        "--no-ff",
        "develop",
        "-m",
        f"Merge branch 'develop' {tag}",
        mutate=True,
    )
    git("tag", tag, mutate=True)
    for remote in REMOTES:
        git("push", remote, "main", mutate=True)
    for remote in REMOTES:
        git("push", remote, tag, mutate=True)
    if standalone:
        say(
            f"\nPhase git done. HEAD is main at {tag}.\n"
            "Next: paste the publishing account's temporary credentials into this shell, then\n"
            f"  make release PHASE=publish CONFIRM={version}"
        )


def url_status(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - fixed https S3 URLs
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError:
        return 0


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as resp:  # nosec B310 - fixed https S3 URLs
        return resp.read().decode()


def verify_artifacts(version: str) -> list[str]:
    """Unauthenticated checks from section 5 of docs/release-runbook.md. Returns problems."""
    problems = []
    for region, basename, prefix in publish_targets():
        b = base_url(region, basename, prefix)
        for name in (f"idp-main_{version}.yaml", "idp-main.yaml"):
            code = url_status(f"{b}/{name}")
            if code != 200:
                problems.append(f"{region}: {name} returned {code}")
        try:
            latest = json.loads(fetch(f"{b}/idp-main-latest.json"))
            if latest.get("version") != version:
                problems.append(
                    f"{region}: idp-main-latest.json names {latest.get('version')}"
                )
            template = fetch(f"{b}/idp-main_{version}.yaml")
            desc = next(
                (ln for ln in template.splitlines() if ln.startswith("Description:")),
                "",
            )
            if f"(v{version})" not in desc:
                problems.append(
                    f"{region}: template Description does not end with (v{version})"
                )
        except Exception as e:  # noqa: BLE001 - reported, not raised
            problems.append(
                f"{region}: could not read latest pointer or template ({e})"
            )
        say(
            f"  {region}: {'ok' if not any(p.startswith(region) for p in problems) else 'PROBLEM'}"
        )
    return problems


HEARTBEAT_SECONDS = 60
STALL_SECONDS = 300
STATUS_LINE = re.compile(r"^(✅|❌|⚠️|ERROR|Error|Uploaded|Built|Publishing)")


def fmt_secs(seconds: float) -> str:
    m, sec = divmod(int(seconds), 60)
    return f"{m}m{sec:02d}s"


def watch_publish(publisher: subprocess.Popen, log_path: Path, started: float) -> int:
    """Wait for the publisher, printing each Region's start and end, a heartbeat, and stalls."""
    regions = [r for r, _, _ in publish_targets()]
    current: str | None = None
    region_started = started
    last_size, last_growth, last_beat = 0, started, started
    stall_warned = False

    def scan() -> tuple[str | None, str]:
        region, status = None, ""
        for line in log_path.read_text(errors="replace").splitlines():
            if line.startswith("+ idp-cli publish") and "--region " in line:
                region = line.split("--region ", 1)[1].split()[0]
            elif STATUS_LINE.match(line.strip()):
                status = line.strip()
        return region, status

    while True:
        rc = publisher.poll()
        now = time.time()
        region, status = scan()
        if region != current:
            if current is not None:
                say(
                    f"  [{regions.index(current) + 1}/{len(regions)}] {current} done in {fmt_secs(now - region_started)}"
                )
            if region is not None:
                n = regions.index(region) + 1 if region in regions else "?"
                say(f"  [{n}/{len(regions)}] {region} publishing")
            current, region_started = region, now
        size = log_path.stat().st_size
        if size != last_size:
            last_size, last_growth, stall_warned = size, now, False
        elif now - last_growth > STALL_SECONDS and not stall_warned:
            say(
                f"  WARNING: no publisher output for {fmt_secs(now - last_growth)}; it may be stuck (see {log_path.relative_to(ROOT)})"
            )
            stall_warned = True
        if rc is not None:
            if current is not None:
                label = "done" if rc == 0 else f"FAILED (exit {rc})"
                n = regions.index(current) + 1 if current in regions else "?"
                say(
                    f"  [{n}/{len(regions)}] {current} {label} in {fmt_secs(now - region_started)}"
                )
            return rc
        if now - last_beat >= HEARTBEAT_SECONDS:
            say(
                f"  ... {fmt_secs(now - started)} elapsed, {current or 'starting'}: {status[:100] or '(building)'}"
            )
            last_beat = now
        time.sleep(5)


def phase_all(version: str, account: str | None, assume_yes: bool) -> None:
    """Every phase in one run, with every check made before the first change."""
    preflight_git(version)
    say("Preflight (publish)")
    cli = run(["idp-cli", "--version"], check=False)
    if not cli.endswith(version):
        raise ReleaseError(
            f"idp-cli reports {cli!r}, not {version}: it is installed from another checkout. Run `make setup`."
        )
    say(f"  ok: {cli}")
    identity = check_credentials(account)
    tag = f"v{version}"
    confirm(
        version,
        f"About to release {tag}: merge develop into main, tag {tag} and push both to "
        f"{', '.join(REMOTES)}; publish with {RELEASE_SCRIPT.name} using {identity}; create and "
        "publish the GitHub Release; deploy the docs; and start the next version on develop.",
        assume_yes,
    )
    phase_git(version, assume_yes=True, standalone=False)
    phase_publish(version, account, assume_yes=True)


def phase_publish(version: str, account: str | None, assume_yes: bool) -> None:
    identity = preflight_publish(version, account)
    tag = f"v{version}"
    confirm(
        version,
        f"About to publish {tag} publicly with {RELEASE_SCRIPT.name}, using {identity}, "
        f"and create the GitHub Release {tag} (draft until verified).",
        assume_yes,
    )

    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"release-{version}-publish.log"
    say(f"Publish (log: {log_path.relative_to(ROOT)})")
    say(
        f"  $ bash {RELEASE_SCRIPT.relative_to(ROOT)} > {log_path.relative_to(ROOT)} 2>&1"
    )
    publisher = None
    if not DRY_RUN:
        log = open(log_path, "w")  # noqa: SIM115 - closed after wait()
        publisher = subprocess.Popen(
            ["bash", str(RELEASE_SCRIPT)],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    say("Draft GitHub Release (while the publish runs)")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(release_notes(version))
        notes_file = f.name
    existing = subprocess.run(
        ["gh", "release", "view", tag, "--json", "isDraft", "-q", ".isDraft"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0 and existing.stdout.strip() == "true":
        # A rerun after a failed publish: refresh the draft's notes rather than refuse.
        run(
            ["gh", "release", "edit", tag, "--title", tag, "--notes-file", notes_file],
            mutate=True,
        )
    elif existing.returncode == 0:
        raise ReleaseError(f"The GitHub Release {tag} is already published.")
    else:
        run(
            [
                "gh",
                "release",
                "create",
                tag,
                "--draft",
                "--verify-tag",
                "--title",
                tag,
                "--notes-file",
                notes_file,
            ],
            mutate=True,
        )

    if publisher is not None:
        started = time.time()
        rc = watch_publish(publisher, log_path, started)
        log.close()
        say(f"  publish finished in {fmt_secs(time.time() - started)}, exit {rc}")
        if rc != 0:
            tail = "\n".join(log_path.read_text().splitlines()[-30:])
            raise ReleaseError(
                f"aws-release.sh failed (exit {rc}); the release stays a draft. Last lines:\n{tail}\n"
                "Fix the cause and rerun with PHASE=publish; republishing a Region overwrites its keys, "
                "and the existing draft is reused."
            )

    say("Verify (no credentials)")
    if DRY_RUN:
        say(
            "  (dry run: would check every Region's versioned and floating template, latest pointer and Description)"
        )
    else:
        problems = verify_artifacts(version)
        if problems:
            raise ReleaseError(
                "Artifacts are not all public and correct; the release stays a draft:\n  "
                + "\n  ".join(problems)
            )

    say("Publish the GitHub Release")
    run(["gh", "release", "edit", tag, "--draft=false", "--latest"], mutate=True)
    say(
        f"  Released {tag}. The solution-profile-release workflow now attaches the profile tarball."
    )
    phase_post(version)


def next_dev(version: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    return f"{major}.{minor}.{patch + 1}.dev1"


def phase_post(version: str, skip_docs: bool = False, skip_bump: bool = False) -> None:
    """Deploy the docs site from the tag, then start the next cycle on develop."""
    tag = f"v{version}"
    if not skip_docs:
        say("Deploy the documentation site (from the tag)")
        if not DRY_RUN and git("rev-parse", "HEAD") != git(
            "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}", check=False
        ):
            raise ReleaseError(
                f"HEAD is not {tag}; check it out before deploying the docs."
            )
        say("  $ make docs-deploy")
        if not DRY_RUN:
            r = subprocess.run(
                ["make", "--no-print-directory", "docs-deploy"], cwd=ROOT
            )
            if r.returncode != 0:
                raise ReleaseError(
                    "make docs-deploy failed; rerun with PHASE=post (add SKIP_BUMP=1 if develop is already bumped)."
                )
    if not skip_bump:
        dev = next_dev(version)
        say(f"Start the next cycle on develop ({dev})")
        git("switch", "develop", mutate=True)
        git("merge", "--ff-only", "main", mutate=True)
        say(f"  $ make version V={dev}")
        if not DRY_RUN:
            r = subprocess.run(
                ["make", "--no-print-directory", "version", f"V={dev}"], cwd=ROOT
            )
            if r.returncode != 0:
                raise ReleaseError(
                    f"make version V={dev} failed; fix it and rerun with PHASE=post SKIP_DOCS=1."
                )
        git("commit", "-a", "-m", f"Start {dev} development", mutate=True)
        for remote in REMOTES:
            git("push", remote, "develop", mutate=True)
    say("\nDone. Still to run: the live validation battery (release-validation skill).")


def main() -> int:
    global DRY_RUN, CONFIRMED
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--phase", choices=("all", "git", "publish", "post"), default="all")
    p.add_argument(
        "--skip-docs",
        action="store_true",
        help="phase post: do not run make docs-deploy",
    )
    p.add_argument(
        "--skip-bump", action="store_true", help="phase post: do not bump develop"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan; run only read-only checks",
    )
    p.add_argument(
        "--yes", action="store_true", help="skip the type-the-version confirmation"
    )
    p.add_argument(
        "--confirm",
        default="",
        metavar="X.Y.Z",
        help="confirm without a prompt; must equal VERSION",
    )
    p.add_argument(
        "--account",
        default=os.environ.get("RELEASE_ACCOUNT_ID"),
        help="publishing account id",
    )
    a = p.parse_args()
    DRY_RUN = a.dry_run
    CONFIRMED = a.confirm.strip()
    try:
        version = read_version()
        say(f"Release v{version}, phase {a.phase}{' (dry run)' if DRY_RUN else ''}")
        if a.phase == "all":
            phase_all(version, a.account, a.yes)
        elif a.phase == "git":
            phase_git(version, a.yes)
        elif a.phase == "publish":
            phase_publish(version, a.account, a.yes)
        else:
            phase_post(version, a.skip_docs, a.skip_bump)
    except ReleaseError as e:
        say(f"\nSTOPPED: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
