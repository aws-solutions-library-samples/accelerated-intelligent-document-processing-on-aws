// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Agent Chat and Quick Start under `fr-CA`: with a fixture catalog a suggested prompt reads
 * French, a click puts its English text into the message box and sending sends that English,
 * in the chat and in Quick Start, so the agent always receives English. Quick Start's heading
 * and placeholder read French, and so does the notice that opting in started a new
 * conversation, which the chat state holds as a token. Under `en` the same renders read today's
 * English.
 */
import React, { useEffect } from 'react';
import { screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import createChatWrapper from '@cloudscape-design/chat-components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import { NEW_SESSION_NOTICE } from '../session-opt-ins';
// One value per mocked hook for every render: a hook that returned fresh objects would
// re-render the page forever. The chat starts empty, so the suggested prompts show.
const state = vi.hoisted(() => ({
  chat: {
    messages: [] as unknown[],
    isLoading: false,
    waitingForResponse: false,
    error: null,
    sendMessage: vi.fn(),
    setOptIn: vi.fn(),
    clearError: vi.fn(),
    clearChat: vi.fn(),
    loadChatSession: vi.fn(),
  },
  app: { user: { username: 'tester' } },
  versions: { versions: [] as unknown[], setActiveVersion: vi.fn(), fetchVersions: vi.fn() },
  upload: { startUpload: vi.fn(), uploading: false, status: null },
  deployment: {},
  graphql: vi.fn(),
}));
vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('../../../hooks/use-agent-chat', () => ({ default: () => state.chat }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => state.versions }));
vi.mock('../useQuickStartUpload', () => ({ default: () => state.upload }));
vi.mock('../AgentChatHistoryDropdown', () => ({ default: () => null }));
vi.mock('../../../hooks/use-deployment-context', () => ({ default: () => state.deployment }));
import AgentChatLayout from '../AgentChatLayout';
import { AgentChatProvider, useAgentChatContext } from '../../../contexts/agentChat';
const CHAT_PROMPT = 'What capabilities do you have?';
const QUICK_START_PROMPT = 'Bootstrap a config for employee paystubs';
const QUICK_START_PLACEHOLDER = 'Describe the documents you want to process, or attach examples to get started';
const NOTICE =
  'Started a new conversation. Code Intelligence and External MCP agents are not used in a conversation that includes Analytics or Error Analyzer results.';
/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'agentChat.layout.chip.capabilities': 'Quelles sont vos capacités?',
  'agentChat.layout.chip.paystubs': 'Préparer une configuration pour les talons de paie',
  'agentChat.layout.titleQuickStart': 'Démarrage rapide',
  'agentChat.layout.placeholderQuickStart': 'Décrivez les documents à traiter, ou joignez des exemples pour commencer',
  'agentChat.layout.newSessionNotice':
    'Nouvelle conversation commencée. Les agents Code Intelligence et MCP externes ne sont pas utilisés dans une conversation qui contient des résultats d’Analytics ou d’Error Analyzer.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;
