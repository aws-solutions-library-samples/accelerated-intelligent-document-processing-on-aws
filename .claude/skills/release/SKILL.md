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

| Phase | Command | Steps |
|---|---|---|
| `git` | `make release` | Preflight: `VERSION` final, tree clean, `develop` and `main` identical locally and on both remotes, `main` an ancestor of `develop`, no `v<x.y.z>` tag or GitHub Release yet, the CHANGELOG section's Templates block naming every Region `scripts/aws-release.sh` publishes, and `make solution-profile-check` passing. Then it merges `develop` into `main` with `--no-ff` and the message `Merge branch 'develop' v<x.y.z>`, tags `v<x.y.z>`, and pushes `main` and the tag to both remotes |
| `publish` | `make release PHASE=publish CONFIRM=<x.y.z>` | Preflight: HEAD is the tag on both remotes, tree clean, `idp-cli --version` matches. The confirmation names the account and role from `aws sts get-caller-identity`, so confirming the version confirms the account too; with `RELEASE_ACCOUNT_ID` set, a different account is refused. Then it runs `scripts/aws-release.sh` (log in `scratch/release-<x.y.z>-publish.log`). While that runs it creates the GitHub Release as a **draft**, titled `v<x.y.z>`, with the CHANGELOG section from its heading through its Templates block as the notes. It then verifies, without credentials, every Region's versioned template, floating template, `idp-main-latest.json` and template `Description`, and only then publishes the release as latest. After that it runs phase `post` |
| `post` | `make release PHASE=post` | `make docs-deploy` from the tag. Then it switches to `develop`, fast-forwards it to `main`, runs `make version V=<x.y.z+1>.dev1`, commits `Start <x.y.z+1>.dev1 development`, and pushes `develop` to both remotes. `SKIP_DOCS=1` or `SKIP_BUMP=1` leaves out one half |

`DRY_RUN=1` prints every command and runs only the read-only checks; with a dirty tree or
before the tag exists it reports those as warnings instead of stopping. Each phase asks
the user to type the version before its first public change. Without a terminal, which
includes the Bash tool and `!` commands, pass `CONFIRM=<x.y.z>` instead.

## Procedure

1. **Dry run.** `make release DRY_RUN=1`. Report any `STOPPED:` line and stop there.
2. **Phase `git`.** The script asks for the version to be typed at a terminal. Neither
   the Bash tool nor a `!` command has one, so pass the version instead:
   `make release CONFIRM=<x.y.z>`. The user can run that as `! make release CONFIRM=<x.y.z>`;
   run it yourself only if the user's request said to go ahead.
3. **Credentials.** Ask the user to paste the publishing account's temporary
   credentials into the session (`! export AWS_ACCESS_KEY_ID=...` and the rest). Before
   confirming, read the account and role the preflight prints, and stop if they are not
   the publishing account. Never write the account id into a tracked file: the repository is public and `scripts/check_account_ids.py` refuses
   it. Environment credentials take precedence over `AWS_PROFILE`, which is what the
   account check guards.
4. **Phase `publish`.** The publish takes a long time, so run it in the background and
   wait for the notification rather than polling:
   `make release PHASE=publish CONFIRM=<x.y.z>`.
5. **Report.** Give the release URL (`gh release view v<x.y.z> --json url`), the
   verification result per Region, the docs deploy result, and the new `develop`
   version and commit. Check that the *Solution profile release asset* workflow
   started (`gh run list --workflow solution-profile-release.yml --limit 1`). Its
   tarball is attached a few minutes after the release is published.
6. **Hand over** the live validation battery, which this skill does not run:
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
