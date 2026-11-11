---
paths:
  - "lib/idp_common_pkg/idp_common/agents/quick_start/**"
  - "lib/idp_common_pkg/idp_common/agents/factory/**"
  - "lib/idp_common_pkg/idp_common/synthesis/**"
  - "lib/idp_common_pkg/idp_common/discovery/**"
  - "lib/idp_common_pkg/idp_common/config/**"
  - "src/lambda/agent_chat_processor/**"
  - "src/lambda/chat_stream_processor/**"
  - "nested/api-resolvers/src/lambda/agent_chat_resolver/**"
  - "nested/api-resolvers/src/lambda/discovery_upload_resolver/**"
  - "src/ui/src/components/agent-chat/**"
  - "src/ui/src/components/genaiidp-layout/navigation.tsx"
  - "src/ui/src/routes/AuthRoutes.tsx"
  - "src/ui/src/routes/AgentChatRoutes.tsx"
  - "src/ui/src/contexts/agentChat.tsx"
  - "src/ui/src/hooks/use-agent-chat.ts"
  - "src/ui/src/hooks/use-user-role.ts"
  - "src/ui/src/api/stream-client.ts"
  - "src/ui/src/components/discovery/**"
  - "src/ui/src/components/welcome/**"
  - "src/ui/src/contexts/guidedTour.tsx"
  - "feature-platform/idp-data-generator/**"
  - "docs/quick-start.md"
---

# Quick Start rules

Covers Quick Start, the chat agent in `idp_common.agents.quick_start` that drafts a
schema from a description and creates and activates a configuration profile, and
the shared code it calls. Quick Start is a new user's first experience, and it
breaks without a failing test when a surface it calls changes under it. It is a
chat agent, so [agents.md](agents.md) applies too.

## Must
- Say in the PR description whether the change affects Quick Start and how you
  checked: open it, describe a document, review the drafted fields, approve, and
  see the profile created and activated.
- Change Quick Start's callers in the same change as any contract it shares with
  another page: the discovery uploads, the configuration profile API, the chat
  route and stream, and chat sessions.
- When you rename a page, a navigation item or a product term, update the agent's
  system prompt in `agents/quick_start/agent.py`, which names screens and paths, and
  `docs/quick-start.md`.
- When Quick Start's entry points or journey change, update its stories in
  `scripts/ux_stories.toml` and its flow in `scripts/ux_flows.yaml`.
- Give a new Quick Start tool or code path an offline test beside the existing ones
  in `lib/idp_common_pkg/tests/unit/synthesis/`.

## Must not
- Name a screen, path or extension in the agent's prompt that the UI does not show
  the user it is talking to.

## Verify with
- `cd lib/idp_common_pkg && make test-unit`
- `cd src/ui && npx vitest run`
- `make ai-ux-test-stories-local STORY=draft-setup-from-a-description AS=platform-admin`,
  against the review stack

## Sources
- [docs/quick-start.md](../../docs/quick-start.md): the user journey, the CLI form
  and the optional test-set generator
- [lib/idp_common_pkg/idp_common/agents/README.md](../../lib/idp_common_pkg/idp_common/agents/README.md):
  how agents are built, registered and given memory
- [.claude/skills/ux-test-stories.md](../skills/ux-test-stories.md): writing and
  running first-time-user stories
