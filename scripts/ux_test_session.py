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
  ./scripts/ux_test_session.py setup <STACK_NAME> --group Annotator --long-lived \
      [--key Annotator-scoped --profiles <profile>[,<profile>]] [--rotate]
  ./scripts/ux_test_session.py teardown <STACK_NAME> --email <email> [--region us-east-1]

``setup`` prints a JSON blob with url / email / password / group. ``teardown``
deletes the user.

``--long-lived`` is the one exception to "throwaway", and exists for the CI review
stack the unattended MR UX test signs in to (``.claude/skills/ux-test-ci.md``). It
creates ``ux-persona-<group>@example.invalid``, a fixed name, so the masked CI
variable ``UX_TEST_PERSONAS``, scoped to the environment ``ux-review``, can hold its
password from one run to the next, and prints the entry to merge into that
variable. The ``--stale`` sweep matches only ``ux-test-`` and so can never select a
persona. Two refusals keep the variable and the pool in step: setup will not set a
new password on an existing persona without ``--rotate``, because that breaks the
variable until somebody updates it, and teardown will not delete one without
``--long-lived``.

An Annotator, and any persona given ``--profiles``, is created through the stack's
own user management, the Lambda ``createUser`` reaches through the API, invoked
directly with the operator's AWS credentials as an Admin. That writes the
users-table row an Annotator and a profile limit are read from, so this script
never re-implements it, and ``teardown --long-lived`` removes such a persona the
same way, with ``deleteUser``. ``--rotate`` changes only an existing persona's
password, so ``--profiles`` is refused with it on one: the limit stays as the
users-table row holds it. An Admin changes that limit in place, keeping the
password, with **Edit scope** on the persona's row of the web UI's User
Management page or the user-management ``updateUser`` operation. That teardown and
then setup again also works, but sets a new password, so ``UX_TEST_PERSONAS`` must
be updated. ``--key`` names a second persona of one group, such as an Annotator
limited to one profile: its address is ``ux-persona-<key>@example.invalid`` and its
``UX_TEST_PERSONAS`` entry is ``<key>``, which its catalogue persona names as
``credentials``.

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
import re
import secrets
import string
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rbac_common import (  # noqa: E402
    aws,
    create_cognito_user,
    delete_cognito_user,
    resolve_stack,
    template_group_names,
)

# Groups a UX test might legitimately run as. Annotator is included because the
# scoped queue is a distinct experience — an annotator sees a different
# navigation and a subset of test sets — and reviewing it as an Admin would miss
# exactly the confusion an annotator hits.
# Read from template.yaml so a new role is available
# without editing a list here.
VALID_GROUPS = tuple(template_group_names())


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


USER_MANAGEMENT_FUNCTION = "UserManagementFunction"
PERSONA_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
PERSONA_KEY_CHARS = 40
_OPERATOR_IDENTITY = {
    "claims": {"cognito:groups": ["Admin"], "cognito:username": "ux-test-session"}
}


def persona_email(group: str) -> str:
    """The fixed address of the long-lived persona for one Cognito group, or for
    the ``--key`` of a second persona of one group.

    Fixed rather than random, so the CI variable holding its password names the
    same user on every run, and a second setup finds the first persona instead of
    leaving two. The reserved .invalid domain still means no invite can reach a
    real mailbox.
    """
    return f"{UX_PERSONA_PREFIX}{group.lower()}@example.invalid"


def _user_management(ctx: dict, field: str, arguments: dict) -> object:
    """Call the stack's user-management resolver directly, as an Admin.

    The event has the shape the API's dispatcher passes the same Lambda, so the
    product's own code checks the arguments and writes the users-table row, the
    Cognito user and its group. A refusal stops the command with its message.

    It is not the API's path, and proves nothing about how the API authorizes. A
    direct ``lambda invoke`` never reaches the API's authorizer, and the identity
    in the event is one this script builds itself, :data:`_OPERATOR_IDENTITY`, an
    Admin claim, so the handler's in-function Admin check is satisfied by a claim
    the caller made. What gates this call is the operator's
    ``lambda:InvokeFunction`` permission on that function.
    """
    function = aws(
        "cloudformation",
        "describe-stack-resource",
        "--stack-name",
        ctx["stack"],
        "--logical-resource-id",
        USER_MANAGEMENT_FUNCTION,
        "--query",
        "StackResourceDetail.PhysicalResourceId",
        "--output",
        "text",
        region=ctx["region"],
    )
    event = {
        "info": {"fieldName": field},
        "arguments": arguments,
        "identity": _OPERATOR_IDENTITY,
    }
    with tempfile.TemporaryDirectory() as tmp:
        response = Path(tmp) / "response.json"
        meta = aws(
            "lambda",
            "invoke",
            "--function-name",
            str(function),
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            json.dumps(event),
            str(response),
            region=ctx["region"],
        )
        text = response.read_text(encoding="utf-8") if response.exists() else ""
    body = json.loads(text) if text.strip() else None
    if isinstance(meta, dict) and meta.get("FunctionError"):
        message = body.get("errorMessage") if isinstance(body, dict) else body
        raise SystemExit(f"the stack's user management refused {field}: {message}")
    return body


