# Data Flows

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.2 |
| **Last Updated** | 2026-09-17 |
| **Applies to release** | v0.6.9 |
| **Classification** | Internal |

> **v3.0 update.** All UI flows were rewritten: AppSync GraphQL + subscriptions
> are replaced by the API Gateway REST dispatcher (`POST /op/{field}`) with
> polling for status, and a Lambda Function URL for chat streaming. The A2I
> human-review flow is replaced by the built-in review portal. New flows added
> for the preprocessing/PII hook, the Feature Platform UI bundle, and the
> Jobs API.

> **v3.2 refresh (v0.6.9).** Re-verified against `template.yaml`,
> `patterns/unified/statemachine/workflow.asl.json`, the queue processor and the
> chat stream processor. Corrections: SQS is encrypted with the stack's KMS CMK,
> not SSE-SQS (§2.1); the admission-control mechanism is now described rather
> than summarised as "a counter" (§2.1); `onError: fail` is terminal at the
> **preprocessing** hook point only, not at every hook point (§2.1a, §8);
> the Identity Pool role is shared by **five** groups, not four (§4.1, §5.1); and
> the dispatcher's lack of a default deny is called out where the request path is
> described (§3.1). In-flight fixes are cited by issue number and marked
> **pending**; nothing marked pending is a control today.

## 1. Overview

This document describes the primary data flows through the GenAI IDP Accelerator, identifying where data crosses trust boundaries, undergoes transformation, and is persisted. Each flow is analyzed for security-relevant characteristics.

## 2. Document Processing Flow (Core Pipeline)

### 2.1 Document Ingestion

```mermaid
sequenceDiagram
    participant User as User / System
    participant S3 as S3 Input Bucket
    participant EB as EventBridge
    participant QS as Queue Sender Lambda
    participant SQS as SQS Queue
    participant QP as Queue Processor Lambda
    participant DDB as DynamoDB
    participant SFN as Step Functions

    User->>S3: Upload document (S3 PutObject / presigned URL)
    S3->>EB: Object created event
    EB->>QS: Trigger Lambda
    QS->>SQS: Enqueue document reference
    SQS->>QP: Dequeue message
    QP->>DDB: Conditional counter increment (admission control)
    QP->>DDB: Create document tracking record
    QP->>SFN: Start execution (deterministic name, document reference, config)
```

**Data in transit**: Document bytes (S3 upload), S3 object key references (SQS messages), configuration JSON (Step Functions input).

**Trust boundary crossings**:
- TB1→TB2: User uploads document over HTTPS
- TB3 internal: S3 → EventBridge → Lambda → SQS → Lambda → Step Functions

**Security controls**:
- S3 bucket policy restricts upload access; a bucket policy denies any request where `aws:SecureTransport` is false
- SQS encryption at rest with the stack's **KMS customer-managed key** (`KmsMasterKeyId: CustomerManagedEncryptionKey` on every queue and DLQ) — not SSE-SQS
- Step Functions input validated by Queue Processor Lambda
- **Admission control** bounds concurrent processing (below)

**Admission control, in detail.** This is the control that prevents an upload
burst from becoming unbounded Step Functions and Bedrock spend, so it is worth
stating precisely rather than as "a counter". A slot is taken with a DynamoDB
`ADD active_count :inc` under a `ConditionExpression` of `active_count < :max`,
so admission is decided atomically and a conditional-check failure — not a
comparison in Lambda memory — is what rejects work over the limit. Around that
core: `reconcile_counter` corrects drift against the real number of running
executions under its own conditional write; drift is emitted as a CloudWatch
metric, so a leaked slot becomes visible rather than silently eroding capacity;
`check_circuit_breaker` can stop admission entirely; SQS visibility is extended
during a downstream outage rather than letting messages expire; and
`execution_name_for` derives the Step Functions execution name deterministically
from the input key and message id, so a redelivered SQS message cannot start a
second execution for the same document. The failure mode to model here is a
**leaked slot** (capacity lost until reconciliation) rather than over-admission.
See SDK.T04.

### 2.1a Preprocessing Hook (runs first, both modes)

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant Disp as Pipeline Hooks Dispatcher
    participant Hook as Preprocessing Hook Lambda
    participant Bedrock as Amazon Bedrock
    participant S3In as S3 Input Bucket

    SFN->>Disp: Preprocessing step (before BDA/pipeline routing)
    Disp->>Hook: Invoke with source document reference
    Note over Hook: Document status shows PREPROCESSING
    Hook->>S3In: Read source document
    Hook->>Bedrock: (PII feature) detect PII in document
    Bedrock-->>Hook: PII spans
    Hook->>S3In: Write redacted copy "(REDACTED)" + config-version tag
    Hook-->>Disp: {halt: true|false}
    alt halt = true
        Disp-->>SFN: End execution (replacement document re-enters pipeline)
    else halt = false
        Disp-->>SFN: Continue to routing
    end
