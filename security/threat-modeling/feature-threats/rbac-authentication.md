# RBAC & Authentication — Threat Analysis

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.4 |
| **Last Updated** | 2026-09-20 |
| **Applies to release** | v0.6.9 |
| **Feature** | Role-Based Access Control & Authentication |
| **Classification** | Internal |

## 1. Feature Overview

The IDP Accelerator implements a **5-group** RBAC system using Amazon Cognito
User Pools (`template.yaml`, `AWS::Cognito::UserPoolGroup` x5):

| Role | Precedence | Capabilities |
|------|-----------|--------------|
| **Admin** | 0 | Full system access: configuration, processing, review, agent access, user management |
| **Author** | 1 | Create/edit configurations, upload documents, run processing, use agents |
| **Reviewer** | 2 | Review processed documents, HITL review tasks, view results |
| **Annotator** | 3 | Ground-truth annotation, restricted to the test sets named in the user's `allowedTestSets` |
| **Viewer** | 4 | Read-only access to processing results and dashboards |

> **`Precedence` is not a hierarchy.** It is only Cognito's tiebreaker when
> resolving which IAM role an identity assumes. Every authorization decision in
> this system is a plain set intersection against an explicit per-operation
> allow-list, so `Admin` is not implicitly a superset of `Author` — it is named
> in each allow-list where it applies. Membership is additive: a user in two
> groups holds the union of both. Reading `Precedence` as a privilege ladder is
> the most likely way to mis-review a new operation's allow-list.

> **`Annotator` cannot be granted by federation.** The external-IdP group mapping
> recognises only Admin, Author, Reviewer and Viewer (there is no
> `ExternalIdPAnnotatorGroupName` parameter), while 12 server-side allow-lists
> and the UI's `APP_GROUPS` do recognise `Annotator`. In a federated deployment
> the group must be assigned directly in Cognito, which places it outside the
> IdP's own joiner/leaver process — an access-review gap rather than a bypass.

