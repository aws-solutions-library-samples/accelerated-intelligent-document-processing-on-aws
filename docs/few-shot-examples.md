---
title: "Few-Shot Examples in Pattern-2: Classification and Extraction"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Few-Shot Examples in Pattern-2: Classification and Extraction

## Overview

Pattern-2 supports few-shot example prompting, a powerful feature that allows you to provide concrete document examples with expected outputs to significantly improve model accuracy, consistency, and reduce hallucination for both document classification and attribute extraction tasks.

## What is Few-Shot Learning?

Few-shot learning enhances AI model performance by providing concrete examples alongside prompts. Instead of relying solely on text descriptions, the model can see actual document images paired with expected outputs, leading to better understanding of document patterns and more accurate results.

## Key Benefits

- **🎯 Improved Accuracy**: Models understand document patterns and expected formats better through concrete examples
- **📏 Consistent Output**: Examples establish exact JSON structure and formatting standards
- **🚫 Reduced Hallucination**: Examples reduce likelihood of made-up classification or attribute values
- **🔧 Domain Adaptation**: Examples help models understand domain-specific terminology and conventions
- **💡 Better Edge Case Handling**: Visual examples clarify ambiguous cases that text descriptions might miss
- **💰 Cost Effectiveness with Caching**: Using prompt caching with few-shot examples can significantly reduce costs for repeated processing

## When to Use Few-Shot Examples

Consider using few-shot examples when:
- Your document types have subtle visual differences that are hard to describe in text
- You need consistent attribute extraction formats across documents
- You're working with domain-specific documents with specialized terminology
- Standard prompting doesn't achieve the accuracy you need
- You want to reduce the amount of prompt engineering required
- You process similar documents repeatedly and can benefit from prompt caching

## Configuration Structure

Few-shot examples are configured within document class definitions using JSON Schema format in your Pattern-2 configuration:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Letter
    x-aws-idp-document-type: Letter
    type: object
    description: "A formal written correspondence..."
    properties:
      SenderName:
        type: string
        description: "The name of the person who wrote the letter..."
      SenderAddress:
        type: string
        description: "The physical address of the sender..."
    x-aws-idp-examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'Letter'"
        name: "Letter1"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "SenderName": "Will E. Clark",
              "SenderAddress": "206 Maple Street P.O. Box 1056 Murray Kentucky 42071-1056",
              "RecipientName": "The Honorable Wendell H. Ford",
              "Date": "10/31/1995",
              "Subject": null
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
      - x-aws-idp-class-prompt: "This is an example of the class 'Letter'"
        name: "Letter2"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "SenderName": "William H. W. Anderson",
              "SenderAddress": "P O. BOX 12046 CAMERON VILLAGE STATION RALEIGH N. c 27605",
              "RecipientName": "Mr. Addison Y. Yeaman",
              "Date": "10/14/1970",
              "Subject": "Invitation to the Twelfth Annual Meeting of the TGIC"
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter2.png"
```

### Example Fields Explained

Each example includes four key components:

- **`x-aws-idp-class-prompt`**: A brief description identifying this as an example of the document class (used for classification). Can include sample OCR text output to show the model what text content looks like for this class.
- **`name`**: A unique identifier for the example (for reference and debugging)
- **`x-aws-idp-attributes-prompt`**: The expected attribute extraction results in exact JSON format (used for extraction). Can include sample OCR text output to demonstrate the text from which attributes should be extracted.
- **`x-aws-idp-image-path`**: Path to example document image(s) - supports single files, local directories, or S3 prefixes (optional but recommended for better visual understanding)

> **The `x-aws-idp-*` names above are canonical; legacy camelCase is still read.**
> Earlier versions stored these three fields as `classPrompt`, `attributesPrompt`
> and `imagePath` — the container has always been `x-aws-idp-examples`, but the
> entry fields lagged behind. Both spellings behave identically wherever examples
> are consumed, so **no config needs changing**. What produces the canonical form:
> the UI's schema editor writes it (and drops the legacy alias from any example you
> edit), config migration converts it, and the shipped `config_library` presets use
> it. If an example somehow carries both, the `x-aws-idp-*` value wins.

> **Unreadable example images degrade to text-only.** If `x-aws-idp-image-path`
> cannot be read (bucket in another account/partition, object deleted, missing
> `s3:GetObject`), a warning is logged and the example is used **without** its
> image rather than failing the document. Check the Extraction/Classification
> function logs for `using example '<name>' without images` if examples look like
> they are having less effect than expected.

### Example Processing Rules

**Important**: Examples are only processed if they contain the required prompt field for the specific task:

- **For Classification**: Examples are only included if they have a non-empty `x-aws-idp-class-prompt` field
  - Examples with only `x-aws-idp-attributes-prompt` or `x-aws-idp-image-path` (but no `x-aws-idp-class-prompt`) are automatically skipped
  - Images from `x-aws-idp-image-path` are still included if the example has a valid `x-aws-idp-class-prompt`

- **For Extraction**: Examples are only included if they have a non-empty `x-aws-idp-attributes-prompt` field
  - Examples with only `x-aws-idp-class-prompt` or `x-aws-idp-image-path` (but no `x-aws-idp-attributes-prompt`) are automatically skipped
  - Images from `x-aws-idp-image-path` are still included if the example has a valid `x-aws-idp-attributes-prompt`

This ensures that examples are only used when they have the appropriate content for their respective tasks, maintaining consistency and preventing irrelevant examples from being included in the prompts.

**Example Configurations**:

```yaml
# Valid for both classification and extraction
- x-aws-idp-class-prompt: "This is an example of the class 'Invoice'"
  x-aws-idp-attributes-prompt: |
    expected attributes are:
        "InvoiceNumber": "INV-001"
  x-aws-idp-image-path: "invoice1.jpg"

