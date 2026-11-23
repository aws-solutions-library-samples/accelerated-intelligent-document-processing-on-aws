// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The agent output that Troubleshoot, the agents page and Agent Chat show, under `fr-CA`: with a
 * fixture catalog a text, a table and a chart result read their headings in French, the table's
 * settings and the chart's type picker too, Chart.js is handed the UI's formatting locale, and
 * the thought process reads its heading and its role names in French. The agent's own text,
 * cells, chart labels, messages and tool names, and the Athena schema reference the thought
 * process shows, stay as they are. Under `en` the same renders read today's English, and the
 * chart options are today's.
 */
import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

interface ChartProps {
  data: { labels: string[] };
  options: Record<string, unknown>;
}

// Chart.js draws on a canvas, which jsdom lacks: each chart is a stub that records the props it is handed.
const state = vi.hoisted(() => ({ chart: vi.fn<(type: string, props: ChartProps) => void>() }));
vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('react-chartjs-2', () => {
  const stub = (type: string) => (props: ChartProps) => {
    state.chart(type, props);
    return null;
  };
  return { Bar: stub('bar'), Line: stub('line'), Pie: stub('pie'), Doughnut: stub('doughnut') };
});

import AgentResultDisplay from '../AgentResultDisplay';
import AgentMessagesDisplay from '../AgentMessagesDisplay';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'agents.result.resultsFor': 'Résultats pour : <i>{query}</i>',
  'agents.result.responseType': 'Type de réponse : <strong>{type}</strong>',
  'agents.text.title': 'Réponse textuelle',
  'agents.table.title': 'Résultats du tableau',
  'agents.table.preferences.title': 'Préférences du tableau',
  'agents.table.preferences.pageSizeOption': '{count, plural, one {# ligne} other {# lignes}}',
  'agents.plot.type.bar': 'Diagramme à barres',
  'agents.plot.defaultTitle': 'Graphique',
  'agents.thoughtProcess.title': 'Processus de réflexion de l’agent',
  'agents.thoughtProcess.role.user': 'Utilisateur',
  'agents.thoughtProcess.role.assistant': 'Assistant IA',
  'agents.thoughtProcess.role.tool': 'Outil',
  'agents.thoughtProcess.viewInfo': 'Voir le schéma',
  'agents.thoughtProcess.schemaTitle': 'Schéma de la base de données',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const QUERY = 'How many documents failed last week?';
const TEXT_RESULT = { responseType: 'text', textData: { content: 'The agent found **3** failed documents.' } };
const TABLE_RESULT = {
  responseType: 'table',
  tableData: {
    headers: [
      { id: 'document', label: 'Document' },
      { id: 'status', label: 'Status' },
    ],
    rows: [{ id: 'r1', data: { document: 'in/invoice.pdf', status: 'FAILED' } }],
  },
};
const PLOT_RESULT = {
  responseType: 'plotData',
  plotData: [
    { type: 'bar', data: { labels: ['January', 'February'], datasets: [{ label: 'Failed documents', data: [1234, 5678] }] }, options: {} },
  ],
};
// A question, then an answer that reads the database schema: the answer splits into its text and a tool step.
const AGENT_MESSAGES = JSON.stringify([
  { role: 'user', content: 'How many documents failed?', sequence_number: 1 },
  {
    role: 'assistant',
    content: [{ text: 'Let me read the schema first.' }, { toolUse: { name: 'get_database_info', input: {} } }],
    sequence_number: 2,
  },
]);

/** The props the last chart drawn was handed. */
const lastChart = (): ChartProps => state.chart.mock.lastCall![1];

/** The chart type picker's trigger, which shows the selected option's label. */
const chartTypeTrigger = (): HTMLElement => createWrapper(document.body).findSelect()!.findTrigger().getElement();

