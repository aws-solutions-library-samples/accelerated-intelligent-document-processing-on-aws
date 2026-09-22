# IDP Accelerator Scripts

This directory contains utility scripts for building, testing, deploying, and operating the IDP Accelerator.

## Directory Structure

```
scripts/
├── setup/               # Development environment setup scripts
├── srt/                 # SRT (Sample Security Review Tool) integration
├── sdlc/                # SDLC CI/CD scripts and infrastructure
│   ├── cfn/             # CloudFormation templates for CI/CD pipeline
│   └── [scripts]        # CI/CD automation scripts
└── generate_govcloud_template.py  # GovCloud template generation (deprecated — use `idp-cli publish --headless`)
```

## Subdirectories

### `setup/` - Development Environment Setup
Setup scripts for different operating systems. See [setup/README.md](setup/README.md).

### `srt/` - SRT Security Scanning
SRT (Sample Security Review Tool) integration for automated security scanning. See [srt/README.md](srt/README.md).

### `sdlc/` - SDLC CI/CD Scripts and Infrastructure
CloudFormation templates and scripts for CI/CD pipeline infrastructure.

| Script | Purpose | Usage |
|--------|---------|-------|
| `codebuild_deployment.py` | CodeBuild deployment automation | Used by CI/CD pipeline |
| `integration_test_deployment.py` | Integration test deployment | Used by CI/CD pipeline |
| `validate_buildspec.py` | Validate buildspec.yml files | See [sdlc/README_validate_buildspec.md](sdlc/README_validate_buildspec.md) |
| `typecheck_pr_changes.py` | Type check Python files in PRs | Used by CI/CD pipeline |
| `validate_service_role_permissions.py` | Validate IAM service role permissions | `python scripts/sdlc/validate_service_role_permissions.py` |

See [sdlc/cfn/README.md](sdlc/cfn/README.md) for CloudFormation templates.

## Utility Scripts

| Script | Purpose | Usage |
|--------|---------|-------|
| `discover_model_limits.py` | Empirically test Bedrock model max_tokens limits | `python scripts/discover_model_limits.py` |
| `test_api_rbac.py` | Live RBAC/auth/arg-mapping test of the REST API across all Cognito roles | `python scripts/test_api_rbac.py --stack-name <stack> --region <region>` |
| `ux_test_session.py` | Web URL and throwaway Cognito user for a browser UX review (see `.claude/skills/ux-test.md`) | `python scripts/ux_test_session.py url <stack> --region <region>` |
| `ux_recorder.py` | Record a UX review or a product demo as a narrated, captioned mp4 (sidecar to the ux-test and product-demo skills; see below) | `python scripts/ux_recorder.py start --stack <stack> --persona Admin --url-contains cloudfront` |
| `demo_storyboards.yaml` | Confirmed product-demo storyboards, so a demo can be re-recorded on a later release (see `.claude/skills/product-demo.md`) | — |
| `generate_govcloud_template.py` | Generate GovCloud-compatible template (**deprecated** — use `idp-cli publish --headless`) | `idp-cli publish --source-dir . --region <region> --headless` |

### UX review and demo recorder (`ux_recorder.py`)

Turns a browser session (the agent driving the debug Chrome per
`.claude/skills/ux-test.md` or `.claude/skills/product-demo.md`) into an mp4 with
spoken narration and an embedded subtitle track, so a review can be shown to the
team instead of re-run, and a feature can be demonstrated without a live walkthrough.

**How it works:** `start` attaches a second DevTools session to the tab being
reviewed (same Chrome on `:9222` the MCP server uses) and captures screencast
frames, which Chrome emits only when the screen changes. The agent calls `mark`
with a narration line before each step, `note` for observations, `pause`/`resume`
around long waits, then `stop`. `render` synthesizes the narration with Amazon
Polly's generative engine, lays the recording out for a human viewer (idle gaps
clamped, paused stretches dropped, each mark's frame held while the voice starts,
speed-ups capped at 3×, a settle before the next chapter), adds title and end
cards, and encodes one mp4 plus `review.srt`, `segments.json` and a chapter table
in `review.md`. Every click is logged with its coordinates and the element under
the pointer; `render` draws each click as a ring on the last frame before it and
holds that frame briefly (the page changes too fast for a live marker to be seen
on the right screen), prints the click table, and flags clicks that hit nothing
interactive, which is how a mis-aimed automation click is told apart from a UI bug.

