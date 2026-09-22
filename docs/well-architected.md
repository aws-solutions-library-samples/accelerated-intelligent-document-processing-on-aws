---
title: "AWS Well-Architected Framework Review"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# AWS Well-Architected Framework Review

This document is a working template for reviewing **your** deployment of the GenAI
Intelligent Document Processing (GenAIIDP) Accelerator against the six pillars of the
[AWS Well-Architected Framework](https://docs.aws.amazon.com/wellarchitected/latest/framework/welcome.html).

It is organized in two halves per pillar. The first half describes what the solution
implements today, naming the template, resource, parameter or script that implements it
so you can go and look at it in your own stack. The second half is a checklist of
questions that the solution deliberately leaves for you to answer, with empty columns
for your comment, the owner you assign, and the date you reviewed it.

The checklist ships empty on purpose. The answers are properties of your account, your
data classification, your recovery objectives and your budget — they are not properties
of the accelerator, and we cannot fill them in for you.

## How to use this document

Copy this page into your own review record — a wiki page, a spreadsheet, or the notes
field of a [Well-Architected Tool](https://docs.aws.amazon.com/wellarchitected/latest/userguide/intro.html)
workload — before you fill anything in. Do not edit it in place in a checkout of this
repository, because a later upgrade will overwrite it.

Then work pillar by pillar:

1. Read the "What the solution implements" section and confirm each statement against
   your deployed stack. Parameter defaults can be overridden at deploy time, so a
   statement that is true of a default deployment may not be true of yours.
2. Answer every row of the pillar's checklist. Record the answer even when it is "we
   accept this risk" — an explicit accepted risk with an owner and a date is a review
   outcome; a blank row is not.
3. Assign an owner and a date to each row. The date is when the item was last reviewed,
   not when it is due, so that a future reviewer can tell how stale an answer is.
4. Re-run the checklist when you upgrade the accelerator, change processing
   configuration in a way that alters cost or model choice, or change the account's
   guardrails.

The rows are intentionally specific to this solution. Generic Well-Architected questions
are better taken from the framework itself; what this document adds is the set of
decisions that *this* accelerator exposes as a choice and that a default deployment
therefore leaves unmade.

## What the solution provides, and what remains yours

The clearest way to read this document is to know the split up front.

**Provided by the accelerator, active on a default deployment.** Infrastructure as code
for the whole stack; fourteen CloudWatch alarms (a fifteenth is declared but only
created when you enable the Bedrock circuit breaker); two CloudWatch dashboards; AWS
X-Ray tracing on the document-processing Lambda functions and both state machines, which
`EnableXRayTracing` turns off in one place if you do not want it; Step Functions retry and catch
blocks with dead-letter queues behind every SQS consumer; a customer-managed KMS key
encrypting the DynamoDB tables, S3 buckets, SNS topics and log groups; 32 TLS-only
resource policies on buckets and queues; S3 versioning and DynamoDB point-in-time recovery;
Cognito authentication with a REST API authorizer; concurrency admission control; and
per-invocation token and cost metering written to a queryable ledger.

**Provided but off by default, so you must choose.** The IAM permissions boundary
(`PermissionsBoundaryArn`); the WAFv2 IP allow-list (`WAFAllowedIPv4Ranges`); the Bedrock
service-outage circuit breaker (`CircuitBreakerEnabled`); Bedrock Guardrails
(`BedrockGuardrailId`); VPC deployment (`DeployInVPC`) and private API visibility
(`ApiGatewayVisibility`).

**Yours entirely, in your account.** These have no resource in any template and no
parameter to set, so a default deployment does not do them at all:

| Responsibility | Why it is yours |
|---|---|
| Confirming the alerts SNS subscription, and adding any further subscribers | Fourteen of the fifteen alarms publish to `AlertsTopic`, and the stack now subscribes the `AdminEmail` address to it — but an SNS email subscription is created in `PendingConfirmation` and delivers nothing at all until the recipient clicks the link in the confirmation email, so confirming it is yours. Read the topic's subscription list in the console or with `aws sns list-subscriptions-by-topic` rather than assuming, because a `PendingConfirmation` subscription looks like coverage and is not. The `--headless` variant has no `AdminEmail` parameter, so it creates no subscription at all and the whole topic is yours to wire up. Any additional operator address, chat webhook or existing operational topic is yours to attach either way |
| Setting an AWS Budget and spend or token-volume alarms | There is no `AWS::Budgets` resource in any template and none of the fifteen alarms is a cost alarm. The metering ledger measures spend after the fact; it does not cap it |
| Enabling MFA on the Cognito user pool | The pool sets a password policy but no `MfaConfiguration`, so MFA is at the Cognito default of off |
| Choosing and configuring WAF rules beyond IP allow-listing | The optional WebACL contains a single IP-allow rule; AWS Managed Rules, rate-based rules and bot control are not configured |
| Choosing log group retention and reviewing what is logged | `LogRetentionDays` sets a default, but custom-resource Lambdas keep CloudWatch's auto-created groups with indefinite retention |
| Enabling CloudTrail and deciding where its trail is stored | No template creates a trail. CloudWatch Logs record application behavior, not the AWS API activity an audit needs |
| Reviewing the optional CloudFormation deployment service role before delegating it | `iam-roles/cloudformation-management/` is a convenience for non-administrative deployers and grants broad IAM permissions; it is not part of the runtime data plane |
| Data classification, residency, and retention obligations for the documents you process | Only you know what the documents contain |
| Defining and testing recovery objectives | See [Disaster Recovery](#disaster-recovery) |

## 1. Operational Excellence

### What the solution implements

The entire solution is defined as code in AWS SAM and CloudFormation: a parent
`template.yaml` plus nested stacks for the processing pipeline
(`patterns/unified/template.yaml`), the UI API (`nested/api-resolvers/template.yaml`),
the optional knowledge base (`nested/bedrockkb/`), and multi-document discovery
(`nested/multi-doc-discovery/`). Deployment is reproducible from source through
`publish.py` or the `idp-cli deploy` command.

Monitoring is concrete rather than aspirational. 16 `AWS::CloudWatch::Alarm`
resources are declared in `template.yaml`, and all alerting for the whole solution runs
through them — the nested stacks declare none. Fifteen of the sixteen alarms publish to
the `AlertsTopic` SNS topic; the sixteenth, `BedrockServiceOutageAlarm`, publishes to
`CircuitBreakerTopic`
and is the only conditional one, so it exists only when you enable the circuit breaker.
The other fifteen are unconditional, which is why a default deployment has exactly
fifteen. They fall into five groups:

| Alarm | What it detects |
|---|---|
| `WorkflowErrorsAlarm`, `WorkflowTimeoutsAlarm`, `SlowExecutionsAlarm` | Step Functions `ExecutionsFailed` above `ErrorThreshold` (default 1), any `ExecutionsTimedOut`, and `ExecutionTime` above `ExecutionTimeThresholdMs` (default 300000) |
| `DocumentQueueDLQAlarm`, `WorkflowTrackerDLQAlarm`, `QueueSenderDLQAlarm`, `DataMartRollupDLQAlarm` | Any visible message on a dead-letter queue |
| `DocumentQueueStalledAlarm` | A metric-math expression that fires only when the oldest message exceeds `QueueStalledAgeThresholdSeconds` (default 1800) *and* zero messages left the queue over six consecutive five-minute periods — a queue that is not draining, as distinct from one that is merely deep |
| `QueueProcessorErrorsAlarm`, `ConcurrencyCounterDriftAlarm`, `ConcurrencyCounterUnderflowAlarm`, `ConcurrencyCounterNegativeAlarm`, `StaleOutputPurgeFailedAlarm` | Lambda errors on the queue processor; a concurrency counter that has drifted from the true running-execution count across three periods; the counter being asked to release a slot it did not hold, which means the same terminal execution was processed twice; the counter actually going negative, which raises the effective concurrency ceiling by that much and costs money silently; and a failed stale-output purge, after which a document can carry text from a previous document of the same name |
| `AssessmentConfidenceUnavailableAlarm` | `ConfidenceUnavailableThreshold` (default ten) or more document sections degraded to "no confidence scores" in fifteen minutes. This is the one alarm here that watches a *successful* outcome: a deterministic confidence-model failure keeps the extraction and degrades the section rather than failing the document, so a systemic confidence failure produces no failed executions and nothing else on this list moves. It alarms on volume rather than on the first occurrence because one degraded section is an expected, self-limiting outcome |
| `AgentTranscriptMessageDroppedAlarm` | Ten or more agent conversation messages dropped from the stored transcript in fifteen minutes. Like the row above it watches an outcome the agent itself reports as success — the user gets their answer and the workflow completes; what is lost is an entry in the transcript the analytics UI replays, so a conversation shows gaps. Sustained drops mean either contention on one job's record beyond what the bounded retry absorbs, or reads that keep failing |

Two `AWS::CloudWatch::Dashboard` resources are created: one in `template.yaml` covering
ingestion, queue depth, the concurrency counter and workflow outcomes, and one in
`patterns/unified/template.yaml` covering the per-service processing steps. See
[Monitoring](./monitoring.md).

Distributed tracing is instrumented, not merely recommended: twenty-two Lambda functions
across the two main templates — seven in `template.yaml` and fifteen in
`patterns/unified/template.yaml` — plus both state machines trace, and seven of the eight
optional `feature-platform/` extension templates set `Tracing: Active` in their `Globals`
section (`seller-entitlement-service` is the exception). In the two main templates the
mode is `!If [EnableXRayTracingCondition, Active, PassThrough]` rather than a literal, so
the single `EnableXRayTracing` parameter — default `true` — turns the whole stack's
tracing on or off; `scripts/tests/test_xray_tracing.py` fails a function that hardcodes it
instead. The extension templates are deployed independently, with their own parameters,
so that switch does not reach them. Note the boundary: the REST API stage does not enable
X-Ray, so an API-initiated call is traced from the Lambda inward rather than from the edge.

Logging verbosity is a single deploy-time parameter. `LogLevel` defaults to `WARN` and
applies across the Lambda functions and the API stage;
`scripts/tests/test_log_level_default.py` pins that default so it cannot silently regress
to `INFO`, because at `INFO` the accelerator can write presigned URLs, document contents
and PII into CloudWatch Logs. That gate accounts for every template in the tree declaring a `LogLevel` parameter,
discovered rather than listed: each one is either enforced at `WARN` or named in an
exemption with a recorded reason, so a new template cannot appear outside both.

Two exceptions to know about if you install extensions. The five catalog features
(`pii-anonymizer`, `idp-data-generator`, `confbench-testset`, and the two samples) pin
`LogLevel: INFO` in their own `feature.yaml`, which the console install flow passes
explicitly — so an installed extension logs at `INFO` regardless of what the host stack
is set to, and you should lower it on the extension's own stack for production. And
`idp-feature-cli deploy` passes the parameter only when `--log-level` is given, so pass
it there rather than relying on the manifest.

**Raise it deliberately, and lower it again.** Above `WARN`, handlers across the
solution log their invocation events. Known-sensitive keys are redacted before an event
is written — tokens, credentials and identity claims, by a single shared denylist
(`idp_common.utils.log_sanitizer`) that every handler either imports or carries a
byte-identical copy of — but a denylist cannot anticipate what a caller puts in a
free-text field, so an event can still carry document content or caller-supplied text.
That is exactly what makes `INFO` and `DEBUG` useful for diagnosis, and what makes them
unsuitable as a steady state.

So treat raising the level as scoped and temporary: raise it for a specific
investigation, gather what you need, and set it back to `WARN`. Note that the data written
while it was raised persists for the log group's whole retention period, so lowering the
level does not undo it. `LogRetentionDays` and the CMK-encrypted log groups described
above bound that exposure; the level is what creates it.

Be aware of what that safe default costs you in observability. The REST API stage's
structured JSON access log — which carries the authorizer status, WAF response code and
integration latency, and is the only thing that diagnoses a request rejected before it
reaches a resolver — is gated on `LogLevel` being `INFO` or `DEBUG`, so on a default
deployment it is **off**, as are the gateway's ERROR-level execution logs and the
per-stage progress lines. See
[Monitoring](./monitoring.md#loglevel--what-warn-turns-off). `LogRetentionDays` defaults
to 30 days.

Automated testing exists at several tiers and is described in full in
[Testing](./testing.md). An end-to-end integration suite lives in
`lib/idp_common_pkg/tests/integration/` behind the `@pytest.mark.integration` marker and
runs via `make -C lib/idp_common_pkg test-integration`; because it deploys real AWS resources it runs in the
GitLab `integration_tests` stage rather than on GitHub pull requests. Repeatable accuracy,
latency, token and cost measurement across a matrix of document sizes and configurations
is in `benchmarks/`, with sample documents in `samples/`.

### Review checklist

| Review item | Your comment | Owner | Date |
|---|---|---|---|
| Is an operator address subscribed to `AlertsTopic`, and has the subscription been confirmed? Who receives it out of hours? | | | |
| Have you tuned `ErrorThreshold`, `ExecutionTimeThresholdMs` and `QueueStalledAgeThresholdSeconds` to your document mix, or are you running the defaults? | | | |
| Is `LogLevel` still `WARN` or `ERROR` in this deployment? If it was raised to `INFO` or `DEBUG` for troubleshooting, was it lowered again? | | | |
| Does `LogRetentionDays` meet your retention obligation, and have you set retention on the custom-resource log groups that keep CloudWatch's indefinite default? | | | |
| Do you accept that the REST API stage is not X-Ray traced, or do you need to add stage tracing? Is `EnableXRayTracing` set the way you want it? It defaults to `true` and controls the Lambda functions as well as the state machines, so it is also the X-Ray cost lever. | | | |
| At the default `LogLevel=WARN` the API access log is off. Do you accept that, or do you need request-level API telemetry enough to raise the level and accept the PII exposure that comes with it? | | | |
| Who owns the runbook for a stalled queue, and has `DocumentQueueStalledAlarm` been exercised at least once? | | | |
| How do you validate a configuration change before it reaches production — the integration suite, the `benchmarks/` harness against your own corpus, or a separate stack? | | | |
| Do you deploy through a pipeline, and does an upgrade go to a non-production stack first? | | | |

## 2. Security

### What the solution implements

**Data protection at rest.** A single customer-managed KMS key,
`CustomerManagedEncryptionKey`, with `EnableKeyRotation: true`, encrypts the DynamoDB
tables, the S3 buckets via SSE-KMS, the SNS topics and the CloudWatch log groups. It is
referenced from the parent template and passed into the nested stacks, so there is one
key to audit and one key policy to review.

**Data protection in transit.** 32 resource policies deny requests where
`aws:SecureTransport` is false, and there is now no exception: in `template.yaml`, all
thirteen S3 bucket policies and all sixteen SQS queue policies carry the deny, plus one
further queue policy in `patterns/unified/template.yaml` and two in the optional
`feature-platform/idp-data-generator/` extension. If you deploy without that extension
the count you should see is 30. All thirteen buckets also set
`PublicAccessBlockConfiguration`, and twelve send server access logs to the logging
bucket.

**Who can get an account.** One parameter decides this, and it is easy to miss.
`AllowedSignUpEmailDomain` defaults to the empty string, which leaves the pool's
`AdminCreateUserConfig.AllowAdminCreateUserOnly` at `true`: self-registration through the
web UI is closed and an administrator has to create every user. Setting the parameter to a
domain — or a comma-separated list of them — flips that flag to `false` and turns on public
self-registration for anyone holding an address at those domains. The five
`AWS::Cognito::UserPoolGroup` resources are not assigned automatically, so a user who
registers that way starts in no group at all; read that together with the authorization
paragraph below, because a user in zero groups still reaches every operation declared
`groups: ANY`. Leave the default unless you intend open sign-up, and if you do set it, make
sure the domain is one you control.

**Authentication and authorization.** The web UI signs in against a Cognito user pool
whose password policy requires a minimum length of 8 with lowercase, uppercase, numeric
and symbol characters. The UI calls an API Gateway REST API — `HttpApi` in
`nested/api-resolvers/template.yaml`. All application traffic goes through one route,
`POST /op/{field}` (`HttpApiMethod`), which is guarded by a `COGNITO_USER_POOLS`
authorizer (`HttpApiAuthorizer`) validating the same JWT the browser holds and is then
dispatched to per-operation resolver Lambdas. That is not the only method on the API,
though. `HttpApiOptionsMethod` is an ordinary unauthenticated CORS preflight, and in
API Gateway hosting mode two further `AuthorizationType: NONE` methods —
`WebUIRootMethod` (`GET /`) and `WebUIProxyMethod` (`GET /{proxy+}`), both conditional on
`ServeWebUI` — serve the React bundle's `index.html` and hashed assets from the Web UI
bucket over the same stage, deliberately and with no JWT, because the browser has no
token until the app has loaded. Those routes serve static files only; see
[API Gateway Hosting](./apigateway-hosting.md).

Authorization on the `/op` route is not uniform, and the difference matters when you
classify your data. `scripts/api_rbac_expectations.yaml` is the declared source of truth
for it and `make api-test-static` fails if the code and that file drift apart. It covers
118 operations. 108 of them require Cognito group membership and 2
(`updateDiscoveryJobStatus`, `updateAgentJobStatus`) are reachable only by IAM
principals, rejecting every Cognito caller. 18 of those accept any assigned group
rather than a named subset — they are declared `ANY_GROUP`, which the build resolves into
the full list of groups `template.yaml` creates, so what they refuse is a caller an
administrator has not placed in any group. That set is every document read
(`getDocument`, `listDocuments`, `listDocumentsByDateRange`, `getDocumentCount`,
`listDocumentsDateHour`, `listDocumentsDateShard`, `listDocumentVersions`,
`getDocumentVersion`, `compareDocumentVersions`, `getStepFunctionExecution`,
`getFileContents`, `getFilePresignedUrl`, `queryKnowledgeBase`), the chat transcript read
`getChatMessages`, the processing-breaker badge `getCircuitBreakerStatus` (whose
`lastError` carries the pausing administrator's email address after a manual pause), and
three mutations (`deleteAgentJob`, `deleteChatSession`, `sendChatDocumentMessage`). The
document reads include the ones that return no extracted
value themselves, because an object key, a section's `s3://` URI, a state machine execution
ARN and a model-written description of a page's contents are each a step of one chain that
ends in extracted data, and that chain was measured composing end to end for a caller in no
group.

The remaining 8 are declared `groups: ANY`, which that file defines as any authenticated
Cognito user — including one in no group, which self-service sign-up produces when you set
`AllowedSignUpEmailDomain`. Two of those 8 narrowed further, by record ownership —
`getMyProfile` returns only the caller's own row, resolved from their token claims with no
argument, and `listChatSessions` can only address the caller's own DynamoDB partition. The
other 6 are not, so a valid session is the whole check: `getLatestPublishedVersion` (the
published release number, which is public), the two fine-tuning job reads and the three
feature-platform reads (`listInstalledFeatures`, `listCatalogFeatures`,
`checkFeatureEntitlement`) — so they disclose a public version number, what models this
deployment has trained and what optional features it has installed or is entitled to,
rather than anything derived from a document.

Three caveats on what the group floor does and does not buy you, and the third is the one
that bounds the other two. It is a check on *who may ask*, not on *which document they may
read*: a Viewer may read any document a Viewer can see, so if your documents need to be
private to their submitter or to a tenant, group membership is the wrong axis and no setting
of these declarations fixes it. The other controls on the file reads are real but are not
per-document either — `getFilePresignedUrl` and `getFileContents` resolve through
`_validate_bucket()` in
`nested/api-resolvers/src/lambda/get_file_contents_resolver/index.py`, which allow-lists
the stack's own buckets and so prevents reading arbitrary S3, not reading another user's
document.

And **the declarations above govern the API, not the buckets.**
`CognitoIdentityPoolSetRole` attaches a single `authenticated` role and declares no role
mappings, so group membership plays no part in which role a signed-in user assumes,
and that role — `CognitoAuthorizedRole` — grants `s3:GetObject`, `s3:GetObjectVersion` and
`s3:ListBucket` on the Input and Output buckets plus `kms:Decrypt` on the
customer-managed key to every authenticated user, group or no group. The web UI uses that
path deliberately: the file viewer defaults to signing in the browser, as do the page
thumbnails, the page-image viewer and the document export. So a caller who is refused
`getFileContents` can still read a **document** object. No `ANY` operation hands over an
object key any more, but that is not what bounds the role: `s3:ListBucket` on it enumerates
the buckets directly, with no API call at all. Narrowing this is a change to the
document-viewing data path — group-scoped identity pool role mappings, or a resolver-only
read path — and it has not been made; the two candidate shapes and their obstacles are set
out in
[Identity Pool group scoping](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/docs/planning/identity-pool-group-scoping-plan.md).
The buckets the per-user scope axes partition — Configuration and Test Set — are
deliberately **not** on that role, so `allowedConfigVersions` and `allowedTestSets` are not
reachable around; those objects are served only by resolvers that check the key against the
caller's scope. Decide whether the current posture is acceptable for your data
classification, and see [RBAC](./rbac.md).

The API Gateway REST transport replaced AWS AppSync entirely — there are no
`AWS::AppSync` resources in any template — see
[AppSync to REST migration](./migration-appsync-to-rest.md).

**Web application firewall.** The optional `ApiWafWebACL` is a REGIONAL WAFv2 WebACL
associated with the REST API stage. It is created only when `WAFAllowedIPv4Ranges` is
changed from its `0.0.0.0/0` default; its default action is `Block` and its single rule
allows the IPv4 ranges you supply. It is an IP allow-list, not a managed rule set: no AWS
Managed Rules, rate-based rules or bot control are configured. In CloudFront hosting mode
the distribution has no WebACL of its own. See
[API Gateway Hosting](./apigateway-hosting.md).

**IAM governance.** `PermissionsBoundaryArn` is an optional parameter (default empty)
threaded into every IAM role in the parent template, `patterns/unified/`,
`nested/api-resolvers/`, `nested/bedrockkb/`, `nested/multi-doc-discovery/` and the
feature-platform templates, so an organization whose SCPs mandate a boundary on all roles
can supply one at deploy time. `scripts/tests/test_iam_privilege_escalation.py` guards the
runtime role surface against privilege-escalation regressions.

Resource scoping is a separate question from boundaries, and it is the weaker of the two
here. Counting across the eleven templates that make up the solution and its optional
extensions, 125 IAM policy statements are written against `Resource: "*"` — 51 in
`template.yaml`, 40 in `patterns/unified/template.yaml`, 8 in
`nested/multi-doc-discovery/template.yaml`, and the remainder in the other nested stacks,
`iam-roles/` and `feature-platform/`. A large share of them are unavoidable, because the
API being called accepts no resource ARN: `cloudwatch:PutMetricData` alone accounts for 28
of the 125, and the X-Ray read actions, `textract:DetectDocumentText` and
`textract:AnalyzeDocument` are account-scoped in the same way. The rest have not been
audited statement by statement, so treat the number as a surface to review rather than as a
count of findings. A permissions boundary is the practical lever for narrowing whatever you
find without editing every policy, which is why the two belong in the same review.

**Content safety.** Bedrock Guardrails are supported but bring-your-own and off by
default: supply the id and version of a guardrail you created via `BedrockGuardrailId`
and `BedrockGuardrailVersion` (`BedrockGuardrailId` defaults to empty, which is what keeps
Guardrails off; `BedrockGuardrailVersion` defaults to `DRAFT`) and every Bedrock and
Knowledge Base call routes through it, including
[Automated Reasoning Checks](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-automated-reasoning-checks.html)
if your guardrail enables them.

**Network isolation.** Setting `DeployInVPC=true` places the document-processing Lambdas
in a VPC you supply, which is a prerequisite for `ApiGatewayVisibility=PRIVATE`. A
reference interface-endpoint stack is provided in `scripts/vpc-endpoints.yaml`. See
[Private Network Deployment](./deployment-private-network.md).

**Security in the build.** Two independent gates run on every GitHub pull request via
`.github/workflows/security-checks.yml` and on every GitLab push and merge request in
`fast_checks`. `make srt-scan` runs the Sample Security Review Tool for code and
infrastructure findings and fails on high-priority findings. `make dep-audit` matches
every pinned Python and Node dependency against the OSV database and fails on HIGH or
above — SRT's SBOM stage inventories dependencies but does no vulnerability matching, so
the two are not redundant. Triaged, unreachable advisories are recorded with a
justification in `scripts/security/dep_audit_allowlist.json`.

### Review checklist

| Review item | Your comment | Owner | Date |
|---|---|---|---|
| Who is allowed to create an account? Is `AllowedSignUpEmailDomain` still empty, keeping sign-up administrator-only, and if you have set it, do you control every domain listed? A self-registered user is in no group, so the API refuses them the document-content operations — but `CognitoAuthorizedRole` still grants them `s3:GetObject`/`ListBucket` on the document buckets directly | | | |
| Is MFA enabled on the Cognito user pool? The pool sets no `MfaConfiguration`, so a default deployment has it off | | | |
| Do you accept that the 6 `groups: ANY` operations carrying no ownership or scope check are reachable by any authenticated user, including one in no group? They are a public version number, the two fine-tuning job reads and the three feature-platform reads — nothing derived from a document. Separately, and not fixed by any of these declarations: `CognitoAuthorizedRole` grants every authenticated user `s3:GetObject`/`ListBucket` on the document buckets, so a direct S3 read reaches document bytes with no API call and no key from an API. Does that meet your data classification? | | | |
| Have you restricted `WAFAllowedIPv4Ranges`, and if the API is reachable from the internet, have you added AWS Managed Rules and a rate-based rule beyond the IP allow-list? | | | |
| Have you supplied a `PermissionsBoundaryArn`, and does your organization require one? | | | |
| Is the 123-statement `Resource: "*"` surface acceptable under your service control policies, and have you reviewed the statements that are not forced by an account-scoped API? | | | |
| Have you created a Bedrock Guardrail and set `BedrockGuardrailId` / `BedrockGuardrailVersion`, or accepted running without content policy enforcement? | | | |
| Have you reviewed the `CustomerManagedEncryptionKey` key policy, and do you need a key you manage outside the stack instead? | | | |
| Is CloudTrail enabled in this account and region, and where does the trail go? No template creates one | | | |
| Is the deployment path a delegated CloudFormation service role from `iam-roles/cloudformation-management/`? If so, who has reviewed its permissions and who can assume it? | | | |
| Does your document data classification require `DeployInVPC=true` and `ApiGatewayVisibility=PRIVATE`, or VPC endpoints for Bedrock, Textract and S3? | | | |
| In CloudFront hosting mode, have you attached a custom domain with an ACM certificate, enforced TLS 1.2 or greater, set security response headers, and considered a GLOBAL-scope WebACL in us-east-1? | | | |
| Have you reviewed what appears in the log groups at your chosen `LogLevel`, given that the documents processed may contain PII? | | | |
| Do you need [Amazon Macie](https://docs.aws.amazon.com/macie/latest/user/what-is-macie.html) on the input and output buckets to discover and classify sensitive data? Macie is decoupled and needs no change to the accelerator | | | |
| Who reviews the SRT and dependency-audit findings on your fork, and is each check a required status check on your default branch? | | | |

## 3. Reliability

### What the solution implements

**Retry posture.** The state machine definition in
`patterns/unified/statemachine/workflow.asl.json` contains 24 `Retry` blocks and 10
`Catch` blocks. Retries target the transient Lambda error classes
(`Lambda.ServiceException`, `Lambda.TooManyRequestsException`, `Lambda.SdkClientException`,
`Lambda.AWSLambdaException`, `Lambda.Unknown`) plus `States.Timeout`, with `MaxAttempts`
as low as 1 where a timeout is deterministic rather than transient and up to 8 for a
transient service error, initial intervals of 2 to 10 seconds and `BackoffRate`
between 2 and 2.5. Independently of Step Functions, the Bedrock client in
`lib/idp_common_pkg/idp_common/bedrock/client.py` applies its own ladder —
`DEFAULT_MAX_RETRIES = 7`, an initial backoff of 2 seconds and a cap of 300 seconds — for
throttling and service errors. The two ladders compose, so a single document can absorb
many model invocations before it fails; that is what makes the cost review item in the
Cost Optimization pillar a real question rather than a formality.

**Dead-letter queues.** Almost every SQS consumer has a DLQ — the exception is
`TestResultCacheUpdateQueue`, which carries no `RedrivePolicy` — and the queues that do
have one set a `maxReceiveCount` chosen for the work they hold. Exactly four queues declare
a redrive policy:

| Queue | `VisibilityTimeout` | `maxReceiveCount` | Retry window before a message is parked |
|---|---|---|---|
| `DiscoveryQueue` | 900s | 1000 | roughly 250 hours |
| `DocumentQueue` | 60s | 500 | roughly 8 hours |
| `TestFileCopyQueue` | 900s | 3 | roughly 45 minutes |
| `TestSetFileCopyQueue` | 900s | 3 | roughly 45 minutes |

The retry window is the product of the two columns and is an upper bound: it is how long a
message can keep being redelivered, not how long processing actually takes. Note what is
*not* in that table. The workflow tracker has no SQS redrive policy at all —
`WorkflowTrackerDLQ` is the Lambda `DeadLetterQueue` target of the `WorkflowTracker`
function, so it receives an asynchronous invocation that Lambda has already retried twice,
which is a different and far shorter mechanism than five hundred queue redeliveries. The
other queues named `...DLQ` work the same way: `QueueSenderDLQ`, `JobTrackerDLQ` and
`PostProcessingDecompressorDLQ`, plus `BDACompletionFunctionDLQ` in
`patterns/unified/template.yaml`, are Lambda dead-letter targets, and `DataMartRollupDLQ`
is an asynchronous-invocation `OnFailure` destination capped at
`MaximumRetryAttempts: 2`. So when you plan a redrive procedure, check which of the two
mechanisms parked the message: only the four queues above are governed by
`maxReceiveCount`. Four of the fifteen alarms watch DLQs for any visible message.

**Circuit breaker.** An opt-in circuit breaker for Bedrock outages is available via
`CircuitBreakerEnabled` (default `"false"`). When enabled, `BedrockServiceOutageAlarm`
notifies `CircuitBreakerTopic`, which invokes `CircuitBreakerManagerFunction` to pause
admission of new workflows; an EventBridge rule runs the manager every five minutes so
recovery timeouts always converge rather than requiring a manual reset. While the breaker
holds messages without deleting them, `DocumentQueueStalledAlarm` also fires and then
clears on its own — expected behavior, not a second fault. See
[Circuit Breaker](./circuit-breaker.md).

**Decoupling and fault isolation.** SQS queues buffer ingestion from processing, so a
downstream failure or a Bedrock throttle backs up in a queue rather than dropping work.
The nested-stack split keeps a pipeline change from touching the ingestion, tracking and
UI resources. It is also what buys room to grow: `template.yaml` declares 315 top-level
resources against CloudFormation's hard limit of 500 per stack, so if you plan to extend
the solution through the `feature-platform/` mechanism, that remaining budget is the number
to watch, and a new extension is better added as its own nested stack than as more
resources in the parent.

**Durable state.** All thirteen S3 buckets have versioning enabled. Ten of the twelve
DynamoDB tables a default deployment creates have point-in-time recovery enabled. Those
twelve are ten in `template.yaml`, one in `patterns/unified/template.yaml` and one in
`nested/api-resolvers/template.yaml`. Both exceptions are deliberate and both hold
ephemeral state: `ConcurrencyTable` holds a single admission counter that is reconciled
from the true running-execution count rather than restored, and
`ChatDocumentSessionsTable` holds per-session chat-ownership records under a short TTL, so
losing it only forces users to start a new chat session. Counting the optional
`feature-platform/` extensions raises the total to nineteen tables; all seven extension
tables have point-in-time recovery enabled.

### Review checklist

| Review item | Your comment | Owner | Date |
|---|---|---|---|
| Have you defined an explicit RTO and RPO for document processing, and does one of the strategies below meet them? | | | |
| Have you enabled `CircuitBreakerEnabled`, or accepted that a Bedrock outage will drive the retry ladders to exhaustion? | | | |
| Who monitors the dead-letter queues, and what is the procedure for redriving a parked document? | | | |
| Have you tested restoring a document from S3 version history and a table from point-in-time recovery, rather than assuming both work? | | | |
| Do you accept no point-in-time recovery on `ConcurrencyTable` and `ChatDocumentSessionsTable`, given that both hold ephemeral state that is reconciled or re-created rather than restored? | | | |
| Are the composed retry ladders — up to 8 Step Functions attempts over up to 7 Bedrock client retries — acceptable for your latency budget and your spend ceiling? | | | |
| Have you confirmed your chosen Bedrock models, Textract, and any Bedrock Data Automation projects are available in every region you intend to fail over to? | | | |
| Have you rehearsed the recovery procedure end to end, and when? | | | |

### Disaster Recovery

The capabilities above form the foundation of a DR strategy but do not by themselves
constitute one, because nothing in the stack replicates data across regions.

- **Durable, versioned storage**: all S3 buckets have versioning enabled, so objects
  survive accidental overwrite or deletion and prior versions can be recovered.
- **Point-in-Time Recovery**: ten of the twelve DynamoDB tables a default deployment
  creates can be restored to any second within the retention window. The two without it are
  `ConcurrencyTable`, whose admission counter is reconciled from the true
  running-execution count rather than restored, and `ChatDocumentSessionsTable`, whose
  per-session chat-ownership records sit under a short TTL. Neither holds document data, so
  neither is on the recovery path for the documents you process.
- **Infrastructure as Code**: the whole stack can be re-provisioned in another account or
  region from source.
- **Stateless compute**: Lambda and Step Functions hold no durable state, so recovery is
  restoring S3 and DynamoDB plus redeploying the templates.
- **Lifecycle rules**: eleven of thirteen buckets carry lifecycle rules, most keyed to
  `DataRetentionInDays` (default 365), with the MCP temp bucket at 7 days and the logging
  bucket at 180. Objects deleted by a lifecycle rule are gone from the DR picture too, so
  align retention with your RPO.

**Choosing a strategy.** The right approach depends on the RTO and RPO you recorded in
the checklist above:

- **Backup and restore** (lowest cost, higher RTO): rely on S3 versioning and DynamoDB
  PITR within a region. For cross-region protection, enable
  [S3 Cross-Region Replication](https://docs.aws.amazon.com/AmazonS3/latest/userguide/replication.html)
  on the document and configuration buckets and use
  [DynamoDB backups](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Backup-and-Restore.html)
  (optionally via [AWS Backup](https://docs.aws.amazon.com/aws-backup/latest/devguide/whatisbackup.html))
  copied to a DR region, then redeploy the stack there when needed.
- **Pilot light or warm standby** (lower RTO, higher cost): pre-deploy the stack in a
  second region and replicate continuously with S3 CRR and
  [DynamoDB global tables](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GlobalTables.html).
  Fail over by redirecting ingestion to the standby region's input bucket.
- **Multi-region active/active**: independent stacks behind a routing layer. Lowest
  RTO/RPO, most operational and cost complexity, and it requires that your models and any
  Bedrock Data Automation projects exist in every target region — see
  [EU Region Model Support](./eu-region-model-support.md) for how region-specific model
  availability actually behaves.

## 4. Performance Efficiency

### What the solution implements

**Admission control.** Throughput is bounded deliberately rather than left to Lambda's
account concurrency. `MaxConcurrentWorkflows` (default 100, `MinValue: 1`, no maximum)
caps concurrent Step Functions executions. The queue processor in
`src/lambda/queue_processor/index.py` increments a counter row in `ConcurrencyTable` under
a DynamoDB `ConditionExpression` of `active_count < :max`, so the cap is enforced
atomically rather than by an approximate read. Because a counter that leaks would pin the
system at zero admission permanently, the processor also reconciles the counter against
the true running-execution count and `ConcurrencyCounterDriftAlarm` fires when a suspected
leak persists across three periods. See [Capacity Planning](./capacity-planning.md).

**Asynchronous, buffered processing.** SQS decouples ingestion from processing so that a
burst of uploads becomes queue depth rather than throttling, and Step Functions map states
parallelize per-page and per-section work within a document.

**Model-aware sizing.** Shard and batch sizes for classification, extraction and
confidence scoring are derived from the selected model's context and output limits in
`config_library/model_config_limits.yaml` rather than being fixed, so changing the model
changes the work partitioning without a separate tuning pass.

**Measurement.** The two dashboards and the latency alarms above give the operational
view. For deliberate comparison of configurations, the `benchmarks/` harness measures
completeness, accuracy, confidence calibration, latency, token use and cost across a
document-size and configuration matrix, which is the mechanism for answering "is this
change faster or cheaper" with numbers instead of impressions.

### Review checklist

| Review item | Your comment | Owner | Date |
|---|---|---|---|
| Is `MaxConcurrentWorkflows` sized to your Bedrock and Textract quotas rather than left at 100? An admission cap above your model quota converts a queue into throttling | | | |
| Have you requested Bedrock and Textract quota increases for your expected peak, and do you know the current limits? | | | |
| What is your target end-to-end latency per document, and have you measured it on your own documents rather than the bundled samples? | | | |
| Have you chosen models per document class deliberately, or is every class using the default? | | | |
| For documents with large tables, have you evaluated agentic extraction with table parsing against your accuracy and latency targets? | | | |
| Do you have a baseline `benchmarks/` run against your own corpus to compare future upgrades to? | | | |
| Is your deployment region chosen for model availability and latency to your users, and have you confirmed the models you want exist there? | | | |
| Does queue depth ever reach the point where `DocumentQueueStalledAlarm` fires from capacity shortfall rather than a wedge, and if so is the answer more concurrency or a higher threshold? | | | |

## 5. Cost Optimization

### What the solution implements

**Pay-per-use.** All compute is serverless — Lambda, Step Functions, Textract and Bedrock
on-demand — so an idle deployment costs storage and log retention rather than capacity.

**Per-invocation metering.** Every model and OCR call records a metering row with token
counts, and the reporting path computes an `estimated_cost` per row from
`config_library/pricing.yaml` — cache-aware, so prompt-cache reads and writes are priced
separately from fresh input tokens — and writes it to Parquet for Athena. That makes cost
queryable per document, per document class and per processing step rather than only as a
monthly bill line. See [Cost Calculator](./cost-calculator.md) and
[Reporting Database](./reporting-database.md). Prices in `pricing.yaml` are estimates and
editable, including from the UI, so a private pricing agreement can be reflected.

**Cost attribution.** Bedrock
[Application Inference Profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-create.html)
can tag model invocations for attribution in Cost Explorer; see
[Cost Attribution](./cost-calculator.md#cost-attribution-with-bedrock-application-inference-profiles).

**Storage lifecycle.** Eleven of thirteen buckets carry lifecycle rules keyed to
`DataRetentionInDays` (default 365), and `LogRetentionDays` (default 30) bounds log
storage.

**Be clear about the limit of all this.** The instrumentation is a ledger, not a control.
It tells you what a document cost after it was processed. There is no `AWS::Budgets`
resource in any template, no Cost Explorer anomaly monitor, and none of the fifteen alarms
is a spend or token-volume alarm. The only ex-ante levers are `MaxConcurrentWorkflows`,
which limits rate rather than spend, and the circuit breaker, which trips on Bedrock
availability rather than on cost. Combined with the composed retry ladders described under
Reliability, a misconfigured document class can generate substantially more spend than
expected before anyone notices, which is why the first two checklist rows below are the
most important in this document.

### Review checklist

| Review item | Your comment | Owner | Date |
|---|---|---|---|
| Have you set an AWS Budget with alerts for this stack's account or cost allocation tag? Nothing in the templates creates one | | | |
| Have you created a CloudWatch alarm on token volume or on the metered cost metric, so that runaway spend is detected in hours rather than at month end? | | | |
| Have you enabled Cost Anomaly Detection for Bedrock and Textract in this account? | | | |
| Have you validated `config_library/pricing.yaml` against your actual rates, including any private pricing agreement? Otherwise every reported cost is off by a constant factor | | | |
| Do you know the cost per document for each of your document classes, and does it match your business case? | | | |
| Have you evaluated cheaper models for the classes where accuracy allows it, rather than using one model everywhere? | | | |
| Is prompt caching effective for your prompts, and have you checked rather than assumed? A prefix below the model's caching minimum is never cached | | | |
| Does `DataRetentionInDays` (default 365) match what you actually need to keep, and have you considered a storage-class transition for older output? | | | |
| Have you applied cost allocation tags so that this workload is separable in Cost Explorer? | | | |

## 6. Sustainability

### What the solution implements

Serverless compute consumes resources only while documents are being processed, so
utilization tracks demand without idle capacity. The concurrency cap and queue buffering
mean work is smoothed rather than run against over-provisioned headroom. Lifecycle rules
on eleven of thirteen buckets and the `LogRetentionDays` default keep stored data from
growing without bound.

Some Lambda functions already run on arm64 — four in `patterns/unified/template.yaml` and
three in `template.yaml`, plus the feature-platform functions — while the remainder take
the x86_64 default. Migrating a function is not always free, because container-image
functions must be built for the target architecture and some Python wheels are not
published for arm64.

Region choice is the largest single lever available to you, and it is constrained: the
region must offer the Bedrock models, Textract features and Bedrock Data Automation
projects your configuration uses.

We do not ship a carbon metric. The
[Customer Carbon Footprint Tool](https://docs.aws.amazon.com/help-panel/awsaccountbilling/latest/console/hp-ccft.html)
reports at the account level, so attributing emissions to this workload specifically
requires that you separate it by account or cost allocation tag.

### Review checklist

| Review item | Your comment | Owner | Date |
|---|---|---|---|
| Is your region choice compatible with a lower-carbon region, given the Bedrock and Textract features your configuration requires? | | | |
| Have you archived or expired processed documents you no longer need, rather than relying on the 365-day default? | | | |
| Have you evaluated arm64 for the functions still on x86_64 in your deployment, accounting for the container-image and wheel-availability constraints? | | | |
| Are you re-processing documents unnecessarily — for example re-running OCR when only the extraction prompt changed? | | | |
| Have you right-sized image preprocessing resolution, which drives both token count and compute? | | | |
| Can you attribute this workload's footprint in the Customer Carbon Footprint Tool, or does it need its own account or tag to be separable? | | | |

## Processing-mode considerations

Since v0.5.0 the solution deploys as a single unified stack containing both processing
modes. The `use_bda` configuration flag, set in the UI, selects the path at runtime;
there is no deployment-time pattern selector. See the
[Architecture Overview](./architecture.md) and
[Upgrading to the Unified Pattern](./migration-v04-to-v05.md).

**BDA mode (`use_bda: true`)** delegates OCR, classification and extraction to the managed
Amazon Bedrock Data Automation service, which reduces the operational surface you own but
moves your throughput ceiling to BDA's service quotas and your cost model to BDA's pricing
rather than per-token pricing. Review both quotas before committing.

**Pipeline mode (`use_bda: false`, the default)** separates OCR on Amazon Textract from
classification and extraction on Amazon Bedrock. You own more configuration — model per
step, prompts, shard sizes, whether agentic extraction is enabled — and correspondingly
more of the cost and accuracy outcome.

> **Note**: the separate Pattern-3 configuration (Textract, a SageMaker UDOP endpoint, and
> Bedrock) was removed in v0.5.0. Custom classification models such as UDOP can be
> integrated through [Lambda Inference Hooks](./lambda-hook-inference.md).

## Recording the outcome

A completed review is a set of filled rows with owners and dates, plus a short list of
the items you decided not to act on and why. The rows that most often turn out to matter
in practice are the ones where a default deployment does nothing at all: nobody
subscribed to the alerts topic, no budget, and MFA off on the user pool. Those three are
worth confirming before the first production document is processed, not at the next
review.

Re-run the checklist on each upgrade of the accelerator. Parameter defaults and the
resources described in this document can change between releases, so an answer recorded
against an earlier version may no longer describe your stack — which is what the date
column is for.