beforeEach(() => {
  state.chart.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the agent results under fr-CA', () => {
  it('reads a text result in French, the question and the agent text as they arrive', async () => {
    renderWithIntl(<AgentResultDisplay result={TEXT_RESULT} query={QUERY} />, FR_CA);

    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByRole('heading', { name: 'Réponse textuelle' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: `Résultats pour : ${QUERY}` })).toBeInTheDocument();
    expect(screen.getByText(QUERY, { selector: 'i' })).toBeInTheDocument();
    expect(screen.getByText('text', { selector: 'strong' }).parentElement).toHaveTextContent('Type de réponse : text');
    expect(screen.getByText(/The agent found/)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Text Response' })).toBeNull();
    expect(screen.queryByText(/Results for:/)).toBeNull();
  });

  it('reads a table result and its settings in French, the cells as they arrive', async () => {
    renderWithIntl(<AgentResultDisplay result={TABLE_RESULT} query={QUERY} />, FR_CA);

    expect(await screen.findByRole('heading', { name: 'Résultats du tableau' })).toBeInTheDocument();
    expect(screen.getByText('in/invoice.pdf')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Préférences du tableau' }));
    expect(await screen.findByText('10 lignes')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Table Results' })).toBeNull();
    expect(screen.queryByText('10 rows')).toBeNull();
  });

  it('reads a chart result in French and hands Chart.js the UI formatting locale, the chart labels as they arrive', async () => {
    const { setLocale } = renderWithIntl(<AgentResultDisplay result={PLOT_RESULT} query={QUERY} />, FR_CA);

    expect(await screen.findByRole('heading', { name: 'Graphique' })).toBeInTheDocument();
    expect(chartTypeTrigger()).toHaveTextContent('Diagramme à barres');
    expect(state.chart.mock.lastCall![0]).toBe('bar');
    expect(lastChart().options.locale).toBe('fr-CA');
    expect(lastChart().data.labels).toEqual(['January', 'February']);

    // The picker keeps the chart type, not its label: a language switch relabels the selected option.
    setLocale('en');
    expect(chartTypeTrigger()).toHaveTextContent('Bar Chart');
    expect(lastChart().options.locale).toBeUndefined();
  });
});

describe('the agent thought process under fr-CA', () => {
  it('reads its heading and the role names in French, the messages, tool names and schema reference as they arrive', async () => {
    renderWithIntl(<AgentMessagesDisplay agentMessages={AGENT_MESSAGES} />, FR_CA);

    expect(await screen.findByRole('heading', { name: 'Processus de réflexion de l’agent' })).toBeInTheDocument();
    expect(screen.getByText('Utilisateur')).toBeInTheDocument();
    expect(screen.getByText('Assistant IA')).toBeInTheDocument();
    expect(screen.getByText('Outil')).toBeInTheDocument();
    expect(screen.getByText('How many documents failed?')).toBeInTheDocument();
    expect(screen.getByText('Let me read the schema first.')).toBeInTheDocument();
    expect(screen.getByText('get_database_info')).toBeInTheDocument();
    expect(screen.queryByText('Assistant')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Agent Thought Process' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Voir le schéma' }));
    expect(await screen.findByRole('dialog', { name: 'Schéma de la base de données' })).toHaveTextContent(
      '# Comprehensive Athena Database Schema',
    );
  });
});

describe('the agent output under en', () => {
  it('reads the inline English for a text result', () => {
    renderWithIntl(<AgentResultDisplay result={TEXT_RESULT} query={QUERY} />);

    expect(screen.getByRole('heading', { name: 'Text Response' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: `Results for: ${QUERY}` })).toBeInTheDocument();
    expect(screen.getByText(QUERY, { selector: 'i' })).toBeInTheDocument();
    expect(screen.getByText('text', { selector: 'strong' }).parentElement).toHaveTextContent('Response type: text');
  });

  it('reads the inline English for a table result and its settings', async () => {
    renderWithIntl(<AgentResultDisplay result={TABLE_RESULT} query={QUERY} />);

    expect(screen.getByRole('heading', { name: 'Table Results' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Preferences' }));
    expect(await screen.findByText('10 rows')).toBeInTheDocument();
  });

  it('reads the inline English for a chart result and hands Chart.js the same options as before', async () => {
    renderWithIntl(<AgentResultDisplay result={PLOT_RESULT} query={QUERY} />);

    expect(await screen.findByRole('heading', { name: 'Chart' })).toBeInTheDocument();
    expect(chartTypeTrigger()).toHaveTextContent('Bar Chart');
    expect(lastChart().options.locale).toBeUndefined();
    expect(lastChart().options).toEqual({ responsive: true, maintainAspectRatio: false, plugins: {} });
  });

  it('reads the inline English for the thought process heading and role names, and the same schema reference', async () => {
    renderWithIntl(<AgentMessagesDisplay agentMessages={AGENT_MESSAGES} />);

    expect(screen.getByRole('heading', { name: 'Agent Thought Process' })).toBeInTheDocument();
    expect(screen.getByText('User')).toBeInTheDocument();
    expect(screen.getByText('Assistant')).toBeInTheDocument();
    expect(screen.getByText('Tool')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'View Info' }));
    expect(await screen.findByRole('dialog', { name: 'Database Schema Information' })).toHaveTextContent(
      '# Comprehensive Athena Database Schema',
    );
  });
});
