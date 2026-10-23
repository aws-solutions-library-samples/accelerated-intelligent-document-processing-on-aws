# Extraction Pipeline — Core Domain Logic — GenAI IDP Accelerator

## Processing Flow
```
Documents → S3 Input Bucket → EventBridge → Queue Sender Lambda
→ SQS → Queue Processor Lambda → Step Functions
→ [OCR → Classification → Extraction → Assessment → Rule Validation → Summarization]
→ S3 Output Bucket
```

## Two Processing Modes
Controlled by `use_bda` flag in configuration:

### Pipeline Mode (`use_bda: false`) — Default
1. **OCR**: Amazon Textract (or Bedrock OCR backend)
2. **Classification**: Bedrock LLM — page-level or holistic document packet classification
3. **Extraction**: Bedrock LLM — traditional or agentic mode
4. **Assessment**: LLM-powered confidence assessment with multimodal analysis
5. **Rule Validation**: Custom business rules (criteria validation)
6. **Summarization**: Document summarization

### BDA Mode (`use_bda: true`)
- End-to-end processing with Bedrock Data Automation (BDA)
- Handles packet or media documents
- Integrated OCR, classification, and extraction
- Then → Rule Validation → Summarization

## Agentic Extraction with Table Parsing
For documents with large tables (100+ rows), bank statements, transaction logs:
```yaml
extraction:
  model: "us.anthropic.claude-sonnet-5"
  agentic:
    enabled: true
    table_parsing:
      enabled: true
      max_empty_line_gap: 3          # Standard quality (0-10)
      auto_merge_adjacent_tables: true
      min_confidence_threshold: 95.0
      min_parse_success_rate: 0.90
```
- **Strands agent framework** for tool-based structured output
- **Deterministic Markdown table parser** for robust tabular data
- **Intelligent lookahead recovery** for OCR artifacts
- **Auto-merge table fragments** split by page breaks
- See `lib/idp_common_pkg/idp_common/extraction/README.md` for details

## Configuration System
### Hierarchy
1. System defaults (`idp_common/config/system_defaults/*.yaml`)
2. Configuration presets (`config_library/unified/<preset>/config.yaml`)
3. Custom overrides (via `CustomConfigPath` parameter or CLI)
4. Stored in DynamoDB Configuration Table (gzip-compressed for large configs)

### Config Structure (YAML)
```yaml
use_bda: false
notes: "Description of this configuration"

classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: DocumentType
    type: object
    x-aws-idp-document-type: DocumentType     # Custom extension
    description: >-
      Description of the document class
    properties:
      FieldName:
        type: string
        description: "Field description"
        x-aws-idp-evaluation-method: EXACT    # EXACT, NUMERIC_EXACT, FUZZY
```

### Custom JSON Schema Extensions
- `x-aws-idp-document-type` — Document class identifier
- `x-aws-idp-evaluation-method` — Evaluation metric type
- `x-aws-idp-few-shot-examples` — Few-shot example references

### 14 Configuration Presets
Located in `config_library/unified/`:
- `lending-package-sample/` (default)
- `bank-statement-sample/`
- `healthcare-multisection-package/`
- `fake-w2/`
- `ocr-benchmark/`
- And 9 more...

## Key Models and Types (`idp_common.models`)
```python
from idp_common.models import Document, Page, Section, Status

class Status(str, Enum):
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    ...

class Document(BaseModel):
    id: str
    input_bucket: str
    input_key: str
    status: Status
    pages: list[Page]
    sections: list[Section]
    ...
```

## Concurrency Management
- DynamoDB atomic counter for workflow concurrency
- `MaxConcurrent` parameter limits Step Functions executions
- Queue Processor manages back-pressure via SQS visibility timeout

## Human-in-the-Loop (HITL)
Built-in review system for human validation:
- Documents flagged by assessment module
- Review UI in the web interface
- Status transitions: IN_PROGRESS → REVIEW → APPROVED/REJECTED

## Evaluation Framework
- Baseline data in S3 Evaluation Baseline bucket
- `stickler-eval` library for accuracy metrics
- Per-field evaluation methods: EXACT, NUMERIC_EXACT, FUZZY
- Configuration versioning for A/B testing
