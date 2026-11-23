// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Agent Chat's help panel and its GitHub issue button under `fr-CA`: with a fixture catalog the
 * panel's heading, an agent's Description label and its sample-query button's label read French,
 * while the agent's name, description and query stay as the registry sends them. The custom MCP
 * agents dialog reads its title, its Close button and its guide link in French, and the issue
 * button reads its label and both menu items in French. Every documentation link keeps its href,
 * and both issue links open the same English issue forms in either language. Under `en` the same
 * renders read today's English.
 */
import React from 'react';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import { buildBugReportUrl, buildFeatureRequestUrl } from '../../../utils/github-feedback';
// The panel calls generateClient() at import time, so the client is hoisted. The issue button
// reads the deployment through a hook, mocked with one value for every render.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  deployment: { version: '1.0.0', region: 'us-east-1', stackName: 'idp-stack', pattern: 'Pattern-2' },
}));
vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('../../../hooks/use-deployment-context', () => ({ default: () => state.deployment }));
import ToolsPanel from '../tools-panel';
import CreateIssueButton from '../../common/create-issue-button';
const DOCS = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';
const MCP_GUIDE =
  'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/custom-MCP-agent.md';
/** One agent as the registry sends it: its name, description and sample query are data. */
const AGENT = {
  agent_id: 'analytics-agent',
  agent_name: 'Analytics Agent',
  agent_description: 'Answers questions about processed documents.',
  sample_queries: ['How many documents failed?'],
};
const QUERY = AGENT.sample_queries[0];
/** The chat answer the button attaches to both issue forms. */
const FINDINGS = 'The agent could not read the configuration.';
/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'helpPanel.agentChat.title': 'Agents disponibles',
  'helpPanel.agentChat.description': 'Description :',
  'helpPanel.agentChat.insertSampleQuery': 'Insérer l’exemple de requête : {query}',
  'helpPanel.agentChat.mcpButton': 'NOUVEAU : intégrez vos propres systèmes avec MCP!',
  'helpPanel.agentChat.docsAgentChat': 'Clavardage avec l’agent compagnon',
  'helpPanel.agentChat.docsCustomMcp': 'Agents MCP personnalisés',
  'helpPanel.agentChat.docsMcpServer': 'Serveur MCP',
  'helpPanel.agentChat.mcp.title': 'Agents MCP personnalisés',
  'helpPanel.agentChat.mcp.close': 'Fermer',
  'helpPanel.agentChat.mcp.learnMoreBody':
    'Pour les instructions détaillées et des exemples, consultez la <link>documentation des agents MCP personnalisés</link>',
  'common.createIssueButton.label': 'Créer un ticket GitHub',
  'common.createIssueButton.reportBug': 'Signaler un bogue',
  'common.createIssueButton.requestFeature': 'Demander une fonctionnalité',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;
