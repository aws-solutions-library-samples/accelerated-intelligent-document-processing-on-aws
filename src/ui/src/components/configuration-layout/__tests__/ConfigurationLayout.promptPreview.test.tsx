// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Preview prompts on the configuration page: a button beside Refresh, not a tab, that
 * opens the prompt preview of the values in the editor in a dialog. A link that still
 * names the former tab (`?tab=prompt-preview`) opens the dialog, on the Processing
 * Settings tab.
 */
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  tabs: [] as string[],
}));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('@monaco-editor/react', () => ({ default: () => null }));
vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({
    versions: [{ versionName: 'default', isActive: true }],
    loaded: true,
    loading: false,
    error: null,
    fetchVersions: vi.fn(),
    fetchVersion: vi.fn(),
    setActiveVersion: vi.fn(),
    deleteVersion: vi.fn(),
    saveAsNewVersion: vi.fn(),
    getVersionOptions: () => [],
  }),
}));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: true, canWrite: true, allowedConfigVersions: null, loading: false }),
}));
vi.mock('../../../hooks/use-configuration-library', () => ({ default: () => ({ listConfigurations: vi.fn(), getFile: vi.fn() }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../contexts/app', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/app')>()),
  default: () => ({ setSuccessMessage: vi.fn() }),
}));
vi.mock('../ConfigBuilder', () => ({
  default: ({ activeTabId }: { activeTabId: string }) => {
    state.tabs.push(activeTabId);
    return null;
  },
}));
vi.mock('../PromptPreview', () => ({
  default: ({ formValues }: { formValues: Record<string, unknown> }) => (
    <div data-testid="prompt-preview">{String(formValues.profile)}</div>
  ),
}));
vi.mock('../ConfigurationVersionsTable', () => ({ default: () => null }));
vi.mock('../ConfigRevisionHistoryPanel', () => ({ default: () => null }));
vi.mock('../ConfigurationComparison', () => ({ default: () => null }));
vi.mock('../../common/CreateConfigProfileModal', () => ({ default: () => null }));

import ConfigurationLayout from '../ConfigurationLayout';

const settle = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

const renderPage = async (hash = '#/documents/config') => {
  window.location.hash = hash;
  render(
    <MemoryRouter>
      <ConfigurationLayout />
    </MemoryRouter>,
  );
  await settle();
  await settle();
};

beforeEach(() => {
  state.tabs = [];
  state.graphql.mockReset().mockResolvedValue({
    data: {
      getConfigVersion: {
        success: true,
        Schema: JSON.stringify({ type: 'object', properties: {} }),
        Default: JSON.stringify({ profile: 'default-values' }),
        Custom: JSON.stringify({}),
      },
    },
  });
});

afterEach(() => {
  window.location.hash = '';
});

describe('Preview prompts', () => {
  it('is a button that opens the preview of the editor values in a dialog', async () => {
    await renderPage();
    expect(screen.queryByTestId('prompt-preview')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Preview prompts' }));
    expect(screen.getByTestId('prompt-preview').textContent).toBe('default-values');
  });

  it('opens from a link to the former Prompt Preview tab, on Processing Settings', async () => {
    await renderPage('#/documents/config?tab=prompt-preview');
    expect(screen.getByTestId('prompt-preview')).toBeTruthy();
    expect(state.tabs.at(-1)).toBe('configuration');
  });
});
