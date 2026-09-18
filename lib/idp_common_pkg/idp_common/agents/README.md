# Conversational Agent System

A multi-turn conversational AI system that provides intelligent assistance through specialized agents with persistent memory and real-time streaming responses.

## Overview

The Conversational Agent System enables natural, multi-turn conversations with AI agents that can help with document analysis, data analytics, error diagnosis, and more. The system automatically routes user queries to the most appropriate specialized agent and maintains conversation history for contextual responses.

### Key Features

- **Multi-Turn Conversations**: Maintains context across multiple exchanges
- **Persistent Memory**: Stores conversation history in DynamoDB
- **Real-Time Streaming**: Streams responses as they are generated, over a Lambda Function URL with `InvokeMode=RESPONSE_STREAM`
- **Automatic Agent Selection**: Orchestrator routes queries to the best agent
- **All Agents Available**: No manual agent selection needed
- **Session-Based**: Each conversation has a unique session ID

## Architecture

```
User Message
    ↓
Browser SigV4-signs and POSTs directly to the chat streaming
Lambda Function URL (POST /chat/agent, InvokeMode=RESPONSE_STREAM)
    ↓
Create Conversational Orchestrator
    ├─ Load conversation history from memory
    ├─ Include all registered agents
    └─ Configure context management
    ↓
Stream Response as Server-Sent Events on the open response body
    ├─ Emit each chunk as one SSE frame in real time
    ├─ Store in memory table
    └─ Emit the final response event
    ↓
User receives streaming response
```

There is a second, non-streaming delivery path for environments where Lambda
Function URLs are unavailable (notably AWS GovCloud, where `VITE_STREAM_URL` is
empty). In that case the UI calls the `sendAgentChatMessage` field on the API
Gateway REST API, which routes through the dispatcher to `AgentChatResolver`;
that Lambda stores the user message in `ChatMessagesTable` and async-invokes
`AgentChatProcessor`, and the UI polls `getChatMessages` until the final
assistant message appears (see `src/ui/src/api/chat-poll.ts`). Because the
processors persist only the final message, this path shows a spinner followed by
the complete answer rather than token-by-token output.

## Components

### 1. Lambda Functions

#### AgentChatResolver
- **Purpose**: Entry point for user messages
- **Location**: `src/lambda/agent_chat_resolver/`
- **Responsibilities**:
  - Validates incoming messages
  - Stores user messages in DynamoDB
  - Invokes the processor asynchronously
  - Returns immediate acknowledgment

#### AgentChatProcessor
- **Purpose**: Processes messages and generates responses
- **Location**: `src/lambda/agent_chat_processor/`
- **Responsibilities**:
  - Creates conversational orchestrator with all agents
  - Loads conversation history from memory
  - Streams responses in real-time
  - Stores responses in memory

### 2. Core Modules

#### Agent Factory (`factory/agent_factory.py`)
Central factory for creating and managing agents.

**Key Method**:
```python
create_conversational_orchestrator(
    agent_ids: List[str],
    session_id: str,
    config: Dict[str, Any],
    session: Any
) -> Agent
```

Creates an orchestrator with:
- Memory hooks for conversation history
- Conversation manager for context optimization
- All specified agents as tools

#### Memory Provider (`utils/memory_provider.py`)
Manages conversation history persistence in DynamoDB.

**Features**:
- Stores messages in JSON arrays within DynamoDB items
- Automatically loads recent conversation history
- Groups messages into turns for efficient context
- Handles message size limits with truncation
- Creates new items when approaching 400KB DynamoDB limit

**Usage**:
```python
from idp_common.agents.utils.memory_provider import DynamoDBMemoryHookProvider

memory_provider = DynamoDBMemoryHookProvider(
    table_name="IdpHelperChatMemoryTable",
    session_id="user-session-123",
    region_name="us-east-2",
    max_history_turns=20
)

# Add to agent
agent.hooks.add_hook(memory_provider)
```

#### Conversation Manager (`utils/conversation_manager.py`)
Optimizes conversation context to stay within token limits.

**Features**:
- Drops verbose tool results to reduce context size
- Applies sliding window to keep recent turns
- Preserves important context
- Configurable tool dropping and window size

