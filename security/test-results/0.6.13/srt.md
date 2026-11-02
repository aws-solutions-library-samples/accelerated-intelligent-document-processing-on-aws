# SRT — SAST & Dependency Scan

Static analysis (Bandit, Semgrep, Checkov), dependency inventory (Syft), and the security-matrix review, aggregated by the [Sample Security Review Tool](https://github.com/aws-samples/sample-security-review-tool). The gate is HIGH findings that are **open or reopened** (i.e. not suppressed or resolved) and not scoped out of it by the rule in `scripts/srt/ci_paths.py`; lower tiers are reported as counts (they are dominated by tracked third-party/vendored code).

## Summary

- **Gate (open/reopened HIGH findings):** PASS ✅
- **CI-visible findings:** 50868
- **Scoped out of the gate:** 51 identifier-name finding(s) (Bandit B105/B106) in test code no deployment artifact is built from — listed below, reported at Bandit's own severity
- **Source:** live scan results (`.srt/issues.json`)

## Analyzers executed

Each analyzer SRT ran, what it covers, and its contribution to the CI-visible findings.

| Analyzer | Coverage | Findings | HIGH |
|----------|----------|---------:|-----:|
| Bandit | Python SAST | 50479 | 53 |
| Checkov | IaC / CloudFormation misconfig | 165 | 1 |
| Semgrep | multi-language SAST | 160 | 1 |
| security-matrix | AWS security-control review (SRT rules) | 64 | 56 |

## Findings by priority × status

| Priority | Open | resolved | suppressed | Total |
|----------|------:|------:|------:|------:|
| HIGH | 51 | 6 | 54 | 111 |
| MEDIUM | 181 | 0 | 0 | 181 |
| LOW | 50566 | 0 | 0 | 50566 |
| INFO | 10 | 0 | 0 | 10 |

## HIGH findings by check (disposition)

Every HIGH check-ID flagged, with how many are in each status. A green gate means every one of them is either resolved, suppressed, or an identifier-name finding scoped out of the gate (see the section below and the Summary count above).

| Source | Check | Open | resolved | suppressed |
|--------|-------|--:|--:|--:|
| Bandit | `B105` | 41 | 0 | 0 |
| Bandit | `B106` | 10 | 0 | 0 |
| Bandit | `B701` | 0 | 2 | 0 |
| Checkov | `CKV_AWS_192` | 0 | 1 | 0 |
| Semgrep | `package_managers.npm.npm-missing-minimum-release-age.npm-missing-minimum-release-age` | 0 | 1 | 0 |
| security-matrix | `API-GW-001` | 0 | 0 | 1 |
| security-matrix | `API-GW-002` | 0 | 0 | 2 |
| security-matrix | `API-GW-004` | 0 | 0 | 4 |
| security-matrix | `API-GW-006` | 0 | 0 | 1 |
| security-matrix | `API-GW-007` | 0 | 0 | 3 |
| security-matrix | `DDB-002` | 0 | 0 | 21 |
| security-matrix | `EC2-002` | 0 | 0 | 3 |
| security-matrix | `IAM-009` | 0 | 0 | 1 |
| security-matrix | `KMS-007` | 0 | 0 | 6 |
| security-matrix | `LAMBDA-002` | 0 | 0 | 1 |
| security-matrix | `LAMBDA-004` | 0 | 0 | 2 |
| security-matrix | `LAMBDA-005` | 0 | 1 | 0 |
| security-matrix | `LAMBDA-011` | 0 | 0 | 2 |
| security-matrix | `LAMBDA-012` | 0 | 1 | 0 |
| security-matrix | `S3-001` | 0 | 0 | 2 |
| security-matrix | `S3-005` | 0 | 0 | 1 |
| security-matrix | `S3-008` | 0 | 0 | 4 |

## ℹ️ Identifier-name findings in test code (non-blocking)

Bandit's B105/B106 match an identifier's **name** against a password wordlist, never its value, and SRT promotes both to HIGH regardless of what was matched. In test code that no deployment artifact is built from they are reported at Bandit's own severity instead of blocking; the same name shape still gates anywhere that ships. This is the scope decision `make srt-scan` applies, applied here so this snapshot and that gate give the same verdict.

| Source | Check | Path | Line |
|--------|-------|------|-----:|
| Bandit | `B106` | `lib/idp_cli_pkg/tests/test_delete_documents_selector_failure.py` | 79 |
| Bandit | `B105` | `lib/idp_cli_pkg/tests/test_parse_parameters.py` | 77 |
| Bandit | `B106` | `lib/idp_common_pkg/tests/unit/agents/error_analyzer/test_config_reaches_tools.py` | 55 |
| Bandit | `B106` | `lib/idp_common_pkg/tests/unit/lambdas/test_abort_test_run_stops_documents.py` | 78 |
| Bandit | `B105` | `lib/idp_common_pkg/tests/unit/lambdas/test_notifications_resolver.py` | 39 |
| Bandit | `B106` | `lib/idp_common_pkg/tests/unit/lambdas/test_processresults_reprocess_keeps_finished_review.py` | 199 |
| Bandit | `B106` | `lib/idp_common_pkg/tests/unit/lambdas/test_reviewer_list_released_review.py` | 316 |
| Bandit | `B106` | `lib/idp_common_pkg/tests/unit/lambdas/test_test_run_send_to_review_keeps_finished_review.py` | 205 |
| Bandit | `B105` | `lib/idp_common_pkg/tests/unit/reporting/bedrock_cache_dimension_digest.py` | 177 |
| Bandit | `B105` | `lib/idp_common_pkg/tests/unit/reporting/bedrock_govcloud_rate_digest.py` | 205 |
| Bandit | `B105` | `lib/idp_common_pkg/tests/unit/reporting/test_save_rule_validation_results.py` | 90 |
| Bandit | `B106` | `lib/idp_common_pkg/tests/unit/review_queues/test_reasons_and_routing.py` | 482 |
| Bandit | `B106` | `lib/idp_common_pkg/tests/unit/review_queues/test_remove_and_delete_queue.py` | 231 |
| Bandit | `B106` | `lib/idp_common_pkg/tests/unit/test_delete_documents_coverage.py` | 1608 |
| Bandit | `B105` | `lib/idp_common_pkg/tests/unit/test_suite_hygiene.py` | 478 |
| Bandit | `B105` | `lib/idp_common_pkg/tests/unit/test_suite_hygiene.py` | 479 |
| Bandit | `B105` | `lib/idp_feature_sdk/tests/aws_hermetic.py` | 79 |
| Bandit | `B106` | `lib/idp_sdk/tests/unit/test_batch_delete_selector_failure.py` | 52 |
| Bandit | `B105` | `scripts/sdlc/tests/test_inbox_live_check.py` | 198 |
| Bandit | `B105` | `scripts/sdlc/tests/test_inbox_live_check.py` | 199 |
| Bandit | `B105` | `scripts/sdlc/tests/test_nightly_notification.py` | 371 |
| Bandit | `B105` | `scripts/sdlc/tests/test_ux_nightly_update.py` | 46 |
| Bandit | `B105` | `scripts/tests/test_ai_ux_test_stories.py` | 3007 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_artifacts.py` | 135 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_artifacts.py` | 136 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_artifacts.py` | 137 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_artifacts.py` | 138 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_docker.py` | 76 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_docker.py` | 103 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_docker.py` | 106 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_docker.py` | 886 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_docker.py` | 887 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_recording.py` | 409 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_recording.py` | 796 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_recording.py` | 797 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_recording.py` | 912 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_session.py` | 554 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_session.py` | 555 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_ui_change.py` | 497 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_ui_change.py` | 501 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_ui_change.py` | 502 |
| Bandit | `B105` | `scripts/tests/test_ux_runner_ui_change.py` | 503 |
| Bandit | `B105` | `scripts/tests/test_ux_test_ci_jobs.py` | 101 |
| Bandit | `B105` | `scripts/tests/test_ux_test_ci_jobs.py` | 102 |
| Bandit | `B105` | `scripts/tests/test_ux_test_ci_jobs.py` | 104 |
| Bandit | `B105` | `scripts/tests/test_ux_test_ci_jobs.py` | 105 |
| Bandit | `B105` | `scripts/tests/test_ux_test_ci_jobs.py` | 111 |
| Bandit | `B105` | `scripts/tests/test_ux_test_ci_jobs.py` | 112 |
| Bandit | `B105` | `scripts/tests/test_ux_test_ci_jobs.py` | 113 |
| Bandit | `B105` | `scripts/tests/test_ux_test_stories_docker.py` | 96 |
| Bandit | `B105` | `scripts/tests/test_ux_test_stories_docker.py` | 99 |

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
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `DDB-002` | `template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. |
| security-matrix | `LAMBDA-004` | `nested/bedrockkb/template.yaml` | Accepted: X-Ray tracing intentionally omitted on helper/custom-resource Lambdas; enabling tracing is a customer deployment choice for this sample solution. |
| security-matrix | `LAMBDA-011` | `nested/bedrockkb/template.yaml` | Accepted: CloudWatch alarms for helper/custom-resource Lambdas are intentionally omitted; failures surface via CloudFormation stack status. Alarm topology is a customer operations decision. |
| security-matrix | `KMS-007` | `template.yaml` | Accepted: KMS key event monitoring (CloudTrail/EventBridge/alarms) is account-level infrastructure owned by the deploying customer, out of scope for the solution template. |
| security-matrix | `EC2-002` | `template.yaml` | False positive: BastionRole (Condition ShouldDeployBastionHost, off by default) carries exactly the managed policy the finding's own fix recommends - AmazonSSMManagedInstanceCore - and nothing else; the instance profile attaches only that role. No excess privilege exists. |
| security-matrix | `EC2-002` | `template.yaml` | False positive: BastionRole (Condition ShouldDeployBastionHost, off by default) carries exactly the managed policy the finding's own fix recommends - AmazonSSMManagedInstanceCore - and nothing else; the instance profile attaches only that role. No excess privilege exists. |
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
| security-matrix | `DDB-002` | `feature-platform/pii-anonymizer/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. Same rationale as every other table in the solution (TrackingTable, InstalledFeaturesTable, ClaimsStatusTable, ...). The audit table stores metadata only (never PII). |
| security-matrix | `DDB-002` | `feature-platform/confbench-testset/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. Same disposition as every other DynamoDB table in this solution. This table holds only ingest-job metadata (job id, tier, variant list, progress counters) for the ConfBench Test Set extension - no document content and no customer data. Encryption at rest (SSESpecification) and point-in-time recovery are both enabled, and rows expire via TTL. |
| security-matrix | `DDB-002` | `feature-platform/pii-anonymizer/template.yaml` | Accepted: CloudTrail DynamoDB data-plane logging is an account-level control (trail + log bucket) owned by the deploying customer; the solution template cannot assume or create account-wide trails. Same rationale as every other table in the solution (TrackingTable, InstalledFeaturesTable, ClaimsStatusTable, ...). The mapping table is SSE-encrypted, PITR-enabled, and reachable only via the RBAC-gated feature API. |
| security-matrix | `KMS-007` | `feature-platform/seller-entitlement-service/template.yaml` | Accepted: KMS key event monitoring (EventBridge rule + monitoring Lambda + alarms) is account-level infrastructure owned by the account operator, out of scope for this template. Same rationale as the other KMS-007 records in this baseline. This service is deployed by an extension SELLER into their own Marketplace seller account, so KMS-event monitoring is theirs to configure against their existing CloudTrail. The service's own security-relevant events are already recorded independently: the API access log records every caller account, the activation roster records every attempt, and a CloudWatch metric filter alarms on refusals. |
| security-matrix | `KMS-007` | `feature-platform/seller-entitlement-service/template.yaml` | Accepted: KMS key event monitoring (EventBridge rule + monitoring Lambda + alarms) is account-level infrastructure owned by the account operator, out of scope for this template. Same rationale as the other KMS-007 records in this baseline. This service is deployed by an extension SELLER into their own Marketplace seller account, so KMS-event monitoring is theirs to configure against their existing CloudTrail. The service's own security-relevant events are already recorded independently: the API access log records every caller account, the activation roster records every attempt, and a CloudWatch metric filter alarms on refusals. |
| security-matrix | `DDB-002` | `feature-platform/seller-entitlement-service/template.yaml` | Accepted: capturing DynamoDB data-plane events requires a CloudTrail trail with data-event selectors (and a log bucket), which is account-global infrastructure owned by the account operator, not something a single feature stack should create. Same rationale as the other DDB-002 records in this baseline. Compensating controls on this specific table are unusually strong: the activation Lambda's IAM grants dynamodb:UpdateItem ONLY (no GetItem, Query, Scan or Delete - asserted by tests/test_template_security.py), so the function can neither read nor destroy the roster; reads require the operator's own credentials; the table has SSE + point-in-time recovery and is Retain-on-delete; and every write is independently mirrored by the API Gateway access log and an EMF activation metric. |
| security-matrix | `LAMBDA-002` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. The flagged environment variables are the fixture's RSA private exponent and OIDC client secret. Both are generated per run by deploy.py (rsa_parameters / secrets.token_urlsafe), sign nothing but this fixture's own test tokens, and are destroyed with the stack. There is no real credential to protect. |
| security-matrix | `LAMBDA-004` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. X-Ray tracing on a fixture that lives for the duration of one manual verification run has no operational value; there is nothing to trace it for and nobody to read it. |
| security-matrix | `LAMBDA-011` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. CloudWatch alarms presuppose an on-call owner and a lifetime longer than one verification run. A failure here fails the verification script, which is the intended signal. |
| security-matrix | `API-GW-002` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. INHERENT to the protocol: an OIDC provider must accept the arbitrary query and form parameters that /authorize and /token carry (redirect_uri, state, scope, code, grant_type, client_id, client_secret...). Attaching a RequestValidator would reject valid OIDC requests and break the flow this fixture exists to exercise. |
| security-matrix | `API-GW-007` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. INHERENT to the protocol: the discovery document, JWKS, /authorize, /token and /userinfo endpoints must be reachable without AWS authentication for Cognito to federate to them at all. An OIDC provider that required AWS auth could not serve any relying party. |
| security-matrix | `API-GW-004` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. INHERENT to the protocol: AuthorizationType must be NONE so Cognito can reach the OIDC endpoints. Setting AWS_IAM or COGNITO_USER_POOLS here would make the provider unusable as an identity provider. |
| security-matrix | `API-GW-007` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. INHERENT to the protocol: this method serves the unauthenticated OIDC endpoints (discovery, JWKS, /authorize, /token, /userinfo) that a relying party must be able to call. |
| security-matrix | `API-GW-004` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. INHERENT to the protocol: same as ProxyMethod — AuthorizationType must be NONE for Cognito to reach the OIDC endpoints. |
| security-matrix | `API-GW-007` | `scripts/security/live_checks/oidc_provider/template.yaml` | Accepted (test fixture, not product infrastructure): scripts/security/live_checks/oidc_provider/ is a throwaway OIDC identity provider that only exists to verify the Cognito pre-token IdP group-mapping trigger (see .claude/skills/live-auth-checks.md). It is deployed by a manual `make verify-idp-federation` run and deleted immediately after; it is not referenced by template.yaml or any nested stack, and never runs in a customer account. INHERENT to the protocol: same as ProxyMethod — the OIDC endpoints are unauthenticated by design. |
