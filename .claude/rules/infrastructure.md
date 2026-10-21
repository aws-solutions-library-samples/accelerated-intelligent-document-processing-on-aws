---
paths:
  - "**/template.yaml"
  - "nested/**"
  - "patterns/**"
  - "iam-roles/**"
  - "feature-platform/**/template.yaml"
---

# Infrastructure rules

Covers the CloudFormation and SAM templates, the nested stacks, the Step Functions
definitions and the example IAM roles. Rules specific to feature stacks are in
[feature-platform.md](feature-platform.md).

## Must
- Make every template partition-safe. Write `arn:${AWS::Partition}:` and
  `${AWS::URLSuffix}`. In ASL files, write `arn:${Partition}:` and add
  `Partition: !Ref AWS::Partition` to `DefinitionSubstitutions`.
- Give every IAM role the conditional
  `!If [HasPermissionsBoundary, !Ref PermissionsBoundaryArn, !Ref AWS::NoValue]`.
- Add each resource to the nested stack that owns it:
  - UI API resolvers and the dispatcher go in `nested/api-resolvers/`.
  - Processing goes in `patterns/unified/`.
  - The Feature Platform host goes in `feature-platform/main-stack-extensions/`.
  - The parent must pass every parameter a nested stack requires.
- Configure Lambdas to the house pattern: `python3.12`, `arm64`, `index.handler`,
  conditional tracing, and the `LOG_LEVEL`, `METRIC_NAMESPACE` and `STACK_NAME`
  environment variables.
- Give every Lambda a dedicated `AWS::Logs::LogGroup` with `RetentionInDays`,
  conditional KMS and a `Condition` that matches the function's. Point the
  function at it with `LoggingConfig`. Name new groups
  `/${AWS::StackName}/lambda/<LogicalId>`, or leave the name out entirely.
- Only one kind of Lambda may keep Lambda's default log group: one that runs only
  during a stack operation (a custom-resource handler or install hook).
- Every new Lambda task state in a state machine needs:
  - a transient retrier with `MaxAttempts` of at least 3
  - any timeout codes in a separate retrier with `MaxAttempts: 1`
  - `TransientError` listed only if its handler re-raises under that name
- Give every cfn-nag or checkov suppression a specific `reason:`.
- When you add, remove or re-scope an IAM role or AWS service, update
  `docs/aws-services-and-roles.md` and `iam-roles/cloudformation-management/` in
  the same change.
- Keep the IAM half of the example service role scoped:
  - no IAM write on `Resource: "*"` without a permissions-boundary condition
  - `iam:PassRole` always carries `iam:PassedToService`
  - the `'Null'` condition key is quoted
- Exempt an ARN that is genuinely single-partition one line at a time in
  `ARN_PARTITION_EXEMPT` (`<path>:<line-pattern>`), with a written justification.

## Must not
- Name a log group after its function (`!Sub "/aws/lambda/${MyFunction}"`). That
  reverses the creation order and leaves orphaned groups that never expire.
- "Fix" existing compliant log-group shapes, or flatten nested stacks into the
  parent.
- Hardcode `arn:aws:`, `amazonaws.com`, a region or an account ID, and that
  includes inside folded `Description: >` blocks. Reword those.
- Use a `States.ALL` or `States.TaskFailed` retrier.
- Add `Resource: "*"`, `Principal: "*"`, a public bucket or topic, or an open
  security group without written justification.
- Reintroduce AppSync or the SageMaker UDOP endpoint, or describe processing as
  Pattern 1/2/3. The modes are BDA mode and Pipeline mode, selected by `use_bda`.

## Verify with
- `make cfn-lint`, `make check-arn-partitions`, `make validate-buildspec`
- `scripts/tests/test_lambda_log_groups.py`, `scripts/tests/test_log_group_encryption.py`,
  `scripts/tests/test_nested_stack_parameters.py`
- `scripts/tests/test_state_machine_retry_policies.py` and
  `patterns/unified/tests/test_workflow_transient_retry.py`
- `python3 scripts/sdlc/validate_service_role_permissions.py` and
  `scripts/tests/test_iam_privilege_escalation.py`
- Before you merge a change on the deploy path, start the manual
  `integration_tests` job.

## Sources
- [.claude/skills/infrastructure.md](../skills/infrastructure.md): the stack
  layout, Lambda resource pattern, log-group invariant, partition gate and
  inventory audit commands
- [.claude/skills/code-review.md](../skills/code-review.md): the infrastructure
  review checklist
- [docs/configuration.md](../../docs/configuration.md): Step Functions retry
  policy and how to add a task state
- [docs/aws-services-and-roles.md](../../docs/aws-services-and-roles.md): the
  service and role inventory
