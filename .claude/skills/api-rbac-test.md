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
- **Per-resolver RBAC:** each resolver Lambda reads
  `identity.claims['cognito:groups']` and raises `PermissionError` →
  the dispatcher maps that to **HTTP 403** with `errorType: "Unauthorized"`.
- **Config-version scope denials are IN-BAND:** the configuration & sync
  resolvers return `{success:false, error:{type:"Unauthorized"}}` with **HTTP
  200** (NOT a 403). The harness treats an in-band `Unauthorized` as a denial.
- **4 groups** (precedence): Admin(0) > Author(1) > Reviewer(2) > Viewer(3),
  defined in `template.yaml`.
- **`@aws_auth(cognito_groups)` is SILENTLY IGNORED** on this multi-auth API
  (it also allows AWS_IAM). Only `@aws_cognito_user_pools(...)` directives and
  server-side checks are real. Server-side enforcement is the source of truth;
  the schema directive is defense-in-depth.
- **IAM_ONLY ops** (`updateAgentJobStatus`, `updateDiscoveryJobStatus`) must
  reject every Cognito caller.

## Three sources of truth that MUST NOT drift

| Source | Where |
|--------|-------|
| Op universe | `FIELD_FUNCTION_MAP` (SSM `/<stack>/http-api/field-function-map`) ∪ `ddb_direct._HANDLED` ∪ `FIELD_ALIASES` in the dispatcher |
| Schema groups | `@aws_cognito_user_pools(cognito_groups:[...])` in `nested/api-resolvers/src/api/schema.graphql` |
| Expectations | `scripts/api_rbac_expectations.yaml` ← **edit this when you add an op** |

The static scan **fails** if these diverge. When you add an API operation you
MUST add an entry to `scripts/api_rbac_expectations.yaml` (and the schema).

## Files

- `scripts/api_rbac_expectations.yaml` — single source of truth (96 ops + gap
  register). Entry schema is documented at the top of the file.
- `scripts/sdlc/scan_api_rbac.py` — static scanner (`--strict` fails on known
  gaps, use to confirm a gap was fixed; `--json PATH` for machine output).
- `scripts/test_api_rbac.py` — dynamic harness.

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
4. **Real leak / fail-open?** If a scoped/lower-privilege caller is ALLOWED,
   check the resolver's IAM grants (a caught `AccessDeniedException` on the
   UsersTable scope query fails OPEN to unrestricted) and the actual group gate.
   Confirm via the resolver's CloudWatch logs (look for
   "Config scope for ...: unrestricted" right after an AccessDenied WARNING).

## Adding a new API operation — checklist

1. Add the resolver's server-side group/scope check.
2. Add the `@aws_cognito_user_pools` directive in `schema.graphql`.
3. Add an entry to `scripts/api_rbac_expectations.yaml` (mirror a similar op).
4. `make api-test-static` must be clean, then run `make api-test` live.