# Valid only for classification (skipped during extraction)
- x-aws-idp-class-prompt: "This is an example of the class 'Invoice'"
  # No x-aws-idp-attributes-prompt - will be skipped during extraction
  x-aws-idp-image-path: "invoice2.jpg"

# Valid only for extraction (skipped during classification)
- x-aws-idp-attributes-prompt: |
    expected attributes are:
        "InvoiceNumber": "INV-002"
  # No x-aws-idp-class-prompt - will be skipped during classification
  x-aws-idp-image-path: "invoice3.jpg"

# Invalid for both (will be skipped entirely)
- name: "InvalidExample"
  # No x-aws-idp-class-prompt or x-aws-idp-attributes-prompt - will be skipped for both tasks
  x-aws-idp-image-path: "invoice4.jpg"
```

#### Enhanced Image Path Support

The `x-aws-idp-image-path` field supports multiple formats for maximum flexibility:

**Single Image File (Original functionality)**:
```yaml
x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
```

**Local Directory with Multiple Images (New)**:
```yaml
x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/"
```

**S3 Prefix with Multiple Images (New)**:
```yaml
x-aws-idp-image-path: "s3://my-config-bucket/few-shot-examples/letter/"
```

**Direct S3 Image URI**:
```yaml
x-aws-idp-image-path: "s3://my-config-bucket/few-shot-examples/letter/example1.jpg"
```

When pointing to a directory or S3 prefix, the system automatically:
- Discovers all image files with supported extensions (`.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`)
- Sorts them alphabetically by filename for consistent ordering
- Includes each image as a separate content item in the few-shot examples
- Gracefully handles individual image loading failures without breaking the entire process

#### Environment Variables for Path Resolution

The system uses these environment variables for resolving relative paths:

- **`CONFIGURATION_BUCKET`**: S3 bucket name for configuration files
  - Used when `x-aws-idp-image-path` doesn't start with `s3://`
  - The path is treated as a key within this bucket

- **`ROOT_DIR`**: Root directory for local file resolution
  - Used when `CONFIGURATION_BUCKET` is not set
  - The path is treated as relative to this directory

### Multimodal vs Text-Only Examples

Few-shot examples in Pattern-2 support both multimodal and text-only approaches:

**Multimodal Examples (Recommended)**:
- Include both `x-aws-idp-image-path` and text descriptions in prompts
- Provide visual context alongside text explanations
- Help models understand document layout and formatting
- Most effective for complex document types