/** The agent's section, once the list has loaded. */
const agentSection = () => createWrapper(document.body).findExpandableSection()!;
/** Expands the agent's section once the list shows it, and returns the section's content. */
const expandAgent = async (): Promise<HTMLElement> => {
  await screen.findByText(AGENT.agent_name);
  agentSection().findExpandButton().click();
  return waitFor(() => agentSection().findExpandedContent()!.getElement());
};
/** The issue button's dropdown. */
const issueMenu = () => createWrapper(document.body).findButtonDropdown()!;
/** The link of the open issue menu's item `id`. */
const itemLink = (id: string): HTMLAnchorElement => issueMenu().findItemById(id)!.getElement().querySelector('a')!;
/** The link whose text is `text`. */
const linkWithText = (scope: HTMLElement, text: string): HTMLAnchorElement => within(scope).getByText(text).closest('a')!;
beforeEach(() => {
  state.graphql.mockReset();
  state.graphql.mockResolvedValue({ data: { listAvailableAgents: [AGENT] } });
});
afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});
describe('the Agent Chat help panel under fr-CA', () => {
  it("reads its heading, an agent's Description label and the sample-query label in French, the agent's own text as sent", async () => {
    renderWithIntl(<ToolsPanel />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByRole('heading', { name: FR['helpPanel.agentChat.title'] })).toBeInTheDocument();
    const agent = await expandAgent();
    expect(within(agent).getByText(FR['helpPanel.agentChat.description'])).toBeInTheDocument();
    expect(within(agent).getByText(AGENT.agent_description)).toBeInTheDocument();
    expect(within(agent).getByRole('button', { name: `Insérer l’exemple de requête : ${QUERY}` })).toHaveTextContent(QUERY);
    expect(screen.queryByRole('heading', { name: 'Available Agents' })).toBeNull();
    expect(within(agent).queryByRole('button', { name: `Insert sample query: ${QUERY}` })).toBeNull();
  });
  it('reads its three documentation links in French, each keeping its href', async () => {
    renderWithIntl(<ToolsPanel />, FR_CA);
    expect(await screen.findByRole('link', { name: FR['helpPanel.agentChat.docsAgentChat'] })).toHaveAttribute(
      'href',
      `${DOCS}/agent-companion-chat/`,
    );
    expect(screen.getByRole('link', { name: FR['helpPanel.agentChat.docsCustomMcp'] })).toHaveAttribute(
      'href',
      `${DOCS}/custom-MCP-agent/`,
    );
    expect(screen.getByRole('link', { name: FR['helpPanel.agentChat.docsMcpServer'] })).toHaveAttribute('href', `${DOCS}/mcp-server/`);
  });
  it('opens the custom MCP agents dialog from its French button, with the title, Close and the guide link in French', async () => {
    renderWithIntl(<ToolsPanel />, FR_CA);
    fireEvent.click(await screen.findByRole('button', { name: `🚀 ${FR['helpPanel.agentChat.mcpButton']}` }));
    const dialog = await screen.findByRole('dialog', { name: FR['helpPanel.agentChat.mcp.title'] });
    expect(within(dialog).getByRole('button', { name: FR['helpPanel.agentChat.mcp.close'] })).toBeInTheDocument();
    expect(linkWithText(dialog, 'documentation des agents MCP personnalisés')).toHaveAttribute('href', MCP_GUIDE);
    expect(within(dialog).queryByText('Custom MCP Agent Documentation')).toBeNull();
  });

  // The catalog arrives after the first render, as a new `intl`; that must not load the list again.
  it('loads the agent list once, not again when the catalog arrives', async () => {
    renderWithIntl(<ToolsPanel />, FR_CA);
    expect(await screen.findByRole('heading', { name: FR['helpPanel.agentChat.title'] })).toBeInTheDocument();
    expect(await screen.findByText(AGENT.agent_name)).toBeInTheDocument();
    expect(state.graphql).toHaveBeenCalledTimes(1);
  });
});
describe('the GitHub issue button under fr-CA', () => {
  it('reads its label and both menu items in French, each opening the same English issue form', async () => {
    renderWithIntl(<CreateIssueButton findings={FINDINGS} />, FR_CA);
    expect(await screen.findByRole('button', { name: FR['common.createIssueButton.label'] })).toHaveTextContent(
      FR['common.createIssueButton.label'],
    );
    issueMenu().openDropdown();
    expect(issueMenu().findItemById('bug')!.getElement()).toHaveTextContent(FR['common.createIssueButton.reportBug']);
    expect(issueMenu().findItemById('feature')!.getElement()).toHaveTextContent(FR['common.createIssueButton.requestFeature']);
    expect(itemLink('bug')).toHaveAttribute('href', buildBugReportUrl(state.deployment, undefined, FINDINGS));
    expect(itemLink('feature')).toHaveAttribute('href', buildFeatureRequestUrl(state.deployment, FINDINGS));
    expect(screen.queryByRole('button', { name: 'Create GitHub issue' })).toBeNull();
  });
});
describe('the Agent Chat help panel and the GitHub issue button under en', () => {
  it('reads the inline English for the heading, the Description label and the sample-query label', async () => {
    renderWithIntl(<ToolsPanel />);
    expect(screen.getByRole('heading', { name: 'Available Agents' })).toBeInTheDocument();
    const agent = await expandAgent();
    expect(within(agent).getByText('Description:')).toBeInTheDocument();
    expect(within(agent).getByRole('button', { name: `Insert sample query: ${QUERY}` })).toHaveTextContent(QUERY);
  });
  it('reads the documentation links in English, with the same hrefs', () => {
    renderWithIntl(<ToolsPanel />);
    expect(screen.getByRole('link', { name: 'Agent Companion Chat' })).toHaveAttribute('href', `${DOCS}/agent-companion-chat/`);
    expect(screen.getByRole('link', { name: 'Custom MCP Agents' })).toHaveAttribute('href', `${DOCS}/custom-MCP-agent/`);
    expect(screen.getByRole('link', { name: 'MCP Server' })).toHaveAttribute('href', `${DOCS}/mcp-server/`);
  });
  it('opens the custom MCP agents dialog with its English title, Close and guide link, the link keeping its href', async () => {
    renderWithIntl(<ToolsPanel />);
    fireEvent.click(screen.getByRole('button', { name: '🚀 NEW: Integrate your own systems with MCP!' }));
    const dialog = await screen.findByRole('dialog', { name: 'Custom MCP Agents' });
    expect(within(dialog).getByRole('button', { name: 'Close' })).toBeInTheDocument();
    expect(linkWithText(dialog, 'Custom MCP Agent Documentation')).toHaveAttribute('href', MCP_GUIDE);
  });
  it('reads the issue button and its items in English, with the same issue links', () => {
    renderWithIntl(<CreateIssueButton findings={FINDINGS} />);
    expect(screen.getByRole('button', { name: 'Create GitHub issue' })).toHaveTextContent('Create GitHub issue');
    issueMenu().openDropdown();
    expect(issueMenu().findItemById('bug')!.getElement()).toHaveTextContent('Report a bug');
    expect(issueMenu().findItemById('feature')!.getElement()).toHaveTextContent('Request a feature');
    expect(itemLink('bug')).toHaveAttribute('href', buildBugReportUrl(state.deployment, undefined, FINDINGS));
    expect(itemLink('feature')).toHaveAttribute('href', buildFeatureRequestUrl(state.deployment, FINDINGS));
  });
});