**Usage**:
```python
from idp_common.agents.utils.conversation_manager import DropAndSlideConversationManager

conversation_manager = DropAndSlideConversationManager(
    tools_to_drop=("read_multiple_files",),
    keep_call_stub=True,
    window_size=20,
    should_truncate_results=True
)

# Add to agent
agent.conversation_manager = conversation_manager
```

### 3. Data Storage

#### ChatMessagesTable (DynamoDB)
Stores all chat messages for display and retrieval.

**Schema**:
- **PK**: `session_id` (e.g., "user-session-123")
- **SK**: `timestamp` (ISO-8601 format)
- **Attributes**: role, content, isProcessing, ExpiresAfter

#### IdHelperChatMemoryTable (DynamoDB)
Stores conversation history for agent memory.

**Schema**:
- **PK**: `conversation#{session_id}`
- **SK**: `timestamp` (ISO-8601 format)
- **Attributes**: conversation_history (JSON), message_count, last_updated

### 4. GraphQL API

#### Mutation: sendAgentChatMessage
Send a message to the conversational agent system.

```graphql
mutation SendMessage {
  sendAgentChatMessage(
    prompt: "How can I analyze document processing errors?"
    sessionId: "user-session-123"
    method: "chat"
  ) {
    role
    content
    timestamp
    isProcessing
    sessionId
  }
}
```

#### Subscription: onAgentChatMessageUpdate
Subscribe to real-time message updates.

```graphql
subscription WatchMessages {
  onAgentChatMessageUpdate(sessionId: "user-session-123") {
    role
    content
    timestamp
    isProcessing
  }
}
```

#### Query: getAgentChatMessages
Retrieve conversation history.

```graphql
query GetHistory {
  getAgentChatMessages(sessionId: "user-session-123") {
    role
    content
    timestamp
    sessionId
  }
}
```

## Available Agents

The system includes several specialized agents:

1. **Document Analysis Agent**: Analyzes document processing workflows
2. **Analytics Agent**: Queries and visualizes data from Athena
3. **Error Analyzer Agent**: Diagnoses errors in CloudWatch logs
4. **Sample Calculator Agent**: Performs calculations and data analysis
5. **External MCP Agents**: Connects to external MCP servers

All agents are automatically available to the orchestrator - no manual selection needed.

## Usage Examples

### Basic Conversation

```python
# Send a message
response = lambda_client.invoke(
    FunctionName='AgentChatResolverFunction',
    Payload=json.dumps({
        "arguments": {
            "prompt": "What agents are available?",
            "sessionId": "user-session-123",
            "method": "chat"
        }
    })
)

# The processor will:
# 1. Load conversation history
# 2. Create orchestrator with all agents
# 3. Stream response in real-time
# 4. Store in memory for next turn
```

### Multi-Turn Conversation

```python
# First message
send_message("Tell me about document processing", "session-456")

# Second message (has context from first)
send_message("How do I fix errors?", "session-456")

# The agent remembers the context about document processing
```

### Testing

Run the backend test script:

```bash
python tests/test_agent_chat_backend.py --stack-name IDP --region us-east-2
```

This tests:
- Message storage in DynamoDB
- Processor invocation
- Assistant response generation
- Memory persistence
- Multi-turn conversations

## Configuration

### Environment Variables

#### AgentChatResolver
- `CHAT_MESSAGES_TABLE`: DynamoDB table for messages
- `AGENT_CHAT_PROCESSOR_FUNCTION`: Processor function name
- `DATA_RETENTION_DAYS`: TTL for messages (default: 30)

#### AgentChatProcessor
- `CHAT_MESSAGES_TABLE`: DynamoDB table for messages
- `ID_HELPER_CHAT_MEMORY_TABLE`: DynamoDB table for memory
- `BEDROCK_REGION`: AWS region for Bedrock/DynamoDB
- `MEMORY_METHOD`: Memory storage method (default: "dynamodb")
- `STREAMING_ENABLED`: Enable streaming (default: true)
- `MAX_CONVERSATION_TURNS`: Max turns to load (default: 20)
- `MAX_MESSAGE_SIZE_KB`: Max message size (default: 8.5)
- `APPSYNC_API_URL`: **Vestigial and always the empty string.** AppSync has been
  removed, but the root `template.yaml` still sets `APPSYNC_API_URL: ""` on about
  a dozen Lambdas (including this one) on purpose: the pre-migration publish
  helpers check the variable and no-op when it is empty, falling back to writing
  DynamoDB directly. If you grep and find it, that is why — do not set it to a
  URL, and do not expect streaming to depend on it.

