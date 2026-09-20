# API RBAC Test — GenAI IDP Accelerator

Use this skill to verify that **every** UI API operation is correctly protected
by (a) its required Cognito group(s) and (b) config-version scope, using two
complementary layers that share one source of truth:

1. **Static scan** (`make api-test-static`) — no AWS, CI-safe. Cross-checks the
   op universe, the schema directives, and the expectations file for drift and
   missing server-side checks.
2. **Dynamic tests** (`make api-test STACK_NAME=<stack>`) — spins up temporary
   Cognito users (one per group + a config-version-scoped Author + a second
   independent user for IDOR), calls every op as every role + unauthenticated +
   with malformed tokens against a *deployed* stack, PLUS the mandatory
   security-focused suites below, and writes an auditable report.

> The dynamic harness has already caught a real, shipping vulnerability
> (config-version scope silently failing open because a resolver was missing a
> `dynamodb:Query` IAM grant). Treat a hard fail as real until proven otherwise.

> ⚠️ **"Every operation" is true of the group check, not of the scope check.** The
> dynamic scope suite makes exactly **two** calls with the scoped token, against
> `getConfigVersion` and `getConfigVersions`. No other scope-enforcing operation is
> exercised live. Ops marked `skip_allowed:` in the expectations file are called
> only in their *denied* role, because an allowed-role call would start real work —
> `sendChatDocumentMessage` starts a chat turn — so their scope enforcement is not
> reached here and is covered by that component's own unit suite instead. The
> static scan's **S4** is a grep: it asserts the `enforced_in` file mentions
> `allowedConfigVersions`, which a file containing a broken lookup also does.
> Neither layer would have caught issue #970, where the lookup named a DynamoDB
> index no template declares.

### Mandatory security-focused test cases (AppSec checklist)

`make api-test` covers the AppSec "Minimum Mandatory Security Focused Test Cases
for APIs" checklist. Mapping (suite → checklist item), implemented in
`scripts/api_security_cases.py`:

| # | Checklist item | Where |
|---|----------------|-------|
| 1 | Unauthenticated access denied | `run_group_matrix` (unauth cell) + `run_token_negatives` |
| 2 / 2.2 | Authorization matrix, negative + positive; role X not authorized for API Y | `run_group_matrix` (every op × every role) |
| 2.1 | IDOR — User A's data unreachable by User B | `run_idor_suite` (chat session ownership) |
| 2.1 | Caller-supplied resource reference bounded to the deployment and the caller's config scope | `run_caller_supplied_ref_suite` — `getStepFunctionExecution` must refuse an ARN naming another state machine, still serve this deployment's own, and refuse a config-scoped caller an out-of-scope execution. **Needs a processed document**, else all three SKIP (see below) |
| 2.3 | Tokens rejected after expiry | `run_token_lifecycle_suite` (+ token negatives); real-expiry wait via `IDP_SECTEST_WAIT_EXPIRY=<seconds>` |
| 2.4 | Tokens revoked after logout | `run_token_lifecycle_suite` — global sign-out then re-test; **stateless-JWT reuse is a documented gap** (`GAP-SEC-LOGOUT`, WARN — see AUTH.T10) |
| 2.5 | Deleted resources no longer accessible | `run_deleted_resource_suite` (config version create→delete→read-gone) |
| 3 | Input validation (invalid input rejected) | `run_input_validation_suite` — **tolerant** by default (4xx or 5xx ok); `IDP_SECTEST_STRICT_INPUT=true` requires a clean 4xx (the behavior central schema validation introduces) |
| 4 | TLS 1.0/1.1/HTTP refused, TLS 1.2+ accepted | `run_tls_suite` (raw-socket protocol probes) |

Notes:
- The AppSec engineer may request **additional** tests per use case — this is the
  floor, not the ceiling.
- Suites that need conditions not present are recorded as **SKIP (pass)**, never
  silent omissions (e.g. expiry without `IDP_SECTEST_WAIT_EXPIRY`).