**Text-Only Examples**:
- Include sample document text (OCR output) within `x-aws-idp-class-prompt` or `x-aws-idp-attributes-prompt`
- Useful when images are not available or for privacy-sensitive scenarios
- Can demonstrate text patterns and content structure
- Still provide significant accuracy improvements over no examples

**Example with OCR Text Content**:
```yaml
examples:
  - x-aws-idp-class-prompt: |
      This is an example of the class 'invoice'. 
      
      Example document text:
      INVOICE
      Invoice #: INV-2024-001
      Date: January 15, 2024
      Bill To: ACME Corp
      Total Amount: $1,250.00
      Due Date: February 15, 2024
    name: "Invoice1"
    x-aws-idp-attributes-prompt: |
      For the above invoice text, expected attributes are:
          "invoice_number": "INV-2024-001",
          "invoice_date": "January 15, 2024",
          "vendor_name": "Your Company Name",
          "customer_name": "ACME Corp", 
          "total_amount": "$1,250.00",
          "due_date": "February 15, 2024"
    # x-aws-idp-image-path is optional - can be omitted for text-only examples
    x-aws-idp-image-path: "config_library/unified/your_config/example-images/invoice1.pdf"
```

## How Few-Shot Examples Work in Pattern-2

Pattern-2 uses few-shot examples differently for classification and extraction tasks:

### Classification Process

When classifying documents:
- **Example Scope**: Uses examples from ALL document classes
- **Purpose**: Help the model distinguish between different document types
- **Content**: Uses `x-aws-idp-class-prompt` field from examples (with optional images)
- **Benefit**: Model sees visual and/or textual examples of each class to make better classification decisions
- **Filtering**: Only examples with non-empty `x-aws-idp-class-prompt` fields are included

### Extraction Process

When extracting attributes from documents:
- **Example Scope**: Uses examples ONLY from the specific document class being processed
- **Purpose**: Show the expected attribute extraction format and values
- **Content**: Uses `x-aws-idp-attributes-prompt` field from examples (with optional images)
- **Benefit**: Model sees concrete examples of what the extraction output should look like
- **Filtering**: Only examples with non-empty `x-aws-idp-attributes-prompt` fields are included

| Aspect | Classification | Extraction |
|--------|---------------|------------|
| **Example Scope** | ALL classes | Specific class only |
| **Prompt Field** | `x-aws-idp-class-prompt` | `x-aws-idp-attributes-prompt` |
| **Purpose** | Distinguish document types | Show extraction format |
| **Content** | Document type descriptions + optional OCR text | Expected JSON attribute values + optional OCR text |
| **Images** | Optional but recommended | Optional but recommended |
| **Filtering** | Requires non-empty `x-aws-idp-class-prompt` | Requires non-empty `x-aws-idp-attributes-prompt` |

## Setting Up Few-Shot Examples

### Step 1: Prepare Example Documents

1. **Select Representative Documents**: Choose 1-3 clear, representative examples for each document class
2. **Gather Content**: Collect both document images and/or sample OCR text output
3. **Cover Variations**: Include examples that demonstrate different formats or edge cases within each class

### Step 2: Create Example Images (Optional)

If using images, store your example document images in an accessible location:

```
config_library/unified/your_config/example-images/
├── letter1.jpg
├── letter2.png
├── email1.jpg
└── invoice1.pdf
```

For multiple images per example, you can organize them in directories:

```
config_library/unified/your_config/example-images/
├── letters/
│   ├── 001_formal_letter.jpg
│   ├── 002_informal_letter.png
│   └── 003_business_letter.jpg
├── invoices/
│   ├── invoice_simple.jpg
│   ├── invoice_complex.png
│   └── invoice_international.jpg
└── emails/
    ├── email_formal.jpg
    └── email_casual.png
```

### Step 3: Define Examples in Configuration

