// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** Bulk Skip All Reviews dialog: dry run first, then an inline run or a job. */

import React from 'react';
import { act, waitFor } from '@testing-library/react';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { api } = vi.hoisted(() => ({ api: { dryRunBulkSkip: vi.fn(), runBulkSkip: vi.fn() } }));
vi.mock('../bulk-review-api', () => api);
vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({ getVersionOptions: () => [{ label: 'claims', value: 'claims' }], loading: false }),
}));

import BulkSkipModal from '../BulkSkipModal';

const result = (over = {}) => ({
  dryRun: true,
  runMode: 'SYNC',
  syncLimit: 50,
  approximate: false,
  eligibleCount: 2,
  limitReached: false,
  counts: [
    { outcome: 'SKIPPED', count: 2 },
    { outcome: 'NOT_PENDING', count: 1 },
  ],
  results: [],
  jobId: null,
  ...over,
});

const modal = () => createWrapper(document.body).findModal()!;
const confirmButton = () =>
  modal()
    .findFooter()!
    .findAllButtons()
    .find((b) => (b.getElement().textContent ?? '').startsWith('Skip All Reviews'))!;
const chooseReason = () => {
  const select = modal().findContent().findAllSelects().at(-1)!;
  act(() => select.openDropdown());
  act(() => select.selectOptionByValue('Duplicate document'));
};

beforeEach(() => {
  document.body.innerHTML = '';
  api.dryRunBulkSkip.mockReset();
  api.runBulkSkip.mockReset();
});

describe('BulkSkipModal', () => {
  it('dry-runs the selection and runs it inline', async () => {
    api.dryRunBulkSkip.mockResolvedValue(result());
    api.runBulkSkip.mockResolvedValue(result({ dryRun: false, jobId: 'brj-1' }));
    const onFinished = vi.fn();
    renderWithIntl(
      <BulkSkipModal mode="SELECTED" objectKeys={['a', 'b', 'c']} onDismiss={vi.fn()} onFinished={onFinished} onJobStarted={vi.fn()} />,
    );
    await waitFor(() => expect(api.dryRunBulkSkip).toHaveBeenCalledWith({ mode: 'SELECTED', objectKeys: ['a', 'b', 'c'] }));
    await waitFor(() => expect(modal().findContent().getElement().textContent).toContain('Will be skipped: 2'));
    chooseReason();
    act(() => confirmButton().click());
    await waitFor(() => expect(onFinished).toHaveBeenCalled());
    expect(api.runBulkSkip.mock.calls[0][1]).toEqual({ reason: 'Duplicate document', note: undefined, resubmit: true });
  });

  it('cannot confirm when nothing is eligible', async () => {
    api.dryRunBulkSkip.mockResolvedValue(result({ eligibleCount: 0, counts: [{ outcome: 'NOT_PENDING', count: 3 }] }));
    renderWithIntl(<BulkSkipModal mode="SELECTED" objectKeys={['a']} onDismiss={vi.fn()} onFinished={vi.fn()} onJobStarted={vi.fn()} />);
    await waitFor(() => expect(modal().findContent().getElement().textContent).toContain('None of the selected documents'));
    expect(confirmButton().isDisabled()).toBe(true);
  });

  it('starts a job above the limit', async () => {
    api.dryRunBulkSkip.mockResolvedValue(result({ runMode: 'ASYNC', eligibleCount: 500 }));
    api.runBulkSkip.mockResolvedValue(result({ dryRun: false, runMode: 'ASYNC', jobId: 'brj-9' }));
    const onJobStarted = vi.fn();
    renderWithIntl(
      <BulkSkipModal mode="SELECTED" objectKeys={['a']} onDismiss={vi.fn()} onFinished={vi.fn()} onJobStarted={onJobStarted} />,
    );
    await waitFor(() => expect(modal().findContent().getElement().textContent).toContain('background job'));
    chooseReason();
    act(() => confirmButton().click());
    await waitFor(() => expect(onJobStarted).toHaveBeenCalledWith('brj-9'));
  });

  it('asks for a profile before counting in profile mode', () => {
    renderWithIntl(<BulkSkipModal mode="PROFILE" onDismiss={vi.fn()} onFinished={vi.fn()} onJobStarted={vi.fn()} />);
    expect(api.dryRunBulkSkip).not.toHaveBeenCalled();
    expect(confirmButton().isDisabled()).toBe(true);
  });
});
