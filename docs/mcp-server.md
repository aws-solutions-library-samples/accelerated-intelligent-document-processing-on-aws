---
title: "MCP Server"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# MCP Server

The GenAI IDP solution provides MCP (Model Context Protocol) integration that enables external applications like Amazon Quick Suite to access IDP functionality through AWS Bedrock AgentCore Gateway. This allows third-party applications to query processed document data and perform analytics operations through natural language interfaces.

## Overview

The MCP integration exposes IDP capabilities to external applications by:

- **Analytics Gateway**: Provides natural language access to processed document analytics data
- **Secure Authentication**: Uses AWS Cognito OAuth 2.0 for secure external application access
- **MCP Protocol**: Implements Model Context Protocol for standardized tool integration
- **Real-time Queries**: Enables external applications to query document processing results in real-time
- **Extensible Architecture**: Designed to support additional IDP functionality in future releases

#### Demo with Quick Suite
https://github.com/user-attachments/assets/529ce6ad-1062-4af5-97c1-86c3a47ac12c

#### Demo with Cline 
https://github.com/user-attachments/assets/28d3a358-7aec-4c40-9081-ad4683d2a89f

## External Application Integration

External applications can integrate with the IDP system through the AgentCore Gateway by:

1. **Authentication**: Obtaining OAuth tokens from the IDP's Cognito User Pool
2. **Gateway Connection**: Connecting to the AgentCore Gateway endpoint
3. **Tool Discovery**: Discovering available analytics tools via MCP protocol
4. **Query Execution**: Executing natural language queries against processed document data

### Integration Flow

```
External App → Cognito Auth → AgentCore Gateway → Analytics Lambda → IDP Data
```

## Enabling and Disabling the Feature

### During Stack Deployment

The MCP integration is controlled by the `EnableMCP` parameter:

**Enable MCP Integration:**
```yaml
EnableMCP: 'true'  # Default value
```

**Disable MCP Integration:**
```yaml
EnableMCP: 'false'
```

When enabled, the stack automatically creates:
- AgentCore Gateway Manager Lambda function
- AgentCore Analytics Lambda function
- External App Client in Cognito User Pool
- Required IAM roles and policies
- AgentCore Gateway resource
- MCP Content Bucket for document uploads

When disabled, these resources are not created, reducing deployment complexity and costs.

## Current Capabilities

The AgentCore Gateway provides five integrated tools for document processing and analytics:

### search

Natural language queries for document analytics and system information.

**Input Schema:**
```json
{
  "query": {
    "type": "string",
    "description": "Natural language question about processed documents or analytics data"
  }
}
```

**Output Schema:**
```json
{
  "success": "boolean",
  "query": "string",
  "result": "string"
}
```

**Example Request:**
```json
{
  "method": "tools/call",
  "params": {
    "name": "search",
    "arguments": {
      "query": "How many documents were processed last month?"
    }
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "query": "How many documents were processed last month?",
  "result": "1,250 documents were processed in the last month with a 98.5% success rate."
}
```

### process

Process documents from S3 or base64 content. To process documents via S3:

1. Upload documents to the `MCPContentBucket` (available in CloudFormation stack outputs):
   ```bash
   aws s3 cp documents/ s3://<MCPContentBucket>/documents/ --recursive
   ```
2. Call the `process` tool with the S3 URI pointing to your uploaded documents
3. The tool queues documents for processing through the IDP pipeline

**Alternatively, process documents via base64 content** by providing the encoded content directly to the tool.

**Input Schema:**
```json
{
  "location": {
    "type": "string",
    "description": "S3 URI for batch processing (e.g., 's3://mcp-content-bucket/documents/'). Optional if content is provided."
  },
  "content": {
    "type": "string",
    "description": "Base64-encoded document content for single document processing. Optional if location is provided."
  },
  "name": {
    "type": "string",
    "description": "Document filename with extension (e.g., 'invoice.pdf'). Required if content is provided."
  },
  "prefix": {
    "type": "string",
    "description": "Optional batch ID prefix (default: 'mcp-batch')"
  },
  "profile": {
    "type": "string",
    "description": "Optional Configuration Profile to process under. Without it the destination decides (a configured prefix mapping, else the active profile)."
  }
}
```

