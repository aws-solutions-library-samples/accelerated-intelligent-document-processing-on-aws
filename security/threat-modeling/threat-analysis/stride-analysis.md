# STRIDE Threat Analysis

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 3.4 |
| **Last Updated** | 2026-11-24 |
| **Applies to release** | v0.6.9 |
| **Classification** | Internal |
| **Methodology** | STRIDE (Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege) |

## 1. Overview

This document provides a comprehensive STRIDE analysis across all components of
the GenAI IDP Accelerator unified architecture. Each STRIDE category is analyzed
for the system's major components: document processing pipeline, AI/ML services,
web UI, agent system, authentication/authorization, extensibility (hooks/MCP), and
data storage/analytics.

> **How to read this document.** It is deliberately **category-level**: rows are
> phrased as threat *classes* rather than as the numbered threat entries, so a
> reader can scan a STRIDE category without holding 123 identifiers in their head.
> The authoritative per-threat record — identifier, score, status, and whether a
> mitigation is present or pending — is the
> [risk register](../risk-assessment/risk-matrix.md), which is derived from the
> generated export. Where a row's mitigation depends on an unmerged change it is
> marked **pending** with its issue number; treat those as absent today.

## 2. Spoofing

Spoofing threats involve an attacker pretending to be something or someone they are not.

### 2.1 Identity Spoofing

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Stolen JWT tokens** used to impersonate legitimate users | API Gateway REST API, Cognito | High | Short-lived tokens (1hr), Cognito advanced security, TLS enforcement; token-negative + expiry + logout suites in `make api-test` (see AUTH.T10) |
| **Compromised SDK/CLI credentials** on developer machines | IDP SDK/CLI | Medium | Environment variable-based credentials, credential helpers, documentation |
| **Self-registration** of unauthorized accounts | Cognito User Pool | Medium | Self-signup is off unless `AllowedSignUpEmailDomain` is set; when it is, a self-registered account holds no group, so the dispatcher's `ANY_GROUP` floor refuses it every document read and the eight `ANY` operations are what it reaches (AUTH.T04, AUTH.T16) |
| **Refresh token replay** for persistent access | Cognito | Medium | Configurable refresh token expiry, revocation capabilities, anomaly detection |
| **External MCP client impersonation** | AgentCore Gateway / MCP handler | Medium | Cognito authentication required for all clients (dedicated M2M resource server) |
| **Client-supplied caller identity on the agent streaming route** — a request body field is read in preference to the request's signed identity | Lambda Function URL (chat streaming) | High | **Not mitigated today.** The identity available from the signed request is the assumed-role session name rather than a verified Cognito `sub`, and that session name is the same for every user of the deployment, so establishing a *verified* caller is the fix — reordering the two would attribute every turn to one shared identifier. See CHAT.T06; **issue #920** / PR #954 is a partial step and leaves the threat Open |
| **Group assignment from a user-writable attribute** — an external identity provider maps a claim the user can edit onto a Cognito group | Cognito pre-token trigger, external IdP | Medium | Group mapping reads provider-controlled claims; `make verify-idp-federation` exercises a real federated sign-in. Note `Annotator` enters that mapping only when `ExternalIdPAnnotatorGroupName` is set; left at its empty default it is assigned in Cognito directly, outside the IdP's joiner/leaver process (**issue #968**). See AUTH.T13 |
| **Tenancy decided outside the stack** — under `TenantGrantSource=IdpClaim` or `Api` the identity provider's role administration or an entitlement system grants each user's tenant | Pre-token trigger, `setUserTenant`, SDK | Medium | Claim honoured only for a user linked to the configured provider and only at a fresh sign-in; exactly one tenant or none; failure fails the sign-in; Admin edits refused; removal applies at the next fresh sign-in, bounded by `RefreshTokenValidityHours`. See TEN.T03 |

### 2.2 Service Spoofing

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **MCP tool response spoofing** — external service returns manipulated data | MCP Integration | Medium | Response validation, tool output sanitization |
| **Lambda hook substitution** — replacing legitimate hook with malicious one | Lambda Hooks | Low | CloudFormation-managed hook ARN, IAM restrictions on Lambda updates |

