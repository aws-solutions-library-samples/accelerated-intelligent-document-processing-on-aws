# Adding (or Changing) a Selectable Bedrock Model — GenAI IDP Accelerator

Use this skill whenever the task is "add support for model X" / "wire up the new
Y model" / "bump the default model". A selectable model touches ~15 files across
templates, config, client routing, UI, docs (both tiers), and tests — this skill
makes it fast and safe. It supersedes the shorter checklist in
`documentation.md` (which this expands).

> **Golden rule:** never invent model facts. Model ID, regions, context/output
> limits, tier support, caching support, and pricing all come from the AWS
> Bedrock **model card** + **region-compatibility page** + **pricing page**.
> Look them up first (see Step 0).

---

## Step 0 — Gather ground truth BEFORE editing (do not skip)

Pull these from AWS docs (the `aws-knowledge` MCP `search_documentation` /
`read_documentation` tools work well; the region page and model cards are the
canonical sources):

- [ ] **Exact model ID(s)** — including any `-variant` suffix (e.g.
      `openai.gpt-5.6-sol`) or region/`:1m` prefix/suffix. A "single" launch may
      ship as **several** model IDs (Sol/Terra/Luna). Confirm how many.
- [ ] **Endpoint / API** — Converse/InvokeModel (normal path) vs
      `bedrock-mantle` Responses API (OpenAI GPT-5.x path). This decides whether
      any routing code is even involved.
- [ ] **Regions** — In-Region list per model, and whether geo/global
      cross-region IDs (`us.`, `eu.`, `global.`) exist. GovCloud? Per-variant
      differences (one variant often lacks a region the others have).
- [ ] **Context window + max output tokens.**
- [ ] **Service tiers** (standard / priority / flex / reserved).
- [ ] **Prompt caching** support (and whether it's implicit or explicit
      breakpoints).
- [ ] **Input modalities** (text / image / document / audio…).
- [ ] **Reasoning model?** (rejects temperature/top_p/top_k, uses effort).
- [ ] **Pricing** — input / output / cache-read / cache-write per 1M tokens.
      The pricing MCP tool may be denied by IAM; fall back to the Bedrock
      pricing page. If unresolved, mark `TODO(pricing)` and flag for the user.

Record findings in the proposal/PR description so reviewers can verify.

---

## Step 1 — Find every touchpoint for the SIBLING model

The safest way to be complete: pick the closest existing model and grep for it
everywhere. That set of files IS your edit list.

```bash
# Replace with the closest sibling ID (family + region variants)
SIB='gpt-5.5'   # or 'claude-sonnet-5', 'nova-pro', ...
grep -rniI "$SIB" . --include='*.py' --include='*.yaml' --include='*.yml' \
  --include='*.json' --include='*.ts' --include='*.tsx' --include='*.md' -l
# Count enum duplication inside the big template so you don't miss a site:
grep -cn "\"openai.$SIB\"" patterns/unified/template.yaml
```

Cross-check against the file map below.

---

## Step 2 — The file map

### A. Client routing (`idp_common/bedrock/`) — only if endpoint/params differ
- [ ] `client.py` — `CACHEPOINT_SUPPORTED_MODELS`, effort model sets
      (`_CLAUDE_4_7_BASE_NAMES` etc.), sampling-param strips. For a normal
      Converse model in an existing family this is often a one-line add or
      nothing.
- [ ] `openai_responses.py` — **mantle/GPT-5.x only**: `_RESPONSES_API_MODELS`,
      `_MODEL_REGIONS` (per-model!), `_MODEL_DEFAULT_REGION`, docstring. The
      `startswith("openai.gpt-5")` forward-compat guard means routing/rejects
      usually need **no change** — but VERIFY with a test.
- [ ] `model_utils.py` — max-output-token docstrings/logic if the limit is new.

### B. Config / pricing / limits
- [ ] `config_library/pricing.yaml` — `bedrock/<model-id>` block per model
      (input/output/cacheRead/cacheWrite). One block per model ID.
