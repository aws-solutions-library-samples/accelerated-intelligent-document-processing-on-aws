# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GenAI Intelligent Document Processing (GenAIIDP) is a serverless solution for automated document processing using AWS services. It combines OCR with generative AI to extract structured data from unstructured documents at scale.

The system uses a modular architecture with nested CloudFormation stacks supporting multiple document processing patterns while maintaining common infrastructure for queueing, tracking, and monitoring.

## Build & Development Commands

### Building and Publishing

Build and publish deployment artifacts to S3:

```bash
# Primary build script (recommended)
python3 publish.py <cfn_bucket_basename> <cfn_prefix> <region> [--verbose]

# Example
python3 publish.py idp-1234567890 idp us-east-1

# With verbose output for debugging build failures
python3 publish.py idp-1234567890 idp us-east-1 --verbose

```

The build process:
- Checks system dependencies (AWS CLI, SAM CLI, Docker, Python 3.12+, Node.js 22.12+)
- Builds CloudFormation templates and assets using SAM
- All pattern functions are built within the unified pattern directory
- Uploads artifacts to S3 bucket named `<cfn_bucket_basename>-<region>`

### Code Quality & Linting

```bash
# Run all linting and formatting (includes UI)
make lint

# Fast lint (skips UI lint if unchanged via checksum)
make fastlint

# Python linting only
make ruff-lint

# Python formatting only
make format

# Type checking with basedpyright
make typecheck
make typecheck-stats

# UI linting (checks for changes via checksum)
make ui-lint

# UI build verification
make ui-build

# CI/CD linting (check-only, no modifications)
make lint-cicd
```

### Testing

```bash
# Run all tests (idp_common_pkg + idp_cli + srt security scan)
make test

# Run tests in idp_common_pkg only
cd lib/idp_common_pkg && make test

# Run unit tests only
cd lib/idp_common_pkg && make test-unit

# Run integration tests (requires AWS resources)
cd lib/idp_common_pkg && make test-integration

# Run idp_cli tests
cd idp_cli && python -m pytest -v

# Run specific test markers
pytest -m "unit"
pytest -m "integration"
```

### Security Scanning