Authorization is enforced at multiple layers:
- **Cognito Groups**: Users assigned to groups corresponding to roles
- **Resolver Lambdas**: Per-operation authorization checking Cognito group
  membership (and, for config-scoped ops, the caller's `allowedConfigVersions`)
- **Lambda Functions**: Role-aware business logic
- **UI Components**: Feature visibility based on user role — an **affordance
  only**; every check is repeated server-side

> **API architecture note (v3.0, re-verified against v0.6.9).** The UI no longer
> talks to AWS AppSync. It now calls a single API Gateway **REST** route,
> `POST /op/{field}`, fronted by a Cognito User Pools authorizer and, optionally,
> WAF and a PRIVATE (VPC-only) endpoint. The authorizer **only authenticates**
> the JWT (401 for a missing/invalid token) — it performs **no group
> evaluation**. Almost all group/scope authorization is enforced inside the
> resolver Lambdas: an HTTP dispatcher (`http_api_dispatcher`) normalizes the
> request and invokes the same resolver Lambda that AppSync used to invoke; the
> resolver raises `PermissionError`, which the dispatcher maps to **HTTP 403**
> with `errorType: "Unauthorized"`. Config-version **scope** denials instead
> return an *in-band* `{success:false, error:{type:"Unauthorized"}}` body with
> **HTTP 200**. This shifts the authorization trust boundary entirely to the
> resolver Lambdas, which makes automated per-operation authorization testing
> (see §5) a primary control rather than a nice-to-have.
>
> **Shape of that surface at v0.6.9:** **118** operations reach the single route —
> 40 through `FIELD_FUNCTION_MAP` (an SSM-published field→Lambda map), **68**
> through `FIELD_ALIASES` onto shared resolvers, and **11** served in-process by
> `ddb_direct` (the former AppSync VTL resolvers). Those add to 119 rather than
> 118 because `getCircuitBreakerStatus` is in both the function map and
> `ddb_direct`; the distinct union is 118, matching
> `scripts/api_rbac_expectations.yaml` entry for entry. `ddb_direct` is the one place
> the dispatcher itself enforces groups, via its own `_REQUIRED_GROUPS` table.
> Across all 118: 21 require `Admin`; 40 `Admin`+`Author`; 15
> `Admin`+`Author`+`Viewer`; 7 `Admin`+`Author`+`Annotator`; 4
> `Admin`+`Reviewer`+`Annotator`; 2 `Admin`+`Reviewer`; 1
> `Admin`+`Author`+`Annotator`+`Viewer` (every group except `Reviewer`);
> 2 are IAM-only (rejected for every Cognito caller); and **26 require
> only authentication**. 13 additionally enforce config-version scope, 4 filter
> list rows by it, and 8 enforce per-object ownership. `scripts/api_rbac_expectations.yaml`
> is the manifest of record for all of this, and records exactly one accepted
> open gap (**GAP-02**, `queryKnowledgeBase` carries no group check).
>
> **Naming trap.** The CloudFormation logical id is `HttpApi` and much of the
> surrounding code says "HTTP API" / "JWT authorizer", but the deployed resource
> is `AWS::ApiGateway::RestApi`. The REST authorizer places claims at
> `requestContext.authorizer.claims` (not `.authorizer.jwt.claims`) and flattens
> `cognito:groups` into a **comma-joined string**;
> `idp_common.api_adapter._coerce_groups` restores the list. That coercion is
> load-bearing for every group check in the system: getting it wrong either
> locks every user out or admits everyone.

## 2. Architecture

```mermaid
flowchart TD
    User[User] -->|Credentials| Cognito[Cognito User Pool]
    Cognito -->|JWT with Groups| Browser[Browser / SDK]

    Browser -->|JWT: POST /op/{field}| APIGW[API Gateway REST + WAF]
    APIGW -->|Cognito authorizer: AUTHENTICATE only 401| APIGW
    APIGW -->|normalized event| Dispatcher[HTTP API Dispatcher Lambda]

    Dispatcher -->|invoke resolver| Resolver[Resolver Lambdas]
    Dispatcher -->|VTL-equivalent| DDB[ddb_direct handlers]

    Resolver -->|check cognito:groups| GroupCheck{Group allowed?}
    GroupCheck -->|no| Deny403[PermissionError → 403 Unauthorized]
    GroupCheck -->|yes| ScopeCheck{Config-version in\nallowedConfigVersions?}
    ScopeCheck -->|no| DenyInBand[in-band Unauthorized 200]
    ScopeCheck -->|yes| BusinessLogic[Role-Aware Logic]
```

> **Second entry path (v3.2).** The REST route above is not the only way into the
> backend. Chat streaming is served by a Lambda **Function URL**
> (`AuthType=AWS_IAM`, `InvokeMode=RESPONSE_STREAM`) that the browser POSTs to
> with SigV4-signed Cognito Identity Pool credentials; its handler drives the two
> chat processors directly, bypassing the dispatcher and the resolvers. That
> transport authenticates but forwards **no** Cognito group claim, so an
> operation reachable both ways must enforce its group check in the component
> that does the work rather than only in the resolver in front of it. See
> AUTH.T14, and the `function_url_endpoints` section of
> `scripts/api_rbac_expectations.yaml` (which the static scan reads).

## 3. Threat Analysis

### AUTH.T01: Privilege Escalation via Group Manipulation

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T01 |
| **Category** | STRIDE: Elevation of Privilege |
| **Description** | If Cognito user group assignments are not properly protected, a user could add themselves to higher-privilege groups (e.g., Viewer → Admin) |
| **Attack Vector** | Direct Cognito API calls to modify group membership using stolen admin credentials, or exploiting misconfigured Cognito permissions |
| **Impact** | Unauthorized access to configuration, processing, and admin functions |
| **Likelihood** | Low |
| **Severity** | Critical |
| **Affected Components** | Cognito User Pool, IAM policies |
| **Mitigations** | IAM policies restricting Cognito admin operations, no self-service group management, Cognito user pool advanced security features, CloudTrail logging of Cognito API calls |

### AUTH.T02: JWT Token Theft/Replay

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T02 |
| **Category** | STRIDE: Spoofing |
| **Description** | JWT tokens stored in browser (localStorage/sessionStorage) or SDK client could be stolen via XSS, malicious browser extensions, or network interception, then replayed for unauthorized access |
| **Attack Vector** | XSS attack on web UI extracts JWT from storage; or man-in-the-middle (unlikely with TLS) captures token |
| **Impact** | Attacker gains authenticated access with victim's role permissions |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | Web UI, SDK/CLI, API Gateway REST API |
| **Mitigations** | Short-lived access tokens (1 hour default), secure token storage practices, Content Security Policy headers, XSS prevention in React app, HTTPS-only |

### AUTH.T03: Insufficient Authorization Granularity

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T03 |
| **Category** | STRIDE: Elevation of Privilege |
| **Description** | The API Gateway authorizer only authenticates, so when authorization lived *solely* in the resolver Lambdas any resolver missing a server-side group check let a lower-privilege authenticated user perform a restricted operation by calling `POST /op/{field}` directly — and a newly added operation was open unless someone remembered to add a check. |
| **Attack Vector** | Call the REST API `POST /op/{field}` directly with a valid low-privilege JWT, targeting operations whose resolver omits (or misconfigures) the `cognito:groups` check — bypassing all UI-level restrictions. |
| **Impact** | Unauthorized configuration changes, document access, or processing operations |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | Resolver Lambdas, `http_api_dispatcher`, `ddb_direct` handlers |
| **Mitigations** | **The dispatcher denies by default.** `http_api_dispatcher/authz.py` checks the caller's groups — read from the verified JWT claim, never the request body — against `api_rbac_manifest.json` before routing, and a field with **no entry** is refused with 403: an operation whose required groups were never declared is closed rather than open, so a forgotten resolver check on a **group-scoped** operation is no longer an open endpoint. ⚠️ **This does not cover every operation.** 26 of the 118 declared operations are declared `ANY` — the dispatcher enforces authentication but not group membership for those — so a forgotten resolver check on one of the 26 is still reachable by any authenticated caller, including a caller in no group at all where self-signup is enabled (`AllowedSignUpEmailDomain`, see AUTH.T04). `getFileContents`, for instance, protects itself with a bucket allowlist rather than a group check. Which of the 26 should be narrowed is tracked as [issue #979](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/979). **An `identity` carried on the event is no longer authoritative** ([#978](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/978), closed): `idp_common.api_adapter.normalize_event` used to pass an event carrying its own `arguments` + `identity` through unchanged, so a principal holding `lambda:InvokeFunction` on the dispatcher directly could state its own `cognito:groups` and have this check made against its own claim — a privilege-escalation amplifier rather than an open door, since API Gateway builds the event itself and a request body cannot add top-level keys to it. The adapter now builds the identity from the authorizer's verified claims, refuses an asserted identity that contradicts them, and refuses one presented with no verified claims at all; the one shape still passed through is an explicitly null `identity`, which is this repository's marker for a service-to-service invocation gated by IAM on the function ARN and asserts no groups. A guard drives the dispatcher end to end with an operation and required groups read from the manifest at test time, and a second one discovers every consumer of the adapter by parsing the tree and fails if one lets the refusal escape as a 5xx instead of a 403. The manifest is generated from `scripts/api_rbac_expectations.yaml` (`scripts/sdlc/generate_api_rbac_manifest.py`), the same file the static scan and the live harness already assert against, so there is no second list to keep in step; it fails closed if unreadable. Resolver-level authorization for every operation remains in place on top of it — the dispatcher enforces the field-level floor, the resolver enforces per-object scope. Plus **automated per-operation authorization testing** — a static scan (including a manifest drift guard) and a live multi-role harness (`make api-test`, see §5) that fail on any missing/incorrect check and track known gaps; security review of new operations. **Not a control:** the `@aws_cognito_user_pools` directives in `schema.graphql` are **not** defense-in-depth here — nothing enforces them at runtime now that AppSync is gone, so they carry no enforcement weight at all. They are retained as the input for the dispatcher's shape validation and as documentation of intent, and they are read by the static scan as one of the things it checks *for drift against the code*. Reading them as a second layer is exactly the mistake AUTH.T08 describes. |

### AUTH.T04: Cognito User Pool Misconfiguration

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T04 |
| **Category** | STRIDE: Spoofing, Information Disclosure |
| **Description** | Misconfigured Cognito user pool settings (e.g., self-signup enabled, weak password policies, unverified email) could allow unauthorized account creation or account takeover |
| **Attack Vector** | Self-register accounts if self-signup is enabled, or exploit weak password requirements |
| **Impact** | Unauthorized system access, even at Viewer level provides access to document processing results |
| **Likelihood** | Low |
| **Severity** | High |
| **Affected Components** | Cognito User Pool |
| **Mitigations** | Self-signup is disabled by default (`AllowAdminCreateUserOnly: true`, admin-created accounts only). Setting `AllowedSignUpEmailDomain` deliberately enables it, restricted to the named email domains and enforced by the `CognitoUserPoolEmailDomainVerifyFunction` PreSignUp/PreAuthentication trigger — anyone holding an address at those domains can then create their own account, so treat the domain list as the trust boundary (it also widens who can reach AUTH.T13). Strong password policy enforcement, MFA option, email verification required, Cognito advanced security features (compromised credential detection). |

### AUTH.T05: Refresh Token Abuse

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T05 |
| **Category** | STRIDE: Spoofing |
| **Description** | Cognito refresh tokens have longer lifetime than access tokens and can be used to obtain new access tokens. Stolen refresh tokens provide persistent access |
| **Attack Vector** | Steal refresh token from browser storage or SDK client, use to continuously obtain fresh access tokens |
| **Impact** | Persistent unauthorized access beyond access token lifetime |
| **Likelihood** | Low |
| **Severity** | High |
| **Affected Components** | Cognito User Pool, Web UI, SDK/CLI |
| **Mitigations** | Configurable refresh token expiration, token revocation capabilities, Cognito advanced security (anomaly detection), secure token storage, session monitoring |

### AUTH.T06: Cross-Tenant Data Access (Multi-Stack)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T06 |
| **Category** | STRIDE: Information Disclosure |
| **Description** | Each deployment is single-tenant, but organizations may deploy multiple stacks. If users have access to multiple stacks' Cognito pools, they could access data across environments |
| **Attack Vector** | User with credentials for multiple stacks accesses data from an environment they shouldn't have access to |
| **Impact** | Cross-environment data access |
| **Likelihood** | Low |
| **Severity** | Medium |
| **Affected Components** | Cognito User Pools (per stack), S3 buckets, DynamoDB tables |
| **Mitigations** | Separate Cognito User Pools per stack (default), IAM resource policies scoped to individual stacks, organizational controls on user provisioning |

### AUTH.T07: Config-Version Scope Bypass (Fail-Open Scope Lookup)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T07 |
| **Category** | STRIDE: Elevation of Privilege, Information Disclosure |
| **Description** | Non-admin users can be restricted to specific named configuration versions via an `allowedConfigVersions` list in the UsersTable. The scope is resolved by querying the UsersTable `EmailIndex` GSI, and the lookup used to **fail open** — treat the caller as *unrestricted* — on two distinct paths. (a) The **lookup key** was derived from a fallback chain (`email` claim, else `identity.username`, else `cognito:username`, else `sub`). Email is the only identifier that joins a Cognito principal to a row on that table: the row's key is a `uuid4` minted by user management, and no `sub` attribute is stored. So a substituted identifier does not find the row by another route — it matches **no** row, and an empty page is indistinguishable from "this user has no restriction". Any verified claims set lacking an `email` claim therefore resolved to *unrestricted*, with **no AWS fault required**. (b) A **failed query** (a missing `dynamodb:Query` grant, a wrong index name, a throttle) was caught and read the same way, so scope enforcement switched itself off precisely on the wiring drift it exists to survive. |
| **Attack Vector** | A config-version-scoped caller invokes a scoped operation (`getConfigVersion`, `getConfigVersions`, the five revision operations, `reprocessDocument`, `syncBdaIdp`, `getStepFunctionExecution`, both document lists, `getDocumentCount`, document chat) for a version outside their allowed set, under verified claims from which the lookup key cannot be derived, or while the scope query is failing. The lookup returns "no restriction" and the request is allowed. A second route needed no unresolvable caller at all: `reprocessDocument` gated its whole scope check on the caller supplying a `version` argument, so omitting it reprocessed any `objectKey` in the deployment — including the documents the document-list resolvers already hide from that caller. |
| **Impact** | Cross-scope disclosure of configuration (prompts, model settings, pricing) and of documents and their processing detail that a tenant/team was meant to be walled off from; and, for `reprocessDocument`/`syncBdaIdp`, mutation under a profile outside the caller's scope. |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | `configuration_resolver`, `reprocess_document_resolver`, `sync_bda_idp_resolver`, `get_stepfunction_execution_resolver`, `list_documents_gsi_resolver`, `list_documents_range_resolver`, `chat_with_document_processor`, `user_management` (own-profile lookup), the PII-anonymizer feature API, UsersTable `EmailIndex`, resolver IAM roles |
| **Mitigations** | **One fail-closed lookup, shared.** `resolve_allowed_config_versions` in `idp_common/config_scope.py` is now the single implementation: the key comes from the `email` claim and nothing else (an absent claim denies, and issues no query), and no UsersTable wired / no key / any failed `dynamodb:Query` raises `ScopeLookupError`, which every consumer turns into a refusal. An **empty page still means unrestricted**, deliberately — scoping is opt-in per user, and denying there would lock every ordinary user out. The two document-list resolvers and the two artifacts that ship without an `idp_common` layer (`user_management`, the PII-anonymizer feature API) carry the rule verbatim rather than a variant of it. **Both directions are checked, not just the argument.** `reprocessDocument` now also resolves the profile each named document was last processed under and refuses any outside the caller's scope, so the control does not stand down when the caller omits `version`; a document whose profile cannot be established (no tracking row, no `ConfigVersion`, a failed read) is refused, and a batch is refused whole rather than part-processed. **The class is gated, not just the instances:** `scripts/tests/test_scope_lookup_fail_closed.py` walks every module under `nested/api-resolvers/src/lambda`, `src/lambda` and `feature-platform` that queries the UsersTable — recognised by the **table**, not by an index name, since naming the index wrongly is itself one of the ways this failed — and fails if any derives the key from another claim, or handles a lookup failure with anything but a refusal (including a suppressing context manager, a conditional raise that falls through, and a success-shaped payload). It reported 37 rule hits over 15 distinct sites in 8 files before this change. Per-site unit suites assert the three outcomes (no `email` claim denies without querying; a DynamoDB error denies; an empty page stays unrestricted). Static check **S4** of `make api-test-static` now reads only the code an operation can *reach* — its dispatch branch bodies and their call closure — which is what exposed `getDocumentCount` declaring `scope_filtered: true` while resolving no caller at all; that operation now applies the same filters as `listDocuments`. Every scope-enforcing resolver holds `dynamodb:Query`/`GetItem` on the UsersTable and its `/index/*` (a missing grant on `ConfigurationResolverFunction` was found and fixed by the live harness on 2026-07-13); a failed lookup is logged at ERROR, and now denies rather than needing an alarm to be noticed. The **live scope suite in `make api-test`** seeds a scoped user and asserts an out-of-scope version is denied and that Admins are unaffected. **Residual:** (1) email remains the join, so a caller's address diverging from the row's (case, an external-IdP attribute remap, a mutable address) still reads as "no row" and therefore unrestricted — the durable fix is to key scope on the immutable Cognito `sub`, which needs that attribute stored on the table; (2) nothing asserts at the adapter which *kind* of Cognito token produced the claims, so which token types can reach these operations is a property of the API Gateway authorizer configuration rather than of this code, and is **unmeasured** — it has not been exercised against a live deployment; (3) the class gate checks **key provenance and failure handling only** — it has no rule about the *matcher*, so a divergent `scope_allows` restatement in an artifact that ships without the shared layer would not be caught (only the two byte-identical vendored copies are held to the letter, by a file-comparison test), and `lib/idp_common_pkg` is outside its scan roots, which leaves `idp_common/testset_scope.py` — a further `EmailIndex` consumer, for the independent `allowedTestSets` axis — unpoliced; its polarity is inverted, so the shapes are not live there; (4) the Chat-with-Document processor and its vendored copy are **discovered and suppressed** by the gate rather than checked, under an entry naming the specific rules, because a concurrent change owns those files; a test asserts that entry is still load-bearing and fails the moment they comply, so the suppression cannot outlive its reason. |

### AUTH.T08: Silently-Ignored Schema Authorization Directives

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T08 |
| **Category** | STRIDE: Elevation of Privilege |
| **Description** | The GraphQL schema still carries authorization directives from the AppSync era. On a multi-auth API the legacy `@aws_auth(cognito_groups: [...])` directive is **silently ignored**, and even `@aws_cognito_user_pools(...)` directives are no longer enforced at the gateway now that the REST dispatcher fronts the resolvers. A developer who relies on a schema directive alone — without a server-side check in the resolver — ships an unprotected operation that *looks* protected in the schema. |
| **Attack Vector** | Add/keep an operation whose only "protection" is a schema directive; a low-privilege caller invokes `POST /op/{field}` and is authorized because no resolver-side check runs. |
| **Impact** | Operations appear group-restricted but are open to any authenticated user. |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | `schema.graphql`, resolver Lambdas |
| **Mitigations** | Server-side group checks are the source of truth; schema directives are defense-in-depth only. Since the dispatcher **denies by default** (AUTH.T03), an operation protected by a directive alone is no longer open: with no entry in the generated required-groups manifest it is refused for every caller, so the failure mode is a visible 403 rather than a silent bypass. The **static scan in `make api-test-static`** flags `@aws_auth`-only / directive-vs-code drift and fails when an operation lacks a documented server-side check; new operations must add a resolver check plus an expectations entry. The feature-platform ops that formerly relied on the silently-ignored `@aws_auth` directive (tracked as GAP-06) now declare `@aws_cognito_user_pools(cognito_groups:["Admin"])` matching their resolver enforcement. |

### AUTH.T12: Missing Input-Shape Validation (Type Confusion via Lost Schema Validation)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T12 |
| **Category** | STRIDE: Tampering, Denial of Service |
| **Description** | AppSync validated every operation's input arguments against the GraphQL schema (unknown args rejected, non-null enforced, scalar types + enums checked) **before** the resolver ran. The REST dispatcher that replaced it originally passed `event["arguments"]` through unvalidated, so a caller could send an object/array where a scalar was expected, an unknown argument, or a missing required argument — reaching resolver code that indexes into the shape untyped. Depending on the resolver this caused silent acceptance or an uncaught 500, and widened the surface for type-confusion / NoSQL-style injection payloads flowing into DynamoDB expressions or LLM prompts. |
| **Attack Vector** | A caller posts `POST /op/{field}` with a malformed `arguments` object (e.g. `{"ObjectKey": {"$ne": null}}` where a `String!` is expected) that the resolver was not written to defend against. |
| **Impact** | Type-confusion / unexpected-shape handling in resolvers; denial of service via uncaught 500s; loss of the boundary that constrained input before business logic. |
| **Likelihood** | Medium |
| **Severity** | Medium |
| **Affected Components** | `http_api_dispatcher` (index.py), all resolver Lambdas, `ddb_direct` |
| **Mitigations** | **Central schema-shape validation in the dispatcher** (`validation.py` + build-time `api_validation_spec.json` generated from `schema.graphql`) rejects unknown args, missing non-null args, wrong scalar types, and bad enum values with HTTP 400 before routing — restoring AppSync's input gate for all operations at once. The validator is conservative (type-only, shallow input-objects) and fails open on its own errors so it can't 500 the API. A drift guard (`generate_api_validation_spec.py --check` + unit test) keeps the spec in sync with the schema. The **input-validation suite in `make api-test`** exercises malformed payloads per op (strict mode asserts a clean 4xx). |

### AUTH.T09: Insecure Direct Object Reference (IDOR / BOLA)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T09 |
| **Category** | STRIDE: Information Disclosure, Elevation of Privilege |
| **Description** | User-owned resources (chat sessions, agent jobs) are addressed by an id supplied in the request. If a resolver keys the record by the supplied id alone — without also constraining to the caller's own identity — one authenticated user (User B) can read or modify another user's (User A's) data by guessing/replaying the id. This is the "broken object-level authorization" class the RBAC group matrix does NOT catch (both users are the same *role*). |
| **Attack Vector** | User B calls `getChatMessages`/`deleteChatSession`/`getAgentJobStatus`/`deleteAgentJob` with User A's `sessionId`/`jobId`. |
| **Impact** | Cross-user disclosure or destruction of chat history / job data within the same deployment. |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | `get_agent_chat_messages_resolver`, `delete_agent_chat_session_resolver`, `ddb_direct` agent-job ops, ChatSessions/ChatMessages/Agent tables |
| **Mitigations** | Ownership is enforced by construction (agent-job ops derive the DynamoDB partition key from the caller identity, so a foreign id resolves under the caller's own empty partition) and by explicit `ownerSub`-vs-caller-`sub` checks in the document-chat message resolver (feature-flagged on by default). The **live IDOR suite in `make api-test`** seeds a session owned by User A and asserts User B is denied/empty for that id while the owner retains access — catching a regression that dropped the ownership check. |

### AUTH.T10: Token Lifecycle — Post-Logout Token Reuse (Stateless JWT)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T10 |
| **Category** | STRIDE: Spoofing, Elevation of Privilege |
| **Description** | Cognito ID/access tokens are stateless JWTs validated by the API Gateway authorizer against signature + `exp`. A global sign-out (`admin-user-global-sign-out`) revokes **refresh** tokens, but a still-valid **access/ID** token continues to be accepted until it expires unless the API additionally checks token revocation. So a token captured before logout remains usable for the remainder of its lifetime. |
| **Attack Vector** | An attacker who captured a valid token continues calling `POST /op/{field}` after the user logs out, until the token's `exp`. |
| **Impact** | Session does not truly end at logout; a leaked token is usable for up to its (short) TTL after sign-out. |
| **Likelihood** | Low (requires an already-captured token; TTL-bounded) |
| **Severity** | Medium |
| **Affected Components** | Cognito User Pool app client, API Gateway Cognito authorizer |
| **Mitigations** | Keep token TTL short (IDP1 uses 1h ID/access token validity) so the post-logout window is bounded; expired tokens ARE rejected (verified by the token-negative + expiry suites in `make api-test`). The **logout suite in `make api-test`** performs a global sign-out and re-tests the token, surfacing continued acceptance as a documented gap (**GAP-SEC-LOGOUT**, WARN) so the accepted-risk is visible. Full revocation would require an authorizer-side revocation check (Cognito token revocation / a deny-list) — tracked as a follow-up, not yet implemented. |

### AUTH.T11: Weak Transport Security (TLS downgrade / cleartext)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T11 |
| **Category** | STRIDE: Information Disclosure, Tampering |
| **Description** | If the API endpoint negotiates obsolete TLS (1.0/1.1) or answers over plaintext HTTP, tokens and document data in transit are exposed to downgrade/interception attacks. |
| **Attack Vector** | A man-in-the-middle forces a TLS 1.0/1.1 handshake or intercepts a cleartext request. |
| **Impact** | Disclosure/tampering of bearer tokens and document content in transit. |
| **Likelihood** | Low |
| **Severity** | Medium |
| **Affected Components** | API Gateway (execute-api) domain / CloudFront distribution TLS policy |
| **Mitigations** | API Gateway `execute-api` enforces TLS 1.2+ and does not serve on port 80; CloudFront uses a TLS 1.2+ minimum-protocol security policy. The **TLS suite in `make api-test`** actively attempts TLS 1.0/1.1 handshakes and a plaintext HTTP request against the live endpoint and asserts they are refused while TLS 1.2 succeeds. |

### AUTH.T13: Group Assignment From a User-Writable Attribute (External IdP Mapping)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T13 |
| **Category** | STRIDE: Elevation of Privilege, Spoofing |
| **Description** | With external IdP federation and `ExternalIdPGroupAttributeName` configured, Cognito maps the IdP's group claim onto the `custom:idp_groups` User Pool attribute, and the `ExternalIdPGroupMappingFunction` pre-token-generation trigger turns that attribute into Cognito group membership — including `Admin`. The attribute is `Mutable: true`, and because Cognito applies IdP attribute mapping *as the app client*, a mapped attribute must also be writable by that client. Nothing in the stored value records which principal wrote it, so a trigger that reads it without establishing provenance treats a value of any origin as the IdP's assertion. |
| **Attack Vector** | A low-privilege user of the pool (or, where `AllowedSignUpEmailDomain` permits self-registration, anyone with an address at an allowed domain) writes the group value corresponding to `ExternalIdPAdminGroupName` to their own attribute, then obtains a fresh token so the trigger reads it. |
| **Impact** | Application Admin: configuration and user management, and every configuration version. The trigger writes group membership with `AdminAddUserToGroup`, so the effect outlives the attribute value that caused it. |
| **Likelihood** | Low (requires federation *and* group mapping to be configured, and knowledge of the deployment-specific admin group string) |
| **Severity** | High |
| **Affected Components** | `ExternalIdPGroupMappingFunction` (inline handler in `template.yaml`; standalone copy `src/lambda/external_idp_group_mapping/index.py`), `UserPool` schema (`idp_groups`), `UserPoolClient` attribute permissions |
| **Mitigations** | **Provenance:** the trigger reads the Cognito-managed `identities` attribute via `AdminGetUser` — a field no client can write — and honours `custom:idp_groups` only for a user linked to the provider named by `EXTERNAL_IDP_NAME`. A native user who writes the attribute themselves gains nothing. **Freshness:** only fresh sign-in trigger sources are honoured, never `TokenGeneration_RefreshTokens`, so a value written after sign-in cannot be picked up by refreshing; at a fresh federated sign-in Cognito has just rewritten the attribute from the assertion. Both checks fail closed. **Attribute permissions:** `UserPoolClient` now declares an explicit `WriteAttributes` naming only the IdP-mapped attributes, so every other attribute is read-only to end users (an attempt returns `NotAuthorizedException`). `scripts/sdlc/tests/test_userpool_attribute_permissions.py` pins that list between its two failure modes. **Tests:** `src/lambda/external_idp_group_mapping/test_index.py` asserts that a user-written attribute and a token refresh both grant nothing, and re-runs those assertions against the InlineCode copy extracted from `template.yaml` so the deployed handler cannot drift from the tested one. `scripts/security/live_checks/verify_idp_group_mapping.py` runs the same scenarios against a real pool, provider and federated identity. **Residual:** a user already federated through the trusted provider can self-set the attribute, and if their IdP omits the group claim on a later fresh sign-in Cognito may leave that value in place — assert the group attribute for all federated users in the IdP (documented in `docs/external-idp.md`). |

### AUTH.T14: Alternate Entry Path Bypassing an Operation's Group Check (Streaming Function URL)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T14 |
| **Category** | STRIDE: Elevation of Privilege, Spoofing |
| **Description** | Agent Chat is reachable by **two** entry paths, not one. Besides `POST /op/sendAgentChatMessage` on the REST dispatcher — whose resolver checks `cognito:groups` — token deltas are streamed from a Lambda **Function URL** (`AuthType=AWS_IAM`, `InvokeMode=RESPONSE_STREAM`) whose FastAPI app (`chat_stream_processor`) drives `agent_chat_processor` directly, without passing through the resolver. An operation whose group check lives *only* in the resolver is therefore enforced on one path and unenforced on the other, and the static RBAC scan only covered dispatcher operations, so the difference was invisible to the gate. Separately, the streaming agent route resolved the caller identity from the request body *before* the transport-verified principal, so a chat turn's persisted attribution (and therefore whose history it was written against) could be decided by the client rather than by the transport. |
| **Attack Vector** | An authenticated principal invokes the streaming route for an operation whose group restriction is implemented only in the resolver; or supplies a caller identity in the request body that differs from the identity the transport verified. |
| **Impact** | The Agent Chat group restriction (Admin/Author/Viewer; Reviewer excluded) is not applied on the streaming path, and chat-session ownership/history can be keyed to an identity other than the verified caller. |
| **Likelihood** | Low (both paths require an authenticated principal of this deployment's pool; the streaming path additionally requires `lambda:InvokeFunctionUrl`, granted to the authenticated Identity Pool role) |
| **Severity** | Medium |
| **Affected Components** | `ChatStreamProcessorUrl` (`AWS::Lambda::Url` in `template.yaml`), `src/lambda/chat_stream_processor/app.py`, `src/lambda/agent_chat_processor/index.py`, `nested/api-resolvers/src/lambda/agent_chat_resolver/index.py`, `scripts/sdlc/scan_api_rbac.py` |
| **Mitigations** | **Enforce at the component that does the work, not only in front of it:** `agent_chat_processor` now applies the Agent Chat group check itself, before any agent/Bedrock call and outside the handler's error-to-stream conversion, raising `PermissionError` exactly as the resolver does; the resolver forwards the caller's group claim (and only that claim), and a unit test plus a both-directions static check assert that the two group lists cannot drift apart. **This is defence in depth, not the deciding check on either path, and the entry is written that way deliberately.** On the dispatcher path the resolver refuses an unauthorized caller *before* it invokes the processor, so the processor's copy never receives one; on the streaming path the transport carries no group claim, so it has nothing to evaluate (the residual below). Its value is that the policy no longer exists in only one place: it holds if the resolver's gate is removed or bypassed, it covers any future caller that reaches the processor directly, and it becomes the load-bearing check the moment the residual is closed. To make that closure a one-line change rather than a redesign, the streaming route applies the gate **synchronously, before the response is committed** — a denial raised after `StreamingResponse` is returned could only be rendered as an error frame on an HTTP 200, which is the same error-to-stream conversion the processor's own gate was placed outside its `try` to avoid. **One identity resolution for both routes:** the transport-verified principal takes precedence, and a body-supplied identity that *contradicts* it is refused with 403 rather than silently preferred either way; both streaming routes call the same helper. **Input shape:** both routes now declare Pydantic request models, so a malformed body is rejected with a 422 before any processing and every string the processors consume is length-bounded. **Coverage:** `make api-test-static` gained Function-URL checks (S6-S9) that discover every `AWS::Lambda::Url` and its routes from the template, require each route to be declared in `scripts/api_rbac_expectations.yaml` with its authorization, and fail when a route prefers a client-supplied identity, omits the conflict refusal, or reaches a handler with no group check — verified to fail against the pre-fix sources. **Residual:** a Function URL forwards no Cognito group claim (`requestContext.authorizer.iam.cognitoIdentity` is documented as unused by Function URLs), so on that transport the processor's group gate has nothing verified to evaluate and stands down as it does for backend IAM invocations; closing that requires the browser to also present its ID token to this endpoint. The residual is wider than the group check alone: neither streaming route obtains a **per-user** identity from the transport either, because under the Cognito Identity Pool enhanced flow the assumed-role session name is a pool-wide constant (measured, not documented, so the identity helper uses a positive shape test that degrades to the body-supplied fallback rather than refusing every request if AWS changes it). That also leaves the `allowedConfigVersions` caller-scope lookup in `chat_with_document_processor` with no verified subject to resolve, so it falls back rather than failing closed. Both halves close together when the endpoint verifies an ID token. This transport exists in the **commercial partition only** — on GovCloud the UI uses the REST dispatcher plus polling, which carries the group claim — so the residual is scoped to commercial deployments. Tracked as **GAP-07** in `scripts/api_rbac_expectations.yaml` as a `residual_gap`, which is listed for auditability but does **not** downgrade any of the S6-S9 findings above; a test drives the scanner over a defective fixture tree to prove that distinction is enforced, and an unrecognised (misspelled) policy key is now a hard failure rather than a silently ignored setting. |
### AUTH.T16: Authorization Is Opt-In Per Resolver (No Default Deny at the Dispatcher)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T16 |
| **Category** | STRIDE: Elevation of Privilege |
| **Description** | AUTH.T03 covers a resolver whose group check is *wrong*. This covers the structural reason that is easy to do: nothing requires a check to exist. The dispatcher routes any field it can resolve — 40 mapped Lambdas, 68 aliases, 11 `ddb_direct` handlers (118 distinct fields) — and, for the Lambda-routed fields, performs only argument-shape validation before invoking. A resolver that simply never consults `cognito:groups` is therefore reachable by any authenticated caller, and it fails *open* rather than closed. There is no shared enforcement primitive: three different naming conventions for the check coexist across ~40 resolvers (`_enforce_operation_group`, `_enforce_rbac`, `_caller_in_groups`), each hand-written, so "did this one get a check?" is answered per file. `ddb_direct` is the exception and enforces a table of required groups before dispatching, but its own helper returns without denying when a field has no entry. A related consequence: whether a denial becomes an HTTP 403 depends on **error-message text** — the dispatcher recognises `PermissionError`/`AuthorizationError` *or* a message beginning `Unauthorized`/`Forbidden`, and at least one resolver raises a bare `Exception` relying on that prefix, so rewording a denial message downgrades it to a 500 and it disappears from denial telemetry. |
| **Attack Vector** | A caller holding any valid pool JWT invokes `POST /op/{field}` for an operation whose resolver was shipped without a group check. No UI path is involved. |
| **Impact** | An unchecked operation is exercisable by every authenticated user regardless of group, up to and including operations intended for `Admin` only. |
| **Likelihood** | Medium (the failure mode is omission during development, not an attack step) |
| **Severity** | High |
| **Affected Components** | `http_api_dispatcher` (`index.py`, `ddb_direct.py`), all resolver Lambdas, `scripts/api_rbac_expectations.yaml` |
| **Mitigations** | **In place today:** the manifest-plus-scan pair is the real control — `scripts/api_rbac_expectations.yaml` declares the required groups for all 118 operations and `make api-test-static` (both CI systems) fails when an operation exists without a documented server-side check or drifts from the manifest; `make api-test` re-tests the matrix live against a real deployment with one user per group. One accepted gap is recorded (GAP-02). **Pending — do not read as present:** a default-deny gate in the dispatcher, so that an operation with no declared authorization is refused rather than forwarded, plus removal of the error-message-prefix dependency in the 403 mapping, is tracked in **issue #928** and is being implemented in a separate change. Until that merges, absence of a check means absence of enforcement, and the scan is the only thing standing between an omission and production. |

### AUTH.T15: Authentication Material in Resolver Logs (Divergent Redaction Denylists)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T15 |
| **Category** | STRIDE: Information Disclosure |
| **Description** | `idp_common.utils.log_sanitizer` holds the canonical denylist of key substrings whose values must never be written to logs (18 entries, covering credential, token, key and password spellings). Ten resolver Lambdas do not import it; each carries a hand-copied 10-entry subset. The copies omit several spellings that appear in real payloads — among them the underscore and concatenated forms of access key, secret key and private key, `passwd`, and the API-key header name. A request or DynamoDB item carrying one of those keys is logged with its value intact, so credential-shaped data can reach CloudWatch Logs, where the audience is everyone with log read access rather than everyone with API access. |
| **Attack Vector** | Not an attack step so much as an exposure: any operation whose logged payload contains one of the missed key spellings writes the value to its log group, where it persists for the retention period. |
| **Impact** | Credential or token material readable by principals granted CloudWatch Logs access, which is a broader and less-reviewed population than the operation's own group allow-list. |
| **Likelihood** | Medium |
| **Severity** | Medium |
| **Affected Components** | Ten resolver Lambdas under `nested/api-resolvers/src/lambda/`, `lib/idp_common_pkg/idp_common/utils/log_sanitizer.py`, `HttpApiDispatcherLogGroup` in `nested/api-resolvers/template.yaml` |
| **Mitigations** | **In place today:** every resolver does redact — the sanitizer runs on the logged payload, so the exposure is limited to the key spellings the copies omit rather than to all sensitive keys; log groups are retention-bounded, and **107 of the 109** log groups declared across `template.yaml` (56), `nested/api-resolvers/template.yaml` (33) and `patterns/unified/template.yaml` (20) set `KmsKeyId` to the stack's customer-managed key — 54 via `!GetAtt CustomerManagedEncryptionKey.Arn` in the parent template and 53 via `!Ref CustomerManagedEncryptionKeyArn` in the two nested ones. `HttpApiDispatcherLogGroup` — the log group for the component every UI API request passes through, and therefore the one most likely to contain request payloads — was the exception that mattered, and PR #973 has merged, so it now declares `KmsKeyId: !Ref CustomerManagedEncryptionKeyArn` and is inside the key policy and the key's grant-level audit trail. **Not in place:** the remaining **two** do not, and both are the `StacknameCheckFunction` and `ReadPreviousIDPPatternFunction` custom-resource groups, which handle no request data; both already carry a `cfn_nag` `W84` suppression and a `checkov:skip=CKV_AWS_158` comment in the template (`template.yaml:1718` and `:2760`), so for those two the decision was recorded even though nothing enforced it. The convention is now a control rather than a habit: `scripts/tests/test_log_group_encryption.py` fails if any log group in these templates drops the property, including via an `Fn::If` branch that resolves to `AWS::NoValue`. **Do not merge this figure with the retention figure**, which is a different property of the same 109 resources: all **109** now take `RetentionInDays` from `!Ref LogRetentionDays`. `HttpApiDispatcherLogGroup` was the only one that hardcoded `30`, and the same PR changed it, so the same gate enforces retention with no exemptions at all. The two counts are stated separately and labelled because they are easy to conflate. **Scope:** these counts are for the three core templates only. Across every template in the repository that declares `AWSTemplateFormatVersion` outside `workshop/` there are **158** log groups, of which **26** set no `KmsKeyId` — the two here plus 24 in the `feature-platform/` stacks, the customer-deployable `samples/lambda-hook-inference/` hook stacks and the copy-me scaffolding, tracked as a follow-up in **issue #972**. The core-stack scope is the right one for this entry, because those 24 belong to optional or sample stacks a deployer opts into; the wider numbers are recorded here so the smaller ones are not read as solution-wide. **Pending — do not read as present:** replacing the ten hand-copied denylists with an import of the canonical list, so there is one list to extend, is tracked in **issue #921**. Until that merges, adding a key to the canonical list does **not** protect these ten resolvers. |

## 4. Security Controls Summary

| Control | Implementation | Threats Mitigated |
|---------|---------------|-------------------|
| **IAM protection** | Restrict Cognito admin API access | AUTH.T01 |
| **Token management** | Short-lived tokens, secure storage | AUTH.T02, AUTH.T05, AUTH.T10 |
| **Dispatcher default-deny** | `authz.py` enforces a generated per-operation required-groups manifest before routing; a field with no entry is refused (403), so an undeclared operation is closed rather than open | AUTH.T03, AUTH.T08, AUTH.T16 |
| **Resolver auth** | Per-operation `cognito:groups` checks inside every resolver Lambda, on top of the dispatcher floor (the API Gateway authorizer only authenticates) | AUTH.T03, AUTH.T08, AUTH.T16 |
| **Dispatcher-side group check** | `ddb_direct._REQUIRED_GROUPS` enforces groups for the 11 in-process handlers before dispatch (restoring what the AppSync schema directive used to gate) | AUTH.T16 |
| **Claim normalization** | `api_adapter._coerce_groups` turns the REST authorizer's comma-joined `cognito:groups` string back into a list before any check reads it | AUTH.T03, AUTH.T16 |
| **Identity provenance** | `api_adapter.normalize_event` builds `identity` from the authorizer's verified claims only; an invocation that asserts its own `identity` is refused (403) rather than believed, so the group list on the event cannot be chosen by the caller who sent it (issue #978). An explicitly null `identity` — the IAM-gated service-to-service marker, which asserts no groups — still passes through | AUTH.T03, AUTH.T16 |
| **Log redaction** | `idp_common.utils.log_sanitizer` denylist applied to logged payloads (ten resolvers still carry narrower copies — issue #921) | AUTH.T15 |
| **Object-level authorization** | Owner-scoped keys / `ownerSub`-vs-caller checks on user-owned resources (chat sessions, agent jobs) | AUTH.T09 |
| **Central input-shape validation** | Dispatcher validates `arguments` against a schema-derived spec (`validation.py`); rejects unknown/missing/wrong-typed args with 400 | AUTH.T12 |
| **Config-version scope** | `allowedConfigVersions` enforced in scope-aware resolvers; resolver IAM roles granted UsersTable GSI Query | AUTH.T07 |
| **Caller-supplied ARN bounding** | `getStepFunctionExecution` requires the supplied `executionArn` to name this deployment's state machine before any Step Functions call, then applies the caller's config-version scope to the execution's `config_version` | AUTH.T07, AUTH.T09 |
| **Enforcement at the worker, not only the front door** | `agent_chat_processor` applies the Agent Chat group check itself (Admin/Author/Viewer) before any agent call, on the same group claim the resolver forwards, so both entry paths evaluate one policy | AUTH.T14 |
| **Single caller-identity resolution** | The streaming routes resolve the caller from the transport-verified principal first and refuse (403) a request-body identity that contradicts it; both routes share one helper | AUTH.T14, AUTH.T09 |
| **IdP group-claim provenance** | Group assignment from `custom:idp_groups` requires the user to be federated through the configured provider (`identities` via `AdminGetUser`) and the trigger source to be a fresh sign-in; explicit `WriteAttributes` keeps non-mapped attributes read-only to end users | AUTH.T13 |
| **Automated authorization testing** | `make api-test-static` (static scan of op↔schema↔expectations drift + missing checks, **including Lambda Function URL routes — S6-S9**) and `make api-test` (live multi-role + scoped-user + token-negative + **IDOR + token-lifecycle + deleted-resource + input-validation + TLS** suites, with an auditable report); known gaps tracked as WARN so real regressions fail the gate | AUTH.T03, AUTH.T07, AUTH.T08, AUTH.T09, AUTH.T10, AUTH.T11, AUTH.T12, AUTH.T14 |
| **Transport security** | API Gateway/CloudFront TLS 1.2+ minimum, no cleartext HTTP | AUTH.T11 |
| **Cognito config** | Self-signup off unless `AllowedSignUpEmailDomain` is set (then domain-restricted), strong passwords, email verification | AUTH.T04 |
| **Schema-vs-code drift detection** | The `@aws_cognito_user_pools` directives in `schema.graphql` are **not** a runtime control — nothing enforces them now that AppSync is gone. What *is* a control is `make api-test-static`: check **S2** asserts each directive's `cognito_groups` matches `scripts/api_rbac_expectations.yaml`, and check **S3** asserts the manifest's `enforced_in` source actually contains an enforcement pattern. A directive that disagrees with the code therefore fails the gate instead of quietly reading as protection | AUTH.T03, AUTH.T08 |
| **Audit logging** | CloudTrail for Cognito, CloudWatch for API Gateway + resolver Lambdas | All |
| **CSP headers** | Content Security Policy in CloudFront | AUTH.T02 |
| **Stack isolation** | Separate Cognito pools per deployment | AUTH.T06 |
