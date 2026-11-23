# RBAC & Authentication — Threat Analysis

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.6 |
| **Last Updated** | 2026-11-17 |
| **Applies to release** | v0.6.16 |
| **Feature** | Role-Based Access Control & Authentication |
| **Classification** | Internal |

## 1. Feature Overview

The IDP Accelerator implements a **5-group** RBAC system using Amazon Cognito
User Pools (`template.yaml`, `AWS::Cognito::UserPoolGroup` x5):

| Role | Precedence | Capabilities |
|------|-----------|--------------|
| **Admin** | 0 | Full system access: configuration, processing, review, agent access, user management |
| **Author** | 1 | Create/edit configurations, upload documents, run processing, use agents |
| **Reviewer** | 2 | Production HITL review: the items of every Production review queue, legacy review of documents no review queue manages, Send to review and bulk Skip All Reviews (HITL.T02, HITL.T05), within `allowedConfigVersions`; view results. No Test Studio work: the HITL operations refuse a Reviewer-only caller a document that carries a `TestSetId` |
| **Annotator** | 3 | Ground-truth annotation within the test sets named in the user's `allowedTestSets`, and production **review-queue** items in the queues that list them as eligible. `allowedTestSets` bounds Test Set bucket keys only; on the document buckets an Annotator is bounded by `allowedConfigVersions`, like every other role (UI.T06) |
| **Viewer** | 4 | Read-only access to processing results and dashboards |

> **`Precedence` is not a hierarchy.** It is only Cognito's tiebreaker when
> resolving which IAM role an identity assumes. Every authorization decision in
> this system is a plain set intersection against an explicit per-operation
> allow-list, so `Admin` is not implicitly a superset of `Author` — it is named
> in each allow-list where it applies. Membership is additive: a user in two
> groups holds the union of both. Reading `Precedence` as a privilege ladder is
> the most likely way to mis-review a new operation's allow-list.