Add examples to each document class in your configuration file using JSON Schema format. You can use images, text, or both:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Invoice
    x-aws-idp-document-type: Invoice
    type: object
    description: "A commercial document requesting payment..."
    properties:
      InvoiceNumber:
        type: string
        description: "The unique invoice identifier..."
      TotalAmount:
        type: string
        description: "The total amount due..."
    x-aws-idp-examples:
      # Example with both image and OCR text content
      - x-aws-idp-class-prompt: |
          This is an example of the class 'Invoice'. 
          
          Sample invoice text content:
          ACME CORPORATION
          INVOICE #INV-2024-001
          Date: 01/15/2024
          Bill To: Tech Solutions Inc
          Amount Due: $1,250.00
        name: "Invoice1"
        x-aws-idp-attributes-prompt: |
          For an invoice like the above, expected attributes are:
              "InvoiceNumber": "INV-2024-001",
              "InvoiceDate": "01/15/2024",
              "VendorName": "ACME Corporation",
              "TotalAmount": "$1,250.00",
              "DueDate": "02/15/2024"
        x-aws-idp-image-path: "config_library/unified/your_config/example-images/invoice1.pdf"
      
      # Example with multiple images from directory
      - x-aws-idp-class-prompt: "These are examples of the class 'Invoice' showing different formats"
        name: "InvoiceVariations"
        x-aws-idp-attributes-prompt: |
          For invoices like these examples, expected attributes format:
              "InvoiceNumber": "string",
              "InvoiceDate": "MM/DD/YYYY",
              "VendorName": "string",
              "TotalAmount": "$X.XX",
              "DueDate": "MM/DD/YYYY or null"
        x-aws-idp-image-path: "config_library/unified/your_config/example-images/invoices/"
      
      # Example with text only (no image)
      - x-aws-idp-class-prompt: |
          This is another example of the class 'Invoice'.
          
          Text from a different invoice format:
          Invoice Number: 2024-0234
          Billing Date: March 10, 2024
          Customer: Small Business LLC
          Total: $875.50
        name: "Invoice2"
        x-aws-idp-attributes-prompt: |
          For this invoice format, expected attributes are:
              "InvoiceNumber": "2024-0234",
              "InvoiceDate": "March 10, 2024",
              "VendorName": "Service Provider Inc",
              "TotalAmount": "$875.50",
              "DueDate": null
        # No x-aws-idp-image-path - text-only example
```

### Step 4: Update Task Prompts with Cache Points

> **The shipped prompts already include `{FEW_SHOT_EXAMPLES}`.** The default
> classification `task_prompt` and all three default extraction prompt variants
> (`task_prompt`, `task_prompt_extraction_with_confidence`,
> `task_prompt_extraction_with_confidence_topk`) carry the placeholder ahead of
> `<<CACHEPOINT>>`, so defining examples on a class is enough for both services to
> use them — no prompt edit required. It is a no-op for classes with no examples
> (and for a service whose prompt field on those examples is empty). What *does*
> still need the placeholder added by hand: any config that replaces a shipped
> `task_prompt` with its own copy. A custom prompt without the placeholder silently
> ignores every example.

Ensure your classification and extraction task prompts include the `{FEW_SHOT_EXAMPLES}` placeholder and use `<<CACHEPOINT>>` for optimal performance. You can also use the `{DOCUMENT_IMAGE}` placeholder for precise image positioning:

**Standard Few-Shot Configuration:**
```yaml
classification:
  task_prompt: |
    Classify this document into exactly one of these categories:
    
    {CLASS_NAMES_AND_DESCRIPTIONS}
    
    <few_shot_examples>
    {FEW_SHOT_EXAMPLES}
    </few_shot_examples>
    
    <<CACHEPOINT>>
    
    <document_content>
    {DOCUMENT_TEXT}
    </document_content>

extraction:
  task_prompt: |
    Extract the following attributes from this {DOCUMENT_CLASS} document:
    
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    
    <few_shot_examples>
    {FEW_SHOT_EXAMPLES}
    </few_shot_examples>
    
    <<CACHEPOINT>>
    
    Document content:
    {DOCUMENT_TEXT}
```

**Enhanced Configuration with Image Placement:**
```yaml
classification:
  task_prompt: |
    Classify this document into exactly one of these categories:
    
    {CLASS_NAMES_AND_DESCRIPTIONS}
    
    <few_shot_examples>
    {FEW_SHOT_EXAMPLES}
    </few_shot_examples>
    
    <<CACHEPOINT>>
    
    Now examine this new document:
    {DOCUMENT_IMAGE}
    
    Document text:
    {DOCUMENT_TEXT}
    
    Classification:

