// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The document page's chat panel under `fr-CA`: with a fixture catalog its title, the model
 * picker's label, the configuration-profile description under it and the Send button read
 * French, and once a reply starts streaming the status pill reads the French streaming label
 * while the streamed text stays as it arrives. Under `en` the same renders read today's English.
 */
import React from 'react';
import { act, fireEvent, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { StreamChatOptions } from '../../../api/stream-client';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render (a hook mock that returned fresh objects would
// re-render forever). The panel calls generateClient() at import time, so the client is hoisted too.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  streamChat: vi.fn<(options: StreamChatOptions) => Promise<void>>(),
  config: {
    schema: { properties: { chat: { properties: { model: { enum: ['model-a', 'model-b'] } } } } },
    mergedConfig: { chat: { model: 'model-a' } },
  },
  creds: { currentCredentials: { accessKeyId: 'AKIDEXAMPLE', secretAccessKey: 'secret', sessionToken: 'token' } },
}));
vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => state.config }));
vi.mock('../../../hooks/use-current-session-creds', () => ({ default: () => state.creds }));
// A stream URL is configured, so the panel streams its reply (the panel reads it at import time).
vi.mock('../../../aws-exports', () => ({ default: {}, apiBaseUrl: '', streamUrl: 'https://stream.example.com/', awsRegion: 'us-east-1' }));
vi.mock('../../../api/stream-client', () => ({ streamChat: state.streamChat }));
vi.mock('../../../api/chat-poll', () => ({ fetchAssistantKeys: vi.fn(), pollForAssistantReply: vi.fn() }));

import ChatPanel from '../ChatPanel';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.chat.title': 'Clavarder avec le document',
  'documents.chat.model.label': 'Modèle',
  'documents.chat.model.description':
    'Par défaut selon le profil de configuration « {profile} ». Choisissez un modèle à contexte plus étendu si le document est très long.',
  'documents.chat.send': 'Envoyer',
  'documents.chat.status.streaming': 'Diffusion de la réponse…',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** The status pill's text, or null while the panel shows none. */
const statusText = (): string | null => createWrapper().findStatusIndicator()?.getElement().textContent ?? null;

/** Types `prompt` into the message box and sends it with the button labelled `send`. */
const sendMessage = async (prompt: string, send: string): Promise<void> => {
  fireEvent.change(document.getElementById('chatTextarea')!, { target: { value: prompt } });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: send }));
  });
};

beforeEach(() => {
  state.streamChat.mockReset();
  // The reply starts streaming: one delta for this panel's session, and the stream stays open.
  state.streamChat.mockImplementation(({ body, onEvent }) => {
    onEvent({ sessionId: body.sessionId, method: 'assistant_stream', content: 'Partial answer' });
    return new Promise<void>(() => undefined);
  });
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the document chat panel under fr-CA', () => {
  it('reads its title, the model label and description and the Send button in French', async () => {
    renderWithIntl(<ChatPanel objectKey="in/a.pdf" configVersion="v2" />, FR_CA);

    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByRole('heading', { name: 'Clavarder avec le document' })).toBeInTheDocument();
    expect(screen.getByText('Modèle')).toBeInTheDocument();
    expect(
      screen.getByText(
        'Par défaut selon le profil de configuration « v2 ». Choisissez un modèle à contexte plus étendu si le document est très long.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Envoyer' })).toBeInTheDocument();
    expect(screen.queryByText('Chat With the Document')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
  });

  it('shows the French streaming label while a reply streams, the streamed text as it arrives', async () => {
    renderWithIntl(<ChatPanel objectKey="in/a.pdf" configVersion="v2" />, FR_CA);
    await screen.findByRole('button', { name: 'Envoyer' });

    await sendMessage('What is this document about?', 'Envoyer');

    expect(state.streamChat).toHaveBeenCalledTimes(1);
    expect(statusText()).toBe('Diffusion de la réponse…');
    expect(screen.getByText('Partial answer')).toBeInTheDocument();
  });
});

describe('the document chat panel under en', () => {
  it('reads the inline English for its title, the model label and description and the Send button', () => {
    renderWithIntl(<ChatPanel objectKey="in/a.pdf" configVersion="v2" />);

    expect(screen.getByRole('heading', { name: 'Chat With the Document' })).toBeInTheDocument();
    expect(screen.getByText('Model')).toBeInTheDocument();
    expect(
      screen.getByText('Default from configuration profile "v2". Choose a larger-context model if the document is very long.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('shows the English streaming label while a reply streams', async () => {
    renderWithIntl(<ChatPanel objectKey="in/a.pdf" configVersion="v2" />);

    await sendMessage('What is this document about?', 'Send');

    expect(state.streamChat).toHaveBeenCalledTimes(1);
    expect(statusText()).toBe('Streaming response…');
    expect(screen.getByText('Partial answer')).toBeInTheDocument();
  });
});