### CloudFormation Resources

The system is deployed via CloudFormation with these key resources:

- `ChatStreamProcessorFunction` / `ChatStreamProcessorUrl`: the streaming endpoint
  (root `template.yaml`) — a Lambda behind the AWS Lambda Web Adapter with a
  Function URL (`AuthType=AWS_IAM`, `InvokeMode=RESPONSE_STREAM`). It runs the
  *same* processor source in-process and emits SSE frames.
- `AgentChatResolverFunction`: Resolver Lambda for the non-streaming path
  (`nested/api-resolvers/template.yaml`), reached via the REST API dispatcher's
  `sendAgentChatMessage` → function mapping
- `AgentChatProcessorFunction`: Processor Lambda
- `ChatMessagesTable`: Message storage
- `IdHelperChatMemoryTable`: Memory storage

## How It Works

### 1. User Sends Message

On the streaming path the browser POSTs the message straight to the chat
streaming Function URL, SigV4-signed with the caller's Cognito Identity Pool
credentials:

```
POST https://<id>.lambda-url.<region>.on.aws/chat/agent
{"sessionId": "session-123", "prompt": "Hello"}
```

On the non-streaming fallback path the UI instead calls the REST API's
`sendAgentChatMessage` field (`POST <apiBaseUrl>/op/sendAgentChatMessage`), which
the dispatcher routes to `AgentChatResolver`.

### 2. Resolver Stores Message (non-streaming path only)

`AgentChatResolver` Lambda:
- Validates the message
- Stores in `ChatMessagesTable` with PK=sessionId, SK=timestamp
- Invokes `AgentChatProcessor` asynchronously
- Returns immediate acknowledgment

On the streaming path there is no separate resolver hop: the streaming function
runs the processor code in-process so it can emit deltas on the response body as
they are produced.

### 3. Processor Creates Orchestrator

`AgentChatProcessor` Lambda:
- Gets ALL registered agents automatically
- Creates conversational orchestrator with:
  - Memory provider (loads last 20 turns)
  - Conversation manager (optimizes context)
  - All agents as tools

### 4. Orchestrator Processes Message

The orchestrator:
- Analyzes the user's query
- Selects the most appropriate agent
- Routes the query to that agent
- Generates a response

### 5. Response Streams Back

As the response is generated:
- Each chunk is written as one Server-Sent-Events frame on the still-open Function
  URL response body (`data: {...}\n\n`)
- The frontend reads the frames incrementally from that response — the event
  shapes are the same objects the old subscription delivered, so the UI's message
  handling is unchanged
- Thinking tags are removed for clean display
- Final response is stored in memory

On the non-streaming fallback path nothing is emitted mid-flight: only the final
assistant message is written to `ChatMessagesTable`, and the UI polls
`getChatMessages` for it.

### 6. Memory Persists

After the response:
- Full conversation stored in `IdHelperChatMemoryTable`
- Available for next turn in the conversation
- Grouped into turns for efficient loading

## Development

### Adding a New Agent

1. Create agent implementation in `agents/{agent_name}/`
2. Register with factory in `agents/__init__.py`:

```python
from .factory import agent_factory
from .my_agent import create_my_agent

agent_factory.register_agent(
    agent_id="my-agent",
    agent_name="My Agent",
    agent_description="Does something useful",
    creator_func=create_my_agent,
    sample_queries=["example query"]
)
```

3. Agent is automatically available to orchestrator!

### Testing Your Agent

```python
# Unit test
from idp_common.agents.factory import agent_factory

agent = agent_factory.create_agent(
    agent_id="my-agent",
    config=config,
    session=session
)

response = agent("test query")
```

### Running Unit Tests

```bash
# Test conversational orchestrator
cd lib/idp_common_pkg/idp_common/agents/testing
python run_conversational_orchestrator_test.py

# Or with pytest
pytest test_conversational_orchestrator.py -v
```