```

**Security-relevant characteristics**:
- The hook runs **before** any classification/extraction model sees the document, which is the point of the PII feature — but the **detection call itself sends the un-redacted document to Bedrock** (TB3→TB4). This is an accepted, documented residual exposure (see PII.T01).
- Writing the redacted copy back to the *input* bucket creates a re-entrancy risk; a marker + guard prevents redaction loops (PII.T02).
- `onError: fail` is terminal **here**: a failed preprocessing hook stops the execution rather than falling through to processing the un-redacted original (a fail-**closed** design; see PII.T03). This is specific to the `preprocessing` hook point. At the other six hook points the state machine's `Catch` block routes *forward* to the next step regardless of `onError`, so `onError: fail` does not halt the workflow there — see HOOK.T07 and §8, fix **pending in issue #919**.

**Trust boundary crossings**: TB3→TB6 (hook is customer/feature-managed), TB3→TB4 (PII detection call).

### 2.2 Pipeline Mode Processing

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant OCR as OCR Lambda
    participant Textract as Amazon Textract
    participant Class as Classification Lambda
    participant Extract as Extraction Lambda
    participant Bedrock as Bedrock (Anthropic/Nova)
    participant Mantle as Bedrock Mantle (OpenAI GPT-5.x)
    participant S3 as S3 Output Bucket

    SFN->>OCR: Invoke with document reference
    OCR->>Textract: DetectDocumentText / AnalyzeDocument (TABLES+LAYOUT)
    Textract-->>OCR: OCR results (text, layout, tables, confidence, geometry)
    OCR->>S3: Store OCR output + pageData.json

    SFN->>Class: Invoke with OCR text
    Class->>Bedrock: Prompt with document text + class definitions
    Bedrock-->>Class: Classification result
    Class->>S3: Store classification output

    SFN->>Extract: Invoke with OCR text + classification
    alt model is OpenAI GPT-5.x
        Extract->>Mantle: Responses API prompt (document text)
        Mantle-->>Extract: Structured data
    else Anthropic / Nova
        Extract->>Bedrock: Converse prompt with extraction schema
        Bedrock-->>Extract: Structured data + per-field confidence (integrated)
    end
    Extract->>Extract: Ground geometry against pageData.json (ocr_only)
    Extract->>S3: Store extraction output + explainability_info
```

**Data transformation**: Raw document → OCR text/layout → classified document type → structured JSON extraction with per-field confidence and bounding boxes.

> **v0.6 change.** Confidence and bounding-box geometry are now **outputs of
> extraction** (`confidence.mode: integrated`), not a separate Assessment
> stage; the standalone Assessment step auto-skips. The former `Assess`
> participant is retained only for `confidence.mode: separate`.

**Sensitive data exposure**: Full document text is sent to Textract and to the configured model endpoint. Where the configured model is an OpenAI GPT-5.x variant, that text goes to the `bedrock-mantle` OpenAI Responses API — a **different model family within TB4** than the Anthropic/Nova default. Deployments with data-residency or model-vendor constraints must account for this (see PM.T08). Extracted PII/sensitive data flows through Lambda memory and is written to S3.

**Trust boundary crossings**:
- TB3→TB4: Lambda sends document text to Textract and Bedrock (Anthropic/Nova **or** OpenAI via mantle)

### 2.3 BDA Mode Processing

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant BDALambda as BDA Lambda
    participant BDA as Bedrock Data Automation
    participant S3 as S3 Output Bucket

    SFN->>BDALambda: Invoke with document reference
    BDALambda->>BDA: Submit document for processing (S3 URI)
    BDA-->>BDALambda: Processing results (classification + extraction)
    BDALambda->>BDALambda: Map BDA output → standard format
    BDALambda->>S3: Store normalized output
