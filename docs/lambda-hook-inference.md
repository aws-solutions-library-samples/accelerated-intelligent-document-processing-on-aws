---
title: "Lambda Hook Inference (Custom LLM Integration)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Lambda Hook Inference (Custom LLM Integration)

The GenAI IDP Accelerator supports integrating custom LLM inference endpoints through a **Lambda Hook** mechanism. This allows you to use any LLM — including models hosted on Amazon SageMaker, Amazon ECS, Amazon EC2, or external inference APIs — for any inference step in the document processing pipeline.

## Overview

Instead of calling the Amazon Bedrock Converse API, the accelerator invokes your custom Lambda function with the same Converse API-compatible payload. Your Lambda function processes the request using whatever inference backend you choose and returns a Converse API-compatible response.

```mermaid
flowchart LR
    SVC[IDP Service<br>Classification/Extraction/etc] --> BC[BedrockClient]
    BC --> CHECK{model == LambdaHook?}
    CHECK -->|No| API[Bedrock<br>Converse API]
    CHECK -->|Yes| S3[Upload images<br>to S3]
    S3 --> LAMBDA[Your Lambda<br>GENAIIDP-*]
    LAMBDA --> CUSTOM[Your LLM<br>SageMaker/External API/etc]
    CUSTOM --> RESP[Converse-compatible<br>Response]
    API --> RESP2[Bedrock Response]
```

## Supported Steps

The LambdaHook option is available for the following pipeline steps in Pattern-1 and Pattern-2:

| Step | Config Field | Description |
|------|-------------|-------------|
| **OCR** (Bedrock backend) | `ocr.model_id` | LLM-based OCR when `backend=bedrock` |
| **Classification** | `classification.model` | Document type classification |
| **Extraction** | `extraction.model` | Structured data extraction |
| **Assessment** | `extraction.confidence.model` | Confidence scoring |
| **Summarization** | `summarization.model` | Document summarization |

## Configuration

### Web UI

1. Navigate to the **Configuration** page
2. Select the step you want to customize (e.g., Extraction)
3. In the **Model** dropdown, select **"LambdaHook"** (first option)
4. Enter your Lambda function ARN in the **Model Lambda Hook ARN** field
5. Save the configuration

### Config YAML

```yaml
extraction:
  model: "LambdaHook"
  model_lambda_hook_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-my-custom-extractor"
  temperature: 0.0
  system_prompt: "You are a document extraction expert..."
  task_prompt: "Extract the following attributes..."
```

### Lambda Function Naming Convention

Your Lambda function name **must start with `GENAIIDP-`**. This naming convention enables secure, scoped IAM permissions — the IDP stack's Lambda functions are granted `lambda:InvokeFunction` permission only for functions matching `GENAIIDP-*`.

**Valid examples:**
- `GENAIIDP-sagemaker-inference`
- `GENAIIDP-api-proxy`
- `GENAIIDP-custom-extraction`

**Invalid examples:**
- `my-custom-function` (missing `GENAIIDP-` prefix)
- `genaiidp-lowercase` (case-sensitive)

## Request Payload

Your Lambda function receives a **Converse API-compatible** payload:

```json
{
  "modelId": "LambdaHook",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "text": "Extract the following attributes from this Bank Statement document:\n\n..."
        },
        {
          "image": {
            "format": "jpeg",
            "source": {
              "s3Location": {
                "uri": "s3://working-bucket/temp/lambdahook/abc123.jpeg",
                "bucketOwner": "123456789012"
              }
            }
          }
        }
      ]
    }
  ],
  "system": [
    {
      "text": "You are a document extraction expert. Respond only with JSON..."
    }
  ],
  "inferenceConfig": {
    "temperature": 0.0,
    "maxTokens": 10000,
    "topK": 5
  },
  "context": "Extraction"
}
```

### Key Differences from Bedrock Converse API

1. **Images use S3 references** — To avoid the Lambda 6MB payload limit, inline image bytes are automatically uploaded to S3 and replaced with `s3Location` references. Your Lambda needs `s3:GetObject` permission on the working bucket.

