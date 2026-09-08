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
AWS_PROFILE=default python3 scripts/security/live_checks/verify_idp_group_mapping.py
AWS_PROFILE=default python3 scripts/security/live_checks/verify_execution_scope.py
```

Both exit non-zero if any check fails, and print a `PASS`/`FAIL` line per check.

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
