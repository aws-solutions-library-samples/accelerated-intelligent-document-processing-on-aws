---
title: "GovCloud Batch Jobs REST API"
---

# GovCloud Batch Jobs REST API

This document covers the Batch Jobs REST API available in GovCloud deployments that include the [Jobs REST API](./govcloud-deployment.md#option-b-no-ui---headless--jobs-rest-api-all-lambdas-in-vpc) deployment option (`EnableJobsApi=true`).

## Overview

The REST API provides programmatic access to document processing via `/jobs` endpoints on a Private API Gateway accessible only from within your VPC. It requires:
- OAuth2 bearer token authentication via Cognito client credentials

## Authentication

Generate a bearer token using the provided script:

```bash
# Print token to stdout
./scripts/get_api_token.sh <STACK_NAME>

# Copy to clipboard (macOS)
./scripts/get_api_token.sh <STACK_NAME> | pbcopy
```

The token is a Cognito client credentials grant with `idp-api/jobs.read` and `idp-api/jobs.write` scopes.

## Authorization model

The API uses **per-client job ownership**: each job is visible only to the API client (Cognito app client) that created it.

- On `POST /jobs`, the caller's Cognito principal (`sub` / `client_id` claim from the bearer token) is persisted on the job record as `CreatedBy`.
- On `GET /jobs/{job_id}`, the caller's principal is compared to the job's `CreatedBy`. Mismatches return **HTTP 404** (not 403) to avoid leaking the existence of jobs owned by other clients.

**Implications:**

- **Single-client deployments** — the default from the bundled CloudFormation template, which creates exactly one `AWS::Cognito::UserPoolClient` (`ApiAppClient`) — see no behavioral change from this model. The one client is always the creator and always the reader.
- **Multi-client deployments** — if you create additional app clients against `ApiUserPool` (for credential rotation, per-service credentials, per-team isolation, etc.), each client sees only its own jobs. Cross-client job visibility is not supported through this API.
- **Credential rotation** — if you rotate an app client's secret (same client, new secret), the client's `sub` stays constant so previously-created jobs remain visible. If you replace the client entirely (delete + recreate), the new client will not be able to read jobs created by the old one.
- **Jobs predating this model** — job records without a `CreatedBy` field (written by builds from before this feature existed) remain readable by any authenticated caller. This is intentional so in-place upgrades don't orphan in-flight jobs.
- **Operator-level access** — if you need cross-client visibility (audit, debugging, bulk export), read from the S3 `OutputBucket` directly with IAM credentials. The Jobs API is not the right surface for this.

## API Reference

The `API_GATEWAY_ENDPOINT` is the `ApiGatewayEndpoint` value from your CloudFormation stack outputs, in the format `https://{restapi-id}.execute-api.{region}.amazonaws.com/{stage}`.

> **Note:** The `{stage}` defaults to `beta` unless overridden via the `ApiStageName` parameter.

### POST /jobs — Create a Job

Creates a new processing job and returns a presigned upload URL.

```bash
TOKEN=$(./scripts/get_api_token.sh <STACK_NAME>)

curl -X POST ${API_GATEWAY_ENDPOINT}/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"fileName": "documents.zip"}'
```

**Request fields:**

| Field | Required | Description |
|---|---|---|
| `fileName` | yes | Name of the ZIP to upload (must end in `.zip`). |
| `configurationVersion` | no | Configuration version to use for processing this job (e.g. `"v2"`, `"lending-v2"`). Validated as ≤128 characters matching `^[a-zA-Z0-9._-]+$`; an invalid value returns `422`. When omitted, the job uses the stack's active/default configuration (unchanged behavior). The version must already exist in the configuration table — an unknown version falls back to pipeline mode downstream. |

Example pinning a specific configuration version:

```bash
curl -X POST ${API_GATEWAY_ENDPOINT}/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"fileName": "documents.zip", "configurationVersion": "lending-v2"}'
```

**Response:**

```json
{
  "jobId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "upload": {
    "uploadUrl": "https://input-bucket.s3.amazonaws.com",
    "expiresInSeconds": 3600,
    "requiredHeaders": {
      "key": "jobs/a1b2c3d4-e5f6-7890-abcd-ef1234567890/archive.zip",
      "Content-Type": "application/zip",
      "x-amz-credential": "...",
      "x-amz-date": "...",
      "x-amz-security-token": "...",
      "x-amz-algorithm": "...",
      "policy": "...",
      "x-amz-signature": "..."
    }
  }
}
```

### Upload the ZIP File

Use the presigned URL and `requiredHeaders` from the POST response:

```bash
curl -X POST "<uploadUrl from response>" \
  -F "Content-Type=<Content-Type from requiredHeaders>" \
  -F "key=<key from requiredHeaders>" \
  -F "x-amz-algorithm=<x-amz-algorithm from requiredHeaders>" \
  -F "x-amz-credential=<x-amz-credential from requiredHeaders>" \
  -F "x-amz-date=<x-amz-date from requiredHeaders>" \
  -F "x-amz-security-token=<x-amz-security-token from requiredHeaders>" \
  -F "policy=<policy from requiredHeaders>" \
  -F "x-amz-signature=<x-amz-signature from requiredHeaders>" \
  -F "file=@documents.zip"
```

### GET /jobs/{job_id} — Check Job Status

```bash
curl ${API_GATEWAY_ENDPOINT}/jobs/{job_id} \
  -H "Authorization: Bearer $TOKEN"
```

**Response (in progress):**

```json
{
  "jobId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "IN_PROGRESS",
  "configurationVersion": "lending-v2",
  "timestamps": {
    "createdAt": "2026-01-23T10:00:00Z",
    "updatedAt": "2026-01-23T10:05:00Z"
  },
  "files": {
    "document_a.pdf": "COMPLETED",
    "document_b.pdf": "IN_PROGRESS"
  }
}
```

> `configurationVersion` is present only when the job was created with one;
> it is `null` (or omitted) for jobs that use the stack default.

**Response (succeeded):**

```json
{
  "jobId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "SUCCEEDED",
  "timestamps": {
    "createdAt": "2026-01-23T10:00:00Z",
    "updatedAt": "2026-01-23T10:10:00Z"
  },
  "files": {
    "document_a.pdf": "COMPLETED",
    "document_b.pdf": "COMPLETED"
  },
  "result": {
    "downloadUrl": "https://output-bucket.s3.amazonaws.com/jobs/.../results.zip?...",
    "expiresInSeconds": 3600
  }
}
```

**Response (partially succeeded):**

```json
{
  "jobId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "PARTIALLY_SUCCEEDED",
  "timestamps": {
    "createdAt": "2026-01-23T10:00:00Z",
    "updatedAt": "2026-01-23T10:10:00Z"
  },
  "files": {
    "document_a.pdf": "COMPLETED",
    "document_b.pdf": "FAILED"
  },
  "result": {
    "downloadUrl": "https://output-bucket.s3.amazonaws.com/jobs/.../results.zip?...",
    "expiresInSeconds": 3600
  }
}
```

### Job Status Values

| Status | Description |
|---|---|
| `PENDING_UPLOAD` | Job created, awaiting ZIP upload |
| `IN_PROGRESS` | Files being processed |
| `SUCCEEDED` | All files completed |
| `PARTIALLY_SUCCEEDED` | Some files completed, some failed/aborted. The results.zip will not include output data from documents that did not complete processing |
| `ABORTED` | All files aborted |
| `FAILED` | All files failed or in failed/aborted states |


## Private API Access via Bastion Tunnel

If you deployed with the [Bastion option](./govcloud-deployment.md#option-c-option-b--bastion-host-development), you can access the private API Gateway from your local machine.

### Prerequisites

- AWS CLI configured with credentials for the target account
- [AWS Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) — required for the SSM-based SSH tunnel
- SSH client

### Step 1: Start the tunnel

```bash
./scripts/bastion.sh <STACK_NAME>
```

### Step 2: Generate a bearer token

In a separate terminal:

```bash
# Print token to stdout
./scripts/get_api_token.sh <STACK_NAME>

# Copy to clipboard (macOS)
./scripts/get_api_token.sh <STACK_NAME> | pbcopy
```

### Step 3: Invoke the API

```bash
# Create a job (get endpoint URL from stack outputs: ApiGatewayEndpoint)
curl -X POST {api-endpoint}/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"fileName": "my-documents.zip"}'
```

## Related Documentation

- [GovCloud Deployment Guide](./govcloud-deployment.md) — prerequisites, deployment options, and deploy commands
- [GovCloud Architecture](./govcloud-architecture.md) — services removed vs. retained, limitations
- [GovCloud Operations](./govcloud-operations.md) — monitoring, troubleshooting, and best practices