```

**Data transformation**: Raw document → BDA-processed results → normalized to standard pipeline output format.

**Trust boundary crossings**:
- TB3→TB4: Lambda provides S3 URI to BDA service; BDA reads document directly from S3

### 2.4 Shared Processing Tail

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant HITL as HITL Check Lambda
    participant DDB as DynamoDB
    participant Reviewer as Reviewer (Web UI portal)
    participant RV as Rule Validation Lambda
    participant Bedrock as Amazon Bedrock
    participant PRVHook as Post-Rule-Validation Hook
    participant Sum as Summarization Lambda
    participant Eval as Evaluation Lambda
    participant S3 as S3 Output Bucket
    participant Report as Reporting Lambda

    SFN->>HITL: Check if any section is below confidence threshold
    alt HITL Enabled and threshold breached
        HITL->>DDB: Mark sections PENDING_REVIEW; end execution
        Note over Reviewer: Decoupled — review happens out-of-band
        Reviewer->>DDB: Claim section, save corrections (completeSectionReview)
        Reviewer->>SFN: Final section resolved -> trigger_reprocessing
    end

    SFN->>RV: Validate extracted data against rules
    RV->>Bedrock: Optional AI-assisted rule evaluation
    RV-->>SFN: Validation results
    SFN->>PRVHook: Dispatch postRuleValidation hook (incl. skip paths)

    SFN->>Sum: Generate document summary
    Sum->>Bedrock: Summarization prompt
    Sum-->>SFN: Summary text

    SFN->>Eval: Compare results to ground truth (if available)
    Eval->>S3: Store evaluation report

    SFN->>Report: Save metering/reporting data
    Report->>S3: Write Parquet file to reporting bucket
    Report->>DDB: Update document record; snapshot immutable run version
```

> **v0.6 change — HITL no longer uses Amazon A2I or SageMaker.** Human review
> is a **built-in portal in the Web UI**, backed by the REST API and DynamoDB
> section-review tracking. There is no A2I `FlowDefinition`, no SageMaker
> workteam, and no external review portal URL. Review is **decoupled** from the
> workflow: per-section actions update tracking directly, and resolving the last
> pending section triggers downstream reprocessing. Authorization for review
> actions is the Admin/Reviewer group set enforced in the resolver Lambdas
> (`completeSectionReview`, `skipSectionReview`), not an A2I task assignment.

### 2.5 Document Version Snapshot

```mermaid
sequenceDiagram
    participant Report as Reporting / Finalize Lambda
    participant S3 as S3 Output Bucket (versioned)
    participant DDB as DynamoDB Document Table

    Report->>S3: Read final result objects
    Report->>DDB: create_document_run: manifest of pinned S3 versionIds
    Note over DDB: Immutable run record (Pages, FileCount,<br/>ConfidenceThresholdAlerts, ProcessingIssues)
    Report->>DDB: Retain for DataRetentionInDays
```

**Security-relevant characteristics**: Re-processing no longer overwrites prior
results — each run is retained as an immutable snapshot addressed by pinned S3
object versions. This **increases data retention surface**: deleted-then-
reprocessed content remains readable through version history until retention
expires. `deleteDocumentVersion` is Admin-only; `listDocumentVersions` is
readable by any authenticated user. Version bytes are fetched via
`getFilePresignedUrl` with an explicit `versionId` — see UI.T02/UI.T06 for the
key-scoping gap that applies to those reads.

## 3. Web UI Data Flows

### 3.1 Authentication + API Request Flow

```mermaid
sequenceDiagram
    participant Browser as Browser
    participant Host as CloudFront OR API Gateway S3 proxy
    participant Cognito as Cognito User Pool
    participant WAF as WAFv2 (optional)
    participant APIGW as API Gateway REST
    participant Disp as HTTP API Dispatcher Lambda
    participant Resolver as Resolver Lambda

    Browser->>Host: Load SPA (React app)
    Host-->>Browser: Static assets from S3 UI bucket
    Browser->>Cognito: Authenticate (username/password or SSO)
    Cognito-->>Browser: JWT tokens (ID, Access, Refresh)

    Browser->>WAF: POST /op/{field} + Bearer JWT
    WAF->>APIGW: (allow-listed IP only; default Block)
    APIGW->>APIGW: Cognito authorizer: AUTHENTICATE only (401 if invalid)
    APIGW->>Disp: Normalized event {field, arguments, identity}
    Disp->>Disp: Validate arguments vs schema-derived spec (400 on bad shape)
    Disp->>Resolver: Invoke mapped resolver
    Resolver->>Resolver: Check cognito:groups -> PermissionError = 403
    Resolver->>Resolver: Check allowedConfigVersions scope -> in-band 200 deny
    Resolver-->>Browser: Result
```

