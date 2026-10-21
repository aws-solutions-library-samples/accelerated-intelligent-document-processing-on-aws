---
paths:
  - "lib/idp_sdk/**"
  - "lib/idp_cli_pkg/**"
  - "lib/idp_mcp_connector_pkg/**"
---

# SDK, CLI and MCP connector rules

Covers `idp_sdk` (the programmatic API), `idp_cli` (`idp-cli`, the command-line
tool built on the SDK) and `idp_mcp_connector`, a local stdio bridge to the
deployed MCP server.

## Must
- Treat `IDPClient` as the single entry point, and export every new public model,
  enum or exception from the top-level `idp_sdk` package.
- **Keep the layering and duplicate nothing.**
  - The CLI wraps the SDK, and the SDK wraps `idp_common`.
  - Put a new capability in an SDK operation namespace first. The CLI command is
    then a thin call to it.
  - Logic that a Lambda also needs goes in `idp_common`.
- **Reuse `idp_common` from the SDK; don't reimplement it.** That covers config
  loading and merging, the `Document` models, S3 URI parsing and the agent
  factory. Import it lazily inside the function that needs it, as `_core` does.
- Raise exceptions from `idp_sdk.exceptions` (`IDPError` and its subclasses), not
  bare `Exception` or `RuntimeError`.
- Pass `--region` to every AWS call a command makes, not only to the
  CloudFormation lookup. Outside a Lambda, never create a configuration client
  without a region.
- When a command emits a machine-readable payload, write only that payload to
  stdout, verbatim, with no colour and no wrapping. Send progress and status to
  stderr.
- Make list and search operations support pagination.
- Have the MCP connector discover tools from the remote server at startup and
  acquire and refresh Cognito tokens internally.
- Talk to MCP endpoints over HTTPS only. Server-side MCP tools must validate their
  inputs and expose only the operations they need.
- Document a user-visible change in `docs/idp-sdk.md`, `docs/idp-cli.md` or
  `docs/mcp-connector.md` (see [documentation.md](documentation.md)).

## Must not
- Import `idp_sdk._core` or `idp_sdk.operations` internals from outside the SDK.
  Ruff `TID251` bans `idp_sdk._core`.
- Introduce a reverse dependency: `idp_sdk` importing `idp_cli`, or `idp_common`
  importing either of them. Don't reach across packages with `sys.path` hacks.
- Copy SDK logic into a CLI command, or `idp_common` logic into the SDK, to make a
  change quicker.
- Hardcode MCP tool definitions in the connector.
- Print or log Cognito tokens, client secrets or full JWTs.
- Install first-party packages by bare name (see
  [security-and-published-text.md](security-and-published-text.md)).

## Verify with
- `cd lib/idp_sdk && pytest -m "not integration" -v`
- `cd lib/idp_cli_pkg && python -m pytest -v`
- `make test-packages-cicd` and `make ruff-lint`
- `scripts/tests/test_config_region_threading.py` and
  `scripts/tests/test_doc_install_commands.py`

## Sources
- [docs/idp-sdk.md](../../docs/idp-sdk.md): the public interface, operation
  namespaces, models and error handling
- [docs/idp-cli.md](../../docs/idp-cli.md): commands, region threading and
  machine-readable output
- [docs/mcp-connector.md](../../docs/mcp-connector.md) and
  [docs/custom-MCP-agent.md](../../docs/custom-MCP-agent.md): connector
  architecture and MCP security practices
- [.claude/skills/backend-lambda.md](../skills/backend-lambda.md): the banned
  imports