extraction:
  task_prompt: |
    Extract the following attributes from this {DOCUMENT_CLASS} document:
    
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    
    <few_shot_examples>
    {FEW_SHOT_EXAMPLES}
    </few_shot_examples>
    
    <<CACHEPOINT>>
    
    Analyze this document:
    {DOCUMENT_IMAGE}
    
    Text content:
    {DOCUMENT_TEXT}
    
    Extract as JSON:
```

**Important**: The `<<CACHEPOINT>>` delimiter separates the static portion of your prompt (classes, few-shot examples) from the dynamic portion (document content). This enables Bedrock prompt caching, significantly reducing costs when processing multiple documents with the same configuration.

### Step 5: Configure Path Resolution (If Using Images)

If using images, set up environment variables for image path resolution:

**For Local Development:**
```bash
export ROOT_DIR=/path/to/your/project/root
```

**For S3 Deployment:**
```bash
export CONFIGURATION_BUCKET=your-config-bucket-name
```

## Prompt Caching with Few-Shot Examples

Using prompt caching with few-shot examples provides significant cost and performance benefits:

### How Prompt Caching Works

The `<<CACHEPOINT>>` delimiter splits your prompt into two parts:
- **Static portion** (before CACHEPOINT): Class definitions, few-shot examples, instructions
- **Dynamic portion** (after CACHEPOINT): The specific document being processed

The static portion is cached by Bedrock and reused across multiple document processing requests, while only the dynamic portion varies per document.

### Cost Benefits

With few-shot examples adding significant token count to prompts, caching becomes essential:

```yaml
# Example prompt structure for cost optimization
task_prompt: |
  You are a document classification expert.
  
  Classes: {CLASS_NAMES_AND_DESCRIPTIONS}
  
  Few-shot examples: {FEW_SHOT_EXAMPLES}
  [This section can be 1000+ tokens with multiple examples]
  
  <<CACHEPOINT>>
  
  Document to classify: {DOCUMENT_TEXT}
  [Only this section varies per document]
