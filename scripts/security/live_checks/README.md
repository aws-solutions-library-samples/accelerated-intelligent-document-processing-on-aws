<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Live authorization checks

Two self-contained harnesses that verify an authorization control against **real
AWS services** rather than mocks. Each one creates its own throwaway resources,
asserts, and deletes everything in a `finally` block — none of them touch a
deployed stack, so they are safe to run in any account you own.

They exist because the controls they cover depend on AWS behaviour that a mock
cannot tell you about, and in one case on behaviour the AWS docs describe
inconsistently. They are not part of CI (they create IAM roles and Cognito
pools); run them when changing the code they cover.

```bash
make live-auth-checks            # both of the self-contained checks below
```

Both exit non-zero if any check fails, and print a `PASS`/`FAIL` line per check.

A third check, `verify_federated_signin.py`, needs a deployed stack and the
throwaway OIDC provider in `oidc_provider/`; run it with
`make verify-idp-federation`. The full sequence, and the Cognito behaviours these
exist to pin down, are in `.claude/skills/live-auth-checks.md`.

## `verify_idp_group_mapping.py`

Covers `ExternalIdPGroupMappingFunction` — the Cognito pre-token-generation
trigger that maps an external IdP's group claim to Cognito groups (AUTH.T13).

It extracts the handler's `InlineCode` **from `template.yaml`**, which is the copy
that actually deploys, and runs it in a real `python3.12` Lambda with the same IAM
policy `ExternalIdPGroupMappingCognitoPolicy` grants, against a real user pool
holding a SAML provider, a native user, and a user linked to that provider with
`AdminLinkProviderForUser`.

What it establishes that unit tests cannot:

- the inline handler imports and runs as deployed;
- the `cognito-idp:AdminGetUser` grant is sufficient for the provenance read;
- Cognito's stored `identities` attribute parses as the handler expects and its
  `providerName` matches the configured provider (its `primary` and `dateCreated`
  come back as *strings*, which is why the unit fixture uses strings too);
- the group add **and remove** side effects land in Cognito.

The six scenarios: a native user who set `custom:idp_groups` on themselves gains
nothing; a federated fresh sign-in is granted the mapped group; a federated user
who sets the attribute themselves and then *refreshes* gains nothing; a fresh
sign-in with a demoted claim removes the old group; an `identities` entry placed
in the trigger *event* grants nothing (so provenance comes from `AdminGetUser`,
not the event); and a non-matching `EXTERNAL_IDP_NAME` grants nothing.

**Not covered:** a real SAML assertion round-trip. The generated metadata points
at a non-existent SSO endpoint, so this cannot prove Cognito's `AttributeMapping`
write still succeeds at federated sign-in now that `UserPoolClient` declares an
explicit `WriteAttributes`. The static guard for that is
`scripts/sdlc/tests/test_userpool_attribute_permissions.py`, which asserts every
mapped attribute is in the write list; proving it end to end needs a real IdP.

## `verify_execution_scope.py`

Covers the two authorization checks in `getStepFunctionExecution` (formerly the
accepted gap GAP-01).

It deploys the shipped resolver source into a real Lambda with the IAM policy the
template grants it, against two real state machines, real executions carrying the
compressed-document wrapper the pipeline sends, and a real DynamoDB table shaped
like `UsersTable` with an `EmailIndex` GSI.

The two state machines are named so that the resolver's IAM grant covers **both**:
the policy allows `execution:<stack>-*`, and the second machine is named as a
sibling deployment whose stack name extends the first (`tmp-idpverify-*` also
matches `tmp-idpverify-prod-...`). Check 0 asserts that the sibling execution
really is describable, so every refusal that follows demonstrably comes from the
resolver code rather than from IAM.

The scenarios: the sibling deployment's execution is refused; the stack's own
execution is served with its real step history parsed; a config-scoped caller is
served an in-scope execution and refused an out-of-scope one via a real
`EmailIndex` Query; an Admin is not scoped; a malformed ARN is refused; and a
genuine not-found still returns the ordinary error payload rather than a denial,
so operational failures and authorization denials stay distinguishable.

Denials are asserted to surface as a Lambda `FunctionError` of type
`PermissionError` with a message beginning `Unauthorized`, which is what
`http_api_dispatcher` maps to HTTP 403.

## `verify_federated_signin.py` + `oidc_provider/`

The only check that exercises a **real federated sign-in**, so Cognito performs
OIDC discovery, the authorization-code exchange, JWT signature validation against
a real JWKS, its own `AttributeMapping` write, and then fires the pre-token
trigger. The `AttributeMapping` write is what an explicit `WriteAttributes` can
break, and it fails the *sign-in* rather than the deploy.

`oidc_provider/` is a real minimal OIDC provider on API Gateway + Lambda:
discovery document, `/authorize`, `/token` issuing RS256-signed id_tokens,
`/userinfo`, and JWKS. RS256 signing uses only the standard library (PKCS#1 v1.5
padding plus a modular exponentiation), so the Lambda needs no dependencies and no
layer. `/authorize` approves without authenticating anyone — that is what makes
the flow scriptable with plain HTTP and no browser, and also why it is fit only
for verification. `deploy.py up` stands it up and prints the `ExternalIdP*` stack
parameters to deploy against it; `deploy.py down` removes it and its secret.

Scenarios: a first federated sign-in maps the asserted group and the **first**
token carries it; a refresh after the attribute is rewritten out of band changes
nothing; the deployed trigger removes a stale role on a demoted claim; a
non-mapped attribute is not writable by an end user while the mapped one is; and a
native user who wrote `custom:idp_groups` themselves gains no role.

Section 3b (a second federated sign-in by the same user) reads the pool's `email`
schema flag: on a pool created with `ExternalIdPEmailMutable=true` it is a real
PASS/FAIL check; on a pool created with the default `false` it is recorded as a
`NOTE`, because such a pool lets each federated user sign in exactly once (#835).
See the skill for what that means and why it is not your change.