**Critical control note**: the Cognito authorizer performs **no group
evaluation** — it only proves the token is valid. It could not do more: there is
a single authorized route (`POST /op/{field}`), so there is nothing
per-operation for a gateway-level policy to attach to. Every group and scope
decision therefore lives in the code behind the dispatcher. A resolver that
omits its check ships an operation that *looks* protected in `schema.graphql`
but is open to any authenticated user (AUTH.T08). This is why the automated
authorization harness (`make api-test` / `make api-test-static`) is a **primary**
control rather than a nice-to-have.

**The dispatcher does not default-deny.** It resolves a field if it can map it,
whether or not the target enforces anything; `ddb_direct._REQUIRED_GROUPS` is the
only group check that happens at dispatcher level, and it returns without denying
for a field that has no entry. Its 403 mapping also keys partly on error-message
prefixes, so a reworded exception can change an HTTP status. A default-deny gate
and the removal of that prefix dependency are **pending in issue #928**; until
then the manifest plus `make api-test-static` is what stands in for a default
deny. See AUTH.T16.

Two mechanical details are load-bearing when reading resolver code: the REST
authorizer places claims at `requestContext.authorizer.claims` and flattens
`cognito:groups` into a comma-joined string, which
`idp_common.api_adapter._coerce_groups` restores to a list before any group
comparison; and the REST API's CloudFormation logical id is `HttpApi` although
its type is `AWS::ApiGateway::RestApi`.

**Trust boundary crossings**: TB1→TB2 (browser to CloudFront/Cognito), TB2→TB3 (JWT to API Gateway → dispatcher → resolvers).

### 3.2 Status Update Flow (polling — replaces subscriptions)

```mermaid
sequenceDiagram
    participant Browser as Browser
    participant APIGW as API Gateway REST
    participant Resolver as Resolver Lambda
    participant DDB as DynamoDB

    loop Polling interval while documents in flight
        Browser->>APIGW: POST /op/listDocuments (+ JWT)
        APIGW->>Resolver: Dispatch
        Resolver->>DDB: Query tracking table
        DDB-->>Resolver: Current statuses
        Resolver-->>Browser: Document list + statuses
    end
```

**Security note**: AppSync real-time subscriptions are gone. Status freshness is
now bounded by the poll interval, and each poll is a fully re-authorized
request — removing the class of threat where a long-lived subscription outlives
the authorization decision that established it. The trade-off is request volume;
throttling is handled by API Gateway stage limits and Lambda concurrency.

### 3.3 Configuration Management Flow

```mermaid
sequenceDiagram
    participant Browser as Browser
    participant APIGW as API Gateway REST
    participant Disp as Dispatcher
    participant Lambda as Config Resolver Lambda
    participant Users as DynamoDB Users Table
    participant S3 as S3 Config Bucket
    participant DDB as DynamoDB Config Table

    Browser->>APIGW: POST /op/updateConfiguration (+ JWT)
    APIGW->>Disp: Authenticated event
    Disp->>Lambda: Dispatch
    Lambda->>Lambda: Enforce Admin/Author group
    Lambda->>Users: Query allowedConfigVersions for caller
    Note over Lambda,Users: Resolver role MUST have UsersTable<br/>Query/GetItem or the request is DENIED (AUTH.T07)
    Lambda->>Lambda: Validate config schema + migrate v0.5->v0.6
    Lambda->>S3: Write config YAML
    Lambda->>DDB: Update config version record
    Lambda-->>Browser: Confirmation
```

**Security note**: Configuration includes model IDs, prompts, extraction schemas, and processing parameters. Malicious configuration could influence all subsequent document processing (PM.T06). On this flow the config-version scope lookup **fails closed**: a missing IAM grant, an unwired UsersTable or a claims set carrying no `email` all refuse the request rather than reading as "this caller has no restriction". An *empty page* — no UsersTable row for the caller — still means unrestricted, which is the opt-in-scoping default. One shared implementation is used by every REST consumer, with `scripts/tests/test_scope_lookup_fail_closed.py` gating key provenance and failure handling across the class; AUTH.T07 records what that gate does not cover, including the one module it discovers and suppresses.

### 3.4 Document Upload Flow (UI)

```mermaid
sequenceDiagram
    participant Browser as Browser
    participant APIGW as API Gateway REST
    participant Lambda as Upload Resolver Lambda
    participant S3 as S3 Input Bucket

    Browser->>APIGW: POST /op/uploadDocument (+ JWT)
    APIGW->>Lambda: Dispatch
    Lambda->>Lambda: Enforce Admin/Author group
    Lambda->>S3: Generate presigned PUT URL (short expiry)
    Lambda-->>Browser: Presigned URL
    Browser->>S3: Direct upload via presigned URL (HTTPS)
```

### 3.5 Web UI Hosting Modes

