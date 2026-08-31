---
title: "AppSync → REST API Migration"
---

# Migrating off AWS AppSync: how the new transport mirrors GraphQL

This document is the reference for the UI ⇄ backend transport. It explains
**how the AppSync-free transport reproduces every capability the solution
previously got from AWS AppSync** — GraphQL queries, mutations, and
subscriptions, plus the security/RBAC model and the performance/scale
characteristics — and the deliberate design choices behind each mapping. Read it
for the component overview, when extending the API, or when auditing security
parity.

The web UI and backend communicate over an **API Gateway REST API**, with
**polling** for status updates and **Lambda response streaming** for chat. There
is **no toggle** and no AppSync footprint — the REST API is the only transport,
and the solution uses only long-lived, GovCloud/FedRAMP-eligible services.

## Why AppSync was removed

AWS AppSync is **not available in AWS GovCloud**, and is **not FedRAMP-compliant**. Regulated and GovCloud
deployments could not use it at all. Rather than maintain two transports, AppSync
was **removed entirely** and replaced with services that are available in every
partition. There is no toggle and no AppSync footprint remaining.

AppSync gave us four things the replacement has to preserve:

| AppSync capability | Replacement |
|---|---|
| GraphQL **queries** & **mutations** (request/response) | API Gateway **REST API** → single **dispatcher Lambda** at `POST /op/{field}` |
| GraphQL **subscriptions** (server push) | **UI polling** of DynamoDB-backed state (the source of truth) |
| Real-time **chat token streaming** (mutation → subscription fan-out) | **Lambda Function URL** with `RESPONSE_STREAM`, read directly by the browser |
| **Auth + RBAC** (`@aws_cognito_user_pools(cognito_groups)`) | Cognito **User Pools authorizer** (authN) + **per-resolver group checks** (authZ) |

The guiding principle: **keep the UI's call shape and the resolvers' event shape
unchanged**, so the change is a transport swap rather than an application rewrite.

## 1. Queries & mutations — the closest mapping

Under AppSync, the UI issued a GraphQL operation and AppSync routed the named
field to a resolver (a Lambda data source, or a VTL DynamoDB resolver).

The replacement keeps exactly that model:

```
UI  client.graphql({ query, variables })
      │   (thin REST client — same call shape as the Amplify client)
      ▼
API Gateway REST API   POST /op/{field}      ← Cognito User Pools authorizer
      ▼
Dispatcher Lambda (idp_common.api_adapter)
      ├── Lambda-backed field → invoke the SAME resolver Lambda AppSync used
      └── DynamoDB-direct field → serve in-process (ports the old VTL resolver)
```

- **Same UI code.** `src/ui/src/api/rest-client.ts` exposes the same
  `client.graphql({ query, variables })` surface the Amplify/AppSync client had,
  so call sites are unchanged. It parses the operation's field name from the
  query string and `POST`s to `${VITE_API_BASE_URL}/op/<field>`. Amplify remains
  only for Cognito token retrieval.
- **Same resolvers.** The dispatcher normalizes the API Gateway request into the
  AppSync resolver **event shape** (`{ arguments, identity, info: { fieldName } }`)
  via `idp_common.api_adapter`, then invokes the *same* resolver Lambda AppSync
  used. Resolvers did not have to be rewritten.
- **VTL resolvers ported to Python.** The handful of fields AppSync served with
  **VTL DynamoDB resolvers** (no Lambda) — `getDocument`, the date-sharded
  document lists, and discovery/agent job reads — are reimplemented in-process in
  the dispatcher's `ddb_direct` module, a faithful port of the VTL against the
  same tables (same keys, same user-scoping).
- **Argument mapping.** GraphQL exposes `$ctx.arguments` keyed by **argument
  name**, which is not always the caller's **variable name**
  (e.g. `getDocument(ObjectKey: $objectKey)`). The REST client remaps
  variable → argument names before sending so resolvers receive the keys they
  expect — reproducing AppSync's `$ctx.arguments` semantics.

### Why a REST API (v1), not an HTTP API (v2)?