- [ ] `config_library/model_config_limits.yaml` — the `pattern:` is a **regex**;
      a new same-family variant often already matches (e.g. `openai\.gpt-5`
      matches `gpt-5.6-sol`). If limits differ, add/split a pattern. Update the
      `description`.
- [ ] `lib/idp_common_pkg/idp_common/config/models.py` — only if the model needs
      a NEW inference parameter/field. Effort docstrings if the effort vocab
      changes.
- [ ] `config/system_defaults/*.yaml` — only if changing a default model.

### C. Templates (CloudFormation enums / ConfigSchema picklists) — the high-miss-count area
- [ ] `patterns/unified/template.yaml` — the `UpdateSchemaConfig` custom
      resource's `Schema:` block IS the **ConfigSchema** that drives every
      model picklist in the configuration UI. Add the ID to **every** service
      `model` / `model_id` enum in it: extraction, per-class
      `extraction_model`, classification, assessment, summarization,
      confidence model, evaluation `llm_method`, chat. Use the grep count from
      Step 1 to confirm you hit them all (both YAML-list and JSON-array enum
      styles appear — watch trailing commas in the JSON-style lists). Put it
      in the correct region sub-list (US / EU / global), including the `:1m`
      variants where the sibling has them.
- [ ] `template.yaml` — IAM. If the model uses an existing endpoint namespace
      already granted (e.g. namespace-wide `bedrock-mantle:*` or
      `bedrock:InvokeModel*` on `foundation-model/*`), no change — VERIFY the
      comment blocks. Add ARNs only for a genuinely new action/endpoint.
- [ ] `template.yaml` — **`BEDROCK_MODEL_QUOTA_CODES` and
      `BEDROCK_MODEL_RPM_QUOTA_CODES`** on the `CalculateCapacity` function. This
      surface is easy to skip and it fails HARD rather than degrading:
      `src/lambda/calculate_capacity/index.py` raises
      `ValueError("TPM quota not available for model …")` for any configured step
      model missing from the TPM map, so **Capacity Planning errors out** for a
      model you made selectable. Get the codes live rather than guessing:
      ```bash
      AWS_PROFILE=default aws service-quotas list-service-quotas \
        --service-code bedrock --region us-east-1 --page-size 100 \
        --query "Quotas[?contains(QuotaName,'<Model Name>')].[QuotaCode,QuotaName,Value]" \
        --output text
      ```
      ⚠️ **Add it to BOTH maps, even when the RPM one has to reuse the TPM code.**
      AWS does not publish a requests-per-minute quota for every model (Claude
      Sonnet 5 and Sonnet 5.5 have none), and the RPM builder matches with
      `clean_mapping_key in clean_model_id` — so for an id that *contains* an
      existing key (`claude-sonnet-5` ⊂ `claude-sonnet-5-5`) an absent entry does
      not raise, it silently binds your model to **another model's** quota code.
      The maps are an acknowledged incomplete subset of the picklists; adding the
      model you are working on does not close that class.
- [ ] `nested/multi-doc-discovery/template.yaml` and other nested templates —
      IAM / enums as applicable.

### D. Region filtering + UI
- [ ] `src/lambda/update_configuration/index.py` — `US_ONLY_MODELS` (or EU
      filtering). Keep explicit sets complete even when a `startswith` fallback
      covers the new ID.
- [ ] `src/lambda/update_configuration/index.py` — Canada: if ca-central-1 has
      no inference profile and no in-region hosting for the new ID (check with
      `aws bedrock list-inference-profiles --region ca-central-1 --type-equals
      SYSTEM_DEFINED` and `aws bedrock list-foundation-models --region
      ca-central-1`), add it to `CA_UNAVAILABLE_MODELS`; otherwise Canada
      picklists offer it and it fails at inference. If it becomes a shipped
      default that Canada cannot call, also add a one-way swap to
      `CA_MODEL_MAPPINGS`.
- [ ] `src/ui/src/constants/schemaConstants.ts` — dropdown `{label,value}` per
      model.
