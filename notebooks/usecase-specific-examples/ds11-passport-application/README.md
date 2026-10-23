# DS-11 U.S. Passport Application — Excluded-Class Demo

This notebook demonstrates the **excluded-class feature** using a real
DS-11 U.S. Passport Application form (6 pages). Pages 1–4 are static
boilerplate (legal warnings, fee info, tax notices, oaths) and are
automatically skipped by the pipeline — zero LLM calls for extraction,
assessment, or summarization. Only pages 5–6 (the actual application
form) are fully processed.

## Prerequisites

- An admin-capable AWS profile with Bedrock (Claude Sonnet / Nova Pro)
  + S3 access. The notebook uses the `default` profile; edit
  `AWS_PROFILE = "default"` in section 1.1 if your admin profile is
  named something else.
- The `idp_common` library installed
  (`pip install -e lib/idp_common_pkg[all]`).
- No manual bucket setup required — buckets are auto-named from account
  ID + region and created on demand. Override with
  `IDP_INPUT_BUCKET_NAME` / `IDP_OUTPUT_BUCKET_NAME` env vars if needed.

## Config — minimal override pattern

`config/config.yaml` is a **minimal override config** containing only
`notes` + `classes` (no `classification:`, `extraction:`, `assessment:`,
etc.). Everything else is inherited from the bundled **system defaults**
(`idp_common/config/system_defaults/pattern-2.yaml`) via
`merge_config_with_defaults()`. This matches the production pattern:
users only specify the document classes they care about; the service
defaults cover everything else.

- **Primary classification** uses the default one-stage, page-granularity
  classification (sends each page's image +
  OCR text to the LLM, which picks the best-matching class using the
  `description` field).
- **Optional regex fast-path** on the excluded class
  (`x-aws-idp-document-page-content-regex`) short-circuits pages whose
  OCR text matches a known stable boilerplate phrase, saving tokens.
  The LLM description is still the primary, robust mechanism — the
  regex is just an optimization.

## How to run

Open `demo.ipynb` and run all cells. The notebook walks through:

1. **Setup** — environment, bucket creation, PDF upload
2. **Load config** — merge minimal user config with system defaults
3. **OCR** — parse the 6-page PDF
4. **Classification** — multimodal page-level classifier (+ optional
   regex fast-path) splits the document into two sections:
   - `PassportApplicationInstructions` (pages 1–4, excluded)
   - `PassportApplication` (pages 5–6, active)
5. **Extraction** — excluded section gets a stub `result.json`; active
   section runs full LLM extraction
6. **Assessment** — skipped for excluded sections
7. **Summarization** — stub `summary.json` for excluded sections,
   narrative summary for active sections
8. **Inspect outputs** — side-by-side real vs stub artefacts for each
   section
9. **ROI summary** — page-count table showing what was skipped

## See also

- `config_library/unified/ds11-passport-application/` — the standalone
  single-file config (suitable for `CustomConfigPath` deployment of a
  full stack)
- `docs/classification.md#excluding-static-pages` — full feature
  documentation
- `lib/idp_common_pkg/idp_common/section_exclusion.py` — shared helpers
- `lib/idp_common_pkg/tests/unit/test_section_exclusion.py` — 31 unit
  tests
