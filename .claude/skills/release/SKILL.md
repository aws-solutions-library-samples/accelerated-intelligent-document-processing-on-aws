---
name: release
description: "Use for 'release <x.y.z>' or 'ship the release' after prep-develop-for-release: merge develop to main, tag, publish the artifacts, create the GitHub Release, deploy the docs and start the next cycle on develop, with make release."
---

# Release — GenAI IDP Accelerator

Use this skill once [prep-develop-for-release](../prep-develop-for-release/SKILL.md) has
been done and committed, and `develop` is pushed to both remotes. It drives
`scripts/release.py` (`make release`), which does the mechanical steps of
[docs/release-runbook.md](../../../docs/release-runbook.md) in a fixed order and checks
each step's preconditions first. The script is the authority. This file says when to
run each phase and what to tell the user.

Trigger phrases: "release 0.6.17", "ship the release", "do the release now".

## What `make release` does

Run it from a terminal that already holds the publishing account's temporary
credentials. `make release CONFIRM=<x.y.z>` (phase `all`, the default) runs every check
before it changes anything, including `aws sts get-caller-identity`, so wrong or expired
credentials stop the release before the merge. It then asks once, naming the version,
the account and the role, and runs the phases below in order. Each phase can also be
run on its own with `PHASE=`, which is how a stopped release is resumed.

| Phase | Command | Steps |
|---|---|---|
| `git` | `make release PHASE=git` | Preflight: `VERSION` final, tree clean, `develop` and `main` identical locally and on both remotes, `main` an ancestor of `develop`, no `v<x.y.z>` tag or GitHub Release yet, the CHANGELOG section's Templates block naming every Region `scripts/aws-release.sh` publishes, and `make solution-profile-check` passing. Then it merges `develop` into `main` with `--no-ff` and the message `Merge branch 'develop' v<x.y.z>`, tags `v<x.y.z>`, and pushes `main` and the tag to both remotes |
| `publish` | `make release PHASE=publish` | Preflight: HEAD is the tag on both remotes, tree clean, `idp-cli --version` matches, and the account and role of the credentials, which the confirmation names. With `RELEASE_ACCOUNT_ID` set, a different account is refused. Then it runs `scripts/aws-release.sh` (log in `scratch/release-<x.y.z>-publish.log`), printing each Region's start and finish with its duration, a heartbeat every minute with the publisher's latest status line, and a warning after five minutes with no output. While that runs it creates the GitHub Release as a **draft**, titled `v<x.y.z>`, with the CHANGELOG section from its heading through its Templates block as the notes. It then verifies, without credentials, every Region's versioned template, floating template, `idp-main-latest.json` and template `Description`, and only then publishes the release as latest. After that it runs phase `post` |
| `post` | `make release PHASE=post` | `make docs-deploy` from the tag. Then it switches to `develop`, fast-forwards it to `main`, runs `make version V=<x.y.z+1>.dev1`, commits `Start <x.y.z+1>.dev1 development`, and pushes `develop` to both remotes. `SKIP_DOCS=1` or `SKIP_BUMP=1` leaves out one half |

`DRY_RUN=1` prints every command and runs only the read-only checks; with a dirty tree or
before the tag exists it reports those as warnings instead of stopping. At a terminal the
confirmation can be typed instead of passing `CONFIRM=`; the Bash tool and `!` commands
have no terminal, so they must pass it.

The publish took 17 minutes for 0.6.16: about 6 for the first Region, which builds, and
3 to 5 for each later one, which reuses that build and only uploads. Isengard
credentials can expire during a long run; start with a fresh session.

## Procedure

1. **Dry run.** `make release DRY_RUN=1`. Report any `STOPPED:` line and stop there.
2. **Credentials.** Each `!` command and each Bash tool call starts a new shell, so
   exported credentials do not carry over. Recommend the user runs the release in their
   own terminal: paste the credentials, then `make release CONFIRM=<x.y.z>`. To run it
   from the session instead, ask them to write the export lines to
   `scratch/release-creds.env` from a terminal (`umask 077; cat > scratch/release-creds.env`),
   run `source scratch/release-creds.env && make release CONFIRM=<x.y.z>` in the
   background, and delete the file afterwards. Never ask for credentials in the chat,
   and never write the account id into a tracked file: the repository is public and
   `scripts/check_account_ids.py` refuses it.
3. **Check the account** the preflight prints before the confirmation is given, and stop
   if it is not the publishing account.
4. **Report.** Give the release URL (`gh release view v<x.y.z> --json url`), the
   per-Region durations and verification results, the docs deploy result, and the new
   `develop` version and commit. Check that the *Solution profile release asset* workflow
   succeeded (`gh run list --workflow solution-profile-release.yml --limit 1`) and that
   the release carries its tarball and `.sha256`. New docs pages return 404 until the
   GitHub Pages build that `docs-deploy` starts has finished, a few minutes later.
5. **Hand over** the live validation battery, which this skill does not run:
   [release-validation](../release-validation/SKILL.md).

## When a phase stops

Every stop prints `STOPPED:` with the reason, and nothing after the failing step has
run.

- **A preflight check fails.** Nothing was changed. Fix the cause and rerun the phase.
- **`aws-release.sh` fails.** The GitHub Release stays a draft. Read the log's last
  lines, fix the cause, and rerun `PHASE=publish`: it reuses the draft and republishes
  every Region, which overwrites the same keys.
- **Verification fails.** The release stays a draft. Report each failing Region. Do not
  publish the release by hand until every check passes.
- **`docs-deploy` or the `develop` bump fails.** The release is already out. Rerun
  `PHASE=post`, with `SKIP_DOCS=1` if the docs were deployed.

Do not delete or move a pushed tag, and do not force-push `main` or `develop`. If a
published release turns out to be broken, follow section 3.2 of the runbook.
