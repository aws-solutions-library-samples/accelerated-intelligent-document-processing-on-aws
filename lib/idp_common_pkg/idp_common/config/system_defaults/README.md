# System Defaults

This directory contains the default configuration files for the IDP Accelerator. These defaults are used as the base for all deployments, with pattern-specific and user-specific overrides applied on top.

## Modular Architecture

The defaults are organized into individual section files for maximum flexibility:

```
system_defaults/
   base.yaml                   # Composite (includes all modules)
   base-notes.yaml             # Configuration notes/description
   base-classes.yaml           # Document class definitions
   base-ocr.yaml               # Textract OCR configuration
   base-classification.yaml    # LLM classification settings
   base-extraction.yaml        # LLM extraction settings
   base-confidence.yaml        # Per-field confidence assessment + HITL thresholds
   base-geometry.yaml          # Field bounding-box geometry mode
   base-summarization.yaml     # Document summarization
   base-chat.yaml              # Chat-with-Document (interactive Q&A)
   base-evaluation.yaml        # Evaluation/testing
   base-rule-validation.yaml   # Rule validation settings
   base-agents.yaml            # Error analyzer, chat companion
   base-discovery.yaml         # Schema discovery
   base-rule-discovery.yaml    # Rule discovery from policy documents
   pattern-1.yaml              # BDA mode root (selective inheritance)
   pattern-2.yaml              # Pipeline mode root (full inheritance)
   README.md
```

`pattern-1.yaml` and `pattern-2.yaml` are the two inheritance roots, one per
processing mode: `pattern-1.yaml` is **BDA mode** (`use_bda: true`) and
`pattern-2.yaml` is **pipeline mode** (`use_bda: false`). Those filenames, the
`pattern=` keyword on `load_system_defaults()` / `merge_config_with_defaults()` /
`validate_config()`, the `VALID_PATTERNS` list in `merge_utils.py`, and the SDK's
`PATTERN_1` / `PATTERN_2` constants are all historical names for the two modes and
are the identifiers the code uses today. The stack picks the root from `use_bda`.

## Inheritance System

Pattern files use `_inherits` directive to declare which modules to include:

### Full inheritance — `pattern-2.yaml` (pipeline mode)
```yaml
# Pipeline mode needs everything
_inherits: base.yaml
```
Which is equivalent to:
```yaml
_inherits:
  - base-notes.yaml
  - base-classes.yaml
  - base-ocr.yaml
  - base-classification.yaml
  - base-extraction.yaml
  - base-confidence.yaml
  - base-geometry.yaml
  - base-summarization.yaml
  - base-chat.yaml
  - base-evaluation.yaml
  - base-rule-validation.yaml
  - base-agents.yaml
  - base-discovery.yaml
  - base-rule-discovery.yaml
```

### Selective inheritance — `pattern-1.yaml` (BDA mode)
```yaml
# BDA mode - excludes OCR, classification, extraction
_inherits:
  - base-notes.yaml
  - base-classes.yaml
  - base-confidence.yaml
  - base-geometry.yaml
  - base-summarization.yaml
  - base-evaluation.yaml
  - base-agents.yaml
  - base-discovery.yaml
```

BDA handles OCR, classification, and extraction internally, so it doesn't inherit those modules.

`base-confidence.yaml` and `base-geometry.yaml` are inherited even though `extraction`
is not: from v0.6 onward, per-field confidence lives at `extraction.confidence`,
bounding-box geometry at `extraction.geometry` and the review thresholds at top-level
`hitl`, so those two modules contribute only those sub-sections and none of the LLM
extraction settings BDA does not use. Keeping both is what gives BDA mode the same
confidence, geometry and HITL defaults as pipeline mode.

## Module Contents

