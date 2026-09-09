# Live authorization checks

Use this when changing **`ExternalIdPGroupMappingFunction`** (the Cognito
pre-token-generation trigger), **`getStepFunctionExecution`**, or
`UserPoolClient`'s attribute permissions — or when you need to prove an
authorization control behaves as intended against real AWS rather than mocks.

Three layers, cheapest first. Run the cheap ones always; the federated one only
when you touch the IdP group-mapping path, because it needs a stack.

| Layer | Command | Needs | Runtime |
|-------|---------|-------|---------|
| Static guard | `pytest scripts/sdlc/tests/test_userpool_attribute_permissions.py` | nothing | <1s |
| Live checks | `make live-auth-checks` | AWS creds | ~3 min |
| Federated sign-in | `make verify-idp-federation ...` | a deployed stack + the throwaway OIDC provider | ~30 min incl. deploy |

## Why these exist

Each covers behaviour a mock cannot tell you about, and in two cases behaviour the
AWS documentation describes inconsistently or not at all:

- **Cognito's default `WriteAttributes`.** With `WriteAttributes` unset, a user's
  own access token *can* write custom attributes — measured against a live pool.
  The `WriteAttributes` API reference says the default is "the Standard
  attributes", implying otherwise; the Developer Guide's "Attribute permissions
  and scopes" section agrees with what was measured. Don't answer this from the
  docs.
- **An IdP-mapped attribute must stay writable by the app client.** Cognito
  applies `AttributeMapping` *as the app client* and fails the federated sign-in
  if it cannot write a mapped attribute. So `custom:idp_groups` cannot simply be
  removed from the write list while it is mapped — the trigger's provenance check
  is what makes its origin decisive.
- **The pre-token response key depends on the trigger's event version.** `V1_0`
  reads `claimsOverrideDetails`; `V2_0`/`V3_0` read
  `claimsAndScopeOverrideDetails`. `template.yaml` registers the trigger with
  `PreTokenGeneration:`, which is `V1_0`. Emitting only the V2 name means the
  override is silently ignored and the first token carries no group claim. The
  handler emits both.
- **Cognito auto-creates a group per identity provider** named
  `<region>_<pool>_<Provider>` and adds federated users to it. It is a real group
  in `AdminListGroupsForUser` output but not one of the app's roles — filter it
  out before asserting on role membership.

## `make live-auth-checks`

Two harnesses in `scripts/security/live_checks/`. Each creates its own throwaway
Cognito pool / state machines / DynamoDB table / IAM roles, asserts, and deletes
everything in a `finally` block. Neither touches a deployed stack, so they are
safe in any account you own. Not wired into CI because they create IAM roles.

`verify_idp_group_mapping.py` extracts the trigger's `InlineCode` **from
`template.yaml`** — the copy that actually deploys — and runs it in a real
`python3.12` Lambda with the same IAM policy the template grants, against a pool
holding a SAML provider and a user linked with `AdminLinkProviderForUser`. If you
change the handler in only one of its two copies, this fails.

`verify_execution_scope.py` deploys the shipped resolver with its shipped IAM
policy against two real state machines, named so the policy's `<stack>-*` prefix
covers both. Check 0 asserts the sibling execution really is describable, so every
refusal that follows is demonstrably the code's and not IAM's.

## `make verify-idp-federation`

The only layer that exercises a **real federated sign-in**: Cognito does OIDC
discovery, the authorization-code exchange, JWT signature validation against a
real JWKS, its own `AttributeMapping` write, and then fires the trigger. That
`AttributeMapping` write is the thing an explicit `WriteAttributes` can break, and
it fails the *sign-in*, not the deploy — so nothing cheaper covers it.

`scripts/security/live_checks/oidc_provider/` holds a real minimal OIDC provider
(discovery, `/authorize`, `/token` issuing RS256-signed id_tokens, `/userinfo`,
JWKS). Its `/authorize` approves without authenticating anyone, which is what
makes the flow scriptable with plain HTTP and no browser — and also why it is fit
only for verification. Signing uses the standard library alone, so the Lambda has
no dependencies. Which user it asserts, and which group claim, is the `MOCK_USER`
environment variable; the checks change it between scenarios.

