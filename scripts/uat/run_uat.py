#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""End-to-end UI acceptance test (UAT) runner: deploy -> test -> report -> teardown.

WHY THIS EXISTS
---------------
`make api-test` proves the REST API enforces authorization, and the primary CI
suite (steps 3-14) drives the pipeline through idp-cli. Neither renders a page.
Nothing in the repo currently answers "can a user actually accomplish task T in
the UI?" — see scripts/sdlc/docs/CI_TEST_COVERAGE.md, "Browser/e2e UI test".

This script owns the whole lifecycle so a UAT run is one command and leaves
nothing behind:

  1. (optional) publish templates from the working tree
  2. deploy a throwaway stack, or attach to one you already have
  3. resolve ApplicationWebURL from stack outputs
  4. create one Cognito user per role, PERMANENT password (no forced reset)
  5. run Playwright against the deployed UI
  6. collect uat-report.md / uat-results.json / traces into a report dir
  7. ALWAYS tear down: delete test users, then delete the stack

TEARDOWN IS UNCONDITIONAL. Steps 4-6 run inside try/finally so a crashed or
interrupted run still removes the users and the stack. --keep opts out, and is
the only way to leave resources behind.

SAFETY
------
* --keep is required to preserve anything. Otherwise the stack this script
  CREATED is deleted on exit.
* A stack passed via --stack-name is NEVER deleted: you own its lifecycle. Only
  the test users this script created are removed.
* Test users are <prefix>-<role>@example.invalid (RFC 2606 .invalid can never be
  a real address) with a random per-run password.

USAGE
-----
  # full cycle: deploy a throwaway stack, test, tear down
  python3 scripts/uat/run_uat.py --admin-email me@example.com --region us-west-2

  # against a stack you already have (not deleted afterwards)
  python3 scripts/uat/run_uat.py --stack-name my-idp-stack

  # against a URL only; no AWS calls at all, users must already exist
  UAT_ADMIN_USER=... UAT_ADMIN_PASSWORD=... \
    python3 scripts/uat/run_uat.py --base-url https://d123.cloudfront.net/

Requires: awscli v2, node >= 22, and AWS credentials unless --base-url is used.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess  # nosec B404 - all argv lists, no shell
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = REPO_ROOT / "src" / "ui" / "e2e"

# rbac_common.py already owns stack resolution and Cognito user lifecycle for
# `make api-test`. Reusing it means the two harnesses cannot drift on stack shape.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

ROLES = ["Admin", "Viewer"]  # keep in step with src/ui/e2e/fixtures/roles.ts


def log(msg: str) -> None:
    print(f"[uat] {msg}", flush=True)


def run(argv: list[str], cwd: Path | None = None, env: dict | None = None) -> int:
    log("$ " + " ".join(argv))
    return subprocess.call(argv, cwd=cwd, env=env)  # nosec B603 - argv list, no shell


def check(argv: list[str], cwd: Path | None = None) -> str:
    return subprocess.check_output(  # nosec B603 - argv list, no shell
        argv, cwd=cwd, text=True
    ).strip()