2. **`<<CACHEPOINT>>` tags are stripped** — These Bedrock-specific tags are removed from text content before sending to your Lambda.

3. **`context` field is added** — Indicates which pipeline step is calling (OCR, Classification, Extraction, Assessment, Summarization).

## Expected Response

Your Lambda function must return a **Converse API-compatible** response:

```json
{
  "output": {
    "message": {
      "role": "assistant",
      "content": [
        {
          "text": "{\"account_number\": \"12345\", \"balance\": \"$1,250.00\"}"
        }
      ]
    }
  },
  "usage": {
    "inputTokens": 1500,
    "outputTokens": 200,
    "totalTokens": 1700
  }
}
```

### Response Fields

| Field | Required | Description |
|-------|----------|-------------|
| `output.message.role` | Yes | Must be `"assistant"` |
| `output.message.content[0].text` | Yes | The model's response text |
| `usage.inputTokens` | No | Input token count (for cost tracking) |
| `usage.outputTokens` | No | Output token count (for cost tracking) |
| `usage.totalTokens` | No | Total token count |

If `usage` is not provided, zeros will be recorded for metering. `usage` values must be
JSON numbers: metering sums and prices them, so non-numeric members (strings, booleans,
lists, objects) are ignored rather than recorded.

### Optional: structured OCR output (confidence + geometry)

