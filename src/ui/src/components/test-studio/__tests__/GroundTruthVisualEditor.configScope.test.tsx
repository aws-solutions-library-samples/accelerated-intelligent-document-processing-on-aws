// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which Configuration Profile the ground-truth editor reads its class list from,
 * for a caller whose `allowedConfigVersions` scope excludes `default`.
 *
 * With no profile stamped on the baseline or declared by the test set, the editor
 * falls back to the active profile and then to `default`. The profile list and the
 * role are mocked at the module boundary; `useConfiguration` runs for real over a
 * mocked transport, so the assertions are on the profiles the editor asks the API
 * for.
 */

import { act, render } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

interface ListedVersion {
  versionName: string;
  isActive?: boolean;
}

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  list: { versions: [] as ListedVersion[], loaded: false },
  role: { allowedConfigVersions: null as string[] | null, loading: false },
  refused: new Set<string>(),
}));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('../../../graphql/generated', () => ({
  getConfigVersion: 'getConfigVersion',
  updateConfiguration: 'updateConfiguration',
  getFilePresignedUrl: 'getFilePresignedUrl',
  uploadDocument: 'uploadDocument',
  reextractTestSetDocument: 'reextractTestSetDocument',
  getDraftLabelJob: 'getDraftLabelJob',
  updateTestSetDocumentSections: 'updateTestSetDocumentSections',
}));
vi.mock('../../../hooks/use-test-doc-pages', () => ({
  default: () => ({ pages: [{ Id: '1', ImageUri: 'blob:1' }], isLoading: false, error: null, previewUnavailable: false }),
}));
vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({ ...state.list, loading: !state.list.loaded, error: null }),
}));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ user: { username: 'tester' } }) }));

import GroundTruthVisualEditor from '../GroundTruthVisualEditor';

const BASELINE = { document_class: { type: 'Invoice' }, split_document: { page_indices: [0] }, inference_result: { total: '1' } };
const SECTIONS = [{ sectionId: '1', baselineKey: 'ts1/baseline/p.pdf/sections/1/result.json', documentClass: 'Invoice', pageIndices: [0] }];

const requestedProfiles = () =>
  state.graphql.mock.calls.filter(([args]) => args.query === 'getConfigVersion').map(([args]) => args.variables.versionName);

const settle = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

const scopedTo = (scope: string[] | null) => {
  state.role = { allowedConfigVersions: scope, loading: false };
  state.refused = new Set(scope ? ['default'] : []);
};

const editor = (configVersion?: string) => (
  <GroundTruthVisualEditor
    bucket="test-set-bucket"
    inputKey="ts1/input/p.pdf"
    objectKey="p.pdf"
    sections={SECTIONS}
    isReadOnly={false}
    testSetId="ts1"
    configVersion={configVersion}
  />
);

beforeEach(() => {
  global.fetch = vi.fn(() => Promise.resolve({ ok: true, text: () => Promise.resolve(JSON.stringify(BASELINE)) })) as never;
  state.graphql.mockReset();
  state.graphql.mockImplementation((args: { query: string; variables?: { versionName?: string } }) => {
    if (args.query === 'getFilePresignedUrl') {
      return Promise.resolve({ data: { getFilePresignedUrl: { presignedUrl: 'https://example.test/signed' } } });
    }
    if (args.query === 'getConfigVersion') {
      const name = args.variables?.versionName ?? '';
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
      return new Promise(() => {});
    }
    return Promise.resolve({ data: {} });
  });
  state.list = { versions: [], loaded: false };
  scopedTo(null);
});

describe('GroundTruthVisualEditor — a scoped caller', () => {
  it('asks for no profile while the in-scope list is loading', async () => {
    scopedTo(['tenant-a_*']);
    render(editor());
    await settle();

    expect(requestedProfiles()).toEqual([]);
  });

  it('does not fall back to a default its scope excludes when the active profile is out of scope', async () => {
    scopedTo(['tenant-a_*']);
    const { rerender } = render(editor());
    await settle();
    state.list = { versions: [{ versionName: 'tenant-a_dev' }, { versionName: 'tenant-a_prod' }], loaded: true };
    rerender(editor());
    await settle();

    expect(requestedProfiles()).toEqual([]);
  });

  it('still reads the profile the test set declares when it is in scope', async () => {
    scopedTo(['tenant-a_*']);
    render(editor('tenant-a_prod'));
    await settle();

    expect(requestedProfiles()).toEqual(['tenant-a_prod']);
  });
});

describe('GroundTruthVisualEditor — an unscoped caller', () => {
  it('reads default while the list loads, then the active profile once it answers', async () => {
    const { rerender } = render(editor());
    await settle();
    expect(requestedProfiles()).toEqual(['default']);

    state.list = { versions: [{ versionName: 'default' }, { versionName: 'lending', isActive: true }], loaded: true };
    rerender(editor());
    await settle();
    expect(requestedProfiles()).toEqual(['default', 'lending']);
  });
});
