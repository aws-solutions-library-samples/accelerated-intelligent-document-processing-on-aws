// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Policy Discovery form's policy group change, rendered (#608): what the panel
// sends to uploadDiscoveryDocument, when Start Discovery is disabled, and that the
// choice resets after an upload. The helpers are tested in policyGroupChange.test.ts;
// this pins the panel's use of them.
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('../../../graphql/generated', () => ({
  uploadDiscoveryDocument: 'uploadDiscoveryDocument',
  listDiscoveryJobs: 'listDiscoveryJobs',
  deleteDiscoveryJob: 'deleteDiscoveryJob',
  autoDetectSections: 'autoDetectSections',
  getConfigVersion: 'getConfigVersion',
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { DiscoveryBucket: 'discovery-bucket' } }) }));

const { versionsHook } = vi.hoisted(() => {
  const versions = [{ versionName: 'rules-profile', isActive: true }];
  const options = [{ value: 'rules-profile', label: 'rules-profile' }];
  const getVersionOptions = () => options;
  const fetchVersions = () => undefined;
  return { versionsHook: () => ({ versions, loading: false, getVersionOptions, fetchVersions }) };
});
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: versionsHook }));

import DiscoveryPanel from '../DiscoveryPanel';

const GROUP_CLASS = {
  'x-aws-idp-policy-type': 'NCCI_Manual',
  'x-aws-idp-policy-group': 'NCCI Manual',
  rule_properties: { visit_limit: { type: 'string', description: '?' }, modifier_25: { type: 'string', description: '?' } },
};

const uploads = () => graphql.mock.calls.filter(([a]) => a.query === 'uploadDiscoveryDocument').map(([a]) => a.variables);

beforeEach(() => {
  graphql.mockReset();
  graphql.mockImplementation(async ({ query }: { query: string }) => {
    if (query === 'getConfigVersion') {
      return { data: { getConfigVersion: { success: true, Custom: JSON.stringify({ policy_classes: [GROUP_CLASS] }) } } };
    }
    if (query === 'uploadDiscoveryDocument') {
      return {
        data: {
          uploadDiscoveryDocument: {
            presignedUrl: JSON.stringify({ url: 'https://upload.example/', fields: { key: 'k' } }),
            objectKey: 'document/rider.pdf',
            usePostMethod: 'true',
          },
        },
      };
    }
    if (query === 'listDiscoveryJobs') return { data: { listDiscoveryJobs: { DiscoveryJobs: [] } } };
    return { data: {} };
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 204, text: async () => '' })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const renderPanel = () => {
  const view = render(
    <IntlProvider locale="en" defaultLocale="en">
      <MemoryRouter>
        <DiscoveryPanel discoveryType="rules" />
      </MemoryRouter>
    </IntlProvider>,
  );
  return { ...view, wrapper: createWrapper(view.container) };
};

const changeInput = (container: HTMLElement, value: string) =>
  container.querySelector(
    `[aria-label="How this upload changes the group"] input[type="radio"][value="${value}"]`,
  ) as HTMLInputElement | null;

const startButton = () => screen.getByRole('button', { name: /Start Discovery/ }) as HTMLButtonElement;

const typeGroupAndChooseFile = async (wrapper: ReturnType<typeof createWrapper>, container: HTMLElement) => {
  fireEvent.change(screen.getByLabelText('Policy group (optional)'), { target: { value: 'NCCI Manual' } });
  await screen.findByText('Supersede chosen rules');
  const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(fileInput, { target: { files: [new File(['%PDF-1.4'], 'rider.pdf', { type: 'application/pdf' })] } });
  return wrapper;
};

describe('DiscoveryPanel policy group change', () => {
  it('sends a supersede with the chosen rules, and only once a rule is chosen', async () => {
    const { wrapper, container } = renderPanel();
    await typeGroupAndChooseFile(wrapper, container);
    fireEvent.click(changeInput(container, 'supersede')!);
    await waitFor(() => expect(startButton().disabled).toBe(true));

    const multiselect = wrapper.findMultiselect()!;
    multiselect.openDropdown();
    multiselect.selectOptionByValue('visit_limit');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    await act(async () => {
      fireEvent.click(startButton());
    });
    await waitFor(() => expect(uploads()).toHaveLength(1));
    expect(uploads()[0]).toMatchObject({
      discoveryType: 'rules',
      version: 'rules-profile',
      policyGroup: 'NCCI Manual',
      policyGroupMode: 'supersede',
      supersededRules: ['visit_limit'],
    });
  });

  it('sends a replace without rules, and returns to Add after the upload', async () => {
    const { wrapper, container } = renderPanel();
    await typeGroupAndChooseFile(wrapper, container);
    fireEvent.click(changeInput(container, 'replace')!);
    await waitFor(() => expect(startButton().disabled).toBe(false));
    await act(async () => {
      fireEvent.click(startButton());
    });
    await waitFor(() => expect(uploads()).toHaveLength(1));
    expect(uploads()[0]).toMatchObject({ policyGroup: 'NCCI Manual', policyGroupMode: 'replace' });
    expect(uploads()[0]).not.toHaveProperty('supersededRules');
    await waitFor(() => expect(changeInput(container, 'append')!.checked).toBe(true));
    expect(changeInput(container, 'replace')!.checked).toBe(false);
  });

  it('sends a plain append, with no change fields, by default', async () => {
    const { wrapper, container } = renderPanel();
    await typeGroupAndChooseFile(wrapper, container);
    await act(async () => {
      fireEvent.click(startButton());
    });
    await waitFor(() => expect(uploads()).toHaveLength(1));
    expect(uploads()[0]).toMatchObject({ policyGroup: 'NCCI Manual' });
    expect(uploads()[0]).not.toHaveProperty('policyGroupMode');
    expect(uploads()[0]).not.toHaveProperty('supersededRules');
  });

  it('offers no change for an ungrouped upload', async () => {
    const { container } = renderPanel();
    await waitFor(() => expect(screen.getByLabelText('Policy group (optional)')).toBeTruthy());
    expect(changeInput(container, 'supersede')).toBeNull();
    expect(screen.queryByText('Supersede chosen rules')).toBeNull();
    // Nor any notice about the group: with no group named there is none to describe.
    expect(screen.queryByText(/has no policy group with this name yet/)).toBeNull();
  });

  it('describes no group change for a group name the server would refuse', async () => {
    renderPanel();
    fireEvent.change(screen.getByLabelText('Policy group (optional)'), { target: { value: 'manual/ch1' } });
    await screen.findByText(/must start with a letter or digit/);
    expect(screen.queryByText(/has no policy group with this name yet/)).toBeNull();
    expect(screen.queryByText('Supersede chosen rules')).toBeNull();
  });
});
