#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Live end-to-end check of the host Inbox (``make stacktest-inbox``).

Posts through ``idp_common.notifications`` with local credentials, the way a
producer Lambda does, then reads the result back through the deployed REST API
with real Cognito ID tokens. It proves what the offline suites cannot: that the
recipient key a producer writes is the one the deployed resolver reads, that
group fan-out reaches a real Cognito group, and that de-duplication,
idempotency, the unread count and read state behave on the real table and GSI.

WHAT IT DOES
------------
  1. Resolves the stack (UI pool/client, API base) and its
     ``NotificationsTableName`` output. No output (an older stack) -> SKIP.
  2. Creates a temporary Cognito group ``inbox-live-check-<rand>`` and two
     temporary users in the ``Viewer`` group; only user A joins the temporary
     group.
  3. Registers the in-process type ``live_check.probe`` (tools only; production
     types are declared in ``idp_common/notifications/catalogue.py``).
  4. Posts to user A, then to user A and the temporary group together (one
     recipient after de-duplication), then twice with one ``idempotency_key``
     (the second is a duplicate) and twice with one ``dedupe_key`` (one message).
  5. With user A's token: ``listInboxMessages`` shows the messages,
     ``getInboxUnreadCount`` matches, and ``markInboxMessages`` lowers the
     count. With user B's token: nothing of user A's is listed and marking user
     A's id answers 404.
  6. Always removes the users, the group and every item in both users'
     partitions, and restores the app client's auth flows.

OUTCOMES AND EXIT CODES
-----------------------
Every check is PASS, FAIL, ERROR (could not be run) or SKIP (precondition
absent). A SKIP is never a PASS. Exit 0 only when every check passed; 1 when any
check failed; 2 when nothing failed but a check errored or was skipped.

USAGE
-----
  AWS_PROFILE=default python3 scripts/inbox_live_check.py --stack-name IDP1 --region us-west-2

Requires: awscli v2 on PATH; credentials with Cognito admin, CloudFormation read
and DynamoDB read/write on the NotificationsTable (the deploy-account creds are
enough).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rbac_common  # noqa: E402

PASS = "PASS"  # nosec B105 - check outcome label, not a password
FAIL = "FAIL"
ERROR = "ERROR"
SKIP = "SKIP"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCOMPLETE = 2

PROBE_TYPE_ID = "live_check.probe"
APP_GROUP = "Viewer"
SOURCE = "live-check:inbox"

# Unread counts come from a GSI, which is eventually consistent.
EVENTUAL_ATTEMPTS = 6
EVENTUAL_DELAY_SECONDS = 2.0


@dataclass
class Check:
    name: str
    outcome: str
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, outcome: str, detail: str) -> None:
        self.checks.append(Check(name, outcome, detail))
        print(f"  [{outcome:5s}] {name}: {detail}")

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.add(name, PASS if ok else FAIL, detail)
        return ok

    def exit_code(self) -> int:
        return exit_code([c.outcome for c in self.checks])


def exit_code(outcomes: list[str]) -> int:
    """0 only when there is at least one check and every one passed."""
    if FAIL in outcomes:
        return EXIT_FAIL
    if not outcomes or any(o != PASS for o in outcomes):
        return EXIT_INCOMPLETE
    return EXIT_PASS


class CheckAborted(Exception):
    """A step could not run; the remaining steps would prove nothing."""


def _library_root_on_path() -> None:
    """Put this repository's idp_common on the path when it is not installed."""
    root = Path(__file__).resolve().parents[1] / "lib" / "idp_common_pkg"
    if root.is_dir() and str(root) not in sys.path:
        sys.path.append(str(root))


def default_call_body(api_base, field_name, args, token):
    """POST /op/<field> and return ``(status, body_text)`` (the RBAC harness's)."""
    from test_api_rbac import call_body

    return call_body(api_base, field_name, args, token)


def default_service_factory(table_name: str, user_pool_id: str, region: str):
    """A producer-side NotificationService on the live table, default settings."""
    _library_root_on_path()
    from idp_common.notifications.models import NotificationSettings
    from idp_common.notifications.service import NotificationService
    from idp_common.notifications.store import NotificationStore

    return NotificationService(
        store=NotificationStore(table_name=table_name, region=region),
        user_pool_id=user_pool_id,
        region=region,
        settings_provider=NotificationSettings,
    )