```bash
make ux-record-deps                                   # ffmpeg, ffprobe, boto3, Pillow
./scripts/ux_recorder.py targets
./scripts/ux_recorder.py start --stack <STACK> --persona Admin --flow 5.1 --url-contains cloudfront --say "..."
./scripts/ux_recorder.py mark "Open the annotation queue" --say "We open the queue from the set's page."
./scripts/ux_recorder.py stop --say "That ends the review."
AWS_PROFILE=default ./scripts/ux_recorder.py render --voice Ruth        # --dry-run prints the pacing table

# a product demo: title card from --title/--subtitle, Key-takeaways end card from demo.md, demo.mp4
./scripts/ux_recorder.py start --kind demo --stack <STACK> --title "Editing test sets in place" \
    --subtitle "Version 0.6.9" --url-contains cloudfront --say "..."
```

`--kind` (default `review`) decides the cards, the report skeleton and the output
names: a review's title card names the stack and persona and its end card lists the
Findings from `review.md`; a demo's carries the `--title` and `--subtitle` lines and
its end card lists the Key takeaways from `demo.md` (`demo.mp4`, `demo.srt`).
Everything else — marks, pauses, click markers, pacing, captions — is shared.

Output lives under `scratch/ux-recordings/<stack>-<timestamp>/` (demos:
`demo-<title>-<timestamp>/`; gitignored):
recordings of a live stack show real documents and are never committed. Polly
receives only the narration text. Stdlib plus boto3/Pillow at render time; the
WebSocket client is vendored in `ux_recorder_cdp.py` rather than adding a
dependency. Unit tests: `scripts/tests/test_ux_recorder.py`.

### Model Limit Discovery (`discover_model_limits.py`)

Tests actual Bedrock API behavior to discover model `max_tokens` limits, then auto-generates `config_library/model_config_limits.yaml`.

**Why:** Instead of trusting documentation, we verify limits empirically to prevent runtime failures.

**Basic usage:**
```bash
# Test all supported models and generate config
python scripts/discover_model_limits.py

# Test specific models only
python scripts/discover_model_limits.py \
    --models "us.anthropic.claude-sonnet-4-20250514-v1:0"

# Verbose mode
python scripts/discover_model_limits.py --verbose
```

**When to use:**
- Adding a new Bedrock model → Add to `DEFAULT_MODELS_TO_TEST`, run script, commit YAML
- Verifying limits are accurate → Run with `--verbose` to see test results
- Investigating one model → Use `--models` flag with specific model ID

**How it works:**
1. Progressively tests larger `max_tokens` values until API rejects it
2. Catches `ValidationException` to identify exact limit
3. Groups models by limit and creates regex patterns
4. Generates YAML with test dates and verified limits

**Requirements:** AWS credentials with Bedrock `InvokeModel` permissions

