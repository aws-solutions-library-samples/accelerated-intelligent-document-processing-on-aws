// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
//
// Per-session opt-in agents for Agent Companion Chat.
//
// Code Intelligence and External MCP agents send their query to a service outside
// the deployment, so each is off until the user opts in. A conversation that
// already holds Analytics or Error Analyzer results never gets them: the server
// leaves them out of such a session, and opting in there starts a new session
// with empty memory. The server reports both facts as stream events (names
// mirror idp_common.chat_session_agents); this module turns them into state.
import type { AgentChatState, ChatMessage } from '../../types/agent-chat';

/** Stream event: this session holds Analytics or Error Analyzer results. */
export const SESSION_DATA_RESULTS_EVENT = 'session_data_results';
/** Stream event: the turn moved to a new session; content is JSON {sessionId}. */
export const SESSION_REPLACED_EVENT = 'session_replaced';

/** Shown when opting in started a new session. */
export const NEW_SESSION_NOTICE =
  'Started a new conversation. Code Intelligence and External MCP agents are not used in a conversation that includes Analytics or Error Analyzer results.';

export type OptInKey = 'enableCodeIntelligence' | 'enableExternalMcpAgents';

/** The opt-in flags a request carries, read from the current state. */
export const optInFlags = (
  state: Pick<AgentChatState, OptInKey>,
): { enableCodeIntelligence: boolean; enableExternalMcpAgents: boolean } => ({
  enableCodeIntelligence: state.enableCodeIntelligence,
  enableExternalMcpAgents: state.enableExternalMcpAgents,
});

/**
 * What changing an opt-in checkbox does.
 *
 * Turning one on in a session the server reported as holding data results starts
 * a new session (carrying both opt-in settings over, with the notice); anything
 * else just updates the checkbox.
 */
export const optInChange = (
  state: Pick<AgentChatState, OptInKey | 'sessionHoldsDataResults'>,
  key: OptInKey,
  checked: boolean,
): { startNewSession: boolean; updates: Partial<AgentChatState> } => {
  if (checked && state.sessionHoldsDataResults) {
    return {
      startNewSession: true,
      updates: { ...optInFlags(state), [key]: true, sessionNotice: NEW_SESSION_NOTICE },
    };
  }
  return { startNewSession: false, updates: { [key]: checked } };
};

/** The new session id carried by a session_replaced event, or null. */
export const replacedSessionId = (content: string | null | undefined): string | null => {
  if (!content) return null;
  try {
    const parsed = JSON.parse(content) as { sessionId?: unknown };
    return typeof parsed.sessionId === 'string' && parsed.sessionId ? parsed.sessionId : null;
  } catch {
    return null;
  }
};

/**
 * The messages to show once a turn moved to ``newSessionId``: only the user
 * message that started it, since the new session holds nothing earlier.
 */
export const messagesForNewSession = (messages: ChatMessage[], newSessionId: string): ChatMessage[] => {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'user') return [{ ...messages[i], sessionId: newSessionId }];
  }
  return [];
};
