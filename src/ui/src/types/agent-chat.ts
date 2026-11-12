// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

export interface ToolMetadata {
  toolUseId: string;
  toolName: string;
}

export interface ToolUseData {
  type?: string;
  toolContent?: string;
  sessionMessages?: ChatMessage[];
  tools?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  messageType: string;
  toolUseData?: ToolUseData | null;
  isProcessing: boolean;
  sessionId: string;
  timestamp: number;
  id: string | number;
  parsedData?: {
    responseType: string;
    data: unknown;
    textContent: string;
  } | null;
  bedrockErrorInfo?: Record<string, unknown>;
  toolMetadata?: ToolMetadata;
  toolUseId?: string;
  toolName?: string;
  executionLoading?: boolean;
  executionDetails?: string | null;
  resultLoading?: boolean;
  resultDetails?: string | null;
  awaitingStructuredData?: boolean;
}

export type ChatMode = 'chat' | 'quick_start';

export interface AgentChatState {
  messages: ChatMessage[];
  sessionId: string;
  isLoading: boolean;
  waitingForResponse: boolean;
  error: string | null;
  expandedSections: Set<string>;
  lastMessageCount: number;
  enableCodeIntelligence: boolean;
  /** Off by default: External MCP agents send their query outside the deployment. */
  enableExternalMcpAgents: boolean;
  /** The server reported that this session holds Analytics or Error Analyzer results. */
  sessionHoldsDataResults: boolean;
  /** Short notice shown above the chat input, e.g. why a new session started. */
  sessionNotice: string | null;
  inputValue: string;
  mode: ChatMode;
}

export interface AgentChatContextValue {
  agentChatState: AgentChatState;
  updateAgentChatState: (updates: Partial<AgentChatState>) => void;
  resetAgentChatState: () => void;
  /** Start a new, empty session, applying ``overrides`` to the fresh state. */
  startNewAgentChatSession: (overrides?: Partial<AgentChatState>) => void;
  loadAgentChatSession: (sessionId: string, messages: ChatMessage[]) => void;
  addMessageToSession: (message: ChatMessage) => void;
  updateMessages: (updaterFunction: (messages: ChatMessage[]) => ChatMessage[]) => void;
}
