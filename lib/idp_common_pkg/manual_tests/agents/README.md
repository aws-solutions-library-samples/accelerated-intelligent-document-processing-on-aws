# Agent Testing Utilities

This directory contains utilities for testing IDP agents locally, outside of the Lambda environment. This allows for rapid development, debugging, and validation of agent functionality.

## Files Overview

| File | Purpose |
|------|---------|
| `test_analytics.py` | Local test script for analytics agent |
| `run_analytics_test.py` | Wrapper script with .env file support |
| `test_agent_chat_integration.py` | Integration test for deployed agent chat system |
| `.env.example` | Template for environment variables |
| `README.md` | This file |
| `README_CONVERSATIONAL_TESTS.md` | Documentation for conversational orchestrator tests |

## Quick Start

### 1. Set up your environment

```bash
# Navigate to the idp_common_pkg directory
cd /path/to/idp_common_pkg

# Activate virtual environment (create if needed)
source venv/bin/activate

# Install with agent dependencies
pip install -e ".[agents,analytics,test]"
```

### 2. Configure environment variables

**Option A: Use environment variables directly**
```bash
export ATHENA_DATABASE="your_database_name"
export ATHENA_OUTPUT_LOCATION="s3://your-bucket/athena-results/"
export AWS_REGION="us-east-1"  # optional
export LOG_LEVEL="INFO"  # optional, application logging level
export STRANDS_LOG_LEVEL="INFO"  # optional, Strands framework logging level
```

**Option B: Use a .env file**
```bash
# Copy the example file
cp manual_tests/agents/.env.example manual_tests/agents/.env

# Edit .env with your actual values
nano manual_tests/agents/.env
```

### 3. Run tests

**Basic usage (equivalent to your `python main.py -q "question"`):**
```bash
python manual_tests/agents/test_analytics.py -q "How many documents have I processed each day of the last week?"
```

**With verbose application logging:**
```bash
python manual_tests/agents/test_analytics.py -q "Show me the top 10 documents by accuracy" --verbose
```

**With Strands framework debug logging (shows LLM prompts and responses):**
```bash
python manual_tests/agents/test_analytics.py -q "Create a chart of document types" --strands-debug
```

**With specific logging levels:**
```bash
python manual_tests/agents/test_analytics.py -q "What's the average accuracy?" --log-level INFO --strands-log-level DEBUG
```

**Using the .env wrapper:**
```bash
python manual_tests/agents/run_analytics_test.py -q "What is the average processing time by document type?"
```

## Analytics Agent Testing

The analytics agent converts natural language questions into SQL queries and visualizations. Here are examples of different response types:

### Text Responses
```bash
python manual_tests/agents/test_analytics.py -q "How many total documents are there?"
```

### Table Responses
```bash
python manual_tests/agents/test_analytics.py -q "List the top 5 documents with accuracy scores"
```

### Plot Responses
```bash
python manual_tests/agents/test_analytics.py -q "Create a bar chart of document types"
```

## Logging Configuration

The analytics agent supports two separate logging configurations:

### 1. Application Logging
Controls logging for the IDP Common package and other application code:
- Set with `--verbose` flag or `--log-level` parameter
- Environment variable: `LOG_LEVEL`
- Default: INFO

### 2. Strands Framework Logging
Controls logging specifically for the Strands framework:
- Set with `--strands-debug` flag or `--strands-log-level` parameter
- Environment variable: `STRANDS_LOG_LEVEL`
- Default: INFO
- Set to DEBUG to see detailed agent interactions, including LLM prompts and responses

### Logging Level Options

Both application and Strands logging support these levels:

- `DEBUG`: Detailed debugging information (very verbose)
- `INFO`: Confirmation that things are working as expected
- `WARNING`: Indication that something unexpected happened
- `ERROR`: Due to a more serious problem, the software hasn't been able to perform a function
- `CRITICAL`: A serious error indicating the program may be unable to continue running

