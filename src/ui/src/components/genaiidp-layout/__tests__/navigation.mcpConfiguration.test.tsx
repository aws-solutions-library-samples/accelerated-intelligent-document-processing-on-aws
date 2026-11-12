// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The MCP Configuration entry of the Admin navigation: shown only where the stack
 * has an MCP server, and its text comes from the message catalog, so it follows
 * the UI's language while staying hidden or shown by its link rather than its text.
 */

import React from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IntlProvider } from 'react-intl';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  settings: {} as Record<string, unknown>,
  nav: null as SideNavigationProps | null,
}));

vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ loading: false, isAdmin: true, isAuthor: false, isViewerOnly: false, isQueueWorker: false }),
}));
vi.mock('../../../hooks/use-installed-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-catalog-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-latest-version', () => ({
  default: () => ({ latestVersion: null, templateUrl: null, isUpdateAvailable: false }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: state.settings }) }));
vi.mock('../../../hooks/use-inbox', () => ({ default: () => ({ unreadCount: null }) }));
vi.mock('@cloudscape-design/components', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@cloudscape-design/components')>()),
  SideNavigation: (props: SideNavigationProps) => {
    state.nav = props;
    return null;
  },
}));

import Navigation from '../navigation';
import { MCP_CONFIGURATION_PATH } from '../../../routes/constants';

const HREF = `#${MCP_CONFIGURATION_PATH}`;

const mcpLink = (locale = 'en', messages: Record<string, string> = {}): SideNavigationProps.Link | undefined => {
  render(
    <IntlProvider locale={locale} defaultLocale="en" messages={messages}>
      <MemoryRouter initialEntries={['/documents']}>
        <Navigation />
      </MemoryRouter>
    </IntlProvider>,
  );
  const configuration = (state.nav!.items ?? []).find(
    (i) =>
      i.type === 'section' && ((i as SideNavigationProps.Section).items ?? []).some((l) => (l as SideNavigationProps.Link).href === HREF),
  ) as SideNavigationProps.Section | undefined;
  return configuration?.items?.find((l) => (l as SideNavigationProps.Link).href === HREF) as SideNavigationProps.Link | undefined;
};

beforeEach(() => {
  state.nav = null;
  state.settings = {};
  document.body.innerHTML = '';
});

describe('MCP Configuration navigation entry', () => {
  it('is hidden on a stack without an MCP server', () => {
    expect(mcpLink()).toBeUndefined();
  });

  it('is shown, in English, on a stack with an MCP server', () => {
    state.settings = { McpServerEndpoint: 'https://gw.example/mcp' };
    expect(mcpLink()?.text).toBe('MCP Configuration');
  });

  it('takes its text from the catalog of the active locale', () => {
    state.settings = { McpServerEndpoint: 'https://gw.example/mcp' };
    expect(mcpLink('fr-CA', { 'navigation.configuration.mcpConfiguration': 'Configuration MCP' })?.text).toBe('Configuration MCP');
  });

  it('stays hidden in another locale on a stack without an MCP server', () => {
    expect(mcpLink('fr-CA', { 'navigation.configuration.mcpConfiguration': 'Configuration MCP' })).toBeUndefined();
  });
});
