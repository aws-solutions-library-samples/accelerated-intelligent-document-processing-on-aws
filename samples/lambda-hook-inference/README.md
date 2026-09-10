# Lambda Hook Inference Samples

Sample Lambda functions for use with the GenAI IDP Accelerator's **LambdaHook** custom inference feature.

When you select `LambdaHook` as the model in any IDP pipeline step (Classification, Extraction, Assessment, Summarization, OCR), the accelerator invokes your custom Lambda function instead of calling Amazon Bedrock directly. This lets you use **any LLM** — SageMaker endpoints, OpenAI, Gemini, Anthropic API, or any other inference provider.

See [docs/lambda-hook-inference.md](../../docs/lambda-hook-inference.md) for full feature documentation.

## Samples

| Sample | Description |
|--------|-------------|
| **GENAIIDP-bedrock-proxy** | Forwards to Bedrock Converse API. Use as a starting template for custom hooks with pre/post processing. |
| **GENAIIDP-sagemaker-hook** | Calls a SageMaker real-time inference endpoint. Shows format conversion between Converse API and SageMaker. |
| **GENAIIDP-chandra-ocr-hook** | Calls the [Chandra OCR 2](https://github.com/datalab-to/chandra) hosted API for high-quality OCR. Converts page images to structured Markdown, JSON, or HTML. |
| **GENAIIDP-mistral-ocr-hook** | Calls the hosted [Mistral OCR](https://mistral.ai/news/ocr-4/) API for high-quality OCR. Returns Markdown **plus per-word confidence scores and bounding-box geometry** (in Amazon Textract format) so extraction confidence and spatial localization work in Assessment and the UI. Fully serverless — no SageMaker/GPU. |
| **GENAIIDP-cohere-parse-hook** | Calls the hosted [Cohere Parse](https://cohere.com/blog/parse) API for low-cost OCR ($1.50 / 1,000 pages). Converts Cohere's HTML tables to Markdown pipe tables and returns **bounding boxes for tables and figures**. **No confidence scores** — Cohere Parse does not provide them. Fully serverless. |
| **GENAIIDP-w2-copy-consistency** | **Assessment** hook (not OCR): deterministically flags Form-W2 fields that disagree across duplicate copies of the same employee on one page (hand-filled Copy B vs Copy C). Pure Python comparison — no LLM call, no added inference cost. No-op passthrough for every other document class. |

## Naming Convention

All Lambda hook function names **must start with `GENAIIDP-`**. This enables secure, scoped IAM permissions — the IDP stack grants `lambda:InvokeFunction` only for functions matching `GENAIIDP-*`.

## Deployment

Each sample is independently deployable using its own SAM template. You can also deploy all samples together using the root template.

### Deploy a Single Sample

Each sample folder contains its own `template.yaml` for independent deployment:

```bash
# Deploy the Bedrock proxy sample
cd samples/lambda-hook-inference/GENAIIDP-bedrock-proxy
sam build
sam deploy --guided \
  --stack-name GENAIIDP-bedrock-proxy \
  --parameter-overrides \
    IDPWorkingBucket=<your-idp-working-bucket-name> \
    CustomerManagedEncryptionKeyArn=<your-kms-key-arn> \
    TargetModelId=us.amazon.nova-pro-v1:0
```

```bash
# Deploy the SageMaker hook sample
cd samples/lambda-hook-inference/GENAIIDP-sagemaker-hook
sam build
sam deploy --guided \
  --stack-name GENAIIDP-sagemaker-hook \
  --parameter-overrides \
    IDPWorkingBucket=<your-idp-working-bucket-name> \
    CustomerManagedEncryptionKeyArn=<your-kms-key-arn> \
    SageMakerEndpointName=<your-endpoint-name>
```

```bash
# Deploy the Chandra OCR hook sample
cd samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook
sam build
sam deploy --guided \
  --stack-name GENAIIDP-chandra-ocr-hook \
  --parameter-overrides \
    IDPWorkingBucket=<your-idp-working-bucket-name> \
    CustomerManagedEncryptionKeyArn=<your-kms-key-arn> \
    ChandraApiKey=<your-datalab-api-key>
```

```bash
# Deploy the Mistral OCR hook sample
cd samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook
sam build
sam deploy --guided \
  --stack-name GENAIIDP-mistral-ocr-hook \
  --parameter-overrides \
    IDPWorkingBucket=<your-idp-working-bucket-name> \
    CustomerManagedEncryptionKeyArn=<your-kms-key-arn> \
    MistralApiKey=<your-mistral-api-key>
```

```bash
# Deploy the Cohere Parse hook sample
cd samples/lambda-hook-inference/GENAIIDP-cohere-parse-hook
sam build
sam deploy --guided \
  --stack-name GENAIIDP-cohere-parse-hook \
  --parameter-overrides \
    IDPWorkingBucket=<your-idp-working-bucket-name> \
    CustomerManagedEncryptionKeyArn=<your-kms-key-arn> \
    CohereApiKey=<your-cohere-api-key>
```

```bash
# Deploy the W-2 copy-consistency Assessment hook sample
# (no parameters: this function makes no AWS service calls)
cd samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency
sam build
sam deploy --guided --stack-name GENAIIDP-w2-copy-consistency
```

> **Note:** The `CustomerManagedEncryptionKeyArn` is optional but required if the IDP stack's working bucket uses KMS encryption (which it does by default). You can find the KMS key ARN in the IDP stack's CloudFormation **Outputs** tab → `CustomerManagedEncryptionKeyArn`.

### Deploy All Samples Together

The root `template.yaml` deploys all samples in a single stack:

```bash
cd samples/lambda-hook-inference
sam build
sam deploy --guided \
  --stack-name GENAIIDP-lambda-hooks \
  --parameter-overrides \
    IDPWorkingBucket=<your-idp-working-bucket-name> \
    TargetModelId=us.amazon.nova-pro-v1:0 \
    SageMakerEndpointName=<your-endpoint-name> \
    ChandraApiKey=<your-datalab-api-key> \
    MistralApiKey=<your-mistral-api-key> \
    CohereApiKey=<your-cohere-api-key>
```

## Choosing an OCR hook

All three OCR hooks plug in the same way; they differ in what they can feed
downstream. Confidence is what Assessment consumes for extraction confidence,
and geometry is what the UI Visual Editor highlights.

| Backend | Confidence | Geometry | Tables | Languages | List price / page |
|---------|-----------|----------|--------|-----------|-------------------|
| Amazon Textract (native, no hook) | per LINE + WORD | per LINE + WORD | table structure via `TABLES` feature | see Textract docs | $0.0015 (DetectDocumentText) |
| **Mistral OCR** hook | per LINE + WORD | paragraph-level | Markdown | 170 | $0.004 |
| **Cohere Parse** hook | **none** | tables + figures only | HTML → converted to Markdown | 9 stable | $0.0015 |
| **Chandra OCR** hook | none | none | Markdown / HTML / JSON | 90+ | see datalab.to |

Pick **Mistral** when you need OCR-grounded extraction confidence or HITL
triggering off OCR quality; **Cohere Parse** when cost and table fidelity matter
more than confidence; **Textract** when you want an in-AWS backend with no
third-party data egress.

## Configuration in IDP

After deploying your Lambda hook:

1. Go to the IDP **Configuration** page
2. Select the step (e.g., Extraction)
3. Set **Model** to `LambdaHook`
4. Set **Model Lambda Hook ARN** to your function's ARN
5. Save

Or in config YAML:
```yaml
extraction:
  model: "LambdaHook"
  model_lambda_hook_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-bedrock-proxy"
```

### Chandra OCR Configuration

To use Chandra OCR 2 as the OCR engine, set the OCR backend to `bedrock` with `LambdaHook` as the model:

```yaml
ocr:
  backend: bedrock
  model_id: "LambdaHook"
  model_lambda_hook_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-chandra-ocr-hook"
```

[Chandra OCR 2](https://github.com/datalab-to/chandra) is a state-of-the-art VLM-based OCR model by [Datalab](https://www.datalab.to) that converts images into structured Markdown, JSON, or HTML. It supports 90+ languages, math, tables, forms (including checkboxes), handwriting, and complex layouts.

**Getting an API key:** Sign up at [datalab.to](https://www.datalab.to) to get your API key, then provide it when deploying the Lambda function.

**Local testing:** You can test Chandra OCR locally before deploying:
```bash
cd samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook
pip install pdf2image Pillow
export CHANDRA_API_KEY="your-api-key"
python test_local.py ../../insurance_package.pdf
```

### Mistral OCR Configuration

To use [Mistral OCR](https://mistral.ai/news/ocr-4/) as the OCR engine, set the OCR backend to `bedrock` with `LambdaHook` as the model:

```yaml
ocr:
  backend: bedrock
  model_id: "LambdaHook"
  model_lambda_hook_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-mistral-ocr-hook"
```

Mistral OCR 4 is a document-understanding model that returns markdown-structured text together with **paragraph-level bounding boxes**, typed-block classification, and **per-page / per-word confidence scores**, across 170 languages.

**Confidence scores and geometry (explainability):** Unlike a plain text-only OCR hook, this hook requests structured output (`include_blocks=true`, `confidence_scores_granularity=word`) and translates the Mistral response into **Amazon Textract response format** (a `Blocks` list with `LINE`/`WORD` blocks carrying `Confidence` and `Geometry.BoundingBox`). It returns this under a top-level `textractBlocks` key. The IDP OCR service detects `textractBlocks` and persists it as the page's `rawText.json` and `textConfidence.json`, so the OCR confidence flows into Assessment (the `{OCR_TEXT_CONFIDENCE}` prompt placeholder), and the geometry is available for UI bounding-box highlighting — exactly like the native Textract backend. Hooks that return only text keep the previous behavior unchanged.

**Cost metering:** The hook returns `usage.pages` (from Mistral's `usage_info.pages_processed`), so per-page cost is tracked. Add a pricing entry to `config_library/pricing.yaml` keyed on the function name with a `pages` unit (Mistral OCR list price is $4 / 1,000 pages = `0.004`):

```yaml
  - name: GENAIIDP-mistral-ocr-hook
    units:
      - name: pages
        price: "0.004"
```

**Getting an API key:** Sign up at [console.mistral.ai](https://console.mistral.ai) to get your API key, then provide it as `MistralApiKey` when deploying the Lambda function.

**Testing:** Three test scripts are provided in the sample folder:
```bash
cd samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook

# 1. Offline unit tests for the Mistral->Textract translation (no API/AWS needed)
python test_translation.py

# 2. Live API test against the hosted Mistral OCR API (single image works without poppler)
export MISTRAL_API_KEY="your-api-key"
python test_local.py ../../old_cal_license.png            # single image
pip install pdf2image Pillow                              # (PDFs need poppler installed)
python test_local.py ../../insurance_package.pdf --pages 1,2

# 3. End-to-end test of the DEPLOYED Lambda (uploads an image to temp/lambdahook/,
#    invokes the function, validates blocks + confidence + geometry + metering, cleans up)
AWS_PROFILE=default python test_deployed.py \
  --bucket <your-idp-working-bucket-name> \
  --image ../../old_cal_license.png
```

### Cohere Parse Configuration

To use [Cohere Parse](https://cohere.com/blog/parse) as the OCR engine, set the OCR backend to `bedrock` with `LambdaHook` as the model:

```yaml
ocr:
  backend: bedrock
  model_id: "LambdaHook"
  model_lambda_hook_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-cohere-parse-hook"
```

Cohere Parse (`parse-v5.0`) is a 2.3B-parameter vision language model that converts document images into Markdown at $1.50 / 1,000 pages — the cheapest of the OCR hooks, and 2.7× cheaper than Mistral OCR. On Cohere's own ParseBench it scores 87.0 on tables against Mistral OCR 4's 73.9 and Textract's 82.3, and 79.2 overall against 74.5 and 53.3 — but it trails Mistral on raw content faithfulness (86.6 vs 89.5). Treat vendor benchmarks as directional and validate on your own documents.

**HTML tables are converted to Markdown (default on):** Cohere Parse returns tables as HTML. The accelerator's deterministic table parser (used by [agentic extraction](../../lib/idp_common_pkg/idp_common/extraction/README.md)) reads Markdown pipe tables, so this hook converts them — otherwise every table would silently fall back to pure-LLM extraction and lose the row-completeness guarantee. Set `CONVERT_HTML_TABLES=false` to keep the raw HTML. `colspan` is expanded so columns stay aligned; a table nested inside another table is left as HTML (Markdown cannot represent it).

**No confidence scores.** Cohere Parse returns none, by design. Consequences to plan for:

- The `{OCR_TEXT_CONFIDENCE}` placeholder reports `N/A` per line rather than a score, so Assessment cannot ground extraction confidence in OCR quality. Confidence still comes from the assessment model itself.
- Don't tune HITL thresholds against OCR confidence on this backend.

Choose the Mistral hook instead if OCR-grounded confidence matters to you.

**Geometry for tables and figures only.** Parse returns bounding boxes on table and figure elements, never on text. The hook requests `output_format=blocks` and attaches each table's/figure's normalized box to every line derived from it, which the OCR service recognizes as paragraph-level geometry (`geometrySource: "paragraph"`). Result: tables and figures highlight in the UI Visual Editor; body text has no overlay. Parse's pixel `bounding_box` is ignored because normalizing it would need source-image dimensions the response does not carry.

⚠️ **Latency: ~50–90 seconds per page** on the hosted API, measured on a bank statement page, and it does not improve with a smaller image (150 dpi 55.6s, 300 dpi 50.9s). The vendor's "4.5 pages/second" is a self-hosted 8×H100 figure, not the hosted API. The pipeline waits up to 900s for a hook; releases before that fix waited only 60s and would re-invoke a slow hook mid-flight, re-running the paid call. Output is also not deterministic — repeat runs on the same page returned different block counts.

⚠️ **Switching an existing profile from `backend: textract` requires `{DOCUMENT_IMAGE}` in `ocr.task_prompt`.** Config validation rejects the save without it. The error's advice to remove `task_prompt` does not help if the stack's `default` profile is Textract-based, since the inherited default lacks the placeholder too — set it explicitly.

**Other limits to know:** images only (`document.type: "image_url"` — PDFs are not accepted, which is fine since the accelerator sends page images); 20 MB / 50 megapixel per image; nine stable languages (ar, en, fr, de, ja, ko, it, pt, es) with others zero-shot at lower accuracy; headers, footers and font hierarchy are not identified; charts get a description rather than extracted data series. Rate limit is a flat **500 requests/minute** for trial *and* production keys, so the hook retries 429/5xx with exponential backoff (`MAX_RETRIES`, default 4). Trial keys are additionally capped at 1,000 calls/month — enough for a POC, not for a batch run.

**Cost metering:** The hook returns `usage.pages` (from Parse's `meta.billed_units.pages`). `config_library/pricing.yaml` ships the entry:

```yaml
  - name: GENAIIDP-cohere-parse-hook
    units:
      - name: pages
        price: "0.0015"
```

**Getting an API key:** Sign up at [dashboard.cohere.com](https://dashboard.cohere.com/api-keys) to get your API key, then provide it as `CohereApiKey` when deploying the Lambda function.

**Testing:** Three test scripts are provided in the sample folder:
```bash
cd samples/lambda-hook-inference/GENAIIDP-cohere-parse-hook

# 1. Offline unit tests for HTML->Markdown tables and the Textract translation
#    (no API key or AWS needed)
python test_translation.py

# 2. Live API test against the hosted Cohere Parse API
export COHERE_API_KEY="your-api-key"
python test_local.py ../../old_cal_license.png            # single image
pip install pdf2image Pillow                              # (PDFs need poppler installed)
python test_local.py ../../insurance_package.pdf --pages 1,2

# 3. End-to-end test of the DEPLOYED Lambda (uploads an image to temp/lambdahook/,
#    invokes the function, validates blocks + geometry + metering + the ABSENCE
#    of confidence, cleans up)
AWS_PROFILE=default python test_deployed.py \
  --bucket <your-idp-working-bucket-name> \
  --image ../../old_cal_license.png
```

### W-2 Copy Consistency Configuration

This is an **Assessment** hook, not an OCR one — it replaces the confidence model
rather than the OCR engine, so it wires into `extraction.confidence`:

```yaml
extraction:
  confidence:
    model: "LambdaHook"
    model_lambda_hook_arn: "arn:aws:lambda:us-east-1:123456789012:function:GENAIIDP-w2-copy-consistency"
hitl:
  confidence_threshold: 0.8   # must be above the 0.2 the hook assigns to mismatches
```

A single Form-W2 page usually carries several duplicate copies of the *same*
employee's W-2 (the perforated Copy B / Copy 2 / Copy C layout). Because these
are hand-completed, one copy can disagree with another — e.g. Box 1 wages differ
between quadrants. The hook groups `w2_copies` rows by normalized SSN and scores
any critical field that disagrees *within an SSN group* at low confidence (0.2)
with a `confidence_reason` naming the mismatch. Values differing across
*different* SSNs (a genuine multi-employee page) are never flagged. Everything is
deterministic Python — no LLM call, so no added inference cost or latency.

For any other document class (no `w2_copies` key in the extraction) the hook is a
**no-op passthrough** returning uniform high confidence, so it is safe to leave
configured on a multi-class stack. Note that this means non-W2 classes get no
real confidence judgement while it is active.

**Local testing** (no AWS or API key needed):
```bash
cd samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency
python test_local.py
```

## Request/Response Format

### Request (sent to your Lambda)

```json
{
  "modelId": "LambdaHook",
  "messages": [
    {
      "role": "user",
      "content": [
        {"text": "Extract the following attributes..."},
        {
          "image": {
            "format": "jpeg",
            "source": {
              "s3Location": {"uri": "s3://working-bucket/temp/lambdahook/abc123.jpeg"}
            }
          }
        }
      ]
    }
  ],
  "system": [{"text": "You are a document extraction expert..."}],
  "inferenceConfig": {"temperature": 0.0, "maxTokens": 10000},
  "context": "Extraction"
}
```

> **Note:** Images are sent as S3 references (not inline bytes) to avoid Lambda's 6MB payload limit. Your function needs `s3:GetObject` permission on the IDP working bucket.

### Response (return from your Lambda)

```json
{
  "output": {
    "message": {
      "role": "assistant",
      "content": [{"text": "{\"account_number\": \"12345\", ...}"}]
    }
  },
  "usage": {
    "inputTokens": 1500,
    "outputTokens": 200,
    "totalTokens": 1700
  }
}
```

#### Optional: structured OCR output (confidence + geometry)

For the **OCR** context, a hook may optionally return a top-level `textractBlocks`
object in **Amazon Textract response format**. When present (and non-empty), the IDP
OCR service persists it as the page's `rawText.json` / `textConfidence.json` instead
of writing the "no confidence data" placeholder — carrying per-line/word confidence
and bounding-box geometry into Assessment and the UI. See the
**GENAIIDP-mistral-ocr-hook** sample for a full implementation.

```json
{
  "output": {"message": {"role": "assistant", "content": [{"text": "# Invoice\n..."}]}},
  "textractBlocks": {
    "DocumentMetadata": {"Pages": 1},
    "Blocks": [
      {"BlockType": "PAGE", "Id": "..."},
      {"BlockType": "LINE", "Id": "...", "Text": "Account: 12345", "Confidence": 97.5,
       "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.02, "Width": 0.4, "Height": 0.03}}},
      {"BlockType": "WORD", "Id": "...", "Text": "12345", "Confidence": 92.0}
    ]
  },
  "usage": {"pages": 1, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
}
```

> Geometry uses Textract's normalized 0–1 `BoundingBox` (`Left`, `Top`, `Width`, `Height`).
> `usage.pages` enables per-page cost metering (add a pricing entry keyed on the function name with a `pages` unit).

## IAM Permissions

Your Lambda function needs:
- **S3 read** on the IDP working bucket (`s3:GetObject` on `arn:aws:s3:::<working-bucket>/temp/lambdahook/*`)
- **KMS decrypt** if the working bucket uses customer-managed KMS encryption (`kms:Decrypt`, `kms:GenerateDataKey`)
- **Bedrock invoke** (for bedrock-proxy sample): `bedrock:InvokeModel` on foundation models
- **SageMaker invoke** (for sagemaker-hook sample): `sagemaker:InvokeEndpoint` on your endpoint

The SAM templates handle these permissions automatically, including conditional KMS access.
