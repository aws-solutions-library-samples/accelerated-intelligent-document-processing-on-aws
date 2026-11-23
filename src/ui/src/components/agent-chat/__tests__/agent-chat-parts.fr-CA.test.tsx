// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Three parts of Agent Chat under `fr-CA`: with a fixture catalog a tool's details read French
 * (the button, the dialog title, its tabs and the CloudWatch search summary, with the event
 * count of each log group), the Bedrock error card reads its title, its retry count and its
 * technical details in French, and the chat history reads its button, each session's line and
 * its delete label in French. The tool name, input and events, Bedrock's own message,
 * suggestions and details, and the session titles stay as they arrive. Under `en` the same
 * renders read today's English, except that a count of one is now singular: `1 message`,
 * `(1 event)`.
 */
import React from 'react';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { listChatSessions } from '../../../graphql/generated';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// The history calls generateClient() at import time, so the client is hoisted.
const state = vi.hoisted(() => ({ graphql: vi.fn() }));
vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));

import AgentToolComponent from '../AgentToolComponent';
import BedrockErrorMessage from '../BedrockErrorMessage';
import AgentChatHistoryDropdown from '../AgentChatHistoryDropdown';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'agentChat.tool.viewDetails': 'Voir les détails',
  'agentChat.tool.detailsTitle': 'Détails : {toolName}',
  'agentChat.tool.tab.execution': 'Détails de l’exécution',
  'agentChat.tool.tab.results': 'Résultats',
  'agentChat.tool.logs.summary': 'Résumé de la recherche',
  'agentChat.tool.logs.stack': 'Pile :',
  'agentChat.tool.logs.events': 'Événements du journal',
  'agentChat.tool.logs.groupEvents': '{logGroup} ({count, plural, one {# événement} other {# événements}})',
  'agentChat.bedrockError.title.rateLimit': 'Limite de requêtes dépassée',
  'agentChat.bedrockError.retries': '(Après {count, plural, one {# nouvelle tentative} other {# nouvelles tentatives}})',
  'agentChat.bedrockError.whatYouCanDo': 'Ce que vous pouvez faire :',
  'agentChat.bedrockError.technicalDetails': 'Détails techniques',
  'agentChat.bedrockError.errorType': 'Type d’erreur : {type}',
  'agentChat.bedrockError.details': 'Détails : {details}',
  'agentChat.bedrockError.retryAttempts': 'Nouvelles tentatives : {count}',
  'agentChat.history.button': 'Charger une conversation précédente',
  'agentChat.history.unknownDate': 'Date inconnue',
  'agentChat.history.sessionMeta': '{date} • {count, plural, one {# message} other {# messages}}',
  'agentChat.history.deleteAriaLabel': 'Supprimer la session de clavardage : {title}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

// The error analyzer's log search: its input, and a result with one log group holding one
// event and one holding three.
const TOOL = 'search_cloudwatch_logs';
const TIME = new Date(0).toISOString();
const logEvent = (stream: string, message: string) => ({ timestamp: TIME, log_stream: `idp/${stream}`, message });
const LOG_SEARCH = {
  stack_name: 'idp-stack',
  filter_pattern: 'ERROR',
  log_groups_searched: 2,
  total_log_groups_found: 6,
  total_events_found: 4,
  results: [
    { log_group: '/aws/lambda/idp-classify', events_found: 1, events: [logEvent('a', 'Classification timed out')] },
    {
      log_group: '/aws/lambda/idp-extract',
      events_found: 3,
      events: [logEvent('b', 'Extraction failed'), logEvent('c', 'Extraction retried'), logEvent('d', 'Extraction failed again')],
    },
  ],
};
const TOOL_INPUT = JSON.stringify({ document_id: 'invoice.pdf', filter_pattern: 'ERROR' });
const tool = (
  <AgentToolComponent
    toolName={TOOL}
    toolUseId="tool-1"
    executionDetails={TOOL_INPUT}
    resultDetails={JSON.stringify(LOG_SEARCH)}
    timestamp={TIME}
  />
);

// Bedrock throttling, as the agent's error handler reports it, after one retry and after three.
const RATE_LIMIT = {
  errorType: 'rate_limit',
  message: 'Too many requests are being processed right now. Please wait a moment before trying again.',
  technicalDetails: 'API rate limit exceeded',
  actionRecommendations: ['Wait 1-2 minutes before retrying', 'Reduce the frequency of your requests'],
};
const cards = (
  <>
    <BedrockErrorMessage errorInfo={{ ...RATE_LIMIT, retryAttempts: 1 }} />
    <BedrockErrorMessage errorInfo={{ ...RATE_LIMIT, retryAttempts: 3 }} />
  </>
);

// Two previous sessions, with one message and with three. Neither has a date, so each line
// starts with the unknown-date text.
const SESSIONS = [
  { sessionId: 'session-1', title: 'Where do I start?', messageCount: 1 },
  { sessionId: 'session-3', title: 'Which documents failed?', messageCount: 3 },
];

/** The second card's technical details, the one with three retries. */
const technicalDetails = () => createWrapper(document.body).findAllExpandableSections()[1];

/** Expands the second card's technical details and returns their content once it shows. */
const expandTechnicalDetails = async (): Promise<HTMLElement> => {
  technicalDetails().findExpandButton().click();
  return waitFor(() => technicalDetails().findExpandedContent()!.getElement());
};

/** The history's button dropdown. */
const history = () => createWrapper(document.body).findButtonDropdown()!;

/** Opens the history once it has loaded: its button is busy while it loads. */
const openHistory = async (): Promise<void> => {
  await waitFor(() => expect(history().findNativeButton().getElement()).not.toHaveAttribute('aria-disabled'));
  history().openDropdown();
};

