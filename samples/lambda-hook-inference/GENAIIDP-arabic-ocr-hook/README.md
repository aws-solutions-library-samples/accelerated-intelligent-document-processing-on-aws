# GENAIIDP-arabic-ocr-hook

An OCR Lambda Hook for Arabic and mixed Arabic/English documents. It keeps the
IDP's full-page Bedrock transcription exactly as `ocr.backend: bedrock`
produces it, and adds Textract-format **LINE bounding boxes** so the Web UI
Visual Editor can highlight where Arabic (and English) fields came from.

## Why

Arabic is not among Amazon Textract's supported languages: its Arabic output
is unreliable (in testing, Arabic words came back in reversed order). The IDP's
Bedrock-vision OCR backend reads Arabic correctly but returns no geometry, so
the Visual Editor has nothing to highlight. This hook supplies that geometry
without changing the text.

## How it works

For each page the IDP sends:

1. **Full-page transcription.** The hook calls the Bedrock model
   (`ReaderModelId`) with the IDP's own OCR system prompt, task prompt and page
   image - the same call the Bedrock OCR backend makes. That text is returned
   unchanged as the page text. If this call fails, the error propagates and the
   IDP retries the page exactly as it would its own Bedrock OCR call.
2. **Box detectors.** PaddleOCR (PP-OCRv5 mobile detector + Arabic recognizer,
   running inside the container) finds Arabic text lines; Amazon Textract
   `DetectDocumentText` finds English/Latin lines. Each detector runs in
   isolation, so one failing only removes its own boxes.
3. **Box text (`ReadMethod`).** With the default `M0`, each box keeps the text
   of the detector that found it - Textract for Latin lines, PaddleOCR for
   Arabic lines - with no extra Bedrock call, so text never moves to another
   box. Two corrections use the transcription, and only ever add what it
   confirms:
   - PaddleOCR's Arabic recognizer drops digits (`١٢`, `٧`). When every other
     character of a PaddleOCR line matches one place in the transcription
     exactly, the line takes that span, digits included.
   - On a mixed row, PaddleOCR reads only the Arabic. Latin and numeric words
     Textract read in the same box (for example `Serial No. UTA 7712 0045`)
     are kept when the same word occurs in the transcription.

   `M1`/`M2` re-read the boxes with one extra Bedrock call per page and `M3`
   with one call per 8 boxes. They can attach a value to a neighbouring box
   (seen in testing: a reference number placed on the "Date" label), so a
   field highlights in the wrong place. Use `M0`.
4. **Response.** The page text plus a top-level `textractBlocks` object: one
   `PAGE` block and one `LINE` block per text line, with normalized
   `Geometry.BoundingBox` and `Polygon`. Rows that are mainly Arabic are
   ordered right-to-left. If no boxes are found, or the box pipeline fails for
   any reason, the response is the transcription alone - the same result as the
   plain Bedrock OCR backend.

## Accuracy

Measured end to end on a deployed IDP stack with three fictional specimen
documents - an Arabic commercial registration certificate, an English building
permit letter and a mixed Arabic/English driving licence, 25 fields with known
values and boxes - using this hook (`M0`, `eu.anthropic.claude-sonnet-5-5`) and
the [`config.yaml`](config.yaml) settings:

| Check | Result |
|---|---|
| Documents processed | 3 / 3, classified correctly |
| Field values in the page transcription | 25 / 25 |
| Highlight on the field's own line, covering the value | 25 / 25 |
| Extracted value identical to the printed value | 23 / 25 (a trailing `.` added; an honorific `Mr.` dropped) |

The same run with `M2` placed 5 of the 25 highlights on the wrong line. Three
documents are a smoke test, not a benchmark: measure on your own documents.

## Deploying

Prerequisites: Docker, the AWS SAM CLI, and a deployed IDP stack. **Deploy in
the same account and Region as the IDP stack**: the IDP's OCR role may invoke
only `GENAIIDP-*` functions in its own account and Region.

```bash
cd samples/lambda-hook-inference/GENAIIDP-arabic-ocr-hook
sam build
sam deploy --guided --resolve-image-repos \
  --stack-name GENAIIDP-arabic-ocr-hook \
  --parameter-overrides \
    IDPWorkingBucket=<idp-working-bucket> \
    CustomerManagedEncryptionKeyArn=<idp-kms-key-arn> \
    ReaderModelId=eu.anthropic.claude-sonnet-5-5
```

`sam build` downloads the two PaddleOCR models from PaddlePaddle's model hub
and bakes them into the image; the function makes no model downloads at run
time.

| Parameter | Default | Description |
|---|---|---|
| `IDPWorkingBucket` | *(required)* | The IDP stack's working bucket |
| `CustomerManagedEncryptionKeyArn` | `""` | The IDP stack's `CustomerManagedEncryptionKeyArn` output. Required for IDP stacks: their working bucket is SSE-KMS encrypted, so without it every page fails with AccessDenied |
| `ReaderModelId` | `eu.anthropic.claude-sonnet-5-5` | Bedrock inference profile for the transcription call (Claude Sonnet 5 or 5.5). Use the `us.` or `eu.` prefix that matches your Region; `global.` profiles may process data in any commercial Region |
| `ReadMethod` | `M0` | How each box gets its text; keep `M0` (see "How it works") |
| `ReaderEffort` | `medium` | Reasoning effort for the transcription call (`none` omits it) |
| `LogRetentionDays` | `30` | CloudWatch log retention |

The function uses 8,192 MB, mainly for PaddleOCR's CPU share. New accounts can
have a lower Lambda memory quota; request an increase or lower `MemorySize`
(Paddle runs slower). `Timeout` is 780 s, below the IDP's 840 s hook read
timeout. Measured: 1.7 GB peak memory; about 85-90 s for a page on a cold
container (model load) and 16-30 s warm.

