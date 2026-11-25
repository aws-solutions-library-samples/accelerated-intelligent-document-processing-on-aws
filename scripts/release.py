#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Release develop: merge to main, tag, publish the artifacts, create the GitHub Release.

Run from the repository root, on a develop that prep-develop-for-release has
prepared (VERSION final, CHANGELOG section cut, solution profile reviewed, pushed
to both remotes).

Two phases, because the publish needs credentials for the publishing account,
which are pasted into the shell between them:

    make release                      # phase "git": preflight, merge, tag, push
    make release PHASE=publish        # phase "publish": check account, publish,
                                      #   draft release, verify, publish release
    make release PHASE=post           # rerun only the last steps: docs-deploy from
                                      #   the tag, then develop to <next>.dev1
    make release DRY_RUN=1            # print the plan and run the read-only checks

Phase "publish" ends by running phase "post" (make docs-deploy, then fast-forward
develop to main, make version V=<x.y.z+1>.dev1, commit and push to both remotes).

Phase "publish" reads the expected account id from RELEASE_ACCOUNT_ID (or
--account). It is never written into this file, because this repository is
public and scripts/check_account_ids.py refuses a committed account id.

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


def preflight_publish(version: str, account: str | None) -> None:
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
    if not account:
        raise ReleaseError(
            "Set RELEASE_ACCOUNT_ID (or pass --account) to the publishing account id."
        )
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
        if ident["Account"] != account:
            raise ReleaseError(
                f"Credentials are for account {ident['Account']}, not {account}. Paste the publishing account's "
                "temporary credentials (environment variables beat AWS_PROFILE) and rerun with PHASE=publish."
            )
        say(
            f"  ok: credentials are for the publishing account ({ident['Arn'].split('/')[-2] if '/' in ident['Arn'] else ident['Arn']})"
        )
    release_notes(version)


# ---------------------------------------------------------------------- phases
def confirm(version: str, what: str, assume_yes: bool) -> None:
    if DRY_RUN or assume_yes:
        return
    answer = input(f"\n{what}\nType {version} to proceed: ").strip()
    if answer != version:
        raise ReleaseError("Not confirmed; nothing was changed by this step.")


def phase_git(version: str, assume_yes: bool) -> None:
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
    say(
        f"\nPhase git done. HEAD is main at {tag}.\n"
        "Next: paste the publishing account's temporary credentials into this shell, then\n"
        "  RELEASE_ACCOUNT_ID=<id> make release PHASE=publish"
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


def phase_publish(version: str, account: str | None, assume_yes: bool) -> None:
    preflight_publish(version, account)
    tag = f"v{version}"
    confirm(
        version,
        f"About to publish {tag} publicly with {RELEASE_SCRIPT.name} and create the GitHub Release {tag} (draft until verified).",
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
        rc = publisher.wait()
        log.close()
        say(f"  publish finished in {int(time.time() - started) // 60} min, exit {rc}")
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
    global DRY_RUN
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--phase", choices=("git", "publish", "post"), default="git")
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
        "--account",
        default=os.environ.get("RELEASE_ACCOUNT_ID"),
        help="publishing account id",
    )
    a = p.parse_args()
    DRY_RUN = a.dry_run
    try:
        version = read_version()
        say(f"Release v{version}, phase {a.phase}{' (dry run)' if DRY_RUN else ''}")
        if a.phase == "git":
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