For the **OCR** step, a hook may additionally return a top-level `textractBlocks`
object in **Amazon Textract response format** (a `DocumentMetadata` object plus a
`Blocks` list of `PAGE`/`LINE`/`WORD` blocks with `Confidence` and normalized
`Geometry.BoundingBox`). When present and non-empty, the OCR service persists it as
the page's `rawText.json` and `textConfidence.json` instead of the default "no
confidence data" placeholder, and folds it into the consolidated per-page
`pageData.json` (so the confidence/geometry surfaces in the Web UI page Visual
Editor). This carries real per-line/word OCR confidence into
**Assessment** (the `{OCR_TEXT_CONFIDENCE}` prompt placeholder used for extraction
confidence) and makes bounding-box **geometry** available for UI highlighting — the
same data the native Textract backend produces. Text-only hooks are unaffected
(they still get a text-only `pageData.json`). See the
[consolidated OCR page data](../lib/idp_common_pkg/idp_common/ocr/README.md#consolidated-ocr-page-data-pagedatajson)
docs for the `pageData.json` schema.

A hook may also return `usage.pages` (in addition to / instead of token counts) to
enable per-page cost metering. See **GENAIIDP-mistral-ocr-hook** for a worked example.

## Sample Lambda Functions

Ready-to-deploy sample Lambda hook functions are provided in [`samples/lambda-hook-inference/`](../samples/lambda-hook-inference/):

| Sample | Description |
|--------|-------------|
| **GENAIIDP-bedrock-proxy** | Forwards to Bedrock Converse API — use as a starting template for custom hooks with pre/post processing |
| **GENAIIDP-sagemaker-hook** | Calls a SageMaker real-time inference endpoint — shows format conversion between Converse API and SageMaker |
| **GENAIIDP-chandra-ocr-hook** | Calls the [Datalab Chandra OCR 2](https://github.com/datalab-to/chandra) hosted API for high-quality OCR — converts page images to structured Markdown, JSON, or HTML |
| **GENAIIDP-mistral-ocr-hook** | Calls the hosted [Mistral OCR](https://mistral.ai/news/ocr-4/) API for high-quality OCR — returns Markdown **plus per-word confidence and bounding-box geometry** (Textract format) for explainability, with per-page cost metering. Fully serverless |

Each sample includes:
- Well-commented Python code with clearly marked customization points
- A SAM template (`template.yaml`) for one-click deployment with proper IAM permissions
- S3 image download handling (since images arrive as S3 references)

```bash
# Deploy the samples
cd samples/lambda-hook-inference
sam build && sam deploy --guided --stack-name GENAIIDP-lambda-hooks
```

See the [samples README](../samples/lambda-hook-inference/README.md) for full deployment instructions.

## Chandra OCR Integration

[Chandra OCR 2](https://github.com/datalab-to/chandra) by [Datalab](https://www.datalab.to) is a state-of-the-art VLM-based OCR model that converts images into structured Markdown, JSON, or HTML. It supports 90+ languages, math, tables, forms (including checkboxes), handwriting, and complex layouts.

The **GENAIIDP-chandra-ocr-hook** sample integrates the Datalab hosted API with the LambdaHook feature for OCR. The Datalab API uses an asynchronous pattern:

1. **Submit**: `POST /api/v1/convert` with the page image (multipart form) → returns a `request_check_url`
2. **Poll**: `GET request_check_url` until `status: "complete"` → returns OCR result

### Configuration

```yaml
ocr:
  backend: bedrock
  model_id: "LambdaHook"
  model_lambda_hook_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-chandra-ocr-hook"
```

### Deployment

```bash
cd samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook
sam build
sam deploy --guided \
  --stack-name GENAIIDP-chandra-ocr-hook \
  --parameter-overrides \
    IDPWorkingBucket=<your-idp-working-bucket-name> \
    CustomerManagedEncryptionKeyArn=<your-kms-key-arn> \
    ChandraApiKey=<your-datalab-api-key>
```

**Getting an API key**: Sign up at [datalab.to](https://www.datalab.to) to get your API key.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CHANDRA_API_KEY` | (required) | Datalab API key |
| `CHANDRA_API_URL` | `https://www.datalab.to` | Datalab API base URL |
| `OUTPUT_FORMAT` | `markdown` | Output format: `markdown`, `json`, or `html` |
| `CONVERSION_MODE` | `accurate` | Quality mode: `fast`, `balanced`, or `accurate` |
| `POLL_INTERVAL` | `3` | Seconds between polling attempts |
| `MAX_POLL_ATTEMPTS` | `60` | Maximum polling attempts before timeout |

### Local Testing

Test locally before deploying:

```bash
cd samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook
pip install pdf2image Pillow
export CHANDRA_API_KEY="your-api-key"
python test_local.py ../../insurance_package.pdf --pages 1,2
```

## Mistral OCR Integration

[Mistral OCR 4](https://mistral.ai/news/ocr-4/) is a document-understanding model
that returns markdown-structured text together with paragraph-level bounding boxes,
typed-block classification, and per-page / per-word confidence scores across 170
languages. The **GENAIIDP-mistral-ocr-hook** sample calls the hosted Mistral OCR
API (`POST https://api.mistral.ai/v1/ocr`, Bearer-key auth) — fully serverless, no
SageMaker endpoint or GPU required.

This hook is the reference implementation of the [structured OCR output](#optional-structured-ocr-output-confidence--geometry)
pattern: it requests `include_blocks=true` and `confidence_scores_granularity=word`,
then translates Mistral's response into Amazon Textract block format (normalizing
the pixel bounding boxes against page dimensions to Textract's 0–1 scale) and returns
it under `textractBlocks`. The result: OCR confidence flows into Assessment and bounding-box
geometry is available to the UI, just like the native Textract backend.

### Configuration

```yaml
ocr:
  backend: bedrock
  model_id: "LambdaHook"
  model_lambda_hook_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-mistral-ocr-hook"
```

**Getting an API key**: Sign up at [console.mistral.ai](https://console.mistral.ai) to get your API key, provided as `MistralApiKey` at deploy time.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MISTRAL_API_KEY` | (required) | Mistral API key (Bearer token) |
| `MISTRAL_API_URL` | `https://api.mistral.ai/v1/ocr` | Mistral OCR endpoint |
| `MISTRAL_OCR_MODEL` | `mistral-ocr-latest` | OCR model id |
| `INCLUDE_BLOCKS` | `true` | Request paragraph bounding boxes (geometry) |
| `CONFIDENCE_GRANULARITY` | `word` | Confidence granularity: `word` or `page` |
| `REQUEST_TIMEOUT` | `120` | Per-request timeout (seconds) |

### Cost metering

The hook returns `usage.pages` (from Mistral's `usage_info.pages_processed`). Add a
pricing entry keyed on the function name (Mistral OCR list price is $4 / 1,000 pages):

```yaml
  - name: GENAIIDP-mistral-ocr-hook
    units:
      - name: pages
        price: "0.004"
```

### Local Testing

```bash
cd samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook
pip install pdf2image Pillow
export MISTRAL_API_KEY="your-api-key"
python test_local.py ../../insurance_package.pdf --pages 1,2   # markdown + confidence + geometry
python test_translation.py                                     # offline unit tests (no API/AWS)
```

## Example Implementations

The following examples show how to build Lambda hooks for various inference providers. For deployable versions, see the samples above.

### SageMaker Endpoint

```python
import json
import boto3

sagemaker_runtime = boto3.client('sagemaker-runtime')
s3_client = boto3.client('s3')

def lambda_handler(event, context):
    """Proxy inference to a SageMaker endpoint."""
    
    # Extract prompts from Converse-compatible payload
    system_text = event['system'][0]['text']
    user_content = event['messages'][0]['content']
    
    # Build prompt for your model
    user_text = ""
    images = []
    for item in user_content:
        if 'text' in item:
            user_text += item['text']
        elif 'image' in item:
            # Download image from S3
            s3_uri = item['image']['source']['s3Location']['uri']
            bucket, key = s3_uri.replace('s3://', '').split('/', 1)
            img_data = s3_client.get_object(Bucket=bucket, Key=key)['Body'].read()
            images.append(img_data)
    
    # Format for your SageMaker model
    payload = {
        "inputs": f"{system_text}\n\n{user_text}",
        "parameters": {
            "temperature": event.get('inferenceConfig', {}).get('temperature', 0.0),
            "max_new_tokens": event.get('inferenceConfig', {}).get('maxTokens', 4096),
        }
    }
    
    response = sagemaker_runtime.invoke_endpoint(
        EndpointName='my-model-endpoint',
        ContentType='application/json',
        Body=json.dumps(payload)
    )
    
    result = json.loads(response['Body'].read())
    
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": result['generated_text']}]
            }
        },
        "usage": {
            "inputTokens": result.get('input_tokens', 0),
            "outputTokens": result.get('output_tokens', 0),
            "totalTokens": result.get('total_tokens', 0),
        }
    }
```

## IAM Permissions

### Your Lambda Function Needs

Your custom Lambda function needs read access to the IDP working bucket (for S3-referenced images):

```json
{
  "Effect": "Allow",
  "Action": ["s3:GetObject"],
  "Resource": "arn:aws:s3:::idp-working-bucket-*/temp/lambdahook/*"
}
```

### IDP Stack Grants

The IDP stack automatically grants its Lambda functions:
- `lambda:InvokeFunction` on `arn:aws:lambda:*:*:function:GENAIIDP-*`
- `s3:PutObject` on the working bucket `temp/lambdahook/` prefix (for image uploads)

## Error Handling

The Lambda Hook includes built-in retry logic:
- **Transient errors** (throttling, timeout): Retried with exponential backoff (same as Bedrock)
- **Function errors**: Retried for unhandled exceptions (cold start issues, etc.)
- **Permanent errors**: Raised immediately (invalid ARN, missing permissions, etc.)

## Metering and Cost Tracking

Lambda Hook invocations are tracked in the document's metering data under:
```
{context}/lambda_hook/{lambda_arn}
```

For example: `Extraction/lambda_hook/arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-extractor`

Token usage from the Lambda response's `usage` field is included in metering for cost calculations.

## Limitations

1. **Lambda payload limit**: The 6MB synchronous invocation payload limit is mitigated by uploading images to S3, but extremely large text content (>5MB of text alone) may still hit the limit.
2. **Lambda timeout**: Lambda functions have a maximum timeout of 15 minutes. For very large documents, consider chunking.
3. **Cold starts**: Lambda cold starts add latency to the first invocation. Use provisioned concurrency for consistent performance.
4. **No cachePoint support**: Bedrock's prompt caching feature is not available with Lambda hooks.
5. **No guardrails**: Bedrock Guardrails are not applied to Lambda hook invocations.