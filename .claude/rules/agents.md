---
paths:
  - "lib/idp_common_pkg/idp_common/agents/**"
  - "src/lambda/*agent*/**"
---

# Agent rules

Covers the Strands-based agent system in `idp_common.agents` (orchestrator,
analytics, error analyzer, code intelligence and external MCP agents) and the
Lambdas that run it.

## Must
- Put a new agent in `agents/<name>/` with a creator function and register it
  through `agent_factory.register_agent(...)`. The orchestrator picks up
  registered agents automatically.
- Build on `agents/common/` (`IDPAgent`, the Strands Bedrock model, config,
  response utilities, the DynamoDB logger, monitoring, cost metrics) and on
  `idp_common.bedrock` instead of writing parallel versions.
- Read agent model IDs from configuration (for example
  `agents.chat_companion.model_id`), not from code or stack parameters.
- Run generated Python only in the AgentCore Code Interpreter sandbox, and move
  data in and out through S3 and the AgentCore APIs.
- Enforce group checks in the processors that do the work, such as
  `agent_chat_processor`. The Function URL streaming path reaches them without
  passing through a resolver (see [api-authorization.md](api-authorization.md)).
- Make chat changes work on both delivery paths: streaming through the Lambda
  Function URL, and the REST fallback that polls `getChatMessages` (used where
  Function URLs are unavailable, such as GovCloud).
- Keep conversation memory inside DynamoDB item-size limits and the sliding
  turn window by going through `DynamoDBMemoryHookProvider`.
- Ship the Code Intelligence agent disabled, behind a toggle that requires user
  consent, with prompts that refuse credentials and personal data.
- Require HTTPS and validate Cognito tokens on every request to an external MCP
  agent, and store its credentials in Secrets Manager.
- Add offline unit tests under `lib/idp_common_pkg/tests/unit/agents/`. Keep live
  scripts in `manual_tests/agents/`, which runs by hand only.

## Must not
- Execute agent-generated code inside the Lambda itself.
- Create `bedrock-runtime` clients directly in agent code.
- Use a request-body identity as an authorization or scope key.
- Send customer data, document content, account IDs or secrets to a third-party
  MCP server.
- Wire a new agent into the orchestrator by hand instead of registering it.

## Verify with
- `cd lib/idp_common_pkg && make test-unit`
- `make api-test-static` (its Function URL checks cover the chat routes)

## Sources
- [lib/idp_common_pkg/idp_common/agents/README.md](../../lib/idp_common_pkg/idp_common/agents/README.md):
  components, memory, streaming and fallback paths, and how to add an agent
- [docs/agent-companion-chat.md](../../docs/agent-companion-chat.md): the
  multi-agent architecture, security and privacy, and Code Intelligence
- [docs/agent-analysis.md](../../docs/agent-analysis.md): the analytics agent
  workflow and sandbox
- [docs/custom-MCP-agent.md](../../docs/custom-MCP-agent.md): integrating an
  external MCP agent