```

**Without caching**: Pay full token cost (static + dynamic) for every document
**With caching**: Pay full cost once, then only dynamic portion for subsequent documents

### Performance Benefits

- **Faster Processing**: Cached static content doesn't need to be reprocessed
- **Reduced Latency**: Less time spent on prompt parsing and understanding
- **Better Consistency**: Same cached context ensures consistent interpretation

### Cache Optimization Tips

1. **Group similar documents**: Process documents of the same class together to maximize cache hits
2. **Stable configurations**: Avoid changing class definitions or examples frequently during processing batches
3. **Strategic placement**: Put all static content (examples, instructions) before the CACHEPOINT
4. **Monitor cache effectiveness**: Track cost reductions to verify caching is working

## Best Practices

### Creating Quality Examples

1. **Use Clear, Representative Documents**
   - Choose documents that clearly represent each class
   - Include realistic OCR text samples that show typical content
   - For images: Ensure text is legible and images are high quality
   - Include typical variations within each document type

2. **Include Required Prompt Fields**
   ```yaml
   # Good - includes both x-aws-idp-class-prompt and x-aws-idp-attributes-prompt for full functionality
   - x-aws-idp-class-prompt: "This is an example of the class 'invoice'"
     x-aws-idp-attributes-prompt: |
       expected attributes are:
           "invoice_number": "INV-001",
           "total_amount": "$1,250.00"
     x-aws-idp-image-path: "invoice1.jpg"
   
   # Limited - only works for classification (extraction will skip this example)
   - x-aws-idp-class-prompt: "This is an example of the class 'invoice'"
     # Missing x-aws-idp-attributes-prompt - skipped during extraction
     x-aws-idp-image-path: "invoice2.jpg"
   ```

3. **Provide Complete Attribute Sets**
   ```yaml
   # Good - shows all attributes with realistic values
   x-aws-idp-attributes-prompt: |
     For the sample invoice text above, expected attributes are:
         "sender_name": "John Smith",
         "sender_address": "123 Main St, City, State 12345",
         "recipient_name": "Jane Doe",
         "date": "03/15/2024",
         "subject": "Business Proposal",
         "cc": null,
         "attachments": null
   
   # Avoid - incomplete attribute sets
   x-aws-idp-attributes-prompt: |
     expected attributes are:
         "sender_name": "John Smith"
         # Missing other important attributes
   ```

4. **Handle Null Values Explicitly**
   ```yaml
   x-aws-idp-attributes-prompt: |
     expected attributes are:
         "invoice_number": "INV-2024-001",
         "po_number": null,  # Explicitly show when fields are not present
         "discount": null,
         "tax_amount": "$125.00"
   ```

5. **Maintain Consistent Formatting**
   - Use consistent JSON structure across all examples
   - Follow the same date formats, currency formats, etc.
   - Ensure field names match your attribute definitions exactly

6. **Organize Multiple Images Effectively**
   When using directories or S3 prefixes with multiple images:

   ```yaml
   # Good: Use descriptive, ordered filenames
   x-aws-idp-image-path: "examples/letters/"
   # Contents: 001_formal_letter.jpg, 002_informal_letter.png, 003_business_letter.jpg

   # Good: Group related examples together
   x-aws-idp-image-path: "s3://config-bucket/examples/invoices/"
   # Contents: invoice_simple.jpg, invoice_complex.png, invoice_international.jpg
   ```

### Optimal Example Quantities

- **1-3 examples per class**: More examples aren't always better; focus on quality
- **Diverse coverage**: Include examples that cover different variations within each class
- **Balanced representation**: Provide examples for your most important document classes
- **Multiple images per example**: When using directories, 3-5 images per example typically provides good coverage

### Image vs Text-Only Considerations

**When to Use Images**:
- Complex layouts where visual structure matters
- Documents with tables, forms, or specific formatting
- When you have high-quality, clear document images available
- Privacy and security allow image sharing

**When to Use Text-Only**:
- Privacy-sensitive environments where images cannot be shared
- When working primarily with text content
- Simple document layouts where visual structure is less important
- When image quality is poor or unavailable

### Image Management (If Using Images)

- **File formats**: JPG, PNG, and PDF are supported
- **Resolution**: Use high-resolution images (300 DPI or higher recommended)
- **File size**: Balance quality with reasonable file sizes for processing efficiency
- **Naming**: Use descriptive names that make examples easy to identify
- **Organization**: Use directories to group related images for multi-image examples

### Cache-Friendly Prompt Design

- **Static content first**: Place all class definitions, examples, and instructions before CACHEPOINT
- **Dynamic content last**: Only document-specific content should come after CACHEPOINT
- **Consistent structure**: Keep the same prompt structure across processing sessions
- **Avoid frequent changes**: Don't modify examples or instructions during active processing

## Testing Your Configuration

Use the provided test notebooks to validate your few-shot configuration:

### Classification Testing
```python
# Test classification few-shot examples
from idp_common.classification.service import ClassificationService
import yaml

# Load your configuration
with open('path/to/your/config.yaml', 'r') as f:
    config = yaml.safe_load(f)

# Initialize service
service = ClassificationService(config=config, backend="bedrock")

# Test building examples for all classes
examples = service._build_few_shot_examples_content()
print(f"Loaded {len(examples)} example items for classification")
```

### Extraction Testing
```python
# Test extraction few-shot examples for specific class
from idp_common.extraction.service import ExtractionService

service = ExtractionService(config=config)

