---
title: "Criteria Validation Service"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Criteria Validation Service

The Criteria Validation Service provides sophisticated document validation against dynamic business rules using Large Language Models (LLMs). Designed primarily for healthcare/insurance prior authorization workflows, this service can be adapted for any scenario requiring document compliance validation against configurable criteria.

## Overview

The criteria validation service processes user documents, evaluates them against configurable business rules/criteria questions, and generates structured validation responses with recommendations (Pass/Fail/Information Not Found). It provides enterprise-grade capabilities including asynchronous processing, intelligent text chunking, multi-file processing with summarization, and comprehensive cost and performance tracking.

### Key Capabilities

- **Dynamic Business Rules**: Configure criteria questions without code changes
- **Asynchronous Processing**: Handle multiple criteria types and questions concurrently
- **Intelligent Text Chunking**: Automatically process large documents with context preservation
- **Multi-File Support**: Process multiple user history files with intelligent summarization
- **Comprehensive Tracking**: Token usage, cost tracking, and detailed timing metrics
- **Robust Error Handling**: Graceful degradation with fallback responses
- **Strong Data Validation**: Pydantic-based input/output validation with automatic data cleaning

### Primary Use Cases

- **Healthcare Prior Authorization**: Validate medical requests against insurance criteria
- **Compliance Validation**: Ensure documents meet regulatory requirements
- **Business Rule Enforcement**: Automate document approval workflows
- **Quality Assurance**: Validate document completeness against checklists
- **Audit Preparation**: Systematically verify document compliance

## Architecture Overview

### Processing Workflow

```
Document Upload → Text Extraction → Criteria Processing → Validation Results
     ↓                ↓                    ↓                     ↓
  S3 Storage    →  Chunking (if needed) → LLM Evaluation  →  S3 Storage
                                          ↓
                                    JSON Response Parsing
                                          ↓
                                    Pydantic Validation
```

### Core Components

1. **CriteriaValidationService**: Main service orchestrating the validation workflow
2. **Async Processing Engine**: Handles concurrent processing with rate limiting
3. **Text Chunking System**: Intelligently splits large documents with overlap
4. **LLM Integration**: Seamless integration with Amazon Bedrock models
5. **Result Aggregation**: Combines responses across files and criteria types
6. **Metrics Collection**: Comprehensive token usage and performance tracking

### Integration with IDP Pipeline

The criteria validation service integrates seamlessly with the IDP accelerator:

- **Common Bedrock Client**: Uses `idp_common.bedrock.invoke_model` for consistent LLM interactions
- **Unified Metering**: Automatic token usage tracking with `utils.merge_metering_data`
- **S3 Operations**: Uses `idp_common.s3` for standardized file operations
- **Configuration Compatibility**: Works with `idp_common.get_config()`

## Getting Started

### Installation

The criteria validation service is included in the IDP common package:

```bash
pip install -e "lib/idp_common_pkg[all]"
```

### Basic Usage

```python
from idp_common.criteria_validation import CriteriaValidationService

# Initialize service
service = CriteriaValidationService(
    region="us-east-1",
    config=validation_config
)

# Validate a request
result = service.validate_request(
    request_id="123456789",
    config=validation_config
)

# Access results
print(f"Request ID: {result.request_id}")
print(f"Token usage: {result.metering}")
print(f"Processing time: {result.metadata['timing']['total_duration']} seconds")
```

## Configuration

### Core Configuration Structure

```python
validation_config = {
    # Model Configuration
    "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "temperature": 0.0,  # Deterministic responses
    "top_k": 5,
    "top_p": 0.1,
    "max_tokens": 4096,
    
    # S3 Storage Configuration
    "request_bucket": "user-history-bucket",
    "request_history_prefix": "prior-auth",
    "criteria_bucket": "criteria-bucket", 
    "output_bucket": "results-bucket",  # Optional
    
    # Processing Configuration
    "criteria_types": ["administration_requirements", "medical_necessity"],
    "recommendation_options": "Pass/Fail/Information Not Found",
    
    # Async Processing Controls
    "criteria_validation": {
        "semaphore": 5,  # Concurrent request limit
        "max_chunk_size": 10000,  # Max tokens per chunk
        "token_size": 4,  # Average chars per token
        "overlap_percentage": 10  # Chunk overlap %
    },
    
    # Required Prompts
    "system_prompt": "You are an expert at evaluating healthcare criteria...",
    "task_prompt": "Evaluate the following criteria: {question}..."
}
```

### Configuration Parameters Reference

#### Processing Controls
- **semaphore**: Controls concurrent LLM requests (default: 5)
- **max_chunk_size**: Maximum tokens per text chunk (default: 10,000)
- **token_size**: Average characters per token for estimation (default: 4)
- **overlap_percentage**: Percentage overlap between chunks (default: 10%)

