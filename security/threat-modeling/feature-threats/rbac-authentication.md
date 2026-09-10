# RBAC & Authentication — Threat Analysis

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.1 |
| **Last Updated** | 2026-07-17 |
| **Feature** | Role-Based Access Control & Authentication |
| **Classification** | Internal |

## 1. Feature Overview

The IDP Accelerator implements a 4-tier RBAC system using Amazon Cognito User Pools:

| Role | Precedence | Capabilities |
|------|-----------|--------------|
| **Admin** | 0 (highest) | Full system access: configuration, processing, review, agent access, user management |
| **Author** | 1 | Create/edit configurations, upload documents, run processing, use agents |
| **Reviewer** | 2 | Review processed documents, HITL review tasks, view results |
| **Viewer** | 3 (lowest) | Read-only access to processing results and dashboards |

Authorization is enforced at multiple layers:
- **Cognito Groups**: Users assigned to groups corresponding to roles
- **Resolver Lambdas**: Per-operation authorization checking Cognito group
  membership (and, for config-scoped ops, the caller's `allowedConfigVersions`)
- **Lambda Functions**: Role-aware business logic
- **UI Components**: Feature visibility based on user role

> **API architecture note (v3.0).** The UI no longer talks to AWS AppSync. It
> now calls a single API Gateway **REST** route, `POST /op/{field}`, fronted by
> a Cognito User Pools authorizer and WAF (private endpoint). The authorizer
> **only authenticates** the JWT (401 for a missing/invalid token) — it performs
> **no group evaluation**. All group/scope authorization is enforced inside the
> resolver Lambdas: an HTTP dispatcher (`http_api_dispatcher`) normalizes the
> request and invokes the same resolver Lambda that AppSync used to invoke; the
> resolver raises `PermissionError`, which the dispatcher maps to **HTTP 403**
> with `errorType: "Unauthorized"`. Config-version **scope** denials instead
> return an *in-band* `{success:false, error:{type:"Unauthorized"}}` body with
> **HTTP 200**. This shifts the authorization trust boundary entirely to the
> resolver Lambdas, which makes automated per-operation authorization testing
> (see §5) a primary control rather than a nice-to-have.

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
| **Description** | Because all authorization now lives in the resolver Lambdas (the API Gateway authorizer only authenticates), any resolver missing a server-side group check lets a lower-privilege authenticated user perform a restricted operation by calling `POST /op/{field}` directly. |
| **Attack Vector** | Call the REST API `POST /op/{field}` directly with a valid low-privilege JWT, targeting operations whose resolver omits (or misconfigures) the `cognito:groups` check — bypassing all UI-level restrictions. |
| **Impact** | Unauthorized configuration changes, document access, or processing operations |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | Resolver Lambdas, `http_api_dispatcher`, `ddb_direct` handlers |
| **Mitigations** | Comprehensive resolver-level authorization for every operation; **automated per-operation authorization testing** — a static scan and a live multi-role harness (`make api-test`, see §5) that fail on any missing/incorrect check and track known gaps; defense-in-depth `@aws_cognito_user_pools` schema directives; security review of new operations. |

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
| **Description** | Non-admin users can be restricted to specific named configuration versions via an `allowedConfigVersions` list in the UsersTable. Resolvers resolve this scope by querying the UsersTable `EmailIndex`/`SubIndex` GSI, and **fail open** (treat the caller as *unrestricted*) whenever the lookup returns nothing or raises. A missing `dynamodb:Query` IAM grant, a wrong table name, or an identity/claim mismatch therefore silently disables scope enforcement entirely — a scoped user gains read access to every configuration version. |
| **Attack Vector** | A config-version-scoped user calls a scoped op (`getConfigVersion`, `getConfigVersions`, `getPricing`, `getModelConfigLimits`, reprocess/sync/chat ops) for a version outside their allowed set; the scope query fails (e.g. resolver role lacks GSI Query permission), is caught, and the request is allowed. |
| **Impact** | Cross-scope disclosure of configuration (prompts, model settings, pricing) that a tenant/team was meant to be walled off from. |
| **Likelihood** | Medium (fail-open makes it a *silent* default whenever IAM/wiring drifts) |
| **Severity** | High |
| **Affected Components** | `configuration_resolver`, `reprocess_document_resolver`, `sync_bda_idp_resolver`, `list_documents_*_resolver`, `chat_with_document_processor`, UsersTable GSIs, resolver IAM roles |
| **Mitigations** | Every scope-enforcing resolver must be granted `dynamodb:Query`/`GetItem` on the UsersTable and its `/index/*` (verified — a missing grant on `ConfigurationResolverFunction` was found and fixed by the live harness on 2026-07-13); the **live scope suite in `make api-test`** seeds a scoped user and asserts an out-of-scope version is denied and that Admins are unaffected, catching fail-open regressions; consider logging/alerting when a scope lookup fails so fail-open is never silent. |

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
| **Mitigations** | Server-side group checks are the source of truth; schema directives are defense-in-depth only. The **static scan in `make api-test-static`** flags `@aws_auth`-only / directive-vs-code drift and fails when an operation lacks a documented server-side check; new operations must add a resolver check plus an expectations entry. The feature-platform ops that formerly relied on the silently-ignored `@aws_auth` directive (tracked as GAP-06) now declare `@aws_cognito_user_pools(cognito_groups:["Admin"])` matching their resolver enforcement. |

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

## 4. Security Controls Summary

| Control | Implementation | Threats Mitigated |
|---------|---------------|-------------------|
| **IAM protection** | Restrict Cognito admin API access | AUTH.T01 |
| **Token management** | Short-lived tokens, secure storage | AUTH.T02, AUTH.T05, AUTH.T10 |
| **Resolver auth** | Per-operation `cognito:groups` checks inside every resolver Lambda (the API Gateway authorizer only authenticates) | AUTH.T03, AUTH.T08 |
| **Object-level authorization** | Owner-scoped keys / `ownerSub`-vs-caller checks on user-owned resources (chat sessions, agent jobs) | AUTH.T09 |
| **Central input-shape validation** | Dispatcher validates `arguments` against a schema-derived spec (`validation.py`); rejects unknown/missing/wrong-typed args with 400 | AUTH.T12 |
| **Config-version scope** | `allowedConfigVersions` enforced in scope-aware resolvers; resolver IAM roles granted UsersTable GSI Query | AUTH.T07 |
| **Caller-supplied ARN bounding** | `getStepFunctionExecution` requires the supplied `executionArn` to name this deployment's state machine before any Step Functions call, then applies the caller's config-version scope to the execution's `config_version` | AUTH.T07, AUTH.T09 |
| **IdP group-claim provenance** | Group assignment from `custom:idp_groups` requires the user to be federated through the configured provider (`identities` via `AdminGetUser`) and the trigger source to be a fresh sign-in; explicit `WriteAttributes` keeps non-mapped attributes read-only to end users | AUTH.T13 |
| **Automated authorization testing** | `make api-test-static` (static scan of op↔schema↔expectations drift + missing checks) and `make api-test` (live multi-role + scoped-user + token-negative + **IDOR + token-lifecycle + deleted-resource + input-validation + TLS** suites, with an auditable report); known gaps tracked as WARN so real regressions fail the gate | AUTH.T03, AUTH.T07, AUTH.T08, AUTH.T09, AUTH.T10, AUTH.T11, AUTH.T12 |
| **Transport security** | API Gateway/CloudFront TLS 1.2+ minimum, no cleartext HTTP | AUTH.T11 |
| **Cognito config** | Self-signup off unless `AllowedSignUpEmailDomain` is set (then domain-restricted), strong passwords, email verification | AUTH.T04 |
| **Defense-in-depth** | `@aws_cognito_user_pools` schema directives in addition to resolver checks | AUTH.T03, AUTH.T08 |
| **Audit logging** | CloudTrail for Cognito, CloudWatch for API Gateway + resolver Lambdas | All |
| **CSP headers** | Content Security Policy in CloudFront | AUTH.T02 |
| **Stack isolation** | Separate Cognito pools per deployment | AUTH.T06 |
