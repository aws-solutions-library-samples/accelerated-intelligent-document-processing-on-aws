// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The reprocess dialog's "Sync To BDA" reminder follows the selected configuration
 * profile's `use_bda` flag, and only once that profile's configuration has loaded.
 *
 * `useConfiguration` is replaced by a stand-in with the real hook's timing: a new
 * profile loads asynchronously, the previous profile's configuration stays in place
 * until the answer arrives, and a failed read sets `error` and leaves it in place.
 * Those are the states in which the dialog could otherwise answer for the wrong
 * profile.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, render, screen } from '@testing-library/react';

const state = vi.hoisted(() => ({
  configs: {} as Record<string, Record<string, unknown>>,
}));

vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({
    versions: [{ versionName: 'bda-profile', isActive: true }, { versionName: 'pipeline-profile' }, { versionName: 'unreadable' }],
    getVersionOptions: () => [
      { label: 'bda-profile (Active)', value: 'bda-profile' },
      { label: 'pipeline-profile', value: 'pipeline-profile' },
      { label: 'unreadable', value: 'unreadable' },
    ],
  }),
}));

vi.mock('../../../hooks/use-configuration', async () => {
  const { useEffect, useState } = await import('react');
  return {
    default: (versionName: string | null) => {
      const [hook, setHook] = useState<{ mergedConfig: Record<string, unknown> | null; loading: boolean; error: string | null }>({
        mergedConfig: null,
        loading: true,
        error: null,
      });
      useEffect(() => {
        if (versionName === null) return undefined;
        setHook((prev) => ({ ...prev, loading: true, error: null }));
        const timer = setTimeout(() => {
          const config = state.configs[versionName];
          setHook((prev) =>
            config ? { mergedConfig: config, loading: false, error: null } : { ...prev, loading: false, error: 'read failed' },
          );
        }, 10);
        return () => clearTimeout(timer);
      }, [versionName]);
      return hook;
    },
  };
});

vi.mock('../ConfigRevisionSelector', () => ({ default: () => null }));

import ReprocessDocumentModal from '../ReprocessDocumentModal';

const REMINDER = /Sync To BDA/;

const dialog = (visible: boolean, configVersion: string) => (
  <ReprocessDocumentModal
    visible={visible}
    onDismiss={vi.fn()}
    onConfirm={vi.fn()}
    selectedItems={[{ objectKey: 'doc.pdf', configVersion } as unknown as { objectKey: string }]}
  />
);

const settle = (ms = 30) =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });

beforeEach(() => {
  state.configs = { 'bda-profile': { use_bda: true }, 'pipeline-profile': { use_bda: false } };
});

describe('ReprocessDocumentModal — Sync To BDA reminder', () => {
  it('shows the reminder once a BDA-mode profile has loaded', async () => {
    render(dialog(true, 'bda-profile'));
    expect(screen.queryByText(REMINDER)).not.toBeInTheDocument();

    await settle();
    expect(screen.getByText(REMINDER)).toBeInTheDocument();
  });

  it('does not show it for a pipeline-mode profile', async () => {
    render(dialog(true, 'pipeline-profile'));
    await settle();

    expect(screen.queryByText(REMINDER)).not.toBeInTheDocument();
  });

  it("does not show the previous profile's answer while the next profile loads", async () => {
    const { rerender } = render(dialog(true, 'bda-profile'));
    await settle();
    expect(screen.getByText(REMINDER)).toBeInTheDocument();

    rerender(dialog(false, 'bda-profile'));
    await settle(0);
    rerender(dialog(true, 'pipeline-profile'));
    await settle(0);
    expect(screen.queryByText(REMINDER)).not.toBeInTheDocument();

    await settle();
    expect(screen.queryByText(REMINDER)).not.toBeInTheDocument();
  });

  it("does not show the previous profile's answer when the next profile cannot be read", async () => {
    const { rerender } = render(dialog(true, 'bda-profile'));
    await settle();
    expect(screen.getByText(REMINDER)).toBeInTheDocument();

    rerender(dialog(false, 'bda-profile'));
    await settle(0);
    rerender(dialog(true, 'unreadable'));
    await settle();

    expect(screen.queryByText(REMINDER)).not.toBeInTheDocument();
  });
});
