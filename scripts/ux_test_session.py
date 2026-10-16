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
  ./scripts/ux_test_session.py setup <STACK_NAME> --group Admin --long-lived [--rotate]
  ./scripts/ux_test_session.py teardown <STACK_NAME> --email <email> [--region us-east-1]

``setup`` prints a JSON blob with url / email / password / group. ``teardown``
deletes the user.

``--long-lived`` is the one exception to "throwaway", and exists for the CI review
stack the unattended MR UX test signs in to (``.claude/skills/ux-test-ci.md``). It
creates ``ux-persona-<group>@example.invalid``, a fixed name, so the masked CI
variable ``UX_TEST_PERSONAS`` can hold its password from one run to the next, and
prints the entry to merge into that variable. The ``--stale`` sweep matches only
``ux-test-`` and so can never select a persona. Two refusals keep the variable and
the pool in step: setup will not set a new password on an existing persona without
``--rotate``, because that breaks the variable until somebody updates it, and
teardown will not delete one without ``--long-lived``. Annotator is refused for
now, because an annotator also needs a UsersTable row this option does not create.

The app client's auth flows are deliberately NOT modified. Setting a known
password non-interactively needs only ``admin-set-user-password --permanent``,
which is an admin API on the user pool and does not depend on the client's
``ExplicitAuthFlows``; the browser then signs in over SRP, which the UI client
already allows. ``ALLOW_ADMIN_USER_PASSWORD_AUTH`` is required only by
``admin-initiate-auth``, which this script never calls. Widening the client for
the session bought nothing and left a live stack's UI client accepting
password-based admin auth for as long as the agent session ran — worse here than
in test_api_rbac.py, which reverts in a ``finally`` even under ``--no-teardown``,
because this is two separate invocations with an open-ended session between them.