### Logging Examples

**To see detailed Strands interactions but minimal application logs:**
```bash
python manual_tests/agents/test_analytics.py -q "Your question" --log-level WARNING --strands-log-level DEBUG
```

**To see detailed application logs but minimal Strands logs:**
```bash
python manual_tests/agents/test_analytics.py -q "Your question" --log-level DEBUG --strands-log-level WARNING
```

## Environment Configuration

### Required Variables by Agent Type

**Analytics Agent:**
- `ATHENA_DATABASE` - Athena database name
- `ATHENA_OUTPUT_LOCATION` - S3 location for query results
- `AWS_REGION` - AWS region (optional, defaults to us-east-1)

**Future Agents:**
Each agent type will document its required environment variables here.

### AWS Credentials

Ensure AWS credentials are configured via:
- `aws configure`
- Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
- IAM roles (if running on EC2)

## Response Format Validation

The test scripts validate agent responses and provide analysis:

```bash
# Example output
============================================================
AGENT RESPONSE:
============================================================
{
  "responseType": "plotData",
  "data": {...},
  "type": "bar"
}
============================================================

Response Type: plotData
Plot Type: bar
```

### Response Types

- **text**: Simple text responses
- **table**: Structured tabular data
- **plotData**: Visualization data (Chart.js format)

## Test Script Architecture

### Core Components

1. **Argument Parsing**: Command-line interface similar to original Strands implementation
2. **Configuration Loading**: Environment variable validation and loading
3. **Agent Creation**: Using the same factory functions as Lambda
4. **Response Processing**: JSON parsing and validation
5. **Error Handling**: Comprehensive error reporting

### Example Test Script Structure

```python
def main():
    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", "-q", help="Question to process")
    args = parser.parse_args()
    
    # Load configuration
    config = get_agent_config()
    
    # Create agent
    agent = create_agent(config)
    
    # Process question
    if args.question:
        response = agent(args.question)
        print(response)
```

## Debugging and Troubleshooting

### Common Issues

**1. Missing Environment Variables**
```
ERROR - Missing required environment variables: ATHENA_DATABASE
```
**Solution**: Set required environment variables or create .env file

**2. AWS Credential Issues**
```
ERROR - Unable to locate credentials
```
**Solution**: Configure AWS credentials properly

**3. Athena Permission Issues**
```
ERROR - Access denied to database
```
**Solution**: Ensure AWS credentials have Athena permissions

### Verbose Logging

Use `--verbose` flag for detailed debugging:

```bash
python manual_tests/agents/test_analytics.py -q "question" --verbose
```

This shows:
- Configuration loading details
- Agent creation process
- Tool execution steps
- Response processing

## Integration Testing

### Local vs Lambda Behavior

The test scripts use the exact same code paths as Lambda functions:

| Component | Local Testing | Lambda Function |
|-----------|---------------|-----------------|
| Configuration | `get_analytics_config()` | `get_analytics_config()` |
| Agent Creation | `create_analytics_agent()` | `create_analytics_agent()` |
| Tools | Same Strands tools | Same Strands tools |
| Response Format | Same JSON format | Same JSON format |

### Validation Steps

1. **Configuration Validation**: Ensure all required variables are set
2. **Agent Creation**: Verify agent initializes without errors
3. **Tool Execution**: Test individual tools work correctly
4. **Response Parsing**: Validate JSON response format
5. **Error Handling**: Test error scenarios and recovery

## Testing Different Agent Types

### Future Agent Testing

As new agent types are added, similar testing scripts can be created following the same pattern:

```bash
# Future document analysis agent
python manual_tests/agents/test_document_analysis.py -q "Analyze this document structure"

# Future workflow agent
python manual_tests/agents/test_workflow.py -q "Automate document approval process"
```

## Agent Chat Integration Testing

The `test_agent_chat_integration.py` script tests the complete conversational agent system in a deployed AWS environment.

