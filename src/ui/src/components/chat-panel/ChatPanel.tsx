// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useRef, useMemo, useEffect, useCallback } from 'react';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Button, Container, SpaceBetween, FormField, Alert, Select, StatusIndicator } from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import { sendChatDocumentMessage, onChatDocumentMessageUpdate } from '../../graphql/generated';
import useConfiguration from '../../hooks/use-configuration';
import useCurrentSessionCreds from '../../hooks/use-current-session-creds';
import { streamChat, type StreamCredentials } from '../../api/stream-client';
import { pollForAssistantReply, fetchAssistantKeys } from '../../api/chat-poll';
import { streamUrl } from '../../aws-exports';
import SafeMarkdown from '../common/SafeMarkdown';
import { useFormatters } from '../../i18n/format';
import './ChatPanel.css';

const useHttpApiTransport = true;
// Stream when a Lambda Function URL is configured (VITE_STREAM_URL); otherwise
// (e.g. GovCloud, where Function URLs don't exist) send over the REST API and
// poll getChatMessages for the final answer — see api/chat-poll.ts.
const streamingAvailable = Boolean(streamUrl);

interface ChatMessage {
  role: string; // 'user' | 'ai' | 'loader'
  content: string;
  dt?: string;
  type?: string; // 'msg' | 'error'
  isStreaming?: boolean; // true while tokens are still being appended
  modelId?: string;
}

interface ChatPanelProps {
  objectKey: string;
  /**
   * The document's ConfigVersion (e.g. "default", "v2"). Used to fetch the
   * chat configuration (model enum + default model) from the same config
   * version that processed the document. Falls back to "default".
   */
  configVersion?: string;
}

/** Shape of the payload published on `onChatDocumentMessageUpdate`. */
interface ChatDocumentUpdate {
  sessionId?: string;
  role?: string; // 'user' | 'assistant'
  content?: string; // full text, delta chunk, or status string
  timestamp?: string;
  method?: string; // 'chat' | 'assistant_status' | 'assistant_stream' | 'assistant_final' | 'assistant_error'
  status?: string; // QUEUED | LOADING_DOCUMENT | CALLING_MODEL | STREAMING | COMPLETE | ERROR
  modelId?: string;
  isProcessing?: boolean;
}

/** Cloudscape StatusIndicator accepts a limited enum for `type`. */
type StatusType = 'loading' | 'info' | 'error' | 'success';

/**
 * The error above the chat: text from the server or from a thrown error, kept as it arrives, or one
 * of the panel's messages, formatted at render so it follows a language switch.
 */
type PanelError =
  | string
  | { message: TypedMessageDescriptor<NoMessageValues> }
  | { message: TypedMessageDescriptor<{ readonly error: string }>; values: { readonly error: string } };

/** Subscribable shape returned by Amplify's `client.graphql({ query: subscription })`. */
interface GraphQLSubscribable {
  subscribe: (handlers: { next: (value: { data: Record<string, unknown> }) => void; error: (err: unknown) => void }) => {
    unsubscribe: () => void;
  };
}

const client = generateClient();
const logger = new ConsoleLogger('ChatWithDocument');

