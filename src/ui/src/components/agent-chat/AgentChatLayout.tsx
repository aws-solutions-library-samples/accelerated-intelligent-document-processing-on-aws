// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useRef, useMemo, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { defineMessages, FormattedMessage, useIntl, type MessageDescriptor, type MessageTag, type NoMessageValues } from 'react-intl';
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
import { activatesWithoutAsking, newQuickStartProfileName } from './quickStartDiscoveryTarget';
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
import { NEW_SESSION_NOTICE } from './session-opt-ins';
import './AgentChatLayout.css';

import type { ChatMessage } from '../../types/agent-chat';

const logger = new ConsoleLogger('AgentChatLayout');

const uiMessages = defineMessages<{
  thirdPartyNotice: NoMessageValues;
  enableExternalMcpAgents: NoMessageValues;
  sampleOpenFailed: NoMessageValues;
  placeholder: NoMessageValues;
  titleQuickStart: NoMessageValues;
  titleChat: NoMessageValues;
  placeholderQuickStart: NoMessageValues;
  errorContent: { readonly error: string };
  agentThinking: { readonly agentName: string };
  loadingHistory: NoMessageValues;
  working: NoMessageValues;
  welcome: { readonly name: string; readonly span: MessageTag };
  welcomeQuickStart: NoMessageValues;
  welcomeChat: NoMessageValues;
  welcomeQuickStartIntro: NoMessageValues;
  quickStartHint: NoMessageValues;
  suggestedPrompts: NoMessageValues;
  attachDocuments: NoMessageValues;
  analysisFailed: NoMessageValues;
  filesRemove: { readonly index: number };
  filesShowFewer: NoMessageValues;
  filesShowMore: NoMessageValues;
  filesErrorIcon: NoMessageValues;
  documentsAttached: { readonly count: number };
  stepWithDocuments: { readonly step: string; readonly count: number };
  documentsAnalyzed: NoMessageValues;
  viewDiscovery: NoMessageValues;
  enableCodeIntelligence: NoMessageValues;
  clearChat: NoMessageValues;
  newSessionNotice: NoMessageValues;
}>({
  thirdPartyNotice: {
    id: 'agentChat.layout.thirdPartyNotice',
    defaultMessage:
      'Avoid sharing sensitive information, the Code Intelligence Agent and External MCP agents may use third-party services.',
    description:
      'Notice beside the agent opt-in checkboxes under the Agent Chat input. Code Intelligence Agent and External MCP agents are the names of the two optional agents.',
  },
  enableExternalMcpAgents: {
    id: 'agentChat.layout.enableExternalMcpAgents',
    defaultMessage: 'Enable External MCP agents',
    description: 'Checkbox under the Agent Chat input that lets the chat use agents served by external MCP servers.',
  },
  sampleOpenFailed: {
    id: 'agentChat.layout.sampleOpenFailed',
    defaultMessage: 'Could not open sample',
    description: 'Error next to a sample-document link in a Quick Start answer when the sample could not be opened.',
  },
  placeholder: {
    id: 'agentChat.layout.placeholder',
    defaultMessage: 'Ask me anything about documents, errors, or IDP code base',
    description: 'Placeholder of the Agent Chat message box. IDP is the name of the solution.',
  },
  titleQuickStart: {
    id: 'agentChat.layout.titleQuickStart',
    defaultMessage: 'Quick Start',
    description: 'Heading of the chat while it runs Quick Start, which drafts a configuration for a document type.',
  },
  titleChat: {
    id: 'agentChat.layout.titleChat',
    defaultMessage: 'IDP Agent Companion Chat',
    description: 'Heading of the Agent Chat page. IDP is the name of the solution; Agent Companion Chat is the name of the chat feature.',
  },
  placeholderQuickStart: {
    id: 'agentChat.layout.placeholderQuickStart',
    defaultMessage: 'Describe the documents you want to process, or attach examples to get started',
    description: 'Placeholder of the Quick Start message box, where the user describes or attaches example documents.',
  },
  errorContent: {
    id: 'agentChat.layout.errorContent',
    defaultMessage: 'Error: {error}',
    description: "Shown in the Agent Chat in place of an answer that failed. {error} is the server's error, in English.",
  },
  agentThinking: {
    id: 'agentChat.layout.agentThinking',
    defaultMessage: '{agentName} - Thinking...',
    description:
      'Heading of the section that shows an agent working in the Agent Chat, while it works. {agentName} is the name of the agent.',
  },
  loadingHistory: {
    id: 'agentChat.layout.loadingHistory',
    defaultMessage: 'Loading chat history...',
    description: 'Shown over the Agent Chat messages while a previous conversation loads.',
  },
  working: {
    id: 'agentChat.layout.working',
    defaultMessage: 'Working on your request… this can take a minute.',
    description: 'Shown in the Agent Chat under the last message while the agent prepares its answer.',
  },
  welcome: {
    id: 'agentChat.layout.welcome',
    defaultMessage: 'Welcome to <span>{name}</span>',
    description:
      'Greeting on the empty chat. {name} is Quick Start or Agent Companion Chat; the text inside <span> is shown in the accent colour.',
  },
  welcomeQuickStart: {
    id: 'agentChat.layout.welcomeQuickStart',
    defaultMessage: 'Quick Start',
    description: 'Name shown in the greeting of the empty Quick Start chat ("Welcome to Quick Start").',
  },
  welcomeChat: {
    id: 'agentChat.layout.welcomeChat',
    defaultMessage: 'Agent Companion Chat',
    description:
      'Name shown in the greeting of the empty Agent Chat ("Welcome to Agent Companion Chat"). Agent Companion Chat is the name of the chat feature.',
  },
  welcomeQuickStartIntro: {
    id: 'agentChat.layout.welcomeQuickStartIntro',
    defaultMessage: "Describe the documents you want to process and I'll help you set up a configuration — no prior setup needed.",
    description: 'Paragraph under the greeting of the empty Quick Start chat. "I" is the Quick Start assistant.',
  },
  quickStartHint: {
    id: 'agentChat.layout.quickStartHint',
    defaultMessage:
      'Quick Start helps you author a configuration for your document type. Describe your documents or attach examples to get started.',
    description: 'Hint under the Quick Start message box.',
  },
  suggestedPrompts: {
    id: 'agentChat.layout.suggestedPrompts',
    defaultMessage: 'Suggested prompts',
    description: 'Screen-reader name of the group of suggested prompts shown on the empty chat.',
  },
  attachDocuments: {
    id: 'agentChat.layout.attachDocuments',
    defaultMessage: 'Attach documents',
    description: 'Screen-reader label of the paper-clip button that attaches example documents to a Quick Start message.',
  },
  analysisFailed: {
    id: 'agentChat.layout.analysisFailed',
    defaultMessage: 'Document analysis failed',
    description: 'Heading of the error under the Quick Start message box when discovery of the attached documents failed.',
  },
  filesRemove: {
    id: 'agentChat.layout.files.remove',
    defaultMessage: 'Remove file {index, number}',
    description:
      'Screen-reader label of the button that removes an attached document. {index} is the position of the file in the list, from 1.',
  },
  filesShowFewer: {
    id: 'agentChat.layout.files.showFewer',
    defaultMessage: 'Show fewer',
    description: 'Button that collapses the list of attached documents under the Quick Start message box.',
  },
  filesShowMore: {
    id: 'agentChat.layout.files.showMore',
    defaultMessage: 'Show more',
    description: 'Button that expands the list of attached documents under the Quick Start message box.',
  },
  filesErrorIcon: {
    id: 'agentChat.layout.files.errorIcon',
    defaultMessage: 'Error',
    description: 'Screen-reader label of the error icon next to an attached document.',
  },
  documentsAttached: {
    id: 'agentChat.layout.documentsAttached',
    defaultMessage:
      '{count, plural, one {# document attached — send your message to analyze it.} other {# documents attached — send your message to analyze them.}}',
    description: 'Note under the Quick Start message box once documents are attached and before the message is sent.',
  },
  stepWithDocuments: {
    id: 'agentChat.layout.stepWithDocuments',
    defaultMessage: '{step} ({count, plural, one {# doc} other {# docs}})',
    description:
      'Progress of the discovery of the documents attached in Quick Start. {step} is the current step as reported, possibly in English; the count is the number of documents.',
  },
  documentsAnalyzed: {
    id: 'agentChat.layout.documentsAnalyzed',
    defaultMessage: 'Documents analyzed',
    description: 'Shown under the Quick Start message box once discovery of the attached documents has finished.',
  },
  viewDiscovery: {
    id: 'agentChat.layout.viewDiscovery',
    defaultMessage: 'View full discovery details',
    description: 'Link under the Quick Start message box to the page of the finished discovery job.',
  },
  enableCodeIntelligence: {
    id: 'agentChat.layout.enableCodeIntelligence',
    defaultMessage: 'Enable Code Intelligence Agent',
    description: 'Checkbox under the Agent Chat input that lets the chat use the Code Intelligence Agent, the name of an optional agent.',
  },
  clearChat: {
    id: 'agentChat.layout.clearChat',
    defaultMessage: 'Clear chat',
    description: 'Button under the chat that clears the conversation and starts a new one.',
  },
  newSessionNotice: {
    id: 'agentChat.layout.newSessionNotice',
    defaultMessage:
      'Started a new conversation. Code Intelligence and External MCP agents are not used in a conversation that includes Analytics or Error Analyzer results.',
    description:
      'Notice above the Agent Chat after turning on an optional agent started a new conversation. Code Intelligence, External MCP, Analytics and Error Analyzer are agent names.',
  },
});

