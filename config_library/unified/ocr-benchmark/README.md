# OmniAI OCR Benchmark Configuration

This configuration is designed for the **OmniAI OCR Benchmark dataset** from HuggingFace (`getomni-ai/ocr-benchmark`), filtered to include only the most representative document formats with consistent schemas.

## Dataset Overview

The OCR Benchmark dataset contains diverse document types with ground truth JSON extraction data. This configuration includes the **9 document formats** with the most samples (formats with >5 samples per schema), totaling **293 pre-selected images**.

## Document Classes

| Class | Description | Key Fields |
|-------|-------------|------------|
| **BANK_CHECK** | Bank checks with MICR encoding | checks[] (bank, personal info, payee, amount, MICR) |
| **COMMERCIAL_LEASE_AGREEMENT** | Commercial property leases | lessor/lessee info, premises, lease terms, rent |
| **CREDIT_CARD_STATEMENT** | Account statements | accountNumber, period, transactions[] |
| **DELIVERY_NOTE** | Shipping/delivery documents | header (from/to), items[] with product specs |
| **EQUIPMENT_INSPECTION** | Inspection reports | equipmentInfo, checkpoints[], overallStatus |
| **GLOSSARY** | Alphabetized term lists | title, pageNumber, glossarySections[] |
| **PETITION_FORM** | Election petition forms | header, candidate, witness, signatures[] |
| **REAL_ESTATE** | Real estate transaction data | transactions[], transactionsByCity[] |
| **SHIFT_SCHEDULE** | Employee scheduling | title, facility, employees[] with shifts |

## Benchmark Results

Evaluated on the full 293-document dataset using IDP Accelerator v0.5.0 (pattern-2, pipeline mode). Evaluation methods are identical across all configs for apples-to-apples comparison.

| Metric | Previous Config | This Config (Nova 2 Lite) | With Sonnet 4.6 |
|--------|----------------|---------------------------|------------------|
| **Overall Accuracy** | 51.5% | 75.2% | 91.2% |
| **Classification Accuracy** | 100% | 100% | 100% |
| **Total Cost (293 docs)** | $2.60 | $2.62 | $9.73 |
| **Cost per Document** | ~$0.009 | ~$0.009 | ~$0.033 |

### Per-Class Extraction Accuracy

| Class | Previous | This Config (Nova) | With Sonnet |
|-------|----------|-------------------|-------------|
| DELIVERY_NOTE (8) | 89.5% | 98.9% | 99.4% |
| PETITION_FORM (51) | 74.7% | 96.7% | 98.4% |
| COMMERCIAL_LEASE_AGREEMENT (52) | 75.5% | 96.3% | 98.5% |
| SHIFT_SCHEDULE (18) | 68.9% | 95.7% | 96.0% |
| REAL_ESTATE (59) | 80.6% | 91.4% | 98.9% |
| BANK_CHECK (52) | 82.6% | 86.1% | 97.0% |
| EQUIPMENT_INSPECTION (11) | 60.8% | 83.6% | 97.1% |
| CREDIT_CARD_STATEMENT (11) | 53.1% | 74.7% | 82.3% |
| GLOSSARY (31) | 68.0% | 67.3% | 95.0% |

### Models Used

- **Classification**: Nova 2 Lite (`us.amazon.nova-2-lite-v1:0`)
- **Extraction**: Nova 2 Lite (`us.amazon.nova-2-lite-v1:0`)
- **OCR**: Textract (Layout feature)

To use Sonnet 4.6 for extraction, change `extraction.model` to
`us.anthropic.claude-sonnet-4-6` (or `us.anthropic.claude-sonnet-4-6:1m` for the
extended-context variant). These are the ids the stack parameter's enum and
`config_library/pricing.yaml` carry; a dated `-v1:0` suffix is not one of them and
config validation rejects it with `extraction.model has invalid model ID`.

## Configuration Variants In This Directory

`config.yaml` is the preset. It is the only file the `ocr-benchmark` value of the
**Configuration Preset** stack parameter resolves to, and the only one the web UI's
*Import from library* offers. The others are benchmark arms, installed by naming them
explicitly — `idp-cli config-upload -f <path>`, `idp-cli deploy --custom-config
<path>`, or a **Custom Configuration Path** S3 URI.

| File | Purpose |
|---|---|
| `config.yaml` | The selectable preset, and the most current of these files |
| `ocr_config.yaml` | Base-model arm of the fine-tuning A/B |
| `fine_tuned_config.yaml` | Fine-tuned arm of the A/B |
| `ocr_fine_tuned_config.yaml` | Byte-identical duplicate of `fine_tuned_config.yaml` |

### Running the fine-tuned arm

`fine_tuned_config.yaml` exists to measure a **fine-tuned** Nova 2 Lite against the
base model, on both `classification.combined.llm.model` and `extraction.model`. A fine-tuned model
is reached through a Bedrock `custom-model-deployment` ARN, which is account-scoped: it
resolves only in the account that owns the deployment, and from anywhere else Bedrock
answers `AccessDeniedException` — *"the provided resource ARN is from a different
account"* — so the stage fails outright rather than degrading. The file therefore
cannot ship a working one.

Both `model` values ship as `us.amazon.nova-2-lite-v1:0`, the base model the fine-tune
was derived from, so the file runs as-is and is priced and limit-checked correctly.

⚠️ **As shipped, the two arms of this A/B are the same configuration, so running both
produces the same numbers and a delta of zero.** That is not a result about
fine-tuning — it is the consequence of the fine-tuned arm having no deployment to
point at until you supply one. `fine_tuned_config.yaml` and `ocr_config.yaml` now
differ only in comments and the position of one key. Substitute your own deployment
before reading any comparison between them.

To run the fine-tuned arm:

1. Fine-tune `amazon.nova-2-lite-v1:0` on your own data.
2. Create a custom model deployment for the result and note its ARN.
3. Replace both `model` values in your copy with that ARN. Each line is marked with a
   comment showing the shape:
   `arn:aws:bedrock:<region>:<your-account-id>:custom-model-deployment/<deployment-id>`.
4. Add a `bedrock/<that ARN>` entry to your pricing configuration if you want cost
   reporting for the run — a model with no pricing entry records a NULL cost rather
   than a wrong one, so the run's spend is reported as incomplete.

### What a substituted ARN does to the output budget

Extraction has no `max_tokens` setting — it always requests the model's maximum
output, resolved from `config_library/model_config_limits.yaml`. That lookup matches
on the model id, so `us.amazon.nova-2-lite-v1:0` resolves to Nova's 10,000-token
output limit and a 300,000-token input window.

A `custom-model-deployment` ARN matches no pattern in that file, so the lookup falls
back to its conservative unmatched defaults — a smaller output budget and input
window than the base model actually has. Nothing fails and nothing is logged as an
error, but a fine-tuned arm sized off those defaults is not sized the same way as the
base arm it is being compared against. If the comparison is about long outputs, add a
pattern for your deployment id to `model_config_limits.yaml` so both arms resolve the
same limits.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false). Set use_bda: true for BDA mode.

## Validation Level

**Level**: 3 - Benchmarked

- **Testing Evidence**: Evaluated on the full 293-document OmniAI OCR Benchmark dataset with per-class accuracy breakdown. Evaluation methods identical to previous config for fair comparison.
- **Known Limitations**: GLOSSARY class has lower accuracy (67.3%) due to OCR challenges with single-digit numbers. Upgrading extraction model to Claude Sonnet 4.6 improves overall accuracy to 91.2% at higher cost.