### What It Tests

- Lambda function invocation (resolver and processor)
- DynamoDB message storage
- Conversation memory persistence
- Multi-turn conversations
- Streaming responses

### Usage

```bash
# Test with default stack name and region
python manual_tests/agents/test_agent_chat_integration.py

# Test with custom stack and region
python manual_tests/agents/test_agent_chat_integration.py --stack-name MyStack --region us-west-2
```

### Requirements

- Deployed CloudFormation stack with agent chat resources
- AWS credentials configured
- IAM permissions to:
  - Invoke Lambda functions
  - Query DynamoDB tables
  - Describe CloudFormation stacks

### Test Flow

1. **Resolve Lambda and DynamoDB resources** from CloudFormation stack
2. **Send test message** via resolver Lambda
3. **Verify message storage** in ChatMessagesTable
4. **Wait for processor** to generate response (60 seconds)
5. **Check assistant response** in DynamoDB
6. **Verify memory persistence** in IdHelperChatMemoryTable
7. **Test multi-turn conversation** with follow-up message

### Expected Output

```
============================================================
Starting Agent Chat Integration Test
============================================================
Resolver: IDP-AgentChatResolverFunction-ABC123
Processor: IDP-AgentChatProcessorFunction-XYZ789
ChatMessagesTable: IDP-ChatMessagesTable-123456
MemoryTable: IDP-IdHelperChatMemoryTable-789012
Using session ID: test-session-1234567890

============================================================
Test 1: Send message via resolver
============================================================
✅ Resolver returned user message

============================================================
Test 2: Verify message in ChatMessagesTable
============================================================
✅ Message found in ChatMessagesTable

============================================================
Test 3: Wait for processor to complete (60 seconds)
============================================================
✅ Assistant response found!

============================================================
Test 4: Check conversation memory
============================================================
✅ Conversation memory stored

============================================================
Test 5: Test multi-turn conversation
============================================================
✅ Second message sent successfully

============================================================
Test Summary
============================================================
Session ID: test-session-1234567890
Messages in ChatMessagesTable: 4
Assistant responses: 2
Memory items: 2

✅ Integration test completed!
```

## Adding New Test Scripts

To add testing for a new agent type:

1. **Create test script** (`test_new_agent.py`):
   ```python
   from idp_common.agents.new_agent import create_new_agent, get_new_agent_config
   
   def main():
       config = get_new_agent_config()
       agent = create_new_agent(config)
       # ... rest of test logic
   ```

2. **Update .env.example** with new required variables

3. **Add documentation** to this README

## Performance Testing

### Response Time Analysis

Test scripts can be extended to measure performance:

```python
import time

start_time = time.time()
response = agent(question)
end_time = time.time()

print(f"Processing time: {end_time - start_time:.2f} seconds")
```

### Load Testing

For load testing, create scripts that:
1. Run multiple queries in parallel
2. Measure response times and success rates
3. Test error handling under load

## Best Practices

### Test Development

1. **Start Simple**: Begin with basic functionality tests
2. **Add Edge Cases**: Test error conditions and edge cases
3. **Validate Responses**: Always parse and validate JSON responses
4. **Use Verbose Mode**: Enable detailed logging during development
5. **Test Locally First**: Validate locally before Lambda deployment

### Environment Management

1. **Use .env Files**: Keep sensitive data out of scripts
2. **Document Requirements**: Clearly specify required variables
3. **Provide Examples**: Include .env.example with sample values
4. **Validate Early**: Check configuration before agent creation

### Error Handling

1. **Graceful Degradation**: Handle missing dependencies gracefully
2. **Clear Messages**: Provide actionable error messages
3. **Exit Codes**: Use appropriate exit codes for automation
4. **Logging**: Use structured logging for debugging

This testing framework ensures that agents work correctly before deployment and provides a development environment that matches the production Lambda environment.
