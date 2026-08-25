# SRT — SAST & Dependency Scan

Static analysis (Bandit, Semgrep, Checkov), dependency inventory (Syft), and the security-matrix review, aggregated by the [Sample Security Review Tool](https://github.com/aws-samples/sample-security-review-tool). The gate is HIGH findings that are **open or reopened** (i.e. not suppressed or resolved); lower tiers are reported as counts (they are dominated by tracked third-party/vendored code).

## Summary

- **Gate (open/reopened HIGH findings):** PASS ✅
- **CI-visible findings:** 11277
- **Source:** live scan results (`.srt/issues.json`) — 271 git-ignored finding(s) excluded to match the CI view

## Analyzers executed

Each analyzer SRT ran, what it covers, and its contribution to the CI-visible findings.

| Analyzer | Coverage | Findings | HIGH |
|----------|----------|---------:|-----:|
| Bandit | Python SAST | 10976 | 54 |
| Checkov | IaC / CloudFormation misconfig | 161 | 2 |
| Semgrep | multi-language SAST | 87 | 1 |
| security-matrix | AWS security-control review (SRT rules) | 53 | 47 |

## Findings by priority × status

| Priority | Open | resolved | suppressed | Total |
|----------|------:|------:|------:|------:|
| HIGH | 0 | 58 | 46 | 104 |
| MEDIUM | 99 | 0 | 0 | 99 |
| LOW | 11055 | 9 | 0 | 11064 |
| INFO | 10 | 0 | 0 | 10 |

## HIGH findings by check (disposition)

Every HIGH check-ID flagged, with how many are in each status. A green gate means all are resolved or suppressed (0 Open and 0 reopened).

| Source | Check | Open | resolved | suppressed |
|--------|-------|--:|--:|--:|
| Bandit | `B105` | 0 | 44 | 0 |
| Bandit | `B106` | 0 | 5 | 0 |
| Bandit | `B602` | 0 | 3 | 0 |
| Bandit | `B701` | 0 | 2 | 0 |
| Checkov | `CKV_AWS_192` | 0 | 2 | 0 |
| Semgrep | `package_managers.npm.npm-missing-minimum-release-age.npm-missing-minimum-release-age` | 0 | 0 | 1 |
| security-matrix | `API-GW-001` | 0 | 0 | 1 |
| security-matrix | `API-GW-002` | 0 | 0 | 1 |
| security-matrix | `API-GW-004` | 0 | 0 | 2 |
| security-matrix | `API-GW-006` | 0 | 0 | 1 |
| security-matrix | `DDB-002` | 0 | 1 | 19 |
| security-matrix | `EC2-002` | 0 | 0 | 3 |
| security-matrix | `IAM-009` | 0 | 0 | 1 |
| security-matrix | `KMS-007` | 0 | 0 | 6 |
| security-matrix | `LAMBDA-004` | 0 | 0 | 1 |
| security-matrix | `LAMBDA-005` | 0 | 0 | 1 |
| security-matrix | `LAMBDA-011` | 0 | 0 | 1 |
| security-matrix | `LAMBDA-012` | 0 | 1 | 1 |
| security-matrix | `S3-001` | 0 | 0 | 2 |
| security-matrix | `S3-005` | 0 | 0 | 1 |
| security-matrix | `S3-008` | 0 | 0 | 4 |

## Suppressed HIGH findings (accepted risk / scanner limitation)

Each carries a recorded justification; the authoritative register is `scripts/srt/issues.json`.

| Source | Check | Path | Justification |
|--------|-------|------|---------------|
| security-matrix | `KMS-007` | `notebooks/examples/demo-lambda/template.yml` | Accepted: KMS key event monitoring (CloudTrail/EventBridge/alarms) is account-level infrastructure owned by the deploying customer, out of scope for the solution template. |
| security-matrix | `DDB-002` | `patterns/unified/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `EC2-002` | `scripts/sdlc/cfn/sdlc-iam-role.yml` | Accepted: BuilderRole is SDLC pipeline scaffolding for dev accounts and intentionally broad to build/publish all solution artifacts. |
| security-matrix | `S3-001` | `scripts/sdlc/cfn/s3-sourcecode.yml` | Accepted: SDLC bootstrap scaffolding deployed in dev accounts; access logging omitted for short-lived pipeline source bucket. |
| security-matrix | `S3-008` | `scripts/sdlc/cfn/s3-sourcecode.yml` | Accepted: SDLC pipeline scaffolding bucket in solution-builder dev accounts; retention of pipeline source/artifact objects is the pipeline owner's decision, and a lifecycle policy here is cost optimization rather than a security control. |
| security-matrix | `KMS-007` | `scripts/sdlc/cfn/codepipeline-s3.yml` | Accepted: KMS key event monitoring (CloudTrail/EventBridge/alarms) is account-level infrastructure owned by the deploying customer, out of scope for the solution template. |
| security-matrix | `IAM-009` | `scripts/sdlc/cfn/codepipeline-s3.yml` | Accepted: CodeBuild role in SDLC dev-account scaffolding; a permissions boundary is the account owner's policy decision, the template cannot assume one exists. |
| security-matrix | `S3-001` | `scripts/sdlc/cfn/codepipeline-s3.yml` | Accepted: SDLC pipeline artifact bucket in dev accounts; access logging omitted for pipeline scaffolding. |
| security-matrix | `S3-008` | `scripts/sdlc/cfn/codepipeline-s3.yml` | Accepted: SDLC pipeline scaffolding bucket in solution-builder dev accounts; retention of pipeline source/artifact objects is the pipeline owner's decision, and a lifecycle policy here is cost optimization rather than a security control. |
| security-matrix | `KMS-007` | `template.yaml` | Accepted: KMS key event monitoring (CloudTrail/EventBridge/alarms) is account-level infrastructure owned by the deploying customer, out of scope for the solution template. |
| security-matrix | `S3-008` | `template.yaml` | Accepted: bucket retention is a customer data-governance decision (evaluation baselines, working documents); imposing a lifecycle policy in the template risks deleting customer data. |
| security-matrix | `S3-005` | `template.yaml` | False positive at scan time: WebUIBucket is fronted by CloudFront with an OriginAccessControl (sigv4, SigningBehavior always) plus a bucket policy conditioned on the distribution ARN when CloudFront hosting is enabled; the conditional resources cannot be evaluated statically. |
| security-matrix | `S3-008` | `template.yaml` | Accepted: WebUIBucket holds only the current built web-app bundle, redeployed in full by CodeBuild on each update; there is no object accumulation to manage, and an expiry lifecycle rule could delete live UI assets. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `LAMBDA-004` | `nested/bedrockkb/template.yaml` | Accepted: X-Ray tracing intentionally omitted on helper/custom-resource Lambdas; enabling tracing is a customer deployment choice for this sample solution. |
| security-matrix | `LAMBDA-011` | `nested/bedrockkb/template.yaml` | Accepted: CloudWatch alarms for helper/custom-resource Lambdas are intentionally omitted; failures surface via CloudFormation stack status. Alarm topology is a customer operations decision. |
| security-matrix | `LAMBDA-012` | `nested/bedrockkb/template.yaml` | Accepted (scanner false positive): StartIngestionJobFunctionRole is referenced exactly once in this template (StartIngestionJobFunction, Role: !GetAtt "StartIngestionJobFunctionRole.Arn") and by nothing else - grep confirms a single consumer, and every Lambda in nested/bedrockkb/template.yaml has its own distinct role (S3VectorManagerRole, OpenSearchLambdaExecutionRole, StartIngestionJobFunctionRole). The check's own generated fix text names no second function ("...sharing role GetAtt:StartIngestionJobFunctionRole with ." - blank), i.e. the scanner found no actual sharer. The 1:1 function-to-role relationship the check asks for already holds. Disposition changed resolved -> suppressed because 'resolved' is not sticky: SRT re-detects the finding on every scan and reopens it. |
| security-matrix | `KMS-007` | `template.yaml` | Accepted: KMS key event monitoring (CloudTrail/EventBridge/alarms) is account-level infrastructure owned by the deploying customer, out of scope for the solution template. |
| security-matrix | `EC2-002` | `template.yaml` | False positive: BastionRole (Condition ShouldDeployBastionHost, off by default) carries exactly the managed policy the finding's own fix recommends - AmazonSSMManagedInstanceCore - and nothing else; the instance profile attaches only that role. No excess privilege exists. |
| security-matrix | `EC2-002` | `template.yaml` | False positive: BastionRole (Condition ShouldDeployBastionHost, off by default) carries exactly the managed policy the finding's own fix recommends - AmazonSSMManagedInstanceCore - and nothing else; the instance profile attaches only that role. No excess privilege exists. |
| Semgrep | `package_managers.npm.npm-missing-minimum-release-age.npm-missing-minimum-release-age` | `src/ui/.npmrc` | Accepted (repo-wide policy decision, mitigation deferred): UI dependencies are pinned by src/ui/package-lock.json and installed with `npm ci`, which resolves nothing from the registry, so a newly published malicious version cannot enter a build from this config. Lockfile bumps arrive via Dependabot PRs that are reviewed before merge. The mitigation the rule asks for (`min-release-age = 7`) requires npm >= 11.10; the toolchain pinned by src/ui/package.json engines (npm >= 10) and the npm in the build image (11.1) predate it, so setting the key today only emits an 'Unknown user config' warning on every install without adding protection. Revisit and set it once the build npm is >= 11.10. Disposition changed resolved -> suppressed because 'resolved' is not sticky: semgrep keys on (path, line, issue) and the rule's message text changes, so each rule-text update reopens the finding. |
| security-matrix | `DDB-002` | `feature-platform/main-stack-extensions/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `feature-platform/sample-health-insurance-review/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `nested/api-resolvers/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `API-GW-002` | `nested/api-resolvers/template.yaml` | Accepted: REST API in Lambda-proxy mode; the data-plane POST method is Cognito-authorized and the dispatcher Lambda validates each request body per-field, and the remaining method is the auth-less MOCK OPTIONS CORS preflight (static 200, no backend). API GW request validators add no value for opaque proxy payloads. |
| security-matrix | `API-GW-004` | `nested/api-resolvers/template.yaml` | Accepted: intentional public static-asset route (enabled only when ServeWebUI=true). This GET serves the React SPA shell (index.html) from S3 to the browser BEFORE the user authenticates in-app; auth happens later against the Cognito-authorized /op POST. A JWT cannot be attached to the browser's initial document fetch, so AuthorizationType NONE is required, and only non-sensitive static UI files are exposed. Defense-in-depth: the stage-level WAFv2 IP allowlist (IsWafEnabled) and the PRIVATE-endpoint VPC policy (UsePrivateApi) still gate this route. |
| security-matrix | `API-GW-004` | `nested/api-resolvers/template.yaml` | Accepted: intentional public static-asset route (enabled only when ServeWebUI=true). This GET /{proxy+} serves hashed SPA static assets (js/css/fonts/images) from S3 to the browser BEFORE the user authenticates in-app; auth happens later against the Cognito-authorized /op POST. A JWT cannot be attached to the browser's asset fetches, so AuthorizationType NONE is required, and only non-sensitive static UI files are exposed. Defense-in-depth: the stage-level WAFv2 IP allowlist (IsWafEnabled) and the PRIVATE-endpoint VPC policy (UsePrivateApi) still gate this route. |
| security-matrix | `API-GW-001` | `nested/api-resolvers/template.yaml` | Accepted (scanner limitation): the stage DOES configure AccessLogSetting with both a DestinationArn (HttpApiAccessLogGroup, KMS-encrypted, RetentionInDays=LogRetentionDays) and a JSON Format, but they are wrapped in Fn::If [EnableApiAccessLogs] (on at LogLevel INFO/DEBUG). SRT's security-matrix cannot resolve Fn::If, so it does not see the DestinationArn/Format inside the conditional branch. Access logging is correctly implemented; the conditionality is intentional to avoid log costs at higher LogLevels. |
| security-matrix | `API-GW-006` | `nested/api-resolvers/template.yaml` | Accepted (scanner limitation): the stage DOES set MethodSettings with LoggingLevel ERROR for ResourcePath '/*' HttpMethod '*', but wrapped in Fn::If [EnableApiAccessLogs]. SRT cannot resolve Fn::If and reads MethodSettings as a non-array, so it reports execution logging disabled. Execution logging is deliberately ERROR-only (INFO would echo full request/response payloads, incl. customer document data, into CloudWatch); JSON access logs cover request metadata. Logging is correctly implemented; the conditionality is intentional. |
| security-matrix | `DDB-002` | `feature-platform/idp-data-generator/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. Consistent with all other DynamoDB tables in this solution. |
| security-matrix | `LAMBDA-005` | `feature-platform/idp-data-generator/template.yaml` | Accepted: bedrock-agentcore:* on Resource '*' is required — CreateAgentRuntime transitively creates/deletes runtime endpoints and other sub-resources, so an action allow-list breaks create/delete; these control-plane APIs do not support resource-level permissions and the runtime ARN does not exist at create time. iam:PassRole/CreateServiceLinkedRole and logs are scoped. |
| security-matrix | `DDB-002` | `feature-platform/pii-anonymizer/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. Same rationale as every other table in the solution (TrackingTable, InstalledFeaturesTable, ClaimsStatusTable, ...). The audit table stores metadata only (never PII). |
| security-matrix | `DDB-002` | `feature-platform/confbench-testset/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. Same disposition as every other DynamoDB table in this solution. This table holds only ingest-job metadata (job id, tier, variant list, progress counters) for the ConfBench Test Set extension - no document content and no customer data. Encryption at rest (SSESpecification) and point-in-time recovery are both enabled, and rows expire via TTL. |
| security-matrix | `DDB-002` | `feature-platform/pii-anonymizer/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. Same rationale as every other table in the solution (TrackingTable, InstalledFeaturesTable, ClaimsStatusTable, ...). The mapping table is SSE-encrypted, PITR-enabled, and reachable only via the RBAC-gated feature API. |
| security-matrix | `KMS-007` | `feature-platform/seller-entitlement-service/template.yaml` | Accepted: KMS key event monitoring (EventBridge rule + monitoring Lambda + alarms) is account-level infrastructure owned by the account operator, out of scope for this template. Same rationale as the other KMS-007 records in this baseline. This service is deployed by an extension SELLER into their own Marketplace seller account, so KMS-event monitoring is theirs to configure against their existing CloudTrail. The service's own security-relevant events are already recorded independently: the API access log records every caller account, the activation roster records every attempt, and a CloudWatch metric filter alarms on refusals. |
| security-matrix | `KMS-007` | `feature-platform/seller-entitlement-service/template.yaml` | Accepted: KMS key event monitoring (EventBridge rule + monitoring Lambda + alarms) is account-level infrastructure owned by the account operator, out of scope for this template. Same rationale as the other KMS-007 records in this baseline. This service is deployed by an extension SELLER into their own Marketplace seller account, so KMS-event monitoring is theirs to configure against their existing CloudTrail. The service's own security-relevant events are already recorded independently: the API access log records every caller account, the activation roster records every attempt, and a CloudWatch metric filter alarms on refusals. |
| security-matrix | `DDB-002` | `feature-platform/seller-entitlement-service/template.yaml` | Accepted: capturing DynamoDB data-plane events requires a CloudTrail trail with data-event selectors (and a log bucket), which is account-global infrastructure owned by the account operator, not something a single feature stack should create. Same rationale as the other DDB-002 records in this baseline. Compensating controls on this specific table are unusually strong: the activation Lambda's IAM grants dynamodb:UpdateItem ONLY (no GetItem, Query, Scan or Delete - asserted by tests/test_template_security.py), so the function can neither read nor destroy the roster; reads require the operator's own credentials; the table has SSE + point-in-time recovery and is Retain-on-delete; and every write is independently mirrored by the API Gateway access log and an EMF activation metric. |
