# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Live cross-tenant check against a deployed stack (``idp-cli tenancy verify``).

What it proves, and how:

1. Two declared tenants each get a throwaway **Viewer** created through the API's
   own ``createUser`` — called as a throwaway **Admin** — with
   ``allowedConfigVersions`` set to that tenant's profile pattern. Going through
   the API rather than writing the UsersTable directly means the run exercises the
   real user-management path, including its tenant-scope validation, and needs no
   credential beyond the Cognito admin and S3 access the operator already has.
2. One document is written under each tenant's prefix, directly to S3, the way an
   external uploader would. The precheck confirms a prefix mapping assigns each a
   profile of its own tenant, because without one the document is not the tenant's
   at all and the check would prove nothing.
3. Each Viewer must be able to read its own tenant's document (``getDocument``,
   ``getFileContents``, ``listDocuments``) and be refused the other tenant's.
4. In ``Strict`` mode, two uploads must not cross the boundary: one outside every
   tenant prefix must be refused, and one under tenant A's prefix whose metadata
   names tenant B's profile must be refused or processed under A's own profile (the
   default "Mapping wins" mode does the latter), never under B's. In ``Off``
   mode those rows are SKIP — the stack does not claim to refuse them.

Each row is PASS, FAIL, SKIP or ERROR, and only PASS is a pass. Everything created
is removed in a ``finally``, and a failed removal is itself an ERROR row, because a
leftover Admin user is the worst thing this command could leave behind.