# Test building examples for specific class (e.g., "letter")
examples = service._build_few_shot_examples_content('letter')
print(f"Loaded {len(examples)} example items for 'letter' class extraction")
```

## Performance Considerations

### Benefits
- **Higher Accuracy**: Well-crafted examples typically improve accuracy by 10-30%
- **Reduced Prompt Engineering**: Less time spent fine-tuning text descriptions
- **Consistent Outputs**: Examples establish clear formatting standards
- **Better Edge Case Handling**: Visual and textual examples help with ambiguous documents
- **Cost Efficiency with Caching**: Prompt caching can reduce costs by 50-90% for repeated processing

### Trade-offs
- **Increased Initial Token Usage**: Examples add to prompt length, increasing initial processing costs
- **Processing Time**: Loading and including example images takes additional time (text-only examples are faster)
- **Storage Requirements**: Example images need to be stored and accessible (not applicable for text-only)
- **Configuration Complexity**: More configuration setup required

### Cost Optimization Strategies

1. **Use prompt caching**: Always include `<<CACHEPOINT>>` in prompts when using few-shot examples
2. **Strategic example selection**: Focus on document classes where accuracy improvements justify the cost
3. **Consider text-only examples**: When images aren't essential, text-only examples reduce processing time
4. **Optimize image sizes**: If using images, use the smallest size that maintains clarity
5. **Batch processing**: Process similar documents together to maximize cache hits
6. **Monitor costs**: Track token usage and cache effectiveness to optimize ROI
7. **A/B test**: Compare accuracy and costs with and without examples

### Cache Performance Monitoring

Monitor these metrics to ensure optimal cache usage:
- **Cache hit rate**: Percentage of requests using cached content
- **Cost reduction**: Compare costs with and without caching
- **Processing latency**: Time savings from cached static content
- **Token usage patterns**: Distribution of static vs. dynamic tokens

## Troubleshooting

### Common Issues

**Examples Not Loading**
- Verify `{FEW_SHOT_EXAMPLES}` placeholder exists in task prompts. The shipped
  extraction prompts include it; a **custom** extraction `task_prompt` and every
  classification prompt must add it explicitly, and exactly once (the placeholder
  is ignored if it appears more than once).
- Check that examples are defined for the document classes being processed
- Ensure examples have the required prompt fields (`x-aws-idp-class-prompt` /
  `classPrompt` for classification, `x-aws-idp-attributes-prompt` /
  `attributesPrompt` for extraction)
- For image examples: Ensure image paths are correct and files exist. An
  unreadable path logs `using example '<name>' without images` and the example is
  still sent as text.

**Examples Being Skipped**
- Verify that examples have non-empty `x-aws-idp-class-prompt` field for classification tasks
- Verify that examples have non-empty `x-aws-idp-attributes-prompt` field for extraction tasks
- Check that the prompt field contains actual content, not just whitespace
- Review the example processing rules described in this documentation

**Images Not Found (If Using Images)**
- Set `ROOT_DIR` environment variable for local development
- Set `CONFIGURATION_BUCKET` for S3 deployment scenarios
- Verify image file paths in configuration match actual file locations
- For directories: Ensure the directory contains image files with supported extensions
- For S3 prefixes: Verify S3 bucket and prefix paths are correct
- Consider using text-only examples if image access is problematic

**Caching Not Working**
- Ensure `<<CACHEPOINT>>` is properly placed in task prompts
- Verify static content is before CACHEPOINT and dynamic content is after
- Check that you're using supported Bedrock models with caching enabled

**High Costs Despite Caching**
- Verify cache hit rates are high (>80% for batch processing)
- Ensure you're not changing static content frequently
- Check that CACHEPOINT placement is optimal

**Inconsistent Results**
- Review example quality and ensure they're representative
- Check that `x-aws-idp-attributes-prompt` format matches expected output exactly
- Ensure examples cover the range of variations in your documents
- For text examples: Verify OCR text samples are realistic and accurate

**Poor Performance**
- Add more diverse examples for problematic document classes
- Improve example quality and accuracy
- Ensure examples demonstrate proper null value handling
- Consider mixing text and image examples for better coverage

## Example Configurations

### Complete Working Example with Caching

See `config_library/unified/few_shot_example/` for a complete working configuration that demonstrates:
- Letter classification and extraction with 2 examples
- Email classification and extraction with 1 example
- Proper image path configuration
- Task prompts with few-shot placeholder integration and optimal CACHEPOINT placement

### Multi-Image Example Configuration

For using multiple images per example with JSON Schema format:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Letter
    x-aws-idp-document-type: Letter
    type: object
    description: "A formal written correspondence"
    properties:
      SenderName:
        type: string
        description: "The name of the person who wrote the letter"
      RecipientName:
        type: string
        description: "The name of the person receiving the letter"
    x-aws-idp-examples:
      # Single image example
      - x-aws-idp-class-prompt: "This is an example of the class 'Letter'"
        name: "Letter1"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "SenderName": "John Smith",
              "RecipientName": "Jane Doe"
        x-aws-idp-image-path: "config_library/unified/your_config/example-images/letter1.jpg"
      
      # Multiple images from directory
      - x-aws-idp-class-prompt: "These are various examples of the class 'Letter'"
        name: "LetterVariations"
        x-aws-idp-attributes-prompt: |
          For letters like these examples, the expected format is:
              "SenderName": "string",
              "RecipientName": "string"
        x-aws-idp-image-path: "config_library/unified/your_config/example-images/letters/"
      
      # Multiple images from S3 prefix
      - x-aws-idp-class-prompt: "Additional letter examples from S3"
        name: "LetterS3Examples"
        x-aws-idp-attributes-prompt: |
          For these letter types, extract:
              "SenderName": "actual sender name",
              "RecipientName": "actual recipient name"
        x-aws-idp-image-path: "s3://my-config-bucket/examples/letters/"
```