Sequence — the provider must exist before the stack that federates to it:

```bash
# 1. Stand up the provider. Prints the stack parameters to use.
python3 scripts/security/live_checks/oidc_provider/deploy.py up --region us-west-2

# 2. Publish the branch and create a stack with those ExternalIdP* parameters.
env -i HOME=$HOME PATH=/usr/local/bin:/usr/bin:/bin AWS_PROFILE=default bash -lc \
  'cd '$PWD' && python3 publish.py <bucket-basename> idp us-west-2 --clean-build'
aws cloudformation create-stack --stack-name IDPVerify \
  --template-url https://s3.<region>.amazonaws.com/<bucket>/idp/idp-main.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --parameters ParameterKey=AdminEmail,ParameterValue=you@example.com \
               <the ExternalIdP* parameters step 1 printed>

# 3. Collect the pool / client / domain / trigger, then run the checks.
POOL=$(aws cognito-idp list-user-pools --max-results 60 \
  --query "UserPools[?Name=='IDPVerify-users'].Id" --output text)
make verify-idp-federation STACK_NAME=IDPVerify REGION=us-west-2 \
  POOL_ID=$POOL \
  CLIENT_ID=$(aws cognito-idp list-user-pool-clients --user-pool-id $POOL \
    --query "UserPoolClients[?ClientName=='IDPVerify-Client'].ClientId" --output text) \
  DOMAIN=https://$(aws cognito-idp describe-user-pool --user-pool-id $POOL \
    --query UserPool.Domain --output text).auth.us-west-2.amazoncognito.com \
  IDP_FUNCTION=<from step 1> \
  TRIGGER_FUNCTION=<the stack's ExternalIdPGroupMappingFunction>

# 4. Tear down.
aws cloudformation delete-stack --stack-name IDPVerify
python3 scripts/security/live_checks/oidc_provider/deploy.py down --region us-west-2
```

### Expect one recorded NOTE, not a failure

The checks print a `NOTE` for behaviour that is pre-existing rather than a
pass/fail of your change. Today there is one: **a second federated sign-in by the
same user fails** with `user.email: Attribute cannot be updated`, because the
pool's `email` attribute is `Mutable: false` (reverted to `false` in `3bed47097`
to keep stack updates working — a schema flag cannot be changed on an existing
pool). Cognito rewrites mapped attributes on every federated sign-in, so the
second one is rejected. Consequences to keep in mind:

- IdP-driven group changes cannot reach an *existing* federated user on such a
  pool. The checks therefore exercise the group-removal path by invoking the
  deployed trigger directly, and delete the federated user record between
  sign-ins (the one-shot bridge `docs/external-idp.md` documents).
- Do not read this as your change breaking federation. Confirm by checking
  whether `email` is `Mutable: false` on the pool.

## What `make api-test` covers, and its one precondition

The live API suite (`.claude/skills/api-rbac-test.md`) owns the per-operation
authorization matrix, and includes the **`SEC-2.1-CALLER-SUPPLIED-REF`** suite:
`getStepFunctionExecution` must refuse an ARN naming another state machine, must
still serve this deployment's own, and must refuse a config-scoped caller an
out-of-scope execution.

Those three checks need a real execution ARN, which the harness resolves via
`listDocuments` → `getDocument` (the list projection does not include
`WorkflowExecutionArn`). **Process a document on the stack first**, or all three
record `SKIP` — a green run that silently skipped them proves nothing about that
operation.

## Security report

No extra step. `.claude/skills/curate-security-results.md` curates the RBAC
dynamic snapshot from `scratch/api-test-results/<stack>-<ts>/report.json`, so the
`SEC-2.1-CALLER-SUPPLIED-REF` rows appear in `rbac-dynamic.md` automatically. The
live checks above are deliberately **not** part of that snapshot: they run against
throwaway resources rather than the deployment being reported on.