Right after a deploy, Lambda can answer the first invocations with a 409
("Lambda is initializing your function") while it prepares the container
image; the IDP retries those pages.

## Configuring the IDP

[`config.yaml`](config.yaml) holds the tested settings: the `ocr` section
(LambdaHook backend, Arabic-preserving prompts) and the extraction model. Merge
it into your configuration profile - keep your own `classes` - set
`model_lambda_hook_arn` to the stack's `ArabicOcrHookFunctionArn` output, and
upload it:

```bash
idp-cli config-upload --stack-name <idp-stack> \
  --config-file my-profile.yaml --config-profile arabic-ocr
```

`ocr.task_prompt` must contain `{DOCUMENT_IMAGE}`, or the hook receives no
image; `idp-cli config-validate` rejects a LambdaHook profile without it.
Process documents under the profile with `--config-profile arabic-ocr`, or
activate it. To roll back, activate the profile you used before.

## Permissions

| Action | Resource |
|---|---|
| `s3:GetObject` | `<IDPWorkingBucket>/temp/lambdahook/*` (the page image the IDP uploads) |
| `textract:DetectDocumentText` | `*` (the action has no resource-level ARN) |
| `bedrock:InvokeModel` | The selected inference profile and its exact foundation model |
| `kms:Decrypt` | The IDP key, only via S3 (`kms:ViaService`), only when the key ARN is set |

Metrics are written as CloudWatch Embedded Metric Format log lines (namespace
`IDP/ArabicOCR`: `Pages`, `BoxLines`, `BoxesDegraded`, `DetectorDegraded`,
`TextractDegraded`), so no `cloudwatch:PutMetricData` permission is needed.

## Cost per page

Estimates from measured averages; check current AWS pricing before budgeting.

- **Transcription:** the same Bedrock call the Bedrock OCR backend makes.
  About 3,100 input and 700-870 output tokens per page, at the
  `eu.anthropic.claude-sonnet-5-5` rates in `config_library/pricing.yaml`, is
  about **$0.015**.
- **Textract `DetectDocumentText`:** $0.0015.
- **Lambda compute:** 8 GB for 14-28 s warm, about **$0.002-0.004**. A cold
  start (model load) adds about 30 s once per container.
- **PaddleOCR:** runs inside the function; no separate charge.
- **M1-M3** add Bedrock calls (`M2`: about 2.6 times the tokens).

The IDP meters hook usage under `<context>/lambda_hook/<function-name>`
(here `OCR/lambda_hook/GENAIIDP-arabic-ocr-hook`), which
`config_library/pricing.yaml` does not price, so its tokens are recorded with
no cost until you add an entry to your profile's `pricing:` list. Copy the
unit prices from the `bedrock/<ReaderModelId>` entry in
`config_library/pricing.yaml`:

```yaml
pricing:
  - name: lambda_hook/GENAIIDP-arabic-ocr-hook
    units:
      - name: inputTokens
        price: "<inputTokens price of bedrock/<ReaderModelId>>"
      - name: outputTokens
        price: "<outputTokens price of bedrock/<ReaderModelId>>"
```

The Textract call is made inside the hook and is not metered by the IDP.

## Limitations

- **No confidence scores.** LINE blocks carry no `Confidence`, so Assessment
  sees `N/A` for OCR confidence.
- **Line-level boxes.** A value that is part of a longer line highlights the
  whole line.
- **Box text is the detector's, not the transcription's.** In `M0`, LINE text
  comes from PaddleOCR or Textract, corrected only where the transcription
  confirms it; a misread letter is not corrected, and that field may not
  highlight.
- **Ambiguous day-first dates may not highlight.** The IDP's grounding matcher
  reads ambiguous numeric dates month-first, so `05.03.2024` meaning 5 March
  can fail to match its line (`14.05.2024` is unambiguous and highlights).
- **Sonnet 5.5 extraction needs a current IDP release.** Older releases
  (tested: v0.6.9) send `temperature`, which Sonnet 5.5 rejects; on those keep
  `extraction.model` on Sonnet 5. The hook's own transcription call sends no
  `temperature` and works with either.
- **Textract throttling.** Throttled or failed Textract calls (at most 3
  attempts) drop the Textract boxes for that page; Latin lines then get a
  PaddleOCR box with Arabic-recognizer text, or no box. The page text is
  unaffected (`TextractDegraded` metric).
- **Deadline skips.** A detector is skipped if less time remains than its
  worst case (Textract 130 s, PaddleOCR 60 s) after a slow transcription call
  (`DetectorDegraded`, `reason=DeadlineSkip`).

## Security

- No document text is logged: logs and metrics carry only exception names,
  error codes and counts, and a test asserts it.
- Python dependencies are pinned in `requirements.txt` (transitive
  dependencies are not). PaddleOCR, PaddleX and PaddlePaddle are Apache-2.0;
  check the PP-OCRv5 model cards for the weights' terms before you ship.
- Pin the base image by digest for reproducible builds
  (`docker buildx imagetools inspect public.ecr.aws/lambda/python:3.12`), and
  enable ECR scan-on-push on the repository `--resolve-image-repos` creates.

## Running the tests

```bash
cd samples/lambda-hook-inference/GENAIIDP-arabic-ocr-hook
pip install -r requirements-dev.txt
python -m pytest
```

The unit tests need no AWS access and no OCR models. The contract tests run the
hook's blocks through the real `idp_common` OCR and grounding code and skip if
`lib/idp_common_pkg` cannot be imported. The single test that loads the real
PaddleOCR models is marked `integration` and is excluded by default
(`python -m pytest -m integration` runs it where `paddleocr` is installed).