const SEND_OPTIONS = { enableCodeIntelligence: false, enableExternalMcpAgents: false };
/** Stores the new-session token in the chat state, as opting in does when it starts a new session. */
const StartedNewSession = (): null => {
  const { updateAgentChatState } = useAgentChatContext();
  useEffect(() => {
    updateAgentChatState({ sessionNotice: NEW_SESSION_NOTICE });
  }, [updateAgentChatState]);
  return null;
};
/** The page in `mode`, inside the app's real chat state; `notice` stores the new-session token first. */
const page = (mode: 'chat' | 'quick_start', notice = false): React.JSX.Element => (
  <MemoryRouter>
    <AgentChatProvider initialMode={mode}>
      {notice && <StartedNewSession />}
      <AgentChatLayout />
    </AgentChatProvider>
  </MemoryRouter>
);
/** The suggested prompt `id`. */
const chip = (id: string) => createChatWrapper(document.body).findSupportPromptGroup()!.findItemById(id)!;
/** The message box. */
const promptInput = () => createWrapper(document.body).findPromptInput()!;
/** Clicks the suggested prompt `id`, then the send button, and returns what the box held before sending. */
const choosePromptAndSend = async (id: string): Promise<string> => {
  chip(id).click();
  await waitFor(() => expect(promptInput().findNativeTextarea().getElement().value).not.toBe(''));
  const inserted = promptInput().findNativeTextarea().getElement().value;
  promptInput().findActionButton().click();
  await waitFor(() => expect(state.chat.sendMessage).toHaveBeenCalledTimes(1));
  return inserted;
};
beforeEach(() => {
  state.chat.sendMessage.mockReset();
  state.chat.sendMessage.mockResolvedValue(undefined);
});
afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});
describe('Agent Chat and Quick Start under fr-CA', () => {
  it('shows a chat suggested prompt in French, puts its English into the box and sends the English', async () => {
    renderWithIntl(page('chat'), FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() => expect(chip('GeneralAgent').getElement()).toHaveTextContent(FR['agentChat.layout.chip.capabilities']));
    expect(screen.queryByText(CHAT_PROMPT)).toBeNull();
    expect(await choosePromptAndSend('GeneralAgent')).toBe(CHAT_PROMPT);
    expect(state.chat.sendMessage).toHaveBeenCalledWith(CHAT_PROMPT, SEND_OPTIONS);
  });
  it('shows a Quick Start suggested prompt in French, puts its English into the box and sends the English', async () => {
    renderWithIntl(page('quick_start'), FR_CA);
    await waitFor(() => expect(chip('QuickStartPaystub').getElement()).toHaveTextContent(FR['agentChat.layout.chip.paystubs']));
    expect(screen.queryByText(QUICK_START_PROMPT)).toBeNull();
    expect(await choosePromptAndSend('QuickStartPaystub')).toBe(QUICK_START_PROMPT);
    expect(state.chat.sendMessage).toHaveBeenCalledWith(QUICK_START_PROMPT, SEND_OPTIONS);
  });
  it("reads Quick Start's heading and placeholder in French", async () => {
    renderWithIntl(page('quick_start'), FR_CA);
    expect(await screen.findByRole('heading', { name: FR['agentChat.layout.titleQuickStart'] })).toBeInTheDocument();
    expect(promptInput().findNativeTextarea().getElement()).toHaveAttribute('placeholder', FR['agentChat.layout.placeholderQuickStart']);
    expect(screen.queryByRole('heading', { name: 'Quick Start' })).toBeNull();
  });
  it('words the stored new-session token as the French notice', async () => {
    renderWithIntl(page('chat', true), FR_CA);
    expect(await screen.findByText(FR['agentChat.layout.newSessionNotice'])).toBeInTheDocument();
    expect(screen.queryByText(NEW_SESSION_NOTICE)).toBeNull();
    expect(screen.queryByText(NOTICE)).toBeNull();
  });
});
describe('Agent Chat and Quick Start under en', () => {
  it('shows and sends the chat suggested prompt in English', async () => {
    renderWithIntl(page('chat'));
    expect(chip('GeneralAgent').getElement()).toHaveTextContent(CHAT_PROMPT);
    expect(await choosePromptAndSend('GeneralAgent')).toBe(CHAT_PROMPT);
    expect(state.chat.sendMessage).toHaveBeenCalledWith(CHAT_PROMPT, SEND_OPTIONS);
  });
  it('shows and sends the Quick Start suggested prompt in English', async () => {
    renderWithIntl(page('quick_start'));
    expect(chip('QuickStartPaystub').getElement()).toHaveTextContent(QUICK_START_PROMPT);
    expect(await choosePromptAndSend('QuickStartPaystub')).toBe(QUICK_START_PROMPT);
    expect(state.chat.sendMessage).toHaveBeenCalledWith(QUICK_START_PROMPT, SEND_OPTIONS);
  });
  it("reads Quick Start's heading and placeholder in English", () => {
    renderWithIntl(page('quick_start'));
    expect(screen.getByRole('heading', { name: 'Quick Start' })).toBeInTheDocument();
    expect(promptInput().findNativeTextarea().getElement()).toHaveAttribute('placeholder', QUICK_START_PLACEHOLDER);
  });
  it('words the stored new-session token as the English notice', async () => {
    renderWithIntl(page('chat', true));
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    expect(screen.queryByText(NEW_SESSION_NOTICE)).toBeNull();
  });
});