**See also:** [`idp-cli config-validate`](../docs/idp-cli.md#config-validate), which checks a configuration's `classification.max_tokens` and `summarization.max_tokens` against the verified limits this script writes

### API RBAC / Auth Test (`test_api_rbac.py`)

Drives the deployed REST API (the `/op/<field>` dispatcher that took over when
AppSync was removed) as each Cognito group — **Admin, Author, Viewer, Reviewer** — plus
unauthenticated, and asserts the authorization outcome of every UI operation
against the schema baseline in `nested/api-resolvers/src/api/schema.graphql`.
That schema is no longer served by anything — AppSync was removed — but it is
retained because its per-field `@aws_cognito_user_pools` directives remain the
declared source of truth for which Cognito groups may call which operation.

**Why:** The UI used to talk to AppSync, where `@aws_cognito_user_pools(cognito_groups:[...])`
schema directives gated operations *before* the resolver ran. The API Gateway
REST transport that replaced it uses a Cognito authorizer that only
*authenticates*, so each resolver
(and the dispatcher's `ddb_direct` module) must re-enforce the group check
itself. A `curl`/`idp-cli` smoke test with an admin identity does **not**
exercise per-role RBAC, so group regressions (a Viewer reaching an Admin-only
op, a resolver soft-denying with HTTP 200, a broken argument mapping) slip
through. This script catches them.

Per operation it verifies:
- unauthenticated → `401`
- a **disallowed** role → `403` (`errorType: "Unauthorized"`)
- an **allowed** role → not denied (read ops use valid args, so a `BadRequest`
  flags an arg-mapping regression; mutation ops use nonexistent ids, so a
  benign validation error is expected and only proves auth passed)
- backend/IAM-only ops (e.g. `updateAgentJobStatus`) → `403` for every Cognito role

**Basic usage:**
```bash
# Full run: create test users, test all ops x 4 roles, tear down. Exit 0 = pass.
AWS_PROFILE=default python3 scripts/test_api_rbac.py --stack-name IDP1 --region us-west-2

# Iterate faster (keep users between runs):
python3 scripts/test_api_rbac.py --stack-name IDP1 --region us-west-2 --setup-only
python3 scripts/test_api_rbac.py --stack-name IDP1 --region us-west-2 --no-teardown
python3 scripts/test_api_rbac.py --stack-name IDP1 --region us-west-2 --teardown-only
```

**When to use:**
- After any change to a UI-facing resolver, the dispatcher, `ddb_direct`, or the
  REST client's argument mapping.
- Before merging changes to `nested/api-resolvers/` — to confirm RBAC parity with
  the `schema.graphql` baseline is preserved.
- When adding a new operation: add it to `READ_OPS`/`MUTATION_OPS` with its
  required groups (mirroring the directive in `schema.graphql`).

**Safety:** Test users are `test-rbac-<role>@example.invalid` (created and
deleted by the script); mutation ops use nonexistent ids so allowed callers hit
benign validation, not real data. The script temporarily enables
`ALLOW_ADMIN_USER_PASSWORD_AUTH` on the UI app client and **always** reverts it
(even on failure). Nothing is destructive to stack data.

**Requirements:** AWS CLI v2 on `PATH`; credentials with Cognito admin +
CloudFormation read (the same credentials used for `idp-cli deploy`). Resolves
the UI user pool, app client, and API base URL from the stack — no hardcoding.

## Operational Commands (via idp-cli)

The following operations are available through the IDP CLI tool:

| Operation | CLI Command |
|-----------|-------------|
| Document status lookup | `idp-cli status --stack-name <name> --document-id <id>` |
| Batch status | `idp-cli status --stack-name <name> --batch-id <id>` |
| Stop workflows | `idp-cli stop-workflows --stack-name <name>` |
| Load testing | `idp-cli load-test --stack-name <name> --rate 2500` |
| Remove residual resources | `idp-cli remove-deleted-stack-resources --dry-run` |

See [CLI Documentation](../docs/idp-cli.md) for complete command reference.

## Migration Notes

The following scripts were migrated to the `idp-cli` tool and removed from this directory:

| Removed Script | Replacement CLI Command |
|----------------|------------------------|
| `lookup_file_status.sh` | `idp-cli status --stack-name <name> --document-id <id>` |
| `simulate_load.py` | `idp-cli load-test --stack-name <name> --rate 100` |
| `simulate_dynamic_load.py` | `idp-cli load-test --stack-name <name> --schedule schedule.csv` |
| `stop_workflows.sh` | `idp-cli stop-workflows --stack-name <name>` |
| `cleanup_orphaned_resources.py` | `idp-cli remove-deleted-stack-resources --dry-run` |

The CLI provides a unified interface with better error handling, progress display, and consistent options.
See [IDP CLI Documentation](../docs/idp-cli.md) for complete usage.

## Related Documentation

- [IDP CLI Documentation](../docs/idp-cli.md)
- [Deployment Guide](../docs/deployment.md)
- [Development Setup](../docs/setup-development-env-macos.md)
- [GovCloud Deployment](../docs/govcloud-deployment.md)