---
paths:
  - "config_library/**"
  - "lib/idp_common_pkg/idp_common/config/**"
  - "lib/idp_common_pkg/idp_common/bedrock/**"
---

# Configuration and model rules

Covers the configuration system (system defaults, presets, and the Default and
Custom records in the Configuration Table), selectable Bedrock models, pricing
and limits, and the Bedrock client.

## Must
- Load configuration through `idp_common.config` (`get_config`,
  `ConfigurationManager`, `IDPConfig`), so that user settings are merged over the
  system defaults.
- Add each new setting both to `config/system_defaults/base-*.yaml`, with its
  default and an inline comment, and to the Pydantic model in `config/models.py`.
  The system defaults are the canonical list of settings, and the loader ignores
  any key the model doesn't define.
- Keep presets under `config_library/unified/<preset>/config.yaml` and validate
  them before you commit.
- Follow the full add-model checklist when you add or change a selectable model.
  Take the facts from the AWS model card, region page and pricing page, and touch
  every touchpoint:
  - every enum in the `patterns/unified/template.yaml` ConfigSchema
  - `pricing.yaml`, with one block per model ID
  - `model_config_limits.yaml`
  - region filtering in `update_configuration`
  - the UI dropdowns
  - both documentation tiers and `CHANGELOG.md`
- Send Bedrock calls through `idp_common.bedrock` so retries, prompt caching and
  token metering stay in one place.
- Add a model to `CACHEPOINT_SUPPORTED_MODELS` only when its model card confirms
  caching and the invocation path actually emits cache points. Caching for
  bedrock-mantle models is handled in `openai_responses.py`.
- When a model is retired, remove it from the picklists but keep its
  `pricing.yaml` entry.
- To smoke-test a model change live, create a new named configuration version.

## Must not
- Hardcode model IDs, prompts, class labels or processing parameters in Lambda
  code.
- Read configuration directly from DynamoDB or S3 in a way that skips the merge
  with defaults.
- Guess pricing. Write `TODO(pricing)` and raise it with the user.
- Add a limits pattern that duplicates a regex that already matches.
- Swap `Config#default` to test a model.
- Describe configuration as Pattern 1/2/3. Use BDA mode and Pipeline mode
  (`use_bda`).

## Verify with
- `make test-config-library`
- `cd lib/idp_common_pkg && make test-unit`
- `scripts/tests/test_model_surface_consistency.py`
- `idp-cli config-validate --config-file <file>`, which lists ignored keys

## Sources
- [.claude/skills/add-model.md](../skills/add-model.md): the ground-truth
  checklist, file map and common traps for models
- [.claude/skills/extraction-pipeline.md](../skills/extraction-pipeline.md): the
  configuration hierarchy, schema extensions and agentic extraction settings
- [docs/configuration.md](../../docs/configuration.md): system defaults,
  unknown-key reporting, retired models and prompt caching
- [lib/idp_common_pkg/idp_common/bedrock/README.md](../../lib/idp_common_pkg/idp_common/bedrock/README.md):
  client behaviour by model family