## Troubleshooting

### No Assistant Response

**Check CloudWatch Logs**:
```bash
aws logs tail /aws/lambda/{ProcessorFunctionName} --follow --region us-east-2
```

Look for:
- Import errors
- Bedrock permission issues
- Memory table access errors
- Orchestrator creation failures

### Memory Not Persisting

**Verify table access**:
- Check IAM permissions for `IdHelperChatMemoryTable`
- Verify `BEDROCK_REGION` environment variable
- Check CloudWatch logs for DynamoDB errors

### Streaming Not Working

**Check the streaming Function URL**:
- Verify the UI has `VITE_STREAM_URL` set to the `ChatStreamProcessorUrl` Function
  URL. When it is empty the UI silently falls back to the non-streaming polling
  path, which looks like "streaming is broken" but is working as designed.
- Check that the authenticated Cognito role grants **`lambda:InvokeFunction`** on
  `ChatStreamProcessorFunction`. A Function URL invocation requires
  `lambda:InvokeFunction` — `lambda:InvokeFunctionUrl` alone returns a 403
  `AccessDeniedException` at invoke time.
- Confirm the browser request is SigV4-signed with Identity Pool credentials; the
  Function URL is `AuthType=AWS_IAM`, so an unsigned request is rejected at the
  function edge before any code runs.
- Ignore `APPSYNC_API_URL` here. It is deliberately empty (see Environment
  Variables above) and has nothing to do with streaming.

### Context Not Maintained

**Check memory loading**:
- Verify `MAX_CONVERSATION_TURNS` is set
- Check memory table has conversation history
- Look for "Loaded X conversation turns" in logs

## Performance

- **Cold Start**: ~5-10 seconds (first invocation)
- **Warm Start**: ~1-2 seconds (subsequent invocations)
- **Response Time**: 30-60 seconds (depends on agent complexity)
- **Memory Size**: 128 MB (resolver), 3072 MB (processor)
- **Timeout**: 30 seconds (resolver), 600 seconds (processor)

## Security

- **Authentication**: the REST API's Cognito User Pools authorizer for the
  `sendAgentChatMessage` path; IAM (SigV4 with Identity Pool credentials) at the
  Function URL for the streaming path
- **Authorization**: enforced in the resolver/processor code from the caller's
  identity — group membership from the `cognito:groups` claim, plus session
  ownership checks against the caller's Cognito `sub`. The REST authorizer only
  authenticates; it does not evaluate any group policy.
- **Encryption**: KMS encryption for DynamoDB tables
- **TTL**: Messages expire after 30 days (configurable)
- **VPC**: Not required (uses AWS service APIs)

## Monitoring

### Key Metrics

- Lambda invocations (resolver and processor)
- Lambda duration and errors
- DynamoDB read/write capacity
- API Gateway request count and 4xx/5xx rate on the REST API
- Invocations, duration and errors on the streaming function
- Bedrock API calls

### CloudWatch Logs

- `/aws/lambda/{ResolverFunctionName}`: Resolver logs
- `/aws/lambda/{ProcessorFunctionName}`: Processor logs

### Alarms

Consider setting up alarms for:
- Lambda errors > 5%
- Lambda duration > 500 seconds
- DynamoDB throttling
- Bedrock API errors

## Cost Optimization

- **DynamoDB**: On-demand pricing, TTL reduces storage
- **Lambda**: Pay per invocation, warm starts reduce cost
- **Bedrock**: Pay per token, context management reduces usage
- **API Gateway**: Pay per request on the REST API transport
- **Streaming function**: Pay for the invocation's full duration (the Lambda stays
  billed while the response streams) plus streamed-response data transfer

## Future Enhancements

- [ ] Support for file attachments
- [ ] Agent-specific memory (per-agent context)
- [ ] Conversation branching
- [ ] Export conversation history
- [ ] Custom agent selection
- [ ] Rate limiting per user
- [ ] Cost tracking per conversation

## Support

For issues or questions:
1. Check CloudWatch logs
2. Review this documentation
3. Run backend test script
4. Check DynamoDB tables for data
5. Verify environment variables

## License

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