const messages = defineMessages<{
  title: NoMessageValues;
  empty: NoMessageValues;
  modelLabel: NoMessageValues;
  modelDescription: { readonly profile: string };
  modelDescriptionNoList: { readonly profile: string };
  modelPlaceholder: NoMessageValues;
  modelEmpty: NoMessageValues;
  modelPending: NoMessageValues;
  messageLabel: NoMessageValues;
  send: NoMessageValues;
  errorFallback: NoMessageValues;
  connectionLost: NoMessageValues;
  sendFailed: NoMessageValues;
  accessDenied: { readonly error: string };
}>({
  title: {
    id: 'documents.chat.title',
    defaultMessage: 'Chat With the Document',
    description: 'Heading of the chat panel on the document page.',
  },
  empty: {
    id: 'documents.chat.empty',
    defaultMessage: 'To start chatting to this document, enter your message below.',
    description: 'Text of the document chat panel before the first message.',
  },
  modelLabel: {
    id: 'documents.chat.model.label',
    defaultMessage: 'Model',
    description: 'Label of the model picker in the document chat panel.',
  },
  modelDescription: {
    id: 'documents.chat.model.description',
    defaultMessage: 'Default from configuration profile "{profile}". Choose a larger-context model if the document is very long.',
    description: 'Hint under the chat model picker. {profile} is the name of the configuration profile that processed the document.',
  },
  modelDescriptionNoList: {
    id: 'documents.chat.model.descriptionNoList',
    defaultMessage: 'Default model from configuration profile "{profile}". (Model list not available — using configured default.)',
    description:
      'Hint beside the chat model name when the configuration lists no models. {profile} is the name of the configuration profile.',
  },
  modelPlaceholder: {
    id: 'documents.chat.model.placeholder',
    defaultMessage: 'Select a chat model',
    description: 'Placeholder of the chat model picker.',
  },
  modelEmpty: {
    id: 'documents.chat.model.empty',
    defaultMessage: 'No models available',
    description: 'Shown in the chat model picker when it has no models.',
  },
  modelPending: {
    id: 'documents.chat.model.pending',
    defaultMessage: '(pending)',
    description: 'Shown in place of the chat model name while no default model is known yet.',
  },
  messageLabel: {
    id: 'documents.chat.message.label',
    defaultMessage: 'Your message',
    description: 'Label of the text box in which the user types a message to the document chat.',
  },
  send: {
    id: 'documents.chat.send',
    defaultMessage: 'Send',
    description: 'Button that sends the typed message to the document chat.',
  },
  errorFallback: {
    id: 'documents.chat.errorFallback',
    defaultMessage: 'An error occurred.',
    description: 'Chat reply shown when the reply failed and the chat gave no reason.',
  },
  connectionLost: {
    id: 'documents.chat.connectionLost',
    defaultMessage: 'Chat connection lost. Please refresh the page.',
    description: 'Error in the document chat panel when its live connection to the chat drops.',
  },
  sendFailed: {
    id: 'documents.chat.sendFailed',
    defaultMessage: 'Failed to send message. Please try again.',
    description: 'Error in the document chat panel when a message could not be sent and gave no reason.',
  },
  accessDenied: {
    id: 'documents.chat.accessDenied',
    defaultMessage: 'Access denied: {error}.',
    description:
      'Error in the document chat panel when the user may not chat with the document. {error} is the reason the server gave, in English.',
  },
});

/**
 * Generate a RFC-4122 v4 UUID. Uses the browser's crypto API when available
 * (covers all supported browsers) with a math-based fallback for older
 * environments. Used as the per-panel chat `sessionId` so AppSync can fan
 * streaming updates back to only this client.
 */
const generateSessionId = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // RFC 4122 v4 fallback
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
};

/**
 * Extract the list of allowed chat models from the schema's `chat.properties.model.enum`.
 * Returns an empty array if the schema is not yet loaded or doesn't contain a chat section
 * (e.g. stack deployed before the chat config feature was added).
 */
const extractChatModelEnum = (schema: Record<string, unknown> | null): string[] => {
  if (!schema) return [];
  try {
    // Schema shape: { properties: { chat: { properties: { model: { enum: [...] } } } } }
    const chatProps = (
      (schema as { properties?: Record<string, unknown> }).properties?.chat as {
        properties?: Record<string, unknown>;
      }
    )?.properties;
    const modelProp = chatProps?.model as { enum?: string[] } | undefined;
    if (modelProp?.enum && Array.isArray(modelProp.enum)) {
      return modelProp.enum.filter((m): m is string => typeof m === 'string');
    }
  } catch (e) {
    logger.warn('Failed to extract chat model enum from schema', e);
  }
  return [];
};

/** Map the processor's machine-readable status to a Cloudscape StatusIndicator type. */
const statusToIndicatorType = (status?: string): StatusType => {
  switch (status) {
    case 'ERROR':
      return 'error';
    case 'COMPLETE':
      return 'success';
    case 'STREAMING':
    case 'CALLING_MODEL':
    case 'LOADING_DOCUMENT':
    case 'QUEUED':
      return 'loading';
    default:
      return 'info';
  }
};

