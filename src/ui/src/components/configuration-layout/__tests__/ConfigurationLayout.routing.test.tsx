// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What the configuration page hands the editor for the Routing Rules tab: the profile's
 * `routing` section out of the form, who may change it, the classes its conditions can
 * name, the form's values its warnings are read from, and a change put back into the
 * form. The Policy Schema and Routing Rules tabs are shown whatever the deployment's
 * pattern setting says.
 */
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

type BuilderProps = Record<string, unknown> & { onRoutingChange: (routing: unknown) => void; formValues: Record<string, unknown> };

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  isAdmin: true,
  settings: {} as Record<string, unknown>,
  builder: [] as Record<string, unknown>[],
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
  default: () => ({ isAdmin: state.isAdmin, canWrite: true, allowedConfigVersions: null, loading: false }),
}));
vi.mock('../../../hooks/use-configuration-library', () => ({ default: () => ({ listConfigurations: vi.fn(), getFile: vi.fn() }) }));
vi.mock('../../../contexts/settings', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/settings')>()),
  default: () => ({ settings: state.settings }),
}));
vi.mock('../../../contexts/app', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/app')>()),
  default: () => ({ setSuccessMessage: vi.fn() }),
}));
vi.mock('../ConfigBuilder', () => ({
  default: (props: Record<string, unknown>) => {
    state.builder.push(props);
    return null;
  },
}));
vi.mock('../PromptPreview', () => ({ default: () => null }));
vi.mock('../ConfigurationVersionsTable', () => ({ default: () => null }));
vi.mock('../ConfigRevisionHistoryPanel', () => ({ default: () => null }));
vi.mock('../ConfigurationComparison', () => ({ default: () => null }));
vi.mock('../../common/CreateConfigProfileModal', () => ({ default: () => null }));

import ConfigurationLayout from '../ConfigurationLayout';

const ROUTING = {
  tables: [
    {
      id: 'review-routing',
      rules: [
        {
          id: 'claims-first',
          label: 'Claims',
          enabled: true,
          conditions: [],
          actions: [{ type: 'review_queue', queue: 'claims' }],
          reason: 'Claims are reviewed.',
        },
      ],
      default_outcome: { actions: [], reason: '' },
    },
  ],
  max_consecutive_placements: 3,
};
const CLASSES = [{ 'x-aws-idp-document-type': 'Invoice', properties: { Total: { type: 'number' } } }];
const POLICY_CLASSES = [{ 'x-aws-idp-policy-type': 'prior_auth', rule_properties: { coverage: {} } }];

const settle = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

const renderPage = async (): Promise<void> => {
  window.location.hash = '#/documents/config';
  renderWithIntl(
    <MemoryRouter>
      <ConfigurationLayout />
    </MemoryRouter>,
  );
  await settle();
  await settle();
};
const builder = (): BuilderProps => state.builder.at(-1) as BuilderProps;

beforeEach(() => {
  state.builder = [];
  state.isAdmin = true;
  state.settings = {};
  state.graphql.mockReset().mockResolvedValue({
    data: {
      getConfigVersion: {
        success: true,
        Schema: JSON.stringify({ type: 'object', properties: {} }),
        Default: JSON.stringify({
          routing: ROUTING,
          classes: CLASSES,
          policy_classes: POLICY_CLASSES,
          hitl: { enabled: true },
          rule_validation: { enabled: false },
        }),
        Custom: JSON.stringify({}),
      },
    },
  });
});

afterEach(() => {
  window.location.hash = '';
});

describe('the Routing Rules tab on the configuration page', () => {
  it('is given the routing section of the profile and the classes its conditions can name', async () => {
    await renderPage();

    expect(builder().showRoutingTab).toBe(true);
    expect(builder().routing).toEqual(ROUTING);
    expect(builder().classes).toEqual(CLASSES);
    expect(builder().policyClasses).toEqual(POLICY_CLASSES);
    expect(builder().routingServerError).toBeNull();
  });

  it('is given, in the form values, what its warnings are read from', async () => {
    await renderPage();

    // Review Routing (hitl.enabled), and whether policy rule validation runs.
    expect(builder().formValues.hitl).toEqual({ enabled: true });
    expect(builder().formValues.rule_validation).toEqual({ enabled: false });
    expect(builder().formValues.policy_classes).toEqual(POLICY_CLASSES);
  });

  it('can be changed by an Admin', async () => {
    await renderPage();

    expect(builder().routingReadOnly).toBe(false);
  });

  it('is read-only for another role', async () => {
    state.isAdmin = false;
    await renderPage();

    expect(builder().routingReadOnly).toBe(true);
    expect(builder().routing).toEqual(ROUTING);
  });

  it('puts a change into the form and leaves the rest of it alone', async () => {
    await renderPage();
    const changed = { ...ROUTING, max_consecutive_placements: 5 };

    act(() => builder().onRoutingChange(changed));

    expect(builder().routing).toEqual(changed);
    expect(builder().formValues.routing).toEqual(changed);
    expect(builder().formValues.classes).toEqual(CLASSES);
  });

  it('is shown, with Policy Schema, whatever the pattern setting of the deployment says', async () => {
    state.settings = { IDPPattern: 'Pattern1 - Packet or Media processing with Bedrock Data Automation (BDA)' };
    await renderPage();

    expect(builder().showRuleSchema).toBe(true);
    expect(builder().showRoutingTab).toBe(true);
  });
});
