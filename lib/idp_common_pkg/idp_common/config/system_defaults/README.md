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
   pattern-1.yaml              # BDA pattern (selective inheritance)
   pattern-2.yaml              # Bedrock LLM pattern (full inheritance)
   README.md
```

## Inheritance System

Pattern files use `_inherits` directive to declare which modules to include:

### Full Inheritance (Pattern-2)
```yaml
# Pattern-2 needs everything
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

### Selective Inheritance (Pattern-1 - BDA)
```yaml
# Pattern-1 (BDA) - excludes OCR, classification, extraction
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
extraction settings BDA does not use. Keeping both is what gives Pattern-1 the same
confidence, geometry and HITL defaults as Pattern-2.

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
2. **Pattern-specific file** - Values in pattern-X.yaml
3. **Inherited modules** - Values from base-*.yaml files
4. **Pydantic defaults** - Code-level fallbacks

## Using with CLI

```bash
# Generate minimal config template
idp-cli config-create --features min --pattern pattern-2 --output config.yaml

# Generate config with specific sections
idp-cli config-create --features ocr classification extraction --output config.yaml

# Validate config
idp-cli config-validate --custom-config ./config.yaml

# Deploy (automatically merges with defaults)
idp-cli deploy --stack-name my-idp --custom-config ./config.yaml --wait

# Download config from deployed stack
idp-cli config-download --stack-name my-stack --output config.yaml
```

## Example: Minimal User Config

Users only need to specify what differs from defaults:

```yaml
# my-config.yaml - minimal Pattern-2 config
notes: "My lending package processor"

classification:
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