```mermaid
flowchart LR
    subgraph Mode1[WebUIHosting=CloudFront default]
        B1[Browser] --> CF[CloudFront + OAC] --> S3A[S3 UI Bucket]
        CF -.ResponseHeadersPolicy.-> B1
    end
    subgraph Mode2[WebUIHosting=APIGateway]
        B2[Browser] --> AG["API Gateway GET / and /proxy+<br/>AuthorizationType: NONE"] --> S3B[S3 UI Bucket]
        AG -.security headers set per-method.-> B2
    end
```

**Security note**: In `APIGateway` hosting mode there is **no CloudFront**, so
the CloudFront `ResponseHeadersPolicy` does not apply; the SPA document and
asset responses set `X-Content-Type-Options`, `Strict-Transport-Security`,
`X-Frame-Options`, and `Referrer-Policy` per-method instead. The two SPA GET
routes are `AuthorizationType: NONE` (the SPA shell and hashed assets are not
secrets; auth happens inside the app against `/op`), but the PRIVATE endpoint
policy and the optional WAF WebACL still gate them. **No CSP is emitted in this
mode** — see UI.T01.

## 4. Agent & Chat Data Flows

### 4.1 Companion Chat Flow

There are **two transports** for chat, and they differ in their authorization
properties. This matters: the streaming transport is a separate internet-facing
endpoint that does not traverse the REST dispatcher.

