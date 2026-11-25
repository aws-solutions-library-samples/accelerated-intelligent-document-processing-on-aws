// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Knowledge Base page and its help panel under `fr-CA`: with a fixture catalog the page's
 * heading, the question box's placeholder (with the knowledge base on and off) and the Submit
 * button read French, and so do the help panel's heading and its documentation link, the link
 * keeping its docs-site `href`. A failed query reads as one French sentence, its action phrase
 * included. Under `en` the same renders read today's English.
 */
import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { I18n } from 'aws-amplify/utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SettingsContext, type SettingsContextValue } from '../../../contexts/settings';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// The page calls generateClient() at import time, so the client is hoisted; the tests that send a
// question make it reject.
const state = vi.hoisted(() => ({ graphql: vi.fn() }));
vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));

import DocumentsQueryLayout from '../DocumentsQueryLayout';
import KnowledgeBaseToolsPanel from '../tools-panel';

const DOCS = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'knowledgeBase.query.title': 'Outil de requête de la base de connaissances des documents',
  'knowledgeBase.query.placeholder': 'Entrez une question pour interroger la base de connaissances de vos documents.',
  'knowledgeBase.query.placeholderDisabled': 'La base de connaissances des documents est réglée à DISABLED pour ce déploiement GenAIIDP.',
  'knowledgeBase.query.submit': 'Soumettre',
  'knowledgeBase.query.queryAction': 'interroger la base de connaissances',
  'common.apiError.failed': 'Échec de l’opération « {action} » : {error}',
  'helpPanel.knowledgeBase.title': 'Requêtes dans la base de connaissances des documents',
  'helpPanel.knowledgeBase.docsKnowledgeBase': 'Base de connaissances',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** What the dispatcher sends for a resolver that raised, as the client throws it. */
const SERVER_ERROR = { errors: [{ message: 'boom', errorType: 'InternalError' }] };

// The deployment's settings through the app's own context, one value each for every render:
// the knowledge base on, and off.
const KB_ON: SettingsContextValue = { settings: { ShouldUseDocumentKnowledgeBase: 'true' } };
const KB_OFF: SettingsContextValue = { settings: { ShouldUseDocumentKnowledgeBase: 'false' } };
const KnowledgeBaseOn = ({ children }: { children: React.ReactNode }) => (
  <SettingsContext.Provider value={KB_ON}>{children}</SettingsContext.Provider>
);
const KnowledgeBaseOff = ({ children }: { children: React.ReactNode }) => (
  <SettingsContext.Provider value={KB_OFF}>{children}</SettingsContext.Provider>
);

afterEach(() => {
  state.graphql.mockReset();
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the Knowledge Base page under fr-CA', () => {
  it('reads its heading, the question box placeholder and the Submit button in French', async () => {
    renderWithIntl(<DocumentsQueryLayout />, { ...FR_CA, wrapper: KnowledgeBaseOn });
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByRole('heading', { name: FR['knowledgeBase.query.title'] })).toBeInTheDocument();
    expect(screen.getByPlaceholderText(FR['knowledgeBase.query.placeholder'])).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Soumettre' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Documents Knowledge Base Query Tool' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Submit' })).toBeNull();
  });

  it('reads the placeholder in French when the deployment has no knowledge base', async () => {
    renderWithIntl(<DocumentsQueryLayout />, { ...FR_CA, wrapper: KnowledgeBaseOff });
    expect(await screen.findByPlaceholderText(FR['knowledgeBase.query.placeholderDisabled'])).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Document Knowledge Base is set to DISABLED for this GenAIIDP deployment.')).toBeNull();
  });

  it('reads a failed query as one French sentence, its action phrase included', async () => {
    state.graphql.mockRejectedValue(SERVER_ERROR);
    renderWithIntl(<DocumentsQueryLayout />, { ...FR_CA, wrapper: KnowledgeBaseOn });
    fireEvent.change(await screen.findByPlaceholderText(FR['knowledgeBase.query.placeholder']), {
      target: { value: 'Quel est le total ?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Soumettre' }));
    expect(await screen.findByText('Échec de l’opération « interroger la base de connaissances » : boom')).toBeInTheDocument();
    expect(screen.queryByText(/Failed to/)).toBeNull();
    expect(state.graphql).toHaveBeenCalledTimes(1);
  });
});

describe('the Knowledge Base help panel under fr-CA', () => {
  it('reads its heading and its documentation link in French, the link keeping its docs href', async () => {
    renderWithIntl(<KnowledgeBaseToolsPanel />, FR_CA);
    expect(await screen.findByRole('heading', { name: FR['helpPanel.knowledgeBase.title'] })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: FR['helpPanel.knowledgeBase.docsKnowledgeBase'] })).toHaveAttribute(
      'href',
      `${DOCS}/knowledge-base/`,
    );
    expect(screen.queryByRole('heading', { name: 'Document Knowledge Base Query' })).toBeNull();
  });
});

describe('the Knowledge Base page and its help panel under en', () => {
  it('reads the inline English for the heading, the placeholder and the Submit button', () => {
    renderWithIntl(<DocumentsQueryLayout />, { wrapper: KnowledgeBaseOn });
    expect(screen.getByRole('heading', { name: 'Documents Knowledge Base Query Tool' })).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Enter a question to query your document knowledge base.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument();
  });

  it('reads the inline English placeholder when the deployment has no knowledge base', () => {
    renderWithIntl(<DocumentsQueryLayout />, { wrapper: KnowledgeBaseOff });
    expect(screen.getByPlaceholderText('Document Knowledge Base is set to DISABLED for this GenAIIDP deployment.')).toBeInTheDocument();
  });

  it('reads a failed query in English', async () => {
    state.graphql.mockRejectedValue(SERVER_ERROR);
    renderWithIntl(<DocumentsQueryLayout />, { wrapper: KnowledgeBaseOn });
    fireEvent.change(screen.getByPlaceholderText('Enter a question to query your document knowledge base.'), {
      target: { value: 'What is the total?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));
    expect(await screen.findByText('Failed to query the knowledge base: boom')).toBeInTheDocument();
  });

  it('reads the help panel heading and its documentation link in English, with the same href', () => {
    renderWithIntl(<KnowledgeBaseToolsPanel />);
    expect(screen.getByRole('heading', { name: 'Document Knowledge Base Query' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Knowledge Base' })).toHaveAttribute('href', `${DOCS}/knowledge-base/`);
  });
});
