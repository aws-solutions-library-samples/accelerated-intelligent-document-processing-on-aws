// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The "Generating final result..." placeholder, across a language change.
 *
 * A table or chart answer opens with `structured_data_start`, which adds a placeholder,
 * and closes with `assistant_final_response`, which replaces it. The hook finds the
 * placeholder by its id, its session and `isProcessing`, not by its text. What this test
 * pins is the outcome: a language switch mid-answer still ends with one finished assistant
 * message. It would pass against a lookup by text too, because the mocked stream, like the
 * real one, hands every event to the callback of the render that sent the message, so the
 * placeholder is written and looked up with the same text. It drives the real hook and
 * context with the stream mocked, and switches the language through the app's
 * `LocaleProvider`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, waitFor } from '@testing-library/react';
import { useIntl } from 'react-intl';
import { I18n } from 'aws-amplify/utils';

import useAgentChat from '../use-agent-chat';
import { AgentChatProvider, useAgentChatContext } from '../../contexts/agentChat';
import { renderHookWithIntl } from '../../test-utils/renderWithIntl';
import { setFixtureCatalog } from '../../test-utils/i18nCatalogsMock';
import type { ChatMessage } from '../../types/agent-chat';

const { streamChat } = vi.hoisted(() => ({ streamChat: vi.fn() }));

vi.mock('virtual:i18n-catalogs', () => import('../../test-utils/i18nCatalogsMock'));
vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../api/stream-client', () => ({ streamChat }));
vi.mock('../../api/chat-poll', () => ({ fetchAssistantKeys: vi.fn(), pollForAssistantReply: vi.fn() }));
vi.mock('../../aws-exports', () => ({ streamUrl: 'https://stream.example/' }));
vi.mock('../use-current-session-creds', () => ({
  default: () => ({ currentCredentials: { accessKeyId: 'ASIASCOPEDKEYEXAMPLE', secretAccessKey: 'x' } }),
}));
vi.mock('../../graphql/generated', () => ({
  sendAgentChatMessage: 'sendAgentChatMessage',
  onAgentChatMessageUpdate: 'onAgentChatMessageUpdate',
  getChatMessages: 'getChatMessages',
}));

const GENERATING_ID = 'agentChat.session.generating';
const GENERATING_FR = 'Génération du résultat final…';

type OnEvent = (event: Partial<ChatMessage>) => void;

/** Holds the open stream: its event callback, and the call that ends it. */
const stream: { onEvent: OnEvent | null; end: () => void } = { onEvent: null, end: () => undefined };

const renderChat = () =>
  renderHookWithIntl(() => ({ chat: useAgentChat(), ctx: useAgentChatContext(), intl: useIntl() }), {
    locale: 'en',
    enabledLocales: ['en', 'fr-CA'],
    wrapper: AgentChatProvider,
  });

const event = (sessionId: string, messageType: string, content: string, timestamp: number): Partial<ChatMessage> => ({
  sessionId,
  role: 'assistant',
  content,
  messageType,
  isProcessing: messageType !== 'assistant_final_response',
  timestamp,
});

beforeEach(() => {
  streamChat.mockReset();
  streamChat.mockImplementation(
    ({ onEvent }: { onEvent: OnEvent }) =>
      new Promise<void>((resolve) => {
        stream.onEvent = onEvent;
        stream.end = resolve;
      }),
  );
});

afterEach(() => {
  stream.end();
  stream.onEvent = null;
  cleanup();
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('en');
});

describe('the generating placeholder', () => {
  it('is replaced by the answer when the language changes while it shows', async () => {
    const view = renderChat();
    const sid = view.result.current.ctx.agentChatState.sessionId;

    let sending: Promise<unknown> = Promise.resolve();
    act(() => {
      sending = view.result.current.chat.sendMessage('how many documents failed?');
    });
    await waitFor(() => expect(stream.onEvent).not.toBeNull());

    act(() => stream.onEvent?.(event(sid, 'structured_data_start', '', 100)));
    const placeholder = view.result.current.ctx.agentChatState.messages.find((m) => m.id === '100-generating');
    expect(placeholder).toMatchObject({ content: 'Generating final result...', isProcessing: true });

    setFixtureCatalog('fr-CA', { [GENERATING_ID]: GENERATING_FR });
    view.setLocale('fr-CA');
    await waitFor(() => expect(view.result.current.intl.messages[GENERATING_ID]).toBeDefined());

    act(() => stream.onEvent?.(event(sid, 'assistant_final_response', '12 documents failed.', 200)));
    await act(async () => {
      stream.end();
      await sending;
    });

    const assistant = view.result.current.ctx.agentChatState.messages.filter((m) => m.role === 'assistant');
    expect(assistant.map((m) => [m.content, m.isProcessing])).toEqual([['12 documents failed.', false]]);
  });
});
