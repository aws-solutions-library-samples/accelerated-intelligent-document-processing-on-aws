// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef } from 'react';
import { generateClient } from '../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import { sendAgentChatMessage, onAgentChatMessageUpdate, getChatMessages } from '../graphql/generated';
import { useAgentChatContext } from '../contexts/agentChat';
import type { ChatMessage } from '../types/agent-chat';
import { streamChat, type StreamCredentials, type StreamEvent } from '../api/stream-client';
import { pollForAssistantReply, fetchAssistantKeys } from '../api/chat-poll';
import { streamUrl } from '../aws-exports';
import useCurrentSessionCreds from './use-current-session-creds';
import {
  SESSION_DATA_RESULTS_EVENT,
  SESSION_REPLACED_EVENT,
  NEW_SESSION_NOTICE,
  messagesForNewSession,
  optInChange,
  replacedSessionId,
  type OptInKey,
} from '../components/agent-chat/session-opt-ins';

const logger = new ConsoleLogger('useAgentChat');
const client = generateClient();
// Chat responses are delivered by streaming from the Lambda Function URL when
// one is configured (VITE_STREAM_URL). When it is absent — e.g. AWS GovCloud,
// where Lambda Function URLs do not exist — we fall back to sending the message
// over the REST API and POLLING getChatMessages for the final answer (no live
// streaming; see api/chat-poll.ts). Auto-detected so commercial keeps streaming
// and GovCloud "just works" with no extra flag.
const streamingAvailable = Boolean(streamUrl);
const useHttpApiTransport = true;

interface AgentChatConfig {
  agentType: string;
  // TODO: mutation/subscription are typed as string, losing branded GraphQL type info.
  // This is a known tradeoff of the dynamic config pattern - the default config always wins.
  mutation: string;
  subscription: string;
  method: string;
}

/** Represents a GraphQL subscription that can be subscribed to and unsubscribed from. */
interface GraphQLSubscribable {
  subscribe: (handlers: { next: (value: { data: Record<string, unknown> }) => void; error: (err: unknown) => void }) => {
    unsubscribe: () => void;
  };
}

interface SendOptions {
  enableCodeIntelligence?: boolean;
  enableExternalMcpAgents?: boolean;
}

interface UseAgentChatReturn {
  messages: ChatMessage[];
  isLoading: boolean;
  waitingForResponse: boolean;
  error: string | null;
  sessionId: string;
  sendMessage: (prompt: string, options?: SendOptions) => Promise<unknown>;
  /** Change an opt-in checkbox; in a session holding data results this starts a new session. */
  setOptIn: (key: OptInKey, checked: boolean) => void;
  cancelResponse: () => void;
  clearError: () => void;
  clearChat: () => void;
  loadChatSession: (targetSessionId: string, existingMessages?: ChatMessage[] | null) => Promise<void>;
  agentConfig: AgentChatConfig;
}