**Output Schema:**
```json
{
  "success": "boolean",
  "batch_id": "string",
  "documents_queued": "integer",
  "message": "string"
}
```

**Example Request (S3 Location):**
```json
{
  "method": "tools/call",
  "params": {
    "name": "process",
    "arguments": {
      "location": "s3://mcp-content-bucket/documents/",
      "prefix": "batch-001"
    }
  }
}
```

**Example Request (Base64 Content):**
```json
{
  "method": "tools/call",
  "params": {
    "name": "process",
    "arguments": {
      "content": "JVBERi0xLjQKJeLjz9MNCjEgMCBvYmo...",
      "name": "invoice.pdf",
      "prefix": "mcp-batch"
    }
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "batch_id": "mcp-batch-20250124-143000",
  "documents_queued": 5,
  "message": "Successfully queued 5 documents for processing"
}
```

### reprocess

Reprocess documents from classification or extraction steps.

**Input Schema:**
```json
{
  "step": {
    "type": "string",
    "enum": ["classification", "extraction"],
    "description": "Pipeline step to reprocess from"
  },
  "document_ids": {
    "type": "string",
    "description": "Comma-separated list of document IDs to reprocess (alternative to batch_id)"
  },
  "batch_id": {
    "type": "string",
    "description": "Batch ID to get document IDs from (alternative to document_ids)"
  },
  "region": {
    "type": "string",
    "description": "AWS region (optional)"
  }
}
```

**Output Schema:**
```json
{
  "success": "boolean",
  "batch_id": "string",
  "documents_queued": "integer",
  "step": "string",
  "message": "string"
}
```

**Example Request:**
```json
{
  "method": "tools/call",
  "params": {
    "name": "reprocess",
    "arguments": {
      "step": "extraction",
      "batch_id": "mcp-batch-20250124-143000"
    }
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "batch_id": "mcp-batch-20250124-143000",
  "documents_queued": 5,
  "step": "extraction",
  "message": "Successfully queued 5 documents for extraction reprocessing"
}
```

### get_results

Retrieve processing results and extracted metadata for documents. Two addressing modes:

- **Batch mode** — pass `batch_id` to retrieve paginated results for every document in a batch.
- **Single-document mode** — pass `document_id` to retrieve one document's results with no batch id. The document id is the S3 object key of the source document; an `s3://` URI of the document's output prefix (e.g. the `idp_raw_ref` a post-processing hook consumer receives, like `s3://<output-bucket>/Borrowing_Notice_#3.pdf/`) is also accepted. Use this when a downstream integration only holds a document reference — hook events do not carry batch ids.

Exactly one of `batch_id` or `document_id` must be provided; passing neither returns a `missing_identifier` error.

**Input Schema:**
```json
{
  "batch_id": {
    "type": "string",
    "description": "Batch identifier (e.g., 'mcp-batch-20250124-143022') to retrieve results for every document in the batch. Provide either batch_id or document_id."
  },
  "document_id": {
    "type": "string",
    "description": "Single document identifier — the document's S3 object key or its s3:// output-prefix URI. Provide either batch_id or document_id."
  },
  "section_id": {
    "type": "integer",
    "description": "Section number within documents (default: 1). Use for multi-section documents like healthcare packages."
  },
  "limit": {
    "type": "integer",
    "description": "Maximum documents to return per page (default: 10, max: 100). Batch mode only."
  },
  "next_token": {
    "type": "string",
    "description": "Pagination token from previous request for retrieving next page of results. Batch mode only."
  }
}
```

**Output Schema:**
```json
{
  "success": "boolean",
  "batch_id": "string (null in single-document mode)",
  "section_id": "integer",
  "count": "integer",
  "total_in_batch": "integer (null in single-document mode)",
  "documents": "array",
  "next_token": "string (optional)",
  "message": "string"
}
```