> **`Annotator` is granted by federation only where the deployment opts in.** The
> external-IdP group mapping covers all five roles, but
> `ExternalIdPAnnotatorGroupName` defaults to empty; while it is empty `Annotator`
> sits outside the set the pre-token trigger manages, whereas 30 server-side
> allow-lists and the UI's `APP_GROUPS` do recognise `Annotator`. On such a
> deployment the group must be assigned directly in Cognito, which places it
> outside the IdP's own joiner/leaver process — an access-review gap rather than a
> bypass. Setting the parameter brings it inside that process (**issue #968**). The
> `allowedTestSets` scope an Annotator needs is assigned in User Management either
> way, so federation alone never reaches a test set.

Authorization is enforced at multiple layers:
- **Cognito Groups**: Users assigned to groups corresponding to roles
- **Resolver Lambdas**: Per-operation authorization checking Cognito group
  membership (and, for config-scoped ops, the caller's `allowedConfigVersions`)
- **Lambda Functions**: Role-aware business logic
- **UI Components**: Feature visibility based on user role — an **affordance
  only**; every check is repeated server-side

> **API architecture.** The UI calls a single API Gateway **REST** route,
> `POST /op/{field}`, fronted by a Cognito User Pools authorizer and, optionally,
> WAF and a PRIVATE (VPC-only) endpoint. The authorizer **only authenticates**
> the JWT (401 for a missing/invalid token) — it performs **no group
> evaluation**. Almost all group/scope authorization is enforced inside the
> resolver Lambdas: an HTTP dispatcher (`http_api_dispatcher`) normalizes the
> request and invokes the resolver Lambda; the resolver raises `PermissionError`, which the dispatcher maps to **HTTP 403**
> with `errorType: "Unauthorized"`. Config-version **scope** denials instead
> return an *in-band* `{success:false, error:{type:"Unauthorized"}}` body with
> **HTTP 200**. This shifts the authorization trust boundary entirely to the
> resolver Lambdas, which makes automated per-operation authorization testing
> (see §5) a primary control rather than a nice-to-have.
>
> **Shape of that surface:** **188** operations reach the single route —
> 44 through `FIELD_FUNCTION_MAP` (an SSM-published field→Lambda map), **134**
> through `FIELD_ALIASES` onto shared resolvers, and **11** served in-process by
> `ddb_direct` (the former AppSync VTL resolvers). Those add to 189 rather than
> 188 because `getCircuitBreakerStatus` is in both the function map and
> `ddb_direct`; the distinct union is 188, matching
> `scripts/api_rbac_expectations.yaml` entry for entry. `ddb_direct` is the one place
> the dispatcher itself enforces groups, via its own `_REQUIRED_GROUPS` table.
> Across all 188: 52 require `Admin`; 43 `Admin`+`Author`; 16
> `Admin`+`Author`+`Viewer`; 11 `Admin`+`Author`+`Annotator`; 1
> `Admin`+`Annotator`; 1 `Annotator`; 16
> `Admin`+`Reviewer`+`Annotator`; 10 `Admin`+`Reviewer`; 1
> `Admin`+`Author`+`Reviewer`+`Annotator`+`Viewer` (every group, listed explicitly);
> 2 are IAM-only (rejected for every Cognito caller); **27 require any assigned
> group** (`ANY_GROUP` — every document read, the chat transcript read, the
> processing-breaker badge, three mutations and the eight Inbox operations,
> resolved at build time to every group `template.yaml` creates); and **8 require only
> authentication**, which includes a caller in no group at all.
> 65 additionally enforce config-version scope, 10 filter
> list rows by it, and 21 enforce per-object ownership. `scripts/api_rbac_expectations.yaml`
> is the manifest of record for all of this. Its `known_gaps` block holds
> `GAP-SEC-INCONCLUSIVE-5XX`, which bounds what the live harness can conclude (a
> 5xx scores a cell INCONCLUSIVE, not a pass), and one accepted authorization gap,
> **GAP-02**: the `queryKnowledgeBase` *resolver* carries no group check of its own
> (the operation is `ANY_GROUP`, so the dispatcher's floor is the only group gate
> on it). The chat Function URL verifies the caller's Cognito ID token and applies
> both chat routes' group checks from it (AUTH.T14).
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

> **Second entry path.** The REST route above is not the only way into the
> backend. Chat streaming is served by a Lambda **Function URL**
> (`AuthType=AWS_IAM`, `InvokeMode=RESPONSE_STREAM`) that the browser POSTs to
> with SigV4-signed Cognito Identity Pool credentials plus its Cognito ID token in
> the `x-idp-id-token` header; its handler verifies the token, applies the group
> checks and the session-owner check itself, and drives the two chat processors
> directly, bypassing the dispatcher and the resolvers. An operation reachable
> both ways must therefore enforce its checks in the component that does the work
> rather than only in the resolver in front of it. See AUTH.T14, and the
> `function_url_endpoints` section of `scripts/api_rbac_expectations.yaml`
> (which the static scan reads, checks S6–S9).
>
> **Third entry path.** When `EnableMCP=true`, the MCP server (an AgentCore
> Gateway whose JWT authorizer admits the clients listed under `mcp_clients`) is a
> third way in. A request interceptor (`src/lambda/mcp_identity_interceptor`)
> resolves the caller — a signed-in user, or a service credential an Admin created
> on the MCP Configuration page — refuses a tool the caller's role may not use,
> and passes a signed caller block to the handler, which applies the caller's
> configuration scope. The tools are declared under `mcp_tools` in
> `scripts/api_rbac_expectations.yaml`, each naming the REST operation it
> corresponds to, and checks M1–M5 of `make api-test-static` fail when a tool
> admits a role its REST equivalent does not. See `mcp-integration.md`.

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
| **Mitigations** | IAM policies restricting Cognito admin operations, no self-service group management, Cognito user pool advanced security features, CloudTrail logging of Cognito API calls. User Management (`Admin` only) assigns exactly the group of the persona it records; before v0.6.13 its sync mapped every non-Admin persona to `Reviewer` on a persona change, so a deployment upgrading from v0.6.12 should review the groups of users whose persona was changed there. |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

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
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

### AUTH.T03: Insufficient Authorization Granularity

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T03 |
| **Category** | STRIDE: Elevation of Privilege |
| **Description** | The API Gateway authorizer only authenticates, so authorization depends on checks behind it. Any operation whose group check is missing or wrong lets a lower-privilege authenticated user perform a restricted operation by calling `POST /op/{field}` directly, and a newly added operation is open unless a check is declared for it. |
| **Attack Vector** | Call the REST API `POST /op/{field}` directly with a valid low-privilege JWT, targeting operations whose resolver omits (or misconfigures) the `cognito:groups` check — bypassing all UI-level restrictions. |
| **Impact** | Unauthorized configuration changes, document access, or processing operations |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | Resolver Lambdas, `http_api_dispatcher`, `ddb_direct` handlers |
| **Mitigations** | **The dispatcher denies by default.** `http_api_dispatcher/authz.py` checks the caller's groups — read from the verified JWT claim, never the request body — against `api_rbac_manifest.json` before routing, and a field with **no entry** is refused with 403: an operation whose required groups were never declared is closed rather than open, so a forgotten resolver check on a **group-scoped** operation is not an open endpoint. ⚠️ **This does not cover every operation.** 8 of the 188 declared operations are declared `ANY` — the dispatcher enforces authentication but not group membership for those — so a forgotten resolver check on one of the 8 is still reachable by any authenticated caller, including a caller in no group at all where self-signup is enabled (`AllowedSignUpEmailDomain`, see AUTH.T04). They are `getMyProfile`, `listChatSessions`, `getLatestPublishedVersion`, the two fine-tuning job reads and the three feature-platform reads, and each entry in `scripts/api_rbac_expectations.yaml` records why `ANY` is the intended policy for **that operation** rather than for the section it sits in. A reason written for a set ("see `listChatSessions` — same decision") is read for whether it holds broadly, not for whether it holds of the one operation carrying it, and the mechanism it points at can differ: `listChatSessions` bounds its read with a DynamoDB key condition on the caller's own partition, while `getChatMessages` reads by session id and layers a separate ownership check (AUTH.T09). 178 require a group: 151 name a subset of roles and 27 are declared **`ANY_GROUP`** — any group `template.yaml` creates, so what those 27 **operations** refuse is precisely the self-registered caller whose `cognito:groups` claim is empty. The remaining 2 are `IAM_ONLY`, which is not a group requirement at all: they reject every Cognito caller irrespective of group. The `ANY_GROUP` set is every document read (`getDocument`, `listDocuments`, `listDocumentsByDateRange`, `getDocumentCount`, `listDocumentsDateHour`, `listDocumentsDateShard`, `listDocumentVersions`, `getDocumentVersion`, `compareDocumentVersions`, `getStepFunctionExecution`, `getFileContents`, `getFilePresignedUrl`, `getFilePresignedUrls`, `queryKnowledgeBase`), the chat transcript read `getChatMessages`, the processing-breaker badge `getCircuitBreakerStatus`, and `deleteAgentJob`, `deleteChatSession` and `sendChatDocumentMessage`, and the eight Inbox operations (`listInboxMessages`, `getInboxUnreadCount`, `getInboxPreferences`, `markInboxMessages`, `markAllInboxMessagesRead`, `deleteInboxMessages`, `clearReadInboxMessages`, `updateInboxPreferences`), each self-scoped to the caller's verified identity; the sentinel is resolved against the `AWS::Cognito::UserPoolGroup` resources at build time, so a group added to the template joins the set without an edit per operation. The document reads in that set include ones that return no extracted value themselves — object keys, a run's section and page URIs, the list of extracted attribute names, a model-written description of the page contents, the workflow execution input and step history — because together they compose into one chain to the content. `getChatMessages` is in it because its ownership check can be switched off (`ENFORCE_CHAT_SESSION_OWNERSHIP=false`), and `getCircuitBreakerStatus` because its `lastError` field carries `Manual pause by <administrator email>: <reason>` after any manual pause. ⚠️ Three things the group floor does not itself decide. Item (1) is an open residual; items (2) and (3) are closed, and (3) is what makes this floor a control on the document bytes rather than only on the API. **(1)** A group check is not a **per-document** check, and the per-document check that exists runs on the configuration-profile axis rather than on ownership: `document_scope` resolves the document owning an Input- or Output-bucket key from the tracking table and matches its `ConfigVersion` against the caller's `allowedConfigVersions` (UI.T06). ⚠️ **State this per caller shape, not per group.** For a **scoped** caller it is a real refusal — a document stamped with a profile outside their scope is denied before S3 is touched, and so are a key no tracked document owns and a document carrying no `ConfigVersion`, both fail-closed. For an **unscoped** caller — a UsersTable row that sets no scope, or no row at all — under the default `TenantIsolationMode=Off` with `TenantGrantSource=Manual` it is deliberately a no-op, because scoping is opt-in per user, so a caller holding any group may still read any document's bytes through the resolver, which is precisely the reach `listDocuments` already gives them; under `Strict` or an external grant source an unscoped non-Admin resolves to a scope no profile matches and is refused (TEN.T01). "Any document that group can see" remains the wrong frame for the same reason it always was: it is true of an unscoped caller, for whom a document's visibility really is a group question, and false of a scoped one, for whom the entire point of `allowedConfigVersions` or `allowedTestSets` is content their group must NOT see. The object-read path enforces the two **per-user** axes on the key as well — a scoped Author cannot read another configuration profile's revision bodies from the Configuration bucket, and a scoped Annotator cannot read another test set's source documents or ground truth from the Test Set bucket. What stays open is per-**document** scope in the sense of ownership, which the product does not define: no record says which user a document belongs to, so there is no such axis for a key check to consult. A tenant is a set of profiles and Input prefixes, so the profile check is also the tenant check. **(2)** The dispatcher's floor gates the **REST route**; `POST /chat/document` on the chat Function URL reaches the same processor as `sendChatDocumentMessage` and applies the same `ANY_GROUP` floor itself, from the Cognito ID token it verifies (`_require_a_group_or_403` in `chat_stream_processor/app.py`), so a groupless caller is refused on both paths (AUTH.T14; check S9 compares the two). **(3) The floor gates the API, and the buckets are behind the API.** `CognitoIdentityPoolSetRole` attaches a **single** `authenticated` role with **no `RoleMappings`**, so group membership plays no part in which role a signed-in user assumes: whatever that role can read, every signed-in user can read, groupless included, with no resolver in the path to apply any check. `CognitoAuthorizedRole` therefore grants **nothing** on the Input and Output buckets, **no** `s3:GetObject`, `s3:GetObjectVersion` or `s3:ListBucket` on any bucket, and **no** KMS action; its two inline policies are `lambda:InvokeFunction` on the chat-streaming Function URL and `ssm:GetParameter` on the settings parameter ([#1033](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1033)). A self-registered caller in no group who runs `cognito-identity get-id` and `get-credentials-for-identity` with their own ID token and assumes that role fails at `s3api get-object`, because the vended credentials can read no bucket. The UI asks `getFileContents`, `getFilePresignedUrl` or `getFilePresignedUrls` for every document byte it displays, including the file viewer, the page thumbnails, the page-image viewer and the document export, so the **fetch** needs an API operation exactly as the **key discovery** does, and each of those operations is declared `ANY_GROUP` — which is what puts this floor in front of the asset. Two further controls run in the same choke point (`_parse_and_validate_one`): the bucket allow-list, and the per-document and per-user key scopes described in residual (1). The option that was not taken — group-scoped Identity Pool `RoleMappings` — and the obstacles each shape ran into (the Annotator's per-object `allowedTestSets` scope has no IAM expression, four UI surfaces signed in the browser, and the dispatcher's 6 MiB response cap is why `getFilePresignedUrl` exists alongside `getFileContents`) are set out in [Identity Pool group scoping](../../../docs/planning/identity-pool-group-scoping-plan.md). ⚠️ **The two per-user-partitioned buckets must stay off this role.** A browser grant on the **Configuration bucket** would make the configuration-revision store — `config_revisions/<profile>/<nnnnnn>.json.gz`, every profile's recorded prompts and few-shot examples — readable by any authenticated user, defeating `allowedConfigVersions` with no API call to detect; neither it nor the Test Set bucket is on the role, so both axes are reachable only through the three object-read operations, where the key check applies. `scripts/tests/test_browser_s3_grants.py` pins this role's policy set in both directions, so an S3 statement cannot come back under any policy name without the gate saying so. **An `identity` carried on the event is not authoritative** ([#978](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/978)): a principal holding `lambda:InvokeFunction` on the dispatcher could otherwise state its own `cognito:groups` in an invocation event and have this check made against its own claim. `idp_common.api_adapter.normalize_event` builds the identity from the authorizer's verified claims, refuses an asserted identity that contradicts them, and refuses one presented with no verified claims at all; the one shape still passed through is an explicitly null `identity`, which is this repository's marker for a service-to-service invocation gated by IAM on the function ARN and asserts no groups. A guard drives the dispatcher end to end with an operation and required groups read from the manifest at test time, and a second one discovers every consumer of the adapter by parsing the tree and fails if one lets the refusal escape as a 5xx instead of a 403. The manifest is generated from `scripts/api_rbac_expectations.yaml` (`scripts/sdlc/generate_api_rbac_manifest.py`), the same file the static scan and the live harness already assert against, so there is no second list to keep in step; it fails closed if unreadable. Resolver-level authorization for every operation remains in place on top of it — the dispatcher enforces the field-level floor, the resolver enforces per-object scope. Plus **automated per-operation authorization testing** — a static scan (including a manifest drift guard) and a live multi-role harness (`make api-test`, see §5) that fail on any missing/incorrect check and track known gaps; security review of new operations. **Not a control:** the `@aws_cognito_user_pools` directives in `schema.graphql` are **not** defense-in-depth here — nothing enforces them at runtime, so they carry no enforcement weight at all. They are retained as the input for the dispatcher's shape validation and as documentation of intent, and they are read by the static scan as one of the things it checks *for drift against the code*. Reading them as a second layer is exactly the mistake AUTH.T08 describes. |
| **Evidence** | `scripts/tests/test_browser_s3_grants.py`; `scripts/sdlc/generate_api_rbac_manifest.py`; `make api-test`; UNVERIFIED: the citations have not been matched to each listed mitigation |

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
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

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
| **Mitigations** | Configurable refresh token expiration: the `RefreshTokenValidityHours` parameter (default 720, 30 days) sets the web UI client's lifetime, and the client is left unmodified at the default (`HasCustomRefreshTokenValidity` in `template.yaml`). Token revocation capabilities, Cognito advanced security (anomaly detection), secure token storage, session monitoring |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

### AUTH.T06: Cross-Tenant Data Access (Multi-Stack)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T06 |
| **Category** | STRIDE: Information Disclosure |
| **Description** | Organizations may deploy one stack per tenant or environment. If users have access to multiple stacks' Cognito pools, they could access data across environments. Several tenants sharing **one** stack is a different boundary, inside the deployment, and is modelled separately as TEN.T01–T05 in [`tenant-isolation.md`](tenant-isolation.md) |
| **Attack Vector** | User with credentials for multiple stacks accesses data from an environment they shouldn't have access to |
| **Impact** | Cross-environment data access |
| **Likelihood** | Low |
| **Severity** | Medium |
| **Affected Components** | Cognito User Pools (per stack), S3 buckets, DynamoDB tables |
| **Mitigations** | Separate Cognito User Pools per stack (default), IAM resource policies scoped to individual stacks, organizational controls on user provisioning |
| **Evidence** | UNVERIFIED: no listed mitigation cites a test, gate or make target |

### AUTH.T07: Config-Version Scope Bypass (Fail-Open Scope Lookup)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T07 |
| **Category** | STRIDE: Elevation of Privilege, Information Disclosure |
| **Description** | Non-admin users can be restricted to specific named configuration versions via an `allowedConfigVersions` list in the UsersTable. The scope is resolved by reading the caller's UsersTable row, and a lookup written carelessly **fails open** — treats the caller as *unrestricted* — on two distinct paths. (a) If the **lookup key** is derived from a fallback chain (`email` claim, else `identity.username`, else `cognito:username`, else `sub`) put to a single email-keyed index, a substituted identifier does not find the row by another route — it matches **no** row, and an empty page is indistinguishable from "this user has no restriction". Any verified claims set lacking an `email` claim then resolves to *unrestricted*, with **no AWS fault required**. The same arithmetic makes **email itself** an unsafe sole join: an address that diverges from the one on the row (a case difference, an external-IdP attribute remap, a self-service change on a mutable pool) also reads as "no row", and so lifts the restriction with no attacker action at all; and because Cognito does not make `email` unique, two accounts can share an address and so a row. (b) If a **failed query** (a missing `dynamodb:Query` grant, a wrong index name, a throttle) is caught and read the same way, scope enforcement switches itself off precisely on the wiring drift it exists to survive. |
| **Attack Vector** | A config-version-scoped caller invokes a scoped operation (`getConfigVersion`, `getConfigVersions`, the five revision operations, `reprocessDocument`, `syncBdaIdp`, `getStepFunctionExecution`, both document lists, `getDocumentCount`, document chat, class, policy and multi-document discovery, `autoDetectSections`, the test-set operations that take a profile, `sendToReview`, `bulkSkipAllReviews`) for a version outside their allowed set, under verified claims from which the lookup key cannot be derived, or while the scope query is failing. The lookup returns "no restriction" and the request is allowed. Two further routes need no unresolvable caller at all: an operation that checks scope only on a caller-supplied `version` argument (as `reprocessDocument` would if it checked nothing else) acts on any named document when the argument is omitted; and an operation that chooses a profile without checking it (`uploadDocument`, `uploadSampleDocument`, a discovery run naming a target profile) places a document, or a discovered class or rule, into another scoped user's profile. **Config prefix mappings widen that route rather than adding a new threat**, because they introduce a second way to choose a profile which is not the `version` argument at all — the destination key prefix — so a check written against the *request* would miss it; and because an S3 key prefix is now part of what decides the partition, whoever may write a mapping decides which scoped users can see the documents landing under it. |
| **Impact** | Cross-scope disclosure of configuration (prompts, model settings, pricing) and of documents and their processing detail that a tenant/team was meant to be walled off from; and, for `reprocessDocument`/`syncBdaIdp`, mutation under a profile outside the caller's scope. |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | `configuration_resolver`, `reprocess_document_resolver`, `upload_resolver`, `discovery_upload_resolver`, `test_set_resolver`, `process_changes_resolver`, `bulk_review_resolver`, `sync_bda_idp_resolver`, `get_stepfunction_execution_resolver`, `list_documents_gsi_resolver`, `list_documents_range_resolver`, `chat_with_document_processor`, `user_management` (own-profile lookup), the PII-anonymizer feature API, UsersTable `EmailIndex`, `idp_common/config/prefix_mappings.py` and the `ConfigPrefixMap#__index` item on the ConfigurationTable, resolver IAM roles |
| **Mitigations** | **One fail-closed lookup, shared.** `resolve_allowed_config_versions` in `idp_common/config_scope.py` is the single implementation, and it reads **two disjoint key spaces** on the UsersTable: the immutable Cognito `sub`, via a `SUB#<sub>` pointer item holding the row's `userId`, tried first; and the `email` claim, via the `EmailIndex` GSI, tried second. Each identifier goes only to the key space that indexes it, and neither is a fallback for the other — a `sub` is never put to an email-keyed condition. Preferring the `sub` is what stops a diverged address lifting the restriction. No UsersTable wired / neither key present / any failed read on **either** key space raises `ScopeLookupError`, which every consumer turns into a refusal; so does a caller presenting only a `sub` that no pointer records, because their row may predate the pointer writer and there is no second key to try. The pointer reuses the table's existing `PK`/`SK` key schema rather than adding a GSI, so **no deployment takes a table update** and there is no index-backfill window in which a fail-closed lookup would deny every scoped caller; `dynamodb:GetItem` was already granted to every consumer alongside `Query`. A pointer carries no `email` attribute, so it is absent from `EmailIndex` and cannot be returned by an email query in place of the row. `src/lambda/user_management` is the writer: `createUser` records the `sub` Cognito assigns, `updateUser` writes the pointer when a scope is set and deletes it when one is removed, `deleteUser` deletes it, and the Cognito sync that runs on every Admin `listUsers` records the `sub` and maintains the pointer on each row it can match — which is the workflow an administrator must use to apply a scope at all. That sync matches a Cognito account to its row on the recorded `sub`, then the exact address, then the **case-folded** address, and leaves a row that another account still in the user pool records bound to that account: matching on the exact address alone is what writes a *duplicate* row for a user whose address has changed, and a duplicate carries no scope. **A pointer exists only for a row that carries `allowedConfigVersions`**, which is what makes the preferred leg incapable of *widening* a scope: an unscoped row answers "unrestricted" through either key, so a pointer for one would pin that answer ahead of whatever the email join would have found — including ahead of the scoped row a duplicate was created beside. The reader enforces the same invariant from its side, treating a pointer that resolves an unscoped row as stale and falling back to the email join. **The reader binds the record to the account's Cognito `sub`.** Cognito does not make `email` unique, so the email join reads every row for the address and uses the one recording the caller's `sub`, then one recording none, then any other; a record naming another `sub`, reached by either leg, denies the caller wherever an unscoped caller is denied (strict isolation, `TenantGrantSource` `IdpClaim` or `Api`), and under `Manual` with isolation off its scope is applied, where it can only narrow access. The per-container cache holds the row's raw scope and that flag, so a cached answer is judged against the mode in force at each call. An **empty page still means unrestricted**, deliberately — scoping is opt-in per user, and denying there would lock every ordinary user out. The two document-list resolvers and the two artifacts that ship without an `idp_common` layer (`user_management`, the PII-anonymizer feature API) carry the rule verbatim rather than a variant of it. **Both directions are checked, not just the argument.** `reprocessDocument` resolves the profile each named document was last processed under and refuses any outside the caller's scope, so the control does not stand down when the caller omits `version`; a document whose profile cannot be established (no tracking row, no `ConfigVersion`, a failed read) is refused, and a batch is refused whole rather than part-processed. **`uploadDocument` and `uploadSampleDocument` enforce the same scope, and on the *resolved* profile rather than the requested one** — the only form that covers both routes to choosing a profile, since naming one in upload metadata and choosing a destination prefix a mapping governs are different requests with the same effect. A non-canonical key cannot evade a mapping: `find_match` canonicalizes the object key itself — collapsing leading and repeated slashes and `.` segments — so `finance/x.pdf`, `/finance/x.pdf`, `finance//x.pdf` and `finance/./x.pdf` all resolve to the same mapping, and a `reject` rule on that prefix fires for every spelling. `..` is matched literally rather than resolved, because an S3 key has no parent directory and resolving it would claim a key the bucket serves from elsewhere. The upload site canonicalizes too, which is what makes the stored key agree with the mapping prefix recorded against the document rather than being a second line of defence. **Prefix-mapping CRUD is `Admin` only** (`listConfigPrefixMappings`, `putConfigPrefixMapping`, `deleteConfigPrefixMapping`), which is an authorization decision rather than a convenience one: an Author who could write a mapping could route documents into, or out of, another scoped user's partition, which puts it on the same side of the line as minting a profile rather than editing one's content. The listing is deliberately **not** scope-filtered for the same reason it is Admin-only — a partially-hidden view of a longest-prefix rule cannot tell you which mapping wins. The read-only dry run `resolveConfigPrefixMapping` is open to **Admin and Author** — the two roles that can upload, which is what it serves — and is **scope-filtered over every profile the answer could disclose**, not merely the one it selected: a refusal resolves to no profile while its reason names the mapped one, and metadata precedence resolves to the caller's own in-scope profile while its reason explains that it beat the mapping's, so a guard predicated on the selected profile would leak through both. It withholds the matched **prefix** as well, because the caller supplies a key and the governing prefix is a refinement of it that locates the boundary a probe at a time. **Discovery and test-set operations hold a named profile to the caller's scope.** `discovery_upload_resolver` checks the target profile (`_assert_profile_in_scope`, which with no profile named checks the active one) and the source documents (`_assert_source_in_scope`) for class, policy and multi-document discovery and `autoDetectSections`, and refuses **Replace** on the active profile (`_assert_replace_allowed`); discovered classes and rules are stored as plain text with URLs, images and HTML removed (`idp_common/discovery/discovered_text.py`). `generateDraftLabels`, `reextractTestSetDocument` and `estimateReviewEffort` refuse a named profile outside the caller's scope, as `startTestRun` does. ⚠️ **All of this gates the API, not the bucket:** a principal holding `s3:PutObject` on the Input bucket picks its own prefix and its own `x-amz-meta-config-version` with no API call in the path, which is the bucket-side residual UI.T06 carries; bound it with IAM on whatever writes there. **The class is gated, not just the instances:** `scripts/tests/test_scope_lookup_fail_closed.py` walks every module under `nested/api-resolvers/src/lambda`, `src/lambda` and `feature-platform` that reads the UsersTable — recognised by the **table**, not by an index name, since naming the index wrongly is itself one of the ways this failed, and counting a `get_item` as well as a `query`, because the `sub` key space names no index at all — and fails if any derives a key from another claim, puts one key space's identifier to the other's (rule **SCOPE4**, which is the `SubIndex` shape this threat is named after), or handles a lookup failure with anything but a refusal (including a suppressing context manager, a conditional raise that falls through, and a success-shaped payload). Per-site unit suites assert the three outcomes (no `email` claim denies without querying; a DynamoDB error denies; an empty page stays unrestricted). Static check **S4** of `make api-test-static` reads only the code an operation can *reach* — its dispatch branch bodies and their call closure — so a declaration the reachable code does not honour fails; check **S10** fails when an operation whose arguments name a profile, a document or a session declares no scope for it (`scope_checked`, `scope_filtered`, `document_scope_checked`, `testset_checked` or `ownership`). Every scope-enforcing resolver holds `dynamodb:Query`/`GetItem` on the UsersTable and its `/index/*`; a failed lookup is logged at ERROR and denies. The **live scope suite in `make api-test`** seeds a scoped user and asserts an out-of-scope version is denied and that Admins are unaffected. **Residual:** (1) the `sub` join closes the email-divergence route only for a row that **records** a `sub`, and the back-fill cannot reach every row. It matches on the recorded `sub`, the exact address, or the case-folded address, so a user whose address was re-mapped or changed *beyond case* before this code ever ran matches none of them: the sync writes them a fresh unscoped row, as it always has, and their original scoped row keeps the email-only exposure indefinitely. (The scoped-rows-only pointer invariant keeps that from widening anything — the duplicate gets no pointer, so correcting the address still restores the scope.) The back-fill also runs only when an Admin opens User Management. `docs/rbac.md` carries a one-line `attribute_not_exists(cognitoSub)` scan that lists the rows still in that state, which is the only way to find them. Neither the writer, the back-fill nor the two-key read has been exercised against a live deployment — every statement about them is inferred from the code and from moto-backed suites; (2) nothing asserts at the adapter which *kind* of Cognito token produced the claims, so which token types can reach these operations is a property of the API Gateway authorizer configuration rather than of this code. **Measured on a live deployment 2026-09-20: an access token is refused.** The `COGNITO_USER_POOLS` authorizer on `POST /op/{field}` declares no `authorizationScopes`, and a Cognito **access** token — which decodes cleanly and even carries `cognito:groups` — is rejected **401 at the gateway, before any Lambda runs**, while the ID token from the same session succeeds. So the substituted-identifier path this residual describes is not reachable by presenting an access token to this API. It remains a property of the authorizer rather than of the code, so a future authorizer change could reopen it, and nothing in the tree asserts the token type; (3) the class gate checks **key provenance and failure handling only** — it has no rule about the *matcher*, so a divergent `scope_allows` restatement in an artifact that ships without the shared layer would not be caught (only the two byte-identical vendored copies are held to the letter, by a file-comparison test), and `lib/idp_common_pkg` is outside its scan roots, which leaves `idp_common/testset_scope.py` — a further `EmailIndex` consumer, for the independent `allowedTestSets` axis — unpoliced; its polarity is inverted, so the shapes are not live there; (4) one consumer the gate discovers is **suppressed** rather than enforced: `idp_common/testset_scope.py`, which resolves the independent `allowedTestSets` axis from the same `EmailIndex` with an `or`-chain key and a caught query failure — both shapes this gate exists to forbid. Its polarity is inverted (`assert_can_access_test_set` requires an *explicit* scope, so an unresolvable Annotator is denied), which makes those shapes an availability defect rather than one that widens access, and it has no `sub` join at all, so an Annotator whose address diverges is locked out of their queue. It is carried in `PENDING_FIX` with its two rules named, so any *other* rule firing on it still fails the gate and `test_the_pending_exemption_is_still_needed` deletes the entry the moment those two stop firing. |
| **Evidence** | `scripts/tests/test_scope_lookup_fail_closed.py`; `lib/idp_common_pkg/tests/unit/test_tenant_grant_sub_binding.py`; `nested/api-resolvers/src/lambda/discovery_upload_resolver/test_profile_scope.py`; `make api-test-static`; `make api-test`; UNVERIFIED: the citations have not been matched to each listed mitigation |

### AUTH.T08: Silently-Ignored Schema Authorization Directives

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T08 |
| **Category** | STRIDE: Elevation of Privilege |
| **Description** | The GraphQL schema carries authorization directives (`@aws_cognito_user_pools(...)`, and the `@aws_auth(cognito_groups: [...])` form that a multi-auth API **silently ignores**), and nothing enforces either at runtime: the REST dispatcher fronts the resolvers and reads the schema only for input-shape validation. A developer who relies on a schema directive alone — without a server-side check in the resolver — ships an unprotected operation that *looks* protected in the schema. |
| **Attack Vector** | Add/keep an operation whose only "protection" is a schema directive; a low-privilege caller invokes `POST /op/{field}` and is authorized because no resolver-side check runs. |
| **Impact** | Operations appear group-restricted but are open to any authenticated user. |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | `schema.graphql`, resolver Lambdas |
| **Mitigations** | Server-side group checks are the source of truth; schema directives carry no enforcement. Since the dispatcher **denies by default** (AUTH.T03), an operation protected by a directive alone is not open: with no entry in the generated required-groups manifest it is refused for every caller, so the failure mode is a visible 403 rather than a silent bypass. The **static scan in `make api-test-static`** flags `@aws_auth`-only / directive-vs-code drift and fails when an operation lacks a documented server-side check; new operations must add a resolver check plus an expectations entry. The feature-platform ops declare `@aws_cognito_user_pools(cognito_groups:["Admin"])` matching their resolver enforcement, so no operation relies on `@aws_auth`. |
| **Evidence** | `make api-test-static`; UNVERIFIED: the citations have not been matched to each listed mitigation |

### AUTH.T12: Missing Input-Shape Validation (Type Confusion via Lost Schema Validation)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T12 |
| **Category** | STRIDE: Tampering, Denial of Service |
| **Description** | The REST dispatcher receives `event["arguments"]` as arbitrary JSON. Without validation against the schema, a caller can send an object/array where a scalar is expected, an unknown argument, or omit a required one — reaching resolver code that indexes into the shape untyped. Depending on the resolver that means silent acceptance or an uncaught 500, and a wider surface for type-confusion / NoSQL-style injection payloads flowing into DynamoDB expressions or LLM prompts. |
| **Attack Vector** | A caller posts `POST /op/{field}` with a malformed `arguments` object (e.g. `{"ObjectKey": {"$ne": null}}` where a `String!` is expected) that the resolver was not written to defend against. |
| **Impact** | Type-confusion / unexpected-shape handling in resolvers; denial of service via uncaught 500s; loss of the boundary that constrained input before business logic. |
| **Likelihood** | Medium |
| **Severity** | Medium |
| **Affected Components** | `http_api_dispatcher` (index.py), all resolver Lambdas, `ddb_direct` |
| **Mitigations** | **Central schema-shape validation in the dispatcher** (`validation.py` + build-time `api_validation_spec.json` generated from `schema.graphql`) rejects unknown args, missing non-null args, wrong scalar types, and bad enum values with HTTP 400 before routing, for every operation at once. The validator is conservative (type-only, shallow input-objects) and fails open on its own errors so it can't 500 the API. A drift guard (`generate_api_validation_spec.py --check` + unit test) keeps the spec in sync with the schema. The **input-validation suite in `make api-test`** exercises malformed payloads per op (strict mode asserts a clean 4xx). |
| **Evidence** | `make api-test`; UNVERIFIED: the citations have not been matched to each listed mitigation |

### AUTH.T09: Insecure Direct Object Reference (IDOR / BOLA)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T09 |
| **Category** | STRIDE: Information Disclosure, Elevation of Privilege |
| **Description** | User-owned resources (chat sessions, agent jobs) are addressed by an id supplied in the request. If a resolver keys the record by the supplied id alone — without also constraining to the caller's own identity — one authenticated user (User B) can read or modify another user's (User A's) data by guessing/replaying the id. This is the "broken object-level authorization" class the RBAC group matrix does NOT catch (both users are the same *role*). |
| **Attack Vector** | User B calls `getChatMessages`/`deleteChatSession`/`sendAgentChatMessage`/`sendChatDocumentMessage`/`getAgentJobStatus`/`deleteAgentJob`, or a chat streaming route, with User A's `sessionId`/`jobId`; or a Reviewer calls `getBulkReviewJob`/`cancelBulkReviewJob`/`deleteBulkReviewJob` with another user's `jobId`. |
| **Impact** | Cross-user disclosure, extension or destruction of chat history, agent conversation memory or job data within the same deployment. |
| **Likelihood** | Medium |
| **Severity** | High |
| **Affected Components** | `get_agent_chat_messages_resolver`, `delete_agent_chat_session_resolver`, `agent_chat_resolver`, `send_chat_document_message_resolver`, `chat_stream_processor`, `agent_chat_processor`, `idp_common/chat_session_owner.py`, `ddb_direct` agent-job ops, `bulk_review_resolver`, ChatSessions/ChatSessionOwners/ChatMessages/Agent tables |
| **Mitigations** | Ownership is enforced by construction where the key allows it (agent-job ops derive the DynamoDB partition key from the caller identity, so a foreign id resolves under the caller's own empty partition; `listChatSessions` reads the caller's own partition). For chat, whose messages are keyed by session id alone, `ChatSessionOwnersTable` records each session id's owner — the Cognito `sub` from verified claims — with a conditional write, so the first user of an id owns it (`idp_common/chat_session_owner.py`, vendored byte-identical into the resolvers without the layer). Every path that writes a turn (both REST send operations and both streaming routes) registers or checks the owner before any processing; `getChatMessages` and `deleteChatSession` check it and never register a fresh id; a session written before the registry is claimed only by the caller holding older evidence of using it. Agent conversation memory is keyed on the verified `sub`. `getChatMessages`'s check is on by default (`ENFORCE_CHAT_SESSION_OWNERSHIP`) and denies when its tables are unconfigured. Bulk review jobs are readable and changeable only by their requester (`RequesterKey` from verified claims) or an Admin; another user's job reads as not found. The **live IDOR suite in `make api-test`** seeds a session owned by User A and asserts User B is denied/empty for that id while the owner retains access — catching a regression that dropped the ownership check. |
| **Evidence** | `make api-test`; `lib/idp_common_pkg/tests/unit/test_chat_session_owner.py`; `nested/api-resolvers/src/lambda/get_agent_chat_messages_resolver/test_session_owner_rest.py`; `src/lambda/chat_stream_processor/tests/test_processor_session_owner.py`; `scripts/tests/test_chat_session_owner_vendored.py`; UNVERIFIED: the citations have not been matched to each listed mitigation; `ENFORCE_CHAT_SESSION_OWNERSHIP=false` switches the read check off and is not guarded |

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
| **Mitigations** | Keep token TTL short (the web UI client sets 1-hour ID and access token validity, with token revocation enabled) so the post-logout window is bounded; expired tokens ARE rejected (verified by the token-negative + expiry suites in `make api-test`). The **logout suite in `make api-test`** performs a global sign-out and re-tests the token, surfacing continued acceptance as a documented gap (**GAP-SEC-LOGOUT**, WARN) so the accepted-risk is visible. Full revocation would require an authorizer-side revocation check (Cognito token revocation / a deny-list) — tracked as a follow-up, not yet implemented. |
| **Evidence** | `make api-test`; UNVERIFIED: the citations have not been matched to each listed mitigation |

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
| **Evidence** | `make api-test`; UNVERIFIED: the citations have not been matched to each listed mitigation |

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
| **Evidence** | `scripts/sdlc/tests/test_userpool_attribute_permissions.py`; `src/lambda/external_idp_group_mapping/test_index.py`; `scripts/security/live_checks/verify_idp_group_mapping.py`; UNVERIFIED: the citations have not been matched to each listed mitigation |

### AUTH.T14: Alternate Entry Path Bypassing an Operation's Group Check (Streaming Function URL)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T14 |
| **Category** | STRIDE: Elevation of Privilege, Spoofing |
| **Description** | Agent Chat and Chat-with-Document are reachable by **two** entry paths. Besides `POST /op/sendAgentChatMessage` and `POST /op/sendChatDocumentMessage` on the REST dispatcher, token deltas are streamed from a Lambda **Function URL** (`AuthType=AWS_IAM`, `InvokeMode=RESPONSE_STREAM`) whose FastAPI app (`chat_stream_processor`) drives `agent_chat_processor` and `chat_with_document_processor` directly, without passing through the dispatcher or the resolvers. IAM authentication of that transport proves only that the caller holds the Identity Pool's authenticated role, which carries no Cognito group. An operation whose group, scope or ownership check lives only in the resolver is therefore enforced on one path and not the other, and a route that takes the caller's identity from the request body lets the client decide whose session and history a turn is written against. |
| **Attack Vector** | An authenticated principal invokes a streaming route for an operation whose group restriction is implemented only in the resolver; supplies a caller identity in the request body that differs from the verified one; or names another user's session id. |
| **Impact** | The Agent Chat group restriction (Admin/Author/Viewer; Reviewer excluded), the `ANY_GROUP` floor on document chat, the Chat-with-Document profile scope or session ownership not applied on the streaming path, and chat history or memory keyed to an identity other than the verified caller. |
| **Likelihood** | Low (both paths require an authenticated principal of this deployment's pool; the streaming path additionally requires `lambda:InvokeFunction` on the URL, granted to the authenticated Identity Pool role) |
| **Severity** | Medium |
| **Affected Components** | `ChatStreamProcessorUrl` (`AWS::Lambda::Url` in `template.yaml`), `src/lambda/chat_stream_processor/app.py`, `src/lambda/agent_chat_processor/index.py`, `src/lambda/chat_with_document_processor/index.py`, `nested/api-resolvers/src/lambda/agent_chat_resolver/index.py`, `scripts/sdlc/scan_api_rbac.py` |
| **Mitigations** | **The transport carries verified claims.** The browser sends its Cognito ID token in `x-idp-id-token`, and both routes verify it (`_verified_caller`) and answer 401 without a valid one; nothing identity-like is read from the request body, and a body `callerSub` is ignored. **Checks in the route, before the response is committed.** `/chat/agent` applies the Agent Chat groups, and the Admin-only rule under tenant isolation, synchronously (`_enforce_groups_or_403`); `/chat/document` applies the `ANY_GROUP` floor (`_require_a_group_or_403`); both register or check the session id against its owner in `ChatSessionOwnersTable` (`_claim_session_or_403`) before the stream opens, so a refusal is an HTTP 403 rather than an error frame on a 200. **Enforcement at the worker as well.** `agent_chat_processor` applies the same group check before any agent or Bedrock call, outside its error-to-stream conversion, on the group claim either path forwards, and keys conversation memory on the verified `sub`; `chat_with_document_processor` applies the `allowedConfigVersions` scope from the verified email and `sub`. A unit test plus a both-directions static check assert that the resolver's and the processor's group lists cannot drift apart. **Input shape:** both routes declare Pydantic request models, so a malformed body is rejected with a 422 and every string the processors consume is length-bounded. **Coverage:** `make api-test-static` checks S6–S9 discover every `AWS::Lambda::Url` and its routes from the template, require each route to be declared in `scripts/api_rbac_expectations.yaml` with its authorization, and fail when a route prefers a client-supplied identity, omits the conflict refusal, or reaches a handler with no group check; a test drives the scanner over a defective fixture tree, and an unrecognised policy key is a hard failure. Under tenant isolation or an external `TenantGrantSource` the web UI is built without the stream URL (`DeniesUnscopedCaller`) and uses the REST path; the transport exists in the commercial partition only, and on GovCloud the UI uses the REST dispatcher plus polling. |
| **Evidence** | `make api-test-static`; `src/lambda/chat_stream_processor/tests/test_route_identity.py`; `src/lambda/chat_stream_processor/tests/test_processor_session_owner.py`; UNVERIFIED: the citations have not been matched to each listed mitigation, and the ID-token verification has not been exercised against a live deployment by a test in this tree |

### AUTH.T16: Per-Object Authorization Is Opt-In Per Resolver (Default Deny Covers the Group Floor Only)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T16 |
| **Category** | STRIDE: Elevation of Privilege |
| **Description** | AUTH.T03 covers a resolver whose group check is *wrong*. This covers the structural reason that is easy to do: beyond the field-level group floor, nothing requires a check to exist. The dispatcher now **denies by default** — `http_api_dispatcher/authz.py` refuses any field with no entry in the generated `api_rbac_manifest.json`, so an operation whose groups were never declared is closed — but that floor decides only *which groups may call an operation*. Every narrower decision is still hand-written inside the resolver that does the work: the configuration-profile scope (`allowedConfigVersions`), the test-set scope (`allowedTestSets`), session and job ownership, review-queue eligibility and assignee-only writes (HITL.T02), and bulk-job requester checks (HITL.T05). A resolver that omits one of those is reachable by every caller the floor admits, and it fails *open*. The shape to watch for is several operations sharing one group policy, such as `[Admin, Reviewer, Annotator]`, where one of them is given the group list without the per-object half, so a scoped Annotator is confined by nothing but the floor. Eight operations are declared `ANY`, where the floor checks authentication only and the resolver is the whole check. Separately, whether a resolver's denial becomes an HTTP 403 still depends partly on **message text**: the dispatcher maps `PermissionError`/`AuthorizationError` by class name *or* a message beginning `Unauthorized`/`Forbidden` (`index.py`), so a reworded denial raised as a bare `Exception` becomes a 500. |
| **Attack Vector** | A caller holding a group the floor admits invokes `POST /op/{field}` naming an object outside their scope — another profile's document, another test set, another user's session, a queue they are not eligible for — on an operation whose resolver was shipped without that object's check. No UI path is involved. |
| **Impact** | A missing per-object check exposes, to every caller the operation's group list admits, the objects the scope or ownership rule was meant to confine; for an `ANY` operation, to every authenticated caller including one in no group. |
| **Likelihood** | Medium (the failure mode is omission during development, not an attack step, and it has recurred) |
| **Severity** | High |
| **Affected Components** | `http_api_dispatcher` (`index.py`, `authz.py`, `ddb_direct.py`), all resolver Lambdas, `scripts/api_rbac_expectations.yaml` |
| **Mitigations** | **Default deny at the dispatcher** (`authz.py`, from `api_rbac_manifest.json`, generated from `scripts/api_rbac_expectations.yaml` and failing closed when unreadable): an undeclared operation is refused with 403 and the message does not distinguish an unknown field from an undeclared one. **The manifest-plus-scan pair** declares the required groups for all 188 operations, and `make api-test-static` (both CI systems) fails when an operation drifts from the manifest or its `enforced_in` file carries no enforcement pattern — including, for an operation declared `scope_checked` or `scope_filtered`, no scope check in the code that operation's dispatch branch can reach. Check **S10** works in the other direction: an operation whose arguments name a configuration profile, a stored object or a session (decided by the words of the argument's name) must declare how that object is scoped — `scope_checked`/`scope_filtered`, `document_scope_checked`, `testset_checked` or `ownership` — and S10 then requires the matching entry point in the code it reaches; an op callable only by Admin or IAM needs none. MCP tools are held to the same table by checks M1–M5, which fail when a tool admits a role its equivalent REST operation does not. `make api-test` re-tests the matrix live with one user per group, and `object_scoped_roles` lets a cell assert a refusal that the group floor did **not** produce (`authzControl` ≠ `api-group-floor`), which is the only automated assertion that a per-object control exists rather than that the floor is right. Every 403 carries `errors[0].authzControl` (`api-group-floor`, `resource`, `resolver`, `unverified-identity`), so a per-object refusal is distinguishable in telemetry from a group refusal. An unexpected error answers a generic 500 `InternalError` with a request id and logs the detail server-side, so a reworded denial that falls through to 500 returns no internal text to the caller. |
| **Residual risk** | **Per-object checks are not enforced by construction.** The static scan verifies that an enforcement *pattern* exists in a named file — for scope, one reachable from the operation's dispatch branch; for a declared `ownership`, an owner check that can refuse — but it cannot verify that the check matches the right object; S10 decides which operations must declare a scope from argument names, so an operation that reads scoped objects without naming one in its arguments is outside it; and the scan has no rule at all for review-queue eligibility. The live harness asserts a per-object refusal only for operations carrying `object_scoped_roles`. The message-prefix arm of the 403 mapping remains, so a denial raised as a bare `Exception` with reworded text becomes a 500 (now generic, so the effect is a lost 403 in denial telemetry rather than a disclosure). The eight `ANY` operations each carry a reason bounded to that operation in the expectations file. |
| **Evidence** | `make api-test-static`; `make api-test`; UNVERIFIED: the citations have not been matched to each listed mitigation |

### AUTH.T15: Authentication Material in Resolver Logs (Name-Based Redaction Denylist)

| Attribute | Value |
|-----------|-------|
| **Threat ID** | AUTH.T15 |
| **Category** | STRIDE: Information Disclosure |
| **Description** | `idp_common.utils.log_sanitizer` holds the canonical denylist of key substrings whose values must never be written to logs (credential, token, key and password spellings). Resolvers log request payloads and DynamoDB items through it, so whether a credential-shaped value reaches CloudWatch Logs depends on that one list naming the key it arrives under. A resolver carrying its own hand-copied subset of that list can fall behind it — missing, say, the underscore and concatenated forms of access key, secret key and private key, `passwd` or the API-key header name — and log a payload carrying one of those keys with its value intact (issue #921). |
| **Attack Vector** | Not an attack step so much as an exposure: any operation whose logged payload contains one of the missed key spellings writes the value to its log group, where it persists for the retention period. |
| **Impact** | Credential or token material readable by principals granted CloudWatch Logs access, which is a broader and less-reviewed population than the operation's own group allow-list. |
| **Likelihood** | Medium |
| **Severity** | Medium |
| **Affected Components** | `lib/idp_common_pkg/idp_common/utils/log_sanitizer.py`, the byte-identical copies vendored into the resolver Lambdas that carry no `idp_common` layer, every resolver that logs its event, CloudWatch Logs |
| **Mitigations** | **One denylist.** Every resolver under `nested/api-resolvers/src/lambda/` calls the canonical `sanitize_event_for_logging`: those with an `idp_common` layer import it, and the eight without one carry a byte-identical copy synced by `scripts/sync_resolver_log_sanitizer.sh`. `scripts/tests/test_resolver_log_sanitizer.py` fails if a copy differs, if the effective denylist differs from the canonical one, or if any file in that tree defines a local key list again; the same rule now covers the logged invocation events under `src/lambda`. Adding a key to the canonical list therefore protects every resolver. **Log storage.** Log groups are retention-bounded, and every log group declared across `template.yaml`, `nested/api-resolvers/template.yaml` and `patterns/unified/template.yaml` except two sets `KmsKeyId` to the stack's customer-managed key, including `HttpApiDispatcherLogGroup`, the group every UI API request passes through. The two that do not are the `StacknameCheckFunction` and `ReadPreviousIDPPatternFunction` custom-resource groups, which handle no request data and carry a recorded `cfn_nag`/`checkov` suppression. `scripts/tests/test_log_group_encryption.py` fails if any log group in these templates drops `KmsKeyId` or `RetentionInDays`, including through an `Fn::If` branch that resolves to `AWS::NoValue`. The optional feature-platform, sample hook and scaffolding stacks declare further log groups without `KmsKeyId`, tracked in **issue #972**. |
| **Residual risk** | The denylist matches key **names**, not values: a token carried in free text, or under a key the list does not name, is logged as it is. Two custom-resource log groups use the default log-group encryption rather than the stack key. |
| **Evidence** | `scripts/sync_resolver_log_sanitizer.sh`; `scripts/tests/test_resolver_log_sanitizer.py`; `scripts/tests/test_log_group_encryption.py`; UNVERIFIED: the citations have not been matched to each listed mitigation |

## 4. Security Controls Summary

| Control | Implementation | Threats Mitigated |
|---------|---------------|-------------------|
| **IAM protection** | Restrict Cognito admin API access | AUTH.T01 |
| **Token management** | Short-lived tokens, secure storage | AUTH.T02, AUTH.T05, AUTH.T10 |
| **Dispatcher default-deny** | `authz.py` enforces a generated per-operation required-groups manifest before routing; a field with no entry is refused (403), so an undeclared operation is closed rather than open | AUTH.T03, AUTH.T08, AUTH.T16 |
| **An assigned group required for document content** | The 14 document reads, the chat transcript read, the processing-breaker badge, 3 mutations and the eight Inbox operations are declared `ANY_GROUP`, resolved at build time to every `AWS::Cognito::UserPoolGroup` the stack creates, so a self-registered user whose `cognito:groups` claim is empty is refused them. The document reads include the ones returning keys, URIs, attribute names and page descriptions rather than values, because those compose into one chain. Bounds what domain-scoped self-signup (AUTH.T04) reaches. It **is** the floor on the object bytes, since the three object-read operations are in this set and the browser's Identity Pool role grants no S3 — but it is not a per-document control (UI.T06). The chat Function URL applies the same floor on `/chat/document` from the verified ID token | AUTH.T03, AUTH.T04, AUTH.T14 |
| **Resolver auth** | Per-operation `cognito:groups` checks inside every resolver Lambda, on top of the dispatcher floor (the API Gateway authorizer only authenticates) | AUTH.T03, AUTH.T08, AUTH.T16 |
| **Dispatcher-side group check** | `ddb_direct._REQUIRED_GROUPS` enforces groups for the 11 in-process handlers before dispatch | AUTH.T16 |
| **Claim normalization** | `api_adapter._coerce_groups` turns the REST authorizer's comma-joined `cognito:groups` string back into a list before any check reads it | AUTH.T03, AUTH.T16 |
| **Identity provenance** | `api_adapter.normalize_event` builds `identity` from the authorizer's verified claims only; an invocation that asserts its own `identity` is refused (403) rather than believed, so the group list on the event cannot be chosen by the caller who sent it (issue #978). An explicitly null `identity` — the IAM-gated service-to-service marker, which asserts no groups — still passes through | AUTH.T03, AUTH.T16 |
| **Log redaction** | `idp_common.utils.log_sanitizer` denylist applied to logged payloads by every resolver, the layer-less ones through byte-identical copies a test holds to the canonical module (issue #921 closed) | AUTH.T15 |
| **Object-level authorization** | Owner-scoped keys on agent jobs and the session list; `ChatSessionOwnersTable` owner checks on every chat read and write, on both chat paths; requester checks on bulk review jobs | AUTH.T09 |
| **Central input-shape validation** | Dispatcher validates `arguments` against a schema-derived spec (`validation.py`); rejects unknown/missing/wrong-typed args with 400 | AUTH.T12 |
| **Config-version scope** | `allowedConfigVersions` enforced in scope-aware resolvers, resolved from the caller's immutable Cognito `sub` via a UsersTable `SUB#<sub>` pointer and, failing that, from the `email` claim via `EmailIndex`; every consumer holds `dynamodb:Query`/`GetItem` on the table and its indexes, and any failure to evaluate the scope denies. See AUTH.T07 for what the `sub` join does **not** yet cover | AUTH.T07 |
| **Caller-supplied ARN bounding** | `getStepFunctionExecution` requires the supplied `executionArn` to name this deployment's state machine before any Step Functions call, then applies the caller's config-version scope to the execution's `config_version` | AUTH.T07, AUTH.T09 |
| **Caller-supplied S3 key bounding** | `getFileContents` / `getFilePresignedUrl` / `getFilePresignedUrls` apply both per-user scope axes to the requested **key**, in the one function all three fields call before any S3 call: `allowedConfigVersions` against the profile in a `config_revisions/<profile>/` key, `allowedTestSets` against the test set in a `<test_set_id>/` key. Uses the canonical fail-closed lookups, so an unevaluable scope denies; a refusal names neither the object nor the scope subject. The two buckets it governs are also kept off the browser's Identity Pool role, so neither axis has a no-API path around it (`scripts/tests/test_browser_s3_grants.py`). On the Input/Output buckets the same choke point adds `document_scope`, matching the owning document's `ConfigVersion` against `allowedConfigVersions`; what remains open there is an **ownership** axis, which the product does not record, so the check narrows a scoped caller only — UI.T06, [#1033](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1033) | AUTH.T07, UI.T06 |
| **Enforcement at the worker, not only the front door** | `agent_chat_processor` applies the Agent Chat group check itself (Admin/Author/Viewer) before any agent call, on the same group claim the resolver forwards, so both entry paths evaluate one policy | AUTH.T14 |
| **Single caller-identity resolution** | The streaming routes take the caller only from the Cognito ID token they verify (`_verified_caller`, 401 without one) and read nothing identity-like from the body | AUTH.T14, AUTH.T09 |
| **IdP group-claim provenance** | Group assignment from `custom:idp_groups` requires the user to be federated through the configured provider (`identities` via `AdminGetUser`) and the trigger source to be a fresh sign-in; explicit `WriteAttributes` keeps non-mapped attributes read-only to end users | AUTH.T13 |
| **Automated authorization testing** | `make api-test-static` (static scan of op↔schema↔expectations drift + missing checks, **including Lambda Function URL routes — S6-S9**, scope declarations for scope-keyed arguments — S10, and MCP tools — M1-M5) and `make api-test` (live multi-role + scoped-user + token-negative + **IDOR + token-lifecycle + deleted-resource + input-validation + TLS** suites, with an auditable report); known gaps tracked as WARN so real regressions fail the gate | AUTH.T03, AUTH.T07, AUTH.T08, AUTH.T09, AUTH.T10, AUTH.T11, AUTH.T12, AUTH.T14 |
| **Transport security** | API Gateway/CloudFront TLS 1.2+ minimum, no cleartext HTTP | AUTH.T11 |
| **Cognito config** | Self-signup off unless `AllowedSignUpEmailDomain` is set (then domain-restricted), strong passwords, email verification | AUTH.T04 |
| **Schema-vs-code drift detection** | The `@aws_cognito_user_pools` directives in `schema.graphql` are **not** a runtime control — nothing enforces them. What *is* a control is `make api-test-static`: check **S2** asserts each directive's `cognito_groups` matches `scripts/api_rbac_expectations.yaml`, and check **S3** asserts the manifest's `enforced_in` source actually contains an enforcement pattern. A directive that disagrees with the code therefore fails the gate instead of quietly reading as protection | AUTH.T03, AUTH.T08 |
| **Audit logging** | CloudTrail for Cognito, CloudWatch for API Gateway + resolver Lambdas | All |
| **CSP headers** | Content Security Policy in both hosting modes (UI.T01, UI.T07) | AUTH.T02 |
| **Stack isolation** | Separate Cognito pools per deployment | AUTH.T06 |