### Text-Only Example Configuration

For environments where images cannot be used, using JSON Schema format:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Invoice
    x-aws-idp-document-type: Invoice
    type: object
    description: "A commercial document requesting payment"
    properties:
      InvoiceNumber:
        type: string
        description: "The unique invoice identifier"
      TotalAmount:
        type: string
        description: "The total amount due"
    x-aws-idp-examples:
      - x-aws-idp-class-prompt: |
          This is an example of the class 'Invoice'.
          
          Example invoice text:
          INVOICE
          Invoice No: INV-2024-001
          Date: January 15, 2024
          Bill To: Customer Name
          Amount: $1,250.00
        name: "Invoice1"
        x-aws-idp-attributes-prompt: |
          For the above invoice text, expected attributes are:
              "InvoiceNumber": "INV-2024-001",
              "InvoiceDate": "January 15, 2024",
              "VendorName": "Your Company",
              "TotalAmount": "$1,250.00"
        # No x-aws-idp-image-path - text-only example

classification:
  task_prompt: |
    Classify this document: {CLASS_NAMES_AND_DESCRIPTIONS}
    Examples: {FEW_SHOT_EXAMPLES}
    <<CACHEPOINT>>
    Document: {DOCUMENT_TEXT}

extraction:
  task_prompt: |
    Extract: {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    Examples: {FEW_SHOT_EXAMPLES}
    <<CACHEPOINT>>
    Document: {DOCUMENT_TEXT}
```

### Production-Ready Configuration

For production use with optimal caching:

```yaml
classification:
  task_prompt: |
    You are a document classification expert. Classify the document into exactly one category.
    
    Available Categories:
    {CLASS_NAMES_AND_DESCRIPTIONS}
    
    Study these examples of each category:
    {FEW_SHOT_EXAMPLES}
    
    Instructions:
    - Analyze the document layout, content, and formatting
    - Return only the category name, no explanation
    - Consider both visual structure and text content shown in examples
    
    <<CACHEPOINT>>
    
    Document to classify:
    {DOCUMENT_TEXT}

extraction:
  task_prompt: |
    You are a document extraction expert. Extract specific attributes from this {DOCUMENT_CLASS} document.
    
    Required Attributes:
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    
    Follow these examples for the expected format:
    {FEW_SHOT_EXAMPLES}
    
    Rules:
    - Return valid JSON only
    - Use null for missing values
    - Match the exact format shown in examples
    - Extract from the document text, don't invent values
    
    <<CACHEPOINT>>
    
    Document content:
    {DOCUMENT_TEXT}
```

## Future Capabilities

The few-shot examples feature continues to evolve with planned enhancements:

- **Dynamic Example Selection**: Automatically choose the most relevant examples based on document similarity
- **Quality Assessment**: Automated evaluation of example quality and recommendations for improvement
- **Confidence Integration**: Use few-shot examples to improve confidence scoring
- **Additional Formats**: Support for more example formats and metadata
- **Enhanced Caching**: More sophisticated caching strategies for different use cases
- **Hybrid Examples**: Better integration of text and image content within examples
- **Enhanced Multi-Image Support**: Advanced algorithms for optimal image selection from directories

Few-shot examples with prompt caching represent a significant step forward in making Pattern-2 more accurate, cost-effective, and easier to configure for diverse document processing scenarios. The flexibility to use images, text, or both, combined with support for multiple images per example, provides comprehensive options for different security, privacy, and performance requirements.