**Example Request:**
```json
{
  "method": "tools/call",
  "params": {
    "name": "get_results",
    "arguments": {
      "batch_id": "mcp-batch-20250124-143022",
      "section_id": 1,
      "limit": 10
    }
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "batch_id": "mcp-batch-20250124-143022",
  "section_id": 1,
  "count": 1,
  "total_in_batch": 1,
  "documents": [
    {
      "document_id": "mcp-batch-20250124-143022/document-001.pdf",
      "document_class": "invoice",
      "fields": {
        "vendor_info": {
          "name": "<vendor_name>",
          "address": "<vendor_address>",
          "tax_id": "<tax_id>"
        },
        "line_items": [
          {"description": "<item_description>", "amount": "<amount>"},
          {"description": "<item_description>", "amount": "<amount>"}
        ],
        "total_amount": "<total>",
        "invoice_date": "<date>"
      },
      "confidence": {
        "vendor_info": {
          "name": 0.98,
          "address": 0.95,
          "tax_id": 1.0
        },
        "total_amount": 0.99,
        "invoice_date": 0.97
      },
      "page_count": 1,
      "status": "COMPLETED"
    }
  ],
  "message": "Retrieved results for 1 document"
}
```

### status

Query batch and document processing status.

**Input Schema:**
```json
{
  "batch_id": {
    "type": "string",
    "description": "Batch identifier (e.g., 'mcp-batch-20250124-143000')"
  },
  "options": {
    "type": "object",
    "description": "Optional status parameters",
    "properties": {
      "detailed": {
        "type": "boolean",
        "description": "Include per-document details (default: false)"
      },
      "include_errors": {
        "type": "boolean",
        "description": "Include error details (default: true)"
      }
    }
  },
  "region": {
    "type": "string",
    "description": "AWS region (optional)"
  }
}
```

⚠️ **`include_errors` is advertised but not implemented.** The handler reads only
`options.detailed`; `include_errors` is accepted and ignored, and the response carries
no error-detail field either way. What you get instead is the **count** of failures in
`status.failed`, plus — with `detailed: true` — each document's own `status` and
`review_status`. For the text of a failure, use `get_results` on the document, or the
[Error Analyzer](./error-analyzer.md).

**Output Schema:**
```json
{
  "success": "boolean",
  "batch_id": "string",
  "status": {
    "total": "integer",
    "completed": "integer",
    "in_progress": "integer",
    "failed": "integer",
    "queued": "integer"
  },
  "progress": {
    "percentage": "number"
  },
  "all_complete": "boolean"
}
```

With `options.detailed: true` the response gains a `documents` array, one entry per
document in the batch, each with `document_id`, `status`, `duration_seconds` and
`review_status`.

**Example Request:**
```json
{
  "method": "tools/call",
  "params": {
    "name": "status",
    "arguments": {
      "batch_id": "mcp-batch-20250124-143000",
      "options": {
        "detailed": true
      }
    }
  }
}
```

**Example Response:**
```json
{
  "success": true,
  "batch_id": "mcp-batch-20250124-143000",
  "status": {
    "total": 5,
    "completed": 3,
    "in_progress": 1,
    "failed": 0,
    "queued": 1
  },
  "progress": {
    "percentage": 60.0
  },
  "all_complete": false
}
```

## Service credentials

People sign in to the MCP server as themselves, through the user-authentication
client (`MCPClientId`), and get their own role and configuration profiles. A client
that signs in as a service instead uses a **service credential**: a Cognito app
client that allows only the client-credentials flow, with its own role,
configuration profiles, expiry and secret.

Admins manage them on the **MCP Configuration** page (Configuration › MCP
Configuration, shown when MCP is enabled), with `idp-cli mcp credentials`, or with
`IDPClient.mcp`.

- **Create.** Choose a name, a role (Viewer, Reviewer, Author; Admin only when
  `TenantIsolationMode=Off` and `TenantGrantSource=Manual`), the configuration
  profiles it may see (required under tenant isolation, and within one tenant), whether
  it may use the write tools `process` and `reprocess` (Admin and Author only), and an
  expiry (90 days by default; never is allowed). The client ID and secret are shown
  **once**, with a ready-made Amazon Quick service-to-service block and an MCP
  Connector settings entry. The secret is also stored in Secrets Manager as
  `<stack>/mcp/credentials/<clientId>`, encrypted with the stack's key, where an
  operator with access to that secret can read it.
