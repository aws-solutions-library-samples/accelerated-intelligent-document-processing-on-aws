// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which Configuration Profiles the document page reads for a caller whose
 * `allowedConfigVersions` scope excludes `default`.
 *
 * The page reads the profile the document was processed with, and `default` for
 * a document that names none. The child panels and the list, role, pricing and
 * polling hooks are mocked; `useConfiguration` runs for real over a mocked
 * transport, so the assertions are on the profiles the page asks the API for and
 * on the configuration it hands the Sections panel.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, render } from '@testing-library/react';

interface ListedVersion {
  versionName: string;
  isActive?: boolean;
}

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  versions: [] as ListedVersion[],
  role: { isReviewer: false, allowedConfigVersions: null as string[] | null, loading: false },
  refused: new Set<string>(),
  sectionsConfig: [] as unknown[],
}));

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: state.graphql }),
}));

vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({ versions: state.versions, loaded: true, loading: false, error: null }),
}));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => ({ pricing: null, nonChargeableUnits: [], loading: false }) }));
vi.mock('../../../hooks/use-polling', () => ({ default: () => undefined }));
vi.mock('../../../hooks/use-classification-comparison', () => ({ default: () => null }));
vi.mock('../../../contexts/app', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/app')>()),
  default: () => ({ currentCredentials: null }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));

vi.mock('../../document-viewers/DocumentViewers', () => ({ default: () => null }));
vi.mock('../../sections-panel', () => ({
  default: (props: { mergedConfig?: unknown }) => {
    state.sectionsConfig.push(props.mergedConfig);
    return null;
  },
}));
vi.mock('../../pages-panel', () => ({ default: () => null }));
vi.mock('../../chat-panel', () => ({ default: () => null }));
vi.mock('../../step-function-flow/StepFunctionFlowViewer', () => ({ default: () => null }));
vi.mock('../TroubleshootModal', () => ({ default: () => null }));
vi.mock('../DocumentVersionsPanel', () => ({ default: () => null }));
vi.mock('../DocumentDownloadModals', () => ({ DownloadOptionsModal: () => null, DownloadProgressModal: () => null }));

import DocumentPanel from '../DocumentPanel';

const profileBody = (name: string) => ({
  success: true,
  Schema: JSON.stringify({ type: 'object', properties: {} }),
  Default: JSON.stringify({ profile: name }),
  Custom: JSON.stringify({}),
});

const requestedProfiles = () => state.graphql.mock.calls.map(([args]) => args?.variables?.versionName).filter((name) => name !== undefined);

const settle = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

const documentWith = (configVersion?: string) => ({
  objectKey: 'invoices/a.pdf',
  objectStatus: 'COMPLETED',
  configVersion,
  sections: [],
  pages: [],
});

const scopedTo = (scope: string[] | null) => {
  state.role = { isReviewer: false, allowedConfigVersions: scope, loading: false };
  state.refused = new Set(scope ? ['default'] : []);
};

beforeEach(() => {
  state.graphql.mockReset();
  state.graphql.mockImplementation(({ variables }: { variables?: { versionName?: string } }) => {
    const name = variables?.versionName;
    if (name === undefined) return new Promise(() => {});
    if (state.refused.has(name)) {
      return Promise.resolve({
        data: {
          getConfigVersion: {
            success: false,
            error: { type: 'Unauthorized', message: `Access denied: version '${name}' is not in your allowed scope` },
          },
        },
      });
    }
    return Promise.resolve({ data: { getConfigVersion: profileBody(name) } });
  });
  state.sectionsConfig = [];
  state.versions = [];
  scopedTo(null);
});

describe('DocumentPanel — a scoped caller', () => {
  it('reads only the profile the document was processed with, never default', async () => {
    scopedTo(['tenant-a_*']);
    state.versions = [{ versionName: 'tenant-a_prod' }];
    render(<DocumentPanel item={documentWith('tenant-a_prod')} />);
    await settle();

    expect(requestedProfiles()).toEqual(['tenant-a_prod']);
    expect(state.sectionsConfig.at(-1)).toEqual({ profile: 'tenant-a_prod' });
  });
});

describe('DocumentPanel — an unscoped caller', () => {
  it('reads default once for a document that names no profile, and uses it', async () => {
    state.versions = [{ versionName: 'default' }, { versionName: 'lending', isActive: true }];
    render(<DocumentPanel item={documentWith(undefined)} />);
    await settle();

    expect(requestedProfiles()).toEqual(['default']);
    expect(state.sectionsConfig.at(-1)).toEqual({ profile: 'default' });
  });

  it('reads default alongside the profile the document names', async () => {
    state.versions = [{ versionName: 'default' }, { versionName: 'lending', isActive: true }];
    render(<DocumentPanel item={documentWith('lending')} />);
    await settle();

    expect(requestedProfiles()).toEqual(['default', 'lending']);
    expect(state.sectionsConfig.at(-1)).toEqual({ profile: 'lending' });
  });
});
