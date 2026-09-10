#!/usr/bin/env python3
"""End-to-end verification against a deployed stack with a real external IdP.

Covers what the isolated live checks cannot: a genuine federated sign-in through
Cognito's hosted UI, so Cognito performs OIDC discovery, the code exchange, JWT
signature validation, its own AttributeMapping write, and then fires the
pre-token-generation trigger. That AttributeMapping write is the one thing an
explicit WriteAttributes could break, and it fails the *sign-in* rather than the
deploy, so nothing short of this exercises it.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.parse

import boto3
import requests

ADMIN_IDP_GROUP = "IdP-Admins"
VIEWER_IDP_GROUP = "IdP-Viewers"
MANAGED = {"Admin", "Author", "Reviewer", "Viewer"}

FED_EMAIL = "fed-admin@example.invalid"
NATIVE_EMAIL = "native-probe@example.invalid"
NATIVE_PW = "Verify-Tmp!9xQz"

_results: list[tuple[bool, str, str]] = []
_notes: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    _results.append((ok, label, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if detail:
        print(f"          {detail}")
    return ok


def note(text: str) -> None:
    """Record an observation about pre-existing behaviour, not a pass/fail."""
    _notes.append(text)
    print(f"  NOTE  {text}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def managed_groups(cog, pool_id: str, username: str) -> set[str]:
    """The app's role groups a user is in.

    Cognito auto-creates a real group per identity provider
    (`<region>_<pool>_<Provider>`) and adds federated users to it. That is not one
    of the app's roles, so it is filtered out here.
    """
    resp = cog.admin_list_groups_for_user(UserPoolId=pool_id, Username=username)
    return {g["GroupName"] for g in resp.get("Groups", [])} & MANAGED


def find_federated_user(cog, pool_id: str) -> str | None:
    for u in cog.list_users(UserPoolId=pool_id, Limit=60).get("Users", []):
        attrs = {a["Name"]: a["Value"] for a in u.get("Attributes", [])}
        if "identities" in attrs:
            return u["Username"]
    return None


def delete_federated_user(cog, pool_id: str) -> None:
    """Remove the federated user record so the next sign-in is a first sign-in.

    Needed because this pool's `email` attribute is `Mutable: false` and Cognito
    rewrites mapped attributes on every federated sign-in, so a second sign-in by
    an existing federated user is rejected with `user.email: Attribute cannot be
    updated`. Pre-existing and independent of the changes under test (see
    docs/external-idp.md); deleting the record is the one-shot bridge that doc
    describes.
    """
    username = find_federated_user(cog, pool_id)
    if username:
        cog.admin_delete_user(UserPoolId=pool_id, Username=username)
        print(f"  removed federated user record {username}")


def set_mock_user(lam, function_name: str, **fields) -> None:
    user = {
        "sub": "fed-1",
        "email": FED_EMAIL,
        "given_name": "Fed",
        "family_name": "Admin",
    }
    user.update(fields)
    cfg = lam.get_function_configuration(FunctionName=function_name)
    env = cfg["Environment"]["Variables"]
    env["MOCK_USER"] = json.dumps(user)
    lam.update_function_configuration(
        FunctionName=function_name, Environment={"Variables": env}
    )
    lam.get_waiter("function_updated_v2").wait(FunctionName=function_name)
    print(f"  mock IdP will assert memberOf={user.get('memberOf', '<omitted>')!r}")


def federated_signin(domain: str, client_id: str, redirect_uri: str, idp_name: str):
    """Drive the hosted-UI authorization-code flow end to end."""
    session = requests.Session()
    url = f"{domain}/oauth2/authorize?" + urllib.parse.urlencode(
        {
            "identity_provider": idp_name,
            "client_id": client_id,
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": redirect_uri,
        }
    )
    hops: list[str] = []
    code = None
    for _ in range(12):
        r = session.get(url, allow_redirects=False, timeout=30)
        hops.append(f"{r.status_code} {url.split('?')[0].split('/')[-1] or '/'}")
        if r.status_code not in (301, 302, 303, 307, 308):
            break
        location = r.headers["Location"]
        parsed = urllib.parse.urlparse(location)
        qs = urllib.parse.parse_qs(parsed.query)
        if "error" in qs or "error_description" in qs:
            msg = (qs.get("error_description") or qs.get("error") or [""])[0]
            hops.append(f"ERROR {msg.strip()}")
            return None, hops
        if location.startswith(redirect_uri) and "code" in qs:
            code = qs["code"][0]
            hops.append("-> app callback with code")
            break
        url = location if parsed.netloc else urllib.parse.urljoin(url, location)
    if not code:
        return None, hops
    r = session.post(
        f"{domain}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if r.status_code != 200:
        hops.append(f"token exchange {r.status_code}: {r.text[:160]}")
        return None, hops
    return r.json(), hops


def pretoken_event(pool_id: str, username: str, groups_value: str, source: str) -> dict:
    return {
        "version": "1",
        "triggerSource": source,
        "userPoolId": pool_id,
        "userName": username,
        "request": {
            "userAttributes": {"custom:idp_groups": groups_value},
            "groupConfiguration": {},
        },
        "response": {},
    }


def override_groups(payload: dict) -> list | None:
    response = payload.get("response") or {}
    details = response.get("claimsOverrideDetails") or response.get(
        "claimsAndScopeOverrideDetails"
    )
    if not details:
        return None
    return (details.get("groupOverrideDetails") or {}).get("groupsToOverride")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack-name", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--idp-function", required=True)
    ap.add_argument("--idp-name", required=True)
    ap.add_argument("--trigger-function", required=True)
    ap.add_argument("--pool-id", required=True)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--domain", required=True)
    args = ap.parse_args()

    s = boto3.Session(region_name=args.region)
    cfn = s.client("cloudformation")
    cog = s.client("cognito-idp")
    lam = s.client("lambda")

    out = {}
    for st in cfn.describe_stacks(StackName=args.stack_name)["Stacks"]:
        for o in st.get("Outputs") or []:
            out[o["OutputKey"]] = o["OutputValue"]
    redirect_uri = (out.get("ApplicationWebURL") or "").rstrip("/")
    pool_id, client_id = args.pool_id, args.client_id
    domain = args.domain.rstrip("/")
    print(f"Hosted UI: {domain}\nRedirect:  {redirect_uri}")

    # ------------------------------------------------------------------ config
    section("0. deployed configuration")
    client = cog.describe_user_pool_client(UserPoolId=pool_id, ClientId=client_id)[
        "UserPoolClient"
    ]
    write_attrs = client.get("WriteAttributes") or []
    check(bool(write_attrs), "UserPoolClient declares WriteAttributes",
          f"WriteAttributes={sorted(write_attrs)}")
    check("custom:idp_groups" in write_attrs,
          "custom:idp_groups is writable (required for IdP AttributeMapping)")
    check("preferred_username" not in write_attrs,
          "a non-mapped attribute is absent from the write list")
    check(args.idp_name in (client.get("SupportedIdentityProviders") or []),
          f"{args.idp_name} is a supported provider")

    # ------------------------------------- 1. real federated sign-in (the R2 gap)
    section("1. real federated sign-in, IdP asserts IdP-Admins")
    delete_federated_user(cog, pool_id)
    set_mock_user(lam, args.idp_function, memberOf=ADMIN_IDP_GROUP)
    tokens, hops = federated_signin(domain, client_id, redirect_uri, args.idp_name)
    print("     " + " | ".join(hops))
    fed_username = None
    if check(tokens is not None,
             "federated sign-in completed — Cognito's AttributeMapping write "
             "succeeded with WriteAttributes declared"):
        claims = jwt_claims(tokens["id_token"])
        groups = claims.get("cognito:groups") or []
        fed_username = claims.get("cognito:username") or claims.get("sub")
        check("Admin" in groups,
              "the FIRST token already carries the mapped Admin group",
              f"cognito:groups={groups}")
        attrs = {
            a["Name"]: a["Value"]
            for a in cog.admin_get_user(UserPoolId=pool_id, Username=fed_username)[
                "UserAttributes"
            ]
        }
        check(attrs.get("custom:idp_groups") == ADMIN_IDP_GROUP,
              "Cognito's AttributeMapping wrote custom:idp_groups",
              f"stored={attrs.get('custom:idp_groups')!r}")
        check("identities" in attrs,
              "the federated user has an identities attribute to check provenance against")
        check(managed_groups(cog, pool_id, fed_username) == {"Admin"},
              "Cognito role membership synced to Admin")

    # ----------------------------------------------------- 2. refresh (freshness)
    if fed_username:
        section("2. attribute rewritten out of band, then a token REFRESH")
        cog.admin_update_user_attributes(
            UserPoolId=pool_id, Username=fed_username,
            UserAttributes=[{"Name": "custom:idp_groups", "Value": VIEWER_IDP_GROUP}],
        )
        print(f"  set custom:idp_groups={VIEWER_IDP_GROUP} via the admin API")
        r = requests.post(
            f"{domain}/oauth2/token",
            data={"grant_type": "refresh_token", "client_id": client_id,
                  "refresh_token": tokens["refresh_token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30,
        )
        if check(r.status_code == 200, "refresh succeeded", f"HTTP {r.status_code}"):
            tok_groups = jwt_claims(r.json()["id_token"]).get("cognito:groups") or []
            roles = managed_groups(cog, pool_id, fed_username)
            check(roles == {"Admin"} and "Viewer" not in tok_groups,
                  "the refresh did NOT act on the rewritten attribute",
                  f"role membership={roles}, token groups={tok_groups}")

        # ------------------------------- 3. removal path on the deployed trigger
        section("3. deployed trigger, fresh sign-in event with a demoted claim")
        print("  invoked directly: a second hosted-UI sign-in by an existing")
        print("  federated user is blocked by this pool's immutable email")
        print("  attribute (pre-existing — see 3b), so the removal path is")
        print("  exercised against the deployed function instead.")
        resp = lam.invoke(
            FunctionName=args.trigger_function,
            Payload=json.dumps(
                pretoken_event(pool_id, fed_username, VIEWER_IDP_GROUP,
                               "TokenGeneration_HostedAuth")
            ).encode(),
        )
        body = json.loads(resp["Payload"].read())
        if check(not resp.get("FunctionError"), "trigger returned normally",
                 str(body)[:160]):
            check(managed_groups(cog, pool_id, fed_username) == {"Viewer"},
                  "Admin removed and Viewer granted on the demoted claim",
                  f"role membership={managed_groups(cog, pool_id, fed_username)}")
            check(override_groups(body) == ["Viewer"],
                  f"token override = {override_groups(body)}")

        section("3b. second hosted-UI sign-in by the SAME federated user")
        set_mock_user(lam, args.idp_function, memberOf=ADMIN_IDP_GROUP)
        tokens2, hops2 = federated_signin(domain, client_id, redirect_uri, args.idp_name)
        print("     " + " | ".join(hops2))
        if tokens2 is None and any("cannot be updated" in h for h in hops2):
            note("second federated sign-in fails with `user.email: Attribute cannot "
                 "be updated` — this pool's email attribute is Mutable: false. "
                 "PRE-EXISTING and unrelated to the changes under test; documented "
                 "in docs/external-idp.md. It does mean IdP-driven group changes "
                 "cannot reach an existing federated user on such a pool.")
        else:
            note(f"second sign-in {'succeeded' if tokens2 else 'failed'}: "
                 f"{' | '.join(hops2)}")

    # ------------------------------------------- 4. attribute write permissions
    section("4. attribute write permissions on the deployed client")
    try:
        cog.admin_delete_user(UserPoolId=pool_id, Username=NATIVE_EMAIL)
    except Exception:
        pass
    cog.admin_create_user(
        UserPoolId=pool_id, Username=NATIVE_EMAIL, MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "email", "Value": NATIVE_EMAIL},
                        {"Name": "email_verified", "Value": "true"}],
    )
    cog.admin_set_user_password(
        UserPoolId=pool_id, Username=NATIVE_EMAIL, Password=NATIVE_PW, Permanent=True
    )
    preserve = {
        k: v for k, v in client.items()
        if k in ("ClientName", "RefreshTokenValidity", "AccessTokenValidity",
                 "IdTokenValidity", "TokenValidityUnits", "ReadAttributes",
                 "WriteAttributes", "SupportedIdentityProviders", "CallbackURLs",
                 "LogoutURLs", "AllowedOAuthFlows", "AllowedOAuthScopes",
                 "AllowedOAuthFlowsUserPoolClient", "PreventUserExistenceErrors",
                 "EnableTokenRevocation")
    }
    flows = client.get("ExplicitAuthFlows") or []
    restore = None
    if "ALLOW_ADMIN_USER_PASSWORD_AUTH" not in flows:
        restore = list(flows)
        cog.update_user_pool_client(
            UserPoolId=pool_id, ClientId=client_id,
            ExplicitAuthFlows=flows + ["ALLOW_ADMIN_USER_PASSWORD_AUTH"], **preserve
        )
        print("  temporarily enabled ADMIN_USER_PASSWORD_AUTH")
    try:
        auth = cog.admin_initiate_auth(
            UserPoolId=pool_id, ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": NATIVE_EMAIL, "PASSWORD": NATIVE_PW},
        )
        access_token = auth["AuthenticationResult"]["AccessToken"]
        # custom:idp_groups MUST stay writable while it is IdP-mapped: Cognito
        # applies AttributeMapping as this client and fails the sign-in otherwise.
        # So the trigger, not the write list, is what makes its origin decisive.
        try:
            cog.update_user_attributes(
                AccessToken=access_token,
                UserAttributes=[{"Name": "custom:idp_groups", "Value": ADMIN_IDP_GROUP}],
            )
            check(True, "custom:idp_groups is writable, as IdP mapping requires")
        except cog.exceptions.NotAuthorizedException as e:
            check(False, "custom:idp_groups is writable, as IdP mapping requires",
                  f"refused — this would break federated sign-in: {e}")
        try:
            cog.update_user_attributes(
                AccessToken=access_token,
                UserAttributes=[{"Name": "preferred_username", "Value": "nope"}],
            )
            check(False, "a non-mapped attribute is NOT writable",
                  "the write succeeded — WriteAttributes is too wide")
        except cog.exceptions.NotAuthorizedException as e:
            check(True, "a non-mapped attribute is NOT writable", str(e)[:100])

        # ------------------- 5. the case the reports describe, on the real stack
        section("5. native user WITH custom:idp_groups set signs in")
        stored = {
            a["Name"]: a["Value"]
            for a in cog.admin_get_user(UserPoolId=pool_id, Username=NATIVE_EMAIL)[
                "UserAttributes"
            ]
        }
        check(stored.get("custom:idp_groups") == ADMIN_IDP_GROUP,
              "the native user's own write persisted",
              f"stored={stored.get('custom:idp_groups')!r}")
        auth = cog.admin_initiate_auth(
            UserPoolId=pool_id, ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": NATIVE_EMAIL, "PASSWORD": NATIVE_PW},
        )
        tok_groups = jwt_claims(
            auth["AuthenticationResult"]["IdToken"]
        ).get("cognito:groups") or []
        assigned = managed_groups(cog, pool_id, NATIVE_EMAIL)
        check("Admin" not in tok_groups and assigned == set(),
              "the native user gained NO role from the attribute they set",
              f"token groups={tok_groups or '[]'}, role membership={assigned or '{}'}")
    finally:
        if restore is not None:
            cog.update_user_pool_client(
                UserPoolId=pool_id, ClientId=client_id,
                ExplicitAuthFlows=restore, **preserve
            )
            print("\n  restored the app client's auth flows")
        try:
            cog.admin_delete_user(UserPoolId=pool_id, Username=NATIVE_EMAIL)
            print("  deleted the native probe user")
        except Exception as e:
            print(f"  WARN could not delete probe user: {e}")

    failed = [f"{label} — {detail}" for ok, label, detail in _results if not ok]
    print(f"\n{'=' * 72}")
    print(f"{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED:")
        for f in failed:
            print(f"  - {f}")
    if _notes:
        print("\nPre-existing behaviour observed (not a pass/fail of this change):")
        for n in _notes:
            print(f"  - {n}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
