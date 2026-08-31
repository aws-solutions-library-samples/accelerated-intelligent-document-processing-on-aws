// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useRef, useMemo, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Container,
  Alert,
  Box,
  Header,
  Spinner,
  PromptInput,
  SpaceBetween,
  ExpandableSection,
  Button,
  Checkbox,
  FileInput,
  FileTokenGroup,
  StatusIndicator,
  Link,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { DISCOVERY_JOB_PATH } from '../../routes/constants';
import { SupportPromptGroup, LoadingBar } from '@cloudscape-design/chat-components';
import SafeMarkdown from '../common/SafeMarkdown';
import { generateClient } from '../../api/client-shim';
import { getSampleDocumentUrl } from '../../graphql/generated';

import useAgentChat from '../../hooks/use-agent-chat';
import useAppContext from '../../contexts/app';
import { useAgentChatContext } from '../../contexts/agentChat';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import useQuickStartUpload from './useQuickStartUpload';
import type { QuickStartUploadResult } from './useQuickStartUpload';
import PlotDisplay from '../document-agents-layout/PlotDisplay';
import TableDisplay from '../document-agents-layout/TableDisplay';
import AgentChatHistoryDropdown from './AgentChatHistoryDropdown';
import AgentToolComponent from './AgentToolComponent';
import BedrockErrorMessage from './BedrockErrorMessage';
import CreateIssueButton from '../common/create-issue-button';
import './AgentChatLayout.css';

import type { ChatMessage } from '../../types/agent-chat';

const logger = new ConsoleLogger('AgentChatLayout');

const sampleClient = generateClient();

// Renders <sampledoc s3key="samples/..."> emitted by the Quick Start agent as a
// clickable link; on click it presigns via getSampleDocumentUrl and opens the
// document (single doc) or downloads the zip (batch) in a new tab.
const SampleDocLink = ({ s3key, children }: { s3key?: string; children?: React.ReactNode }): React.JSX.Element => {
  const [failed, setFailed] = useState(false);
  const open = async (): Promise<void> => {
    if (!s3key) return;
    setFailed(false);
    try {
      const resp = await sampleClient.graphql({ query: getSampleDocumentUrl, variables: { s3Key: s3key } });
      const url = (resp as { data?: { getSampleDocumentUrl?: { url?: string } } })?.data?.getSampleDocumentUrl?.url;
      if (url) {
        window.open(url, '_blank', 'noopener,noreferrer');
      } else {
        setFailed(true);
      }
    } catch (err) {
      logger.error('Failed to open sample document', err);
      setFailed(true);
    }
  };
  // No href: Cloudscape Link with only onFollow runs the action without any
  // route navigation (matches the WelcomeContent Quick Start link pattern).
  // On failure show an inline notice so the click isn't silently swallowed.
  return (
    <>
      <Link onFollow={open}>{children}</Link>
      {failed && (
        <>
          {' '}
          <StatusIndicator type="error">Could not open sample</StatusIndicator>
        </>
      )}
    </>
  );
};

const markdownComponents = { sampledoc: SampleDocLink } as unknown as Record<string, React.ComponentType<Record<string, unknown>>>;

interface AgentConfig {
  agentType?: string;
  mutation?: Record<string, unknown> | ((...args: unknown[]) => unknown);
  subscription?: Record<string, unknown> | ((...args: unknown[]) => unknown);
  method?: string;
}

interface AgentChatLayoutProps {
  title?: string;
  placeholder?: string;
  agentConfig?: AgentConfig;
  className?: string;
  showHeader?: boolean;
  customStyles?: React.CSSProperties;
  welcomeName?: string;
}

