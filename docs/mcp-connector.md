---
title: "IDP MCP Connector"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# IDP MCP Connector

## 1. Overview

The **IDP MCP Connector** (`idp_mcp_connector`) is a lightweight local Python tool that bridges IDE-based AI coding assistants — such as [Cline](https://github.com/cline/cline) (VS Code) and [Kiro](https://kiro.dev) — to the IDP Accelerator's remote MCP Server running in AWS via Amazon Bedrock AgentCore Gateway.

The IDP Accelerator exposes document intelligence capabilities through a remote MCP Server that requires OAuth 2.0 authentication via Amazon Cognito. IDE tools like Cline use OAuth 2.1 with Dynamic Client Registration (RFC 7591), which Cognito does not support — causing direct connections to fail. The connector runs locally as a stdio MCP server, handles Cognito authentication internally using pre-registered credentials, and transparently forwards all tool calls to the remote server.

### Key Features

- **Zero-code integration** — developers use IDP tools directly from their IDE chat interface
- **Automatic token management** — Cognito OAuth tokens are acquired and refreshed transparently
- **Dynamic tool discovery** — tools are fetched from the remote server at startup; no hardcoded tool definitions
- **Cross-platform** — works on macOS, Windows, and Linux
- **Multi-IDE** — works with Cline, Kiro, or any MCP client that supports stdio transport
- **Future-proof** — automatically picks up new tools added to the MCP Server without code changes

---

## 2. Architecture Design

### High-Level Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│  Developer's Machine                                                      │
│                                                                           │
│  ┌──────────────┐   stdio (MCP)   ┌──────────────────────────────────┐    │
│  │   Cline      │ ◄─────────────► │       idp_mcp_connector          │    │
│  │   or Kiro    │                 │       (local Python process)     │    │
│  └──────────────┘                 └──────────────────────────────────┘    │
│                                                    │                      │
│                                    ┌───────────────┘                      │
│                                    │  1. Cognito Auth (client_credentials)│
│                                    ▼                                      │
│                           ┌─────────────────┐                             │
│                           │  AWS Cognito    │                             │
│                           │  Token Endpoint │                             │
│                           └─────────────────┘                             │
└───────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │  2. HTTPS + Bearer Token
                                    ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  AWS Cloud                                                               │
│                                                                          │
│  ┌──────────────────────┐       ┌─────────────────────────────────────┐  │
│  │  AgentCore Gateway   │ ────► │  agentcore_mcp_handler (Lambda)     │  │
│  │  (IDP MCP Server)    │       │                                     │  │
│  │                      │ ◄──── │  Available Tools:                   │  │
│  └──────────────────────┘       │  ├── search      (NL analytics)     │  │
│                                 │  ├── process     (submit docs)      │  │
│                                 │  ├── reprocess   (re-run pipeline)  │  │
│                                 │  ├── status      (batch monitor)    │  │
│                                 │  └── get_results (extracted data)   │  │
│                                 └─────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

## 3. How to Install

### Prerequisites

- Python 3.11 or higher
- An IDP Accelerator stack deployed with `EnableMCP: true`
- The following values from your CloudFormation stack outputs:

| CloudFormation Output | Description |
|----------------------|-------------|
| `MCPServerEndpoint` | The AgentCore Gateway HTTPS endpoint |
| `MCPTokenURL` | Cognito OAuth token endpoint URL |
| `MCPConnectorClientId` | The stack-created client-credentials client's ID (listed on the MCP Configuration page as "Default (stack-created)") |
| `MCPConnectorClientSecretArn` | The Secrets Manager ARN holding that client's secret |
| `MCPContentBucket` | S3 bucket for uploading documents (optional, for `process` tool) |

Prefer a [service credential](mcp-server.md#service-credentials) of the connector's own,
created on the MCP Configuration page: it carries only the role and configuration
profiles you give it, and its settings entry is shown ready to paste. To use the
stack-created client instead, read its secret with
`aws secretsmanager get-secret-value --secret-id <MCPConnectorClientSecretArn>`.

### Installation

Navigate to the IDP Accelerator directory and install the package:

```bash
# Install in development mode (recommended — keeps it up to date with repo changes)
pip install -e genaiic-idp-accelerator/lib/idp_mcp_connector_pkg

# Or install with uv (faster)
uv pip install -e genaiic-idp-accelerator/lib/idp_mcp_connector_pkg
```

### Verify Installation

```bash
python -m idp_mcp_connector --version
# Output: idp_mcp_connector 0.5.2

python -m idp_mcp_connector --help
# Displays usage information and required environment variables
```

---

## 4. Usage

### Configuration

The connector is configured entirely through environment variables. All four authentication variables are required:

| Environment Variable | Required | CloudFormation Output | Description |
|---------------------|----------|----------------------|-------------|
| `IDP_MCP_ENDPOINT` | ✅ Yes | `MCPServerEndpoint` | AgentCore Gateway URL |
| `IDP_MCP_TOKEN_URL` | ✅ Yes | `MCPTokenURL` | Cognito OAuth token endpoint |
| `IDP_MCP_CLIENT_ID` | ✅ Yes | a service credential, or `MCPConnectorClientId` | Cognito app client ID (machine-to-machine (M2M) `client_credentials` flow — no user login required) |
| `IDP_MCP_CLIENT_SECRET` | ✅ Yes | a service credential, or the secret at `MCPConnectorClientSecretArn` | Cognito app client secret |

### Configuring Cline

Add the following to your Cline MCP settings file (`~/.cline/data/settings/cline_mcp_settings.json`), using the values from your CloudFormation stack outputs:

```json
{
  "mcpServers": {
    "idp-mcp-server": {
      "notes": "Set command to the full path of the Python interpreter where idp_mcp_connector is installed",
      "command": "python3",
      "args": ["-m", "idp_mcp_connector"],
      "env": {
        "IDP_MCP_ENDPOINT": "<MCPServerEndpoint from CloudFormation outputs>",
        "IDP_MCP_TOKEN_URL": "<MCPTokenURL from CloudFormation outputs>",
        "IDP_MCP_CLIENT_ID": "<client ID of a service credential (MCP Configuration page)>",
        "IDP_MCP_CLIENT_SECRET": "<its client secret, shown once when created>"
      },
      "alwaysAllow": [],
      "disabled": false
    }
  }
}
```

> **Windows note**: Use `"command": "python"` instead of `"python3"`.

See [Section 5](#5-example-working-with-cline) for a complete step-by-step walkthrough.

---

### Available Tools

The connector dynamically discovers and exposes all tools from the IDP MCP Server. The current IDP MCP Server provides the following tools:

| Tool | Description |
|------|-------------|
| `search` | Natural language queries on processed document analytics (e.g., "How many invoices failed last week?") |
| `process` | Submit documents for processing from an S3 URI |
| `reprocess` | Re-run documents through the classification or extraction pipeline steps |
| `status` | Check processing status of a batch (total, completed, in-progress, failed counts) |
| `get_results` | Retrieve extracted fields, confidence scores, and metadata for processed documents |

---

## 5. Example: Working with Cline

This section walks through the complete setup and a realistic demo scenario using Cline in VS Code.

### Step 1: Deploy the IDP Stack with MCP Enabled

Ensure your `config.yaml` has MCP enabled before deploying:

```yaml
EnableMCP: 'true'
```

After deployment, navigate to the CloudFormation console and copy these output values:

```
MCPServerEndpoint       →  https://xxx.bedrock-agentcore.us-east-1.amazonaws.com/mcp
MCPTokenURL             →  https://idp-stack-xxx.auth.us-east-1.amazoncognito.com/oauth2/token
MCPConnectorClientId    →  abc123def456ghi789
MCPConnectorClientSecretArn → arn:aws:secretsmanager:...:secret:<stack>/mcp/credentials/abc123def456ghi789
MCPContentBucket        →  idp-stack-mcp-content-bucket-xxx
```

### Step 2: Install the Connector

```bash
pip install -e genaiic-idp-accelerator/lib/idp_mcp_connector_pkg
```

### Step 3: Configure Cline

Cline stores MCP server configuration in a single shared file used by both VS Code and JetBrains IDEs:

```
~/.cline/data/settings/cline_mcp_settings.json
```

On macOS this expands to `/Users/<your-username>/.cline/data/settings/cline_mcp_settings.json`.

**Open the file:**
- **VS Code**: Cline sidebar → MCP Servers icon → Configure tab → "Configure MCP Servers"
- **JetBrains (PyCharm)**: Cline tool window → MCP Servers → Settings gear icon → "Edit MCP Settings"
- Or open it directly in any text editor using the path above

Add the IDP connector configuration using the values from your CloudFormation stack outputs:

```json
{
  "mcpServers": {
    "idp-mcp-server": {
      "notes": "Set command to the full path of the Python interpreter where idp_mcp_connector is installed",
      "command": "python3",
      "args": ["-m", "idp_mcp_connector"],
      "env": {
        "IDP_MCP_ENDPOINT": "https://xxx.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        "IDP_MCP_TOKEN_URL": "https://idp-stack-xxx.auth.us-east-1.amazoncognito.com/oauth2/token",
        "IDP_MCP_CLIENT_ID": "abc123def456ghi789",
        "IDP_MCP_CLIENT_SECRET": "<your-client-secret>"
      },
      "alwaysAllow": [],
      "disabled": false
    }
  }
}
```

> **Windows note**: Use `"command": "python"` instead of `"python3"`.

After saving, Cline automatically detects the file change and starts the connector process.

### Step 4: Verify Connection

After saving the configuration, Cline automatically launches the connector process in the background. To confirm it is working:

1. Click the **MCP Servers icon** in the top-right corner of the Cline panel
2. Select the **"Installed"** tab (or **"Configure"** tab)
3. Find **`idp-mcp-server`** in the list and check its status indicator:
   - **🟢 Green dot** — connector is running, authenticated with Cognito, and ready
   - **🟡 Yellow dot** — connector is starting up (wait a few seconds, then refresh)
   - **🔴 Red dot** — connector failed to start (see Troubleshooting below)
4. Click on **`idp-mcp-server`** to expand the entry — you should see all 5 tools listed under it:

   | Tool | Status |
   |------|--------|
   | `search` | Available |
   | `process` | Available |
   | `reprocess` | Available |
   | `status` | Available |
   | `get_results` | Available |

5. If you see the green dot and all 5 tools — the connector is fully operational. You can now use IDP capabilities directly from the Cline chat.

> **Tip**: You can also verify from the terminal before configuring Cline. Run the connector manually with your environment variables set — if you see `"Discovered 5 tools: search, process, reprocess, status, get_results"` in the output, the credentials and endpoint are correct.

### Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| Red dot in Cline MCP panel | Connector not starting | Check Python is on PATH; run `python -m idp_mcp_connector` manually to see error |
| `Authentication failed` | Wrong credentials | Verify `IDP_MCP_CLIENT_ID` and `IDP_MCP_CLIENT_SECRET` from CloudFormation outputs |
| `No tools discovered` | Wrong endpoint | Verify `IDP_MCP_ENDPOINT` value; check MCP is enabled in your IDP stack |
| `Tool call failed: 401` | Token expired (shouldn't happen — connector auto-refreshes) | Restart Cline MCP server |
| `Tool call failed: 403` | Insufficient permissions | Verify the Cognito client has the correct OAuth scopes |
| Tools not showing | Connector crashed silently | Check Cline's MCP server logs (MCP Servers panel → server name → logs) |

---

### Related Documentation

- [MCP Server](./mcp-server.md) — IDP MCP Server overview and tool reference
- [Custom MCP Agent](./custom-MCP-agent.md) — Connecting your own MCP servers to the IDP web interface
- [IDP CLI](./idp-cli.md) — Command-line interface for IDP operations
- [IDP SDK](./idp-sdk.md) — Python SDK for programmatic IDP access