/** The suggested prompts on the empty chat. The chip shows the message; the chat sends its English. */
const chipMessages = defineMessages<{
  capabilities: NoMessageValues;
  recentDocumentsTable: NoMessageValues;
  classificationPipeline: NoMessageValues;
  recentErrors: NoMessageValues;
  insuranceClaims: NoMessageValues;
  invoices: NoMessageValues;
  paystubs: NoMessageValues;
  addDocumentType: NoMessageValues;
}>({
  capabilities: {
    id: 'agentChat.layout.chip.capabilities',
    defaultMessage: 'What capabilities do you have?',
    description:
      'Suggested prompt on the empty Agent Chat. The English text is also the prompt the chat sends, so the agent always receives English; translate the chip only.',
  },
  recentDocumentsTable: {
    id: 'agentChat.layout.chip.recentDocumentsTable',
    defaultMessage: 'Can you make a table of the documents uploaded in the last three days?',
    description:
      'Suggested prompt on the empty Agent Chat. The English text is also the prompt the chat sends, so the agent always receives English; translate the chip only.',
  },
  classificationPipeline: {
    id: 'agentChat.layout.chip.classificationPipeline',
    defaultMessage: 'Explain how the document classification pipeline works in the IDP accelerator',
    description:
      'Suggested prompt on the empty Agent Chat. The English text is also the prompt the chat sends, so the agent always receives English; translate the chip only.',
  },
  recentErrors: {
    id: 'agentChat.layout.chip.recentErrors',
    defaultMessage: 'Analyze recent errors in document processing',
    description:
      'Suggested prompt on the empty Agent Chat. The English text is also the prompt the chat sends, so the agent always receives English; translate the chip only.',
  },
  insuranceClaims: {
    id: 'agentChat.layout.chip.insuranceClaims',
    defaultMessage: 'Help me set up IDP for processing auto insurance claims',
    description:
      'Suggested prompt on the empty Quick Start chat. The English text is also the prompt the chat sends, so the agent always receives English; translate the chip only.',
  },
  invoices: {
    id: 'agentChat.layout.chip.invoices',
    defaultMessage: 'I have invoices with vendor, amount, and due date — get me started',
    description:
      'Suggested prompt on the empty Quick Start chat. The English text is also the prompt the chat sends, so the agent always receives English; translate the chip only.',
  },
  paystubs: {
    id: 'agentChat.layout.chip.paystubs',
    defaultMessage: 'Bootstrap a config for employee paystubs',
    description:
      'Suggested prompt on the empty Quick Start chat. The English text is also the prompt the chat sends, so the agent always receives English; translate the chip only.',
  },
  addDocumentType: {
    id: 'agentChat.layout.chip.addDocumentType',
    defaultMessage: 'Add a document type to one of my profiles',
    description:
      'Suggested prompt on the empty Quick Start chat. The English text is also the prompt the chat sends, so the agent always receives English; translate the chip only.',
  },
});

