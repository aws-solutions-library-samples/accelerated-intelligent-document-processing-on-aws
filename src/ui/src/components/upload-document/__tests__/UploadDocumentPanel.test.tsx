// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Component tests for the Upload panel's pre-flight notice.
 *
 * `previewAssignment` is mocked at the hook boundary so each test can hand the
 * panel one exact API answer. What is pinned is that every answer which disables
 * the Upload button also says why. A refusal under strict tenant isolation
 * matches no prefix mapping, so it arrives with `mappingPrefix: null`; the notice
 * used to return early on that and left a disabled button with no message.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import type { ConfigAssignmentPreview } from '../../../hooks/use-config-prefix-mappings';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));

vi.mock('../../../contexts/settings', () => ({
  default: () => ({ settings: { InputBucket: 'input-bucket' } }),
}));

const versions = [{ versionName: 'default', isActive: true }];
const versionOptions = [{ label: 'default (Active)', value: 'default' }];
vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({ versions, getVersionOptions: () => versionOptions, saveAsNewVersion: vi.fn() }),
}));

vi.mock('../../../hooks/use-sample-documents', () => ({
  default: () => ({ listSamples: vi.fn(async () => []), uploadSample: vi.fn() }),
}));

vi.mock('../../../hooks/use-configuration-library', () => ({
  default: () => ({ getFile: vi.fn() }),
}));

vi.mock('../../common/ConfigRevisionSelector', () => ({
  default: () => null,
}));

const preview = vi.hoisted(() => ({ answer: null as ConfigAssignmentPreview | null }));
vi.mock('../../../hooks/use-config-prefix-mappings', () => ({
  default: () => ({ previewAssignment: vi.fn(async () => preview.answer) }),
}));

import UploadDocumentPanel from '../UploadDocumentPanel';

const STRICT_REASON =
  'Refused under strict tenant isolation: this destination and the profile it would be processed under do not belong to the same tenant.';

describe('UploadDocumentPanel pre-flight notice', () => {
  beforeEach(() => {
    preview.answer = null;
  });

  it('explains a strict-isolation refusal that matched no mapping', async () => {
    // The live API's answer for an upload outside every tenant's prefixes.
    preview.answer = {
      objectKey: 'inbox/2026/__probe__',
      outOfScope: false,
      configProfile: 'default',
      configRevision: null,
      source: 'rejected',
      mappingPrefix: null,
      conflict: false,
      rejected: true,
      reason: STRICT_REASON,
    };
    render(<UploadDocumentPanel />);

    expect(await screen.findByText('This upload would be refused')).toBeTruthy();
    // The reason and the advice render as sibling text nodes, so read the page
    // text. waitFor, because the preview re-runs once the default profile is
    // selected after mount, clearing the first answer for one debounce window.
    await waitFor(() => expect(document.body.textContent).toContain(STRICT_REASON));
    // No mapping, so no profile to tell the user to choose.
    expect(document.body.textContent).not.toContain('Choose that profile');
  });

  it('keeps the profile advice for a reject-mode mapping', async () => {
    preview.answer = {
      objectKey: 'locked/__probe__',
      outOfScope: false,
      configProfile: 'lending',
      source: 'prefix-mapping',
      mappingPrefix: 'locked/',
      conflict: true,
      rejected: true,
      reason: "Prefix mapping 'locked/' requires profile 'lending'.",
    };
    render(<UploadDocumentPanel />);

    expect(await screen.findByText('This upload would be refused')).toBeTruthy();
    await waitFor(() =>
      expect(document.body.textContent).toContain(
        "Prefix mapping 'locked/' requires profile 'lending'. Choose that profile, or upload somewhere else.",
      ),
    );
  });

  it('shows nothing when no mapping matched and nothing was refused', async () => {
    preview.answer = {
      objectKey: '__probe__',
      outOfScope: false,
      configProfile: 'default',
      source: 'metadata',
      mappingPrefix: null,
      conflict: false,
      rejected: false,
      reason: 'No mapping matched.',
    };
    render(<UploadDocumentPanel />);

    await waitFor(() => expect(screen.getByText('Upload Documents')).toBeTruthy());
    // Give the 400 ms debounce time to deliver the answer before asserting absence.
    await new Promise((resolve) => setTimeout(resolve, 600));
    expect(screen.queryByText('No mapping matched.')).toBeNull();
    expect(screen.queryByText('This upload would be refused')).toBeNull();
  });
});
