#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Prototype driver for ONE agentic usability worker.

Provisions a Cognito user, signs in deterministically to produce a storageState, runs
the worker, verifies its claim against an out-of-band oracle, then deletes the user.

Sign-in is done deterministically on purpose: logging in is not the feature under test,
and making the agent spend its budget on it would only measure the login form. The
"Welcome to GenAI IDP" interstitial is deliberately NOT dismissed — for a cold run,
landing on it is the genuine first-run experience and part of what we are measuring.

Usage:
  AWS_PROFILE=accelerator python3 scripts/uat/agentic/run_prototype.py \
      --stack-name <your-stack> --region us-west-2 --flow config-discovery [--warm]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser import BrowserSession  # noqa: E402
from flows import ad_hoc, all_flows  # noqa: E402
from report import render  # noqa: E402
from verify import verify_claim  # noqa: E402
from worker import Flow, run_worker  # noqa: E402


def log(msg: str) -> None:
    print(f"[agentic-uat] {msg}", flush=True)


async def _sign_in_async(base_url: str, user: str, password: str, out: Path) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    async with BrowserSession(base_url) as sess:
        page = sess.page
        assert page is not None
        await page.goto("./", wait_until="domcontentloaded")
        await page.get_by_label("Username").first.fill(user)
        await page.get_by_label("Password").first.fill(password)
        await page.get_by_role("button", name="Sign in").first.click()
        try:
            # The identity chip in the banner is the proof of auth. NOT the nav links:
            # a fresh user lands on the welcome interstitial, which has no nav.
            await (
                page.get_by_role("banner")
                .get_by_role("button")
                .filter(has_text=user)
                .wait_for(state="visible", timeout=60_000)
            )
        except Exception as exc:  # noqa: BLE001
            log(f"ERROR: sign-in did not complete: {str(exc)[:200]}")
            return False
        await sess._ctx.storage_state(path=str(out))  # noqa: SLF001 - intentional
    return True


def sign_in_and_save_state(base_url: str, user: str, password: str, out: Path) -> bool:
    """Deterministic sign-in. Returns False if the form did not authenticate."""
    return asyncio.run(_sign_in_async(base_url, user, password, out))


def main() -> int:
    ap = argparse.ArgumentParser()
    # Not required up front: --list-flows must work without AWS access.
    ap.add_argument("--stack-name")
    ap.add_argument("--region", default="us-west-2")
    # No `choices=`: flow files under flows/*.yaml are discovered at run time, so a new
    # target must not require editing this file.
    ap.add_argument("--flow", help="Flow id (builtin or a flows/*.yaml file)")
    ap.add_argument(
        "--goal",
        help='Ad-hoc target, e.g. --goal "create a test set and run it". '
        "No oracle, so the verdict is `unverified`.",
    )
    ap.add_argument(
        "--list-flows", action="store_true", help="List available flow ids and exit"
    )
    ap.add_argument(
        "--warm", action="store_true", help="Give the agent the documented procedure"
    )
    ap.add_argument("--role", default="Admin")
    ap.add_argument("--max-tool-calls", type=int, default=40)
    ap.add_argument(
        "--max-vision-calls",
        type=int,
        default=6,
        help="Image reads allowed (images cost far more tokens than a11y trees)",
    )
    ap.add_argument("--no-vision", action="store_true", help="Accessibility tree only")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--out", default=str(REPO_ROOT / "scratch" / "agentic-uat"))
    args = ap.parse_args()

    available = all_flows()
    if args.list_flows:
        for fid, spec in sorted(available.items()):
            v = (spec.get("verify") or {}).get("method", "none")
            src = "yaml" if spec.get("docs_ref") else "builtin"
            print(f"  {fid:<38} verify={v:<22} [{src}]")
        return 0
    if not args.stack_name:
        ap.error("--stack-name is required (except with --list-flows)")
    if not args.flow and not args.goal:
        ap.error('give --flow <id>, --goal "...", or --list-flows')
    if args.flow and args.flow not in available:
        ap.error(
            f"unknown flow {args.flow!r}. Available: {', '.join(sorted(available))}"
        )

    from rbac_common import create_cognito_user, delete_cognito_user, resolve_stack

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    log(f"resolving stack {args.stack_name}...")
    ctx = resolve_stack(args.stack_name, args.region)

    import subprocess  # nosec B404 - argv list

    base_url = subprocess.check_output(  # nosec B603
        [
            "aws",
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            args.stack_name,
            "--region",
            args.region,
            "--query",
            "Stacks[0].Outputs[?OutputKey=='ApplicationWebURL'].OutputValue",
            "--output",
            "text",
            "--no-cli-pager",
        ],
        text=True,
    ).strip()
    log(f"target UI: {base_url}")

    email = f"uat-agent-{secrets.token_hex(3)}@example.invalid"
    password = "Aa1!" + secrets.token_urlsafe(24)
    state = outdir / "state.json"
    result: dict | None = None

    try:
        log(f"creating {args.role} user {email}")
        create_cognito_user(ctx, email, args.role, password)

        log("signing in deterministically to capture storageState...")
        if not sign_in_and_save_state(base_url, email, password, state):
            return 2

        spec = available[args.flow] if args.flow else ad_hoc(args.goal)
        if spec.get("ad_hoc"):
            log("AD-HOC goal: no oracle, so the result will be `unverified` by design.")
        flow = Flow(
            flow_id=spec["flow_id"],
            claim=spec["claim"],
            docs=spec["docs"] if args.warm else None,
            documented_steps=spec.get("documented_steps") if args.warm else None,
        )
        log(f"running worker: flow={flow.flow_id} mode={flow.mode}")

        capture = outdir / f"{flow.flow_id}-{flow.mode}"
        result = run_worker(
            flow,
            base_url,
            storage_state=str(state),
            region=args.region,
            max_tool_calls=args.max_tool_calls,
            max_vision_calls=args.max_vision_calls,
            vision=not args.no_vision,
            headless=not args.headed,
            capture_dir=str(capture),
        )

        log("verifying claim out-of-band (non-agentic)...")
        result["verification"] = verify_claim(
            spec, result.get("report"), ctx, args.region
        )

        # JSON lives INSIDE the capture dir so report.html, the PNGs and the raw data
        # travel together as one attachable bundle.
        (capture / "result.json").write_text(json.dumps(result, indent=2))
        report_path = render(result, capture)
        log(f"report card: {report_path}")
        r = result.get("report") or {}
        v = result["verification"]
        log(
            f"verdict: confirmed={v.get('confirmed')} difficulty={r.get('difficulty')} "
            f"clicks={result['measured'].get('clicks')} stages={len(result.get('stages') or [])}"
        )
    finally:
        log(f"deleting test user {email}")
        delete_cognito_user(ctx, email)
        state.unlink(missing_ok=True)

    if not result:
        return 1
    v = result.get("verification") or {}
    return 0 if v.get("confirmed") else 1


if __name__ == "__main__":
    sys.exit(main())