const useAgentChat = (config: Partial<AgentChatConfig> = {}): UseAgentChatReturn => {
  // Default configuration for backward compatibility
  const defaultConfig: AgentChatConfig = {
    agentType: 'idp-help',
    mutation: sendAgentChatMessage,
    subscription: onAgentChatMessageUpdate,
    method: 'chat',
  };

  const agentConfig: AgentChatConfig = { ...defaultConfig, ...config };

  // Use context for persistent state
  const {
    agentChatState,
    updateAgentChatState,
    resetAgentChatState,
    startNewAgentChatSession,
    updateMessages: updateMessagesTyped,
  } = useAgentChatContext();
  const updateMessages = updateMessagesTyped;

  // Extract state from context
  const { messages, isLoading, waitingForResponse, error, sessionId } = agentChatState;

  const sentMessagesRef = useRef(new Set<string>());

  // AbortController for the non-streaming poll path, so an in-flight poll is
  // cancelled on cancelResponse()/unmount rather than running for the full
  // timeout against a switched or unmounted session.
  const pollAbortRef = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      pollAbortRef.current?.abort();
    },
    [],
  );

  // Cognito Identity Pool credentials for SigV4-signing the streaming request
  // (httpapi transport only). Unused under the appsync transport.
  // The caller's identity is not sent in the body: streamChat presents the
  // Cognito ID token and the endpoint takes the caller from it.
  const { currentCredentials } = useCurrentSessionCreds({});
  const credentialsRef = useRef<unknown>(undefined);
  credentialsRef.current = currentCredentials;

  // Handle tool execution start messages - creates standalone tool message chronologically
  const handleToolExecutionStart = (newMessage: ChatMessage): void => {
    const toolMetadata = newMessage.toolMetadata ?? { toolUseId: '', toolName: '' };

    updateMessages((prevMessages) => {
      const updatedMessages = [...prevMessages];

      // Find active tool_use session
      const activeToolUseIndex = updatedMessages.findIndex(
        (msg) => msg.role === 'assistant' && msg.isProcessing && msg.sessionId === newMessage.sessionId && msg.messageType === 'tool_use',
      );

      // Create the tool message object
      const toolMessage: ChatMessage = {
        role: 'assistant',
        content: '',
        messageType: 'unified_tool',
        toolUseId: toolMetadata.toolUseId,
        toolName: toolMetadata.toolName,
        executionLoading: true,
        executionDetails: null,
        resultLoading: false,
        resultDetails: null,
        isProcessing: false,
        sessionId: newMessage.sessionId,
        timestamp: newMessage.timestamp,
        id: `unified-tool-${toolMetadata.toolUseId}`,
      };

      // If there's an active tool_use session, add to its sessionMessages
      if (activeToolUseIndex >= 0) {
        updatedMessages[activeToolUseIndex] = {
          ...updatedMessages[activeToolUseIndex],
          toolUseData: {
            ...updatedMessages[activeToolUseIndex].toolUseData,
            sessionMessages: [...(updatedMessages[activeToolUseIndex].toolUseData?.sessionMessages || []), toolMessage],
          },
        };
        return updatedMessages;
      }

      // Otherwise, check if this tool already exists as standalone to prevent duplicates
      const existingToolIndex = updatedMessages.findIndex(
        (msg) => msg.messageType === 'unified_tool' && msg.toolUseId === toolMetadata.toolUseId,
      );

      if (existingToolIndex >= 0) {
        // Update existing tool to reset its state
        updatedMessages[existingToolIndex] = {
          ...updatedMessages[existingToolIndex],
          executionLoading: true,
          timestamp: newMessage.timestamp,
        };
        return updatedMessages;
      }

      // Finalize any currently streaming message before adding tool
      const finalizedMessages = updatedMessages.map((msg) => {
        if (msg.role === 'assistant' && msg.isProcessing && msg.sessionId === newMessage.sessionId && msg.messageType !== 'tool_use') {
          return { ...msg, isProcessing: false };
        }
        return msg;
      });

      // Add as standalone tool message
      return [...finalizedMessages, toolMessage];
    });
  };

  // Handle tool execution complete messages - updates execution phase
  const handleToolExecutionComplete = (newMessage: ChatMessage): void => {
    const toolMetadata = newMessage.toolMetadata ?? { toolUseId: '', toolName: '' };

    updateMessages((prevMessages) => {
      return prevMessages.map((msg) => {
        // Check standalone tools first
        if (msg.messageType === 'unified_tool' && msg.toolUseId === toolMetadata.toolUseId) {
          return {
            ...msg,
            executionLoading: false,
            executionDetails: newMessage.content,
            timestamp: newMessage.timestamp,
          };
        }

        // Check tools within agent sessionMessages
        if (msg.messageType === 'tool_use' && msg.toolUseData?.sessionMessages) {
          const updatedSessionMessages = msg.toolUseData.sessionMessages.map((sessionMsg: ChatMessage) => {
            if (sessionMsg.messageType === 'unified_tool' && sessionMsg.toolUseId === toolMetadata.toolUseId) {
              return {
                ...sessionMsg,
                executionLoading: false,
                executionDetails: newMessage.content,
                timestamp: newMessage.timestamp,
              };
            }
            return sessionMsg;
          });

          return {
            ...msg,
            toolUseData: {
              ...msg.toolUseData,
              sessionMessages: updatedSessionMessages,
            },
          };
        }

        // Check nested tools within agents (legacy)
        if (msg.messageType === 'tool_use' && msg.toolUseData?.tools) {
          const updatedTools = msg.toolUseData.tools.map((tool: Record<string, unknown>) => {
            if (tool.toolUseId === toolMetadata.toolUseId) {
              return {
                ...tool,
                executionLoading: false,
                executionDetails: newMessage.content,
                timestamp: newMessage.timestamp,
              };
            }
            return tool;
          });

          return {
            ...msg,
            toolUseData: {
              ...msg.toolUseData,
              tools: updatedTools,
            },
          };
        }

        return msg;
      });
    });
  };

  // Handle tool result start messages - updates result loading phase
  const handleToolResultStart = (newMessage: ChatMessage): void => {
    const toolMetadata = newMessage.toolMetadata ?? { toolUseId: '', toolName: '' };

    updateMessages((prevMessages) => {
      return prevMessages.map((msg) => {
        // Check standalone tools first
        if (msg.messageType === 'unified_tool' && msg.toolUseId === toolMetadata.toolUseId) {
          return {
            ...msg,
            resultLoading: true,
            timestamp: newMessage.timestamp,
          };
        }

        // Check tools within agent sessionMessages
        if (msg.messageType === 'tool_use' && msg.toolUseData?.sessionMessages) {
          const updatedSessionMessages = msg.toolUseData.sessionMessages.map((sessionMsg: ChatMessage) => {
            if (sessionMsg.messageType === 'unified_tool' && sessionMsg.toolUseId === toolMetadata.toolUseId) {
              return {
                ...sessionMsg,
                resultLoading: true,
                timestamp: newMessage.timestamp,
              };
            }
            return sessionMsg;
          });

          return {
            ...msg,
            toolUseData: {
              ...msg.toolUseData,
              sessionMessages: updatedSessionMessages,
            },
          };
        }

        // Check nested tools within agents (legacy)
        if (msg.messageType === 'tool_use' && msg.toolUseData?.tools) {
          const updatedTools = msg.toolUseData.tools.map((tool: Record<string, unknown>) => {
            if (tool.toolUseId === toolMetadata.toolUseId) {
              return {
                ...tool,
                resultLoading: true,
                timestamp: newMessage.timestamp,
              };
            }
            return tool;
          });

          return {
            ...msg,
            toolUseData: {
              ...msg.toolUseData,
              tools: updatedTools,
            },
          };
        }

        return msg;
      });
    });
  };

  // Handle tool result complete messages - completes result phase
  const handleToolResultComplete = (newMessage: ChatMessage): void => {
    const toolMetadata = newMessage.toolMetadata ?? { toolUseId: '', toolName: '' };

    updateMessages((prevMessages) => {
      return prevMessages.map((msg) => {
        // Check standalone tools first
        if (msg.messageType === 'unified_tool' && msg.toolUseId === toolMetadata.toolUseId) {
          return {
            ...msg,
            resultLoading: false,
            resultDetails: newMessage.content,
            timestamp: newMessage.timestamp,
          };
        }

        // Check tools within agent sessionMessages
        if (msg.messageType === 'tool_use' && msg.toolUseData?.sessionMessages) {
          const updatedSessionMessages = msg.toolUseData.sessionMessages.map((sessionMsg: ChatMessage) => {
            if (sessionMsg.messageType === 'unified_tool' && sessionMsg.toolUseId === toolMetadata.toolUseId) {
              return {
                ...sessionMsg,
                resultLoading: false,
                resultDetails: newMessage.content,
                timestamp: newMessage.timestamp,
              };
            }
            return sessionMsg;
          });

          return {
            ...msg,
            toolUseData: {
              ...msg.toolUseData,
              sessionMessages: updatedSessionMessages,
            },
          };
        }

        // Check nested tools within agents (legacy)
        if (msg.messageType === 'tool_use' && msg.toolUseData?.tools) {
          const updatedTools = msg.toolUseData.tools.map((tool: Record<string, unknown>) => {
            if (tool.toolUseId === toolMetadata.toolUseId) {
              return {
                ...tool,
                resultLoading: false,
                resultDetails: newMessage.content,
                timestamp: newMessage.timestamp,
              };
            }
            return tool;
          });

          return {
            ...msg,
            toolUseData: {
              ...msg.toolUseData,
              tools: updatedTools,
            },
          };
        }

        return msg;
      });
    });
  };

  // Parse JSON from message content and extract responseType
  const parseResponseData = (content: string): { responseType: string; data: unknown; textContent: string } | null => {
    try {
      // Look for JSON objects containing responseType with proper bracket matching
      const findJsonWithResponseType = (text: string): string | null => {
        const startIndex = text.indexOf('"responseType"');
        if (startIndex === -1) return null;

        // Find the opening brace before responseType
        let openBraceIndex = -1;
        for (let i = startIndex; i >= 0; i -= 1) {
          if (text[i] === '{') {
            openBraceIndex = i;
            break;
          }
        }

        if (openBraceIndex === -1) return null;

        // Find the matching closing brace
        let braceCount = 0;
        let closeBraceIndex = -1;

        for (let j = openBraceIndex; j < text.length; j += 1) {
          if (text[j] === '{') {
            braceCount += 1;
          } else if (text[j] === '}') {
            braceCount -= 1;
            if (braceCount === 0) {
              closeBraceIndex = j;
              break;
            }
          }
        }

        if (closeBraceIndex === -1) return null;

        return text.substring(openBraceIndex, closeBraceIndex + 1);
      };

      const jsonStr = findJsonWithResponseType(content);

      if (jsonStr) {
        const parsed = JSON.parse(jsonStr);

        if (parsed.responseType) {
          // Remove the JSON from the original content to get text content
          const textContent = content.replace(jsonStr, '').trim();

          let processedData;

          if (parsed.responseType === 'plotData') {
            // Format plotData to match PlotDisplay component expectations
            processedData = {
              data: parsed.data || parsed,
              options: parsed.options || {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                  title: {
                    display: true,
                    text: parsed.title || 'Chart',
                  },
                },
              },
              type: parsed.type || 'line',
            };
          } else {
            // For other types (table, etc.), use data as-is
            processedData = parsed.data || parsed.tableData || parsed.plotData || parsed;
          }

          return {
            responseType: parsed.responseType,
            data: processedData,
            textContent,
          };
        }
      }

      return null;
    } catch (parseError) {
      console.warn('Failed to parse response data:', parseError);
      return null;
    }
  };

  // Add a ref to track when we're in structured data mode (suppressing intermediate messages)
  const structuredDataModeRef = useRef(false);

  // Parse Bedrock error information from message content
  const parseBedrockerrorInfo = (content: string): Record<string, unknown> | null => {
    try {
      const parsed = JSON.parse(content);
      if (parsed.type === 'bedrock_error' && parsed.errorInfo) {
        return parsed.errorInfo;
      }
    } catch (e) {
      // Not JSON or not a Bedrock error
    }
    return null;
  };

  // Handle streaming messages with proper phase management
  const handleStreamingMessage = (newMessage: ChatMessage): void => {
    // Handle new tool message types using the messageType field from GraphQL
    if (newMessage.messageType === 'tool_execution_start') {
      return handleToolExecutionStart(newMessage);
    }
    if (newMessage.messageType === 'tool_execution_complete') {
      return handleToolExecutionComplete(newMessage);
    }
    if (newMessage.messageType === 'tool_result_start') {
      return handleToolResultStart(newMessage);
    }
    if (newMessage.messageType === 'tool_result_complete') {
      return handleToolResultComplete(newMessage);
    }

    // Handle structured data start - enter suppression mode
    if (newMessage.messageType === 'structured_data_start') {
      structuredDataModeRef.current = true;

      // Add a placeholder message to show we're generating the final result
      updateMessages((prevMessages) => {
        const placeholderMessage: ChatMessage = {
          role: 'assistant',
          content: 'Generating final result...',
          messageType: 'text',
          toolUseData: null,
          isProcessing: true,
          sessionId: newMessage.sessionId,
          timestamp: newMessage.timestamp,
          id: `${newMessage.timestamp}-generating`,
        };

        return [...prevMessages, placeholderMessage];
      });

      return; // Don't add the structured_data_start message to UI
    }

    // Handle final response - exit suppression mode and show final message
    if (newMessage.messageType === 'assistant_final_response' || (!newMessage.isProcessing && newMessage.role === 'assistant')) {
      structuredDataModeRef.current = false;

      updateMessages((prevMessages) => {
        // Check if this is a Bedrock error message
        const bedrockErrorInfo = parseBedrockerrorInfo(newMessage.content);

        // Parse the final message content for responseType (tables, charts, etc.)
        const parsedData = parseResponseData(newMessage.content);

        // Create the final message
        const finalMessage: ChatMessage = {
          role: 'assistant',
          content: newMessage.content,
          messageType: bedrockErrorInfo ? 'bedrock_error' : 'text',
          toolUseData: null,
          isProcessing: false,
          sessionId: newMessage.sessionId,
          timestamp: newMessage.timestamp,
          id: newMessage.timestamp,
        };

        // Add Bedrock error info if available
        if (bedrockErrorInfo) {
          finalMessage.bedrockErrorInfo = bedrockErrorInfo;
          finalMessage.content = (bedrockErrorInfo.message as string) || newMessage.content; // Use user-friendly message
        }
        // Add parsed data if available
        else if (parsedData) {
          finalMessage.parsedData = parsedData;
          finalMessage.content = parsedData.textContent || newMessage.content;
        }

        const updatedMessages = [...prevMessages];
        const placeholderIndex = updatedMessages.findIndex(
          (msg) => msg.content === 'Generating final result...' && msg.sessionId === newMessage.sessionId && msg.isProcessing,
        );

        if (placeholderIndex >= 0) {
          // Replace the placeholder with the final message
          updatedMessages[placeholderIndex] = finalMessage;
          return updatedMessages;
        }

        // Check if we already have a final message with the same timestamp to prevent duplicates
        const existingFinalIndex = updatedMessages.findIndex(
          (msg: ChatMessage) =>
            msg.role === 'assistant' &&
            !msg.isProcessing &&
            msg.sessionId === newMessage.sessionId &&
            msg.timestamp === newMessage.timestamp,
        );

        if (existingFinalIndex >= 0) {
          // Update existing final message instead of creating duplicate
          updatedMessages[existingFinalIndex] = finalMessage;
          return updatedMessages;
        }

        // Find any processing messages to update instead of adding new message
        const processingMessageIndex = updatedMessages.findIndex(
          (msg) => msg.role === 'assistant' && msg.isProcessing && msg.sessionId === newMessage.sessionId,
        );

        if (processingMessageIndex >= 0) {
          // Update the existing processing message to final state
          updatedMessages[processingMessageIndex] = {
            ...updatedMessages[processingMessageIndex],
            ...finalMessage,
            id: updatedMessages[processingMessageIndex].id, // Keep original ID
          };
          return updatedMessages;
        }

        // Only add as new message if no existing processing message found
        return [...updatedMessages, finalMessage];
      });

      // Mark processing as complete and remove loading indicators
      updateAgentChatState({
        waitingForResponse: false,
        isLoading: false,
      });
      return;
    }

    // If we're in structured data mode, suppress intermediate messages except subagent events
    if (structuredDataModeRef.current) {
      const hasSubagentStart = newMessage.content.includes('"type": "subagent_start"');
      const hasSubagentEnd = newMessage.content.includes('"type": "subagent_end"');

      // Allow subagent messages through for tool display
      if (hasSubagentStart || hasSubagentEnd) {
        // Continue with normal subagent handling below
      } else {
        // Suppress all other intermediate messages
        return;
      }
    }

    updateMessages((prevMessages) => {
      // Check if this message contains subagent markers
      const hasSubagentStart = newMessage.content.includes('"type": "subagent_start"');
      const hasSubagentEnd = newMessage.content.includes('"type": "subagent_end"');

      if (hasSubagentStart) {
        // This message contains subagent_start - parse it and create tool use message
        try {
          const startRegex = /\{[^{}]*"type":\s*"subagent_start"[^{}]*\}/;
          const startMatch = newMessage.content.match(startRegex);

          if (startMatch) {
            const startData = JSON.parse(startMatch[0]);

            // Find the last regular message to finalize it
            const lastRegularIndex = prevMessages.findIndex(
              (msg) =>
                msg.role === 'assistant' && msg.isProcessing && msg.sessionId === newMessage.sessionId && msg.messageType !== 'tool_use',
            );

            const updatedMessages = [...prevMessages];

            if (lastRegularIndex >= 0) {
              // Finalize the last regular message
              updatedMessages[lastRegularIndex] = {
                ...updatedMessages[lastRegularIndex],
                isProcessing: false,
              };
            }

            // Create new tool use message with an array to collect session messages
            const toolUseMessage: ChatMessage = {
              role: 'assistant',
              content: '',
              messageType: 'tool_use',
              toolUseData: {
                ...startData,
                toolContent: '',
                sessionMessages: [], // Array to collect all messages in this agent session
              },
              isProcessing: true,
              sessionId: newMessage.sessionId,
              timestamp: newMessage.timestamp,
              id: `${newMessage.timestamp}-tool`,
            };

            return [...updatedMessages, toolUseMessage];
          }
        } catch (e) {
          console.warn('Failed to parse subagent_start JSON:', e);
        }
      } else if (hasSubagentEnd) {
        // This message contains subagent_end - finalize tool use and potentially create new message
        const updatedMessages = [...prevMessages];

        // Find the current tool use message
        const toolUseIndex = updatedMessages.findIndex(
          (msg) => msg.role === 'assistant' && msg.isProcessing && msg.sessionId === newMessage.sessionId && msg.messageType === 'tool_use',
        );

        if (toolUseIndex >= 0) {
          // Mark tool use as complete (don't add the subagent_end JSON to content)
          updatedMessages[toolUseIndex] = {
            ...updatedMessages[toolUseIndex],
            isProcessing: false,
            timestamp: newMessage.timestamp,
          };

          // Extract any content after the subagent_end JSON
          const endRegex = /\{[^{}]*"type":\s*"subagent_end"[^{}]*\}/;
          const contentAfterEnd = newMessage.content.replace(endRegex, '').trim();

          // If there's content after subagent_end, create a new streaming message
          if (contentAfterEnd) {
            const postToolMessage: ChatMessage = {
              role: 'assistant',
              content: contentAfterEnd,
              messageType: 'text',
              toolUseData: null,
              isProcessing: true,
              sessionId: newMessage.sessionId,
              timestamp: newMessage.timestamp,
              id: `${newMessage.timestamp}-post-tool`,
            };

            return [...updatedMessages, postToolMessage];
          }

          return updatedMessages;
        }
      }

      // Check if there's any tool message after the last finalized text message
      // This prevents continuing to stream into messages that existed before a tool
      let lastToolIndex = -1;
      for (let i = prevMessages.length - 1; i >= 0; i--) {
        if (prevMessages[i].messageType === 'unified_tool') {
          lastToolIndex = i;
          break;
        }
      }

      // Find existing streaming message for this session (but only if it comes after any tools)
      const existingStreamingIndex = prevMessages.findIndex((msg, index) => {
        return (
          msg.role === 'assistant' && msg.isProcessing && msg.sessionId === newMessage.sessionId && index > lastToolIndex // Only continue messages that come after the last tool
        );
      });

      if (existingStreamingIndex >= 0) {
        const updatedMessages = [...prevMessages];
        const existingMessage = updatedMessages[existingStreamingIndex];

        if (existingMessage.messageType === 'tool_use') {
          // Check if there's an existing text message in sessionMessages that's still streaming
          const sessionMessages = existingMessage.toolUseData?.sessionMessages || [];
          const lastSessionMsg = sessionMessages[sessionMessages.length - 1];

          if (lastSessionMsg && lastSessionMsg.messageType === 'text' && lastSessionMsg.isProcessing) {
            // Update the existing text message in sessionMessages
            const updatedSessionMessages = [...sessionMessages];
            updatedSessionMessages[updatedSessionMessages.length - 1] = {
              ...lastSessionMsg,
              content: lastSessionMsg.content + newMessage.content,
              timestamp: newMessage.timestamp,
            };

            updatedMessages[existingStreamingIndex] = {
              ...existingMessage,
              toolUseData: {
                ...existingMessage.toolUseData,
                sessionMessages: updatedSessionMessages,
              },
              timestamp: newMessage.timestamp,
            };
          } else {
            // Create a new text message in sessionMessages
            const newTextMessage: ChatMessage = {
              role: 'assistant',
              content: newMessage.content,
              messageType: 'text',
              isProcessing: true,
              sessionId: newMessage.sessionId,
              timestamp: newMessage.timestamp,
              id: `session-text-${newMessage.timestamp}`,
            };

            updatedMessages[existingStreamingIndex] = {
              ...existingMessage,
              toolUseData: {
                ...existingMessage.toolUseData,
                sessionMessages: [...sessionMessages, newTextMessage],
              },
              timestamp: newMessage.timestamp,
            };
          }

          return updatedMessages;
        }
        if (existingMessage.awaitingStructuredData) {
          // Don't accumulate content when awaiting structured data
          // Just update timestamp to keep message alive
          updatedMessages[existingStreamingIndex] = {
            ...existingMessage,
            timestamp: newMessage.timestamp,
          };

          return updatedMessages;
        }
        // Regular content streaming
        updatedMessages[existingStreamingIndex] = {
          ...existingMessage,
          content: existingMessage.content + newMessage.content,
          timestamp: newMessage.timestamp,
        };

        return updatedMessages;
      }

      // No existing streaming message or there's a recent tool, create new one
      return [
        ...prevMessages,
        {
          role: newMessage.role,
          content: newMessage.content,
          messageType: 'text',
          toolUseData: null,
          isProcessing: newMessage.isProcessing,
          sessionId: newMessage.sessionId,
          timestamp: newMessage.timestamp,
          id: newMessage.timestamp,
        },
      ];
    });
  };

  // The server moved this turn to a new session (it opted in to an agent that a
  // session holding data results does not get). Follow it there.
  const moveToSession = (newSessionId: string): void => {
    updateAgentChatState({
      sessionId: newSessionId,
      sessionHoldsDataResults: false,
      sessionNotice: NEW_SESSION_NOTICE,
    });
    updateMessages((prevMessages) => messagesForNewSession(prevMessages, newSessionId));
  };

  const setOptIn = (key: OptInKey, checked: boolean): void => {
    const { startNewSession, updates } = optInChange(agentChatState, key, checked);
    if (startNewSession) {
      startNewAgentChatSession(updates);
      sentMessagesRef.current = new Set();
      return;
    }
    updateAgentChatState(updates);
  };

  const addMessage = (newMessage: ChatMessage): void => {
    // Session-level events carry state, not a message to show.
    if (newMessage.messageType === SESSION_DATA_RESULTS_EVENT) {
      updateAgentChatState({ sessionHoldsDataResults: true });
      return;
    }
    if (newMessage.messageType === SESSION_REPLACED_EVENT) {
      const newSessionId = replacedSessionId(newMessage.content);
      if (newSessionId) moveToSession(newSessionId);
      return;
    }

    // Filter out messages with isProcessing=true and content containing responseType (JSON data)
    // BUT allow structured_data_start messages through
    if (
      newMessage.role === 'assistant' &&
      newMessage.isProcessing &&
      newMessage.content &&
      newMessage.content.includes('responseType') &&
      newMessage.messageType !== 'structured_data_start'
    ) {
      logger.debug('Filtering out JSON message with responseType during processing');
      return;
    }

    if (newMessage.role === 'assistant') {
      handleStreamingMessage(newMessage);
      return;
    }

    // For user messages, check if we already have this content in our sent messages
    // to avoid duplicates when the subscription echoes back our locally added message
    const messageKey = `${newMessage.sessionId}:${newMessage.content}`;

    if (sentMessagesRef.current.has(messageKey)) {
      return;
    }

    updateMessages((prevMessages) => [
      ...prevMessages,
      {
        ...newMessage,
        id: newMessage.timestamp,
      },
    ]);
  };

  // Subscribe to chat message updates (appsync transport only). Under httpapi
  // the agent response is streamed directly from the Function URL in
  // sendMessage, so there is no subscription to set up.
  useEffect(() => {
    if (useHttpApiTransport) return undefined;
    logger.info('Setting up GraphQL subscription for session:', sessionId);
    logger.info('Using agent config:', agentConfig);

    const subscription = (
      client.graphql({
        query: agentConfig.subscription,
        variables: { sessionId },
      }) as unknown as GraphQLSubscribable
    ).subscribe({
      next: ({ data }: { data: Record<string, unknown> }) => {
        const chatMessage = data?.onAgentChatMessageUpdate as ChatMessage | undefined;

        if (chatMessage) {
          addMessage(chatMessage);
        } else {
          console.log('No chat message in subscription data:', data);
        }
      },
      error: (err: unknown) => {
        logger.error('Subscription error:', err);
        updateAgentChatState({ error: 'Connection to chat service lost. Please refresh the page.' });
      },
    });

    return () => {
      if (subscription) {
        subscription.unsubscribe();
      }
    };
  }, [sessionId, agentConfig.subscription]);

  // Send a chat message
  const sendMessage = async (prompt: string, options: SendOptions = {}): Promise<unknown> => {
    if (!prompt.trim()) return undefined;

    updateAgentChatState({
      isLoading: true,
      waitingForResponse: true,
      error: null,
    });

    const messageKey = `${sessionId}:${prompt}`;
    sentMessagesRef.current.add(messageKey);

    const userMessage: ChatMessage = {
      role: 'user',
      content: prompt,
      messageType: 'text',
      toolUseData: null,
      isProcessing: false,
      sessionId,
      timestamp: Date.now(),
      id: `user-${Date.now()}`,
    };

    updateMessages((prevMessages) => [...prevMessages, userMessage]);

    try {
      if (useHttpApiTransport && streamingAvailable) {
        // Stream directly from the IAM-authed Lambda Function URL. Each SSE
        // event has the same shape onAgentChatMessageUpdate delivered, so we
        // feed them straight into addMessage. streamChat resolves when the
        // stream ends (final/error already applied via addMessage).
        const creds = credentialsRef.current as StreamCredentials | undefined;
        if (!creds?.accessKeyId) {
          throw new Error('No AWS credentials available for streaming.');
        }
        await streamChat({
          path: '/chat/agent',
          body: {
            prompt,
            sessionId,
            method: agentConfig.method,
            enableCodeIntelligence: options.enableCodeIntelligence,
            enableExternalMcpAgents: options.enableExternalMcpAgents,
          },
          credentials: creds,
          onEvent: (event: StreamEvent) => addMessage(event as unknown as ChatMessage),
        });
        return undefined;
      }

      if (useHttpApiTransport && !streamingAvailable) {
        // No Function URL (e.g. GovCloud): send the message over the REST API
        // (async-invokes the processor), then POLL getChatMessages for the
        // final assistant reply. No live streaming — the user sees a spinner
        // until the full answer lands. Baseline the existing assistant messages
        // BEFORE sending so the poll matches only the NEW reply (clock-
        // independent — no client/server timestamp comparison).
        let knownAssistantKeys = await fetchAssistantKeys(client, sessionId);
        pollAbortRef.current?.abort();
        pollAbortRef.current = new AbortController();

        const sent = (await client.graphql({
          query: agentConfig.mutation,
          variables: {
            prompt,
            sessionId,
            method: agentConfig.method,
            enableCodeIntelligence: options.enableCodeIntelligence,
            enableExternalMcpAgents: options.enableExternalMcpAgents,
          },
        } as unknown as Parameters<typeof client.graphql>[0])) as { data?: { sendAgentChatMessage?: { sessionId?: string } } };

        // The resolver answers with a different session id when this message
        // started a new session; the reply is written there.
        let replySessionId = sessionId;
        const returnedSessionId = sent?.data?.sendAgentChatMessage?.sessionId;
        if (returnedSessionId && returnedSessionId !== sessionId) {
          moveToSession(returnedSessionId);
          replySessionId = returnedSessionId;
          knownAssistantKeys = new Set();
        }

        try {
          const reply = await pollForAssistantReply({
            client,
            sessionId: replySessionId,
            knownAssistantKeys,
            signal: pollAbortRef.current.signal,
          });
          if (reply) {
            addMessage({
              role: 'assistant',
              content: reply.content,
              messageType: reply.messageType ?? 'assistant_final_response',
              method: reply.messageType ?? 'assistant_final_response',
              isProcessing: false,
              sessionId: replySessionId,
              timestamp: reply.timestamp,
              id: `assistant-${reply.timestamp}`,
            } as unknown as ChatMessage);
          } else {
            throw new Error(
              'Timed out waiting for the assistant response. The request may still be processing — try reloading the chat session.',
            );
          }
        } catch (pollErr) {
          // A cancelled/aborted poll (session switch, unmount, cancel) is not an
          // error to surface to the user.
          if ((pollErr as { name?: string })?.name === 'AbortError') return undefined;
          throw pollErr;
        }
        return undefined;
      }

      const response = await client.graphql({
        query: agentConfig.mutation,
        variables: {
          prompt,
          sessionId,
          method: agentConfig.method,
          enableCodeIntelligence: options.enableCodeIntelligence,
          enableExternalMcpAgents: options.enableExternalMcpAgents,
        },
      } as unknown as Parameters<typeof client.graphql>[0]);

      return response;
    } catch (err) {
      // Extract detailed error message from GraphQL errors
      const gqlErr = err as { errors?: { message: string; errorType?: string }[]; message?: string };
      let errorMessage = 'Failed to send message. Please try again.';
      if (gqlErr.errors && gqlErr.errors.length > 0) {
        const firstError = gqlErr.errors[0];
        if (firstError.errorType === 'Unauthorized') {
          errorMessage = `Access denied: ${firstError.message}. Check your role permissions.`;
        } else {
          errorMessage = firstError.message || errorMessage;
        }
      } else if (gqlErr.message) {
        errorMessage = gqlErr.message;
      }
      updateAgentChatState({
        error: errorMessage,
        waitingForResponse: false,
      });
      logger.error('Chat error:', err);
      throw err;
    } finally {
      updateAgentChatState({ isLoading: false });
    }
  };

  // Cancel waiting for response
  const cancelResponse = (): void => {
    // Abort an in-flight non-streaming poll so it stops immediately.
    pollAbortRef.current?.abort();
    updateAgentChatState({ waitingForResponse: false });
    logger.info('Response cancelled by user');
  };

  // Clear error
  const clearError = (): void => {
    updateAgentChatState({ error: null });
  };

  // Clear chat
  const clearChat = (): void => {
    resetAgentChatState();
    sentMessagesRef.current = new Set();
  };

  // Load a previous chat session
  const loadChatSession = async (targetSessionId: string, existingMessages: ChatMessage[] | null = null): Promise<void> => {
    try {
      updateAgentChatState({
        isLoading: true,
        error: null,
      });

      // If messages are already provided (from dropdown), use them
      let messagesToLoad = existingMessages;

      // Otherwise, fetch messages from the server
      if (!messagesToLoad) {
        const response = await client.graphql({
          query: getChatMessages,
          variables: { sessionId: targetSessionId },
        });
        // getChatMessages returns partial shape (no `id` field) - normalized in formatting below
        messagesToLoad = (response.data.getChatMessages as unknown as ChatMessage[]) ?? [];
      }

      // Convert messages to the format expected by the UI
      const formattedMessages: ChatMessage[] = messagesToLoad.map((msg: ChatMessage, index: number) => {
        const baseMessage: ChatMessage = {
          role: msg.role,
          content: msg.content,
          messageType: 'text',
          toolUseData: null,
          isProcessing: false, // Historical messages are never processing
          sessionId: msg.sessionId,
          timestamp: msg.timestamp,
          id: `${msg.timestamp}-${index}`,
        };

        // For assistant messages, parse content to extract structured data (charts, tables, etc.)
        if (msg.role === 'assistant' && msg.content) {
          const parsedData = parseResponseData(msg.content);

          if (parsedData) {
            // If we found structured data, add it to the message
            baseMessage.parsedData = parsedData;
            // Update content to show only the text portion (without the JSON)
            baseMessage.content = parsedData.textContent || msg.content;
          }
        }

        return baseMessage;
      });

      // Update context with loaded session
      updateAgentChatState({
        messages: formattedMessages,
        sessionId: targetSessionId,
        waitingForResponse: false,
        lastMessageCount: formattedMessages.length,
        // Not known for a loaded session until the server reports it.
        sessionHoldsDataResults: false,
        sessionNotice: null,
      });

      sentMessagesRef.current = new Set();

      // Log for debugging
      console.log(`🔄 Loaded chat session: ${targetSessionId} with ${formattedMessages.length} messages`);
      console.log(`🔍 SessionId after loading: ${targetSessionId}`);

      logger.info(`Loaded chat session ${targetSessionId} with ${formattedMessages.length} messages`);
    } catch (err) {
      updateAgentChatState({ error: 'Failed to load chat session. Please try again.' });
      logger.error('Error loading chat session:', err);
      throw err;
    } finally {
      updateAgentChatState({ isLoading: false });
    }
  };

  return {
    messages,
    isLoading,
    waitingForResponse,
    error,
    sessionId,
    sendMessage,
    setOptIn,
    cancelResponse,
    clearError,
    clearChat,
    loadChatSession,
    agentConfig, // Expose config for debugging
  };
};

export default useAgentChat;