beforeEach(() => {
  state.graphql.mockReset();
  state.graphql.mockImplementation(({ query }: { query: string }) =>
    query === listChatSessions
      ? Promise.resolve({ data: { listChatSessions: { items: SESSIONS } } })
      : Promise.reject(new Error('unexpected operation')),
  );
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the agent chat parts under fr-CA', () => {
  it("reads a tool's details in French, the tool name, its input and the log events as they arrive", async () => {
    renderWithIntl(tool, FR_CA);

    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    fireEvent.click(await screen.findByRole('button', { name: 'Voir les détails' }));
    expect(screen.getByText('Search Cloudwatch Logs', { selector: 'strong' })).toBeInTheDocument();
    expect(await screen.findByRole('dialog', { name: 'Détails : Search Cloudwatch Logs' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Détails de l’exécution' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Résultats' }));

    expect(await screen.findByText('Résumé de la recherche')).toBeInTheDocument();
    expect(screen.getByText('Pile :')).toBeInTheDocument();
    expect(screen.getByText('idp-stack')).toBeInTheDocument();
    expect(screen.getByText('Événements du journal')).toBeInTheDocument();
    expect(screen.getByText('/aws/lambda/idp-classify (1 événement)')).toBeInTheDocument();
    expect(screen.getByText('/aws/lambda/idp-extract (3 événements)')).toBeInTheDocument();
    expect(screen.getByText('Classification timed out')).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Execution Details' })).toBeNull();
    expect(screen.queryByText('Search Summary')).toBeNull();
  });

  it("reads the Bedrock error card in French, with one retry and with three, Bedrock's own text as it arrives", async () => {
    renderWithIntl(cards, FR_CA);

    expect(await screen.findAllByText('Limite de requêtes dépassée')).toHaveLength(2);
    expect(screen.getByText('(Après 1 nouvelle tentative)')).toBeInTheDocument();
    expect(screen.getByText('(Après 3 nouvelles tentatives)')).toBeInTheDocument();
    expect(screen.getAllByText('Ce que vous pouvez faire :')).toHaveLength(2);
    expect(screen.getAllByText(RATE_LIMIT.message)).toHaveLength(2);
    expect(screen.getAllByText(RATE_LIMIT.actionRecommendations[0])).toHaveLength(2);
    expect(screen.queryByText('Rate Limit Exceeded')).toBeNull();

    expect(technicalDetails().findHeaderText()!.getElement()).toHaveTextContent('Détails techniques');
    const details = await expandTechnicalDetails();
    expect(within(details).getByText('Type d’erreur : rate_limit')).toBeInTheDocument();
    expect(within(details).getByText('Détails : API rate limit exceeded')).toBeInTheDocument();
    expect(within(details).getByText('Nouvelles tentatives : 3')).toBeInTheDocument();
  });

  it("reads the chat history's button, each session's line and its delete label in French, the titles as they arrive", async () => {
    renderWithIntl(<AgentChatHistoryDropdown onSessionSelect={vi.fn()} />, FR_CA);

    await waitFor(() => expect(history().findNativeButton().getElement()).toHaveTextContent('Charger une conversation précédente'));
    await openHistory();
    expect(screen.getByText('Where do I start?')).toBeInTheDocument();
    expect(screen.getByText('Date inconnue • 1 message')).toBeInTheDocument();
    expect(screen.getByText('Date inconnue • 3 messages')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Supprimer la session de clavardage : Where do I start?' })).toBeInTheDocument();
    expect(screen.queryByText('Load previous chat')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete chat session: Where do I start?' })).toBeNull();
  });
});

describe('the agent chat parts under en', () => {
  it("reads the inline English for a tool's details, with one event in the singular", async () => {
    renderWithIntl(tool);

    fireEvent.click(screen.getByRole('button', { name: 'View Details' }));
    expect(await screen.findByRole('dialog', { name: 'Search Cloudwatch Logs Details' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Execution Details' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Results' }));

    expect(await screen.findByText('Search Summary')).toBeInTheDocument();
    expect(screen.getByText('Stack:')).toBeInTheDocument();
    expect(screen.getByText('Log Events')).toBeInTheDocument();
    expect(screen.getByText('/aws/lambda/idp-classify (1 event)')).toBeInTheDocument();
    expect(screen.getByText('/aws/lambda/idp-extract (3 events)')).toBeInTheDocument();
  });

  it('reads the inline English for the Bedrock error card, with one retry and with three', async () => {
    renderWithIntl(cards);

    expect(screen.getAllByText('Rate Limit Exceeded')).toHaveLength(2);
    expect(screen.getByText('(After 1 retry attempt)')).toBeInTheDocument();
    expect(screen.getByText('(After 3 retry attempts)')).toBeInTheDocument();
    expect(screen.getAllByText('What you can do:')).toHaveLength(2);

    expect(technicalDetails().findHeaderText()!.getElement()).toHaveTextContent('Technical Details');
    const details = await expandTechnicalDetails();
    expect(within(details).getByText('Error Type: rate_limit')).toBeInTheDocument();
    expect(within(details).getByText('Details: API rate limit exceeded')).toBeInTheDocument();
    expect(within(details).getByText('Retry Attempts: 3')).toBeInTheDocument();
  });

  it("reads the inline English for the chat history's button, each session's line, with one message in the singular, and its delete label", async () => {
    renderWithIntl(<AgentChatHistoryDropdown onSessionSelect={vi.fn()} />);

    expect(history().findNativeButton().getElement()).toHaveTextContent('Load previous chat');
    await openHistory();
    expect(screen.getByText('Unknown date • 1 message')).toBeInTheDocument();
    expect(screen.getByText('Unknown date • 3 messages')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete chat session: Where do I start?' })).toBeInTheDocument();
  });
});
