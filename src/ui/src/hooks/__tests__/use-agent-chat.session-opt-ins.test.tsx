// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Opt-in agents and sessions that hold Analytics or Error Analyzer results.
 *
 * The server reports a session holding data results with a `session_data_results`
 * stream event and moves an opt-in turn on such a session with `session_replaced`.
 * These tests drive the real hook and context with the stream mocked, and assert:
 * both opt-ins are sent on both transports, opting in after the flag starts a new
 * empty session with a notice, and a server-side move is followed.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';

const { graphql, streamChat, transport } = vi.hoisted(() => ({
  graphql: vi.fn(),
  streamChat: vi.fn(),
  transport: { streamUrl: 'https://stream.example/' as string | undefined },
}));

vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('../../api/stream-client', () => ({ streamChat }));
vi.mock('../../api/chat-poll', () => ({
  fetchAssistantKeys: vi.fn(async () => new Set<string>()),
  pollForAssistantReply: vi.fn(async ({ sessionId }: { sessionId: string }) => ({
    content: `reply in ${sessionId}`,
    timestamp: 1,
  })),
}));
vi.mock('../../aws-exports', () => ({
  get streamUrl() {
    return transport.streamUrl;
  },
}));
vi.mock('../use-current-session-creds', () => ({
  default: () => ({ currentCredentials: { accessKeyId: 'ASIASCOPEDKEYEXAMPLE', secretAccessKey: 'x' } }),
}));
vi.mock('../../graphql/generated', () => ({
  sendAgentChatMessage: 'sendAgentChatMessage',
  onAgentChatMessageUpdate: 'onAgentChatMessageUpdate',
  getChatMessages: 'getChatMessages',
}));

import { NEW_SESSION_NOTICE } from '../../components/agent-chat/session-opt-ins';

// Imported after vi.resetModules so the hook reads the transport mocked for the
// test; the context comes from the same module graph as the hook.
const renderChat = async () => {
  const { default: useAgentChat } = await import('../use-agent-chat');
  const { AgentChatProvider, useAgentChatContext } = await import('../../contexts/agentChat');
  const wrapper = ({ children }: { children: React.ReactNode }): React.JSX.Element => <AgentChatProvider>{children}</AgentChatProvider>;
  return renderHook(() => ({ chat: useAgentChat(), ctx: useAgentChatContext() }), { wrapper });
};

const serverEvent = (sessionId: string, method: string, content = '') => ({
  sessionId,
  role: 'assistant',
  content,
  messageType: method,
  method,
  isProcessing: true,
});

beforeEach(() => {
  vi.resetModules();
  graphql.mockReset();
  streamChat.mockReset();
  transport.streamUrl = 'https://stream.example/';
});

afterEach(() => cleanup());

describe('opt-in agents per session', () => {
  it('starts with both opt-ins off and no flag', async () => {
    const { result } = await renderChat();
    const state = result.current.ctx.agentChatState;
    expect(state.enableCodeIntelligence).toBe(false);
    expect(state.enableExternalMcpAgents).toBe(false);
    expect(state.sessionHoldsDataResults).toBe(false);
  });

  it('sends the External MCP opt-in on the streaming path', async () => {
    streamChat.mockResolvedValue(undefined);
    const { result } = await renderChat();
    await act(async () => {
      await result.current.chat.sendMessage('hi', { enableCodeIntelligence: false, enableExternalMcpAgents: true });
    });
    expect(streamChat.mock.calls[0][0].body).toMatchObject({ enableCodeIntelligence: false, enableExternalMcpAgents: true });
  });

  it('opting in after a data result starts a new, empty session with a notice', async () => {
    const { result } = await renderChat();
    const sid = result.current.ctx.agentChatState.sessionId;
    streamChat.mockImplementation(async ({ onEvent }: { onEvent: (e: unknown) => void }) => {
      onEvent(serverEvent(sid, 'session_data_results'));
    });
    await act(async () => {
      await result.current.chat.sendMessage('how many documents?');
    });
    expect(result.current.ctx.agentChatState.sessionHoldsDataResults).toBe(true);
    expect(result.current.ctx.agentChatState.messages.length).toBeGreaterThan(0);

    act(() => result.current.chat.setOptIn('enableCodeIntelligence', true));

    const state = result.current.ctx.agentChatState;
    expect(state.sessionId).not.toBe(sid);
    expect(state.messages).toEqual([]);
    expect(state.enableCodeIntelligence).toBe(true);
    expect(state.sessionHoldsDataResults).toBe(false);
    expect(state.sessionNotice).toBe(NEW_SESSION_NOTICE);
  });

  it('opting in on a session with no data result keeps the session', async () => {
    const { result } = await renderChat();
    const sid = result.current.ctx.agentChatState.sessionId;
    act(() => result.current.chat.setOptIn('enableExternalMcpAgents', true));
    expect(result.current.ctx.agentChatState.sessionId).toBe(sid);
    expect(result.current.ctx.agentChatState.enableExternalMcpAgents).toBe(true);
    expect(result.current.ctx.agentChatState.sessionNotice).toBeNull();
  });

  it('follows a session_replaced event to the new session', async () => {
    const { result } = await renderChat();
    const sid = result.current.ctx.agentChatState.sessionId;
    streamChat.mockImplementation(async ({ onEvent }: { onEvent: (e: unknown) => void }) => {
      onEvent(serverEvent('new-sid', 'session_replaced', JSON.stringify({ sessionId: 'new-sid', previousSessionId: sid })));
    });
    await act(async () => {
      await result.current.chat.sendMessage('explain the code', { enableCodeIntelligence: true });
    });
    const state = result.current.ctx.agentChatState;
    expect(state.sessionId).toBe('new-sid');
    expect(state.sessionNotice).toBe(NEW_SESSION_NOTICE);
    expect(state.messages.map((m) => [m.role, m.content, m.sessionId])).toEqual([['user', 'explain the code', 'new-sid']]);
  });

  it('follows a new session id returned on the polling path', async () => {
    transport.streamUrl = undefined;
    graphql.mockResolvedValue({ data: { sendAgentChatMessage: { sessionId: 'polled-sid' } } });
    const { result } = await renderChat();
    await act(async () => {
      await result.current.chat.sendMessage('explain the code', { enableCodeIntelligence: true, enableExternalMcpAgents: true });
    });
    expect(graphql.mock.calls[0][0].variables).toMatchObject({ enableCodeIntelligence: true, enableExternalMcpAgents: true });
    const state = result.current.ctx.agentChatState;
    expect(state.sessionId).toBe('polled-sid');
    expect(state.sessionNotice).toBe(NEW_SESSION_NOTICE);
    expect(state.messages.at(-1)?.content).toBe('reply in polled-sid');
  });
});
