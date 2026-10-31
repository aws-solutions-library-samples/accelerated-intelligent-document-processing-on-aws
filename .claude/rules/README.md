# Claude Code rules

These are modular rule files for Claude Code. A file with a `paths:` list in its
frontmatter loads only when Claude works on a file that matches one of those
globs, which are relative to the repository root. A file without frontmatter
always loads.

| Rule file | Scope | Purpose | Source |
|---|---|---|---|
| [backend-lambda.md](backend-lambda.md) | `src/lambda/**`, `lib/**/*.py` | Thin handlers, reuse of `idp_common` with no duplication, and Lambda conventions | [backend-lambda](../skills/backend-lambda.md), [code-review](../skills/code-review.md) |
| [frontend-ui.md](frontend-ui.md) | `src/ui/**` | Consistent UI: Cloudscape components, reuse of shared components and hooks, uniform loading, empty and error states | [frontend-ui](../skills/frontend-ui.md), [pr-review](../skills/pr-review.md) |
| [infrastructure.md](infrastructure.md) | templates, `nested/**`, `patterns/**`, `iam-roles/**` | Partition-safe templates, log groups, retriers, IAM, and keeping the service inventories in sync | [infrastructure](../skills/infrastructure.md) |
| [api-authorization.md](api-authorization.md) | `nested/api-resolvers/**`, `src/lambda/**`, `src/ui/src/graphql/**`, `scripts/api_rbac_expectations.yaml` | Default-deny operations, group and scope checks, Function URL paths | [api-rbac-test](../skills/api-rbac-test.md), [docs/rbac.md](../../docs/rbac.md) |
| [sdk-cli-mcp.md](sdk-cli-mcp.md) | `lib/idp_sdk/**`, `lib/idp_cli_pkg/**`, `lib/idp_mcp_connector_pkg/**` | The CLI calls the SDK and the SDK calls `idp_common`, with nothing duplicated; public interface; CLI output | [docs/idp-sdk.md](../../docs/idp-sdk.md), [docs/idp-cli.md](../../docs/idp-cli.md) |
| [agents.md](agents.md) | `idp_common/agents/**`, `src/lambda/*agent*/**` | Agent registration, sandboxing, both chat delivery paths, data sent to third parties | [agents README](../../lib/idp_common_pkg/idp_common/agents/README.md), [docs/agent-companion-chat.md](../../docs/agent-companion-chat.md) |
| [config-and-models.md](config-and-models.md) | `config_library/**`, `idp_common/config/**`, `idp_common/bedrock/**` | Config merging, new settings, and how to add or retire a model | [add-model](../skills/add-model.md), [docs/configuration.md](../../docs/configuration.md) |
| [testing.md](testing.md) | `**/tests/**`, `scripts/tests/**`, `**/test_*.py` | Test structure, registering suites, hermetic runs, proving a test can go red | [testing-qa](../skills/testing-qa.md) |
| [lint-and-gates.md](lint-and-gates.md) | always on | Local gates, lint debt, the gate exemption registry, CI parity | [CLAUDE.md](../../CLAUDE.md), [code-review](../skills/code-review.md) |
| [documentation.md](documentation.md) | `docs/**`, `**/README.md`, `CHANGELOG.md`, `.claude/skills/**` | Page template, updating both doc tiers, links, writing what is true now | [documentation](../skills/documentation.md) |
| [security-and-published-text.md](security-and-published-text.md) | always on | Secrets, safe installation of first-party packages, untrusted input, public commit and PR text | [CLAUDE.md](../../CLAUDE.md), [code-review](../skills/code-review.md) |
| [feature-platform.md](feature-platform.md) | `feature-platform/**` | Host contract, registration, hooks, entitlement, publishing with the SDK CLI | [docs/feature-platform-developer-guide.md](../../docs/feature-platform-developer-guide.md) |

## Maintaining these rules

- Update these files with the workspace `/work-update-rules` command, not by
  editing them ad hoc.
- Rules carry no IDs. Each one is a short, normative bullet under **Must** or
  **Must not**, with **Verify with** naming the gate that checks it.
- Keep each rule short. The detail, the reasoning and the procedures belong in the
  source skill or doc listed under **Sources**. Link to it instead of copying it.
- Each rule lives in exactly one file. If a topic spans files, link to the file
  that owns it.
- Write what is true now, and don't put counts of files, tests or other items in
  these files (see [lint-and-gates.md](lint-and-gates.md)).