## 3. Tampering

Tampering threats involve unauthorized modification of data or code.

### 3.1 Document/Data Tampering

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Prompt injection via document content** — adversarial text in uploaded documents | Pipeline Processing | High | Input sanitization, prompt guardrails, output validation, Bedrock Guardrails |
| **Malicious file upload** — a weaponized PDF/Office document whose payload is in the file structure, not its text, so Bedrock Guardrails cannot see it | Document Ingest | High | Optional ingest gate on GuardDuty Malware Protection for S3 (`MalwareScanning`); refused documents never start a workflow, so their bytes never reach OCR, page rendering or Bedrock. Off by default, and static detection only — see [malware-scanning.md](../../../docs/malware-scanning.md) and PM.T09 |
| **Reviewer-controlled final output** — a queue item's holder saves drafts that the finalize run copies verbatim over the extraction results | Review Queues | Medium | Holder-only writes conditioned in DynamoDB on version, state and owner; supervisor actions Admin only; finalize message built server-side. Drafts are not checked against the section schema. See HITL.T02 |
| **Prompt injection via chat messages** — manipulating agent behavior | Companion Chat | High | System prompt hardening, input/output tags, Bedrock Guardrails |
| **Configuration tampering** — malicious modification of prompts/schemas | Configuration System | Critical | RBAC, config versioning, JSON Schema validation, audit logging |
| **Few-shot example poisoning** — biased training examples | Pipeline Processing | Medium | Admin-only management, evaluation framework |
| **Knowledge Base poisoning** — contaminated reference documents | Knowledge Base | High | Admin-only KB management, sync review, evaluation monitoring |
| **RAG context injection** — prompt injection via retrieved KB content | Knowledge Base | High | Context isolation in prompts, output validation |
| **Conversation history poisoning** — multi-turn prompt injection | Companion Chat | Medium | History sanitization, session limits |
| **Evaluation data manipulation** — hiding accuracy degradation | Evaluation | High | S3 versioning, RBAC, integrity checks |
| **Reporting data tampering** — altering analytics records | Reporting | Medium | S3 versioning, write-only IAM, CloudTrail |
| **Discovery prompt injection** — adversarial sample documents | Discovery | High | Draft-only configs, human review required, schema validation |
| **Decision model answers accepted as results** — a configured decision model function sets a section's class or a value's confidence with no independent check | Lambda Hooks (decision models) | Medium | Selecting a model is an Admin/Author versioned configuration change; responses must match the `SystemOneResponse` contract; an unknown class goes to the fallback class; the confidence source is recorded per value. See HOOK.T08 |
| **Bulk skip releases unreviewed output** — one `bulkSkipAllReviews` request completes every pending review of a profile with its machine output | Review Queues | Medium | Admin and Reviewer only at the dispatcher and the resolver; profile scope checked before any document is read; per-document classification of what may be skipped; a required reason, review history and an Inbox notice; one job per stack. See HITL.T05 |
| **Routing decides what a person reviews** — a profile's routing table, the Author-editable settings that feed it and reprocess skip options decide whether a document is held for review (HITL.T06) | Review Queues | Medium | Routing table Admin-only; S-FEEL parser with no interpreter; unevaluable tables fail to the Default queue; DMN parsed with `defusedxml`, DTDs forbidden; reprocess options Admin/Author and scope-checked |
| **Uploader-asserted document attributes** — key-value pairs from upload metadata or a sidecar carried to results and hooks as if authoritative (PM.T10) | Document Ingest | Low | One validation module; values never logged; not used in prompts, paths, profile or tenant selection or routing; rendered as text |

