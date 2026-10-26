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
- Sections 2+: Additional document types within the same file
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
5. **Analytics Lambda** processes the request and returns results

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
- Tokens include scopes (openid, email, profile) for fine-grained access control
- Token expiration and refresh mechanisms prevent unauthorized access

### IAM Role-Based Access Control

The AgentCore Analytics Lambda operates with least-privilege IAM permissions:
- Read-only access to DynamoDB tracking and configuration tables
- Read-only access to S3 analytics and reporting buckets
- No write permissions to input or output buckets
- Scoped permissions prevent access to resources outside the IDP stack
- Service role restricts Lambda execution to authorized operations only

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
- **Sensitive Data**: Client secrets are stored securely in AWS Secrets Manager and rotated regularly

## MCP Content Bucket

The stack creates a dedicated S3 bucket for MCP document uploads:

- **Bucket Name**: `MCPContentBucket` (available in CloudFormation stack outputs)
- **Purpose**: Upload documents for processing via the `process` tool
- **Access**: Accessible through the MCP Server tools with proper authentication
- **Usage**: Provide the S3 URI (e.g., `s3://mcp-content-bucket/documents/`) to the `process` tool
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
- **Stack Outputs**: `MCPClientId`, `MCPClientSecret`

### MCP Connector Client

The `MCPConnectorClient` is used by AI coding assistants (Cline, Amazon Q, etc.) that connect to the IDP MCP server via machine-to-machine (M2M) OAuth — authentication happens automatically in the background without any user login prompt.

**Client Configuration:**
- **Client Name**: `mcp-connector-client`
- **Client Secret**: Generated automatically
- **OAuth Flows**: `client_credentials` (machine-to-machine (M2M) — no user login or browser redirect required)
- **OAuth Scopes**: `idp-mcp-connector/access`
- **Stack Outputs**: `MCPConnectorClientId`, `MCPConnectorClientSecret`

> **Note**: Use `MCPConnectorClientId` / `MCPConnectorClientSecret` for MCP Connector configuration. These credentials use the machine-to-machine (M2M) `client_credentials` OAuth flow, meaning the connector authenticates directly using its client ID and secret — no user login or browser is involved. The `MCPClientId` / `MCPClientSecret` outputs are reserved for QuickSight and other external apps that use the `authorization_code` flow (user-interactive login).

### Token Management

Each client type uses a different OAuth flow for token acquisition:

**MCP Connector — Client Credentials Flow** (machine-to-machine (M2M): the connector authenticates using its client ID and secret directly, with no user login or browser redirect):
```bash
curl -X POST <MCPTokenURL> \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&scope=idp-mcp-connector/access" \
  -u "<MCPConnectorClientId>:<MCPConnectorClientSecret>"
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
  -d "grant_type=authorization_code&client_id=<MCPClientId>&client_secret=<MCPClientSecret>&code=AUTH_CODE&redirect_uri=CALLBACK_URL"
```

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

- **`MCPConnectorClientSecret`**: Cognito client secret for the MCP Connector (machine-to-machine (M2M) `client_credentials` flow)
  - Use this when configuring the IDP MCP Connector package
  - Should be securely stored (e.g., in environment variables or a secrets manager)

**External App / QuickSight (authorization_code — use for user-facing applications):**

- **`MCPClientId`**: Cognito client ID for the External App Client (QuickSight / authorization code flow)
  - Use this for Amazon QuickSight and other external applications requiring user login
  - Used in OAuth authorization code flows

- **`MCPClientSecret`**: Cognito client secret for the External App Client (QuickSight / authorization code flow)
  - Use this for Amazon QuickSight and other external applications requiring user login
  - Should be securely stored and rotated regularly

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

This example uses the MCP Connector client credentials (`MCPConnectorClientId` / `MCPConnectorClientSecret`) for machine-to-machine (M2M) authentication — the application authenticates directly using its client ID and secret, with no user login or browser redirect involved.

```python
import requests
import json

# Configuration from CloudFormation outputs
GATEWAY_URL = "<MCPServerEndpoint>"       # From stack outputs
CLIENT_ID = "<MCPConnectorClientId>"      # From stack outputs (M2M client_credentials client)
CLIENT_SECRET = "<MCPConnectorClientSecret>"  # From stack outputs (M2M client_credentials client)
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

For Amazon QuickSight integration, configure the MCP connection using the **External App Client** outputs (authorization code flow). These are separate from the MCP Connector credentials.

- **MCP Server**: Use `MCPServerEndpoint` output value
- **Client ID**: Use `MCPClientId` output value _(External App Client — authorization code flow)_
- **Client Secret**: Use `MCPClientSecret` output value _(External App Client — authorization code flow)_
- **Token URL**: Use `MCPTokenURL` output value
- **Authorization URL**: Use `MCPAuthorizationURL` output value
- **Content Bucket**: Use `MCPContentBucket` output value for document uploads

> **Do not** use `MCPConnectorClientId` / `MCPConnectorClientSecret` for QuickSight. Those are for the MCP Connector's machine-to-machine (M2M) `client_credentials` flow (no user login) and will not work with the `authorization_code` flow required by QuickSight, which expects a user login redirect.