- Threat-model coverage: AUTH.T09 (IDOR), AUTH.T10 (token lifecycle), AUTH.T11
  (TLS), AUTH.T13 (IdP group-claim provenance) in
  `security/threat-modeling/feature-threats/rbac-authentication.md`.
- **Process a document on the stack before running this**, or the three
  `SEC-2.1-CALLER-SUPPLIED-REF` checks record SKIP. They need a real execution
  ARN, which the harness resolves via `listDocuments` -> `getDocument` — the list
  projection does not carry `WorkflowExecutionArn`. `apply_dynamic_args` also
  substitutes that ARN into `getStepFunctionExecution`'s matrix args, because the
  placeholder ARN in the expectations file is (correctly) refused now, and for an
  ANY-auth op the matrix reads any refusal as a failure.
- The Cognito pre-token IdP group-mapping trigger is NOT covered here — it is not
  an API operation. See `.claude/skills/live-auth-checks.md`.

## The architecture you are testing (read this first)

- **Single route:** the UI calls `POST /op/{field}` on an API Gateway REST API
  (logical id `HttpApiDispatcher`). The Cognito authorizer only
  **authenticates** (401 for missing/bad token); it does **no** group checks.
- **The dispatcher denies by default** (`http_api_dispatcher/authz.py`): before
  routing, it checks the caller's groups (from the verified JWT claim, never the
  body) against `api_rbac_manifest.json`, and **a field with no entry there is
  403** — unmapped means denied. The manifest is generated from
  `scripts/api_rbac_expectations.yaml` by
  `scripts/sdlc/generate_api_rbac_manifest.py`; it fails closed if unreadable.
  So an operation whose groups were never declared is closed, not open.
- **Per-resolver RBAC:** each resolver Lambda *also* reads
  `identity.claims['cognito:groups']` and raises `PermissionError` →
  the dispatcher maps that to **HTTP 403** with `errorType: "Unauthorized"`. This
  layer stays: the dispatcher enforces the field-level floor, the resolver
  enforces per-object scope (config version, test set, ownership) on top of it.
  When triaging a 403, check which layer produced it — the dispatcher logs
  `Denied <field>: ...` in `authz.py`, the resolver logs its own message.
- **Config-version scope denials are IN-BAND:** the configuration & sync
  resolvers return `{success:false, error:{type:"Unauthorized"}}` with **HTTP
  200** (NOT a 403). The harness treats an in-band `Unauthorized` as a denial.
- **5 groups** (precedence): Admin(0) > Author(1) > Reviewer(2) > Annotator(3) >
  Viewer(4), defined in `template.yaml`. That template is where the group
  vocabulary lives — `generate_api_rbac_manifest.py` reads it to resolve
  `ANY_GROUP` and to reject a group name no `AWS::Cognito::UserPoolGroup` creates.
  ⚠️ `make api-test`'s live matrix drives four of them (`ROLES` in
  `scripts/test_api_rbac.py`); it creates **no groupless user**, so
  "denied to a caller in no group" is asserted offline only, in
  `test_http_api_dispatcher_authz.py`.
- **`@aws_auth(cognito_groups)` is SILENTLY IGNORED** on this multi-auth API
  (it also allows AWS_IAM). Only `@aws_cognito_user_pools(...)` directives and
  server-side checks are real. Server-side enforcement is the source of truth;
  the schema directive is defense-in-depth.
- **IAM_ONLY ops** (`updateAgentJobStatus`, `updateDiscoveryJobStatus`) must
  reject every Cognito caller.

## The REST route is NOT the only entry path (Function URLs — S6..S9)

Chat streaming is served by a Lambda **Function URL** (`AuthType=AWS_IAM`,
`InvokeMode=RESPONSE_STREAM`; `ChatStreamProcessorUrl` in `template.yaml`) whose
FastAPI app (`src/lambda/chat_stream_processor/app.py`) drives the chat
processors **directly** — no dispatcher, no resolver. Consequences:

- An op reachable both ways must enforce its group check **in the component that
  does the work** (e.g. `src/lambda/agent_chat_processor/index.py`), not only in
  the resolver in front of it. A resolver-only check is enforced on one path.
- That transport **authenticates but carries no `cognito:groups`**:
  `requestContext.authorizer.iam.cognitoIdentity` is documented as unused by
  Function URLs, and the assumed-role session name under the Identity Pool
  enhanced flow is a pool-wide constant. So there is nothing verified to gate on
  there; the residual difference is **GAP-07** / AUTH.T14.
- A request-body `callerSub` is a **fallback only** — the transport-verified
  principal wins, and a body value that *contradicts* it is refused (403), not
  silently preferred. Both routes go through one helper so they cannot drift.
  It is nonetheless the **effective** identity on this transport, because the
  verified value is that pool-wide constant, so it must not be used as an
  authorization key. Both processors read `identity` for that instead, and this
  route can only report it as `None`.
- **`allowedConfigVersions` is therefore not enforced on `POST /chat/document`.**
  The processor resolves the caller from verified claims and denies a turn whose
  scope it cannot evaluate; with no claims to resolve from, it stands the check
  down and logs that it did so once per turn rather than appearing to have
  consulted the UsersTable. The REST path through the dispatcher does enforce it.
  Both halves of GAP-07 close when this endpoint verifies a Cognito ID token —
  and the claims it then returns must include `email`, which is the only
  identifier that joins a Cognito principal to a UsersTable row.

Declare every Function URL and route in the **`function_url_endpoints:`** section
of `scripts/api_rbac_expectations.yaml`. The scanner's Function-URL checks:

| Check | Fails when |
|-------|-----------|
| **S6** | a `AWS::Lambda::Url` in `template.yaml`, or a route in its handler, is not declared (or a declared route no longer exists) |
| **S7** | a route reads a client-supplied identity **before** the transport-verified one |
| **S8** | no function in the handler package refuses a *contradicting* client identity (names the verified value, compares `!=`, and rejects) |
| **S9** | the handler named by `enforced_in` has no group check for the route's groups, or its group list disagrees with the `equivalent_op`'s |

⚠️ `known_gap:` downgrades a finding to WARN; **`residual_gap:` does not** — it
records the gap in the register for auditability while leaving the checks armed.
Use `residual_gap` when part of a route's authorization is genuinely impossible
on the transport but the rest must still be enforced. `scripts/sdlc/tests/test_scan_api_rbac_function_urls.py`
pins S7/S8 against snippets of both shapes, so the rules cannot go inert once the
live repo only exercises the passing side.

## Three sources of truth that MUST NOT drift

| Source | Where |
|--------|-------|
| Op universe | `FIELD_FUNCTION_MAP` (SSM `/<stack>/http-api/field-function-map`) ∪ `ddb_direct._HANDLED` ∪ `FIELD_ALIASES` in the dispatcher |
| Schema groups | `@aws_cognito_user_pools(cognito_groups:[...])` in `nested/api-resolvers/src/api/schema.graphql` |
| Expectations | `scripts/api_rbac_expectations.yaml` ← **edit this when you add an op** |
| Runtime manifest | `http_api_dispatcher/api_rbac_manifest.json` — **generated** from the expectations file; never hand-edit |

The static scan **fails** if these diverge. When you add an API operation you
MUST add an entry to `scripts/api_rbac_expectations.yaml` (and the schema), then
regenerate the manifest.

## Files

- `scripts/api_rbac_expectations.yaml` — single source of truth (118 ops + gap
  register). Entry schema is documented at the top of the file.
- `scripts/sdlc/scan_api_rbac.py` — static scanner (`--strict` fails on known
  gaps, use to confirm a gap was fixed; `--json PATH` for machine output).
- `scripts/sdlc/generate_api_rbac_manifest.py` — writes the dispatcher's
  required-groups manifest from the expectations file (`--check` = drift guard,
  run by `make api-test-static`).
- `nested/api-resolvers/src/lambda/http_api_dispatcher/authz.py` — the
  default-deny enforcement point.