- **Use.** Exchange the client ID and secret at the token URL (`MCPTokenURL`) for an
  access token that lasts one hour, and call the MCP server with it. The gateway's
  interceptor reads the credential on every call, so its role and profiles apply as
  they would to a user's.
- **Rotate.** Issues a replacement with the same access and shows its secret once;
  the old one keeps working for up to seven days so callers can move across. The
  access is checked again first, so a credential the stack's tenant settings no
  longer allow (an Admin credential after tenant isolation was turned on, say)
  cannot be rotated and has to be replaced with one that fits.
- **Revoke.** Refused from the next request, even while one of its tokens is still
  valid; its app client is deleted and its secret scheduled for deletion ("Default
  (stack-created)" keeps both; see below).
- **Expiry.** A daily sweep revokes expired credentials and sends the Admin who
  created one an Inbox message two weeks before it expires.
- **Audit.** Every change is logged with the acting Admin, the credential's id,
  name and new access, never its secret; the credential also records who created
  and last changed it, and when it was last used (to the hour).

Under `TenantGrantSource=Api` an entitlement system grants a credential its tenant,
as it does a user's, with `setMcpCredentialTenant` (or `idp-cli mcp credentials
set-tenant`); until then the credential sees no documents.

Who can manage which credentials:

- **An Admin with configuration profiles of their own** (a scoped Admin) sees,
  grants and changes only credentials inside those profiles, can grant only a tenant
  whose profile pattern is inside them, and cannot create an Admin credential or one
  with no profiles. Under `TenantGrantSource=Api` every new credential starts with no
  profiles, so there only an unscoped Admin can create one.
- **`idp-cli` and the SDK** act with the AWS credentials they are run with, not a
  sign-in, so whoever can invoke them has the authority of an unscoped Admin.

⚠️ **Removing the credential registry revokes every credential.** Deleting the
stack, setting `EnableMCP` to `false`, or downgrading to a release without service
credentials removes each credential's app client, schedules its secret for deletion
and takes it off the gateway. They are not restored if the stack is later upgraded
or MCP is switched back on; create them again.

## Important Notes

### Pagination

The `get_results` tool returns paginated results:
- Default page size: 10 documents
- Recommended maximum page size: 100 documents. This is **advice, not a limit** — the
  schema states it in prose only, and `limit` is passed through to a list slice with no
  clamping, so a larger value is honoured. Treat 100 as the point beyond which a single
  response gets slow and large enough to be worth paginating instead
- Use `next_token` to retrieve subsequent pages
- `total_in_batch` shows the complete batch size
- Per-document data is accurate for the current page only

### Document Sections

For multi-section documents (e.g., lending packages):
- Section 1: Primary extraction results
- Sections 2+: Further sections of the same document, each with its own class
- Use `section_id` parameter to retrieve specific sections

### Confidence Structure

Confidence scores mirror the field structure:
- **Flat fields**: Confidence is the score stored for the field, passed through
  unchanged (normally a number from 0.0 to 1.0), or `null` when the stored entry
  carries no score. A field with no stored entry at all is left out of
  `confidence`, so look a field up with a default rather than assuming its key is
  present
- **Nested objects**: Confidence is nested with the same structure as fields
- **Array fields**: Confidence is a list with one entry per item, in item order.
  For a list of objects (e.g., `line_items`) each entry holds a score per column
  of that row; for a list of plain values each entry is that item's score
- **Null values**: A field whose value is null carries whatever was stored for it.
  The tool passes stored scores through unchanged, so such a field can show a low
  score, a high score, `null`, or no entry. A score is not a statement that the
  field is absent; read the value in `fields` for that
- **Stand-in scores**: A stored score is usually the confidence model's answer, but
  not always, and each stand-in records why in the field's `confidence_reason`.
  When the confidence response cannot be parsed, or returns a field in an
  unexpected shape, the affected fields are stored as 0.5, except that the list
  rows of a response that cannot be parsed are stored as `null`. A list row or a
  populated field that the confidence step did not score is also stored as
  `null`: the response returned fewer rows than were extracted, left the field
  out, or the time budget ran out before the row's batch was sent. In integrated confidence on the Simple path
  (1S-TopK), a field whose candidate carries no usable probability is stored as
  0.0. This tool does not return `confidence_reason`, so a stand-in looks the same
  as a scored value here; read `explainability_info` in the section's
  `result.json` to tell them apart
- **No confidence at all**: `confidence` is `null` when the section has no
  confidence data, and `{}` when the confidence step stored an empty assessment

The tool returns scores, not alerts. In Pipeline mode, a field's confidence alert
is raised for any numeric score below its threshold, whether or not the field has
a value, and a NaN or infinite score is read as 0.0 and raises one. A field with a
null or non-numeric score raises none. See
[Human review](human-review.md) for how alerts route a document to review.

Example:
```json
{
  "fields": {
    "vendor_info": {"tax_id": "<tax_id>"},
    "line_items": [{"description": "<item>", "amount": "<amount>"}]
  },
  "confidence": {
    "vendor_info": {"tax_id": 1.0},
    "line_items": [{"description": 0.95, "amount": 0.72}]
  }
}
```

## Implementation Details

### Architecture Components

1. **AgentCore Gateway Manager Lambda**
   - Creates and manages the AgentCore Gateway
   - Handles CloudFormation custom resource lifecycle
   - Configures JWT authorization using Cognito

2. **AgentCore MCP Handler Lambda**
   - Implements MCP protocol following AgentCore schema
   - Processes natural language queries via search_genaiidp tool
   - Translates queries to appropriate backend operations
   - Returns structured responses in natural language

3. **AgentCore Gateway**
   - AWS Bedrock AgentCore Gateway resource
   - Routes requests between external applications and MCP handler Lambda
   - Handles authentication and authorization

### Authentication Flow

1. **External Application** requests access token from Cognito
2. **Cognito User Pool** validates credentials and returns JWT token
3. **External Application** calls AgentCore Gateway with Bearer token
4. **AgentCore Gateway** validates JWT token against Cognito
5. **Caller interceptor** resolves the token to a role and a configuration scope
   (a signed-in user's groups and `allowedConfigVersions`, or the stack-created
   client-credentials client), refuses a tool that role may not call, and passes a
   signed caller block to the MCP handler
6. **MCP handler** verifies the block, applies the caller's configuration scope to
   every document it reads or writes, and returns the result

Which role may call which tool is in [RBAC: MCP Tools](./rbac.md#mcp-tools).

### Data Access

The Analytics Lambda has read-only access to:
- **Analytics Database**: Glue catalog with processed document metadata
- **Reporting Bucket**: S3 bucket containing analytics data and query results
- **Configuration Tables**: DynamoDB tables with system configuration
- **Tracking Tables**: DynamoDB tables with processing status

## Security

### Authentication & Authorization

The MCP Server uses AWS Cognito OAuth 2.0 for secure authentication:
- External applications obtain JWT tokens from the Cognito User Pool
- AgentCore Gateway validates JWT tokens on every request
- A token is resolved to the caller's role and configuration scope, and each tool
  admits the roles of the web UI operation it corresponds to; see
  [RBAC: MCP Tools](./rbac.md#mcp-tools)
- A caller limited to some Configuration Profiles sees and processes only documents
  in those profiles, and cannot process from an S3 location
- Token expiration and refresh mechanisms prevent unauthorized access

### IAM Role-Based Access Control

What a caller may do is decided per caller, by role and configuration scope (above);
the functions' IAM roles bound what the server as a whole can reach:

- The MCP handler reads the tracking and configuration tables, reads and writes the
  Input, Output, Reporting and MCP content buckets (processing copies documents into
  the Input bucket), and may delete only in the Output bucket (reprocessing replaces
  a document's earlier section results). It runs the analytics agent for `search`
  against the reporting database.
- The caller interceptor reads the user pool (`AdminGetUser`), the Users table, the
  credential registry and its signing key, and nothing else.
- The credential manager creates and deletes the MCP credentials' app clients and
  their secrets under `<stack>/mcp/credentials/`, and changes the gateway's allowed
  clients.

### S3 Bucket Access

Document processing through the MCP Server follows secure S3 access patterns:
- Input documents from S3 are processed through the standard IDP pipeline
- Base64-encoded documents are uploaded to a temporary MCP bucket with restricted access
- Temporary files are automatically cleaned up after processing
- All S3 operations use IAM role credentials (no long-lived access keys)
- Bucket policies restrict access to the IDP stack's execution roles

### Data Encryption

Data security is maintained throughout the MCP integration:
- **In Transit**: All communication between external applications and AgentCore Gateway uses HTTPS/TLS
- **At Rest**: DynamoDB tables and S3 buckets use AWS-managed encryption keys
- **JWT Tokens**: Signed with Cognito's private keys and validated using public keys
- **Client secrets**: every MCP client secret is in AWS Secrets Manager, encrypted
  with the stack's key, and none is a stack output (see
  [Where the secrets are](#where-the-secrets-are)). Secrets are not rotated
  automatically: rotate a service credential, including "Default (stack-created)",
  on the MCP Configuration page

## MCP Content Bucket

The stack creates a dedicated S3 bucket for MCP document uploads:

- **Bucket Name**: `MCPContentBucket` (available in CloudFormation stack outputs)
- **Purpose**: Upload documents for processing via the `process` tool
- **Access**: Accessible through the MCP Server tools with proper authentication
- **Usage**: Provide the S3 URI (e.g., `s3://mcp-content-bucket/documents/`) to the `process` tool. Processing from an S3 location is available only to a caller with no configuration scope; a scoped caller sends the document as base64 `content` instead
- **Cleanup**: Temporary files are automatically managed by the IDP pipeline

**Example Workflow:**
1. Upload documents to MCPContentBucket via S3 console or AWS CLI
2. Use the `process` tool with the S3 URI pointing to MCPContentBucket
3. Documents are processed through the standard IDP pipeline
4. Results are available in the output bucket

## Cognito User Pool Utilization

### User Pool Configuration

The IDP solution creates a Cognito User Pool with:
- **Domain**: Auto-generated unique domain (e.g., `stack-name-timestamp.auth.region.amazoncognito.com`)
- **Password Policy**: Configurable security requirements
- **User Management**: Admin-managed user creation
- **OAuth Flows**: Authorization code flow for external applications; client credentials flow for machine-to-machine (M2M) integrations (no user login required)

### Two-Client Architecture

When MCP is enabled, the stack creates **two separate Cognito User Pool Clients** with different OAuth flows. Cognito does not allow mixing `client_credentials` and `authorization_code` flows on the same client, so each integration type requires its own dedicated client.

| CloudFormation Resource | Client Name | OAuth Flow | Purpose |
|------------------------|-------------|-----------|---------|
| `ExternalAppClient` | `external-app-client` | `authorization_code` | External apps, QuickSight integration |
| `MCPConnectorClient` | `mcp-connector-client` | `client_credentials` | MCP Connector machine-to-machine (M2M) auth — no user login required |

### External App Client

The `ExternalAppClient` is used for external applications requiring user-based login (e.g., Amazon QuickSight).

**Client Configuration:**
- **Client Name**: `external-app-client`
- **Client Secret**: Generated automatically
- **Auth Flows**: USER_PASSWORD_AUTH, ADMIN_USER_PASSWORD_AUTH, REFRESH_TOKEN_AUTH
- **OAuth Flows**: Authorization code flow
- **OAuth Scopes**: openid, email, profile
- **Callback URLs**:
  - CloudFront distribution URL
  - Quick Suite OAuth callback
  - Cognito User Pool domain
- **Stack Outputs**: `MCPClientId`, `MCPClientSecretArn` (the Secrets Manager ARN
  holding the client ID and secret)
- **Who the caller is**: the person who signs in, with their own groups and
  configuration profiles

### MCP Connector Client

The `MCPConnectorClient` is used by AI coding assistants (Cline, Amazon Q, etc.) that connect to the IDP MCP server via machine-to-machine (M2M) OAuth — authentication happens automatically in the background without any user login prompt.

**Client Configuration:**
- **Client Name**: `mcp-connector-client`
- **Client Secret**: Generated automatically; stored in Secrets Manager as
  `<stack>/mcp/credentials/<MCPConnectorClientId>`
- **OAuth Flows**: `client_credentials` (machine-to-machine (M2M) — no user login or browser redirect required)
- **OAuth Scopes**: `idp-mcp-connector/access`
- **Stack Outputs**: `MCPConnectorClientId`, `MCPConnectorClientSecretArn`
- **Who the caller is**: the credential "Default (stack-created)" on the MCP
  Configuration page — an Author with no configuration scope and write tools on, the
  access every caller of this client has always had. Under
  `TenantIsolationMode=Strict` or an external `TenantGrantSource` it is created
  **disabled**, because unscoped access is what those modes forbid; an Admin can give
  it one tenant's profiles and enable it there. Whether it starts enabled is decided once, when the stack first registers it, and
  a later stack update never changes it. Switching `TenantIsolationMode` to `Strict`
  afterwards leaves it enabled but, having no profiles, it then sees no documents;
  switching back to `Off` leaves a disabled one disabled until an Admin enables it.
- **Changing it**: an Admin can narrow, disable or enable it like any credential.
  Two operations differ for this one. **Revoking** it refuses it and takes it off the
  gateway, but keeps its app client and secret, which belong to the stack.
  **Rotating** it issues an ordinary credential with the same access, shows that
  secret once, and ends this one within the overlap you choose (seven days at most);
  after that, `MCPConnectorClientSecretArn` names the secret of a client that is
  refused, so move every integration to the new credential first.

> **Note**: Prefer a [service credential](#service-credentials) of its own for each
> integration over sharing this one: each then carries only the access it needs,
> and can be rotated or revoked without affecting the others.

### Token Management

Each client type uses a different OAuth flow for token acquisition:

**MCP Connector — Client Credentials Flow** (machine-to-machine (M2M): the connector authenticates using its client ID and secret directly, with no user login or browser redirect):
```bash
curl -X POST <MCPTokenURL> \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&scope=idp-mcp-connector/access" \
  -u "<client ID>:<client secret>"
```

**External App / QuickSight — Authorization Code Flow** (user-interactive):
```bash
# Step 1: Get authorization code
<MCPAuthorizationURL>?\
  response_type=code&\
  client_id=<MCPClientId>&\
  redirect_uri=CALLBACK_URL&\
  scope=openid+email+profile

# Step 2: Exchange code for tokens
curl -X POST <MCPTokenURL> \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&client_id=<MCPClientId>&client_secret=<user-client secret>&code=AUTH_CODE&redirect_uri=CALLBACK_URL"
```

## Where the secrets are

No MCP client secret is a stack output. Each is in AWS Secrets Manager, encrypted with
the stack's key:

| Client | Secret | Output naming it |
|---|---|---|
| External App Client (user authentication) | `<stack>/mcp/user-client` | `MCPClientSecretArn` |
| Stack-created client-credentials client | `<stack>/mcp/credentials/<MCPConnectorClientId>` | `MCPConnectorClientSecretArn` |
| Each service credential | `<stack>/mcp/credentials/<clientId>` | shown on the MCP Configuration page |

A service credential's secret is shown once when it is created or rotated, and an
operator with access to the secret can read it later with
`aws secretsmanager get-secret-value`. None is rotated automatically. Rotate a service
credential on the page. The two stack-created clients keep their secrets for the life
of the stack; to stop using the connector client's, rotate "Default
(stack-created)" as described above. The user-authentication client's secret cannot
act as anyone on its own: each call still needs a person to sign in.

## Output Parameters

When MCP integration is enabled, the CloudFormation stack provides the following outputs required for external application integration:

### MCP Content Bucket

- **`MCPContentBucket`**: S3 bucket for uploading documents to process via MCP tools
  - Use this bucket to upload documents before calling the `process` tool
  - Provide the S3 URI from this bucket to the `process` tool's `location` parameter

### MCP Server Endpoint

- **`MCPServerEndpoint`**: The HTTPS endpoint for the MCP Server
  - The AgentCore Gateway URL for MCP protocol communication
  - Required for external applications to connect to the gateway via MCP protocol

### Authentication Outputs

The stack provides separate output parameters for each Cognito client:

**MCP Connector (client_credentials — use for AI coding assistants):**

- **`MCPConnectorClientId`**: Cognito client ID for the MCP Connector (machine-to-machine (M2M) `client_credentials` flow — no user login required)
  - Use this when configuring the IDP MCP Connector package
  - Required for `client_credentials` token requests

- **`MCPConnectorClientSecretArn`**: Secrets Manager ARN holding that client's ID and
  secret. Read it with
  `aws secretsmanager get-secret-value --secret-id <MCPConnectorClientSecretArn>`,
  or create a [service credential](#service-credentials) of the integration's own

**External App / QuickSight (authorization_code — use for user-facing applications):**

- **`MCPClientId`**: Cognito client ID for the External App Client (QuickSight / authorization code flow)
  - Use this for Amazon QuickSight and other external applications requiring user login
  - Used in OAuth authorization code flows

- **`MCPClientSecretArn`**: Secrets Manager ARN holding the External App Client's ID
  and secret. Read it with
  `aws secretsmanager get-secret-value --secret-id <MCPClientSecretArn>`

**Shared authentication parameters:**

- **`MCPUserPool`**: Cognito User Pool ID
  - Required for token validation and user management
  - Used by both clients

- **`MCPTokenURL`**: OAuth token endpoint URL
  - Format: `https://domain-name.auth.region.amazoncognito.com/oauth2/token`
  - Used for obtaining access tokens via both OAuth flows

- **`MCPAuthorizationURL`**: OAuth authorization endpoint URL
  - Format: `https://domain-name.auth.region.amazoncognito.com/oauth2/authorize`
  - Used for initiating OAuth authorization code flows (External App / QuickSight only)

## Usage Examples

### External Application Setup

This example signs in as a service with the client-credentials flow, using a [service credential](#service-credentials) created on the MCP Configuration page (or the stack-created one, whose secret is at `MCPConnectorClientSecretArn`). The application authenticates directly with the client ID and secret, with no user login or browser redirect.

```python
import requests
import json

# Configuration from CloudFormation outputs
GATEWAY_URL = "<MCPServerEndpoint>"       # From stack outputs
CLIENT_ID = "<client ID>"                # From the MCP Configuration page
CLIENT_SECRET = "<client secret>"        # Shown once on creation; also in Secrets Manager
TOKEN_URL = "<MCPTokenURL>"              # From stack outputs
MCP_BUCKET = "<MCPContentBucket>"        # From stack outputs

# Get access token via client_credentials flow
token_response = requests.post(
    TOKEN_URL,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    data={
        "grant_type": "client_credentials",
        "scope": "idp-mcp-connector/access"
    },
    auth=(CLIENT_ID, CLIENT_SECRET)
)
access_token = token_response.json()["access_token"]

# Process documents from MCP bucket
process_request = {
    "method": "tools/call",
    "params": {
        "name": "process",
        "arguments": {
            "location": f"s3://{MCP_BUCKET}/documents/",
            "prefix": "batch-001"
        }
    }
}

response = requests.post(
    GATEWAY_URL,
    headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    },
    json=process_request
)

result = response.json()
print(f"Processing result: {result}")
```

### Amazon Quick Suite Integration

Amazon Quick connects to a remote MCP server in one of two ways, and both work here.
Choose by **who the integration should act as**.

**User authentication (recommended where people use it).** Each Quick user signs in
as themselves and gets their own role and configuration profiles, exactly as in the
IDP console.

- **MCP Server**: `MCPServerEndpoint`
- **Client ID**: `MCPClientId` _(External App Client — authorization code flow)_
- **Client Secret**: from `MCPClientSecretArn`
  (`aws secretsmanager get-secret-value --secret-id <MCPClientSecretArn>`)
- **Token URL**: `MCPTokenURL`
- **Authorization URL**: `MCPAuthorizationURL`

Holding this client's secret lets someone run the sign-in flow, not act as anyone:
every request still carries the signed-in user's own identity.

**Service-to-service OAuth (for a shared workflow).** The integration signs in as a
service credential with its own role and profiles. Create one on the MCP
Configuration page, which shows its client ID, secret and token URL once, in a
ready-made Quick block.

- **MCP Server**: `MCPServerEndpoint`
- **Client ID** and **Client Secret**: from the credential
- **Token URL**: `MCPTokenURL`

> **Do not** mix them: the External App Client allows only the authorization code
> flow, and a service credential only the client-credentials flow.

