// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The reprocess dialog's "Sync To BDA" reminder follows the selected configuration
 * profile's `use_bda` flag. Every stack deploys the unified pattern, so the stack's
 * `IDPPattern` setting names no processing mode and must not decide it.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

const state = vi.hoisted(() => ({
  configs: {} as Record<string, Record<string, unknown>>,
  requested: [] as (string | null)[],
  settings: {} as Record<string, unknown>,
}));

vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({
    versions: [{ versionName: 'bda-profile', isActive: true }, { versionName: 'pipeline-profile' }],
    getVersionOptions: () => [
      { label: 'bda-profile (Active)', value: 'bda-profile' },
      { label: 'pipeline-profile', value: 'pipeline-profile' },
    ],
  }),
}));

vi.mock('../../../hooks/use-configuration', () => ({
  default: (versionName: string | null) => {
    state.requested.push(versionName);
    return { mergedConfig: versionName ? (state.configs[versionName] ?? null) : null };
  },
}));

vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: state.settings }) }));
vi.mock('../ConfigRevisionSelector', () => ({ default: () => null }));

import ReprocessDocumentModal from '../ReprocessDocumentModal';

const REMINDER = /Sync To BDA/;

const renderFor = (configVersion: string) =>
  render(
    <ReprocessDocumentModal
      visible
      onDismiss={vi.fn()}
      onConfirm={vi.fn()}
      selectedItems={[{ objectKey: 'doc.pdf', configVersion } as unknown as { objectKey: string }]}
    />,
  );

beforeEach(() => {
  state.configs = { 'bda-profile': { use_bda: true }, 'pipeline-profile': { use_bda: false } };
  state.requested = [];
  state.settings = { IDPPattern: 'Unified' };
});

describe('ReprocessDocumentModal — Sync To BDA reminder', () => {
  it('shows the reminder when the selected profile runs in BDA mode', () => {
    renderFor('bda-profile');

    expect(state.requested).toContain('bda-profile');
    expect(screen.getByText(REMINDER)).toBeInTheDocument();
  });

  it('does not show it for a pipeline-mode profile', () => {
    renderFor('pipeline-profile');

    expect(state.requested).toContain('pipeline-profile');
    expect(screen.queryByText(REMINDER)).not.toBeInTheDocument();
  });

  it('ignores the stack setting, which names no processing mode', () => {
    state.settings = { IDPPattern: 'Pattern1 - Packet processing with Bedrock Data Automation (BDA)' };
    renderFor('pipeline-profile');

    expect(screen.queryByText(REMINDER)).not.toBeInTheDocument();
  });
});