#### Model Parameters
- **temperature**: LLM temperature for deterministic responses (default: 0.0)
- **top_k**: Top-k sampling parameter (default: 5)
- **top_p**: Top-p sampling parameter (default: 0.1)
- **max_tokens**: Optional maximum tokens in response

#### Storage Configuration
- **request_bucket**: S3 bucket containing user history files
- **request_history_prefix**: Prefix for organizing request data
- **criteria_bucket**: S3 bucket containing criteria definitions
- **output_bucket**: S3 bucket for results (defaults to request_bucket)

## Data Structure Requirements

### User History Files

User history documents must be stored as UTF-8 encoded text files in S3:

```
s3://{request_bucket}/{request_history_prefix}-{request_id}/extracted_text/
├── medical_history.txt
├── treatment_plan.txt
└── physician_notes.txt
```

**Requirements:**
- Files must have `.txt` extension
- UTF-8 encoding required
- Automatically chunked if exceeding token limits
- Multiple files processed with optional summarization

### Criteria Files

Criteria definitions are stored as JSON files in S3:

```
s3://{criteria_bucket}/{criteria_type}.json
```

**Format:**
```json
{
    "criteria": [
        "Will the therapy be administered under qualified medical supervision?",
        "Is the facility equipped to handle potential emergency situations?", 
        "Has the physician determined the appropriate dosage and schedule?",
        "Are there documented contraindications or drug interactions?"
    ]
}
```

**Best Practices for Criteria Questions:**
- Write specific, answerable questions
- Focus on binary Pass/Fail outcomes when possible
- Ensure questions can be answered from typical user history
- Use clear, professional language
- Include context about where information might be found

## Advanced Usage

### Asynchronous Processing

For high-volume processing, use the async API:

```python
import asyncio
from idp_common.criteria_validation import CriteriaValidationService

async def validate_multiple_requests():
    service = CriteriaValidationService(config=config)
    
    # Process multiple requests concurrently
    tasks = [
        service.validate_request_async(request_id, config)
        for request_id in ["req1", "req2", "req3"]
    ]
    
    # Wait for all to complete
    results = await asyncio.gather(*tasks)
    
    # Aggregate metrics
    total_tokens = sum(r.metering.get('total_tokens', 0) for r in results)
    print(f"Total tokens used: {total_tokens}")
    
    return results

# Run async processing
results = asyncio.run(validate_multiple_requests())
```

### Multi-File Summarization

When processing multiple files per request, enable summarization:

```python
validation_config.update({
    "summary": {
        "system_prompt": "You are an expert at summarizing validation responses...",
        "task_prompt": """Summarize the following responses for question: {question}
        
        Initial Responses: {initial_response}
        Criteria Type: {criteria_type}
        
        Provide consolidated recommendation...""",
        "temperature": 0.0
    }
})
```

### Custom Prompt Engineering

Customize prompts for specific use cases:

```python
# Healthcare-specific system prompt
healthcare_system_prompt = """
You are a specialized healthcare insurance evaluator with expertise in:
- Medical necessity criteria
- Treatment authorization protocols  
- Clinical guideline interpretation
- Prior authorization requirements

Evaluate each criterion carefully based on medical evidence and insurance policies.
"""

# Structured task prompt with XML formatting
healthcare_task_prompt = """
<user_history>
<source_filepath>{source_filepath}</source_filepath>
<content>{content}</content>
</user_history>

<criteria>
<criteria_type>{criteria_type}</criteria_type>
<question>{question}</question>
</criteria>

<instructions>
Evaluate the patient's eligibility for the specified criteria:

Decision Options: {recommendation_options}

Response Format:
{{
  "criteria_type": "{criteria_type}",
  "source_file": ["{source_filepath}"],
  "question": "question text",
  "Recommendation": "Pass/Fail/Information Not Found", 
  "Reasoning": "detailed explanation with citations"
}}
</instructions>
"""
```

## Response Format and Processing

### Individual Validation Response

Each validation generates a structured response:

```json
{
    "criteria_type": "administration_requirements",
    "source_file": ["s3://bucket/extracted_text/medical_history.txt"],
    "question": "Is the facility equipped to handle emergencies?",
    "Recommendation": "Pass",
    "Reasoning": "The document confirms that Memorial Hospital Infusion Center has 24/7 emergency support, trained nursing staff, and emergency response equipment including epinephrine for anaphylaxis treatment."
}
```

### Complete Validation Result

The service returns a comprehensive result object:

```python
@dataclass
class CriteriaValidationResult:
    request_id: str                           # Unique request identifier
    criteria_type: str                        # Primary criteria type
    validation_responses: List[Dict[str, Any]] # All validation responses
    summary: Optional[Dict[str, Any]]         # Multi-file summary
    metering: Optional[Dict[str, Any]]        # Token usage tracking
    metadata: Optional[Dict[str, Any]]        # Timing and processing info
    output_uri: Optional[str]                 # Result storage location
    errors: Optional[List[str]]               # Any processing errors
```

### Result Storage Structure

Results are automatically saved to S3:

```
s3://{output_bucket}/responses/
├── request_id_{request_id}_administration_requirements_responses.json
├── request_id_{request_id}_medical_necessity_responses.json
└── request_id_{request_id}_coverage_requirements_responses.json
```

## Performance Optimization

### Text Chunking Strategy

Large documents are intelligently chunked with overlap for context preservation:

```python
# Chunking configuration
"criteria_validation": {
    "max_chunk_size": 8000,    # Smaller chunks = more requests, better accuracy
    "overlap_percentage": 15,   # Higher overlap = better context, more tokens
    "token_size": 4            # Adjust based on model tokenizer
}
```

**Chunking Process:**
1. Estimate tokens using `len(text) // token_size`
2. If exceeding `max_chunk_size`, split into overlapping chunks
3. Each chunk overlaps by `overlap_percentage` with the next
4. Process each chunk independently and aggregate results

### Concurrent Processing Optimization

```python
# Optimal semaphore configuration
"semaphore": 5,  # Start with 5, adjust based on rate limits

# Monitor and adjust based on performance
"semaphore": 3,  # Reduce if hitting rate limits
"semaphore": 8,  # Increase if responses are slow and no rate limiting
```

### Cost Optimization Strategies

1. **Token Monitoring**: Track usage per criteria type for cost attribution
2. **Chunk Size Tuning**: Balance accuracy vs. token consumption  
3. **Model Selection**: Choose appropriate model for accuracy/cost balance
4. **Summarization Strategy**: Use only when processing multiple files
5. **Criteria Optimization**: Write efficient, focused criteria questions

## Monitoring and Metrics

### Token Usage Tracking

The service provides comprehensive token and cost tracking:

```python
# Automatic token aggregation
{
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "inputTokens": 15420,
        "outputTokens": 892,
        "totalTokens": 16312
    }
}
```

### Timing Metrics

Detailed timing information is collected:

```python
{
    "timing": {
        "start_time": "2024-01-15T10:30:00",
        "end_time": "2024-01-15T10:32:15",
        "total_duration": 135.2,  # seconds
        "criteria_processing_time": [
            {"criteria_type": "administration_requirements", "duration": 45.1},
            {"criteria_type": "medical_necessity", "duration": 67.3}
        ]
    }
}
```

### Performance Monitoring

Key metrics to monitor in production:

- **Token Usage**: Track usage patterns and costs per criteria type
- **Processing Time**: Monitor timing metrics by criteria complexity
- **Error Rates**: Track parsing failures and LLM errors
- **Throughput**: Requests processed per hour
- **Accuracy**: Success rate of criteria validation

## Error Handling and Troubleshooting

### Graceful Error Handling

The service includes comprehensive error handling:

```python
# Request-level error handling
try:
    result = service.validate_request(request_id, config)
except ValueError as e:
    logger.error(f"Configuration error: {str(e)}")
except Exception as e:
    logger.error(f"Processing error: {str(e)}")
```

### Common Issues and Solutions

#### No Text Files Found
```
Error: "No text files found for request {request_id}"
Solution: Verify file locations and .txt extension
Expected: s3://{bucket}/{prefix}-{request_id}/extracted_text/*.txt
```

#### JSON Parsing Failures
```
Symptoms: "Information Not Found" responses with parsing errors
Solutions: 
- Review prompts for clear JSON format instructions
- Use temperature = 0.0 for deterministic responses
- Add example JSON structure in prompts
```

#### Rate Limiting Issues
```
Symptoms: HTTP 429 errors or slow processing
Solutions:
- Reduce semaphore value (e.g., from 5 to 3)
- Monitor CloudWatch metrics for throttling
- Implement exponential backoff
```

#### High Token Costs
```
Solutions for cost optimization:
- Reduce max_chunk_size (e.g., from 10000 to 8000)
- Lower overlap_percentage (e.g., from 10% to 5%)
- Use more specific, focused criteria questions
- Consider model selection (Claude Haiku vs Sonnet)
```

### Debug Logging

Enable detailed logging for troubleshooting:

```python
import logging
logging.getLogger('idp_common.criteria_validation').setLevel(logging.DEBUG)

# Key debug information logged:
# - Token metrics before/after merge
# - Chunk processing details  
# - Response parsing steps
# - Timing information
# - S3 operations
```