@dataclass
class Deps:
    """Everything that touches AWS or the network, injectable for tests."""

    resolve_stack: Callable[..., dict] = rbac_common.resolve_stack
    aws: Callable[..., Any] = rbac_common.aws
    enable_admin_auth: Callable[[dict], None] = rbac_common.enable_admin_auth
    restore_auth_flows: Callable[[dict], None] = rbac_common.restore_auth_flows
    create_cognito_user: Callable[..., None] = rbac_common.create_cognito_user
    delete_cognito_user: Callable[..., None] = rbac_common.delete_cognito_user
    get_id_token: Callable[..., Any] = rbac_common.get_id_token
    call_body: Callable[..., tuple] = default_call_body
    service_factory: Callable[..., Any] = default_service_factory
    sleep: Callable[[float], None] = time.sleep
    rand: Callable[[], str] = lambda: secrets.token_hex(4)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Live Inbox check: post via idp_common.notifications, read via the API."
    )
    ap.add_argument("--stack-name", required=True)
    ap.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
    )
    args = ap.parse_args(argv)
    if not args.region:
        ap.error("--region is required (or set AWS_REGION / AWS_DEFAULT_REGION)")
    return args


def notifications_table_name(deps: Deps, ctx: dict) -> str:
    name = deps.aws(
        "cloudformation",
        "describe-stacks",
        "--stack-name",
        ctx["stack"],
        "--query",
        "Stacks[0].Outputs[?OutputKey=='NotificationsTableName'].OutputValue",
        "--output",
        "text",
        region=ctx["region"],
    )
    name = (name or "").strip()
    return "" if name in ("", "None") else name


def _json(body: str) -> Any:
    try:
        return json.loads(body or "")
    except (ValueError, TypeError):
        return None