def _users_row_id(ctx: dict, email: str) -> str | None:
    """The ``userId`` of the users-table row holding ``email``, or None.

    None too for a stack with no UsersTable, which holds no such row.
    """
    if not ctx.get("users_table"):
        return None
    found = aws(
        "dynamodb",
        "query",
        "--table-name",
        ctx["users_table"],
        "--index-name",
        "EmailIndex",
        "--key-condition-expression",
        "email = :email",
        "--expression-attribute-values",
        json.dumps({":email": {"S": email}}),
        region=ctx["region"],
    )
    items = (found or {}).get("Items") if isinstance(found, dict) else None
    for item in items or []:
        user_id = (item.get("userId") or {}).get("S")
        if user_id:
            return user_id
    return None


def _persona_key(args: argparse.Namespace) -> str:
    """The ``--key`` of a second persona of one group, or the group itself."""
    key = getattr(args, "key", None)
    if key is None or key == "":
        return args.group
    if (
        not isinstance(key, str)
        or len(key) > PERSONA_KEY_CHARS
        or not PERSONA_KEY_RE.fullmatch(key)
    ):
        raise SystemExit(
            "--key must be letters, digits and single hyphens, at most "
            f"{PERSONA_KEY_CHARS} characters"
        )
    return key


def _profiles(args: argparse.Namespace) -> list[str] | None:
    """The ``--profiles`` a persona is limited to, or None for no limit."""
    value = getattr(args, "profiles", None)
    if not isinstance(value, str):
        return None
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise SystemExit("--profiles names no profile")
    return names


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


def _limit_remedy(email: str) -> str:
    """How an existing persona's profile limit is changed, the route that keeps
    its password first.

    ``updateUser``, which the User Management page's Edit scope calls, sets
    ``allowedConfigVersions`` on the existing users-table row and leaves the
    Cognito user alone. It is Admin-only and refused, as ``createUser`` with a
    limit is, where another source owns the scope, so it is open wherever
    ``--profiles`` is. Teardown and setup works too, but mints a new password.
    """
    return (
        "To change the limit and keep the password, an Admin edits it in place: "
        "Edit scope on the persona's row of the web UI's User Management page, or "
        "the user-management updateUser operation. Removing the persona with "
        f"teardown --email {email} --long-lived and running setup again with "
        "--profiles also works, but sets a new password, so UX_TEST_PERSONAS must "
        "be updated"
    )


