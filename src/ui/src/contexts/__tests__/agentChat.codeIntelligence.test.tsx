// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The "Enable Code Intelligence Agent" checkbox must start unchecked.
 *
 * The Code Intelligence Agent routes queries to a third-party MCP service
 * (DeepWiki), so it is opt-in: a fresh session — and a session reset — must
 * leave it off until the user checks the box themselves.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';

import { AgentChatProvider, useAgentChatContext } from '../agentChat';

const wrapper = ({ children }: { children: React.ReactNode }): React.JSX.Element => <AgentChatProvider>{children}</AgentChatProvider>;

describe('AgentChatProvider code intelligence default', () => {
  it('starts with code intelligence disabled', () => {
    const { result } = renderHook(() => useAgentChatContext(), { wrapper });
    expect(result.current.agentChatState.enableCodeIntelligence).toBe(false);
  });

  it('keeps the user opt-in for the rest of the session', () => {
    const { result } = renderHook(() => useAgentChatContext(), { wrapper });
    act(() => result.current.updateAgentChatState({ enableCodeIntelligence: true }));
    expect(result.current.agentChatState.enableCodeIntelligence).toBe(true);
  });

  it('reverts to disabled when the session is reset', () => {
    const { result } = renderHook(() => useAgentChatContext(), { wrapper });
    act(() => result.current.updateAgentChatState({ enableCodeIntelligence: true }));
    act(() => result.current.resetAgentChatState());
    expect(result.current.agentChatState.enableCodeIntelligence).toBe(false);
  });

  it('starts with External MCP agents disabled, and a reset turns them off again', () => {
    const { result } = renderHook(() => useAgentChatContext(), { wrapper });
    expect(result.current.agentChatState.enableExternalMcpAgents).toBe(false);
    act(() => result.current.updateAgentChatState({ enableExternalMcpAgents: true }));
    act(() => result.current.resetAgentChatState());
    expect(result.current.agentChatState.enableExternalMcpAgents).toBe(false);
  });

  it('starts a new session with the overrides applied', () => {
    const { result } = renderHook(() => useAgentChatContext(), { wrapper });
    const before = result.current.agentChatState.sessionId;
    act(() => result.current.updateAgentChatState({ sessionHoldsDataResults: true }));
    act(() => result.current.startNewAgentChatSession({ enableExternalMcpAgents: true, sessionNotice: 'n' }));
    const state = result.current.agentChatState;
    expect(state.sessionId).not.toBe(before);
    expect(state.sessionHoldsDataResults).toBe(false);
    expect(state.enableExternalMcpAgents).toBe(true);
    expect(state.enableCodeIntelligence).toBe(false);
    expect(state.sessionNotice).toBe('n');
  });
});