/** The status-pill labels, one per status code the processor sends. */
const statusMessages = defineMessages<{
  queued: NoMessageValues;
  loadingDocument: NoMessageValues;
  callingModel: NoMessageValues;
  streaming: NoMessageValues;
  complete: NoMessageValues;
  error: NoMessageValues;
}>({
  queued: {
    id: 'documents.chat.status.queued',
    defaultMessage: 'Queued…',
    description: 'Status of a document chat request waiting to start.',
  },
  loadingDocument: {
    id: 'documents.chat.status.loadingDocument',
    defaultMessage: 'Loading document text…',
    description: 'Status of a document chat request while the document text loads.',
  },
  callingModel: {
    id: 'documents.chat.status.callingModel',
    defaultMessage: 'Querying model…',
    description: 'Status of a document chat request while the model is queried.',
  },
  streaming: {
    id: 'documents.chat.status.streaming',
    defaultMessage: 'Streaming response…',
    description: 'Status of a document chat request while the reply streams in.',
  },
  complete: {
    id: 'documents.chat.status.complete',
    defaultMessage: 'Done',
    description: 'Status of a document chat request whose reply is complete.',
  },
  error: {
    id: 'documents.chat.status.error',
    defaultMessage: 'Error',
    description: 'Status of a document chat request that failed.',
  },
});

/** The label in the UI's language; without `intl` the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/**
 * Default human-readable status-pill labels when the processor doesn't send one, in the UI's
 * language. An unknown status shows as it arrives.
 */
const defaultStatusLabel = (status?: string, intl?: IntlShape): string => {
  switch (status) {
    case 'QUEUED':
      return labelOf(statusMessages.queued, intl);
    case 'LOADING_DOCUMENT':
      return labelOf(statusMessages.loadingDocument, intl);
    case 'CALLING_MODEL':
      return labelOf(statusMessages.callingModel, intl);
    case 'STREAMING':
      return labelOf(statusMessages.streaming, intl);
    case 'COMPLETE':
      return labelOf(statusMessages.complete, intl);
    case 'ERROR':
      return labelOf(statusMessages.error, intl);
    default:
      return status || '';
  }
};

/** The panel's error in the UI language. */
const errorText = (error: PanelError, intl: IntlShape): string => {
  if (typeof error === 'string') return error;
  return 'values' in error ? intl.formatMessage(error.message, error.values) : intl.formatMessage(error.message);
};