def _setup_long_lived(args: argparse.Namespace, ctx: dict, url: str) -> int:
    """Create a group's long-lived persona and print its UX_TEST_PERSONAS entry.

    The persona is the account the unattended MR UX test signs in as, so its
    password has to live in a masked CI variable rather than in a session that
    ends. Setting a new password on an existing persona is therefore an explicit
    act, --rotate, because until the variable is updated every MR UX test fails to
    sign in. A new Annotator, or a new persona given ``--profiles``, is created
    through the stack's user management (:func:`_user_management`), which writes
    the users-table row the product reads either from. An existing one is not
    created again, so --rotate changes only its password and its profile limit
    stays as that row holds it; --profiles with --rotate on an existing persona is
    therefore refused before anything is written, rather than printed as a limit
    that was never applied. Both refusals that mention a limit name the route that
    keeps the password (:func:`_limit_remedy`), an Admin's in-place edit, ahead of
    teardown and setup, which sets a new one. The persona is given a display name
    (:func:`persona_display_name`), so the UI's top bar shows that rather than the
    sign-in address. An existing persona is given it too before the refusal without
    --rotate, since naming does not touch the password: one made before the
    display name existed would otherwise keep showing its address.
    """
    key = _persona_key(args)
    profiles = _profiles(args)
    if profiles is not None and args.group == "Admin":
        raise SystemExit(
            "--profiles limits a non-Admin persona; an Admin is never limited to "
            "profiles"
        )
    email = persona_email(key)
    exists = _user_exists(ctx, email)
    needs_row = args.group == "Annotator" or profiles is not None
    if needs_row and exists != (_users_row_id(ctx, email) is not None):
        held = (
            "the user pool but not the users table"
            if exists
            else ("the users table but not the user pool")
        )
        raise SystemExit(
            f"{email} is in {held} of {args.stack}, so it is not the persona "
            f"asked for. Remove it with teardown --email {email} --long-lived, "
            "then run setup again"
        )
    rotate = getattr(args, "rotate", False) is True
    if exists and rotate and profiles is not None:
        raise SystemExit(
            f"{email} already exists in {args.stack}, and --rotate changes only "
            "its password: its profile limit stays as its users-table row holds "
            "it, whatever --profiles names. Nothing was changed. Drop --profiles "
            f"to set a new password alone. {_limit_remedy(email)}."
        )
    if exists and not rotate:
        _name_persona(ctx, email, key)
        shown = " ".join(persona_display_name(key))
        message = (
            f"{email} already exists in {args.stack}; its display name is now "
            f"{shown!r} and its password is unchanged. That password lives in the CI "
            "variable UX_TEST_PERSONAS, and setting a new one breaks that variable "
            "until it is updated. Re-run with --rotate to do that deliberately"
        )
        if profiles is not None:
            message += (
                ", and without --profiles, which --rotate cannot apply: the profile "
                f"limit stays as its users-table row holds it. {_limit_remedy(email)}"
            )
        raise SystemExit(f"{message}.")
    password = _password()
    if not exists and needs_row:
        arguments: dict[str, object] = {"email": email, "persona": args.group}
        if profiles is not None:
            arguments["allowedConfigVersions"] = profiles
        _user_management(ctx, "createUser", arguments)
        if _users_row_id(ctx, email) is None:
            raise SystemExit(
                f"the stack's user management answered createUser, but no "
                f"users-table row holds {email}"
            )
    create_cognito_user(ctx, email, args.group, password)
    _name_persona(ctx, email, key)

    next_step = (
        "merge this entry into the JSON object of every persona, base64 it, and "
        "store it as the CI variable UX_TEST_PERSONAS: masked, not protected, "
        "environment scope ux-review by its exact name, not a wildcard, which "
        "would also match ux-review-build, where the MR's npm ci runs; and no "
        "copy left at the default scope, which reaches every job"
    )
    if key != args.group:
        next_step += (
            f'; and give its persona in scripts/ux_stories.toml credentials = "{key}"'
        )
    session = {
        "url": url,
        "email": email,
        "password": password,
        "display_name": " ".join(persona_display_name(key)),
        "group": args.group,
        "stack": args.stack,
        "region": args.region,
        "persona": {key: {"username": email, "password": password}},
        "next": next_step,
    }
    if profiles is not None:
        session["profiles"] = profiles
    print(json.dumps(session, indent=2))
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    long_lived = getattr(args, "long_lived", False) is True
    given = [
        f"--{name}"
        for name in ("key", "profiles")
        if isinstance(getattr(args, name, None), str)
    ]
    if given and not long_lived:
        raise SystemExit(
            f"{' and '.join(given)} name a long-lived persona; add --long-lived, "
            "or drop them for a throwaway user"
        )
    ctx = _resolve_or_explain(args.stack, args.region)
    url = _web_url(args.stack, args.region)

    if long_lived:
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

    if args.email.lower().startswith(UX_PERSONA_PREFIX):
        user_id = _users_row_id(ctx, args.email)
        if user_id is not None:
            _user_management(ctx, "deleteUser", {"userId": user_id})
            print(f"Deleted {args.email} and its users-table row.")
            return 0
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
            "its UX_TEST_PERSONAS entry. An Annotator, or a persona given "
            "--profiles, is created through the stack's user management"
        ),
    )
    setup.add_argument(
        "--key",
        help=(
            "With --long-lived, a second persona of the group: its address is "
            "ux-persona-<key>@example.invalid and its UX_TEST_PERSONAS entry <key>"
        ),
    )
    setup.add_argument(
        "--profiles",
        help=(
            "With --long-lived, the comma-separated configuration profiles a "
            "non-Admin persona is limited to (its allowedConfigVersions)"
        ),
    )
    setup.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "With --long-lived, set a new password on a persona that already "
            "exists. Its profile limit is kept, so --profiles is refused with it "
            "on such a persona. UX_TEST_PERSONAS is broken until it is updated"
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
