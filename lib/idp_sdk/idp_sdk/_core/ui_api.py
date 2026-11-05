# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Drive the web UI's REST API (``POST /op/<field>``) as a real Cognito user.

Used by ``tenancy verify``, which has to observe what a *scoped user* is refused —
something no admin credential can show. The same primitives exist in
``scripts/rbac_common.py`` for the repository's own RBAC harness; the SDK cannot
import from ``scripts/``, so this is the boto3 form of them, and both must keep the
two behaviours that harness learned the hard way:

* **UpdateUserPoolClient is a full replace.** Changing only the auth flows clears
  every other property — callback URLs, OAuth flows, identity providers,
  ``WriteAttributes`` — and CloudFormation will not restore them. So the client is
  read, one field changed, and the whole thing written back; and restored the same
  way.
* **The auth flow takes a few seconds to propagate.** ``AdminInitiateAuth`` can
  answer "Auth flow not enabled for this client" right after the update; only that
  message is retried, and the wait is bounded.

No token is ever logged or returned to a caller outside this module's users.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

ADMIN_AUTH_FLOW = "ALLOW_ADMIN_USER_PASSWORD_AUTH"
AUTH_FLOW_NOT_PROPAGATED = "Auth flow not enabled for this client"
AUTH_FLOW_RETRY_DELAYS = (1, 2, 4, 8, 15, 30)
_CLIENT_READ_ONLY_FIELDS = frozenset(
    {"ClientSecret", "CreationDate", "LastModifiedDate"}
)
REQUEST_TIMEOUT_SECONDS = 40


@dataclass
class UiContext:
    stack: str
    region: Optional[str]
    user_pool: str
    client: str
    api_base: str
    users_table: str


def resolve_ui_context(stack: str, region: Optional[str], cfn: Any = None) -> UiContext:
    """The Cognito pool and client, the API base URL and the UsersTable of a stack."""
    import boto3

    cfn = cfn or boto3.client("cloudformation", region_name=region)

    def physical(stack_name: str) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for page in cfn.get_paginator("list_stack_resources").paginate(
            StackName=stack_name
        ):
            for r in page.get("StackResourceSummaries", []):
                found[r["LogicalResourceId"]] = r.get("PhysicalResourceId", "")
        return found

    top = physical(stack)
    pool, client, api_stack = (
        top.get("UserPool"),
        top.get("UserPoolClient"),
        top.get("APIRESOLVERSTACK"),
    )
    if not (pool and client and api_stack):
        raise RuntimeError(
            f"Stack {stack!r} has no web UI user pool, app client or API stack; "
            f"tenancy verify needs the UI API."
        )
    outputs = cfn.describe_stacks(StackName=api_stack)["Stacks"][0].get("Outputs", [])
    api_base = next(
        (o["OutputValue"] for o in outputs if o.get("OutputKey") == "HttpApiEndpoint"),
        "",
    )
    if not api_base:
        raise RuntimeError(f"The API stack of {stack!r} has no HttpApiEndpoint output.")
    return UiContext(
        stack=stack,
        region=region,
        user_pool=pool,
        client=client,
        api_base=api_base.rstrip("/"),
        users_table=top.get("UsersTable", ""),
    )


class AdminAuthFlow:
    """Context manager: enable ADMIN_USER_PASSWORD_AUTH on the app client, then restore.

    The original flow list is captured and written back verbatim on exit, through
    the same read-modify-write, so the client ends exactly as it started.
    """

    def __init__(self, ctx: UiContext, cognito: Any):
        self.ctx = ctx
        self.cognito = cognito
        self._original: Optional[list] = None

    def _describe(self) -> Dict[str, Any]:
        return self.cognito.describe_user_pool_client(
            UserPoolId=self.ctx.user_pool, ClientId=self.ctx.client
        )["UserPoolClient"]

    def _set_flows(self, flows: list) -> None:
        current = self._describe()
        desired = {
            k: v
            for k, v in current.items()
            if k not in _CLIENT_READ_ONLY_FIELDS and v is not None
        }
        desired["ExplicitAuthFlows"] = list(flows)
        self.cognito.update_user_pool_client(**desired)

    def __enter__(self) -> "AdminAuthFlow":
        flows = list(self._describe().get("ExplicitAuthFlows") or [])
        self._original = flows
        if ADMIN_AUTH_FLOW not in flows:
            self._set_flows([*flows, ADMIN_AUTH_FLOW])
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._original is not None and ADMIN_AUTH_FLOW not in self._original:
            self._set_flows(self._original)


def create_cognito_user(
    cognito: Any, pool: str, email: str, password: str, group: Optional[str]
) -> None:
    """A confirmed user with a permanent password, optionally in one group."""
    try:
        cognito.admin_create_user(
            UserPoolId=pool,
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
    except cognito.exceptions.UsernameExistsException:
        pass
    set_password(cognito, pool, email, password)
    if group:
        cognito.admin_add_user_to_group(
            UserPoolId=pool, Username=email, GroupName=group
        )


def set_password(cognito: Any, pool: str, email: str, password: str) -> None:
    cognito.admin_set_user_password(
        UserPoolId=pool, Username=email, Password=password, Permanent=True
    )


def id_token(
    cognito: Any, ctx: UiContext, email: str, password: str, sleep=time.sleep
) -> str:
    """A Cognito ID token — the bearer the API's user-pool authorizer expects."""
    for delay in (*AUTH_FLOW_RETRY_DELAYS, None):
        try:
            result = cognito.admin_initiate_auth(
                UserPoolId=ctx.user_pool,
                ClientId=ctx.client,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
            return result["AuthenticationResult"]["IdToken"]
        except Exception as exc:  # noqa: BLE001 - only one message is retried
            if delay is None or AUTH_FLOW_NOT_PROPAGATED not in str(exc):
                raise
            logger.info("Auth flow not yet active on the app client; retrying")
            sleep(delay)
    raise AssertionError("unreachable")


def call(
    api_base: str, field: str, args: Dict[str, Any], token: str
) -> Tuple[int, Any]:
    """``POST /op/<field>``. Returns ``(http_status, parsed_body_or_None)``.

    Status 0 means the request did not complete, which a caller must treat as an
    error, never as a refusal.
    """
    req = urllib.request.Request(
        f"{api_base}/op/{field}",
        data=json.dumps({"arguments": args}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": token},
    )
    try:
        with urllib.request.urlopen(  # nosec B310 - the stack's own API base URL, resolved from CloudFormation
            req, timeout=REQUEST_TIMEOUT_SECONDS
        ) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"requestError": str(e)}
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, {"unreadableBody": raw.decode("utf-8", "replace")[:200]}