class InboxLiveCheck:
    def __init__(self, args: argparse.Namespace, deps: Deps, report: Report):
        self.args = args
        self.deps = deps
        self.report = report
        tag = deps.rand()
        self.tag = tag
        self.group = f"inbox-live-check-{tag}"
        self.user_a = f"inbox-live-a-{tag}@example.invalid"
        self.user_b = f"inbox-live-b-{tag}@example.invalid"
        self.password = "Aa1!" + secrets.token_urlsafe(24)
        self.ctx: dict = {}
        self.service = None
        self.created_users: list[str] = []
        self.group_created = False
        self.auth_enabled = False
        self.type_registered = False

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        try:
            self._run()
        except CheckAborted as e:
            self.report.add("inbox-live-check", ERROR, str(e))
        except Exception as e:  # noqa: BLE001 - report, never mask as a pass
            self.report.add(
                "inbox-live-check", ERROR, f"unexpected {type(e).__name__}: {e}"
            )
        finally:
            self.cleanup()

    def _run(self) -> None:
        d = self.deps
        self.ctx = d.resolve_stack(self.args.stack_name, self.args.region)
        table = notifications_table_name(d, self.ctx)
        if not table:
            self.report.add(
                "stack-has-inbox",
                SKIP,
                "no NotificationsTableName output — the stack predates the Inbox",
            )
            return
        print(f"NotificationsTable: {table}")

        self._create_identities()
        self._register_type()
        self.service = d.service_factory(
            table, self.ctx["user_pool"], self.ctx["region"]
        )
        expected = self._post_messages()
        self._read_through_api(expected)

    # ------------------------------------------------------------ fixtures
    def _create_identities(self) -> None:
        d = self.deps
        d.enable_admin_auth(self.ctx)
        self.auth_enabled = True
        d.aws(
            "cognito-idp",
            "create-group",
            "--user-pool-id",
            self.ctx["user_pool"],
            "--group-name",
            self.group,
            "--description",
            "Temporary group for the Inbox live check",
            region=self.ctx["region"],
        )
        self.group_created = True
        for email in (self.user_a, self.user_b):
            self.created_users.append(email)
            d.create_cognito_user(self.ctx, email, APP_GROUP, self.password)
        d.aws(
            "cognito-idp",
            "admin-add-user-to-group",
            "--user-pool-id",
            self.ctx["user_pool"],
            "--username",
            self.user_a,
            "--group-name",
            self.group,
            region=self.ctx["region"],
        )

    def _register_type(self) -> None:
        _library_root_on_path()
        from idp_common.notifications import NotificationType, register_type

        register_type(
            NotificationType(
                id=PROBE_TYPE_ID,
                display_name="Live check probe",
                description="Posted and removed by scripts/inbox_live_check.py.",
            )
        )
        self.type_registered = True

    # ---------------------------------------------------------------- posts
    def _post(self, **kwargs):
        _library_root_on_path()
        from idp_common.notifications.models import NotificationRequest

        request = NotificationRequest(type_id=PROBE_TYPE_ID, source=SOURCE, **kwargs)
        return self.service.post(request)

    def _post_messages(self) -> dict:
        """Post every probe; return what user A's Inbox must then hold."""
        r = self.report
        t = self.tag
        direct = self._post(title=f"direct {t}", users=[self.user_a])
        r.check(
            "post-to-user",
            direct.delivered == 1 and direct.failed == 0,
            f"delivered={direct.delivered} failed={direct.failed}",
        )

        fan = self._post(title=f"group {t}", users=[self.user_a], groups=[self.group])
        r.check(
            "post-to-group-deduplicates-recipients",
            fan.recipients_considered == 1 and fan.delivered == 1,
            f"recipients={fan.recipients_considered} delivered={fan.delivered} "
            "(user A is named directly and through the group)",
        )

        key = f"live-check-idem-{t}"
        first = self._post(
            title=f"idempotent {t}", users=[self.user_a], idempotency_key=key
        )
        again = self._post(
            title=f"idempotent {t}", users=[self.user_a], idempotency_key=key
        )
        r.check(
            "idempotency-key-repost-is-duplicate",
            first.delivered == 1 and again.duplicates == 1 and again.delivered == 0,
            f"first delivered={first.delivered}; repost duplicates={again.duplicates} "
            f"delivered={again.delivered}",
        )

        dkey = f"live-check-dedupe-{t}"
        d1 = self._post(title=f"dedupe-1 {t}", users=[self.user_a], dedupe_key=dkey)
        d2 = self._post(title=f"dedupe-2 {t}", users=[self.user_a], dedupe_key=dkey)
        r.check(
            "dedupe-key-posts-succeed",
            d1.failed == 0 and d2.failed == 0,
            f"failed={d1.failed}+{d2.failed}",
        )
        failed = sum(p.failed for p in (direct, fan, first, again, d1, d2))
        if failed:
            raise CheckAborted(
                f"{failed} post(s) failed; the API checks would prove nothing"
            )
        return {
            "titles": {f"direct {t}", f"group {t}", f"idempotent {t}", f"dedupe-2 {t}"},
            "absent": {f"dedupe-1 {t}"},
        }

    # ------------------------------------------------------------------ API
    def _token(self, email: str) -> str:
        token = self.deps.get_id_token(self.ctx, email, self.password)
        if not token or len(token) < 100:
            raise CheckAborted(f"could not mint an ID token for {email}")
        return token

    def _api(self, field_name: str, args: dict, token: str) -> tuple[int, Any]:
        status, body = self.deps.call_body(
            self.ctx["api_base"], field_name, args, token
        )
        if status in (0, None) or (isinstance(status, int) and status >= 500):
            raise CheckAborted(f"{field_name} did not complete: {status} {body!r:.200}")
        return status, _json(body)

    def _eventually_count(self, token: str, want: int) -> int | None:
        got = None
        for attempt in range(EVENTUAL_ATTEMPTS):
            status, body = self._api("getInboxUnreadCount", {}, token)
            got = body if status == 200 and isinstance(body, int) else None
            if got == want:
                return got
            if attempt + 1 < EVENTUAL_ATTEMPTS:
                self.deps.sleep(EVENTUAL_DELAY_SECONDS)
        return got

    def _read_through_api(self, expected: dict) -> None:
        r = self.report
        a_tok = self._token(self.user_a)
        b_tok = self._token(self.user_b)

        status, page = self._api("listInboxMessages", {"limit": 100}, a_tok)
        items = page.get("items") if isinstance(page, dict) else None
        if status != 200 or not isinstance(items, list):
            raise CheckAborted(f"user A's list answered {status} without items")
        titles = {i.get("title") for i in items if isinstance(i, dict)}
        r.check(
            "api-lists-posted-messages",
            expected["titles"] <= titles and not (expected["absent"] & titles),
            f"expected {sorted(expected['titles'])}, replaced "
            f"{sorted(expected['absent'])}; got {sorted(t for t in titles if t)}",
        )
        r.check(
            "api-dedupe-leaves-one-message",
            len(items) == len(expected["titles"]),
            f"{len(items)} messages listed for {len(expected['titles'])} expected",
        )
        unread = len(expected["titles"])
        count = self._eventually_count(a_tok, unread)
        r.check(
            "api-unread-count-matches",
            count == unread,
            f"getInboxUnreadCount={count} list unreadCount={page.get('unreadCount')} "
            f"expected {unread}",
        )

        target = items[0].get("id")
        status, result = self._api(
            "markInboxMessages", {"ids": [target], "read": True}, a_tok
        )
        r.check(
            "api-mark-read-lowers-count",
            status == 200
            and isinstance(result, dict)
            and result.get("updated") == 1
            and result.get("unreadCount") == unread - 1,
            f"status={status} result={result}",
        )
        count = self._eventually_count(a_tok, unread - 1)
        r.check(
            "api-unread-count-after-mark",
            count == unread - 1,
            f"getInboxUnreadCount={count} expected {unread - 1}",
        )

        a_ids = {i.get("id") for i in items}
        status, b_page = self._api("listInboxMessages", {"limit": 100}, b_tok)
        b_items = b_page.get("items") if isinstance(b_page, dict) else None
        r.check(
            "api-user-b-sees-nothing-of-user-a",
            status == 200
            and isinstance(b_items, list)
            and not ({i.get("id") for i in b_items} & a_ids),
            f"status={status} items={len(b_items) if isinstance(b_items, list) else None}",
        )
        status, _ = self._api(
            "markInboxMessages", {"ids": [target], "read": False}, b_tok
        )
        r.check(
            "api-user-b-mark-of-user-a-id-is-404",
            status == 404,
            f"status={status}",
        )

    # -------------------------------------------------------------- cleanup
    def cleanup(self) -> None:
        """Best-effort and complete: each step runs even if an earlier one fails."""
        steps: list[tuple[str, Callable[[], None]]] = [
            ("inbox items", self._delete_items),
            ("users", self._delete_users),
            ("group", self._delete_group),
            ("auth flows", self._restore_auth),
            ("probe type", self._unregister_type),
        ]
        for label, step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001 - keep cleaning up
                print(f"  cleanup of {label} failed: {type(e).__name__}: {e}")

    def _delete_items(self) -> None:
        if self.service is None:
            return
        _library_root_on_path()
        from idp_common.notifications.keys import partition_key

        store = self.service.store
        for email in (self.user_a, self.user_b):
            pk = partition_key(email)
            sks: list[str] = []
            for response in store.client.query_pages(
                "PK = :pk",
                expression_attribute_names={"#sk": "SK"},
                expression_attribute_values={":pk": pk},
                projection_expression="#sk",
                consistent_read=True,
            ):
                sks.extend(str(i["SK"]) for i in response.get("Items", []))
            store.delete_messages(pk, sks)

    def _delete_users(self) -> None:
        for email in self.created_users:
            self.deps.delete_cognito_user(self.ctx, email)

    def _delete_group(self) -> None:
        if not self.group_created:
            return
        self.deps.aws(
            "cognito-idp",
            "delete-group",
            "--user-pool-id",
            self.ctx["user_pool"],
            "--group-name",
            self.group,
            region=self.ctx["region"],
        )

    def _restore_auth(self) -> None:
        if self.auth_enabled:
            self.deps.restore_auth_flows(self.ctx)

    def _unregister_type(self) -> None:
        if not self.type_registered:
            return
        from idp_common.notifications.catalogue import unregister_type

        unregister_type(PROBE_TYPE_ID)


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    args = parse_args(argv)
    report = Report()
    print(f"Inbox live check: stack {args.stack_name} ({args.region})")
    InboxLiveCheck(args, deps or Deps(), report).run()
    code = report.exit_code()
    counts = {
        o: sum(c.outcome == o for c in report.checks) for o in (PASS, FAIL, ERROR, SKIP)
    }
    print(
        f"\n{len(report.checks)} checks: {counts[PASS]} passed, {counts[FAIL]} failed, "
        f"{counts[ERROR]} could not be run, {counts[SKIP]} skipped"
    )
    verdict = {EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_INCOMPLETE: "INCOMPLETE"}[
        code
    ]
    print(f"RESULT: {verdict} (exit {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