The project includes automated security scanning with the [Sample Security Review Tool (SRT)](https://github.com/aws-samples/sample-security-review-tool):

```bash
# Run full SRT workflow (setup → scan → optional fix)
make srt

# Or run individual steps:
make srt-setup     # Download and configure SRT
make srt-scan      # Run security assessment
make srt-fix       # Interactive fix mode
```

**CI/CD Integration:**
- SRT automatically runs on merge requests targeting `develop` branch (GitLab CI `security_review` stage)
- Does not run on feature branch pushes to avoid blocking development
- Pipeline fails if high-priority security findings are detected
- Provides security gate before code is merged to `develop`

**SRT does NOT cover dependency CVEs.** Its `syft` stage builds an SBOM
(inventory only, no vulnerability matching), so a separate gate handles SCA:

```bash
make dep-audit        # audit every pinned Python + Node dep against OSV (fails on HIGH+)
make dep-audit-fast   # reuse existing dist/manifests instead of regenerating
```

Gated in CI by the `dep_audit` job (`fast_checks`, every push and MR, no AWS
needed). Triage unreachable advisories in
`scripts/security/dep_audit_allowlist.json` with a justification — the same
pattern `scripts/srt/issues.json` uses for SRT. See
`.claude/skills/srt-security-scan.md`.

### IDP CLI Commands

The IDP CLI is used for programmatic deployment and batch processing:

```bash
# Install all packages into current Python environment
make setup
# Or create an isolated .venv first
make setup-venv

# Deploy a new stack
idp-cli deploy \
    --stack-name my-idp-stack \
    --pattern pattern-2 \
    --admin-email your.email@example.com \
    --max-concurrent 100 \
    --wait

# Deploy with custom configuration
idp-cli deploy \
    --stack-name my-idp-stack \
    --pattern pattern-2 \
    --custom-config ./config_library/unified/bank-statement-sample/config.yaml \
    --wait

# Process documents in batch
idp-cli run-inference \
    --stack-name <your-stack-name> \
    --dir ./samples/ \
    --monitor

# Download results
idp-cli download-results \
    --stack-name <your-stack-name> \
    --batch-id <batch-id> \
    --output-dir ./results/
```

### Local Lambda Testing

```bash
cd patterns/unified/
sam build
sam local invoke OCRFunction -e ../../testing/OCRFunction-event.json --env-vars ../../testing/env.json
```

### Development Setup

```bash
# Install idp_common library in edit mode with all dependencies
cd lib/idp_common_pkg && make dev
```

## Architecture Overview

### Nested Stack Architecture

The solution uses a modular architecture with the main template (`template.yaml`) and nested pattern stacks:

**Main Stack** (`template.yaml`) - Pattern-agnostic resources:
- S3 Buckets (Input, Output, Working, Configuration, Evaluation Baseline)
- SQS Queues and Dead Letter Queues
- DynamoDB Tables (Execution Tracking, Concurrency, Configuration)
- Lambda Functions (Queue Processing, Queue Sending, Workflow Tracking, Document Status Lookup, Evaluation, UI Integration)
- CloudWatch Alarms and Dashboard
- Web UI Infrastructure (CloudFront, S3 for static assets, CodeBuild)
- Authentication (Cognito User Pool, Identity Pool)
- AppSync GraphQL API (for UI-backend communication)

**Unified Pattern Stack** (`patterns/unified/template.yaml`) - Processing resources:
- Step Functions State Machine (BDA branch + Pipeline branch + shared tail)
- Lambda Functions (OCR, Classification, Extraction, Assessment, Summarization, Evaluation, etc.)
- CloudWatch Dashboard

### Processing Modes

The unified architecture supports two processing modes, controlled by the `use_bda` configuration flag:

1. **BDA Mode** (formerly Pattern 1)
   - Uses AWS Bedrock Data Automation for end-to-end processing
   - Handles packet or media documents with integrated OCR, classification, and extraction

2. **Pipeline Mode** (formerly Pattern 2)
   - OCR with Amazon Textract
   - Classification with Bedrock (page-level or holistic)
   - Extraction with Bedrock (traditional or agentic)
   - Supports few-shot examples
   - Optional agentic extraction with deterministic table parsing

> **Note**: The separate `patterns/pattern-1/`, `patterns/pattern-2/`, and `patterns/pattern-3/` directories have been removed. All processing is now in `patterns/unified/`. See [pattern-1.md](docs/pattern-1.md) and [pattern-2.md](docs/pattern-2.md) for historical reference.

### Agentic Extraction with Table Parsing

The extraction service supports an optional **agentic extraction mode** with intelligent table parsing:

**When to Use**:
- Documents with large tables (100+ rows) where completeness is critical
- Bank statements, transaction logs, brokerage statements
- Multi-page tables that may split across OCR page breaks
- Documents where OCR artifacts (empty lines, missing characters) cause data loss

**Key Features**:
- **Intelligent Lookahead Recovery**: Tolerates OCR artifacts (empty lines, missing pipes) by looking ahead to detect table continuation
- **Auto-Merge Table Fragments**: Automatically merges tables with identical columns that were split by page breaks
- **Smart Warnings**: Agent receives actionable warnings (⚠️ fragmentation, ℹ️ recovery) to verify completeness
- **Hybrid Extraction**: Agent uses deterministic parsing for well-structured tables, falls back to LLM for complex layouts
- **Completeness Validation**: Service validates extracted data against schema constraints (e.g., `minItems`)

**Configuration**:
```yaml
extraction:
  model: "us.anthropic.claude-sonnet-4-20250514-v1:0"
  agentic:
    enabled: true
    table_parsing:
      enabled: true  # Enable deterministic table parser tool
      max_empty_line_gap: 3  # Tolerate up to 3 empty lines in tables (0-10)
      auto_merge_adjacent_tables: true  # Merge table fragments
      min_confidence_threshold: 95.0  # OCR confidence target (Textract only)
      min_parse_success_rate: 0.90  # Quality threshold for parsed results
```

**Tuning**:
- **High-quality OCR**: `max_empty_line_gap: 2`
- **Standard quality**: `max_empty_line_gap: 3` (default)
- **Complex/noisy documents**: `max_empty_line_gap: 5-7`

See `lib/idp_common_pkg/idp_common/extraction/README.md` for detailed documentation.

### Document Processing Flow

1. Documents uploaded to Input S3 bucket trigger EventBridge events
2. Queue Sender Lambda records event in tracking table and sends to SQS
3. Queue Processor Lambda:
   - Picks up messages in batches
   - Manages workflow concurrency using DynamoDB counter
   - Starts Step Functions executions
4. Step Functions workflow runs pattern-specific processing steps
5. Results written to Output S3 bucket
6. Workflow completion events update tracking and metrics

### Key Libraries

**`idp_common_pkg`** (`lib/idp_common_pkg/`):
- Core shared library powering the accelerator
- Modular installation: Install only needed components to minimize Lambda package size
  - ⚠️ Always install first-party packages **from the local checkout**, never by
    bare name — those names on public PyPI belong to unrelated parties, so a bare
    `pip install` fetches someone else's code. See
    `docs/dependency-confusion.md`.
  - `pip install -e "lib/idp_common_pkg[core]"` - minimal dependencies
  - `pip install -e "lib/idp_common_pkg[ocr]"` - OCR support
  - `pip install -e "lib/idp_common_pkg[classification]"` - Classification support
  - `pip install -e "lib/idp_common_pkg[extraction]"` - Extraction support (includes optional agentic mode with deterministic table parsing tool)
  - `pip install -e "lib/idp_common_pkg[evaluation]"` - Evaluation support
  - `pip install -e "lib/idp_common_pkg[all]"` - everything
- Components: OCR, Classification, Extraction (supports traditional and agentic modes with intelligent table parsing), Evaluation, Summarization, AppSync integration, Reporting, BDA integration
- Configuration management via DynamoDB
- Document models and data structures
- Extraction features:
  - Traditional LLM-based extraction with few-shot examples
  - Agentic extraction with tool-based structured output (Strands framework)
  - Deterministic Markdown table parser for robust tabular data extraction
  - Intelligent recovery from OCR artifacts (empty lines, missing pipes)
  - Automatic merging of table fragments split by page breaks
  - Hybrid extraction: agent uses parsing for tables, LLM for complex layouts

**`idp_cli`** (`idp_cli/`):
- Command-line interface for deployment and batch processing
- Stack deployment and updates
- Batch document processing
- Evaluation workflows
- Analytics integration

### Web UI

- React-based interface using Cloudscape Design System
- Vite build system
- Node.js 22.12+ and npm required
- Authentication via AWS Amplify v6 and Cognito
- Real-time document status via AppSync GraphQL subscriptions
- Location: `src/ui/`

## Configuration System

Configuration is managed via DynamoDB Configuration Table with two record types:
- **Default**: Built-in pattern configurations from `config_library/`
- **Custom**: User-provided overrides (via CustomConfigPath parameter or CLI)

Configuration presets available:
- **Pattern 1**: `lending-package-sample`, `realkie-fcc-verified`
- **Pattern 2**: `lending-package-sample`, `rvl-cdip`, `rvl-cdip-with-few-shot-examples`, `bank-statement-sample`, `realkie-fcc-verified`
- **Pattern 3**: `rvl-cdip`

Custom configurations override selected pattern presets when specified.

## Development Practices

### Code Quality Standards

- **Python**:
  - PEP 8 style guidelines
  - Linting with `ruff` configured in `ruff.toml`
  - Type checking with `basedpyright` configured in `pyrightconfig.json`
  - Line length: 88 characters
  - Target version: Python 3.12

- **JavaScript/TypeScript**:
  - ESLint configuration in `src/ui/.eslintrc`
  - Run `npm run lint` in `src/ui/` to verify

### Testing Standards

- Tests located in `lib/idp_common_pkg/tests/` and `idp_cli/tests/`
- Use pytest markers: `@pytest.mark.unit` and `@pytest.mark.integration`
- Integration tests require AWS resources

### Git Workflow

- Main development branch: `develop`
- Create feature branches with prefixes: `feature/`, `fix/`, `docs/`
- PRs should target `develop` branch

## Important Implementation Details

### Pattern-2 Container Deployment

Pattern-2 functions are deployed as container images (not ZIP files) due to size constraints. The build process:
1. Builds container images using Docker
2. Pushes images to ECR
3. Lambda functions reference ECR image URIs

Ensure Docker is running and you have ECR permissions when building Pattern-2.

### GovCloud Compatibility

The codebase maintains GovCloud compatibility:
- Use `arn:${AWS::Partition}:` instead of hardcoded `arn:aws:`
- Use `${AWS::URLSuffix}` instead of hardcoded `amazonaws.com`
- Validation enforced via `make check-arn-partitions`

### Nested Stacks

The solution is split into nested stacks to stay under CloudFormation resource
limits. Notably, `nested/api-resolvers/` holds the UI API resolver Lambdas plus
the API Gateway REST API + dispatcher that the web UI calls (logical id
`APIRESOLVERSTACK`). (This stack was historically named `nested/appsync` /
`APPSYNCSTACK` when the UI used AWS AppSync, which has since been removed.)

### Lambda Layer Dependencies

Lambda functions reference the `idp_common_pkg` library:
- In `requirements.txt`: `../../lib/idp_common_pkg[extraction]`
- Use modular installation to minimize package size
- The library path is relative from Lambda source directories

### UI Checksum Optimization

The build system uses checksums to avoid rebuilding UI unnecessarily:
- Checksum stored in `src/ui/.checksum` and root `.checksum`
- `make ui-lint` skips linting if checksum unchanged
- Speeds up CI/CD and local development

## Sample Documents

Testing samples available in `samples/`:
- **Pattern 1 & 2**: `lending_package.pdf`
- **Pattern 3**: `rvl_cdip_package.pdf`

## Validation Scripts

- `scripts/sdlc/validate_buildspec.py` - Validates CodeBuild buildspec files
- `scripts/sdlc/validate_service_role_permissions.py` - Verifies IAM service role permissions
- `scripts/sdlc/typecheck_pr_changes.py` - Type checks only changed files in PRs

## AWS Access for Live Troubleshooting

The `default` AWS CLI profile is configured with credentials for the active
deployment account. Use it (not the runtime sandbox's ambient credentials,
which may point at a different account) to inspect deployed resources when
troubleshooting:

```bash
AWS_PROFILE=default aws sts get-caller-identity   # confirm the account first
AWS_PROFILE=default aws logs tail /aws/lambda/<fn> --since 1h
```

For the CloudWatch MCP tools, pass `profile_name: "default"` (and the stack's
region). Find Lambda log groups by listing with the deployment stack-name
prefix, e.g. `/aws/lambda/<StackName>-...`. Hook-related functions to look for:
the pipeline-hooks dispatcher (`...-PipelineHooksDispatcher...`), a feature's
hook Lambda, a feature's `...-FeatureApiFunction-...`, and the config-preset
resolver (`...-ApplyFeatureConfigPreset...`).

## AWS Service Requirements

### Required Bedrock Model Access

Request access to these models in Amazon Bedrock before deployment:
- **Amazon**: All Nova models, Titan Text Embeddings V2
- **Anthropic**: Claude 3.x models, Claude 4.x models

### Key AWS Services Used

- Amazon Bedrock (Foundational Models, Data Automation, Knowledge Bases)
- Amazon Textract
- Amazon SageMaker (for Pattern-3 UDOP endpoint)
- AWS Lambda
- AWS Step Functions
- Amazon S3
- Amazon SQS
- Amazon DynamoDB
- Amazon CloudWatch
- AWS AppSync
- Amazon Cognito
- Amazon CloudFront
- Amazon EventBridge
- Amazon SNS
- Amazon Glue (for evaluation analytics)
- Amazon Athena (for evaluation reporting)

## Troubleshooting

### Build Failures

Use `--verbose` flag with publish.py to see detailed error messages:
```bash
python3 publish.py idp-1234567890 idp us-east-1 --verbose
```

### Pattern-2 Container Build Issues

Ensure:
- Docker daemon is running
- AWS credentials have ECR permissions
- Sufficient disk space for container images

### UI Build Issues

Check:
- Node.js version >= 22.12.0
- Run `npm ci` in `src/ui/` to install dependencies
- Check `src/ui/.checksum` if builds are being skipped unexpectedly

## Skill Files

Domain-specific coding conventions, checklists, and workflows live in
`.claude/skills/`. Consult the relevant skill file whenever a task touches
that domain:

| Skill File | When to Use |
|------------|-------------|
| `.claude/skills/backend-lambda.md` | Writing Lambda handlers or `idp_common` Python code |
| `.claude/skills/frontend-ui.md` | React / TypeScript / Cloudscape UI changes |
| `.claude/skills/infrastructure.md` | CloudFormation / SAM templates, nested stacks, GovCloud |
| `.claude/skills/extraction-pipeline.md` | Document processing pipeline, configuration, agentic extraction |
| `.claude/skills/code-review.md` | Pre-commit self-review checklist for your own changes |
| `.claude/skills/srt-security-scan.md` | Running the SRT security scan (`make srt-scan`), triaging HIGH findings, and mitigating (`# nosec`/code fix) or suppressing (`scripts/srt/issues.json`) them |
| `.claude/skills/curate-security-results.md` | Publishing a public-safe, auditable snapshot of the four security tests (SRT, ZAP DAST, RBAC static/dynamic) into `security/test-results/<version>/` via `scripts/security/curate_results.py` |
| `.claude/skills/api-rbac-test.md` | Verifying API authorization (Cognito groups + config-version scope) via `make api-test` / `make api-test-static`; adding a new API operation |
| `.claude/skills/ux-test.md` | Browser-driven UX testing of the web UI against a live stack (`make ux-test`) — functional pass/fail per flow **plus** usability findings. The only test layer here that opens a browser; flows live in `scripts/ux_flows.yaml` |
| `.claude/skills/run-stack-tests.md` | Running the deploy-variant stack-tests (`make stacktest-*`: ZAP DAST, Jobs API, WAF, APIGateway hosting variants) manually against a live stack — they no longer run automatically in CI. Includes VPC auto-discovery + confirm for the VPC-requiring ones |
| `.claude/skills/transform-deploy-test.md` | Deploy-testing the `--headless` / `--govcloud` template **transforms** (`make transform-deploy-test-*`) — the only tier that deploys a transformed template and processes a real document. Includes the commercial-vs-GovCloud caveat you must report |
| `.claude/skills/pr-review.md` | Reviewing an external GitHub PR or GitLab MR at a URL (e.g. `review <url>`) |
| `.claude/skills/dependabot-prs.md` | Triaging Dependabot PRs — retarget to `develop`, per-PR risk assessment, redundancy check vs develop, merge-if-safe, mandatory post-merge test validation |
| `.claude/skills/create-hf-dataset-pr.md` | Contributing a data/label correction to an external HuggingFace dataset via a community PR (parquet key-order gotcha, verification, review artifacts) |
| `.claude/skills/testing-qa.md` | Writing tests, pytest patterns, moto, conftest setup |
| `.claude/skills/full-test-battery.md` | Running the FULL test battery (all suites + lint/typecheck) to validate a branch/merge; includes the known pre-existing-failure baseline so real regressions stand out |
| `.claude/skills/test-upgrade.md` | Validating an in-place CloudFormation stack upgrade between two published releases (X→Y) — deploy the FROM template, `update-stack` to the TO template, watch the `UpdateDefaultConfig` custom resource, diagnose/recover a rollback deadlock, tear down |
| `.claude/skills/live-eval-and-cost.md` | Live benchmark A/B, upgrade testing, reading accuracy/cost/confidence + prompt-cache/model cost facts |
| `.claude/skills/run-benchmarks.md` | Running the empirical benchmark suite in `benchmarks/` (config × doc-size matrix with exact ground truth; success/completeness/accuracy/calibration/time/tokens/cost) to produce the guidance paper or gate a change vs baseline |
| `.claude/skills/documentation.md` | Documentation standards, two doc tiers, CHANGELOG, docs-site, and the "adding a Bedrock model" checklist |
| `.claude/skills/add-model.md` | Adding or changing a selectable Bedrock model (model IDs, regions, limits, pricing, template enums, client routing, UI, both doc tiers, tests) — the full expanded checklist |
| `.claude/skills/prepare-changelog.md` | Preparing the `[Unreleased]` CHANGELOG section for release — three-section shape (Added/Changed/Fixed), net-since-release entries (drop intra-cycle churn), compact entries with doc/PR links |
| `.claude/skills/cut-release-changelog.md` | Cutting the release section at tag time — relabel `[Unreleased]` to the `VERSION` number (no date) and append the version-pinned `## Templates` URLs for the three published regions |

> **Note:** `.claude/skills/` is canonical. The Cline assistant's
> `.cline/skills/` files are **symlinks** to these (different filenames), so
> editing a `.claude` skill updates both — do not create separate `.cline`
> copies. See the "Two skill systems, one source of truth" section in
> `.claude/skills/documentation.md` for the filename map and portability caveat.

### Documentation lives in two tiers

When changing `idp_common` or adding/changing models, update **both** tiers:
1. **User/feature docs** — `docs/*.md` (published to the Starlight site).
2. **Developer/module docs** — `lib/idp_common_pkg/**/README.md` (one per
   subpackage; the canonical home for library/client behavior).

Adding a selectable Bedrock model touches many files (template enums,
`config_library/pricing.yaml`, `model_config_limits.yaml`, config models,
`update_configuration`, UI dropdown, the bedrock client, IAM, **both doc
tiers**, and `CHANGELOG.md`). Follow the checklist in
`.claude/skills/documentation.md` so none are missed.

### Reviewing External PRs / MRs

When the user asks `review <MR/PR URL>` (or similar), load
`.claude/skills/pr-review.md` and produce a structured review answering:

1. Is this a good PR?
2. Safe? No regressions?
3. Good UX?
4. No security issues?
5. Well documented?
6. Safe to merge?

The expected target branch for this repo is `develop` — flag any PR/MR that
targets a different branch as the first finding. The review is read-only: do
not push, merge, approve, or post comments on the PR/MR unless the user
explicitly asks.

