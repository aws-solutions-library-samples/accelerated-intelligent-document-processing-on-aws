// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The agents page under `fr-CA`, with the app's own analytics context: with a fixture catalog
 * its heading, the Select All and Deselect All button, the question history's button, its
 * entry's date and status, the entry's delete label and the four job statuses read French,
 * and so do the error label and the page's own text for a job that failed without a reason.
 * The agent and the question stay as they arrive. Under `en` the same renders read today's
 * English.
 */
import React from 'react';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { AnalyticsProvider } from '../../../contexts/analytics';
import { getAgentJobStatus, listAgentJobs, listAvailableAgents, onAgentJobComplete } from '../../../graphql/generated';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// The page and its query input call generateClient() at import time, so the client is hoisted,
// with the status the next job read returns.
const state = vi.hoisted(() => ({ graphql: vi.fn(), status: 'COMPLETED' }));
vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));

import DocumentsAgentsLayout from '../DocumentsAgentsLayout';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'agents.page.title': 'Analyse par agents',
  'agents.page.jobFailed': 'Le traitement de la tâche a échoué',
  'agents.queryInput.selectAll': 'Sélectionner tous les agents',
  'agents.queryInput.deselectAll': 'Désélectionner tous les agents',
  'agents.queryInput.historyButton': 'Sélectionner une question précédente',
  'agents.queryInput.unknownDate': 'Date inconnue',
  'agents.queryInput.completed': 'Terminée',
  'agents.queryInput.deleteAriaLabel': 'Supprimer la requête : {query}',
  'agents.jobStatus.pending': 'Tâche créée, en attente du début du traitement',
  'agents.jobStatus.processing': 'Traitement de votre requête',
  'agents.jobStatus.completed': 'Traitement terminé',
  'agents.jobStatus.failed': 'Échec du traitement',
  'agents.jobStatus.errorLabel': 'Erreur :',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

// One agent from the registry and one previous question. The question has no date, so its
// history entry shows the unknown-date text; a job read returns it with `state.status` and no
// reason, result or messages.
const AGENT = {
  agent_id: 'analytics',
  agent_name: 'Analytics Agent',
  agent_description: 'Answers questions about the processed documents.',
  sample_queries: ['How many documents failed last week?'],
};
const QUERY = 'What is in this document?';
const JOB = { jobId: 'job-1', query: QUERY, status: 'COMPLETED', agentIds: '["analytics"]' };

/** The page's one button dropdown: the question history. */
const history = () => createWrapper(document.body).findButtonDropdown()!;

/** Opens the question history once it has loaded: its button is busy while it loads. */
const openHistory = async (): Promise<void> => {
  await waitFor(() => expect(history().findNativeButton().getElement()).not.toHaveAttribute('aria-disabled'));
  history().openDropdown();
};

/** Picks the previous question, which the page then reads back as a job with `state.status`. */
const pickJob = async (): Promise<void> => {
  await openHistory();
  history().findItemById(JOB.jobId)!.click();
};

let consoleLog: MockInstance<typeof console.log>;

beforeEach(() => {
  state.status = 'COMPLETED';
  state.graphql.mockReset();
  state.graphql.mockImplementation(({ query }: { query: string }) => {
    if (query === onAgentJobComplete) return { subscribe: () => ({ unsubscribe: () => undefined }) };
    if (query === listAvailableAgents) return Promise.resolve({ data: { listAvailableAgents: [AGENT] } });
    if (query === listAgentJobs) return Promise.resolve({ data: { listAgentJobs: { items: [JOB] } } });
    if (query === getAgentJobStatus) {
      return Promise.resolve({
        data: { getAgentJobStatus: { ...JOB, status: state.status, result: null, error: null, agent_messages: null } },
      });
    }
    return Promise.reject(new Error('unexpected operation'));
  });
  // The history pick logs each of its steps with console.log.
  consoleLog = vi.spyOn(console, 'log').mockImplementation(() => undefined);
});

