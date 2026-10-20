// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Production Queues table in HITL › Queue Management. What has to hold: each
 * queue shows whether it takes documents (Status) and its confidence threshold
 * (Threshold, two decimals like the review reasons); every queue but Default can be
 * deleted, even one that holds items, and the confirmation says those items move to
 * the Default queue; a refusal while an item is being completed is shown in the
 * dialog.
 */

import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const listReviewQueues = vi.fn();
const deleteReviewQueue = vi.fn();
const fetchQueuesOverview = vi.fn();
vi.mock('../reviewer-assignment', () => ({
  listReviewQueues: () => listReviewQueues(),
  deleteReviewQueue: (id: string) => deleteReviewQueue(id),
  listAnnotatorEmails: async () => [],
  listTestSetQueues: async () => [],
  createReviewQueue: vi.fn(),
  updateReviewQueue: vi.fn(),
  updateTestSetQueue: vi.fn(),
}));
vi.mock('../review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../review-api')>()),
  fetchQueuesOverview: () => fetchQueuesOverview(),
}));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
const setSuccessMessage = vi.fn();
vi.mock('../../../contexts/app', () => ({ default: () => ({ setSuccessMessage, setErrorMessage: vi.fn() }) }));

import QueuesPage, { deleteConfirmText, deleteDisabledReason, formatThreshold } from '../QueuesPage';

const def = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  name: id[0].toUpperCase() + id.slice(1),
  description: '',
  priority: 0,
  checklist: [],
  rejectReasons: [],
  timeLimitMinutes: 60,
  eligibleAnnotators: [],
  isDefaultQueue: id === 'default',
  confidenceThreshold: 0.8,
  enabled: true,
  ...over,
});

const health = (name: string, depth: number) => ({
  reviewers: [],
  summary: { name, displayName: name, depth, unclaimed: depth, claimed: 0, inProgress: 0 },
});

const renderPage = async () => {
  render(
    <MemoryRouter>
      <QueuesPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(table().findRows().length).toBeGreaterThan(0));
};

const table = () => createWrapper(document.body).findTable()!;
const headers = () =>
  table()
    .findColumnHeaders()
    .map((h) => h.getElement().textContent);
const cell = (queue: string, header: string): string => {
  const col = headers().indexOf(header);
  const row = table()
    .findRows()
    .findIndex((r) => (r.getElement().textContent ?? '').includes(queue));
  return (
    table()
      .findBodyCell(row + 1, col + 1)!
      .getElement().textContent ?? ''
  );
};
const deleteModal = () => {
  const m = createWrapper(document.body)
    .findAllModals()
    .find((w) => w.findHeader().getElement().textContent === 'Delete queue');
  if (!m) throw new Error('no delete dialog');
  return m;
};
/** Whether a menu item is disabled: Cloudscape marks the item's interactive element. */
const itemDisabled = (item: { getElement: () => HTMLElement }) =>
  item.getElement().getAttribute('aria-disabled') === 'true' || !!item.getElement().querySelector('[aria-disabled="true"]');
const openMenu = (queue: string) => {
  const dropdown = createWrapper(document.body)
    .findAllButtonDropdowns()
    .find((d) => d.findNativeButton().getElement().getAttribute('aria-label') === `Actions for ${queue}`);
  if (!dropdown) throw new Error(`no actions menu for ${queue}`);
  dropdown.openDropdown();
  return dropdown;
};

beforeEach(() => {
  document.body.innerHTML = '';
  listReviewQueues
    .mockReset()
    .mockResolvedValue([
      def('default', { enabled: false, confidenceThreshold: 0.9 }),
      def('claims', { confidenceThreshold: 0.65, priority: 5 }),
    ]);
  fetchQueuesOverview.mockReset().mockResolvedValue({ queues: [health('default', 0), health('claims', 3)] });
  deleteReviewQueue.mockReset().mockResolvedValue(true);
  setSuccessMessage.mockReset();
});

describe('the Production Queues table', () => {
  it('shows each queue’s Status and Threshold', async () => {
    await renderPage();
    expect(headers()).toEqual(expect.arrayContaining(['Status', 'Threshold']));
    expect(cell('Default', 'Status')).toBe('Disabled');
    expect(cell('Claims', 'Status')).toBe('Enabled');
    expect(cell('Default', 'Threshold')).toBe('0.90');
    expect(cell('Claims', 'Threshold')).toBe('0.65');
  });

  it('lets a queue holding items be deleted, saying they move to Default', async () => {
    await renderPage();
    const menu = openMenu('Claims');
    const del = menu.findItemById('delete')!;
    expect(itemDisabled(del)).toBe(false);
    menu.findItemById('delete')!.click();
    const modal = deleteModal();
    expect(modal.findContent().getElement().textContent).toContain('3 items will move to the Default queue.');
    fireEvent.click(modal.findFooter()!.findAllButtons().at(-1)!.getElement());
    await waitFor(() => expect(deleteReviewQueue).toHaveBeenCalledWith('claims'));
    await waitFor(() => expect(setSuccessMessage).toHaveBeenCalledWith(expect.stringContaining('moved to the Default queue')));
  });

  it('keeps the Default queue from being deleted', async () => {
    await renderPage();
    const del = openMenu('Default').findItemById('delete')!;
    expect(itemDisabled(del)).toBe(true);
  });

  it('shows a refusal while an item is being completed', async () => {
    deleteReviewQueue.mockRejectedValue({ errors: [{ message: 'QUEUE_FINALIZING: an item is being finalized' }] });
    await renderPage();
    openMenu('Claims').findItemById('delete')!.click();
    const modal = deleteModal();
    fireEvent.click(modal.findFooter()!.findAllButtons().at(-1)!.getElement());
    await waitFor(() =>
      expect(modal.findContent().getElement().textContent).toContain('An item in this queue is being completed. Try again in a minute.'),
    );
  });
});

describe('the table helpers', () => {
  it('formats a threshold with two decimals, or a dash when none is stored', () => {
    expect(formatThreshold(0.8)).toBe('0.80');
    expect(formatThreshold(1)).toBe('1.00');
    expect(formatThreshold(null)).toBe('—');
  });

  it('refuses deleting only Default and undefined queues', () => {
    expect(deleteDisabledReason(def('default'))).toBe("The default queue can't be deleted");
    expect(deleteDisabledReason(def('escalation'))).toBeUndefined();
    expect(deleteDisabledReason(null)).toBe('This queue is not defined');
  });

  it('names the items that will move only when there are some', () => {
    expect(deleteConfirmText({ id: 'claims', name: 'Claims' }, 0)).not.toContain('Default queue');
    expect(deleteConfirmText({ id: 'claims', name: 'Claims' }, 1)).toContain('1 item will move to the Default queue.');
  });
});