interface SupportPrompt {
  id: string;
  message: MessageDescriptor;
}

const chatSupportPrompts: SupportPrompt[] = [
  { id: 'GeneralAgent', message: chipMessages.capabilities },
  { id: 'AnalyticsAgent', message: chipMessages.recentDocumentsTable },
  { id: 'CodeIntelligenceAgent', message: chipMessages.classificationPipeline },
  { id: 'ErrorAnalyzerAgent', message: chipMessages.recentErrors },
];

const quickStartSupportPrompts: SupportPrompt[] = [
  { id: 'QuickStartInsurance', message: chipMessages.insuranceClaims },
  { id: 'QuickStartInvoice', message: chipMessages.invoices },
  { id: 'QuickStartPaystub', message: chipMessages.paystubs },
  { id: 'QuickStartAddType', message: chipMessages.addDocumentType },
];

/** Renders a `<span>...</span>` element value: the accent-coloured name in the greeting. */
const accentSpan = (chunks: React.ReactNode[]): React.JSX.Element => <span>{chunks}</span>;

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
          <StatusIndicator type="error">
            <FormattedMessage {...uiMessages.sampleOpenFailed} />
          </StatusIndicator>
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
  placeholder,
  agentConfig = {},
  className = '',
  showHeader = true,
  customStyles = {},
  welcomeName,
}: AgentChatLayoutProps): React.JSX.Element => {
  const intl = useIntl();
  const [welcomeAnimated, setWelcomeAnimated] = useState(false);
  const [isLoadingSession, setIsLoadingSession] = useState(false);
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(new Set());
  const chatMessagesRef = useRef<HTMLDivElement>(null);

  const { agentChatState, updateAgentChatState } = useAgentChatContext();
  const { inputValue, lastMessageCount, enableCodeIntelligence, enableExternalMcpAgents, sessionNotice, mode } = agentChatState;

  const effectiveAgentConfig = useMemo(
    () => (mode === 'quick_start' ? { ...agentConfig, method: 'quick_start' } : { ...agentConfig, method: 'chat' }),
    [agentConfig, mode],
  );

  const { messages, isLoading, waitingForResponse, error, sendMessage, setOptIn, clearError, clearChat, loadChatSession } = useAgentChat(
    effectiveAgentConfig as Record<string, unknown>,
  );
  const { user } = useAppContext();

  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [completedJobId, setCompletedJobId] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const { setActiveVersion, fetchVersions } = useConfigurationVersions();
  const navigate = useNavigate();

  const discoveryTargetRef = useRef<string | null>(null);

  const handleUploadComplete = useCallback(
    (result: QuickStartUploadResult) => {
      // The Quick Start agent reads this summary, so it stays English; it also shows as the user's own message.
      const names = result.classNames.length ? result.classNames.join(', ') : 'document type(s)';
      const discoveryKind = result.totalDocuments === 1 ? 'Single-document' : 'Multi-document';
      const typeCount = result.clustersFound || result.classNames.length;
      const summaryFor = (activeness: 'already' | 'not') =>
        `I uploaded ${result.totalDocuments} document(s). ${discoveryKind} discovery inferred ` +
        `${typeCount} document type(s): ${names}, saved as a new configuration profile. Please summarize what was ` +
        `found in plain language for a first-time user. Briefly explain, in one plain sentence, that ` +
        `this was saved as a reusable "configuration" that tells the system what to pull out of these ` +
        `documents (introduce the term gently, do not assume I already know it). Then ask whether I'd ` +
        `like to adjust any fields or start processing my documents. Keep it concise; save deeper ` +
        `configuration-version details for if I ask.\n\n` +
        `[Discovery saved the schema to a new configuration profile "${result.configVersion}", which is ${activeness} the active profile; if it is not, ask before I process documents whether to make it the active profile, and call activate_config_version only once I agree. Its fields ` +
        `are not shown here — call get_class_schema for version "${result.configVersion}" before ` +
        `answering questions about the fields or refining them.]`;
      const send = (activated: boolean) =>
        sendMessage(summaryFor(activated ? 'already' : 'not'), { enableCodeIntelligence, enableExternalMcpAgents });
      setAttachedFiles([]);
      setUploadError(null);
      setCompletedJobId(result.jobId);
      void fetchVersions().then((current) => {
        if (!current || !activatesWithoutAsking(current, result.configVersion)) {
          send(false);
          return;
        }
        setActiveVersion(result.configVersion)
          .then(() => send(true))
          .catch((e) => {
            logger.error('Failed to activate the Quick Start profile:', e);
            send(false);
          });
      });
    },
    [sendMessage, enableCodeIntelligence, enableExternalMcpAgents, setActiveVersion, fetchVersions],
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
      if (!discoveryTargetRef.current) discoveryTargetRef.current = newQuickStartProfileName(new Date());
      const version = discoveryTargetRef.current;
      setCompletedJobId(null);
      setUploadError(null);
      setAttachedFiles([]);
      startUpload(filesToProcess, version);
    }

    let messageToSend: string;
    if (hasFiles) {
      // The Quick Start agent reads this note, so it stays English.
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
      await sendMessage(messageToSend, { enableCodeIntelligence, enableExternalMcpAgents });
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

  const effectiveTitle = title ?? intl.formatMessage(mode === 'quick_start' ? uiMessages.titleQuickStart : uiMessages.titleChat);
  const effectivePlaceholder =
    mode === 'quick_start'
      ? intl.formatMessage(uiMessages.placeholderQuickStart)
      : (placeholder ?? intl.formatMessage(uiMessages.placeholder));

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
      discoveryTargetRef.current = null;
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
          contentText = intl.formatMessage(uiMessages.errorContent, { error: String(content.error) });
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
                      headerText={
                        message.isProcessing
                          ? intl.formatMessage(uiMessages.agentThinking, {
                              agentName: String((message.toolUseData as Record<string, unknown>).agent_name),
                            })
                          : String((message.toolUseData as Record<string, unknown>).agent_name)
                      }
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
  }, [messages, user, collapsedSections, handleExpandedChange, userInitial, lastAssistantIndex, intl]);

  const chatContent = (
    <div className="chat-container">
      <div className="chat-content">
        {error && (
          <Alert type="error" dismissible onDismiss={clearError}>
            {error}
          </Alert>
        )}
        {sessionNotice && (
          <Alert type="info" dismissible onDismiss={() => updateAgentChatState({ sessionNotice: null })}>
            {sessionNotice === NEW_SESSION_NOTICE ? <FormattedMessage {...uiMessages.newSessionNotice} /> : sessionNotice}
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
                <FormattedMessage {...uiMessages.loadingHistory} />
              </Box>
            </div>
          )}

          {messages.length === 0 ? (
            <div className={`welcome-text ${welcomeAnimated ? 'animate-in' : ''}`}>
              <h2>
                <FormattedMessage
                  {...uiMessages.welcome}
                  values={{
                    name: welcomeName || intl.formatMessage(mode === 'quick_start' ? uiMessages.welcomeQuickStart : uiMessages.welcomeChat),
                    span: accentSpan,
                  }}
                />
              </h2>
              {mode === 'quick_start' && (
                <Box variant="p" color="text-body-secondary">
                  <FormattedMessage {...uiMessages.welcomeQuickStartIntro} />
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
                          <FormattedMessage {...uiMessages.working} />
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
                  ariaLabel={intl.formatMessage(uiMessages.suggestedPrompts)}
                  alignment="horizontal"
                  items={supportPrompts.map((item) => ({
                    text: intl.formatMessage(item.message),
                    id: item.id,
                  }))}
                  onItemClick={async ({ detail }) => {
                    const selectedPrompt = supportPrompts.find((prompt) => prompt.id === detail.id);
                    if (selectedPrompt) {
                      // The chip shows its label in the UI language; the agent receives the English.
                      updateAgentChatState({ inputValue: String(selectedPrompt.message.defaultMessage) });
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
                        {intl.formatMessage(uiMessages.attachDocuments)}
                      </FileInput>
                    </Box>
                  ) : undefined
                }
                secondaryContent={
                  mode === 'quick_start' && (attachedFiles.length > 0 || uploadStatus || completedJobId || uploadError) ? (
                    <SpaceBetween size="xs">
                      {uploadError && (
                        <Alert
                          type="error"
                          dismissible
                          onDismiss={() => setUploadError(null)}
                          header={intl.formatMessage(uiMessages.analysisFailed)}
                        >
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
                            removeFileAriaLabel: (idx) => intl.formatMessage(uiMessages.filesRemove, { index: idx + 1 }),
                            limitShowFewer: intl.formatMessage(uiMessages.filesShowFewer),
                            limitShowMore: intl.formatMessage(uiMessages.filesShowMore),
                            errorIconAriaLabel: intl.formatMessage(uiMessages.filesErrorIcon),
                          }}
                        />
                      )}
                      {attachedFiles.length > 0 && !uploadInProgress && (
                        <Box fontSize="body-s" color="text-body-secondary">
                          <FormattedMessage {...uiMessages.documentsAttached} values={{ count: attachedFiles.length }} />
                        </Box>
                      )}
                      {uploadStatus && uploadInProgress && (
                        <StatusIndicator type="in-progress">
                          {uploadStatus.totalDocuments
                            ? intl.formatMessage(uiMessages.stepWithDocuments, {
                                step: uploadStatus.currentStep || uploadStatus.status,
                                count: uploadStatus.totalDocuments,
                              })
                            : uploadStatus.currentStep || uploadStatus.status}
                        </StatusIndicator>
                      )}
                      {completedJobId && !uploadInProgress && (
                        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                          <StatusIndicator type="success">
                            <FormattedMessage {...uiMessages.documentsAnalyzed} />
                          </StatusIndicator>
                          <Link
                            onFollow={() => navigate(`${DISCOVERY_JOB_PATH}/${completedJobId}`)}
                            ariaLabel={intl.formatMessage(uiMessages.viewDiscovery)}
                          >
                            <FormattedMessage {...uiMessages.viewDiscovery} />
                          </Link>
                        </SpaceBetween>
                      )}
                    </SpaceBetween>
                  ) : undefined
                }
              />
              {mode === 'quick_start' ? (
                <Box {...({ fontSize: 'body-s', color: 'text-status-info', flex: '1' } as Record<string, unknown>)}>
                  <FormattedMessage {...uiMessages.quickStartHint} />
                </Box>
              ) : (
                <SpaceBetween direction="horizontal" size="m" alignItems="center">
                  <Box {...({ fontSize: 'body-s', color: 'text-status-info', flex: '1' } as Record<string, unknown>)}>
                    {intl.formatMessage(uiMessages.thirdPartyNotice)}
                  </Box>
                  <Checkbox
                    checked={enableCodeIntelligence}
                    onChange={({ detail }) => setOptIn('enableCodeIntelligence', detail.checked)}
                    disabled={waitingForResponse}
                  >
                    <Box fontSize="body-s">
                      <FormattedMessage {...uiMessages.enableCodeIntelligence} />
                    </Box>
                  </Checkbox>
                  <Checkbox
                    checked={enableExternalMcpAgents}
                    onChange={({ detail }) => setOptIn('enableExternalMcpAgents', detail.checked)}
                    disabled={waitingForResponse}
                  >
                    <Box fontSize="body-s">{intl.formatMessage(uiMessages.enableExternalMcpAgents)}</Box>
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
                  discoveryTargetRef.current = null;
                  setWelcomeAnimated(false);
                  setTimeout(() => {
                    setWelcomeAnimated(true);
                  }, 100);
                }}
                disabled={waitingForResponse || isLoadingSession}
              >
                <FormattedMessage {...uiMessages.clearChat} />
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
