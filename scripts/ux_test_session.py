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


def cmd_setup(args: argparse.Namespace) -> int:
    ctx = resolve_stack(args.stack, args.region)
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


def cmd_teardown(args: argparse.Namespace) -> int:
    ctx = resolve_stack(args.stack, args.region)
    delete_cognito_user(ctx, args.email)
    restore_auth_flows(ctx)
    print(f"Deleted {args.email} and restored app-client auth flows.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

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
