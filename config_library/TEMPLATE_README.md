Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# [Configuration Name] Configuration

This directory contains a specialized configuration for [brief description of the use case] using the GenAI IDP Accelerator.

## Pattern Association

**Pattern**: [Pattern-X] - [Brief description of the pattern]

## Validation Level

**Level**: [0-3] - [Level name]

- **Testing Evidence**: [Describe the testing performed, including number and types of documents tested, performance metrics, etc.]
- **Production Usage**: [If applicable, describe where and how this configuration has been used in production]
- **Known Limitations**: [List any known limitations or edge cases]

## Overview

[Provide a comprehensive overview of the configuration, including its purpose, target document types, and key features]

The [configuration name] configuration is designed to handle various types of [domain] documents, including:

- [Document type 1]
- [Document type 2]
- [Document type 3]
- [etc.]

## Key Components

### Document Classes

The configuration defines [number] document classes, each with specific attributes to extract:

- **[Class 1]**: [Brief description]
  - Extracts [attribute 1], [attribute 2], etc.
- **[Class 2]**: [Brief description]
  - Extracts [attribute 1], [attribute 2], etc.
- [etc.]

### Classification Settings

- **Model**: [Model name]
- **Strategy / granularity**: [e.g. One stage, page granularity]
- **Temperature**: [Temperature value]
- **Top-k**: [Top-k value]

[Additional details about classification approach]

### Extraction Settings

- **Model**: [Model name]
- **Temperature**: [Temperature value]
- **Top-k**: [Top-k value]

[Additional details about extraction approach]

### Summarization Settings

- **Model**: [Model name]
- **Temperature**: [Temperature value]
- **Top-k**: [Top-k value]

[Additional details about summarization approach]

## Key Differences from Default Configuration

### 1. [Difference Category 1]

[Describe the key differences and improvements compared to the default configuration]

### 2. [Difference Category 2]

[Describe the key differences and improvements compared to the default configuration]

### 3. [Difference Category 3]

[Describe the key differences and improvements compared to the default configuration]

## Sample Documents

This configuration includes sample documents in the `samples/` directory:

- `sample1.pdf`: [Description of the document and what it demonstrates]
- `sample2.pdf`: [Description of the document and what it demonstrates]
- [etc.]

## Performance Metrics

[Include relevant performance metrics such as accuracy, processing time, token usage, etc.]

| Metric | Value | Notes |
|--------|-------|-------|
| Classification Accuracy | [value] | [notes] |
| Extraction Accuracy | [value] | [notes] |
| Processing Time | [value] | [notes] |
| Token Usage | [value] | [notes] |

## Usage Instructions

To use this configuration:

1. [Step 1]
2. [Step 2]
3. [Step 3]

## Customization Guidance

[Provide guidance on how to further customize this configuration for specific needs]

## Contributors

- [Contributor 1] - [Organization]
- [Contributor 2] - [Organization]

## Version History

- v1.0 (YYYY-MM-DD): Initial release
- [Additional versions as needed]