| Module | Section | Description | Used By |
|--------|---------|-------------|---------|
| `base-notes.yaml` | `notes` | Configuration description | Both modes |
| `base-classes.yaml` | `classes` | Document class definitions | Both modes |
| `base-ocr.yaml` | `ocr` | Textract OCR | Pipeline mode |
| `base-classification.yaml` | `classification` | LLM classification | Pipeline mode |
| `base-extraction.yaml` | `extraction` | LLM extraction | Pipeline mode |
| `base-confidence.yaml` | `extraction.confidence`, `hitl` | Per-field confidence scoring, review thresholds | Both modes |
| `base-geometry.yaml` | `extraction.geometry` | Field bounding-box geometry | Both modes |
| `base-summarization.yaml` | `summarization` | Doc summarization | Both modes |
| `base-chat.yaml` | `chat` | Chat-with-Document | Pipeline mode |
| `base-evaluation.yaml` | `evaluation` | Testing/evaluation | Both modes |
| `base-rule-validation.yaml` | `rule_validation` | Rule validation | Pipeline mode |
| `base-agents.yaml` | `agents` | Error analyzer, chat | Both modes |
| `base-discovery.yaml` | `discovery` | Schema discovery | Both modes |
| `base-rule-discovery.yaml` | `discovery.rules` | Rule discovery from policy documents | Pipeline mode |

## Merge Priority

Configuration values are merged in this priority order (highest first):

1. **User's custom config** - Values explicitly set by the user
2. **Mode root file** - Values in `pattern-1.yaml` / `pattern-2.yaml`
3. **Inherited modules** - Values from base-*.yaml files
4. **Pydantic defaults** - Code-level fallbacks

## Using with CLI

```bash
# Generate minimal config template
idp-cli config-create --features min --output config.yaml

# Generate config with specific sections (comma-separated, as ONE argument)
idp-cli config-create --features "ocr,classification,extraction" --output config.yaml

# Validate config
idp-cli config-validate --config-file ./config.yaml

# Deploy (automatically merges with defaults)
idp-cli deploy --stack-name my-idp --custom-config ./config.yaml --wait

# Download config from deployed stack
idp-cli config-download --stack-name my-stack --output config.yaml
```

`config-create` exposes no mode selector, so it always generates from the
`pattern-2.yaml` (pipeline mode) defaults. To build a template against the
`pattern-1.yaml` (BDA mode) defaults, call `IDPClient().config.create(pattern="pattern-1")`
from the SDK, or `idp_common.config.merge_utils.generate_config_template(pattern="pattern-1")`
directly.

`generate_config_template` takes a feature-set name (`min`, `core`, `all`) or a
**list** of section names; the comma-separated string form is the SDK's
(`config.create` / `config.parse_features`), which the CLI calls. A section is a
top-level key of the pattern's defaults holding a mapping or a list
(`template_sections(pattern)`). A preset skips a section the pattern lacks, but a
list naming one, or naming anything that is not a section, raises `ValueError`
listing the valid sections. `min` writes `enabled: false` for summarization and
evaluation and copies `extraction`, so confidence scoring
(`extraction.confidence.mode`) stays at its default `separate`. A section the defaults
leave empty is not written: `classes` is `[]` in the defaults, and an uploaded
file replaces a list rather than merging it, so writing it would empty the
profile's classes; a request naming only such sections (`["classes"]`) raises
`ValueError`, since the file would hold nothing else. The header comment lists the sections the file copies,
switches off and leaves out.

## Example: Minimal User Config

Users only need to specify what differs from defaults:

```yaml
# my-config.yaml - minimal pipeline-mode config
notes: "My lending package processor"

classification:
  combined:
    llm:
      model: us.amazon.nova-lite-v1:0

extraction:
  model: us.amazon.nova-lite-v1:0

classes:
  - $id: W2
    type: object
    x-aws-idp-document-type: W2 Tax Form
    properties:
      employer_name:
        type: string
      employee_name:
        type: string
```

Everything else (OCR settings, prompts, confidence config, agents, etc.) comes from system defaults.