Always run teardown — ``--json`` output includes the exact command.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rbac_common import (  # noqa: E402
    aws,
    create_cognito_user,
    delete_cognito_user,
    resolve_stack,
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


#: The username prefix of a long-lived persona. Disjoint from UX_TEST_PREFIX, the
#: only prefix the --stale sweep matches, so no sweep can ever select a persona.
UX_PERSONA_PREFIX = "ux-persona-"


def persona_email(group: str) -> str:
    """The fixed address of the long-lived persona for one Cognito group.

    Fixed rather than random, so the CI variable holding its password names the
    same user on every run, and a second setup finds the first persona instead of
    leaving two. The reserved .invalid domain still means no invite can reach a
    real mailbox.
    """
    return f"{UX_PERSONA_PREFIX}{group.lower()}@example.invalid"


def persona_display_name(group: str) -> tuple[str, str]:
    """The (given_name, family_name) a group's persona is shown by in the UI.

    The top navigation shows the ID token's given and family names when it carries
    them, and the sign-in address otherwise, on every page. The unattended MR UX
    test records those pages and hands their snapshots to a model, and the address
    is the persona's sign-in name, so the persona gets a name of its own instead.
    The app client reads both attributes, so the token carries them.
    """
    return "UX", f"Persona {group}"


def _name_persona(ctx: dict, email: str, group: str) -> None:
    """Give the persona its display name; the attributes are standard ones."""
    given, family = persona_display_name(group)
    aws(
        "cognito-idp",
        "admin-update-user-attributes",
        "--user-pool-id",
        ctx["user_pool"],
        "--username",
        email,
        "--user-attributes",
        json.dumps(
            [
                {"Name": "given_name", "Value": given},
                {"Name": "family_name", "Value": family},
            ]
        ),
        region=ctx["region"],
    )


def _user_exists(ctx: dict, email: str) -> bool:
    """Whether the stack's user pool already holds `email`.

    Only Cognito's UserNotFoundException means no. Any other failure, a denial, a
    throttle or the wrong pool, stops the command: read as absence, it would send
    an existing persona down the create path, which sets a new password on it and
    silently breaks UX_TEST_PERSONAS.
    """
    try:
        aws(
            "cognito-idp",
            "admin-get-user",
            "--user-pool-id",
            ctx["user_pool"],
            "--username",
            email,
            region=ctx["region"],
        )
    except RuntimeError as err:
        if "UserNotFoundException" in str(err):
            return False
        raise SystemExit(f"Could not tell whether {email} exists: {err}") from err
    return True


def _setup_long_lived(args: argparse.Namespace, ctx: dict, url: str) -> int:
    """Create a group's long-lived persona and print its UX_TEST_PERSONAS entry.

    The persona is the account the unattended MR UX test signs in as, so its
    password has to live in a masked CI variable rather than in a session that
    ends. Setting a new password on an existing persona is therefore an explicit
    act, --rotate, because until the variable is updated every MR UX test fails to
    sign in. Annotator is refused: an annotator needs a UsersTable row as well as
    the group, and this option creates only the Cognito user. The persona is given
    a display name (:func:`persona_display_name`), so the UI's top bar shows that
    rather than the sign-in address. An existing persona is given it too before
    the refusal, since naming does not touch the password: one made before the
    display name existed would otherwise keep showing its address.
    """
    if args.group == "Annotator":
        supported = ", ".join(group for group in VALID_GROUPS if group != "Annotator")
        raise SystemExit(
            "--long-lived does not support Annotator yet: an annotator also needs a "
            f"UsersTable row, which this option does not create. Use one of "
            f"{supported}."
        )
    email = persona_email(args.group)
    if _user_exists(ctx, email) and getattr(args, "rotate", False) is not True:
        _name_persona(ctx, email, args.group)
        shown = " ".join(persona_display_name(args.group))
        raise SystemExit(
            f"{email} already exists in {args.stack}; its display name is now "
            f"{shown!r} and its password is unchanged. That password lives in the CI "
            "variable UX_TEST_PERSONAS, and setting a new one breaks that variable "
            "until it is updated. Re-run with --rotate to do that deliberately."
        )
    password = _password()
    create_cognito_user(ctx, email, args.group, password)
    _name_persona(ctx, email, args.group)

    session = {
        "url": url,
        "email": email,
        "password": password,
        "display_name": " ".join(persona_display_name(args.group)),
        "group": args.group,
        "stack": args.stack,
        "region": args.region,
        "persona": {args.group: {"username": email, "password": password}},
        "next": (
            "merge this entry into the JSON object of every persona, base64 it, and "
            "store it as the masked CI variable UX_TEST_PERSONAS"
        ),
    }
    print(json.dumps(session, indent=2))
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    ctx = _resolve_or_explain(args.stack, args.region)
    url = _web_url(args.stack, args.region)

    if getattr(args, "long_lived", False) is True:
        return _setup_long_lived(args, ctx, url)

    email = f"ux-test-{secrets.token_hex(4)}@example.invalid"
    password = _password()

    # example.invalid is reserved by RFC 2606, so a stray invite email can never
    # reach a real mailbox. Delivery is suppressed anyway (see create_cognito_user).
    #
    # No enable_admin_auth: see the module docstring. The three admin APIs used
    # here need no client auth flow, and the browser signs in over SRP.
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


UX_TEST_PREFIX = "ux-test-"


def _stale_ux_users(ctx, older_than_hours: float) -> list:
    """UX-test users in the pool older than `older_than_hours`.

    Matched on the ``ux-test-`` prefix *and* the reserved ``@example.invalid``
    domain, so a real operator account can never be selected however it is named.
    """
    result = aws(
        "cognito-idp",
        "list-users",
        "--user-pool-id",
        ctx["user_pool"],
        "--region",
        ctx["region"],
        region=ctx["region"],
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    stale = []
    for user in result.get("Users", []):
        name = user.get("Username", "")
        if not (name.startswith(UX_TEST_PREFIX) and name.endswith("@example.invalid")):
            continue
        created = user.get("UserCreateDate")
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created is None or created <= cutoff:
            stale.append(name)
    return stale


def cmd_teardown(args: argparse.Namespace) -> int:
    """Delete one user, or sweep abandoned throwaway ones with --stale.

    A long-lived persona is refused without --long-lived, before any AWS call. Its
    password lives in the CI variable UX_TEST_PERSONAS, so deleting it breaks the
    MR UX test until the persona is recreated and the variable updated, and a
    teardown command copied from a throwaway session must not be how that happens.
    The prefix is compared case-blind because the pool's usernames are.
    """
    email = getattr(args, "email", None)
    if (
        isinstance(email, str)
        and email.lower().startswith(UX_PERSONA_PREFIX)
        and getattr(args, "long_lived", False) is not True
    ):
        raise SystemExit(
            f"{email} is a long-lived persona, not a throwaway session: its "
            "password lives in the CI variable UX_TEST_PERSONAS, and deleting it "
            "breaks the MR UX test until it is recreated and the variable updated. "
            "Pass --long-lived to delete it deliberately."
        )
    ctx = _resolve_or_explain(args.stack, args.region)

    # --stale exists because the weak point of this script is structural, not a
    # missing feature: setup and teardown are two separate invocations with an
    # open-ended agent session between them, so an abandoned session leaves a real
    # Cognito account — permanent password, Admin group by default — in a live pool
    # with nothing to expire it. A printed reminder is not a control. This gives the
    # next run a way to close the previous one's leak.
    if args.stale is not None:
        stale = _stale_ux_users(ctx, args.stale)
        if not stale:
            print(f"No ux-test users older than {args.stale}h in {args.stack}.")
            return 0
        for email in stale:
            delete_cognito_user(ctx, email)
            print(f"Deleted stale {email}.")
        print(f"Swept {len(stale)} stale ux-test user(s).")
        return 0

    if not args.email:
        raise SystemExit("teardown needs --email, or --stale to sweep abandoned users")

    delete_cognito_user(ctx, args.email)
    # Nothing to restore: setup never widened the client.
    print(f"Deleted {args.email}.")
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
    setup.add_argument(
        "--long-lived",
        action="store_true",
        help=(
            "Instead of a throwaway user, create the group's standing persona "
            "ux-persona-<group>@example.invalid for the CI review stack, and print "
            "its UX_TEST_PERSONAS entry. Not for Annotator"
        ),
    )
    setup.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "With --long-lived, set a new password on a persona that already "
            "exists. UX_TEST_PERSONAS is broken until it is updated"
        ),
    )
    setup.set_defaults(func=cmd_setup)

    teardown = sub.add_parser("teardown", help="Delete the throwaway user")
    teardown.add_argument("stack")
    # Not required, because --stale is the other way to call this.
    teardown.add_argument("--email")
    teardown.add_argument(
        "--stale",
        nargs="?",
        type=float,
        const=12.0,
        default=None,
        metavar="HOURS",
        help=(
            "Instead of one user, delete every ux-test-*@example.invalid user in the "
            "pool older than HOURS (default 12) — sweeps sessions whose teardown was "
            "never run"
        ),
    )
    teardown.add_argument("--region", default="us-east-1")
    teardown.add_argument(
        "--long-lived",
        action="store_true",
        help=(
            "Required to delete a ux-persona-* user, whose password the CI "
            "variable UX_TEST_PERSONAS holds"
        ),
    )
    teardown.set_defaults(func=cmd_teardown)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
