Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# GenAI IDP Accelerator Configuration Library

This directory serves as a centralized repository for configuration files used with the GenAI Intelligent Document Processing (IDP) Accelerator. It contains various configuration examples for different use cases, allowing users to quickly adapt the accelerator to their specific needs.

## Purpose

The Configuration Library:

- Provides ready-to-use configuration examples for common document processing scenarios
- Demonstrates best practices for configuring the GenAI IDP Accelerator
- Serves as a knowledge base of proven configurations for specific use cases
- Enables teams to share and reuse successful configurations
- Showcases advanced features like few-shot example prompting for improved accuracy

## Processing Modes

The unified architecture supports two processing modes, controlled by the `use_bda` flag:

- **Pipeline mode** (`use_bda: false`, default) — Uses Amazon Textract for OCR, then Bedrock LLM (Nova or Claude) for classification, extraction, assessment, and summarization step by step.
- **BDA mode** (`use_bda: true`) — Uses Bedrock Data Automation for end-to-end processing.

All presets default to pipeline mode. To switch to BDA mode, simply set `use_bda: true` in your configuration — the same class schemas work with both modes.

## System Defaults and Configuration Inheritance

Configurations in this library **inherit from system defaults**, meaning you only need to specify what differs from the defaults. This makes configurations:

- **Simpler** - Only specify `notes`, `classes`, and any intentional overrides
- **Maintainable** - Changes to system defaults automatically apply to all configs
- **Focused** - Easy to see what makes each configuration unique

### Minimal Configuration Example

```yaml
# All settings inherit from system defaults
use_bda: false
notes: "My document processing configuration"

classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: Invoice
    type: object
    x-aws-idp-document-type: Invoice
    description: "A billing document"
    properties:
      invoice_number:
        type: string
        description: "Unique invoice identifier"
```

### Override Example

To override a specific setting:

```yaml
notes: "Configuration with custom classification method"

# Override just the classification method - everything else uses defaults
classification:
  classificationMethod: textbasedHolisticClassification

classes:
  # ... your document classes
```

### System Default Files

System defaults are located in:
```
lib/idp_common_pkg/idp_common/config/system_defaults/
├── pattern-1.yaml    # BDA mode defaults
├── pattern-2.yaml    # Pipeline mode defaults
```

## Few-Shot Example Support

The accelerator supports few-shot example prompting to improve processing accuracy by providing concrete examples of documents and their expected outputs. This is demonstrated in the `unified/rvl-cdip-with-few-shot-examples/` configuration.

## Validation Levels

To help users understand the reliability and testing status of each configuration, we use the following validation level indicators:

- **Level 0 - Experimental**: Configuration has been created but not systematically tested
- **Level 1 - Basic Testing**: Configuration has been tested with a small set of documents
- **Level 2 - Comprehensive Testing**: Configuration has been tested with a diverse set of documents and has shown consistent performance
- **Level 3 - Production Validated**: Configuration has been used in production environments with documented performance metrics

Each configuration's README.md should include its validation level and supporting evidence.

## Directory Structure

```
config_library/
├── README.md                      # This file
├── TEMPLATE_README.md             # Template for new configuration READMEs
├── unified/                       # Configuration presets (use_bda toggles BDA vs pipeline)
│   ├── README.md
│   ├── bank-statement-sample/
│   ├── docsplit/
│   ├── healthcare-multisection-package/
│   ├── lending-package-sample/
│   ├── lending-package-sample-govcloud/
│   ├── ocr-benchmark/
│   ├── realkie-fcc-verified/
│   ├── rule-validation/
│   ├── rvl-cdip/
│   └── rvl-cdip-with-few-shot-examples/
```

Each configuration directory contains:
- `config.yaml` - The configuration file
- `README.md` - Documentation with validation level
- `samples/` - (Optional) Sample documents for testing

## Creating a New Configuration

To add a new configuration to the library:

1. **Create a new directory** with a descriptive name that reflects the use case:
   ```
   mkdir -p config_library/unified/your_use_case_name
   ```

2. **Create a minimal configuration** - Start with just `notes` and `classes`:
   ```yaml
   use_bda: false
   notes: "Description of your use case"
   
   classes:
     - $schema: https://json-schema.org/draft/2020-12/schema
       $id: YourDocType
       type: object
       x-aws-idp-document-type: YourDocType
       description: "Document description"
       properties:
         # ... your fields
   ```

3. **Add overrides only if needed** - Most configurations don't need to override defaults

4. **Create a README.md** in your use case directory using the TEMPLATE_README.md as a guide. Include:
   - Description of the use case
   - Validation level with supporting evidence
   - Key changes made to the configuration
   - Findings and results
   - Any limitations or considerations

5. **Include sample documents** in a samples/ directory to demonstrate the configuration's effectiveness

6. **Test your configuration** thoroughly before contributing

### Adding Few-Shot Examples

To add few-shot examples to your configuration:

1. **Create example images**: Collect clear, representative document images for each class
2. **Define examples**: Add `x-aws-idp-examples` to each class with:
   - `x-aws-idp-class-prompt`: Text describing the document class
   - `x-aws-idp-attributes-prompt`: Expected attribute extraction in JSON format  
   - `x-aws-idp-image-path`: Path to the example document image
   - `name`: Descriptive name for the example
3. **Update prompts**: Ensure task prompts include `{FEW_SHOT_EXAMPLES}` placeholder
4. **Test thoroughly**: Validate that examples improve accuracy

## Naming Conventions

- Use lowercase for directory names
- Use hyphens to separate words in directory names
- Choose descriptive names that reflect the use case (e.g., `lending-package-sample`, `bank-statement-sample`)
- Keep names concise but informative

## Sample Documents and the Samples Manifest

The repository's top-level `samples/` directory holds example documents (single files and batch subdirectories such as `w2/`). At publish time the build:

1. Scans `samples/` and generates `config_library/samples-manifest.json` — a machine-readable index of each sample (`id`, `name`, `description`, `s3Key`, `kind`, `fileCount`, and `configId`).
2. Uploads the curated sample binaries to the artifacts bucket and, at deploy time, copies both the manifest and the binaries into the stack's ConfigurationBucket under `samples/`.

The web UI reads the manifest to let users browse and process bundled samples directly (see [web-ui.md](../docs/web-ui.md#upload-documents)).

### Associating a sample with a configuration (`configId`)

`configId` links a sample to the `config_library/unified` preset it is designed for, so the UI can offer to import and use that configuration when the sample is launched. It is resolved at publish time (see `_SAMPLE_OVERRIDES` and `_sample_config_id` in `lib/idp_sdk/idp_sdk/_core/publish.py`) by:

1. An explicit entry in the curated overrides table, or
2. A folder-name convention — if `config_library/unified/<sample-id>/` exists, that preset is used automatically.

To associate a new sample with a config, either name the sample to match its config folder, or add an override entry. Samples with no match get `configId: null` and upload under the currently selected configuration version.

## Best Practices

### Document Classes

- Define classes with clear, specific descriptions
- Include all relevant attributes for each class
- Provide detailed descriptions for each attribute to guide extraction
- Use JSON Schema extensions like `x-aws-idp-evaluation-method` for evaluation

### Few-Shot Examples

- **Quality Examples**: Use clear, representative examples of each document type
- **Diverse Examples**: Include examples that cover edge cases and variations
- **Accurate Labels**: Ensure `x-aws-idp-attributes-prompt` values are correct and consistent
- **Image Quality**: Use high-resolution, clear images for examples
- **Balanced Coverage**: Provide examples for your most important document classes

### Prompts (Overrides Only)

- Only override prompts when the default behavior doesn't meet your needs
- Keep prompts clear and focused
- Include specific instructions for handling edge cases
- Use consistent formatting and structure
- Include `{FEW_SHOT_EXAMPLES}` placeholder where appropriate

### Model Selection

- Choose models appropriate for the complexity of your task
- Consider cost vs. performance tradeoffs
- Document the rationale for model selection

### Agentic Extraction with Table Parsing (Optional)

For documents with extensive tabular data, consider enabling agentic extraction with table parsing:

```yaml
extraction:
  model: "us.anthropic.claude-sonnet-4-20250514-v1:0"
  agentic:
    enabled: true
    table_parsing:
      enabled: true  # Deterministic Markdown table parser
      max_empty_line_gap: 3  # Tolerate OCR page breaks (0-10)
      auto_merge_adjacent_tables: true  # Merge table fragments
      min_confidence_threshold: 95.0  # OCR confidence target
      min_parse_success_rate: 0.90  # Quality threshold
      lazy_images: true  # Skip pre-loading page images when the table parse
                         # succeeds (text-driven tool; view_image on demand).
                         # Big cost saver on multi-page docs. false = always
                         # attach images (image-dependent corpora).
```

**When to use**:
- Documents with large tables (100+ rows)
- Bank statements, transaction logs, inventory lists
- Multi-page tables that may split across pages
- High accuracy requirements for tabular data

**Benefits**:
- Dramatically improves completeness (handles OCR artifacts)
- Faster and more accurate than LLM-only extraction for tables
- Reduces token costs for large tables
- Provides quality metrics and warnings

**Tuning**:
- **High-quality OCR**: `max_empty_line_gap: 2`
- **Standard quality**: `max_empty_line_gap: 3` (default)
- **Complex/noisy documents**: `max_empty_line_gap: 5-7`

See `lib/idp_common_pkg/idp_common/extraction/README.md` for detailed documentation.

### Configuration Management

- Document all significant overrides in your README
- Include version information when applicable
- Note any dependencies or requirements
- Document table parsing configuration if enabled

## Contributing

When contributing a new configuration:

1. Ensure your configuration follows the structure and naming conventions
2. Include comprehensive documentation in your README.md with validation level
3. Test your configuration with representative documents
4. Document performance metrics and findings
5. Include sample documents for demonstration and testing
6. If using few-shot examples, demonstrate the capability works correctly

By following these guidelines, we can build a valuable library of configurations that benefit the entire GenAI IDP Accelerator community.