- [ ] `src/ui/.../SchemaInspector.tsx` — any other hardcoded UI list.

### E. Feature guards (models rejected for some features)
- [ ] `discovery/classes_discovery.py`, `discovery/rules_discovery.py` — GPT-5.x
      is rejected for discovery (PDF document blocks unsupported).
- [ ] `config/merge_utils.py` — `_validate_agentic_openai`,
      `_validate_discovery_openai` reject GPT-5.x for advanced/agentic + discovery.
- [ ] `chat_with_document_processor/index.py` + its **vendored copy** in
      `chat_stream_processor/vendored/` — keep in sync.

### F. Docs — BOTH tiers (see `documentation.md`)
- [ ] `lib/idp_common_pkg/idp_common/bedrock/README.md` — module behavior,
      per-model regions/caching/caveats (canonical home for client behavior).
- [ ] `docs/*.md` — every feature doc that lists models: `openai-models.md` (or
      a new dedicated guide if materially different), `configuration.md`,
      `discovery.md`, `extraction-and-confidence.md`, `service-tiers.md`,
      `web-ui.md`, `eu-region-model-support.md`, `canada-region-model-support.md`,
      `policy-discovery.md`, `idp-cli.md`, `cross-account-bedrock.md`.
- [ ] `docs/README.md` — index link if a new doc was added.
- [ ] `CHANGELOG.md` — `[Unreleased]` entry stating what IS and IS NOT supported
      (regions, caching, GovCloud, agentic/discovery).

### G. Tests
- [ ] `tests/unit/test_bedrock_openai_responses.py` (mantle) or the relevant
      client test — routing + region resolution per model.
- [ ] `tests/unit/config/test_validation.py` — reject cases for unsupported
      feature combos (advanced+GPT-5.x, discovery+GPT-5.x).
- [ ] `tests/unit/discovery/test_classes_discovery.py` — reject case.
- [ ] Any pricing/limits test that enumerates known models.

---

## Step 3 — Validate

```bash
cd lib/idp_common_pkg && make test-unit
make lint && make typecheck
python3 -c "import yaml,sys; [yaml.safe_load(open(f)) for f in \
  ['config_library/pricing.yaml','config_library/model_config_limits.yaml']]"
# cfn-lint the templates (enum-only changes are low risk but lint anyway)
```

Live smoke (per the e2e memory convention): create a **NEW named config
version** pointing a service at the new model — **never** swap `Config#default`.
Run one document in a region where the model is available; confirm routing and
metering. Watch the model's Lambda log group under the stack-name prefix (use
`AWS_PROFILE=default`).

---

## Common traps

- **Multiple model IDs from one "launch"** — check for variant suffixes.
- **Enum duplication** in `patterns/unified/template.yaml` (often 9 sites).
- **Vendored chat copy** drift (`chat_stream_processor/vendored/`).
- **Regex limit patterns** already matching a new variant (verify, don't blindly
  add a duplicate pattern).
- **Region drift on siblings** — AWS adds regions to existing models over time;
  the code snapshot may be stale. Refresh sibling `_MODEL_REGIONS` if you notice.
- **Caching claims** — only add to `CACHEPOINT_SUPPORTED_MODELS` if the model
  card confirms it AND the invocation path actually emits cache points. Note the
  mechanism differs by model: Bedrock Converse uses `cachePoint` blocks;
  bedrock-mantle OpenAI models split into **automatic** (GPT-5.4/5.5: any prefix
  > 1024 tokens, no request change) vs **explicit** (GPT-5.6: request needs
  `prompt_cache_options: {mode: explicit}` + `prompt_cache_key` +
  `prompt_cache_breakpoint`). `CACHEPOINT_SUPPORTED_MODELS` is Converse-path only
  — mantle models handle caching in `openai_responses.py`, not that list.
- **Pricing** — never guess; `TODO(pricing)` + flag to the user if unresolved.
- **GovCloud** — check per-variant; don't assume the family's GovCloud status.