const ChatPanel = ({ objectKey, configVersion = 'default' }: ChatPanelProps): React.JSX.Element => {
  const intl = useIntl();
  const [error, setError] = useState<PanelError | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  // The processor's status code, with the text it sent if any. Without that text the status's own
  // label is worded at render, so it follows a language switch.
  const [currentStatus, setCurrentStatus] = useState<{ status: string; label?: string } | null>(null);
  const [isWaiting, setIsWaiting] = useState(false);
  const { formatTime } = useFormatters();

  const textareaRef = useRef<HTMLInputElement>(null);
  const rowIdRef = useRef(0);
  // Aborts an in-flight non-streaming poll on document switch / unmount so it
  // does not keep running (up to its timeout) against a stale session.
  const pollAbortRef = useRef<AbortController | null>(null);

  // Cognito Identity Pool credentials for SigV4-signing the streaming request
  // (httpapi transport only). Unused under the appsync transport.
  const { currentCredentials } = useCurrentSessionCreds({});

  // One sessionId per panel instance (i.e. per opened document). Regenerate
  // when the document changes so we can't cross-contaminate sessions.
  const [sessionId, setSessionId] = useState<string>(() => generateSessionId());
  useEffect(() => {
    // Fresh document → fresh session + clear chat history. Abort any poll from
    // the previous document so it can't apply a stale answer.
    pollAbortRef.current?.abort();
    setSessionId(generateSessionId());
    setChatMessages([]);
    setCurrentStatus(null);
    setIsWaiting(false);
    setError(null);
  }, [objectKey]);

  // Abort any in-flight poll on unmount.
  useEffect(
    () => () => {
      pollAbortRef.current?.abort();
    },
    [],
  );

  // Fetch the config for this document's version. We use this for two things:
  //   1) Populate the model-selector dropdown from schema.chat.properties.model.enum
  //   2) Default the selected model to mergedConfig.chat.model (or summarization.model as fallback)
  const { schema, mergedConfig } = useConfiguration(configVersion);

  // Compute available model options and the default-selected model from config.
  const { modelOptions, defaultModelId } = useMemo(() => {
    const enumList = extractChatModelEnum(schema);
    const options: SelectProps.Option[] = enumList.map((m) => ({ label: m, value: m }));

    // Prefer chat.model, fall back to summarization.model (backward-compat for
    // configs created before the chat section existed).
    const chatCfg = (mergedConfig?.chat as { model?: string } | undefined) ?? undefined;
    const summCfg = (mergedConfig?.summarization as { model?: string } | undefined) ?? undefined;
    const cfgModel = chatCfg?.model || summCfg?.model || '';

    return { modelOptions: options, defaultModelId: cfgModel };
  }, [schema, mergedConfig]);

  // Selected model: start empty and derive from defaultModelId once config loads.
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const effectiveModelId = selectedModelId || defaultModelId;
  const selectedOption: SelectProps.Option | null = effectiveModelId ? { label: effectiveModelId, value: effectiveModelId } : null;

  const generateId = () => {
    rowIdRef.current += 1;
    return rowIdRef.current;
  };

  /**
   * Apply a subscription update from the processor. Handles three processor
   * message types:
   *   * assistant_status — update the status pill only
   *   * assistant_stream — append delta text to the in-flight assistant bubble
   *   * assistant_final  — finalize the assistant bubble, clear the status pill
   *   * assistant_error  — replace the in-flight bubble with an error bubble
   * Any echo of our own `method="chat"` user publish is ignored (we render
   * user messages optimistically on submit).
   */
  const handleUpdate = useCallback(
    (update: ChatDocumentUpdate) => {
      if (!update || update.sessionId !== sessionId) return;

      const method = update.method || '';
      const text = update.content || '';
      const statusCode = update.status || '';

      if (method === 'chat') {
        // Our own user message echoing back — ignore; optimistic UI already showed it.
        return;
      }

      if (method === 'assistant_status') {
        setCurrentStatus({
          status: statusCode,
          label: text,
        });
        return;
      }

      if (method === 'assistant_stream') {
        setCurrentStatus({ status: 'STREAMING' });
        setChatMessages((prev) => {
          const next = [...prev];
          const lastIdx = next.length - 1;
          // Append to the last in-flight assistant bubble, or create one.
          if (lastIdx >= 0 && next[lastIdx].role === 'ai' && next[lastIdx].isStreaming) {
            next[lastIdx] = {
              ...next[lastIdx],
              content: (next[lastIdx].content || '') + text,
            };
            return next;
          }
          // Drop any loader bubble before adding the streaming assistant bubble.
          const withoutLoader = next.filter((m) => m.role !== 'loader');
          withoutLoader.push({
            role: 'ai',
            content: text,
            dt: formatTime(new Date()),
            type: 'msg',
            isStreaming: true,
            modelId: update.modelId,
          });
          return withoutLoader;
        });
        return;
      }

      if (method === 'assistant_final') {
        setChatMessages((prev) => {
          const next = prev.filter((m) => m.role !== 'loader');
          const lastIdx = next.length - 1;
          if (lastIdx >= 0 && next[lastIdx].role === 'ai' && next[lastIdx].isStreaming) {
            // Replace streamed content with the final answer (processor guarantees
            // full text in `assistant_final`). This handles the edge case where
            // the final text differs from the concatenation of streamed deltas
            // (e.g. post-processing cleanup).
            next[lastIdx] = {
              ...next[lastIdx],
              content: text || next[lastIdx].content,
              isStreaming: false,
              dt: formatTime(new Date()),
            };
            return next;
          }
          // No in-flight bubble — append a fresh one.
          next.push({
            role: 'ai',
            content: text,
            dt: formatTime(new Date()),
            type: 'msg',
            modelId: update.modelId,
          });
          return next;
        });
        setCurrentStatus(null);
        setIsWaiting(false);
        return;
      }

      if (method === 'assistant_error') {
        setChatMessages((prev) => {
          const next = prev.filter((m) => m.role !== 'loader');
          const lastIdx = next.length - 1;
          if (lastIdx >= 0 && next[lastIdx].role === 'ai' && next[lastIdx].isStreaming) {
            next[lastIdx] = {
              ...next[lastIdx],
              content: text,
              isStreaming: false,
              type: 'error',
              dt: formatTime(new Date()),
            };
            return next;
          }
          next.push({
            role: 'ai',
            content: text,
            type: 'error',
            dt: formatTime(new Date()),
          });
          return next;
        });
        setCurrentStatus({ status: 'ERROR', label: text });
        setIsWaiting(false);
        return;
      }
    },
    [sessionId, formatTime],
  );

  // Subscribe to streaming updates for this session (appsync transport only).
  // Under httpapi the response is streamed directly from the Function URL in
  // handlePromptSubmit, so there is no subscription to set up.
  useEffect(() => {
    if (useHttpApiTransport) return undefined;
    logger.info('Subscribing to chat-doc updates', { sessionId });
    const subscription = (
      client.graphql({
        query: onChatDocumentMessageUpdate,
        variables: { sessionId },
      }) as unknown as GraphQLSubscribable
    ).subscribe({
      next: ({ data }) => {
        const update = (data?.onChatDocumentMessageUpdate as ChatDocumentUpdate | undefined) || undefined;
        if (update) handleUpdate(update);
      },
      error: (err: unknown) => {
        logger.error('chat-doc subscription error', err);
        setError({ message: messages.connectionLost });
      },
    });
    return () => {
      try {
        subscription.unsubscribe();
      } catch (e) {
        logger.warn('Failed to unsubscribe chat-doc subscription', e);
      }
    };
  }, [sessionId, handleUpdate]);

  const handlePromptSubmit = async () => {
    const prompt = textareaRef.current?.value?.trim() || '';
    if (!prompt || isWaiting) return;

    // Optimistically render the user bubble + a loader while we wait for the
    // first subscription event.
    setChatMessages((prev) => [
      ...prev,
      {
        role: 'user',
        content: prompt,
        dt: formatTime(new Date()),
        type: 'msg',
      },
      { role: 'loader', content: 'loader' },
    ]);
    if (textareaRef.current) textareaRef.current.value = '';
    setError(null);
    setIsWaiting(true);
    setCurrentStatus({ status: 'QUEUED' });

    try {
      if (useHttpApiTransport && streamingAvailable) {
        // Stream directly from the IAM-authed Lambda Function URL. Each SSE
        // event has the same shape the AppSync subscription delivered, so we
        // feed them straight into handleUpdate. streamChat resolves when the
        // stream ends (final/error already applied via handleUpdate).
        const creds = currentCredentials as StreamCredentials | undefined;
        if (!creds?.accessKeyId) {
          throw new Error('No AWS credentials available for streaming.');
        }
        await streamChat({
          path: '/chat/document',
          body: {
            sessionId,
            prompt,
            s3Uri: objectKey,
            modelId: effectiveModelId,
          },
          credentials: creds,
          onEvent: (event) => handleUpdate(event as ChatDocumentUpdate),
        });
      } else if (useHttpApiTransport && !streamingAvailable) {
        // No Function URL (e.g. GovCloud): send over the REST API (async-invokes
        // the processor, which persists the final message), then POLL
        // getChatMessages for the answer. No live token streaming — the loader
        // bubble stays until the full answer lands, delivered via a synthetic
        // assistant_final event so the existing handleUpdate path renders it.
        // Baseline the existing assistant messages BEFORE sending so the poll
        // matches only the new reply (clock-independent).
        const knownAssistantKeys = await fetchAssistantKeys(client, sessionId);
        pollAbortRef.current?.abort();
        pollAbortRef.current = new AbortController();
        setCurrentStatus({ status: 'CALLING_MODEL' });
        await client.graphql({
          query: sendChatDocumentMessage,
          variables: {
            sessionId,
            prompt,
            method: 'chat',
            s3Uri: objectKey,
            modelId: effectiveModelId,
          },
        });
        const reply = await pollForAssistantReply({
          client,
          sessionId,
          knownAssistantKeys,
          signal: pollAbortRef.current.signal,
        });
        if (reply) {
          handleUpdate({
            sessionId,
            method: 'assistant_final',
            status: 'COMPLETE',
            content: reply.content,
            modelId: effectiveModelId,
          } as ChatDocumentUpdate);
        } else {
          throw new Error(
            'Timed out waiting for the response. The request may still be processing — reopen the document to see the answer.',
          );
        }
      } else {
        await client.graphql({
          query: sendChatDocumentMessage,
          variables: {
            sessionId,
            prompt,
            method: 'chat',
            s3Uri: objectKey,
            modelId: effectiveModelId,
          },
        });
      }
    } catch (err) {
      // A cancelled/aborted poll (document switch, unmount) is not a user error.
      if ((err as { name?: string })?.name === 'AbortError') return;
      // Extract a useful error message.
      const gqlErr = err as { errors?: { message: string; errorType?: string }[]; message?: string };
      let errorMessage: PanelError = { message: messages.sendFailed };
      if (gqlErr.errors && gqlErr.errors.length > 0) {
        const firstError = gqlErr.errors[0];
        if (firstError.errorType === 'Unauthorized') {
          // String(): an error without a message read "undefined" here before; ICU would print nothing.
          errorMessage = { message: messages.accessDenied, values: { error: String(firstError.message) } };
        } else {
          errorMessage = firstError.message || errorMessage;
        }
      } else if (gqlErr.message) {
        errorMessage = gqlErr.message;
      }
      setChatMessages((prev) => prev.filter((m) => m.role !== 'loader'));
      setError(errorMessage);
      setIsWaiting(false);
      setCurrentStatus(null);
      logger.error('sendChatDocumentMessage error', err);
    }
  };

  return (
    <div id="chatDiv">
      <SpaceBetween size="l">
        <Container
          header={
            <h2>
              <FormattedMessage {...messages.title} />
            </h2>
          }
        >
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {errorText(error, intl)}
            </Alert>
          )}

          {chatMessages.length > 0 ? (
            chatMessages.map((post) => (
              <div className="chat-message-container" key={generateId()}>
                {(() => {
                  switch (post.role) {
                    case 'user':
                      return (
                        <div className="chat-user">
                          <p>
                            {post.content}
                            <br />
                            <span className="time">{post.dt}</span>
                          </p>
                        </div>
                      );
                    case 'loader':
                      return <div className="loader" />;
                    case 'ai':
                      return (
                        <div className={`chat-assistant ${post.type === 'error' ? 'error' : ''}`}>
                          {post.type === 'error' ? (
                            // Error messages are plain text, not markdown — render verbatim so
                            // stack traces, raw JSON, and quoted model output aren't mangled.
                            <p style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                              {post.content || <FormattedMessage {...messages.errorFallback} />}
                            </p>
                          ) : (
                            <div className="chat-markdown">
                              <SafeMarkdown>{post.content || ''}</SafeMarkdown>
                              {post.isStreaming ? (
                                <span className="chat-streaming-cursor" aria-hidden="true">
                                  ▍
                                </span>
                              ) : null}
                            </div>
                          )}
                          <div className="time">
                            {post.dt}
                            {post.modelId ? ` • ${post.modelId}` : ''}
                          </div>
                        </div>
                      );
                    default:
                      return '';
                  }
                })()}
              </div>
            ))
          ) : (
            <p>
              <FormattedMessage {...messages.empty} />
            </p>
          )}

          {currentStatus && (
            <div style={{ margin: '6px 0' }}>
              <StatusIndicator type={statusToIndicatorType(currentStatus.status)}>
                {currentStatus.label || defaultStatusLabel(currentStatus.status, intl)}
              </StatusIndicator>
            </div>
          )}

          <SpaceBetween size="m">
            <FormField
              label={intl.formatMessage(messages.modelLabel)}
              description={
                modelOptions.length
                  ? intl.formatMessage(messages.modelDescription, { profile: configVersion })
                  : intl.formatMessage(messages.modelDescriptionNoList, { profile: configVersion })
              }
            >
              {modelOptions.length > 0 ? (
                <Select
                  selectedOption={selectedOption}
                  onChange={({ detail }) => setSelectedModelId((detail.selectedOption.value as string) || '')}
                  options={modelOptions}
                  placeholder={intl.formatMessage(messages.modelPlaceholder)}
                  empty={intl.formatMessage(messages.modelEmpty)}
                />
              ) : (
                <span>{defaultModelId || <FormattedMessage {...messages.modelPending} />} </span>
              )}
            </FormField>

            <FormField label={intl.formatMessage(messages.messageLabel)} {...({ style: { flex: 8 } } as Record<string, unknown>)}>
              <input
                type="text"
                name="postContent"
                ref={textareaRef}
                style={{ padding: '3px', width: '100%' }}
                id="chatTextarea"
                disabled={isWaiting}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    handlePromptSubmit();
                  }
                }}
              />
            </FormField>

            <Button variant="primary" onClick={handlePromptSubmit} disabled={isWaiting}>
              <FormattedMessage {...messages.send} />
            </Button>
          </SpaceBetween>
        </Container>
      </SpaceBetween>
    </div>
  );
};

export default ChatPanel;