- `scripts/test_api_rbac.py` — dynamic harness.
- `lib/idp_common_pkg/tests/unit/test_http_api_dispatcher_authz.py` — manifest
  parity (enumerated from `FIELD_ALIASES`, `ddb_direct._HANDLED` and the
  template's field→function map) plus the deny paths.

## Environment (gotchas)

```bash
# ALL AWS calls target the deployment account — use AWS_PROFILE=default,
# NOT the sandbox's ambient creds (which may point elsewhere). Confirm first:
AWS_PROFILE=default aws sts get-caller-identity

# The harness prefers AWS CLI v2 at /usr/local/bin/aws (the conda `aws` on PATH
# is v1 and lacks some flags). It handles this internally via AWS_BIN.
```

## Workflow

```bash
# 1. Static scan — run on every change to an API op / schema / expectations.
make api-test-static                 # exit 0 with WARNs for known gaps
make api-test-static STRICT=1        # exit 1 on any known gap (verify a fix)

# 2. Dynamic tests against a deployed stack (needs deploy-account creds).
AWS_PROFILE=default make api-test STACK_NAME=IDP1 REGION=us-west-2
#   -> writes ./api-test-results/<stack>-<ts>/{report.md,report.json,meta.json}
#   REPORT_DIR=<dir>   override output location
#   NO_TEARDOWN=1      keep the temp Cognito users (debugging; rerun teardown
#                      later with:  python3 scripts/test_api_rbac.py \
#                        --stack-name <s> --region <r> --teardown-only)
```

The harness **always tears down** its temp users and restores the app client's
original auth flows on exit (even NO_TEARDOWN only skips the user delete).
Test users get a **random per-run password** (printed when NO_TEARDOWN or
--setup-only keeps them alive). Exit code is non-zero only on **hard fails**
(a real leak) — known gaps are WARNs.

## Reading the report

- `meta.json` — stack, account, git_sha, api_base, per-run totals, request IDs.
- `report.md` — the group matrix (op × role), scope suite, token negatives.
- A finding is a **hard fail** only if it has no `known_gap`; documented gaps
  (GAP-01..) surface as WARN so they stay visible without failing the gate.

## When a hard fail appears — triage order

1. **In-band denial not recognized?** If the op denies via `{success:false,
   error:{type:Unauthorized}}` (200), confirm `classify()`/`_denied()` treat
   in-band `Unauthorized` as denied. (Config & sync resolvers use this.)
2. **Expectation wrong?** Read the `enforced_in` file and confirm the actual
   server-side group set. The code is the source of truth; fix the YAML.
3. **Test input short-circuits?** For mutations, auth is checked BEFORE the
   bogus id is used, so an *allowed* role legitimately gets 400 (not-found) —
   that's a pass, not a leak. A *disallowed* role must still get Unauthorized.
4. **Allowed role denied on a NEW op?** Look for `Denied <field>: no
   required-groups entry` in the dispatcher log — the operation's groups were
   never declared, so the floor denies everyone. Declare them and regenerate the
   manifest (see the checklist below); do not widen the manifest to make the
   symptom go away.
5. **Real leak / fail-open?** If a scoped/lower-privilege caller is ALLOWED, check
   the actual group gate. On every REST operation the scope **lookup** fails closed —
   `resolve_allowed_config_versions` in `idp_common.config_scope` raises
   `ScopeLookupError` for a missing UsersTable, an absent `email` claim or any
   failed `dynamodb:Query`, and each REST consumer turns that into a refusal — so a
   missing IAM grant presents as a **denial**, not as an unrestricted caller. In
   the resolver's CloudWatch logs, look for "config-version scope lookup failed on
   EmailIndex" at ERROR. ⚠️ The **chat-streaming** transport is the exception and is
   not a REST operation: see GAP-07 and AUTH.T07's residuals. An *empty page* is
   still deliberately unrestricted
   (scoping is opt-in), so a caller with no UsersTable row legitimately sees
   everything: check the row exists before reading a broad result as a leak.

## Adding a new API operation — checklist

1. Add the resolver's server-side group/scope check.
2. Add the `@aws_cognito_user_pools` directive in `schema.graphql`.
3. **Declare the operation's required groups** in
   `scripts/api_rbac_expectations.yaml` (mirror a similar op) — this is now the
   source the dispatcher enforces, not only what the tests expect.
4. **Regenerate the dispatcher manifest:**
   `python3 scripts/sdlc/generate_api_rbac_manifest.py`, and commit the changed
   `nested/api-resolvers/src/lambda/http_api_dispatcher/api_rbac_manifest.json`.
5. `make api-test-static` must be clean (it runs the manifest drift check too),
   then run `make api-test` live.
6. If the op is **also** reachable off the REST route (a Function URL route, a
   direct `lambda:InvokeFunction` path), put the group check in the component
   that does the work and declare the route under `function_url_endpoints:` —
   otherwise S6 fails and the check covers only one path. The dispatcher's
   default deny does not help there: it gates the REST route only.

> ⚠️ Steps 3–4 are not optional bookkeeping. The dispatcher **denies by default**,
> so an operation with no manifest entry returns **403 for everyone** — including
> Admin — no matter what its resolver allows. If a brand-new operation 403s, the
> fix is to declare its groups and regenerate (steps 3–4). Do **not** "fix" it by
> widening an existing entry, adding a bypass, or making an unmapped field fall
> through: unmapped-means-denied is the property this layer exists to provide, and
> the reason a forgotten check on a **group-scoped** operation is no longer an open
> endpoint. Declaring an operation `ANY` to make a 403 go away is exactly the
> widening this warns against: `ANY` means the dispatcher checks authentication
> only, so a forgotten resolver check on an `ANY` operation is still reachable by
> any authenticated caller — including one in no group, which self-signup produces.
> 15 of the 118 operations are `ANY`, each with a note in the expectations file
> saying why that is the intended answer for it; they are enumeration, platform,
> profile and feature-catalog reads.

### The four policies — pick the weakest one that is still correct

| Declare | Means | Use for |
|---|---|---|
| `[Admin, Author, ...]` | one of those groups | anything only a subset of roles should do |
| `ANY_GROUP` | **any** group `template.yaml` creates; a caller in no group is refused | operations every onboarded role legitimately needs, where "onboarded at all" is the real requirement — document content, and mutations |
| `ANY` | authentication only | the caller's own profile, public metadata, enumeration that discloses no content |
| `IAM_ONLY` | no Cognito caller at all | backend-written status updates |

⚠️ **Do not spell `ANY_GROUP` out as the five group names.** The sentinel is
resolved against the `AWS::Cognito::UserPoolGroup` resources in `template.yaml` by
`generate_api_rbac_manifest.py` on every build, so a sixth group is covered
automatically; five names written per operation would silently stop covering it —
the "fix applied to the instance and not the class" defect this repo keeps hitting.
The `schema.graphql` directive *does* have to name them all, because GraphQL cannot
express "any group"; check **S2** fails until it does, which is the intended way to
be told a group was added. Check **S0** rejects an unrecognised sentinel outright,
because the *scanner alone* would not notice one — S2 would compare a set of its
*characters* and S3 would accept a resolver with no check at all. (`make
api-test-static` would still fail, one command later, on the generator's `--check`;
S0 is what makes the scanner sound on its own and what covers a Function-URL route
policy, which the generator never reads.) `authz.py` never sees `ANY_GROUP`: it is
expanded before the manifest is written, and an unexpanded one there means a broken
build and is treated as one (deny-all).

⚠️ **A group floor is not the whole control.** It gates the REST API. The Identity
Pool attaches one `authenticated` role with no role mappings, so every signed-in
user — group or no group — holds `s3:GetObject`/`ListBucket` on the document
buckets, and the UI reads them directly by default. Do not describe an `ANY_GROUP`
operation as making document content unreachable; it makes that *operation*
unreachable. See the residuals in `docs/rbac.md` and AUTH.T03.
