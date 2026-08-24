#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
ux_test_session.py

Set up (or tear down) a throwaway browser session for a UX test run against a
live IDP stack: resolves the web UI URL and creates a disposable Cognito user in
a chosen group, so a UX test never runs as — or risks the password of — a real
operator's account.

This is deliberately *only* setup and teardown. The browsing, the assertions and
the UX judgement are the agent's job (see ``.claude/skills/ux-test.md``); putting
them here would mean a deterministic script pretending to have opinions about
usability, which is the thing the harness exists to avoid.

Reuses ``scripts/rbac_common.py`` — the same helpers the API RBAC dynamic test
uses to mint temporary users — so there is one implementation of "make a user in
this stack's pool" rather than two that drift.

Usage:
  ./scripts/ux_test_session.py setup <STACK_NAME> [--group Admin] [--region us-east-1]
  ./scripts/ux_test_session.py teardown <STACK_NAME> --email <email> [--region us-east-1]

``setup`` prints a JSON blob with url / email / password / group. ``teardown``
deletes the user and restores the app client's auth flows.

Note: setup temporarily enables ALLOW_ADMIN_USER_PASSWORD_AUTH on the UI app
client (needed to set a known password non-interactively) and teardown restores
whatever was there before. Always run teardown — ``--json`` output includes the
exact command.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rbac_common import (  # noqa: E402
    aws,
    create_cognito_user,
    delete_cognito_user,
    enable_admin_auth,
    resolve_stack,
    restore_auth_flows,
)

# Groups a UX test might legitimately run as. Annotator is included because the
# scoped queue is a distinct experience — an annotator sees a different
# navigation and a subset of test sets — and reviewing it as an Admin would miss
# exactly the confusion an annotator hits.
VALID_GROUPS = ("Admin", "Author", "Reviewer", "Annotator", "Viewer")


def _password() -> str:
    """A password satisfying the pool's policy, for a user that lives minutes."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(20))
    # Guarantee the character classes rather than hoping 20 random picks cover
    # them; a rejected password here fails the run for a silly reason.
    return f"Ux!{body}9aZ"


def _web_url(stack: str, region: str) -> str:
    """The URL a person types into a browser for this stack.

    ApplicationWebURL is correct under both hosting variants — CloudFront's
    domain, or the REST API stage when the SPA is served from API Gateway.
    """
    url = aws(
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        stack,
        "--query",
        "Stacks[0].Outputs[?OutputKey=='ApplicationWebURL'].OutputValue",
        "--output",
        "text",
        region=region,
    )
    if not url:
        raise RuntimeError(
            f"Stack {stack} has no ApplicationWebURL output — is the web UI enabled?"
        )
    return url


def _candidate_stacks(region: str) -> list[str]:
    """Root IDP stacks in this region, for a wrong-name error message.

    Best-effort: if listing fails we simply have no suggestions to offer, which
    is no worse than the message without them.
    """
    try:
        names = aws(
            "cloudformation",
            "list-stacks",
            "--stack-status-filter",
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
            "--query",
            "StackSummaries[?ParentId==null].StackName",
            "--output",
            "text",
            region=region,
        )
    except Exception:  # noqa: BLE001 — a suggestion list is a nicety
        return []
    return [n for n in names.split() if "idp" in n.lower()]


def _resolve_or_explain(stack: str, region: str):
    """resolve_stack, but a wrong stack name or region reads as an instruction.

    The default was a nine-frame traceback ending in a botocore ValidationError,
    which is exactly the "does the error say what to do?" failure this harness
    exists to catch elsewhere. Wrong region is the likeliest cause and the
    hardest to spot, since the stack genuinely does not exist where you looked.
    """
    try:
        return resolve_stack(stack, region)
    except RuntimeError as err:
        if "does not exist" not in str(err):
            raise
        candidates = _candidate_stacks(region)
        lines = [
            f"Stack {stack!r} does not exist in {region}.",
            "",
            "The most common cause is the wrong --region: a stack is invisible "
            "from any other one.",
        ]
        if candidates:
            lines += ["", f"IDP stacks found in {region}:"]
            lines += [f"  {name}" for name in candidates]
        else:
            lines += [
                "",
                f"No IDP stacks found in {region} at all — try another region, "
                "or check AWS_PROFILE (the sandbox default points at a "
                "different account than the deployment).",
            ]
        raise SystemExit("\n".join(lines)) from err


def cmd_setup(args: argparse.Namespace) -> int:
    ctx = _resolve_or_explain(args.stack, args.region)
    url = _web_url(args.stack, args.region)

    email = f"ux-test-{secrets.token_hex(4)}@example.invalid"
    password = _password()

    # example.invalid is reserved by RFC 2606, so a stray invite email can never
    # reach a real mailbox. Delivery is suppressed anyway (see create_cognito_user).
    enable_admin_auth(ctx)
    create_cognito_user(ctx, email, args.group, password)

    session = {
        "url": url,
        "email": email,
        "password": password,
        "group": args.group,
        "stack": args.stack,
        "region": args.region,
        "teardown": (
            f"./scripts/ux_test_session.py teardown {args.stack} "
            f"--email {email} --region {args.region}"
        ),
    }
    print(json.dumps(session, indent=2))
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    """Just the web UI URL.

    The common case is a reviewer attaching to their own already-signed-in
    browser, which needs no user and no teardown — only the address. Keeping
    that a separate subcommand means the simple path stays one command with
    nothing to clean up afterwards.
    """
    print(_web_url(args.stack, args.region))
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    ctx = _resolve_or_explain(args.stack, args.region)
    delete_cognito_user(ctx, args.email)
    restore_auth_flows(ctx)
    print(f"Deleted {args.email} and restored app-client auth flows.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    url = sub.add_parser("url", help="Print the stack's web UI URL and nothing else")
    url.add_argument("stack")
    url.add_argument("--region", default="us-east-1")
    url.set_defaults(func=cmd_url)

    setup = sub.add_parser("setup", help="Create a throwaway UX-test session")
    setup.add_argument("stack")
    setup.add_argument("--group", default="Admin", choices=VALID_GROUPS)
    setup.add_argument("--region", default="us-east-1")
    setup.set_defaults(func=cmd_setup)

    teardown = sub.add_parser("teardown", help="Delete the throwaway user")
    teardown.add_argument("stack")
    teardown.add_argument("--email", required=True)
    teardown.add_argument("--region", default="us-east-1")
    teardown.set_defaults(func=cmd_teardown)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