def stack_output(stack: str, region: str, key: str) -> str:
    return check(
        [
            "aws",
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack,
            "--region",
            region,
            "--query",
            f"Stacks[0].Outputs[?OutputKey=='{key}'].OutputValue",
            "--output",
            "text",
            "--no-cli-pager",
        ]
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--stack-name", help="Test an EXISTING stack (never deleted by this script)"
    )
    p.add_argument(
        "--base-url",
        help="Skip AWS entirely and test this URL (users must already exist)",
    )
    p.add_argument(
        "--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    )
    p.add_argument("--admin-email", help="Required when self-deploying")
    p.add_argument("--template-url", help="Published template URL (skips --publish)")
    p.add_argument(
        "--publish",
        action="store_true",
        help="Publish templates from the working tree first",
    )
    p.add_argument("--bucket-basename", help="S3 bucket basename for --publish")
    p.add_argument("--report-dir", default=str(REPO_ROOT / "scratch" / "uat-report"))
    p.add_argument(
        "--keep", action="store_true", help="Do NOT tear down (leaves stack + users)"
    )
    p.add_argument("--grep", help="Only run scenarios matching this pattern")
    p.add_argument("--headed", action="store_true", help="Run with a visible browser")
    p.add_argument(
        "--skip-browser-install",
        action="store_true",
        help="Assume the Chromium build Playwright wants is already cached",
    )
    return p.parse_args()


def npm_setup(skip_browser: bool) -> None:
    if not shutil.which("npm"):
        sys.exit("[uat] ERROR: npm not found on PATH (need Node >= 22.12).")
    log("installing e2e dependencies...")
    if run(["npm", "install", "--no-audit", "--no-fund"], cwd=E2E_DIR) != 0:
        sys.exit("[uat] ERROR: npm install failed in src/ui/e2e")
    if not skip_browser:
        log("installing Chromium for Playwright...")
        # Not fatal: a cached browser of the right build may already be present,
        # and Playwright will say so clearly if it is not.
        run(["npx", "playwright", "install", "chromium"], cwd=E2E_DIR)


def new_stack_name() -> str:
    return f"idp-uat-{time.strftime('%m%d-%H%M%S')}"


def deploy_stack(args: argparse.Namespace, stack: str) -> None:
    """Deploy `stack`. The caller must record the name BEFORE calling this, so a
    failed/partial deploy is still torn down rather than orphaned."""
    if not args.admin_email:
        sys.exit("[uat] ERROR: --admin-email is required when self-deploying.")

    template_url = args.template_url
    if args.publish and not template_url:
        log("publishing templates from the working tree...")
        cmd = ["make", "publish", f"REGION={args.region}", "HEADLESS=1"]
        if args.bucket_basename:
            cmd.append(f"BUCKET_BASENAME={args.bucket_basename}")
        if run(cmd, cwd=REPO_ROOT) != 0:
            sys.exit("[uat] ERROR: publish failed")

    log(f"deploying stack {stack} (this takes ~30-70 min)...")
    cmd = [
        "make",
        "deploy",
        f"STACK_NAME={stack}",
        f"REGION={args.region}",
        f"ADMIN_EMAIL={args.admin_email}",
        "HEADLESS=1",
    ]
    if template_url:
        cmd.append(f"TEMPLATE_URL={template_url}")
    else:
        cmd.append("FROM_CODE=1")
    if run(cmd, cwd=REPO_ROOT) != 0:
        # The caller already recorded the stack name, so its finally block tears
        # down whatever CloudFormation managed to create.
        log(f"ERROR: deploy failed for {stack}; it will still be torn down")
        raise RuntimeError(f"deploy failed for {stack}")


def delete_stack(stack: str, region: str) -> None:
    log(f"deleting stack {stack}...")
    run(
        [
            "make",
            "delete-stack",
            f"STACK_NAME={stack}",
            f"REGION={region}",
            "FORCE=1",
            "EMPTY_BUCKETS=1",
            "FORCE_DELETE_ALL=1",
        ],
        cwd=REPO_ROOT,
    )


def main() -> int:
    args = parse_args()
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    created_stack: str | None = None
    ctx = None
    users: list[str] = []
    env = os.environ.copy()
    exit_code = 1

    try:
        # ---- resolve target -------------------------------------------------
        if args.base_url:
            base_url = args.base_url
            log(f"using --base-url {base_url}; no AWS calls, no user provisioning")
            for role in ROLES:
                if not env.get(f"UAT_{role.upper()}_USER"):
                    sys.exit(
                        f"[uat] ERROR: --base-url requires UAT_{role.upper()}_USER and "
                        f"UAT_{role.upper()}_PASSWORD to be exported."
                    )
        else:
            if args.stack_name:
                stack = args.stack_name
                log(f"attaching to existing stack {stack} (will NOT be deleted)")
            else:
                # Record the name FIRST: if the deploy fails half-way, the finally
                # block must still be able to delete the partial stack.
                stack = new_stack_name()
                created_stack = stack
                deploy_stack(args, stack)

            base_url = stack_output(stack, args.region, "ApplicationWebURL")
            if not base_url or base_url == "None":
                raise RuntimeError(
                    f"could not read ApplicationWebURL from stack {stack}"
                )
            log(f"target UI: {base_url}")
            env["UAT_STACK_NAME"] = stack

            # ---- provision users -------------------------------------------
            from rbac_common import (
                create_cognito_user,
                delete_cognito_user,
                resolve_stack,
            )

            ctx = resolve_stack(stack, args.region)
            prefix = f"uat-{secrets.token_hex(3)}"
            # "Aa1!" guarantees the Cognito password policy classes regardless of
            # what token_urlsafe produces.
            password = "Aa1!" + secrets.token_urlsafe(24)
            for role in ROLES:
                email = f"{prefix}-{role.lower()}@example.invalid"
                log(f"creating {role} test user {email}")
                create_cognito_user(ctx, email, role, password)
                users.append(email)
                env[f"UAT_{role.upper()}_USER"] = email
                env[f"UAT_{role.upper()}_PASSWORD"] = password

        env["UAT_BASE_URL"] = base_url

        # ---- run ------------------------------------------------------------
        npm_setup(args.skip_browser_install)
        pw = ["npx", "playwright", "test"]
        if args.grep:
            pw += ["--grep", args.grep]
        if args.headed:
            pw.append("--headed")
        exit_code = run(pw, cwd=E2E_DIR, env=env)
        log(f"playwright exited {exit_code}")

        # ---- collect --------------------------------------------------------
        for src_name in ("test-results", "playwright-report"):
            src = E2E_DIR / src_name
            if src.exists():
                dst = report_dir / src_name
                shutil.rmtree(dst, ignore_errors=True)
                shutil.copytree(src, dst)
        summary = report_dir / "test-results" / "uat-report.md"
        if summary.exists():
            print("\n" + summary.read_text())
        log(f"report artifacts: {report_dir}")

    finally:
        # ---- teardown (always) ---------------------------------------------
        if args.keep:
            log("--keep set: leaving stack and test users in place")
            if created_stack:
                log(
                    f"  stack: {created_stack} (delete with: make delete-stack STACK_NAME={created_stack} FORCE=1)"
                )
        else:
            if ctx and users:
                from rbac_common import delete_cognito_user

                for email in users:
                    log(f"deleting test user {email}")
                    delete_cognito_user(ctx, email)  # best-effort, never raises
            if created_stack:
                delete_stack(created_stack, args.region)
            elif args.stack_name:
                log(f"stack {args.stack_name} was pre-existing; left untouched")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