## Best Practices

### Configuration Management

1. **Environment-Specific Configs**: Separate configurations for dev/test/prod
2. **Parameter Tuning**: Start with defaults, adjust based on performance
3. **Cost Monitoring**: Set up alerts for unexpected token usage
4. **Version Control**: Track configuration changes over time

### Criteria Design Principles

1. **Specific Questions**: Write clear, answerable criteria questions
   ```
   Good: "Is the facility equipped with emergency response equipment?"
   Poor: "Is the facility adequate?"
   ```

2. **Binary Decisions**: Design for clear Pass/Fail outcomes when possible
   ```
   Good: "Has the physician documented contraindications?"
   Poor: "What are the physician's thoughts on contraindications?"
   ```

3. **Context Requirements**: Ensure questions can be answered from typical user history
4. **Regular Updates**: Review and update criteria based on business needs

### Processing Optimization

1. **Batch Processing**: Process multiple requests together when possible
2. **Result Caching**: Cache results for identical requests to avoid reprocessing
3. **Monitoring Setup**: Implement comprehensive monitoring and alerting
4. **Testing Strategy**: Regular testing with representative data

### Production Deployment

1. **Scaling Configuration**: 
   ```python
   # Production settings
   "criteria_validation": {
       "semaphore": 3,  # Conservative to avoid rate limits
       "max_chunk_size": 8000,  # Balance cost vs accuracy
       "overlap_percentage": 10
   }
   ```

2. **Error Monitoring**: Set up CloudWatch alarms for error rates
3. **Cost Controls**: Implement budget alerts and usage tracking
4. **Security**: Ensure proper IAM roles and S3 bucket policies

## Integration Examples

### Healthcare Prior Authorization

```python
# Healthcare-specific configuration
healthcare_config = {
    "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "criteria_types": [
        "medical_necessity",
        "administration_requirements", 
        "coverage_criteria",
        "safety_protocols"
    ],
    "system_prompt": """You are a healthcare insurance evaluator specializing in prior authorization reviews. Evaluate each criterion based on medical evidence, clinical guidelines, and insurance policy requirements.""",
    "recommendation_options": """
    Pass: Criterion is fully met with supporting documentation
    Fail: Criterion is not met or requires additional information  
    Information Not Found: Insufficient data in user history to make determination
    """
}

# Process prior authorization request
result = service.validate_request("PA-2024-001", healthcare_config)
```

### Compliance Validation

```python
# Regulatory compliance configuration
compliance_config = {
    "criteria_types": ["regulatory_requirements", "documentation_standards"],
    "system_prompt": """You are a compliance specialist evaluating document adherence to regulatory standards. Focus on completeness, accuracy, and compliance with established guidelines.""",
    "task_prompt": """Review the document against this compliance requirement:
    
    Requirement: {question}
    Category: {criteria_type}
    Document Content: {content}
    
    Determine if the document meets the requirement and provide specific citations."""
}
```

### Quality Assurance Workflow

```python
# Quality assurance configuration  
qa_config = {
    "criteria_types": ["completeness_check", "accuracy_validation"],
    "system_prompt": """You are a quality assurance specialist reviewing documents for completeness and accuracy. Identify missing information and potential errors.""",
    "criteria_validation": {
        "semaphore": 10,  # Higher concurrency for QA workflows
        "max_chunk_size": 15000  # Larger chunks for comprehensive review
    }
}
```

## Future Enhancements

### Planned Features

- **Document Model Integration**: Direct integration with IDP Document instances
- **Confidence Scoring**: Confidence levels for recommendations with thresholds
- **Custom Validation Rules**: Programmable validation logic beyond LLM evaluation
- **Real-time API Endpoints**: REST API for real-time validation requests
- **Enhanced Caching**: Intelligent caching based on content similarity

### Advanced Capabilities Under Consideration

- **Multi-Modal Support**: Integration of document images with text analysis
- **Feedback Loop Integration**: Learning from validation corrections and user feedback
- **Custom Model Support**: Support for domain-specific fine-tuned models
- **Workflow Integration**: Integration with business process management systems
- **Audit Trail Enhancement**: Comprehensive audit logging for regulatory compliance

## Related Documentation

- [Configuration Guide](./configuration.md) - General IDP configuration management
- [Extraction & Confidence](./extraction-and-confidence.md) - Information extraction, confidence, and geometry capabilities
- [Monitoring Guide](./monitoring.md) - System monitoring and alerting
- [Troubleshooting Guide](./troubleshooting.md) - Common issues and solutions

---

The Criteria Validation Service provides a powerful foundation for implementing business rule validation workflows using AI. Its flexible configuration system, robust error handling, and comprehensive monitoring make it suitable for production deployments across various industries and use cases.