The orchestration takes its collaborators as arguments (``api``, ``cognito``,
``s3``, ``clock``) so it is unit-testable with fakes.
"""

from __future__ import annotations

import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from idp_sdk.models.tenancy import (
    TenantVerifyCheck,
    TenantVerifyOutcome,
    TenantVerifyResult,
)

Api = Callable[[str, Dict[str, Any], str], Tuple[int, Any]]

_PASS, _FAIL, _SKIP, _ERROR = (
    TenantVerifyOutcome.PASS,
    TenantVerifyOutcome.FAIL,
    TenantVerifyOutcome.SKIP,
    TenantVerifyOutcome.ERROR,
)


def _password() -> str:
    # Satisfies the pool's default policy: upper, lower, digit, symbol, length.
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(20))
    return f"Tv{body}9!"


def _refused(status: int, body: Any) -> bool:
    """Whether a response is a refusal: an HTTP denial or an in-band scope error."""
    if status in (401, 403, 404):
        return True
    if status == 200 and isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("type"):
            return True
        if body.get("errors"):
            return True
    return False


def _document(status: int, body: Any) -> Optional[Dict[str, Any]]:
    if status == 200 and isinstance(body, dict) and body.get("ObjectKey"):
        return body
    return None


def _pick_profile_for(
    tenant: Mapping[str, Any], key: str, mappings: Sequence[Mapping[str, Any]], tenants
) -> Optional[str]:
    from idp_common.config.prefix_mappings import find_match
    from idp_common.config.tenants import tenant_for_profile

    match = find_match(key, mappings)
    if not match:
        return None
    owner = tenant_for_profile(match["configProfile"], tenants)
    if not owner or owner["tenantId"] != tenant["tenantId"]:
        return None
    return match["configProfile"]


def run_verify(
    *,
    isolation_mode: str,
    tenants: Sequence[Mapping[str, Any]],
    mappings: Sequence[Mapping[str, Any]],
    document: bytes,
    document_name: str,
    input_bucket: str,
    api: Api,
    make_admin: Callable[[str, str], str],
    make_password: Callable[[str, str], str],
    delete_cognito_user: Callable[[str], None],
    s3: Any,
    tenant_ids: Optional[Sequence[str]] = None,
    timeout_seconds: int = 240,
    email_domain: str = "example.com",
    poll_seconds: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    run_id: Optional[str] = None,
) -> TenantVerifyResult:
    """Run the matrix. See the module docstring for what each row means.

    ``make_admin(email, password)`` creates a Cognito Admin and returns its ID token.
    ``make_password(email, password)`` sets a permanent password on a user the API
    created and returns its ID token. ``delete_cognito_user(email)`` removes one.
    """
    from idp_common.config.tenants import tenant_for_profile

    result = TenantVerifyResult(isolation_mode=isolation_mode)
    checks = result.checks

    def add(name: str, outcome: TenantVerifyOutcome, detail: str = "") -> None:
        checks.append(TenantVerifyCheck(name=name, outcome=outcome, detail=detail))

    ordered = sorted(tenants, key=lambda t: t["tenantId"])
    if tenant_ids:
        wanted = list(tenant_ids)
        ordered = [t for t in ordered if t["tenantId"] in wanted]
        missing = set(wanted) - {t["tenantId"] for t in ordered}
        if missing:
            add(
                "tenants",
                _ERROR,
                f"Not declared on this stack: {', '.join(sorted(missing))}",
            )
            return result
    if len(ordered) < 2:
        add(
            "tenants",
            _ERROR,
            "At least two declared tenants are needed to check isolation between them.",
        )
        return result
    a, b = ordered[0], ordered[1]
    run = run_id or secrets.token_hex(4)
    started = datetime.now(timezone.utc)

    keys = {
        t["tenantId"]: f"{sorted(t['prefixes'])[0]}tenancy-verify-{run}/{document_name}"
        for t in (a, b)
    }
    profiles: Dict[str, str] = {}
    for t in (a, b):
        profile = _pick_profile_for(t, keys[t["tenantId"]], mappings, tenants)
        if not profile:
            add(
                f"precheck:{t['tenantId']}",
                _ERROR,
                f"No enabled prefix mapping assigns {keys[t['tenantId']]!r} to one of "
                f"tenant {t['tenantId']!r}'s profiles ({t['profilePattern']}). Add one "
                f"covering {sorted(t['prefixes'])[0]!r}.",
            )
        else:
            profiles[t["tenantId"]] = profile
    if len(profiles) < 2:
        return result

    # The domain must be one the stack accepts: user management refuses a
    # createUser outside AllowedSignUpEmailDomain when that parameter is set.
    admin_email = f"tenancy-verify-admin-{run}@{email_domain}"
    users: Dict[str, Dict[str, str]] = {}
    uploaded: List[str] = []
    admin_token: Optional[str] = None

    def wait_for(
        key: str, done: Callable[[Dict[str, Any]], bool]
    ) -> Optional[Dict[str, Any]]:
        deadline = clock() + timeout_seconds
        last: Optional[Dict[str, Any]] = None
        while True:
            status, body = api("getDocument", {"ObjectKey": key}, admin_token or "")
            last = _document(status, body) or last
            if last and done(last):
                return last
            if clock() >= deadline:
                return last
            sleep(poll_seconds)

    try:
        admin_token = make_admin(admin_email, _password())

        for t in (a, b):
            tid = t["tenantId"]
            email = f"tenancy-verify-{tid}-{run}@{email_domain}"
            status, body = api(
                "createUser",
                {
                    "email": email,
                    "persona": "Viewer",
                    "allowedConfigVersions": [t["profilePattern"]],
                },
                admin_token,
            )
            if status != 200 or not isinstance(body, dict) or not body.get("userId"):
                add(
                    f"create-user:{tid}",
                    _ERROR,
                    f"createUser returned {status}: {body}",
                )
                return result
            users[tid] = {"email": email, "userId": body["userId"]}
            users[tid]["token"] = make_password(email, _password())

        for t in (a, b):
            s3.put_object(Bucket=input_bucket, Key=keys[t["tenantId"]], Body=document)
            uploaded.append(keys[t["tenantId"]])

        for t in (a, b):
            tid = t["tenantId"]
            doc = wait_for(keys[tid], lambda d: bool(d.get("ConfigVersion")))
            if not doc:
                add(
                    f"ingest:{tid}",
                    _ERROR,
                    f"{keys[tid]!r} was not tracked within {timeout_seconds}s.",
                )
                return result
            if doc.get("ConfigVersion") == profiles[tid]:
                add(f"ingest:{tid}", _PASS, f"Processed under {profiles[tid]!r}.")
            else:
                add(
                    f"ingest:{tid}",
                    _FAIL,
                    f"Expected profile {profiles[tid]!r}, got {doc.get('ConfigVersion')!r} "
                    f"({doc.get('ObjectStatus')}).",
                )

        window = {
            "startDateTime": (started - timedelta(minutes=10))
            .isoformat()
            .replace("+00:00", "Z"),
            "endDateTime": (datetime.now(timezone.utc) + timedelta(minutes=10))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        for own, other in ((a, b), (b, a)):
            tid, oid = own["tenantId"], other["tenantId"]
            token = users[tid]["token"]
            own_key, other_key = keys[tid], keys[oid]

            status, body = api("getDocument", {"ObjectKey": own_key}, token)
            add(
                f"{tid}:getDocument:own",
                _PASS
                if _document(status, body)
                else (_ERROR if status == 0 else _FAIL),
                f"HTTP {status}",
            )
            status, body = api("getDocument", {"ObjectKey": other_key}, token)
            doc = _document(status, body)
            add(
                f"{tid}:getDocument:{oid}",
                _FAIL if doc else (_ERROR if status == 0 else _PASS),
                f"HTTP {status}"
                + (" — the other tenant's document was returned" if doc else ""),
            )

            for label, key, expect_ok in (
                ("own", own_key, True),
                (oid, other_key, False),
            ):
                status, body = api(
                    "getFileContents", {"s3Uri": f"s3://{input_bucket}/{key}"}, token
                )
                readable = (
                    status == 200 and isinstance(body, dict) and "content" in body
                )
                if status == 0:
                    outcome = _ERROR
                elif expect_ok:
                    outcome = _PASS if readable else _FAIL
                else:
                    outcome = (
                        _FAIL
                        if readable
                        else (_PASS if _refused(status, body) else _FAIL)
                    )
                add(f"{tid}:getFileContents:{label}", outcome, f"HTTP {status}")

            seen: List[str] = []
            next_token = None
            listed_ok = True
            for _ in range(20):
                args = dict(window, limit=100)
                if next_token:
                    args["nextToken"] = next_token
                status, body = api("listDocuments", args, token)
                if status != 200 or not isinstance(body, dict):
                    listed_ok = False
                    break
                seen.extend(
                    str(d.get("ObjectKey")) for d in body.get("Documents") or [] if d
                )
                next_token = body.get("nextToken")
                if not next_token:
                    break
            if not listed_ok:
                add(f"{tid}:listDocuments", _ERROR, f"HTTP {status}")
            elif other_key in seen:
                add(
                    f"{tid}:listDocuments",
                    _FAIL,
                    f"Lists {oid}'s document {other_key!r}.",
                )
            elif own_key not in seen:
                add(
                    f"{tid}:listDocuments",
                    _FAIL,
                    "Its own tenant's document is not listed.",
                )
            else:
                add(
                    f"{tid}:listDocuments",
                    _PASS,
                    f"{len(seen)} listed, none of {oid}'s.",
                )

        # The third element is the tenant whose profile the document may still be
        # processed under. Outside every tenant there is none, so only a refusal
        # passes. Under A's prefix with B's profile in its metadata, a mapping in
        # the default "Mapping wins" mode processes it under A's own profile and
        # ignores the metadata: that keeps it inside A and is a pass, as is a
        # refusal. What must never happen is processing under B's profile.
        strict_cases = [
            (
                "ingest:outside-every-tenant",
                f"tenancy-verify-outside-{run}/{document_name}",
                None,
                None,
            ),
            (
                f"ingest:{a['tenantId']}-prefix-with-{b['tenantId']}-metadata",
                f"{sorted(a['prefixes'])[0]}tenancy-verify-cross-{run}/{document_name}",
                profiles[b["tenantId"]],
                a,
            ),
        ]
        for name, key, metadata_profile, may_stay_in in strict_cases:
            if isolation_mode != "Strict":
                add(
                    name,
                    _SKIP,
                    f"The stack runs with TenantIsolationMode={isolation_mode}.",
                )
                continue
            extra = (
                {"Metadata": {"config-version": metadata_profile}}
                if metadata_profile
                else {}
            )
            s3.put_object(Bucket=input_bucket, Key=key, Body=document, **extra)
            uploaded.append(key)
            doc = wait_for(
                key, lambda d: d.get("ObjectStatus") in ("FAILED", "COMPLETED")
            )
            if not doc:
                add(name, _ERROR, f"{key!r} was not tracked within {timeout_seconds}s.")
            elif doc.get("ObjectStatus") == "FAILED" and doc.get(
                "ConfigAssignmentError"
            ):
                add(name, _PASS, str(doc.get("ConfigAssignmentError"))[:200])
            elif (
                may_stay_in is not None
                and doc.get("ObjectStatus") == "COMPLETED"
                and tenant_for_profile(doc.get("ConfigVersion"), [may_stay_in])
            ):
                add(
                    name,
                    _PASS,
                    f"Processed under {doc.get('ConfigVersion')!r}, its own tenant's "
                    f"profile; the other tenant's profile in its metadata was not used.",
                )
            else:
                add(
                    name,
                    _FAIL,
                    f"Not refused at ingest: status {doc.get('ObjectStatus')}, profile "
                    f"{doc.get('ConfigVersion')!r}.",
                )
    except Exception as exc:  # noqa: BLE001 - reported as a row, then cleaned up
        add("run", _ERROR, f"{type(exc).__name__}: {exc}")
    finally:
        problems: List[str] = []
        if uploaded and admin_token:
            status, _ = api("deleteDocument", {"objectKeys": uploaded}, admin_token)
            if status != 200:
                problems.append(f"deleteDocument returned {status}")
        for key in uploaded:
            try:
                s3.delete_object(Bucket=input_bucket, Key=key)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"delete {key}: {exc}")
        for tid, user in users.items():
            if admin_token:
                status, _ = api("deleteUser", {"userId": user["userId"]}, admin_token)
                if status != 200:
                    problems.append(f"deleteUser {tid} returned {status}")
            try:
                delete_cognito_user(user["email"])
            except Exception:  # noqa: BLE001 - deleteUser usually removed it already
                pass
        try:
            delete_cognito_user(admin_email)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"delete admin user {admin_email}: {exc}")
        if problems:
            add("cleanup", _ERROR, "; ".join(problems))
    return result