### 3.2 Code/Infrastructure Tampering

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Malicious MCP tool injection** — adding unauthorized tools | MCP Integration | High | IaC-managed tool definitions, admin-only deployment |
| **MCP response injection** — external service injects malicious content | MCP Integration | High | Output sanitization, response size limits |
| **Inference hook result tampering** — corrupted extraction results | Lambda Hooks | High | Output schema validation, assessment step, evaluation |
| **Cross-step data poisoning** — corrupted intermediate pipeline data | Pipeline Processing | High | S3 encryption, IAM role separation, state validation |
| **BDA output mapping errors** — data corruption in format normalization | BDA Mode | Medium | Strict schema validation, defensive parsing |
| **OCR manipulation** — adversarial documents producing incorrect text | Pipeline Processing | Medium | Format validation, confidence thresholds |
| **Glue Catalog manipulation** — altered data schemas/locations | Reporting | Medium | IAM restrictions, CloudTrail, catalog validation |
| **Hook failure does not halt the workflow** — closed: `onError: fail` is terminal at all seven hook points, each routing its catch to a terminal failure state (issue #919) | Lambda Hooks | High | `onError: fail` is terminal at the `preprocessing` hook point only; at the other six the state machine's `Catch` block routes forward to the next step. A deployment relying on a hook as a compliance or business gate at one of those points does not have that guarantee. See HOOK.T07; **pending in issue #919** |
| **Terraform module substitution** — `idp-cli deploy --terraform` applies a module fetched from a URL with the deployer's credentials | SDK/CLI | Medium | SHA-256 checksum checked against the `idp-terraform-latest.json` pointer on the SDK's fetch; S3 or HTTPS URLs only. Terraform's own download carries no pinned checksum, and a module URL given directly carries none. See SDK.T07 |
| **Destructive Terraform plan or migration** — an apply, upgrade or migration deletes or replaces a bucket, table, key or user pool | SDK/CLI | Medium | The saved plan is refused when it deletes or replaces a retained or stateful address unless `--terraform-allow-replace-stateful` is given; migration phases are dry runs without `--apply`, and state is backed up first. See SDK.T08 |

## 4. Repudiation

Repudiation threats involve users denying they performed an action.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Untracked configuration changes** — no audit trail for config modifications | Configuration System | Medium | CloudWatch logging, config versioning, CloudTrail |
| **Untracked agent actions** — agent tool invocations without logging | Agent System | Medium | All agent/tool invocations logged to CloudWatch |
| **BDA processing opacity** — limited internal processing visibility | BDA Mode | Medium | BDA API call logging via CloudTrail, output validation |
| **Untracked document uploads** — uploads without attribution | S3 Input Bucket | Low | EventBridge events, S3 access logging, CloudTrail |
| **Review history that is not tamper-evident** — review activity and dispositions live in a table several roles can write | Review Queues | Medium | Activity, field edits and disposition recorded per item; reviewer and disposition also in `status.json` and the `document_reviews` reporting table. See HITL.T02 |
| **Untracked hook invocations** — customer code execution without logging | Lambda Hooks | Low | Platform logs hook invocations, CloudWatch logs on hook Lambdas |

## 5. Information Disclosure

Information disclosure threats involve exposure of sensitive data to unauthorized parties.

### 5.1 Data Exposure

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Document data sent to AI services** — PII/sensitive content in LLM prompts | Bedrock, Textract | High | AWS data processing policies (no training), TLS, IAM roles |
| **Cross-user data leakage via Athena** — accessing other users' documents | Analytics Agent | High | RBAC, single-tenant deployment or Admin-only under `TenantIsolationMode: Strict` in a shared one, query scoping |
| **Athena query results exposure** — stored results accessible | Reporting | High | Workgroup restrictions, result lifecycle policies, IAM |
| **Conversation history exposure** — sensitive chat content | Companion Chat | High | DynamoDB encryption, user-scoped access, TTL policies |
| **Data exfiltration via MCP tools** — sending data to external services | MCP Integration | Critical | IAM least-privilege, VPC egress controls, audit logging |
| **Data exfiltration via post-processing hooks** — full results sent externally | Lambda Hooks | Critical | Customer-managed VPC, security review, monitoring |
| **Vector store exposure** — KB embeddings and text chunks accessible outside the query resolver | Knowledge Base | Low | Customer-managed key encryption; grants and data access policies name the knowledge base's own resources and service role; knowledge-base queries are Admin-only under tenant isolation. See KB.T03 |
| **Client-side config exposure** — backend endpoints visible in JS | Web UI | Low | All endpoints require auth, security through access control |
| **Review items outside the caller's profile scope** — a system-wide queue holds several profiles' items, so a listing must filter each item by its own profile | Review Queues | Low | Every per-item operation and listing applies `scope_allows` to the item's profile, and so do a scoped caller's queue counts. See HITL.T01 |
| **Cross-tenant access in a shared stack** — tenants share every Lambda, table and bucket, so isolation is the application's scope checks | Tenant isolation | High | Write-time boundary checks; under `Strict` or an external grant source unscoped non-Admins are denied, cross-tenant documents refused at ingest, and the agent, Knowledge Base, MCP and chat-stream surfaces closed; Admins are cross-tenant, and `Off` with `Manual` (the default) leaves an unscoped non-Admin unrestricted. See TEN.T01 |
| **Review metadata beyond the queue's audience** — group-addressed Inbox announcements, `status.json`, Output-bucket EventBridge events | Review Queues, Inbox | Low | Self-scoped Inbox reads, no posting operation, in-app links only, sanitized rendering; announcements address whole groups. See HITL.T03 |
| **Credential-shaped values in resolver logs** — a name-based redaction denylist | API resolver Lambdas, CloudWatch Logs | Medium | Every resolver applies the one canonical denylist (issue #921 closed, held by a test), so the exposure is a credential under a key the list does not name or in free text; log groups are retention-bounded and 113 of the 115 in the core templates use the stack KMS customer-managed key. See AUTH.T15 |
| **Deployment data forwarded by an opt-in agent** — the Code Intelligence Agent and External MCP agents send queries to servers outside the deployment | Agent System | Medium | Both are off unless a conversation opts in; once a data agent returns a result in a session no opt-in agent runs in it. See AGT.T06 |
| **Document content sent to any `GENAIIDP-*` function** — decision model and `LambdaHook` grants are a name prefix, not a list of owned functions | Lambda Hooks | Medium | Grant and `lambda:FunctionName` condition bounded to this account, Region and prefix; the client refuses an endpoint without the prefix; endpoint changes are Admin/Author and versioned. See HOOK.T09 |
| **Test Set Generator rendering fetches remote resources** — model-written HTML referencing remote images, stylesheets or fonts | Feature Platform (Test Set Generator) | Low | The PDF renderer runs with remote access disallowed; pinned `seed-data` and `xhtml2pdf` keep the tool set fixed. See FEAT.T05 |
| **Terraform state disclosure** — `idp-cli deploy --terraform` keeps state, including secrets, in a local working directory with no backend | SDK/CLI | High | `terraform.tfvars`, parameters and migration backups written owner-only; working directory gitignored; docs recommend an encrypted, locked remote backend. `terraform.tfstate` itself is unencrypted on local disk. See SDK.T06 |

### 5.2 Session/Token Exposure

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **JWT token theft via XSS** — browser-stored tokens stolen | Web UI | High | React XSS protection, CSP headers, short token lifetime |
| **Chat stream cross-user access** — reading another user's stream via their `sessionId` | Lambda Function URL (chat streaming) | High | **Mitigated.** The endpoint verifies the caller's Cognito ID token, applies the RBAC group checks from it, and checks each session id against its recorded owner before any memory or history loads; `make api-test` has cells for this transport (`run_chat_session_idor_suite`). See CHAT.T03 |
| **Conversation session hijacking** — accessing other users' chats | Companion Chat | High | UUID session IDs, user-scoped DynamoDB queries |
| **SDK credential exposure** — credentials on developer machines | SDK/CLI | High | Env var credentials, short-lived tokens, secure documentation |
| **Presigned URL interception** — captured upload URLs reused | Web UI | Medium | Short expiration, conditions, TLS |
| **MCP client secret exposure** — a client secret readable more widely than intended, or a credential granted beyond its creator's scope | MCP Integration | Medium | Secrets in Secrets Manager under the stack key, returned once and never listed; credential operations Admin-only and confined to the creator's scope. See MCP.T07 |

## 6. Denial of Service

Denial of service threats involve making the system unavailable.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Processing pipeline saturation** — flooding with documents | Document Processing | Medium | SQS buffering plus DynamoDB **admission control**: a slot is taken by a conditional counter increment (`active_count < max`), so over-limit work is rejected atomically rather than by an in-memory comparison; drift is reconciled and emitted as a metric, a circuit breaker can stop admission entirely, and execution names are derived deterministically so a redelivered message cannot start a second execution. The failure mode to watch is a *leaked slot* (capacity lost until reconciliation), not over-admission. CloudWatch alarms |
| **Bedrock quota exhaustion** — excessive model invocations | AI/ML Services | Medium | Token limits, rate limiting, capacity planning, alarms |
| **Textract throttling** — exceeding OCR API limits | Pipeline Mode | Medium | Retry with backoff, DLQ, service quota management |
| **BDA service unavailability** — BDA outage or throttling | BDA Mode | Medium | Mode switching fallback, retry/DLQ, alarms |
| **Lambda concurrency exhaustion** — all functions saturated | All Lambdas | Medium | Reserved concurrency, SQS buffering, alarms |
| **Expensive agent operations** — complex tool chains consuming resources | Agent System | Medium | Lambda timeouts, token limits, rate limiting |
| **Batch processing abuse via SDK** — overwhelming pipeline | SDK/CLI | Medium | Concurrency controls, rate limiting, alarms |
| **Test Studio cost escalation** — excessive interactive testing | Test Studio | Medium | RBAC, rate limiting, cost monitoring |
| **Hook Lambda failure cascade** — customer hooks blocking pipeline | Lambda Hooks | Medium | Step Functions timeouts, error handling, DLQ |
| **AgentCore gateway disruption** — MCP infrastructure unavailable | MCP Integration | Medium | Health monitoring, alarms, lifecycle management |
| **Tenant declarations unreadable at ingest** — under `Strict` every document's tenant check reads one ConfigurationTable item | Tenant isolation | Low | Fails closed (document retried, not admitted unchecked) with `TenantIsolationLookupFailedAlarm`; the index is size-bounded. Throughput is shared between tenants and not metered per tenant. See TEN.T05 |
| **Decision model failure or cost amplification** — a configured decision model has no LLM fallback, and each call is a Lambda execution | Lambda Hooks (decision models) | Medium | Typed failures, bounded retries clamped to the caller's remaining time, `max_concurrency`, `max_pages` and `timeout_ms` limits, and a failure metric. See HOOK.T10 |

## 7. Elevation of Privilege

Elevation of privilege threats involve gaining capabilities beyond what was authorized.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Cognito group manipulation** — self-promoting to Admin role | Authentication | Critical | IAM-protected Cognito admin APIs, no self-service groups, CloudTrail |
| **API authorization bypass** — exploiting resolver gaps | RBAC | High | The dispatcher's default-deny group floor plus per-op resolver checks (the gateway authorizer only authenticates); `make api-test`/`api-test-static` gate regressions; see AUTH.T03/T08/T16 |
| **Per-object authorization is opt-in per resolver** — the dispatcher's default deny enforces each operation's declared groups, and every narrower check (scope, ownership, queue eligibility) is hand-written in the resolver | API Gateway dispatcher | High | The dispatcher refuses an operation with no entry in the generated manifest (`scripts/api_rbac_expectations.yaml`, all 164 operations); `make api-test-static` in both CI systems checks that an enforcement pattern exists for each declared scope or ownership rule, and the live harness asserts a per-object refusal only where `object_scoped_roles` is declared. The 403 mapping still keys partly on error-message text. See AUTH.T16 |
| **Deployment role breadth** — the shipped CloudFormation service role holds `cloudformation:*` and broad service wildcards | Deployment / SDLC | Medium | **Partially mitigated.** IAM writes and `iam:PassRole` are confined to names starting with `ManagedStackNamePrefix`, every role it creates must carry the `CreatedRolePermissionsBoundaryArn` boundary, and removing a boundary is denied (issue #927); the service wildcards stay broad by design, so who may pass the role remains the control. See SDK.T05 |
| **MCP credentials managed outside the application roles** — `idp-cli mcp credentials` and `IDPClient.mcp` call the credential library with the caller's AWS credentials | SDK/CLI | Medium | The same `validate_access` tenancy checks as the MCP Configuration page; the actor recorded is the caller's STS ARN; the IAM permissions needed already confer comparable control. See SDK.T09 |
| **Resolver grants wider than the Annotator operations they serve** — UsersTable writes on the test-set resolver, tracking-table writes and `DocumentQueue` sends on the review-queues resolver | Review Queues | Medium | Grants scoped to this stack's named resources; the scope-writing operation is Admin only at the dispatcher and in the resolver. See HITL.T04 |
| **Access-point delegation** — under `Strict` the Input and Output bucket policies admit any access point the account owns | Tenant isolation | Medium | Generated access point policies confine with explicit denies and refuse account-root principals; restricting `s3:CreateAccessPoint`/`s3:PutAccessPointPolicy` is a documented prerequisite the template cannot enforce. See TEN.T02 |
| **Sign-in trigger write grant** — under `IdpClaim` the pre-token trigger holds whole-table UsersTable write, and a `persona` write reaches Cognito groups through the stream sync | Tenant isolation | Medium | Grant and layer exist only under `IdpClaim`; the writer changes scope attributes only and never an Admin row; IAM does not restrict the attributes. See TEN.T04 |
| **Parser exploit in a processing Lambda** — a weaponized document runs code with the function's role | Pipeline Processing | Medium | Optional GuardDuty ingest gate, pinned dependencies with `make dep-audit`, one role per function. See PM.T09 |
| **Agent routing manipulation** — tricking orchestrator to invoke restricted agents | Agent System | Medium | Agent-level auth, tool access controls, audit logging |
| **Configuration-driven privilege escalation** — malicious config enabling unauthorized model access | Configuration | High | Config schema validation, RBAC, model allowlisting |
| **MCP tool parameter manipulation** — causing tools to access unauthorized resources | MCP Integration | High | Input validation, parameter schemas, least-privilege credentials |
| **Hook IAM role over-privilege** — customer hooks accessing platform resources | Lambda Hooks | High | Separate IAM roles, platform resource policies, documentation |
| **Arbitrary code execution via AgentCore** — AI-generated malicious code | Agent System | Medium | AgentCore sandbox (no credentials; no public internet access; reaches Amazon S3 in the session's region and the instance metadata service, which lists no role), output validation |
| **BDA project configuration tampering** — altering BDA processing behavior | BDA Mode | High | IAM restrictions, CloudTrail, admin-only access |

## 8. Cross-Cutting Threats

These threats span multiple STRIDE categories and components:

### 8.1 Supply Chain Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **SDK dependency compromise** | Code execution on developer machines | Pinned versions, dependency scanning |
| **Lambda layer compromise** | Code execution in processing pipeline | Verified layers, integrity checks; first-party packages installed from the checkout, not by name; custom-resource Lambdas carry the CloudFormation response helper beside the handler rather than installing `cfnresponse` from the public index |
| **React dependency compromise** | XSS in web UI | Lock files, dependency scanning, CSP |
| **In-account image built from third-party code** — the Strands Decider extension builds upstream source and model weights with CodeBuild (FEAT.T06) | Code processing document content inside the account, with egress | Commit-SHA pins for source and weights, fork and branch revisions refused, scan on push, logs-only function role |

### 8.2 AI/ML-Specific Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **Model hallucination** | Incorrect business decisions | Output validation, evaluation, human review |
| **Training data contamination** (upstream) | Degraded model performance | Use well-known models, evaluation framework |
| **Indirect prompt injection** (via documents, KB, MCP responses) | Model behavior manipulation | Input sanitization, context isolation, Bedrock Guardrails |

### 8.3 Infrastructure Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **CloudFormation stack manipulation** | Full system compromise | IAM and stack policies. The CloudFormation **service role** confines its IAM writes to the stack-name prefix and requires a permissions boundary on every role it creates (issue #927), but its service wildcards stay broad, so it bounds IAM escalation rather than the blast radius of a stack operation (SDK.T05) |
| **S3 bucket policy misconfiguration** | Data exposure | IaC-defined policies, security reviews, automated checks |
| **DynamoDB over-permissions** | Cross-table data access | Per-table IAM policies, least-privilege Lambda roles |
