// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { createContext, useContext, useState, useCallback, useMemo } from 'react';
import { v4 as uuidv4 } from 'uuid';
import type { AgentChatState, AgentChatContextValue, ChatMessage, ChatMode } from '../types/agent-chat';

const AgentChatContext = createContext<AgentChatContextValue | null>(null);

interface AgentChatProviderProps {
  children: React.ReactNode;
  initialMode?: ChatMode;
}

export const AgentChatProvider = ({ children, initialMode = 'chat' }: AgentChatProviderProps): React.JSX.Element => {
  // State for the agent chat
  const [agentChatState, setAgentChatState] = useState<AgentChatState>({
    messages: [], // Current chat messages
    sessionId: uuidv4(), // Current session ID
    isLoading: false,
    waitingForResponse: false,
    error: null,
    expandedSections: new Set(),
    lastMessageCount: 0,
    // Off by default: the Code Intelligence Agent sends repository context to a
    // third-party MCP service, so it must be an explicit user opt-in.
    enableCodeIntelligence: false,
    // Off by default for the same reason: External MCP agents send their query
    // to a server outside the deployment.
    enableExternalMcpAgents: false,
    sessionHoldsDataResults: false,
    sessionNotice: null,
    inputValue: '',
    mode: initialMode,
  });

  // Function to update agent chat state
  const updateAgentChatState = useCallback((updates: Partial<AgentChatState>) => {
    setAgentChatState((prevState) => ({
      ...prevState,
      ...updates,
    }));
  }, []);

  // Start a new, empty session. ``overrides`` are applied to the fresh state, so a
  // caller can carry settings over (see startNewSessionForOptIn in use-agent-chat).
  const startNewAgentChatSession = useCallback((overrides: Partial<AgentChatState> = {}) => {
    setAgentChatState((prevState) => ({
      messages: [],
      sessionId: uuidv4(),
      isLoading: false,
      waitingForResponse: false,
      error: null,
      expandedSections: new Set(),
      lastMessageCount: 0,
      enableCodeIntelligence: false,
      enableExternalMcpAgents: false,
      sessionHoldsDataResults: false,
      sessionNotice: null,
      inputValue: '',
      mode: prevState.mode,
      ...overrides,
    }));
  }, []);

  // Function to reset agent chat state (new session)
  const resetAgentChatState = useCallback(() => startNewAgentChatSession(), [startNewAgentChatSession]);

  // Function to load a specific session
  const loadAgentChatSession = useCallback((sessionId: string, messages: ChatMessage[]) => {
    setAgentChatState((prevState) => ({
      ...prevState,
      messages,
      sessionId,
      expandedSections: new Set(),
      lastMessageCount: messages.length,
      error: null,
      waitingForResponse: false,
      isLoading: false,
      // Not known for a loaded session until the server reports it.
      sessionHoldsDataResults: false,
      sessionNotice: null,
    }));
  }, []);

  // Function to add a message to the current session
  const addMessageToSession = useCallback((message: ChatMessage) => {
    setAgentChatState((prevState) => ({
      ...prevState,
      messages: [...prevState.messages, message],
    }));
  }, []);

  // Function to update messages (for streaming updates)
  const updateMessages = useCallback((updaterFunction: (messages: ChatMessage[]) => ChatMessage[]) => {
    setAgentChatState((prevState) => ({
      ...prevState,
      messages: updaterFunction(prevState.messages),
    }));
  }, []);

  const contextValue = useMemo(
    () => ({
      agentChatState,
      updateAgentChatState,
      resetAgentChatState,
      startNewAgentChatSession,
      loadAgentChatSession,
      addMessageToSession,
      updateMessages,
    }),
    [
      agentChatState,
      updateAgentChatState,
      resetAgentChatState,
      startNewAgentChatSession,
      loadAgentChatSession,
      addMessageToSession,
      updateMessages,
    ],
  );

  return <AgentChatContext.Provider value={contextValue}>{children}</AgentChatContext.Provider>;
};

export const useAgentChatContext = (): AgentChatContextValue => {
  const context = useContext(AgentChatContext);
  if (!context) {
    throw new Error('useAgentChatContext must be used within an AgentChatProvider');
  }
  return context;
};

export default AgentChatContext;
