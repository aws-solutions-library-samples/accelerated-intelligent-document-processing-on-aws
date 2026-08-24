Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Configuration example using few-shot multimodal prompts for classification and extraction

This directory contains a comprehensive configuration example for the GenAI IDP Accelerator demonstrating few-shot example prompting for both classification and extraction tasks. This configuration showcases how to use concrete document examples with images and expected outputs to improve processing accuracy.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false) — Uses Amazon Textract for OCR, then Bedrock LLM (Nova/Claude) for classification, extraction, assessment, and summarization. Set use_bda: true for BDA mode.

## Validation Level

**Level**: Level 1 - Basic Testing

- **Testing Evidence**: Tested with provided example documents for letter and email classes
- **Known Limitations**: Limited to a few document classes with examples

## Overview

This configuration demonstrates the few-shot example prompting capability, where you can provide concrete examples of documents along with their expected classification and extraction outputs. The system uses these examples to better understand document patterns and improve accuracy.

Key features:
- **Few-shot classification examples**: Uses `x-aws-idp-class-prompt` with example images to improve document classification
- **Few-shot extraction examples**: Uses `x-aws-idp-attributes-prompt` with example images to improve attribute extraction  
- **Class-specific examples**: Extraction examples are filtered by document class for targeted learning
- **Multimodal examples**: Combines text prompts with actual document images

The configuration handles various business document types including:

- Letters (with examples)
- Forms  
- Invoices
- Resumes
- Scientific publications
- Memos
- Advertisements
- Emails (with examples)
- Questionnaires
- Specifications
- Generic documents

## Key Components

### Few-Shot Examples Structure

Examples are defined within each document class and contain:
- **name**: Descriptive name for the example
- **x-aws-idp-class-prompt**: Text used for classification few-shot prompting
- **x-aws-idp-attributes-prompt**: Text used for extraction few-shot prompting with expected attribute values
- **x-aws-idp-image-path**: Path to the example document image

Example structure:
```yaml
classes:
  - name: letter
    description: "A formal written correspondence..."
    attributes: [...]
    examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'letter'"
        name: "Letter1"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "sender_name": "Will E. Clark",
              "sender_address": "206 Maple Street P.O. Box 1056 Murray Kentucky 42071-1056",
              ...
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
```

### Classification Settings

- **Model**: Amazon Nova Pro
- **Method**: Multimodal page based classification with few-shot examples
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5
- **Few-shot support**: Uses `{FEW_SHOT_EXAMPLES}` placeholder in task_prompt

The classification task prompt includes few-shot examples from ALL classes to help the model distinguish between different document types.

### Extraction Settings

- **Model**: Amazon Nova Pro  
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5
- **Few-shot support**: Uses `{FEW_SHOT_EXAMPLES}` placeholder in task_prompt

The extraction task prompt includes few-shot examples from ONLY the specific class being extracted to provide targeted guidance for attribute extraction.

### Document Classes with Examples

Currently includes examples for:

- **letter**: 2 examples with complete sender/recipient information extraction
- **email**: 1 example with email header and addressing extraction

Other classes (form, invoice, resume, etc.) are defined but don't have examples yet.

## How Few-Shot Examples Work

### For Classification

1. When classifying a document, the system includes examples from ALL document classes
2. Each example shows what a document of that class looks like
3. Uses `x-aws-idp-class-prompt` text and document images from the examples
4. Helps the model distinguish between different document types

### For Extraction  

1. When extracting from a document of class X, the system includes examples ONLY from class X
2. Each example shows the expected attribute extraction for that document type
3. Uses `x-aws-idp-attributes-prompt` text and document images from the examples
4. Provides concrete guidance on what attributes to extract and their expected format

### Path Resolution

The system supports flexible image path resolution:

- **Local development**: Set `ROOT_DIR` environment variable to project root
- **S3 deployment**: Set `CONFIGURATION_BUCKET` environment variable
- **Direct S3 URIs**: Use full `s3://bucket/key` paths in x-aws-idp-image-path

## How to Use

### 1. Direct Use

Deploy with this configuration to see few-shot prompting in action:
```bash
# Use the few_shot_example configuration
CONFIG_BUCKET_URI="s3://your-bucket/config_library/unified/few_shot_example/"
```

### 2. Add Your Own Examples

To add examples for existing classes:

1. Create example document images and place them in the `example-images/` directory
2. Add example entries to the class definition:
   ```yaml
   examples:
     - x-aws-idp-class-prompt: "This is an example of the class 'your_class'"
       name: "YourExample1" 
       x-aws-idp-attributes-prompt: |
         expected attributes are:
             "attribute1": "example_value1",
             "attribute2": "example_value2"
       x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/your_example.jpg"
   ```

### 3. Create New Classes with Examples

1. Define a new class in the `classes` array
2. Add attributes for the class
3. Create example documents and add them to `example-images/`
4. Add example definitions with both `x-aws-idp-class-prompt` and `x-aws-idp-attributes-prompt`

## Testing the Configuration

Use the provided test notebooks to validate the few-shot functionality:

- `notebooks/test_few_shot_classification.ipynb`: Test classification with examples
- `notebooks/test_few_shot_extraction.ipynb`: Test extraction with examples

These notebooks demonstrate:
- How examples are loaded and processed
- Class-specific filtering for extraction
- Path resolution logic
- Content building with few-shot examples

## Performance Considerations

Few-shot examples provide several benefits:

- **Improved Accuracy**: Concrete examples help models understand document patterns better
- **Consistency**: Examples establish expected output formats
- **Reduced Ambiguity**: Visual examples clarify edge cases that text descriptions might miss

Trade-offs:
- **Increased Token Usage**: Examples add to prompt length and cost
- **Processing Time**: Loading and processing example images takes additional time
- **Storage Requirements**: Example images need to be stored and accessible

## Best Practices

1. **Quality Examples**: Use clear, representative examples of each document type
2. **Diverse Examples**: Include examples that cover edge cases and variations
3. **Accurate Labels**: Ensure `x-aws-idp-attributes-prompt` values are correct and consistent
4. **Image Quality**: Use high-resolution, clear images for examples
5. **Balanced Coverage**: Provide examples for your most important document classes

## Contributors

- GenAI IDP Accelerator Team
