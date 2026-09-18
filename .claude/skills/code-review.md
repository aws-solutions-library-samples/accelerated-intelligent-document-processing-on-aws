# Code Review Checklist — GenAI IDP Accelerator

## Pre-Submit Checks (REQUIRED)
Run these before every commit:
```bash
make lint               # Full lint (ruff + format + ARN + buildspec + UI + codegen)
make test               # All tests
make typecheck          # basedpyright
```

**Before opening an MR** (not needed on every commit), also run the SRT
security scan so new findings are known and addressed up front — CI runs it
on MRs to `develop` and will fail on HIGH open findings:
```bash
make srt-scan           # requires one-time `make srt-setup`; ~5–10 min
# or: make srt          # clean → setup → scan → optional interactive fix
```
Locally the scan exits 0 even with findings — read the
`OPEN HIGH PRIORITY SECURITY ISSUES` table it prints (or `.srt/issues.json`),
don't rely on the exit code. Fix real issues; for false positives use
`make srt-fix` to suppress **with a specific justification** and commit the
updated `scripts/srt/issues.json`.
Or use the convenience command:
```bash
make commit             # lint + test + auto-generate commit message + push
make fastcommit         # fastlint (skip UI) + auto-commit + push
```

## Python Backend Review

### Style & Formatting
- [ ] `ruff check --fix` passes clean
- [ ] `ruff format` applied (double quotes, 88-char lines)
- [ ] basedpyright passes (basic mode)
- [ ] License header present: `# Copyright Amazon.com, Inc. ...` + `# SPDX-License-Identifier: MIT-0`

### Lambda Functions
- [ ] Handler is `handler(event, context)` in `index.py`
- [ ] X-Ray tracing: `patch_all()` at module level
- [ ] AWS clients initialized at module level (not inside handler)
- [ ] Logging via `logging.getLogger()` with `LOG_LEVEL` env var
- [ ] Type hints on function signatures
- [ ] Explicit `ClientError` catches with `exc_info=True`
- [ ] No hardcoded AWS account IDs or regions

### DynamoDB Access
- [ ] `make check-filtered-scans` passes — a filtered `Scan` must **paginate**
      (`LastEvaluatedKey`), because DynamoDB bounds `Limit` and the 1MB page by
      items **examined**, not items matching `FilterExpression`. An unpaginated
      one finds a match only when it lands in the examined window, so it breaks
      *silently* and *later*, as data grows (issue #599). Deliberate bounded
      samples need an inline `# filtered-scan-ok: <reason>`.
- [ ] Project only the attributes you need (`ProjectionExpression`) on a scan
      used to locate a row — without it the page is spent reading whole item
      bodies, so far fewer rows are examined per call
- [ ] A boto3 paginator for one service is NOT pagination for another; check
      that the paginator in view actually wraps *this* call

### idp_common Library
- [ ] New modules use lazy loading pattern (register in `__init__.py.__getattr__`)
- [ ] Modular dependency groups updated if new deps added (`pyproject.toml`)
- [ ] No direct imports from `idp_sdk._core` (use `IDPClient`)
- [ ] Tests added mirroring module structure in `tests/unit/`

## Frontend UI Review

### Style & Formatting
- [ ] ESLint passes (`make ui-lint`)
- [ ] Prettier applied (140-char lines, single quotes, trailing commas)
- [ ] TypeScript strict mode passes (`npm run typecheck`)

### Component Standards
- [ ] Arrow function components (not `function` declarations)
- [ ] Cloudscape Design System components used (not Material UI, Ant Design, etc.)
- [ ] `ConsoleLogger` for logging (not `console.log` in production code)
- [ ] Context API for state (not Redux)
- [ ] New hooks use kebab-case filenames (`use-my-hook.ts`)

### GraphQL
- [ ] If schema changed, `make codegen` run and committed
- [ ] Generated files (`src/graphql/generated/`) not manually edited

## Infrastructure Review

### CloudFormation Templates
- [ ] `make check-arn-partitions` passes — NO hardcoded `arn:aws:`
- [ ] Service endpoints use `${AWS::URLSuffix}` — NO hardcoded `amazonaws.com`
- [ ] PermissionsBoundary conditional on all IAM roles
- [ ] New IAM role or AWS service? → both `docs/aws-services-and-roles.md` AND `iam-roles/cloudformation-management/` updated (see infrastructure.md)
- [ ] Dedicated LogGroup per Lambda with KMS encryption
- [ ] cfn-nag + checkov suppression metadata where needed (with justification)
- [ ] `make validate-buildspec` passes

### Security
- [ ] No credentials, API keys, or secrets in code or templates
- [ ] No full JWT tokens logged in plaintext (Talos finding #10)
- [ ] S3 access properly scoped (no wildcard resource ARNs without justification)
- [ ] Input validation on all API endpoints
- [ ] DOMPurify used for any HTML rendering in UI

## Documentation
- [ ] New features documented in `docs/`
- [ ] YAML frontmatter with `title` field
- [ ] License header after frontmatter
- [ ] `CHANGELOG.md` updated for user-facing changes
- [ ] Cross-references to related docs where appropriate

## Testing
- [ ] Unit tests added (`@pytest.mark.unit`)
- [ ] Tests use `moto` `@mock_aws` for AWS service mocking
- [ ] Class-based test organization with `setup_method`
- [ ] Test files mirror module structure in `tests/unit/`
- [ ] Integration tests tagged with `@pytest.mark.integration`
- [ ] Config library validation tests pass: `make test-config-library`

## Git Workflow
- [ ] Branch from `develop` using prefix: `feature/`, `fix/`, `docs/`
- [ ] Focused, single-issue changes
- [ ] Version bump if needed: `make version V=x.y.z` (PEP 440 compliant)

### Commit messages and PR descriptions are public and permanent

This repository is public, and a commit message cannot be edited once merged. A
force-push does not remove it either: GitHub keeps a merged PR's own commits and
diff view independently of any branch, so the only remedy is a support request.
Treat both as published text.

- [ ] No internal-only references: corporate email addresses, hostnames that
      resolve only on the internal network, internal review or ticket ids. A
      `PreToolUse` hook (`scripts/hooks/check_commit_text.py`) blocks the common
      cases; if it fires, fix the text rather than working around it.
- [ ] Written at summary altitude: what the change accomplishes and why, not an
      inventory of the individual strings it touched. "Trim the governance docs to
      community-facing guidance" is the right altitude; the line-by-line detail
      belongs in the diff, where it stays accurate.
- [ ] No third-party or personal information published on someone else's behalf:
      contributor names, contribution metrics or rankings, individual repository
      permissions.
- [ ] Exploitable security findings go through the private channel in
      `SECURITY.md`, not into a commit message, roadmap entry or changelog line.
- [ ] Facts that rot are dated or omitted: live permission tables, "as of today"
      counts and in-flight PR states are stale within a week.