**(a) Streaming transport — Lambda Function URL (the UI's default path)**

```mermaid
sequenceDiagram
    participant Browser as Browser
    participant IdPool as Cognito Identity Pool
    participant FURL as Lambda Function URL (AuthType=AWS_IAM)
    participant Proc as Chat Stream Processor (LWA/FastAPI)
    participant DDB as DynamoDB Chat tables
    participant Bedrock as Amazon Bedrock
    participant Tools as Agent Tools (Athena, MCP, etc.)

    Browser->>IdPool: Exchange Cognito JWT for temp AWS credentials
    IdPool-->>Browser: SigV4 credentials (CognitoAuthorizedRole)
    Browser->>FURL: POST /chat/agent or /chat/document (SigV4-signed)
    FURL->>FURL: IAM authZ: lambda:InvokeFunctionUrl on caller role
    FURL->>Proc: Invoke (RESPONSE_STREAM) + x-amzn-request-context
    Proc->>Proc: Derive caller id from SigV4 request context (role session name)
    Proc->>DDB: Load session memory keyed by sessionId
    Proc->>Bedrock: Invoke model with context + tools

    loop Tool Use
        Bedrock-->>Proc: Tool call request
        Proc->>Tools: Execute tool (Athena query, MCP call, etc.)
        Tools-->>Proc: Tool result
        Proc->>Bedrock: Continue with tool result
    end

    Bedrock-->>Proc: Token deltas
    Proc-->>Browser: SSE frames (text/event-stream), streamed
    Proc->>DDB: Persist chat turn (attributed to callerSub)
```

**Security-relevant characteristics of the streaming transport**:

| Property | Status |
|---|---|
| Network exposure | Public Function URL (no CloudFront/WAF/VPC in front) |
| Authentication | SigV4 via `AuthType=AWS_IAM` — unauthenticated callers rejected by Lambda |
| Authorization granularity | **Only** `lambda:InvokeFunctionUrl` on the shared `CognitoAuthorizedRole` — the role is common to **all five RBAC groups** (`Admin`, `Author`, `Reviewer`, `Annotator`, `Viewer`), so the IAM gate cannot distinguish Admin from Reviewer |
| Group (RBAC) enforcement | **None on this path.** The Admin/Author/Viewer restriction on `sendAgentChatMessage` is enforced in the *resolver*, not here. Fix **pending in issue #920** |
| Session ownership | **Not enforced.** Neither streaming processor performs the `ownerSub`-vs-caller check the resolver path does. Fix **pending in issue #920** |
| Caller identity available | Weaker than the diagram suggests: the value derived from the request context is the assumed-role **session name**, and on `/chat/agent` a body-supplied `callerSub` takes precedence over it. That session name is the same for every user of the deployment, so no per-user identity exists on this transport at all. **Issue #920** (PR #954) removes the precedence problem but does not create one — establishing a verified subject is still outstanding; see CHAT.T06 |
| CORS | `AllowOrigins: "*"`, `AllowCredentials: false` (safe: SigV4 in headers, no cookies) |
| Covered by `make api-test` | **No** — the harness drives `POST /op/{field}` only |
| Covered by the WAF / stage throttling | **No** — the WebACL is associated with the REST API stage; Lambda concurrency is the bound here |

See CHAT.T03 (streaming-path authorization gaps) and CHAT.T06 (caller-identity
trust inconsistency) for the full analysis.

**(b) Non-streaming transport — REST resolver (fallback; GovCloud without streaming)**

```mermaid
sequenceDiagram
    participant Browser as Browser
    participant APIGW as API Gateway REST
    participant Resolver as agent_chat / send_chat_document resolver
    participant DDB as DynamoDB Chat tables
    participant Proc as Chat Processor Lambda

    Browser->>APIGW: POST /op/sendAgentChatMessage (+ JWT)
    APIGW->>Resolver: Dispatch
    Resolver->>Resolver: Enforce group (Admin/Author/Viewer)
    Resolver->>DDB: Claim/verify session ownerSub == caller sub
    Note over Resolver: Mismatch -> "Unauthorized: session belongs to another user"
    Resolver->>Proc: Invoke processor (persistUserMessage=false)
    Resolver-->>Browser: Ack; UI polls for the final answer
```

**Trust boundary crossings**:
- TB1→TB3: User message via the Function URL (SigV4) **or** the REST API (JWT)
- TB3→TB4: Conversation context sent to Bedrock
- TB3→TB5: Analytics queries to Athena
- TB3→TB6: MCP tool calls to customer-managed agents

### 4.2 Agent Analysis Flow

```mermaid
sequenceDiagram
    participant Lambda as Agent Processor Lambda
    participant Bedrock as Orchestrator (Bedrock)
    participant Analytics as Analytics Agent
    participant Athena as Amazon Athena
    participant AgentCore as Bedrock AgentCore
    participant MCP as MCP Agents

    Lambda->>Bedrock: User query + tools
    Bedrock-->>Lambda: Route to analytics agent
    Lambda->>Analytics: Execute analytics query
    Analytics->>Athena: SQL query over processed data
    Athena-->>Analytics: Query results
    Analytics->>AgentCore: Execute Python visualization code
    AgentCore-->>Analytics: Generated chart/analysis
    Analytics-->>Lambda: Analysis result
```

**Security note**: Natural language → SQL translation creates SQL injection risk. AgentCore code execution is sandboxed but executes AI-generated code.

### 4.3 MCP Integration Flow

```mermaid
sequenceDiagram
    participant Agent as Agent Lambda
    participant Bedrock as Amazon Bedrock
    participant MCPLambda as MCP Gateway Lambda
    participant ExtService as External Service / API

    Agent->>Bedrock: Prompt with MCP tool definitions
    Bedrock-->>Agent: Tool call (MCP tool)
    Agent->>MCPLambda: Invoke MCP Lambda with tool parameters
    MCPLambda->>ExtService: Call external API / service
    ExtService-->>MCPLambda: Response
    MCPLambda-->>Agent: Tool result
```

**Trust boundary crossings**: TB3→TB6→External. MCP agents can call external services, introducing data exfiltration and injection risks.

## 5. SDK/CLI Data Flow

```mermaid
sequenceDiagram
    participant CLI as SDK/CLI Client
    participant Cognito as Cognito User Pool
    participant APIGW as API Gateway REST
    participant S3 as S3 Buckets

    CLI->>Cognito: Authenticate (SRP / username+password)
    Cognito-->>CLI: JWT tokens
    CLI->>APIGW: POST /op/{field} operations (config, status, etc.)
    CLI->>S3: Upload documents (presigned URLs)
    loop Monitor
        CLI->>APIGW: Poll document status
    end
```

**Security note**: SDK/CLI stores credentials locally on developer machines. Tokens are short-lived but refresh tokens provide extended access. The CLI is subject to exactly the same resolver-side group/scope checks as the browser — there is no privileged CLI bypass.

### 5.1 Jobs API Flow (machine-to-machine, `EnableJobsApi=true`)

```mermaid
sequenceDiagram
    participant M2M as Machine Client
    participant ApiPool as Cognito ApiUserPool (separate pool)
    participant VPCe as VPC Endpoint
    participant JobsAPI as API Gateway PRIVATE /jobs
    participant Handler as Api Handler Lambda
    participant S3 as S3 Input Bucket

    M2M->>ApiPool: client_credentials grant (client id + secret)
    ApiPool-->>M2M: Access token with scope idp-api/jobs.read|jobs.write
    M2M->>VPCe: POST /jobs (+ Bearer token)
    VPCe->>JobsAPI: Resource policy: deny unless aws:SourceVpce matches
    JobsAPI->>JobsAPI: Cognito authorizer validates token
    JobsAPI->>Handler: Dispatch
    Handler->>S3: Submit document(s) for processing
```

**Security-relevant characteristics**:
- Uses a **separate Cognito user pool** (`ApiUserPool`) from the Web UI pool — so Jobs API clients are *not* subject to the five-group RBAC model at all. Authorization is by OAuth **scope** (`jobs.read` / `jobs.write`), not Cognito group.
- The client has a **static secret** (`GenerateSecret: true`); there are no per-document or per-config-version restrictions on what a `jobs.write` client may submit.
- Endpoint is `PRIVATE` with a resource policy denying any request whose `aws:SourceVpce` doesn't match the supplied endpoint — so exposure is VPC-scoped, not internet-scoped.
- **Not covered** by `make api-test` (which targets the UI `/op` route).

See [JOB.T01–T03](../feature-threats/jobs-api.md).

### 5.2 Feature Platform / Extension Install + Load Flow

```mermaid
sequenceDiagram
    participant Admin as Admin
    participant CFN as CloudFormation
    participant FeatStack as Feature Stack (own template)
    participant Deployer as Feature ui-deployer
    participant WebUIB as S3 Web UI Bucket
    participant Host as Host SPA (browser)
    participant APIGW as API Gateway REST

    Admin->>CFN: Deploy feature stack (out-of-band, console/CLI)
    CFN->>FeatStack: Create feature resources + register feature
    FeatStack->>Deployer: Run ui-deployer
    Deployer->>WebUIB: Write features/<id>/v<ver>/ui-bundle.js (Cache-Control 300)
    Note over Deployer,WebUIB: Scoped write: only its own prefix

    Host->>APIGW: POST /op/listInstalledFeatures (any authenticated user)
    APIGW-->>Host: Feature registry incl. uiBundlePath
    Host->>Host: createElement('script') src=/<path>/ui-bundle.js
    WebUIB-->>Host: Bundle bytes (SAME ORIGIN as host SPA)
    Host->>Host: Bundle calls IdpFeatures.register(); gets React,<br/>Cloudscape, amplify, IdpFeatureHost.generateClient
    Host->>APIGW: Feature-initiated API calls using the USER'S session
```

**Trust boundary crossings**: TB6→TB1. This is the sharpest boundary crossing
added since v2.0: the feature's JavaScript executes **in the host SPA's origin**,
inside the authenticated user's session, with a host-provided API client. It is
**not** sandboxed (no iframe, no CSP isolation — and the CloudFront CSP permits
`unsafe-inline`/`unsafe-eval`). Installing a feature grants it the effective
privilege of every user who loads the UI. Install is Admin-gated and the
ui-deployer's S3 write is prefix-scoped, but there is **no integrity check**
(no SRI hash, no signature) on the bundle at load time: `FeatureLoader` appends
a `<script>` whose `src` is `/<feature>/ui-bundle.js` with `crossOrigin` set and
**no `integrity` attribute**, so nothing binds the bytes reviewed at install time
to the bytes the browser executes later. The controls that do apply are the
bucket's write permissions and the install-time decision to trust the feature.

See [FEAT.T01–T04](../feature-threats/feature-platform.md).

## 6. Reporting & Analytics Data Flow

```mermaid
sequenceDiagram
    participant Lambda as Reporting Lambda
    participant S3 as S3 Reporting Bucket
    participant Glue as AWS Glue Crawler
    participant Athena as Amazon Athena
    participant Agent as Analytics Agent

    Lambda->>S3: Write Parquet files (metering, evaluation data)
    Glue->>S3: Crawl and catalog Parquet data
    Glue->>Glue: Update Glue Data Catalog
    Agent->>Athena: SQL query
    Athena->>S3: Read Parquet data
    Athena-->>Agent: Query results
```

## 7. Knowledge Base Data Flow

```mermaid
sequenceDiagram
    participant Lambda as KB Lambda
    participant S3 as S3 KB Source Bucket
    participant BedrockKB as Bedrock Knowledge Base
    participant OpenSearch as OpenSearch Serverless
    participant Pipeline as Processing Pipeline

    Lambda->>S3: Upload reference documents
    Lambda->>BedrockKB: Trigger data source sync
    BedrockKB->>S3: Read source documents
    BedrockKB->>BedrockKB: Chunk, embed documents
    BedrockKB->>OpenSearch: Store vector embeddings

    Pipeline->>BedrockKB: RAG query (retrieve relevant context)
    BedrockKB->>OpenSearch: Vector similarity search
    OpenSearch-->>BedrockKB: Matching chunks
    BedrockKB-->>Pipeline: Retrieved context for augmented prompts
```

## 8. Lambda Hook Data Flows

### 8.1 Inference Hook

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant Hook as Customer Lambda Hook
    participant ExtModel as External Model / API

    SFN->>Hook: Invoke with document data + context
    Hook->>ExtModel: Custom inference call
    ExtModel-->>Hook: Model results
    Hook-->>SFN: Results in expected format
```

### 8.2 Post-Processing Hook

```mermaid
sequenceDiagram
    participant SFN as Step Functions
    participant Decomp as Decompressor Lambda
    participant Hook as Customer Lambda Hook
    participant ExtSys as External System

    SFN->>Decomp: Invoke with compressed results
    Decomp->>Decomp: Decompress document data
    Decomp->>Hook: Invoke with full document results
    Hook->>ExtSys: Push results to external system
    Hook-->>Decomp: Acknowledgment
```

**Trust boundary crossings**: TB3→TB6. Customer-managed Lambda hooks receive full document processing results and can send data to arbitrary external systems.

> **Failure containment across hook points.** `onError: fail` is documented as
> stopping the workflow, and does so at the `preprocessing` hook point. At the
> other six (`postOcr`, `postClassification`, `postExtraction`,
> `postRuleValidation`, `postSummarization`, `postprocessing`) the state
> machine's `Catch` block routes forward to the next step, so a hook that fails
> — including one configured `onError: fail` — does not halt processing. A
> deployment relying on a hook to enforce a business or compliance gate at one of
> those points does not have that guarantee today. See HOOK.T07; fix **pending in
> issue #919**.

## 9. Summary of Cross-Boundary Data Flows

| Flow | From | To | Data Sensitivity | Controls |
|------|------|----|-----------------|----------|
| Document upload | TB1 | TB3 | High (customer docs) | HTTPS, presigned URLs, Admin/Author group |
| OCR processing | TB3 | TB4 | High (full document text) | TLS, IAM roles |
| LLM prompts (Anthropic/Nova) | TB3 | TB4 | High (document text + PII) | TLS, IAM roles, no training opt-out |
| **LLM prompts (OpenAI GPT-5.x via mantle)** | TB3 | TB4 | High (document text + PII) | TLS, IAM roles; **different model family — verify vendor/residency constraints** (PM.T08) |
| **UI API operations** | TB1 | TB3 | High | JWT authn at gateway; **group/scope authz in resolver Lambdas only**; input-shape validation; optional WAF |
| **Chat streaming (SSE)** | TB1 | TB3→TB4 | Medium-High | **SigV4 on a public Function URL; no group check, no session-ownership check** (CHAT.T03) — fixes **pending in issue #920**; not covered by the WAF or by `make api-test` |
| Chat messages (REST path) | TB1 | TB3→TB4 | Medium-High | Group check + `ownerSub` ownership check |
| **Jobs API submission** | TB1 | TB3 | High | Separate Cognito pool, OAuth scopes, PRIVATE endpoint + VPCe resource policy (JOB.T01) |
| **Feature UI bundle** | TB6 | TB1 | High (runs as the user) | Admin-gated install, prefix-scoped S3 write; **no SRI/signature, same-origin, unsandboxed** (FEAT.T01) |
| **Preprocessing / PII hook** | TB3 | TB6→TB4 | High (un-redacted document) | Fail-closed `onError: fail` (terminal at this hook point only — HOOK.T07, **pending in issue #919**); re-entrancy guard; detection call still sees raw PII (PII.T01) |
| MCP tool calls | TB3 | TB6→External | Variable (depends on tool) | IAM, customer responsibility |
| Lambda hooks | TB3 | TB6 | High (full processing results) | IAM, invocation-only permissions |
| Analytics queries | TB3 | TB5 | High (aggregated processing data) | Athena workgroup, IAM |
| KB retrieval | TB3 | TB4→TB5 | Medium (reference doc chunks) | IAM, encryption |
| SDK/CLI auth | TB1 | TB2 | High (credentials) | SRP protocol, short-lived tokens |
| Configuration | TB1 | TB3 | Medium (prompts, schemas) | Auth, schema validation, config-version scope |
| **Presigned object reads** | TB3 | TB1 | High (any object in stack buckets) | Bucket allow-list only — **no key-level scoping** (UI.T06) |
| **Test-set ground truth writes** | TB1 | TB3 | Medium (evaluation baselines) | Admin/Author group; `_editHistory` provenance (RPT.T07) |