afterEach(() => {
  consoleLog.mockRestore();
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the agents page under fr-CA', () => {
  it('reads its heading, the agent buttons and the question history in French, the agent and the question as they arrive', async () => {
    renderWithIntl(<DocumentsAgentsLayout />, { ...FR_CA, wrapper: AnalyticsProvider });

    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByRole('heading', { name: 'Analyse par agents' })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Sélectionner tous les agents' }));
    expect(screen.getByRole('button', { name: 'Désélectionner tous les agents' })).toBeInTheDocument();
    expect(screen.getByText('Analytics Agent')).toBeInTheDocument();
    // With an agent selected, its first sample question is the placeholder, as the registry sends it.
    expect(screen.getByPlaceholderText(AGENT.sample_queries[0])).toBeInTheDocument();

    await openHistory();
    expect(history().findNativeButton().getElement()).toHaveTextContent('Sélectionner une question précédente');
    expect(screen.getByRole('button', { name: `Supprimer la requête : ${QUERY}` })).toBeInTheDocument();
    expect(screen.getByText(QUERY)).toBeInTheDocument();
    expect(screen.getByText('Date inconnue • Terminée')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Agent Analysis' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Select All Agents' })).toBeNull();
    expect(screen.queryByRole('button', { name: `Delete query: ${QUERY}` })).toBeNull();
  });

  it.each([
    ['PENDING', 'Tâche créée, en attente du début du traitement'],
    ['PROCESSING', 'Traitement de votre requête'],
    ['COMPLETED', 'Traitement terminé'],
  ])('reads a %s job status in French', async (status, text) => {
    state.status = status;
    renderWithIntl(<DocumentsAgentsLayout />, { ...FR_CA, wrapper: AnalyticsProvider });
    await screen.findByRole('heading', { name: 'Analyse par agents' });

    await pickJob();

    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('reads a failed job status, the error label and the page text for a job that failed without a reason in French', async () => {
    state.status = 'FAILED';
    renderWithIntl(<DocumentsAgentsLayout />, { ...FR_CA, wrapper: AnalyticsProvider });
    await screen.findByRole('heading', { name: 'Analyse par agents' });

    await pickJob();

    expect(await screen.findByText('Échec du traitement')).toBeInTheDocument();
    expect(screen.getByText('Erreur :', { selector: 'strong' }).parentElement).toHaveTextContent(
      'Erreur : Le traitement de la tâche a échoué',
    );
  });
});

describe('the agents page under en', () => {
  it('reads the inline English for its heading, the agent buttons and the question history', async () => {
    renderWithIntl(<DocumentsAgentsLayout />, { wrapper: AnalyticsProvider });

    expect(screen.getByRole('heading', { name: 'Agent Analysis' })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Select All Agents' }));
    expect(screen.getByRole('button', { name: 'Deselect All Agents' })).toBeInTheDocument();
    expect(screen.getByText('Analytics Agent')).toBeInTheDocument();
    expect(screen.getByPlaceholderText(AGENT.sample_queries[0])).toBeInTheDocument();

    await openHistory();
    expect(history().findNativeButton().getElement()).toHaveTextContent('Select a previous question');
    expect(screen.getByRole('button', { name: `Delete query: ${QUERY}` })).toBeInTheDocument();
    expect(screen.getByText(QUERY)).toBeInTheDocument();
    expect(screen.getByText('Unknown date • Completed')).toBeInTheDocument();
  });

  it.each([
    ['PENDING', 'Job created, waiting to start processing'],
    ['PROCESSING', 'Processing your query'],
    ['COMPLETED', 'Processing complete'],
  ])('reads the inline English for a %s job status', async (status, text) => {
    state.status = status;
    renderWithIntl(<DocumentsAgentsLayout />, { wrapper: AnalyticsProvider });

    await pickJob();

    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('reads the inline English for a failed job status, the error label and the page text for a job that failed without a reason', async () => {
    state.status = 'FAILED';
    renderWithIntl(<DocumentsAgentsLayout />, { wrapper: AnalyticsProvider });

    await pickJob();

    expect(await screen.findByText('Processing failed')).toBeInTheDocument();
    expect(screen.getByText('Error:', { selector: 'strong' }).parentElement).toHaveTextContent('Error: Job processing failed');
  });
});