const AgentChatLayout = ({
  title,
  placeholder = 'Ask me anything about documents, errors, or IDP code base',
  agentConfig = {},
  className = '',
  showHeader = true,
  customStyles = {},
  welcomeName,
}: AgentChatLayoutProps): React.JSX.Element => {
  const [welcomeAnimated, setWelcomeAnimated] = useState(false);
  const [isLoadingSession, setIsLoadingSession] = useState(false);
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(new Set());
  const chatMessagesRef = useRef<HTMLDivElement>(null);

  const { agentChatState, updateAgentChatState } = useAgentChatContext();
  const { inputValue, lastMessageCount, enableCodeIntelligence, mode } = agentChatState;

  const effectiveAgentConfig = useMemo(
    () => (mode === 'quick_start' ? { ...agentConfig, method: 'quick_start' } : { ...agentConfig, method: 'chat' }),
    [agentConfig, mode],
  );

  const { messages, isLoading, waitingForResponse, error, sendMessage, clearError, clearChat, loadChatSession } = useAgentChat(
    effectiveAgentConfig as Record<string, unknown>,
  );
  const { user } = useAppContext();

  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [completedJobId, setCompletedJobId] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const { versions, setActiveVersion, fetchVersions } = useConfigurationVersions();
  const navigate = useNavigate();

  const targetConfigVersion = useMemo(() => {
    const active = versions.find((v) => v.isActive);
    if (active && active.versionName !== 'default') {
      return active.versionName;
    }
    return 'quickstart';
  }, [versions]);

  const handleUploadComplete = useCallback(
    (result: QuickStartUploadResult) => {
      const names = result.classNames.length ? result.classNames.join(', ') : 'document type(s)';
      const discoveryKind = result.totalDocuments === 1 ? 'Single-document' : 'Multi-document';
      const typeCount = result.clustersFound || result.classNames.length;
      const summary =
        `I uploaded ${result.totalDocuments} document(s). ${discoveryKind} discovery inferred ` +
        `${typeCount} document type(s): ${names}, saved and ready to use. Please summarize what was ` +
        `found in plain language for a first-time user. Briefly explain, in one plain sentence, that ` +
        `this was saved as a reusable "configuration" that tells the system what to pull out of these ` +
        `documents (introduce the term gently, do not assume I already know it). Then ask whether I'd ` +
        `like to adjust any fields or start processing my documents. Keep it concise; save deeper ` +
        `configuration-version details for if I ask.\n\n` +
        `[Discovery saved the schema to configuration profile "${result.configVersion}". Its fields ` +
        `are not shown here — call get_class_schema for version "${result.configVersion}" before ` +
        `answering questions about the fields or refining them.]`;
      setAttachedFiles([]);
      setUploadError(null);
      setCompletedJobId(result.jobId);
      if (result.configVersion === 'quickstart' && !versions.some((v) => v.versionName === 'quickstart' && v.isActive)) {
        setActiveVersion('quickstart')
          .then(() => fetchVersions())
          .catch((e) => logger.error('Failed to activate quickstart version:', e));
      }
      sendMessage(summary, { enableCodeIntelligence });
    },
    [sendMessage, enableCodeIntelligence, setActiveVersion, fetchVersions, versions],
  );

  const handleUploadError = useCallback((message: string) => {
    setUploadError(message);
  }, []);

  const {
    startUpload,
    uploading,
    status: uploadStatus,
  } = useQuickStartUpload({
    onComplete: handleUploadComplete,
    onError: handleUploadError,
  });

  const userInitial = useMemo(() => {
    if (!user?.username) return 'U';
    return user.username.charAt(0).toUpperCase();
  }, [user]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setWelcomeAnimated(true);
    }, 100);

    return () => clearTimeout(timer);
  }, []);

  // Listen for sample query insertion events from the tools panel
  useEffect(() => {
    const handleSampleQueryInsert = (event: CustomEvent<{ query: string }>) => {
      const { query } = event.detail;
      updateAgentChatState({ inputValue: query });
    };

    window.addEventListener('insertSampleQuery', handleSampleQueryInsert as EventListener);

    return () => {
      window.removeEventListener('insertSampleQuery', handleSampleQueryInsert as EventListener);
    };
  }, [updateAgentChatState]);

  // Track new messages and scroll to new assistant messages (but not while streaming)
  useEffect(() => {
    if (messages.length > lastMessageCount) {
      const newMessages = messages.slice(lastMessageCount);

      const newAssistantMessage = newMessages.find((msg: ChatMessage) => msg.role === 'assistant' && msg.isProcessing === true);

      if (newAssistantMessage) {
        setTimeout(() => {
          if (chatMessagesRef.current) {
            const assistantMessages = chatMessagesRef.current.querySelectorAll('.assistant-message');
            if (assistantMessages.length > 0) {
              const lastAssistantMessage = assistantMessages[assistantMessages.length - 1];
              lastAssistantMessage.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
          }
        }, 100);
      }

      updateAgentChatState({ lastMessageCount: messages.length });
    }
  }, [messages, lastMessageCount, updateAgentChatState]);

  const handlePromptSubmit = async () => {
    const prompt = inputValue;
    const hasFiles = mode === 'quick_start' && attachedFiles.length > 0;
    if (!prompt.trim() && !hasFiles) return;

    const filesAttachedCount = attachedFiles.length;

    if (hasFiles) {
      const filesToProcess = attachedFiles;
      const version = targetConfigVersion || `bootstrap-${Date.now().toString(36)}`;
      setCompletedJobId(null);
      setUploadError(null);
      setAttachedFiles([]);
      startUpload(filesToProcess, version);
    }

    let messageToSend: string;
    if (hasFiles) {
      const isSingle = filesAttachedCount === 1 && !attachedFiles[0]?.name.toLowerCase().endsWith('.zip');
      const discoveryKind = isSingle ? 'single-document' : 'multi-document';
      const note =
        `[${filesAttachedCount} document(s) attached — ${discoveryKind} Discovery is now running on them ` +
        `in the background. Acknowledge it's processing and that you'll summarize the results when they ` +
        `arrive; do not ask me to upload again.]`;
      messageToSend = prompt.trim() ? `${prompt.trim()}\n\n${note}` : note;
    } else {
      messageToSend = prompt;
    }

    updateAgentChatState({ inputValue: '' });
    try {
      await sendMessage(messageToSend, { enableCodeIntelligence });
      // Scroll to the latest user message after sending
      setTimeout(() => {
        if (chatMessagesRef.current) {
          const userMessages = chatMessagesRef.current.querySelectorAll('.user-message');
          if (userMessages.length > 0) {
            const lastUserMessage = userMessages[userMessages.length - 1];
            lastUserMessage.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
        }
      }, 100); // Small delay to ensure the message is rendered
    } catch (err) {
      console.error('Failed to send message:', err);
    }
  };

  const handleInputChange = (event: { detail: { value: string } }) => {
    updateAgentChatState({ inputValue: event.detail.value });
  };

  const effectiveTitle = title ?? (mode === 'quick_start' ? 'Quick Start' : 'IDP Agent Companion Chat');
  const effectivePlaceholder =
    mode === 'quick_start' ? 'Describe the documents you want to process, or attach examples to get started' : placeholder;

  const uploadInProgress = uploading || (!!uploadStatus && !['COMPLETED', 'FAILED'].includes(uploadStatus.status));

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const handleKeyDown = (event: any) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      handlePromptSubmit();
    }
  };

  // Handle expandable section state changes
  const handleExpandedChange = useCallback((messageId: string | number, expanded: boolean) => {
    const collapsedKey = `collapsed-${messageId}`;

    setCollapsedSections((prev) => {
      const newSet = new Set(prev);
      if (expanded) {
        newSet.delete(collapsedKey);
      } else {
        newSet.add(collapsedKey);
      }
      return newSet;
    });
  }, []);

  // Handle session selection from dropdown
  const handleSessionSelect = async (session: { sessionId: string }, sessionMessages: ChatMessage[]) => {
    try {
      setIsLoadingSession(true);
      console.log('Loading chat session:', session.sessionId);

      await loadChatSession(session.sessionId, sessionMessages);

      // Scroll to bottom after loading
      setTimeout(() => {
        if (chatMessagesRef.current) {
          chatMessagesRef.current.scrollTop = chatMessagesRef.current.scrollHeight;
        }
      }, 100);
    } catch (err) {
      console.error('Failed to load chat session:', err);
    } finally {
      setIsLoadingSession(false);
    }
  };

  // Handle session deletion
  const handleSessionDeleted = (sessionId: string) => {
    console.log('Session deleted:', sessionId);
    // If the deleted session was the current one, clear the chat
    // Note: We can't easily check if it's the current session since sessionId might be different
    // The dropdown component handles the UI state, so we don't need to do anything here
  };

  const chatSupportPrompts = [
    {
      id: 'GeneralAgent',
      prompt: 'What capabilities do you have?',
    },
    {
      id: 'AnalyticsAgent',
      prompt: 'Can you make a table of the documents uploaded in the last three days?',
    },
    {
      id: 'CodeIntelligenceAgent',
      prompt: 'Explain how the document classification pipeline works in the IDP accelerator',
    },
    {
      id: 'ErrorAnalyzerAgent',
      prompt: 'Analyze recent errors in document processing',
    },
  ];

  const quickStartSupportPrompts = [
    {
      id: 'QuickStartInsurance',
      prompt: 'Help me set up IDP for processing auto insurance claims',
    },
    {
      id: 'QuickStartInvoice',
      prompt: 'I have invoices with vendor, amount, and due date — get me started',
    },
    {
      id: 'QuickStartPaystub',
      prompt: 'Bootstrap a config for employee paystubs',
    },
    {
      id: 'QuickStartAddType',
      prompt: 'Add a new document type to my existing configuration',
    },
  ];

  const supportPrompts = mode === 'quick_start' ? quickStartSupportPrompts : chatSupportPrompts;

  // Index of the last assistant message — the "Create GitHub issue" affordance
  // is shown only under the final answer to avoid cluttering every turn.
  const lastAssistantIndex = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'assistant') return i;
    }
    return -1;
  }, [messages]);

  const renderedMessages = useMemo(() => {
    return messages.map((message: ChatMessage, messageIndex: number) => {
      let contentText = '';
      if (typeof message.content === 'string') {
        contentText = message.content;
      } else if (Array.isArray(message.content) && (message.content as unknown[])[0]) {
        const content = (message.content as unknown[])[0] as Record<string, unknown>;
        if (content?.error) {
          contentText = `Error: ${content.error}`;
        } else {
          contentText = (content?.text as string) || '';
        }
      }

      const isUser = message.role === 'user';

      // Handle tool_use messages with collapsible section using sessionMessages
      if (message.messageType === 'tool_use' && message.toolUseData) {
        const agentMessageId = message.id || `agent-${message.timestamp}`;
        const collapsedKey = `collapsed-${agentMessageId}`;

        const isExpanded = !collapsedSections.has(collapsedKey);
        const sessionMessages = message.toolUseData.sessionMessages || [];

        return (
          <div key={`agent-session-${message.timestamp}`} className="chat-message-wrapper assistant-message">
            <div className="message-container">
              <div className="message-content">
                <Box>
                  <div style={{ border: '1px #ddd solid', borderRadius: '14px', padding: '10px', background: '#f6f6f9' }}>
                    {/* Collapsible section for process and tools */}
                    <ExpandableSection
                      variant="footer"
                      headingTagOverride="h5"
                      expanded={isExpanded}
                      onChange={({ detail }) => handleExpandedChange(agentMessageId, detail.expanded)}
                      headerText={`${(message.toolUseData as Record<string, unknown>).agent_name}${
                        message.isProcessing ? ' - Thinking...' : ''
                      }`}
                    >
                      <div className="tool-usage-container">
                        {sessionMessages.map((sessionMsg: ChatMessage) => {
                          if (sessionMsg.messageType === 'text') {
                            return (
                              <Box
                                key={sessionMsg.id}
                                padding={{ right: 's', top: 's', bottom: 'n' }}
                                {...({ backgroundColor: 'background-container-content' } as Record<string, unknown>)}
                              >
                                <SafeMarkdown>{sessionMsg.content as string}</SafeMarkdown>
                              </Box>
                            );
                          }

                          if (sessionMsg.messageType === 'unified_tool') {
                            return (
                              <Box padding={{ right: 's', bottom: 'n' }} key={`tool-${sessionMsg.toolUseId}`}>
                                <AgentToolComponent
                                  toolName={sessionMsg.toolName || ''}
                                  toolUseId={sessionMsg.toolUseId || ''}
                                  executionLoading={sessionMsg.executionLoading}
                                  executionDetails={sessionMsg.executionDetails}
                                  resultLoading={sessionMsg.resultLoading}
                                  resultDetails={sessionMsg.resultDetails}
                                  timestamp={String(sessionMsg.timestamp)}
                                  parentProcessing={message.isProcessing}
                                />
                              </Box>
                            );
                          }

                          return null;
                        })}
                      </div>
                    </ExpandableSection>
                  </div>
                </Box>
              </div>
            </div>
          </div>
        );
      }

      // Handle user messages and other assistant messages normally
      return (
        <div
          key={`${message.role}-${message.timestamp}`}
          className={`chat-message-wrapper ${isUser ? 'user-message' : 'assistant-message'}`}
        >
          <div className="message-container">
            {isUser && (
              <div className="message-avatar">
                <div className="avatar-circle">{userInitial}</div>
              </div>
            )}
            <div className="message-content">
              {(() => {
                // Handle unified tool message type (standalone tools not part of agent session)
                if (message.messageType === 'unified_tool') {
                  return (
                    <AgentToolComponent
                      toolName={message.toolName || ''}
                      toolUseId={message.toolUseId || ''}
                      executionLoading={message.executionLoading}
                      executionDetails={message.executionDetails}
                      resultLoading={message.resultLoading}
                      resultDetails={message.resultDetails}
                      timestamp={String(message.timestamp)}
                      parentProcessing={message.isProcessing}
                    />
                  );
                }

                // Handle Bedrock error messages with user-friendly display
                if (message.messageType === 'bedrock_error' && message.bedrockErrorInfo) {
                  return (
                    <BedrockErrorMessage
                      errorInfo={
                        message.bedrockErrorInfo as {
                          errorType: string;
                          message: string;
                          technicalDetails?: string;
                          actionRecommendations?: string[];
                          retryAttempts?: number;
                        }
                      }
                    />
                  );
                }

                // Handle existing parsedData message type (preserve existing functionality)
                if (message.parsedData) {
                  return (
                    <SpaceBetween size="m">
                      {message.parsedData.textContent && <SafeMarkdown>{message.parsedData.textContent}</SafeMarkdown>}

                      {message.parsedData.responseType === 'plotData' && (
                        <PlotDisplay plotData={message.parsedData.data as Record<string, unknown>} />
                      )}

                      {message.parsedData.responseType === 'table' && (
                        <TableDisplay tableData={message.parsedData.data as Record<string, unknown>} />
                      )}

                      {mode !== 'quick_start' &&
                        messageIndex === lastAssistantIndex &&
                        !message.isProcessing &&
                        message.parsedData.textContent && (
                          <Box>
                            <CreateIssueButton findings={message.parsedData.textContent} />
                          </Box>
                        )}
                    </SpaceBetween>
                  );
                }

                // Handle regular text messages (preserve existing functionality)
                return (
                  <SpaceBetween size="xs">
                    <SafeMarkdown components={markdownComponents}>{contentText}</SafeMarkdown>
                    {mode !== 'quick_start' &&
                      messageIndex === lastAssistantIndex &&
                      !message.isProcessing &&
                      contentText.trim().length > 0 && (
                        <Box>
                          <CreateIssueButton findings={contentText} />
                        </Box>
                      )}
                  </SpaceBetween>
                );
              })()}
            </div>
          </div>
        </div>
      );
    });
  }, [messages, user, collapsedSections, handleExpandedChange, userInitial, lastAssistantIndex]);

  const chatContent = (
    <div className="chat-container">
      <div className="chat-content">
        {error && (
          <Alert type="error" dismissible onDismiss={clearError}>
            {error}
          </Alert>
        )}

        <div
          ref={chatMessagesRef}
          className="chat-messages"
          style={{
            position: 'relative',
            opacity: isLoadingSession ? 0.5 : 1,
            pointerEvents: isLoadingSession ? 'none' : 'auto',
            transition: 'opacity 0.3s ease',
          }}
        >
          {isLoadingSession && (
            <div
              style={{
                position: 'absolute',
                top: '50%',
                left: '50%',
                transform: 'translate(-50%, -50%)',
                zIndex: 1000,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: '12px',
                backgroundColor: 'rgba(255, 255, 255, 0.9)',
                padding: '20px',
                borderRadius: '8px',
                boxShadow: '0 4px 12px rgba(0, 0, 0, 0.1)',
              }}
            >
              <Spinner size="large" />
              <Box fontSize="body-m" color="text-body-secondary">
                Loading chat history...
              </Box>
            </div>
          )}

          {messages.length === 0 ? (
            <div className={`welcome-text ${welcomeAnimated ? 'animate-in' : ''}`}>
              <h2>
                Welcome to <span>{welcomeName || (mode === 'quick_start' ? 'Quick Start' : 'Agent Companion Chat')}</span>
              </h2>
              {mode === 'quick_start' && (
                <Box variant="p" color="text-body-secondary">
                  Describe the documents you want to process and I&apos;ll help you set up a configuration — no prior setup needed.
                </Box>
              )}
            </div>
          ) : (
            <>
              {renderedMessages}
              {waitingForResponse && (
                <div className="chat-message-wrapper assistant-message" aria-live="polite">
                  <div className="message-container">
                    <div className="message-content">
                      <Box color="text-body-secondary" fontSize="body-s" padding={{ bottom: 'xxs' }}>
                        <Spinner size="normal" />
                        <Box variant="span" padding={{ left: 'xs' }}>
                          Working on your request… this can take a minute.
                        </Box>
                      </Box>
                      <LoadingBar variant="gen-ai-masked" />
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <div className="prompt-input-container">
        <SpaceBetween direction="vertical" size="m">
          {messages.length === 0 && (
            <SpaceBetween direction="horizontal" size="s" alignItems="center">
              <Box {...({ flex: '1' } as Record<string, unknown>)}>
                <SupportPromptGroup
                  ariaLabel="Suggested prompts"
                  alignment="horizontal"
                  items={supportPrompts.map((item) => ({
                    text: item.prompt,
                    id: item.id,
                  }))}
                  onItemClick={async ({ detail }) => {
                    const selectedPrompt = supportPrompts.find((prompt) => prompt.id === detail.id);
                    if (selectedPrompt) {
                      updateAgentChatState({ inputValue: selectedPrompt.prompt });
                    }
                  }}
                />{' '}
              </Box>
            </SpaceBetween>
          )}
          <Box>
            <SpaceBetween direction="vertical" size="xs">
              <PromptInput
                value={inputValue}
                onChange={handleInputChange}
                onKeyDown={handleKeyDown}
                placeholder={effectivePlaceholder}
                disabled={isLoading || isLoadingSession || waitingForResponse}
                actionButtonIconName="send"
                onAction={handlePromptSubmit}
                minRows={3}
                secondaryActions={
                  mode === 'quick_start' ? (
                    <Box padding={{ left: 'xxs', top: 'xs' }}>
                      <FileInput
                        variant="icon"
                        multiple
                        accept=".pdf,.png,.jpg,.jpeg,.tiff,.tif,.webp,.zip"
                        value={attachedFiles}
                        onChange={({ detail }) => setAttachedFiles(detail.value)}
                      >
                        Attach documents
                      </FileInput>
                    </Box>
                  ) : undefined
                }
                secondaryContent={
                  mode === 'quick_start' && (attachedFiles.length > 0 || uploadStatus || completedJobId || uploadError) ? (
                    <SpaceBetween size="xs">
                      {uploadError && (
                        <Alert type="error" dismissible onDismiss={() => setUploadError(null)} header="Document analysis failed">
                          {uploadError}
                        </Alert>
                      )}
                      {attachedFiles.length > 0 && (
                        <FileTokenGroup
                          alignment="horizontal"
                          items={attachedFiles.map((file) => ({ file }))}
                          showFileSize
                          onDismiss={({ detail }) => setAttachedFiles((prev) => prev.filter((_, i) => i !== detail.fileIndex))}
                          i18nStrings={{
                            removeFileAriaLabel: (idx) => `Remove file ${idx + 1}`,
                            limitShowFewer: 'Show fewer',
                            limitShowMore: 'Show more',
                            errorIconAriaLabel: 'Error',
                          }}
                        />
                      )}
                      {attachedFiles.length > 0 && !uploadInProgress && (
                        <Box fontSize="body-s" color="text-body-secondary">
                          {attachedFiles.length} document(s) attached — send your message to analyze them.
                        </Box>
                      )}
                      {uploadStatus && uploadInProgress && (
                        <StatusIndicator type="in-progress">
                          {uploadStatus.currentStep || uploadStatus.status}
                          {uploadStatus.totalDocuments ? ` (${uploadStatus.totalDocuments} docs)` : ''}
                        </StatusIndicator>
                      )}
                      {completedJobId && !uploadInProgress && (
                        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                          <StatusIndicator type="success">Documents analyzed</StatusIndicator>
                          <Link
                            onFollow={() => navigate(`${DISCOVERY_JOB_PATH}/${completedJobId}`)}
                            ariaLabel="View full discovery details"
                          >
                            View full discovery details
                          </Link>
                        </SpaceBetween>
                      )}
                    </SpaceBetween>
                  ) : undefined
                }
              />
              {mode === 'quick_start' ? (
                <Box {...({ fontSize: 'body-s', color: 'text-status-info', flex: '1' } as Record<string, unknown>)}>
                  Quick Start helps you author a configuration for your document type. Describe your documents or attach examples to get
                  started.
                </Box>
              ) : (
                <SpaceBetween direction="horizontal" size="m" alignItems="center">
                  <Box {...({ fontSize: 'body-s', color: 'text-status-info', flex: '1' } as Record<string, unknown>)}>
                    Avoid sharing sensitive information, the Code Intelligence Agent may use third-party services.
                  </Box>
                  <Checkbox
                    checked={enableCodeIntelligence}
                    onChange={({ detail }) => updateAgentChatState({ enableCodeIntelligence: detail.checked })}
                    disabled={waitingForResponse}
                  >
                    <Box fontSize="body-s">Enable Code Intelligence Agent</Box>
                  </Checkbox>
                </SpaceBetween>
              )}
            </SpaceBetween>
          </Box>
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <Box {...({ flex: '1' } as Record<string, unknown>)}>
              <AgentChatHistoryDropdown
                onSessionSelect={handleSessionSelect}
                onSessionDeleted={handleSessionDeleted}
                disabled={waitingForResponse || isLoadingSession}
                surface={mode === 'quick_start' ? 'quick_start' : 'chat'}
              />
            </Box>
            {messages.length > 0 && (
              <Button
                variant="normal"
                iconName="refresh"
                onClick={() => {
                  clearChat();
                  setWelcomeAnimated(false);
                  setTimeout(() => {
                    setWelcomeAnimated(true);
                  }, 100);
                }}
                disabled={waitingForResponse || isLoadingSession}
              >
                Clear chat
              </Button>
            )}
          </SpaceBetween>
        </SpaceBetween>
      </div>
    </div>
  );

  if (showHeader) {
    return (
      <div className={`agent-chat-layout ${className}`} style={customStyles}>
        <Container header={<Header variant="h2">{effectiveTitle}</Header>}>{chatContent}</Container>
      </div>
    );
  }

  return (
    <div className={`agent-chat-layout ${className}`} style={customStyles}>
      {chatContent}
    </div>
  );
};

export default AgentChatLayout;