Only the **REST API** supports a **PRIVATE endpoint type** and an **AWS WAFv2
WebACL** on its stage — both required for regulated/GovCloud deployments. HTTP
APIs support neither. See [§6 Private endpoint & WAF](#6-private-endpoint--waf).

### CORS

The UI (CloudFront origin) and API are different origins, so the REST API adds
what AppSync handled implicitly: an unauthenticated `OPTIONS` preflight method on
`/op/{field}`, `GatewayResponses` that put `Access-Control-Allow-Origin` on
error responses (so the authorizer's 401/403 still carry CORS headers), and the
dispatcher adds the header to its own responses.

## 2. Subscriptions → polling (server push over a pull model)

AppSync subscriptions delivered server-pushed updates for: document
create/update, discovery job status, agent job completion, and circuit-breaker
status. Each was fed by a mutation the backend published to.

**Key realization:** those subscriptions existed to notify the UI that
**DynamoDB state had changed** — and DynamoDB is the source of truth. So the
replacement drops the push channel and has the UI **poll the same state
directly**:

- The backend workers write the **TrackingTable directly**
  (`DocumentDynamoDBService`; the document-service factory is DynamoDB-only) —
  they no longer publish a mutation to trigger a subscription.
- The UI polls on sensible intervals and **pauses polling while the browser tab
  is hidden** (see `src/ui/src/hooks/use-polling.ts`) to limit cost:

  | Former subscription | Polled by | Interval |
  |---|---|---|
  | document list changes | `use-graphql-api` document list | ~5 s |
  | open document updates | document detail | ~4 s until terminal status |
  | discovery / agent job status | their panels | per-panel interval |
  | circuit-breaker status | `use-circuit-breaker` | ~15 s |

- The UI reuses its existing dedup/merge logic, so already-loaded detail is
  preserved across polls and the user experience matches the old
  subscription-driven refresh.

**Trade-off, stated honestly:** polling adds up to one interval of latency versus
an instant push, and issues periodic reads. This is acceptable because IDP is a
document-processing console (multi-second/minute workflows), not a chat-latency
UI, and tab-hidden pausing bounds the cost. The one place sub-second streaming
*does* matter — chat — keeps true streaming via a different mechanism (§3).

## 3. Chat streaming → Lambda response streaming

The two chat flows (chat-with-document, agent help chat) previously used an
AppSync **mutation → subscription fan-out**: the processor published each token
delta as a mutation, and the UI received them over a subscription. This is the
one flow where AppSync's real-time push was load-bearing.

The replacement is a dedicated **Lambda Function URL** with
`InvokeMode=RESPONSE_STREAM` (via the AWS Lambda Web Adapter), addressed
**directly by the browser**, which reads the Server-Sent-Events body
incrementally:

- **Auth:** the Function URL is `AuthType=AWS_IAM`; the browser SigV4-signs the
  request with the authenticated **Cognito Identity Pool** credentials, enforced
  by AWS at the function edge (no token-verification code to maintain). The
  authenticated Cognito role is granted `lambda:InvokeFunction` on the streaming
  function (a Function URL invocation requires `InvokeFunction`, *not*
  `InvokeFunctionUrl`).
- **Same event payloads.** The chat processors emit the **same event objects**
  they previously published as mutations; the UI consumes them through the same
  message-handling code, only sourced from the stream instead of a subscription —
  so the chat UX is unchanged.
- **Hosting-agnostic:** because the browser hits the Function URL directly, token
  streaming works identically under CloudFront or the API Gateway hosting option
  (API Gateway buffers responses and can't stream, which is exactly why the
  streaming endpoint is a direct Function URL). The UI's CSP `connect-src`
  allows `https://*.on.aws` so the browser can reach `*.lambda-url.<region>.on.aws`.

## 4. Security & RBAC parity

This is the most important equivalence to get right, because the two transports
enforce authorization at **different layers**.

**Under AppSync**, the schema directive
`@aws_cognito_user_pools(cognito_groups: ["Admin", "Author", ...])` gated a field
**before** the resolver ran — a Viewer's request to an Admin-only field was
rejected by AppSync itself.

**Under the REST API**, the Cognito User Pools authorizer only
**authenticates** (is this a valid signed-in user?). It does **not** read the
schema's group directives. Therefore **authorization (RBAC) must be re-enforced
inside each resolver** (and inside `ddb_direct`). The migration preserves parity
as follows:

- **`cognito:groups` is restored to a list.** The API Gateway authorizer
  flattens the groups claim (e.g. the string `"[Admin Author]"` or a
  comma-joined form) where AppSync delivered a JSON list. `api_adapter`
  re-normalizes it to a list in `identity.claims['cognito:groups']`, so every
  resolver's group check sees the same shape it did under AppSync.
- **Each resolver checks its own groups.** Every UI-facing resolver (and each
  RBAC-gated op in `ddb_direct`) intersects the caller's groups with the set the
  AppSync schema required, and raises `PermissionError` if the caller isn't a
  member — defense-in-depth that mirrors the directive.
- **The dispatcher maps errors to HTTP status the way AppSync did.** A resolver
  `PermissionError` becomes **403** with `errorType: "Unauthorized"` (which the
  UI keys on); `ValueError`/`KeyError` become **400 BadRequest**. Unauthenticated
  requests are rejected with **401** by the authorizer before reaching any code.
- **IAM-only operations stay backend-only.** Fields that were IAM-authorized in
  AppSync (backend writers such as `updateAgentJobStatus`,
  `updateDiscoveryJobStatus`) are **rejected for all Cognito callers** — and the
  backend workers now write DynamoDB directly rather than calling the API at all.
- **Ownership scoping is preserved.** User-scoped data (agent jobs, chat
  sessions) is still partitioned by the caller's identity (`PK = agent#<user>`,
  session ownership checks), so a user only sees their own records — exactly as
  the VTL/AppSync resolvers enforced.
- **Streaming auth** is enforced by IAM at the Function URL edge (§3), and the
  **Feature Platform** install-hook fields use direct `lambda:InvokeFunction`
  with IAM instead of the old AppSync SigV4 mutation (see
  [§5 Feature Platform](#5-feature-platform)).

### Input-shape validation parity

AppSync also validated every request's **input shape** against the schema for
free — it rejected an unknown argument, a missing non-null argument, a wrong
scalar type (e.g. an object where a `String!` was expected), or an out-of-set
enum value **before** the resolver ran. The REST API's Cognito authorizer does
none of this, so without a replacement a malformed request flowed straight into
a resolver (silently accepted, or a 500 deep in the code). That is a
defense-in-depth regression — the type-confusion / unexpected-argument class,
distinct from the authorization parity above.

This boundary gate is re-established by **central argument validation in the
dispatcher** — intentionally **stricter than AppSync on input coercion** (see the
last bullet), not a byte-for-byte re-creation of it:

- **A build-time spec, not a runtime GraphQL parser.**
  [`scripts/sdlc/generate_api_validation_spec.py`](../scripts/sdlc/generate_api_validation_spec.py)
  parses `schema.graphql` (reusing `scan_api_rbac.py`'s parser) into a compact
  `api_validation_spec.json` — each field's argument signature, the enum value
  sets, and input-object shapes. The JSON is committed next to the dispatcher (in
  its CodeUri, so SAM bundles it) and the Lambda validates against it with
  **stdlib only** (no `graphql-core` dependency).
- **What it rejects (HTTP 400 `BadRequest`).** Unknown arguments, missing
  non-null arguments, wrong JSON type (string/int/float/bool/list), and
  out-of-set enum values. Rejection raises `ValueError`, which the dispatcher
  already maps to 400 — the same status AppSync returned for a malformed query.
- **Conservative by design.** Type-only checks (no date/email/URL *format* regex
  yet); input objects are validated shallowly (must be an object). The validator
  **fails open on its own internal errors** (a validator bug never 500s the API)
  and fails closed only on genuine input violations.
- **Stricter than AppSync on coercion (safe for the UI).** AppSync *coerced* some
  inputs before validating; this validator *rejects* them instead: a scalar
  passed for a list arg (AppSync → one-element list), an integer passed for an
  `ID` (AppSync → string), and a bare-scalar `AWSJSON` value (AppSync accepts any
  JSON). The current Web UI never sends those shapes — it sends list args as
  arrays, IDs as strings, and `AWSJSON` as `JSON.stringify` objects (verified
  across all UI operations) — so nothing breaks, but a non-UI client relying on
  AppSync-style coercion would get a 400. Coercing (rather than rejecting) these
  is a documented follow-up (see the `TODO`s in `validation.py`).
- **Drift-guarded.** A unit test (and `generate_api_validation_spec.py --check`)
  fails CI if the committed spec falls out of sync with `schema.graphql`, so the
  spec can't silently rot as operations are added. (The generator strips GraphQL
  `"""` descriptions and `#` comments before parsing, so prose containing a
  `word: Type` colon can't be misread as a field or argument.)

See [`validation.py`](../nested/api-resolvers/src/lambda/http_api_dispatcher/validation.py).

The authoritative baseline for BOTH authorization and input shape remains
`nested/api-resolvers/src/api/schema.graphql` (its `Query`/`Mutation` group
directives and argument signatures). A live verification harness,
[`scripts/test_api_rbac.py`](../scripts/README.md), drives the deployed API as
each Cognito group (Admin/Author/Viewer/Reviewer) plus unauthenticated and
asserts every operation's authorization outcome against that baseline — plus
mandatory security-focused suites (IDOR, token lifecycle, deleted-resource,
input validation, TLS) — run it after any change to a resolver, the dispatcher,
`ddb_direct`, or the REST client.

## 5. Feature Platform

The optional Feature Platform (`EnableFeaturePlatform=true`) is also AppSync-free:

- its **6 UI-facing fields** (list catalog/installed, check entitlement, launch
  URL, subscribe/unsubscribe) route through the same REST API dispatcher — their
  resolver function ARNs flow from the FeaturePlatform nested stack into the
  dispatcher's field→function map;
- its **6 install-hook fields** (register/unregister feature & hooks,
  apply/remove config preset) are invoked **directly** (`lambda:InvokeFunction`)
  by feature stacks at install time, using function ARNs the host re-exports —
  replacing the old AppSync SigV4 mutation.

> **Upgrading a stack that already has installed feature stacks:** installed
> feature stacks used to import the host's `AppSyncApiArn`/`AppSyncApiUrl`
> exports, which this release removes. CloudFormation forbids removing an in-use
> export, so the one-time upgrade sequence is **delete the feature stacks →
> update the host stack → reinstall the features** from a build updated for this
> release. After that the coupling is gone (features use direct Lambda invoke).
> External/marketplace features must be rebuilt against the new host exports: a
> feature's ui-deployer calls `registerFeature` / `registerFeatureHooks` /
> `applyFeatureConfigPreset` (and their un/remove counterparts) by direct
> `lambda:InvokeFunction` on the host resolver ARNs exported as
> `<MainStackName>-<Field>FunctionArn`, with the unchanged AppSync event payload
> `{info:{fieldName}, arguments, identity}`.

## 6. Private endpoint & WAF

The REST API can be locked down for regulated deployments — capabilities AppSync
could not offer in GovCloud/FedRAMP partitions:

- **`ApiGatewayVisibility=PRIVATE`** makes the REST API a **PRIVATE** endpoint
  reachable only from within the VPC via an interface VPC endpoint
  (`ApiGatewayVpcEndpointId`) + a resource policy — like the existing headless
  Jobs API. The resolver/dispatcher Lambdas then run inside the VPC. The default
  is `GLOBAL` (public, Cognito-authorized).
- **WAFv2 WebACL:** setting `WAFAllowedIPv4Ranges` to anything other than
  `0.0.0.0/0` attaches a REGIONAL WAFv2 WebACL to the API stage that blocks
  non-listed source IPs. (The WebACL is `REGIONAL` and lives in the stack's
  region — API Gateway is a regional resource, so unlike CloudFront it does *not*
  require a us-east-1 WebACL.)

## 7. Performance & scale

The replacement preserves — and in places simplifies — the scaling story:

- **Request/response (queries & mutations):** API Gateway and the dispatcher
  Lambda both scale horizontally and on demand, the same elastic model AppSync
  had. The dispatcher adds at most one synchronous Lambda hop in front of the
  existing resolver — the resolvers themselves (and their DynamoDB access
  patterns) are unchanged, so per-operation latency and throughput are
  essentially the same. The DynamoDB-direct fields are actually **faster** than
  before: they run in-process in the dispatcher with **no** second Lambda hop
  (VTL had none either, so this matches AppSync's VTL path).
- **Field → resolver map:** the dispatcher loads its `field → resolver ARN` map
  (~60 entries, too large for a Lambda env var) once at cold start from an SSM
  Parameter (`/${StackName}/http-api/field-function-map`), so steady-state
  dispatch is a dictionary lookup.
- **Status updates (polling):** polling trades a small amount of latency for
  eliminating the AppSync subscription infrastructure. Cost/scale is bounded by
  fixed intervals, tab-hidden pausing, and DynamoDB's per-request scaling; the
  polled reads are the same key/GSI queries the app already ran. There are no
  long-lived WebSocket connections to manage or scale.
- **Chat streaming:** Lambda response streaming scales per-invocation like any
  Lambda and streams tokens with latency comparable to the old
  mutation→subscription path, without the fan-out overhead of publishing every
  token as a mutation.
- **Private endpoint & WAF:** because it's a REST API, regulated deployments can
  set `ApiGatewayVisibility=PRIVATE` and attach a WAFv2 WebACL — scale and
  isolation controls AppSync could not offer in these partitions (see §6).

## Deploying

No special parameter is needed — the REST API is built unconditionally:

```bash
idp-cli deploy \
  --stack-name my-idp-stack \
  --template-url <published idp-main.yaml> \
  --region <region> \
  --wait
```

The Lambda Web Adapter layer (for chat streaming) ARN is exposed as the
`LambdaWebAdapterLayerArn` parameter (leave blank to use the region-default LWA
x86_64 layer, or set it to pin a version).

> **GovCloud / China:** LWA is published **only in the commercial partition**, and
> AWS account IDs do not exist across partitions — so there is no other-partition
> publisher account to override this parameter with. The `--govcloud` transform
> removes the streaming function and this parameter outright, and the UI falls
> back to its polling chat transport. See
> [GovCloud Deployment](./govcloud-deployment.md#keeping-the-web-ui-in-govcloud---govcloud).

## Summary

| Dimension | AppSync | AppSync-free replacement | Parity |
|---|---|---|---|
| Queries / mutations | GraphQL field → resolver | `POST /op/{field}` → dispatcher → same resolver | Same resolvers, same UI call shape |
| VTL DynamoDB resolvers | AppSync VTL | `ddb_direct` in-process port | Same behavior, no extra hop |
| Subscriptions | Server push | UI polling of DynamoDB (source of truth) | Same data; +≤1 interval latency |
| Chat streaming | mutation → subscription | Lambda Function URL `RESPONSE_STREAM` | Same events, true streaming |
| AuthN | Cognito (AppSync) | Cognito User Pools authorizer | Equivalent |
| AuthZ / RBAC | schema `cognito_groups` directive | per-resolver group check (+ `ddb_direct`) | Enforced; verified by `test_api_rbac.py` |
| Input-shape validation | GraphQL schema (types/non-null/enums) | dispatcher validates against a schema-derived spec (`validation.py`) | Restored centrally; drift-guarded |
| Availability | not GovCloud / FedRAMP | GovCloud/FedRAMP-eligible services only | **Improved** |
| Private + WAF | not supported | REST API PRIVATE + WAFv2 | **Improved** |

## See also

- [API RBAC test script](../scripts/README.md) (`scripts/test_api_rbac.py`)
- [Architecture](architecture.md)
- [GovCloud deployment](govcloud-deployment.md)
- [API Gateway hosting](apigateway-hosting.md)
