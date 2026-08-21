---
title: "IDP Configuration Best Practices Guide"
---

# IDP Configuration Best Practices Guide

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

## Table of Contents

### Part I: IDP Prompting Best Practices
- [Introduction](#introduction)
- [Class and Attribute Definitions](#class-and-attribute-definitions)
- [Classification Prompt Customization](#classification-prompt-customization)
- [Extraction Prompt Customization](#extraction-prompt-customization)
- [Assessment and Evaluation Prompts](#assessment-and-evaluation-prompts)
- [Summarization Prompts](#summarization-prompts)
- [Few-Shot Prompting Mastery](#few-shot-prompting-mastery)
- [Cache Checkpoint Strategy](#cache-checkpoint-strategy)
- [LLM Inference Parameters](#llm-inference-parameters)
- [Token Efficiency and Cost Optimization](#token-efficiency-and-cost-optimization)

### Part II: IDP Configuration Best Practices
- [Configuration Architecture Overview](#configuration-architecture-overview)
- [Advanced Image Processing](#advanced-image-processing)
- [Assessment and Quality Assurance](#assessment-and-quality-assurance)
- [Evaluation and Analytics](#evaluation-and-analytics)
- [Advanced Configuration Management](#advanced-configuration-management)
- [Testing and Validation](#testing-and-validation)

### Shared Resources
- [Common Patterns and Examples](#common-patterns-and-examples)

---

# Part I: IDP Prompting Best Practices

## Introduction

This guide provides comprehensive best practices for customizing both prompts and configurations in the GenAI IDP accelerator system. Effective prompting and proper configuration are critical for accurate document classification, extraction, and assessment across diverse document types and use cases.

### Key Prompt Components

The IDP accelerator configuration system manages five primary prompt types:

1. **Classification Prompts**: Categorize documents into predefined classes
2. **Extraction Prompts**: Extract structured data based on attribute definitions
3. **Assessment Prompts**: Evaluate extraction confidence and quality
4. **Evaluation Prompts**: Compare extracted data against ground truth
5. **Summarization Prompts**: Generate comprehensive document summaries

### Prompting Philosophy

Effective IDP prompting follows these core principles:
- **Specificity over Generality**: Detailed descriptions outperform generic ones
- **Evidence-Based Processing**: Always require document-based evidence
- **Structured Output**: Enforce consistent JSON/YAML formatting
- **Cost Optimization**: Strategic cache checkpoint placement
- **Multi-Modal Integration**: Leverage both visual and textual information

## Class and Attribute Definitions

### Class Definition Best Practices

Document classes serve as the foundation for both classification and extraction. Well-defined classes improve accuracy across all processing stages.

#### Clear, Distinctive Descriptions

**Good Example (from lending-package-sample):**
```yaml
classes:
  - name: Payslip
    description: >-
      An employee wage statement showing earnings, deductions, taxes, and net pay for a specific pay period, 
      typically issued by employers to document compensation details including gross pay, various tax withholdings, 
      and year-to-date totals.
```

**Why it works:**
- Specific purpose and context
- Key identifying features mentioned
- Typical use case described

**Poor Example:**
```yaml
classes:
  - name: Document
    description: A paper with text on it
```

#### Visual and Structural Characteristics

Include visual elements that help distinguish document types:

```yaml
classes:
  - name: Bank-checks
    description: >-
      A written financial instrument directing a bank to pay a specific amount of money from 
      the account holder's account to a designated payee, containing payment details, account 
      information, and verification elements.
```

### Attribute Definition Best Practices

Attributes define the structured data to extract from documents. Comprehensive attribute definitions are crucial for accurate extraction.

#### Specific Field Descriptions with Location Hints

**Good Example:**
```yaml
properties:
  YTDNetPay:
    type: string
    description: >-
      Year-to-date net pay amount representing cumulative take-home earnings after all deductions 
      from the beginning of the year to the current pay period.
    x-aws-idp-evaluation-method: NUMERIC_EXACT
```

**Enhanced Example with Location Hints:**
```yaml
properties:
  invoice_number:
    type: string
    description: >-
      The unique identifier for this invoice, typically labeled as 'Invoice #', 'Invoice Number', 
      or similar. Usually found in the upper portion of the document, often in a prominent box or header.
```

#### Attribute Types and Their Use Cases

**Simple Attributes** - Single value fields:
```yaml
properties:
  PayDate:
    type: string
    description: >-
      The actual date when the employee was paid, representing when the compensation was issued 
      or deposited.
    x-aws-idp-evaluation-method: EXACT
```

**Group Attributes** - Nested structured data:
```yaml
properties:
  CompanyAddress:
    type: object
    description: >-
      The complete business address of the employing company, including street address, 
      city, state, and postal code information.
    x-aws-idp-evaluation-method: LLM
    properties:
      State:
        type: string
        description: The state or province portion of the company's business address.
        x-aws-idp-evaluation-method: EXACT
      ZipCode:
        type: string
        description: The postal code portion of the company's business address.
        x-aws-idp-evaluation-method: EXACT
      City:
        type: string
        description: The city portion of the company's business address.
        x-aws-idp-evaluation-method: EXACT
```

**List Attributes** - Arrays of structured items:
```yaml
properties:
  FederalTaxes:
    type: array
    description: >-
      List of federal tax withholdings showing different types of federal taxes deducted, 
      with both current period and year-to-date amounts.
    x-aws-idp-evaluation-method: LLM
    x-aws-idp-list-item-description: Each item represents a specific federal tax withholding category
    items:
      type: object
      properties:
        YTD:
          type: string
          description: Year-to-date amount for this federal tax item.
          x-aws-idp-evaluation-method: NUMERIC_EXACT
        Period:
          type: string
          description: Current period amount for this federal tax item.
          x-aws-idp-evaluation-method: NUMERIC_EXACT
        ItemDescription:
          type: string
          description: Description of the specific federal tax type or category.
          x-aws-idp-evaluation-method: EXACT
```

#### Evaluation Methods Integration

Choose appropriate evaluation methods based on data type:

- **EXACT**: Precise string matching (names, IDs, codes)
- **NUMERIC_EXACT**: Numeric comparison with format normalization (amounts, quantities)
- **FUZZY**: Similarity matching with configurable thresholds (addresses, descriptions)
- **SEMANTIC**: Meaning-based comparison using embeddings
- **LLM**: AI-powered evaluation for complex comparisons

### Negative Prompting Techniques

Negative prompting is a powerful technique for improving classification and extraction accuracy when dealing with similar document types or closely related attributes. By explicitly stating what a document class or attribute is NOT, you help the model make more precise distinctions.

#### When to Use Negative Prompting

Use negative prompting in these scenarios:
- **Similar Document Types**: When documents share visual or textual similarities but serve different purposes
- **Confusing Attributes**: When multiple attributes might appear in similar locations or formats
- **Common Misclassifications**: When evaluation shows consistent confusion between specific classes
- **Domain-Specific Distinctions**: When industry knowledge is required to differentiate between options

#### Negative Prompting for Document Classes

**Example 1: Invoice vs Purchase Order**
```yaml
classes:
  - name: Invoice
    description: >-
      A billing document requesting payment for goods/services already delivered. 
      Contains terms like "Amount Due", "Payment Terms", "Invoice Number", "Remit Payment To".
      This is NOT a Purchase Order, which requests goods/services to be delivered
      and typically contains "PO Number", "Requested Delivery Date", "Ship To" address, "Please Supply".
  
  - name: Purchase-Order
    description: >-
      A request to purchase goods/services with specified quantities and delivery requirements.
      Contains "PO Number", "Ship To", "Requested Delivery Date", "Please Supply", "Order Date".
      This is NOT an Invoice, which bills for completed deliveries and contains "Amount Due", 
      "Payment Due Date", "Remit Payment To".
```

**Example 2: Medical Test Results vs Test Request Form**
```yaml
classes:
  - name: Test-Results
    description: >-
      Laboratory results showing completed test values, measurements, and diagnostic findings.
      Contains actual test values, reference ranges, "Results", "Normal/Abnormal", measurement units.
      This is NOT a Test Request Form, which orders tests to be performed
      and contains "Requested Tests", "Order Date", empty checkboxes for test selection.
  
  - name: Test-Request-Form
    description: >-
      Medical form used to order laboratory tests or diagnostic procedures.
      Contains "Requested Tests", "Order Date", checkboxes for test selection, "Physician Orders".
      This is NOT Test Results, which show completed values and measurements
      and contain actual numeric results, reference ranges, "Results" sections.
```

**Example 3: Clinical Notes vs Letter of Medical Necessity**
```yaml
classes:
  - name: Clinical-Notes
    description: >-
      Physician's documentation of patient encounter, symptoms, examination, and treatment notes.
      Free-form narrative format, progress notes, SOAP format, medical terminology.
      This is NOT a Letter of Medical Necessity, which follows formal business letter format
      with addresses, salutation ("Dear"), structured justification paragraphs, and formal closing.
  
  - name: Letter-of-Medical-Necessity
    description: >-
      Formal business letter justifying medical treatment or equipment coverage.
      Follows standard letter format with sender/recipient addresses, "Dear" salutation, 
      structured justification paragraphs, formal closing ("Sincerely").
      This is NOT Clinical Notes, which use free-form medical documentation
      and contain progress notes, SOAP format, examination findings.
```

#### Negative Prompting for Attribute Definitions

**Example 1: Employee Address vs Company Address**
```yaml
properties:
  employee_address:
    type: string
    description: >-
      The residential address of the employee receiving the payslip or benefits.
      Usually found in the "Employee Information", "Pay To", or recipient section, often indented or in a box.
      This is NOT the company address, which appears in the header/letterhead area
      and represents the employer's business location with company logos or "From" labels.
  
  company_address:
    type: string
    description: >-
      The business address of the employing company or organization.
      Typically found in the header, letterhead, or "From" section with company branding.
      This is NOT the employee address, which appears in the employee details section
      and represents the recipient's personal residence, often in a "Pay To" or "Mail To" area.
```

**Example 2: Bill To vs Ship To Address**
```yaml
properties:
  bill_to_address:
    type: string
    description: >-
      The billing address where the invoice should be sent for payment processing.
      Usually labeled "Bill To", "Billing Address", "Invoice To", or "Accounts Payable".
      This is NOT the shipping address where goods are physically delivered,
      which is labeled "Ship To", "Delivery Address", or "Service Location".

  ship_to_address:
    type: string
    description: >-
      The delivery address where goods/services are provided or shipped.
      Usually labeled "Ship To", "Delivery Address", "Service Location", or "Deliver To".
      This is NOT the billing address where invoices are sent for payment,
      which is labeled "Bill To", "Billing Address", or "Accounts Payable".
```

**Example 3: Patient Name vs Physician Name**
```yaml
properties:
  patient_name:
    type: string
    description: >-
      The full name of the patient receiving medical care, testing, or treatment.
      Usually found in patient information sections, labeled "Patient", "Patient Name", or in demographic areas.
      This is NOT the physician name, which appears in provider sections
      and may be preceded by "Dr.", "MD", found in signature areas, or labeled "Physician", "Provider".

  physician_name:
    type: string
    description: >-
      The name of the medical doctor or healthcare provider.
      Usually found in provider sections, preceded by "Dr.", "MD", or in signature areas.
      May be labeled "Physician", "Provider", "Attending", or "Ordering Physician".
      This is NOT the patient name, which appears in patient demographic sections
      and is labeled "Patient", "Patient Name", or in the main subject area of the document.
```

#### Best Practices for Negative Prompting

1. **Be Specific About Locations**
   ```yaml
   # Good - specific location hints
   description: >-
     Invoice total amount, typically in the bottom right corner or final summary section.
     This is NOT the subtotal, which appears above the tax calculations.
   
   # Poor - vague location
   description: >-
     The total amount. Not the subtotal.
   ```

2. **Use Visual and Contextual Clues**
   ```yaml
   # Good - visual and contextual cues
   description: >-
     Employee signature area, usually a handwritten signature or "Employee Signature" line.
     This is NOT the supervisor signature, which appears in approval sections
     and may be labeled "Supervisor", "Manager", or "Approved By".
   ```

3. **Highlight Key Differentiating Terms**
   ```yaml
   # Good - key terms highlighted
   description: >-
     Purchase order number for ordering goods, labeled "PO #", "Order Number", or "Purchase Order".
     This is NOT an invoice number, which relates to billing and contains terms like
     "Invoice #", "Bill Number", or appears on documents requesting payment.
   ```

4. **Balance Positive and Negative Information**
   ```yaml
   # Good - balanced approach
   description: >-
     Current period gross pay showing earnings for this specific pay cycle.
     Found in the current pay section, often in the left column of pay stubs.
     This is NOT year-to-date gross pay, which shows cumulative earnings
     and appears in YTD columns or annual summary sections.
   ```

5. **Address Common Confusion Points**
   ```yaml
   # Good - addresses known confusion
   description: >-
     Federal tax withholding for the current pay period.
     This is NOT state tax withholding, which is listed separately and may have different rates.
     This is also NOT year-to-date federal tax, which shows cumulative withholdings.
   ```

#### Implementation Guidelines

- **Start with Problem Areas**: Implement negative prompting first for classes or attributes with known accuracy issues
- **Monitor Performance**: Track whether negative prompting improves or degrades performance for specific cases  
- **Keep It Concise**: Negative descriptions should be clear but not overly lengthy
- **Test Iteratively**: Add negative prompting incrementally and measure impact on accuracy
- **Document Decisions**: Keep track of why specific negative prompts were added for future reference

## Classification Prompt Customization

### TextBasedHolisticClassification (rvl-cdip)

This approach analyzes entire document packages to identify logical document boundaries.

#### Key Components

**System Prompt Design:**
```yaml
system_prompt: >-
  You are a document classification expert who can analyze and classify multiple documents 
  and their page boundaries within a document package from various domains. Your task is to 
  determine the document type based on its content and structure, using the provided document 
  type definitions. Your output must be valid JSON according to the requested format.
```

**Task Prompt Structure:**
```yaml
task_prompt: >-
  <task-description>
  You are a document classification system. Your task is to analyze a document package 
  containing multiple pages and identify distinct document segments, classifying each 
  segment according to the predefined document types provided below.
  </task-description>

  <document-types>
  {CLASS_NAMES_AND_DESCRIPTIONS}
  </document-types>

  <document-boundary-rules>
  Rules for determining document boundaries:
  - Content continuity: Pages with continuing paragraphs, numbered sections, or ongoing narratives belong to the same document
  - Visual consistency: Similar layouts, headers, footers, and styling indicate pages belong together
  - Logical structure: Documents typically have clear beginning, middle, and end sections
  - New document indicators: Title pages, cover sheets, or significantly different subject matter signal a new document
  </document-boundary-rules>

  <<CACHEPOINT>>

  <document-text>
  {DOCUMENT_TEXT}
  </document-text>
```

**Key Features:**
- Structured XML-like tags for organization
- Clear boundary detection rules
- Cache checkpoint placement for optimization
- Specific output format requirements

### MultimodalPageLevelClassification (lending-package-sample)

This approach classifies individual pages using both visual and textual information.

#### Key Components

**System Prompt Design:**
```yaml
system_prompt: >-
  You are a multimodal document classification expert that analyzes business documents using 
  both visual layout and textual content. Your task is to classify single-page documents into 
  predefined categories based on their structural patterns, visual features, and text content. 
  Your output must be valid JSON according to the requested format.
```

**Task Prompt with Image Integration:**
```yaml
task_prompt: >-
  <task-description>
  Analyze the provided document using both its visual layout and textual content to determine 
  its document type. You must classify it into exactly one of the predefined categories.
  </task-description>

  <document-types>
  {CLASS_NAMES_AND_DESCRIPTIONS}
  </document-types>

  <classification-instructions>
  Follow these steps to classify the document:
  1. Examine the visual layout: headers, logos, formatting, structure, and visual organization
  2. Analyze the textual content: key phrases, terminology, purpose, and information type
  3. Identify distinctive features that match the document type descriptions
  4. Consider both visual and textual evidence together to determine the best match
  5. CRITICAL: Only use document types explicitly listed in the <document-types> section
  </classification-instructions>

  <<CACHEPOINT>>

  <document-ocr-data>
  {DOCUMENT_TEXT}
  </document-ocr-data>

  <document-image>
  {DOCUMENT_IMAGE}
  </document-image>
```

**Key Features:**
- Multi-modal analysis (visual + textual)
- Step-by-step classification process
- Image placement control with {DOCUMENT_IMAGE}
- Strict constraint on using only defined document types

## Extraction Prompt Customization

### System Prompt Design

The system prompt establishes the overall behavior and constraints for extraction:

```yaml
system_prompt: >-
  You are a document assistant. Respond only with JSON. Never make up data, only provide 
  data found in the document being provided.
```

**Key Principles:**
- Clear output format specification
- Prohibition against data fabrication
- Emphasis on document-based evidence

### Task Prompt Structure

**Comprehensive Example (from lending-package-sample):**
```yaml
task_prompt: >-
  <background>
  You are an expert in document analysis and information extraction. 
  You can understand and extract key information from documents classified as type {DOCUMENT_CLASS}.
  </background>

  <task>
  Your task is to take the unstructured text provided and convert it into a well-organized 
  table format using JSON. Identify the main entities, attributes, or categories mentioned 
  in the attributes list below and use them as keys in the JSON object. 
  Then, extract the relevant information from the text and populate the corresponding values 
  in the JSON object.
  </task>

  <extraction-guidelines>
  Guidelines:
      1. Ensure that the data is accurately represented and properly formatted within the JSON structure
      2. Include double quotes around all keys and values
      3. Do not make up data - only extract information explicitly found in the document
      4. Do not use /n for new lines, use a space instead
      5. If a field is not found or if unsure, return null
      6. All dates should be in MM/DD/YYYY format
      7. Do not perform calculations or summations unless totals are explicitly given
      8. If an alias is not found in the document, return null
      9. Guidelines for checkboxes:
         9.A. CAREFULLY examine each checkbox, radio button, and selection field:
            - Look for marks like ✓, ✗, x, filled circles (●), darkened areas, or handwritten checks indicating selection
            - For checkboxes and multi-select fields, ONLY INCLUDE options that show clear visual evidence of selection
            - DO NOT list options that have no visible selection mark
      10. Think step by step first and then answer.
  </extraction-guidelines>

  <attributes>
  {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
  </attributes>

  <<CACHEPOINT>>

  <document-text>
  {DOCUMENT_TEXT}
  </document-text>

  <document_image>
  {DOCUMENT_IMAGE}
  </document_image>
```

### Handling Different Data Types

**Checkboxes and Forms:**
```yaml
9.B. For ambiguous or overlapping tick marks:
   - If a mark overlaps between two or more checkboxes, determine which option contains the majority of the mark
   - Consider a checkbox selected if the mark is primarily inside the check box or over the option text
   - When a mark touches multiple options, analyze which option was most likely intended based on position and density
   - Carefully analyze visual cues and contextual hints. Think from a human perspective, anticipate natural tendencies, and apply thoughtful reasoning
```

**Date Formatting:**
```yaml
6. All dates should be in MM/DD/YYYY format
```

**Numeric Data:**
```yaml
7. Do not perform calculations or summations unless totals are explicitly given
```

### Image Placement Strategy

**Visual-First Approach:**
```yaml
task_prompt: |
  First, examine the document layout and visual structure:
  {DOCUMENT_IMAGE}
  
  Now analyze the extracted text:
  {DOCUMENT_TEXT}
  
  Extract the requested fields as JSON:
```

**Verification Approach:**
```yaml
task_prompt: |
  Document text (may contain OCR errors):
  {DOCUMENT_TEXT}
  
  Use this image to verify and correct any unclear information:
  {DOCUMENT_IMAGE}
  
  Extracted data (JSON format):
```

## Assessment and Evaluation Prompts

### Assessment Prompt Design

Assessment prompts evaluate the confidence of extraction results:

```yaml
assessment:
  system_prompt: >-
    You are a document analysis assessment expert. Your task is to evaluate the confidence 
    of extraction results by analyzing the source document evidence. Respond only with JSON 
    containing confidence scores for each extracted attribute.
  
  task_prompt: >-
    <background>
    You are an expert document analysis assessment system. Your task is to evaluate the 
    confidence of extraction results for a document of class {DOCUMENT_CLASS}.
    </background>

    <task>
    Analyze the extraction results against the source document and provide confidence 
    assessments for each extracted attribute. Consider factors such as:
    1. Text clarity and OCR quality in the source regions
    2. Alignment between extracted values and document content
    3. Presence of clear evidence supporting the extraction
    4. Potential ambiguity or uncertainty in the source material
    5. Completeness and accuracy of the extracted information
    </task>

    <assessment-guidelines>
    For each attribute, provide:
    A confidence score between 0.0 and 1.0 where:
       - 1.0 = Very high confidence, clear and unambiguous evidence
       - 0.8-0.9 = High confidence, strong evidence with minor uncertainty
       - 0.6-0.7 = Medium confidence, reasonable evidence but some ambiguity
       - 0.4-0.5 = Low confidence, weak or unclear evidence
       - 0.0-0.3 = Very low confidence, little to no supporting evidence
    </assessment-guidelines>

    <<CACHEPOINT>>

    <document-image>
    {DOCUMENT_IMAGE}
    </document-image>

    <extraction-results>
    {EXTRACTION_RESULTS}
    </extraction-results>
```

### Evaluation Prompt Design

Evaluation prompts compare extracted values against ground truth:

```yaml
evaluation:
  llm_method:
    system_prompt: >-
      You are an evaluator that helps determine if the predicted and expected values match 
      for document attribute extraction. You will consider the context and meaning rather 
      than just exact string matching.
    
    task_prompt: >-
      I need to evaluate attribute extraction for a document of class: {DOCUMENT_CLASS}.

      For the attribute named "{ATTRIBUTE_NAME}" described as "{ATTRIBUTE_DESCRIPTION}":
      - Expected value: {EXPECTED_VALUE}
      - Actual value: {ACTUAL_VALUE}

      Do these values match in meaning, taking into account formatting differences, word order, 
      abbreviations, and semantic equivalence?

      Provide your assessment as a JSON with three fields:
      - "match": boolean (true if they match, false if not)
      - "score": number between 0 and 1 representing the confidence/similarity score
      - "reason": brief explanation of your decision

      Respond ONLY with the JSON and nothing else.
```

## Summarization Prompts

### Structured Summarization

```yaml
summarization:
  system_prompt: >-
    You are a document summarization expert who can analyze and summarize documents from 
    various domains including medical, financial, legal, and general business documents. 
    Your task is to create a summary that captures the key information, main points, and 
    important details from the document. Your output must be in valid JSON format.
  
  task_prompt: >-
    <document-text>
    {DOCUMENT_TEXT}
    </document-text>

    Analyze the provided document (<document-text>) and create a comprehensive summary.

    CRITICAL INSTRUCTION: You MUST return your response as valid JSON with the EXACT structure 
    shown at the end of these instructions.

    Create a summary that captures the essential information from the document. Your summary should:
    1. Extract key information, main points, and important details
    2. Maintain the original document's organizational structure where appropriate
    3. Preserve important facts, figures, dates, and entities
    4. Reduce the length while retaining all critical information
    5. Use markdown formatting for better readability (headings, lists, emphasis, etc.)
    6. Cite all relevant facts from the source document using inline citations
    7. Format citations as markdown links that reference the full citation list
    8. Include a "References" section with exact text from the source document

    Output Format:
    You MUST return ONLY valid JSON with the following structure:
    ```json
    {
      "summary": "A comprehensive summary in markdown format with inline citations linked to a references section at the bottom"
    }
    ```
```

## Few-Shot Prompting Mastery

### What is Few-Shot Learning?

Few-shot learning enhances AI model performance by providing concrete examples alongside prompts. Instead of relying solely on text descriptions, the model can see actual document images paired with expected outputs, leading to better understanding of document patterns and more accurate results.

### Key Benefits

- **🎯 Improved Accuracy**: Models understand document patterns and expected formats better through concrete examples
- **📏 Consistent Output**: Examples establish exact JSON structure and formatting standards
- **🚫 Reduced Hallucination**: Examples reduce likelihood of made-up classification or attribute values
- **🔧 Domain Adaptation**: Examples help models understand domain-specific terminology and conventions
- **💡 Better Edge Case Handling**: Visual examples clarify ambiguous cases that text descriptions might miss
- **💰 Cost Effectiveness with Caching**: Using prompt caching with few-shot examples can significantly reduce costs for repeated processing

### Configuration Structure

Few-shot examples are configured within document class definitions using JSON Schema format:

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

- **`x-aws-idp-class-prompt`**: A brief description identifying this as an example of the document class (used for classification)
- **`name`**: A unique identifier for the example (for reference and debugging)
- **`x-aws-idp-attributes-prompt`**: The expected attribute extraction results in exact JSON format (used for extraction)
- **`x-aws-idp-image-path`**: Path to example document image(s) - supports single files, local directories, or S3 prefixes

### Example Processing Rules

**Important**: Examples are only processed if they contain the required prompt field for the specific task:

- **For Classification**: Examples are only included if they have a non-empty `x-aws-idp-class-prompt` field
- **For Extraction**: Examples are only included if they have a non-empty `x-aws-idp-attributes-prompt` field

### Enhanced Image Path Support

The `x-aws-idp-image-path` field supports multiple formats:

**Single Image File:**
```yaml
x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
```

**Local Directory with Multiple Images:**
```yaml
x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/"
```

**S3 Prefix with Multiple Images:**
```yaml
x-aws-idp-image-path: "s3://my-config-bucket/few-shot-examples/letter/"
```

**Direct S3 Image URI:**
```yaml
x-aws-idp-image-path: "s3://my-config-bucket/few-shot-examples/letter/example1.jpg"
```

### Integration with Template Prompts

Few-shot examples are automatically integrated using the `{FEW_SHOT_EXAMPLES}` placeholder:

**Classification with Few-Shot Examples:**
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
```

**Extraction with Few-Shot Examples:**
```yaml
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

### Best Practices for Few-Shot Examples

1. **Use Clear, Representative Documents**
   - Choose documents that clearly represent each class
   - Include realistic content that shows typical variations
   - Ensure examples have the required prompt fields

2. **Provide Complete Attribute Sets**
   ```yaml
   # Good - shows all attributes with realistic values
   x-aws-idp-attributes-prompt: |
     For the sample document above, expected attributes are:
         "sender_name": "John Smith",
         "sender_address": "123 Main St, City, State 12345",
         "recipient_name": "Jane Doe",
         "date": "03/15/2024",
         "subject": "Business Proposal",
         "cc": null,
         "attachments": null
   ```

3. **Handle Null Values Explicitly**
   ```yaml
   x-aws-idp-attributes-prompt: |
     expected attributes are:
         "invoice_number": "INV-2024-001",
         "po_number": null,  # Explicitly show when fields are not present
         "discount": null,
         "tax_amount": "$125.00"
   ```

4. **Leverage Prompt Caching**
   - Always include `<<CACHEPOINT>>` to separate static examples from dynamic content
   - Place all examples before the cache point for maximum cost savings

## Cache Checkpoint Strategy

### Optimal Placement

Cache checkpoints should separate static content from dynamic content:

**Static Content (Cacheable):**
- System instructions
- Class definitions
- Few-shot examples
- Attribute descriptions
- Processing guidelines

**Dynamic Content (Not Cacheable):**
- Document text
- Document images
- Specific extraction results

### Example Implementation

```yaml
task_prompt: >-
  <background>
  You are an expert in business document analysis and information extraction.
  </background>
  
  <class-definitions>
  {CLASS_NAMES_AND_DESCRIPTIONS}
  </class-definitions>
  
  <extraction-guidelines>
  [Static guidelines that don't change per document]
  </extraction-guidelines>
  
  <<CACHEPOINT>>
  
  <document-text>
  {DOCUMENT_TEXT}
  </document-text>
  
  <document-image>
  {DOCUMENT_IMAGE}
  </document-image>
```

### Cost Benefits

For models supporting cache checkpoints:
- **Initial Request**: Full token cost
- **Subsequent Requests**: Cache read cost (typically 10x cheaper) + new content cost
- **Typical Savings**: 60-90% cost reduction for repeated processing

## LLM Inference Parameters

### Temperature Settings

**Classification (Deterministic):**
```yaml
temperature: 0.0  # Consistent classification results
```

**Extraction (Deterministic):**
```yaml
temperature: 0.0  # Consistent data extraction
```

**Assessment (Deterministic):**
```yaml
temperature: 0.0  # Consistent confidence scoring
```

**Summarization (Slightly Creative):**
```yaml
temperature: 0.0  # Still deterministic for consistent summaries
```

### Top-p and Top-k Configuration

**Balanced Configuration:**
```yaml
top_p: 0.1    # Focus on most likely tokens
top_k: 5      # Consider top 5 candidates
```

**Conservative Configuration:**
```yaml
top_p: 0.05   # More focused selection
top_k: 3      # Fewer candidates
```

### Max Tokens Sizing

`max_tokens` is an **optional cap** on model output. Leave it empty (the
default everywhere) and the service uses the selected model's maximum output
limit — resolved at request time from the model limits list (editable in the
web UI under **View / Edit Model Limits**). This avoids silent truncation when
you switch to a larger-context model. Set a positive value only when you
deliberately want to cap output *below* the model maximum (e.g. to bound cost
or latency on a task with short responses).

```yaml
# Recommended: leave empty to use the model's full output budget
max_tokens: ""

# Or set an explicit cap (must not exceed the model's limit)
max_tokens: 4096
```

Extraction and the confidence pass have no `max_tokens` knob at all — they
always request the model's maximum output so long lists and large documents are
never truncated.

## Token Efficiency and Cost Optimization

### JSON vs YAML Output Support

The IDP services support both JSON and YAML output formats from LLM responses, with automatic format detection and parsing.

#### Automatic Format Detection

The system automatically detects whether the LLM response is in JSON or YAML format:

```yaml
# JSON response (traditional)
extraction:
  task_prompt: |
    Extract the following fields and respond with JSON:
    {
      "invoice_number": "extracted value",
      "total_amount": "extracted value"
    }

# YAML response (more token-efficient)
extraction:
  task_prompt: |
    Extract the following fields and respond with YAML:
    invoice_number: extracted value
    total_amount: extracted value
```

#### Token Efficiency Benefits

YAML format provides significant token savings for all processing tasks:

- **10-30% fewer tokens** than equivalent JSON
- No quotes required around keys
- More compact syntax for nested structures
- Natural support for multiline content
- Cleaner representation of complex extracted data

#### Example Prompt Configurations

**JSON-focused extraction prompt:**
```yaml
extraction:
  system_prompt: |
    You are a document assistant. Respond only with JSON. Never make up data, only provide data found in the document being provided.
  task_prompt: |
    Extract the following fields from this {DOCUMENT_CLASS} document and return a JSON object:
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    Document text: {DOCUMENT_TEXT}
    JSON response:
```

**YAML-focused extraction prompt (more efficient):**
```yaml
extraction:
  system_prompt: |
    You are a document assistant. Respond only with YAML. Never make up data, only provide data found in the document being provided.
  task_prompt: |
    Extract the following fields from this {DOCUMENT_CLASS} document and return YAML:
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    Document text: {DOCUMENT_TEXT}
    YAML response:
```

#### Token Efficiency Example

For a typical invoice extraction with 10 fields:

**JSON format (traditional):**
```json
{"invoice_number": "INV-2024-001", "invoice_date": "2024-03-15", "vendor_name": "ACME Corp", "total_amount": "1,234.56", "tax_amount": "123.45", "subtotal": "1,111.11", "due_date": "2024-04-15", "payment_terms": "Net 30", "customer_name": "John Smith", "customer_address": "456 Oak Ave, City, State 67890"}
```

**YAML format (more efficient):**
```yaml
invoice_number: INV-2024-001
invoice_date: 2024-03-15
vendor_name: ACME Corp
total_amount: 1,234.56
tax_amount: 123.45
subtotal: 1,111.11
due_date: 2024-04-15
payment_terms: Net 30
customer_name: John Smith
customer_address: 456 Oak Ave, City, State 67890
```

The YAML version uses approximately 25% fewer tokens while maintaining the same information content.

### OCR Confidence Data Integration

The assessment feature implements several cost optimization techniques:

1. **Text Confidence Data**: Uses condensed OCR confidence information instead of full raw OCR results (80-90% token reduction)
2. **Conditional Image Processing**: Images only processed when `{DOCUMENT_IMAGE}` placeholder is present
3. **Efficient Prompting**: Optimized prompt templates minimize token usage while maintaining accuracy

---

# Part II: IDP Configuration Best Practices

## Configuration Architecture Overview

The IDP accelerator supports two primary processing patterns, each with distinct configuration optimization strategies:

### Pattern Comparison

| Aspect | Holistic Classification | Page-Level Classification |
|--------|------------------------|---------------------------|
| **Primary Use Case** | Multi-document packages | Single-page documents |
| **Input Data** | OCR text + document images | Document images only |
| **Processing Method** | Document boundary detection | Independent page analysis |
| **Example Config** | `rvl-cdip` | `lending-package-sample` |
| **Configuration Complexity** | Higher (boundary rules) | Lower (direct classification) |
| **Output Format** | Segmented page ranges | Single classification |

### Configuration Structure

Each configuration contains these essential components:

```yaml
# Core Processing Configuration
ocr: [OCR method and parameters]
classes: [Document type definitions]
classification: [Classification prompts and parameters]
extraction: [Extraction prompts and parameters]
assessment: [Assessment prompts and parameters]
evaluation: [Evaluation prompts and parameters]
summarization: [Summarization prompts and parameters]
pricing: [Cost calculation parameters]
```

## Advanced Image Processing

### {DOCUMENT_IMAGE} Placeholder Control

The extraction and classification services support precise control over where document images are positioned within prompts using the `{DOCUMENT_IMAGE}` placeholder.

#### How {DOCUMENT_IMAGE} Works

**Without Placeholder (Default Behavior):**
```yaml
task_prompt: |
  Extract the following fields from this {DOCUMENT_CLASS} document:
  {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
  
  Document text:
  {DOCUMENT_TEXT}
  
  Respond with valid JSON.
```
Images are automatically appended after the text content.

**With Placeholder (Controlled Placement):**
```yaml
task_prompt: |
  Extract the following fields from this {DOCUMENT_CLASS} document:
  {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
  
  Examine this document image:
  {DOCUMENT_IMAGE}
  
  Text content:
  {DOCUMENT_TEXT}
  
  Respond with valid JSON containing the extracted values.
```
Images are inserted exactly where `{DOCUMENT_IMAGE}` appears in the prompt.

#### Usage Examples

**Visual-First Processing:**
```yaml
task_prompt: |
  You are extracting data from a {DOCUMENT_CLASS}. Here are the fields to find:
  {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
  
  First, examine the document layout and visual structure:
  {DOCUMENT_IMAGE}
  
  Now analyze the extracted text:
  {DOCUMENT_TEXT}
  
  Extract the requested fields as JSON:
```

**Image for Context and Verification:**
```yaml
task_prompt: |
  Extract these fields from a {DOCUMENT_CLASS}:
  {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
  
  Document text (may contain OCR errors):
  {DOCUMENT_TEXT}
  
  Use this image to verify and correct any unclear information:
  {DOCUMENT_IMAGE}
  
  Extracted data (JSON format):
```

### Image Processing Configuration

The services support configurable image dimensions for optimal performance:

#### New Default Behavior (Preserves Original Resolution)

**Important Change**: Empty strings or unspecified image dimensions now preserve the original document resolution for maximum processing accuracy:

```yaml
classification:
  model: us.amazon.nova-pro-v1:0
  # Image processing settings - preserves original resolution
  image:
    target_width: ""     # Empty string = no resizing (recommended)
    target_height: ""    # Empty string = no resizing (recommended)
```

#### Custom Image Dimensions

Configure specific dimensions when performance optimization is needed:

```yaml
# For high-accuracy processing with controlled dimensions
classification:
  image:
    target_width: "1200"   # Resize to 1200 pixels wide
    target_height: "1600"  # Resize to 1600 pixels tall

# For fast processing with lower resolution
classification:
  image:
    target_width: "600"    # Smaller for faster processing
    target_height: "800"   # Maintains reasonable quality
```

#### Image Resizing Features

- **Original Resolution Preservation**: Empty strings preserve full document resolution for maximum accuracy
- **Aspect Ratio Preservation**: Images are resized proportionally without distortion when dimensions are specified
- **Smart Scaling**: Only downsizes images when necessary (scale factor < 1.0)
- **High-Quality Resampling**: Better visual quality after resizing
- **Performance Optimization**: Configurable dimensions allow balancing accuracy vs. speed

### Multi-Page Document Handling

For documents with multiple pages, the system provides comprehensive image support:

- **Automatic Pagination**: Images are processed in page order
- **No Image Limits**: All document pages are processed following Bedrock API removal of image count restrictions
- **Info Logging**: System logs image counts for monitoring purposes
- **Comprehensive Processing**: Documents of any length are fully processed

### Best Practices for Image Processing

1. **Use Empty Strings for High Accuracy**: For critical document processing, use empty strings to preserve original resolution
2. **Consider Document Types**: Complex layouts benefit from higher resolution, simple text documents may work well with smaller dimensions
3. **Test Performance Impact**: Higher resolution images provide better accuracy but consume more resources
4. **Monitor Processing Time**: Balance processing accuracy with processing speed based on your requirements
5. **Strategic Image Placement**: Position images where they provide maximum context for the specific task

## Confidence (Assessment) and Quality Assurance

### Overview

As of **config v0.6**, per-field **confidence** and **geometry** are **outputs of
extraction**, configured under `extraction.confidence.*` and
`extraction.geometry.*` (there is no top-level `assessment.{model, geometry_mode,
...}` block anymore). Confidence assessment analyzes extraction outputs against
source documents to provide confidence scores and explanations for each extracted
attribute. See [Extraction & Confidence](./extraction-and-confidence.md) for the
full reference.

### Key Configuration Features

- **Multimodal Analysis**: Combines text analysis with document images for comprehensive confidence assessment
- **Per-Attribute Scoring**: Provides individual confidence scores and explanations for each extracted attribute
- **Token-Optimized Processing**: Uses condensed text confidence data for 80-90% token reduction compared to full OCR results
- **UI Integration**: Seamlessly displays confidence results in the web interface with explainability information
- **Confidence Threshold Support**: Configurable global and per-attribute confidence thresholds with color-coded visual indicators
- **Runtime Control**: Enabled/disabled via configuration (`extraction.confidence.enabled` / `mode`), no stack redeployment required
- **Large-list batching**: Long list fields (hundreds of rows) are scored in sequential batches so every row is covered

### Confidence modes

`extraction.confidence.mode` controls *where* per-field confidence is produced:

- **`separate`** *(default)* — on the Simple (non-agentic) path, confidence runs
  as the standalone Assessment step; on the Advanced (agentic) path, it runs
  inside each extraction shard and the standalone step auto-skips.
- **`integrated`** — a single extraction inference returns values **and** inline
  confidence together (works on **both** the simple and agentic paths); the
  service splits the envelope, enriches with thresholds, and emits
  `explainability_info`, so the standalone Assessment step auto-skips.
- **`off`** — no confidence scoring (equivalent to `enabled: false`); zero LLM cost.

```yaml
extraction:
  confidence:
    enabled: true             # false disables confidence entirely (zero LLM cost)
    mode: separate            # off | separate (default) | integrated
    model: "us.amazon.nova-lite-v1:0"
    temperature: 0
    list_batch_size: 25       # rows per assessment batch for large lists
    image:
      target_width: ""        # Empty string = no resizing (recommended)
      target_height: ""       # Empty string = no resizing (recommended)
```

### Large lists (`list_batch_size`)

For complex documents with large lists (bank statements with hundreds of
transactions, line-item tables, brokerage holdings), the standalone Assessment
step **batches large lists automatically**: it slices the largest list field into
`extraction.confidence.list_batch_size` chunks (default **25**), assesses each
chunk sequentially with the shared scalars/context, then reconciles the results
so **every** list cell gets its own confidence and bounding box. A bounded
missing-row retry re-scores any rows the model dropped, so coverage reaches 100%.
Lower `list_batch_size` if a model still struggles to enumerate a full chunk;
raise it to reduce the number of inference calls.

> **Granular assessment is retired.** The former "granular assessment" service
> (parallel thread-pool fan-out with DynamoDB caching, formerly configured under
> `assessment.granular` / `extraction.confidence.granular`) has been **retired and
> deleted**. Large-list batching is its full replacement, and `list_batch_size` is
> the one knob. Any leftover `granular.*` keys still validate but are ignored — no
> config edit required. See
> [Granular Assessment Retirement](./migration-granular-retirement.md).

### UI Integration Configuration

Confidence results automatically appear in the web interface with color-coded displays:

- 🟢 **Green**: Confidence meets or exceeds threshold (high confidence)
- 🔴 **Red**: Confidence falls below threshold (requires review)
- ⚫ **Black**: Confidence available but no threshold for comparison

### Best Practices for Confidence Configuration

1. **Enable Selectively**: Disable confidence (`enabled: false` or `mode: off`) for non-critical document types to control costs
2. **Use Advanced (agentic) for Complex Documents**: For very large documents and big tables, prefer agentic extraction — it shards both extraction and confidence assessment and yields the best-calibrated confidence
3. **Tune `list_batch_size` for Large Lists**: Lower it if a chunk under-enumerates; raise it to cut inference count
4. **Configure Appropriate Image Dimensions**: Use original resolution for maximum accuracy
5. **Monitor Resource Usage**: Track processing time and costs when using confidence features

## Evaluation and Analytics

### Overview

The GenAIIDP solution includes a comprehensive evaluation framework configuration to assess the accuracy of document processing outputs by comparing them against baseline (ground truth) data.

### Evaluation Configuration Parameters

Set the following parameters during stack deployment:

```yaml
EvaluationBaselineBucketName:
  Description: Existing bucket with baseline data, or leave empty to create new bucket
```

**Note:** Evaluation is now controlled via configuration file (`evaluation.enabled: true/false`) rather than stack parameters. See the [evaluation.md](./evaluation.md) documentation for details.

### Evaluation Methods Configuration

Configure evaluation methods for specific document classes and attributes using JSON Schema format:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Invoice
    x-aws-idp-document-type: Invoice
    type: object
    description: A commercial invoice
    properties:
      InvoiceNumber:
        type: string
        description: The unique identifier for the invoice
        x-aws-idp-evaluation-method: EXACT  # Use exact string matching
      AmountDue:
        type: string
        description: The total amount to be paid
        x-aws-idp-evaluation-method: NUMERIC_EXACT  # Use numeric comparison
      VendorName:
        type: string
        description: Name of the vendor
        x-aws-idp-evaluation-method: FUZZY  # Use fuzzy matching
        x-aws-idp-confidence-threshold: 0.8  # Minimum similarity threshold
```

### Supported Evaluation Methods

The framework supports multiple comparison methods:

- **Exact Match (EXACT)**: Compares values character-by-character after normalizing whitespace and punctuation
- **Numeric Exact Match (NUMERIC_EXACT)**: Compares numeric values after normalizing formats
- **Fuzzy Match (FUZZY)**: Allows for minor variations in formatting with configurable similarity thresholds
- **Semantic Match (SEMANTIC)**: Evaluates meaning equivalence using embedding-based similarity
- **List Matching (HUNGARIAN)**: Uses the Hungarian algorithm for optimal bipartite matching of lists
- **LLM-Powered Analysis (LLM)**: Uses AI to determine functional equivalence with detailed explanations

### Baseline Data Configuration

#### Baseline Bucket Structure Configuration
```
baseline-bucket/
├── document1.pdf.json    # Baseline for document1.pdf
├── document2.pdf.json    # Baseline for document2.pdf
└── subfolder/
    └── document3.pdf.json  # Baseline for subfolder/document3.pdf
```

### Aggregate Evaluation Analytics Configuration

The solution includes comprehensive analytics through a structured database:

#### ReportingDatabase Configuration

The evaluation framework automatically saves detailed metrics to an AWS Glue database:

1. **document_evaluations**: Document-level metrics configuration
2. **section_evaluations**: Section-level metrics configuration
3. **attribute_evaluations**: Detailed attribute-level metrics configuration

#### Data Retention Configuration

```yaml
DataRetentionInDays:
  Type: Number
  Default: 90
  Description: Number of days to retain evaluation data
```

### Best Practices for Evaluation Configuration

1. **Enable auto-evaluation during testing/tuning phases**
2. **Disable auto-evaluation in production for cost efficiency**
3. **Configure appropriate evaluation methods for each attribute type**
4. **Set up baseline bucket structure properly**
5. **Configure data retention policies based on compliance requirements**

## Advanced Configuration Management

### Bedrock OCR Configuration

Pattern 2 supports Amazon Bedrock LLMs (Claude, Nova) as an alternative OCR backend alongside Amazon Textract:

```yaml
ocr:
  backend: "bedrock"  # Options: "textract", "bedrock", "none"
  model_id: "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
  system_prompt: "You are an expert OCR system. Extract all text from the provided image accurately, preserving layout where possible."
  task_prompt: "Extract all text from this document image. Preserve the layout, including paragraphs, tables, and formatting."
  
  # Image processing configuration for OCR
  image:
    target_width: ""     # Empty string = no resizing (recommended)
    target_height: ""    # Empty string = no resizing (recommended)
    preprocessing: true  # Enable adaptive binarization
```

#### Supported Vision-Capable Models

Configure from these supported models:

- `us.amazon.nova-lite-v1:0`
- `us.amazon.nova-pro-v1:0`
- `us.amazon.nova-premier-v1:0`
- `us.amazon.nova-2-lite-v1:0`
- `us.anthropic.claude-3-haiku-20240307-v1:0`
- `us.anthropic.claude-haiku-4-5-20251001-v1:0`
- `us.anthropic.claude-3-5-sonnet-20241022-v2:0`
- `us.anthropic.claude-3-7-sonnet-20250219-v1:0`
- `us.anthropic.claude-sonnet-4-20250514-v1:0`
- `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
- `us.anthropic.claude-sonnet-4-6`
- `us.anthropic.claude-sonnet-4-6:1m`
- `us.anthropic.claude-opus-4-20250514-v1:0`
- `us.anthropic.claude-opus-4-1-20250805-v1:0`
- `us.anthropic.claude-opus-4-5-20251101-v1:0`
- `us.anthropic.claude-opus-4-6-v1`
- `us.anthropic.claude-opus-4-6-v1:1m`
- `us.anthropic.claude-opus-4-7`
- `us.anthropic.claude-opus-4-7:1m`
- `us.anthropic.claude-opus-4-8`
- `us.anthropic.claude-opus-4-8:1m`
- `us.anthropic.claude-opus-5`
- `us.anthropic.claude-opus-5:1m`
- `eu.amazon.nova-lite-v1:0`
- `eu.amazon.nova-pro-v1:0`
- `eu.amazon.nova-2-lite-v1:0`
- `eu.anthropic.claude-3-haiku-20240307-v1:0`
- `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
- `eu.anthropic.claude-3-5-sonnet-20241022-v2:0`
- `eu.anthropic.claude-3-7-sonnet-20250219-v1:0`
- `eu.anthropic.claude-sonnet-4-20250514-v1:0`
- `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`
- `eu.anthropic.claude-sonnet-4-6`
- `eu.anthropic.claude-sonnet-4-6:1m`
- `eu.anthropic.claude-opus-4-5-20251101-v1:0`
- `eu.anthropic.claude-opus-4-6-v1`
- `eu.anthropic.claude-opus-4-6-v1:1m`
- `eu.anthropic.claude-opus-4-7`
- `eu.anthropic.claude-opus-4-7:1m`
- `eu.anthropic.claude-opus-4-8`
- `eu.anthropic.claude-opus-4-8:1m`
- `eu.anthropic.claude-opus-5`
- `eu.anthropic.claude-opus-5:1m`
- `qwen.qwen3-vl-235b-a22b`
- `global.amazon.nova-2-lite-v1:0`
- `global.anthropic.claude-haiku-4-5-20251001-v1:0`
- `global.anthropic.claude-sonnet-4-5-20250929-v1:0`
- `global.anthropic.claude-sonnet-4-6`
- `global.anthropic.claude-sonnet-4-6:1m`
- `global.anthropic.claude-opus-4-5-20251101-v1:0`
- `global.anthropic.claude-opus-4-6-v1`
- `global.anthropic.claude-opus-4-6-v1:1m`
- `global.anthropic.claude-opus-4-7`
- `global.anthropic.claude-opus-4-7:1m`
- `global.anthropic.claude-opus-4-8`
- `global.anthropic.claude-opus-4-8:1m`
- `global.anthropic.claude-opus-5`
- `global.anthropic.claude-opus-5:1m`

#### When to Configure Bedrock OCR

Configure Bedrock OCR for:
- **Complex layouts** or mixed content types
- **Handwritten or low-quality documents** where Textract struggles
- **Domain-specific documents** requiring contextual understanding
- **Unified processing** across the entire pipeline
- **Experimental or specialized use cases** requiring prompt customization

### Configuration Presets

The IDP accelerator supports multiple configuration presets for different use cases:

- **Default**: Standard processing configuration
- **few_shot_example**: Enhanced with few-shot learning examples
- **medical_records_summarization**: Specialized for medical document processing
- **checkboxed_attributes_extraction**: Optimized for form processing

### Dynamic Configuration Updates

Configuration management features:

- **Web UI Configuration**: Update configurations through the web interface without stack redeployment
- **Configuration Library**: Organized preset configurations for different document types
- **Runtime Updates**: Changes take effect immediately without code deployment
- **Version Control**: Configuration versioning for rollback capabilities

### Best Practices for Configuration Management

1. **Use Configuration Library**: Leverage pre-built configurations for common use cases
2. **Test Configuration Changes**: Thoroughly validate changes before production deployment
3. **Monitor Performance**: Track metrics after configuration updates
4. **Version Control**: Maintain configuration versions for rollback capabilities
5. **Environment-Specific Configs**: Use different configurations for development and production
6. **OCR Backend Selection**: Choose appropriate OCR backend based on document types and requirements

## Testing and Validation

### Configuration Testing Strategy

1. **Start with Basic Configurations**
   - Simple, clear settings
   - Minimal complexity
   - Test with sample documents

2. **Add Complexity Gradually**
   - Include advanced image processing
   - Add assessment configurations
   - Handle edge cases

3. **Incorporate Advanced Features**
   - Add few-shot examples
   - Tune confidence (`list_batch_size` for large lists; consider Advanced agentic extraction)
   - Test multi-modal understanding

4. **Optimize for Performance**
   - Configure image dimensions
   - Set appropriate inference parameters
   - Balance accuracy vs cost

### Performance Monitoring Configuration

**Key Metrics to Configure:**
- Classification accuracy thresholds
- Extraction completeness targets
- Confidence score distributions
- Token usage limits
- Processing latency thresholds

**Validation Configuration:**
- Test with representative document sets
- Configure baseline comparison thresholds
- Set up failure pattern monitoring
- Configure iteration feedback loops

### Common Configuration Pitfalls and Solutions

**Pitfall: Incorrect Image Dimensions**
```yaml
# Poor - fixed small dimensions
image:
  target_width: "300"
  target_height: "400"

# Better - preserve original resolution
image:
  target_width: ""
  target_height: ""
```

**Pitfall: Missing OCR Configuration**
```yaml
# Poor - no OCR backend specified
ocr:
  # Missing backend configuration

# Better - explicit OCR backend
ocr:
  backend: "textract"  # or "bedrock" based on requirements
```

**Pitfall: Inappropriate Assessment Configuration**
```yaml
# Poor - assessment enabled for all documents
assessment:
  # No selective configuration

# Better - selective assessment
assessment:
  # Only enable for critical document types
  enabled_for_classes: ["invoice", "bank_statement"]
```

---

# Shared Resources

## Common Patterns and Examples

### Standard Document Classes

**Financial Documents:**
```yaml
classes:
  - name: Payslip
    description: "Employee wage statement with earnings, deductions, and tax information"
  - name: Bank-Statement
    description: "Periodic account activity summary with transactions and balances"
  - name: W2
    description: "Annual tax document with wage and withholding information"
```

**Identification Documents:**
```yaml
classes:
  - name: US-drivers-licenses
    description: "Government-issued driving authorization with personal details and restrictions"
  - name: Bank-checks
    description: "Financial instrument for directing payment from bank account"
```

**Business Documents:**
```yaml
classes:
  - name: Homeowners-Insurance-Application
    description: "Application for property insurance with coverage details and applicant information"
```

### Attribute Patterns

**Simple Attributes:**
```yaml
properties:
  date_field:
    type: string
    description: "Specific date with clear location hint and format requirement"
    x-aws-idp-evaluation-method: EXACT
```

**Complex Nested Structures:**
```yaml
properties:
  address_group:
    type: object
    properties:
      street:
        type: string
      city:
        type: string
      state:
        type: string
      zip_code:
        type: string
```

**Dynamic Lists:**
```yaml
properties:
  transaction_list:
    type: array
    items:
      type: object
      properties:
        date:
          type: string
        amount:
          type: string
        description:
          type: string
```

### Prompt Templates

**Classification Template:**
```yaml
system_prompt: "Classification expert with domain knowledge"
task_prompt: >-
  <instructions>Clear classification steps</instructions>
  <document-types>{CLASS_NAMES_AND_DESCRIPTIONS}</document-types>
  <<CACHEPOINT>>
  <document-content>{DOCUMENT_TEXT}</document-content>
```

**Extraction Template:**
```yaml
system_prompt: "Extraction expert with JSON output requirement"
task_prompt: >-
  <guidelines>Detailed extraction rules</guidelines>
  <attributes>{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}</attributes>
  <<CACHEPOINT>>
  <document-data>{DOCUMENT_TEXT}</document-data>
```

### Configuration Templates

**Basic Configuration Template:**
```yaml
# Core Processing Configuration
ocr:
  backend: "textract"
  image:
    target_width: ""
    target_height: ""

classes: [Document type definitions]
classification: [Classification configuration]
extraction: [Extraction configuration]
pricing: [Cost calculation parameters]
```

**Advanced Configuration Template:**
```yaml
# Advanced Processing Configuration
ocr:
  backend: "bedrock"
  model_id: "us.amazon.nova-pro-v1:0"
  image:
    target_width: ""
    target_height: ""
    preprocessing: true

classes: [Document type definitions with examples]
classification: [Classification configuration with few-shot]
extraction: [Extraction configuration with few-shot]
assessment: [Assessment configuration]
evaluation: [Evaluation configuration]
summarization: [Summarization configuration]
pricing: [Cost calculation parameters]
```

This comprehensive guide provides the foundation for effective IDP prompt engineering and configuration management, covering all major components and best practices for optimal document processing results.