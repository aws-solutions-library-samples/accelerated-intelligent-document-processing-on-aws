// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A production queue's page, for an Admin. What has to hold: Queue Progress is a bar of
 * the documents assigned or in progress, of all the queue's documents (a finished item
 * leaves its queue); Key Metrics shows exactly Pending, Assigned, In Progress,
 * Annotators (how many are listed) and Priority; the Annotators table lists each listed
 * Annotator, and anyone still holding documents, with the documents assigned to each;
 * the Documents table shows Document, Queue, Reason, State and Annotator, then the row
 * Actions menu, and offers Source, Priority and Age in its settings, kept in the browser
 * under the page's own key; the page reports the queue's name for the breadcrumb; and the Settings
 * dialog stays open, keeping what was typed, through a refresh that briefly returns no
 * definition, without carrying one queue's definition to another queue's page; and the
 * routing rules that send documents to the queue are listed, with what to do when there
 * are none and an error when they cannot be read.
 */

import React from 'react';
import { act, fireEvent, waitFor } from '@testing-library/react';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const defaultQueue = {
  id: 'default',
  name: 'Default',
  description: '',
  priority: 50,
  checklist: [],
  rejectReasons: [],
  skipReasons: [],
  timeLimitMinutes: 60,
  eligibleAnnotators: ['a@example.com', 'b@example.com'],
  isDefaultQueue: true,
  enabled: true,
  version: 4,
};
const { listReviewQueues, getReviewQueueReferences, role } = vi.hoisted(() => ({
  listReviewQueues: vi.fn(),
  getReviewQueueReferences: vi.fn(),
  role: { isAdmin: true },
}));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setSuccessMessage: vi.fn(), setErrorMessage: vi.fn() }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => ({ isAdmin: role.isAdmin, isAnnotator: !role.isAdmin, loading: false }) }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../reviewer-assignment', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../reviewer-assignment')>()),
  listReviewQueues: () => listReviewQueues(),
  getReviewQueueReferences: (queueId: string) => getReviewQueueReferences(queueId),
  listAnnotatorEmails: async () => [],
}));
vi.mock('../review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../review-api')>()),
  fetchQueueItems: async () => ({
    items: [
      {
        objectKey: 'in/doc.pdf',
        cycleId: 'c1',
        profile: 'default',
        queue: 'default',
        priority: 50,
        state: 'OPEN',
        enqueuedAt: '2026-10-22T00:00:00.000Z',
        reasons: [],
        reasonCount: 0,
        isMine: false,
      },
    ],
    nextToken: null,
  }),
  fetchQueuesOverview: async () => ({
    queues: [
      {
        summary: { name: 'default', displayName: 'Default', depth: 5, unclaimed: 2, claimed: 3, inProgress: 1, annotators: 2 },
        reviewers: [
          { email: 'a@example.com', claimed: 2 },
          { email: 'gone@example.com', claimed: 1 },
        ],
      },
    ],
  }),
}));

// The bulk dialog has its own tests; here it only shows which mode the page opened it in.
vi.mock('../../review-bulk/BulkSkipModal', () => ({
  default: ({ mode }: { mode: string }) => <div data-testid="bulk-skip">{mode}</div>,
}));

import ReviewQueuePage from '../ReviewQueuePage';
import { queueAnnotatorRows } from '../QueueAnnotatorsTable';

let navigateTo: (path: string) => void = () => undefined;
const Navigator = () => {
  navigateTo = useNavigate();
  return null;
};

const renderPage = async (onName?: (name: string) => void) => {
  renderWithIntl(
    <MemoryRouter initialEntries={['/review-queues/queues/default']}>
      <Navigator />
      <Routes>
        <Route path="/review-queues/queues/:queue" element={<ReviewQueuePage onName={onName} />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(createWrapper(document.body).findAllTables()).toHaveLength(3));
  await waitFor(() => expect(itemsTable().findRows()).toHaveLength(1));
};

beforeEach(() => {
  listReviewQueues.mockReset().mockResolvedValue([defaultQueue]);
  getReviewQueueReferences.mockReset().mockResolvedValue([]);
  role.isAdmin = true;
  // The Documents table keeps its settings in the browser.
  localStorage.clear();
});

const tables = () => createWrapper(document.body).findAllTables();
// In page order: the Annotators, the routing rules that use the queue, the documents.
const annotatorsTable = () => tables()[0];
const referencesTable = () => tables()[1];
const itemsTable = () => tables()[2];
const headers = (t: ReturnType<typeof annotatorsTable>) => t.findColumnHeaders().map((h) => h.getElement().textContent);

describe('a production queue page', () => {
  it('shows the work underway in Queue Progress, before Key Metrics', async () => {
    await renderPage();
    const bar = createWrapper(document.body).findProgressBar()!;
    // claimed 3 (assigned or in progress) of unclaimed 2 + claimed 3.
    expect(bar.findAdditionalInfo()!.getElement().textContent).toBe('3 of 5 documents assigned or in progress');
    const titles = createWrapper(document.body)
      .findAllContainers()
      .map((c) => c.findHeader()?.getElement().textContent);
    expect(titles.indexOf('Queue Progress')).toBeLessThan(titles.indexOf('Key Metrics'));
  });

  it('shows Pending, Assigned, In Progress, Annotators and Priority in Key Metrics', async () => {
    await renderPage();
    const pairs = createWrapper(document.body).findKeyValuePairs()!;
    expect(pairs.findItems().map((i) => i.findLabel()?.getElement().textContent)).toEqual([
      'Pending',
      'Assigned',
      'In Progress',
      'Annotators',
      'Priority',
    ]);
    expect(pairs.findItems().map((i) => i.findValue()?.getElement().textContent)).toEqual(['2', '2', '1', '2', '50']);
  });

  it('lists each Annotator with the documents assigned to them', async () => {
    await renderPage();
    expect(headers(annotatorsTable())).toEqual(['Annotator', 'Assigned documents']);
    const rows = annotatorsTable()
      .findRows()
      .map((r) => r.getElement().textContent);
    expect(rows).toEqual(['a@example.com2', 'b@example.com0', 'gone@example.com (not listed)1']);
  });

  it('shows Document, Queue, Reason, State, Annotator and Actions in the Documents table', async () => {
    await renderPage();
    expect(itemsTable().findHeaderSlot()!.getElement().textContent).toContain('Documents');
    // After the selection column.
    expect(headers(itemsTable()).slice(1)).toEqual(['Document', 'Queue', 'Reason', 'State', 'Annotator', 'Actions']);
  });

  it('offers Source, Priority and Age in the settings of the Documents table, and keeps the choice under its own key', async () => {
    await renderPage();
    const preferences = itemsTable().findCollectionPreferences()!;
    act(() => preferences.findTriggerButton().click());
    const options = preferences.findModal()!.findContentDisplayPreference()!.findOptions();
    expect(options.map((o) => o.findLabel().getElement().textContent)).toEqual([
      'Document',
      'Source',
      'Queue',
      'Priority',
      'Age',
      'Reason',
      'State',
      'Annotator',
    ]);

    act(() => options[4].findVisibilityToggle().findNativeInput().click());
    act(() => preferences.findModal()!.findConfirmButton()!.click());

    expect(headers(itemsTable()).slice(1)).toEqual(['Document', 'Queue', 'Age', 'Reason', 'State', 'Annotator', 'Actions']);
    expect(JSON.parse(localStorage.getItem('review-queue-items-preferences')!).contentDisplay).toContainEqual({ id: 'age', visible: true });
  });
});

describe('the routing rules that use the queue', () => {
  const referencesSection = () =>
    createWrapper(document.body)
      .findAllContainers()
      .find((c) => (c.findHeader()?.getElement().textContent ?? '').includes('Routing rules that use this queue'))!;

  it('are read once for this queue and listed by profile, revision and rule', async () => {
    getReviewQueueReferences.mockResolvedValue([
      { profile: 'lending', revision: 3, published: true, ruleId: 'big', ruleLabel: 'Big loans' },
      { profile: 'lending', revision: 4, published: false, ruleId: 'big', ruleLabel: 'Big loans' },
      { profile: 'mortgage', revision: null, published: true, ruleId: 'plain', ruleLabel: null },
      { profile: 'claims', revision: 2, published: true, ruleId: '', ruleLabel: null },
    ]);
    await renderPage();
    await waitFor(() => expect(referencesTable().findRows()).toHaveLength(4));

    expect(getReviewQueueReferences.mock.calls).toEqual([['default']]);
    expect(headers(referencesTable())).toEqual(['Configuration profile', 'Revision', 'Routing rule']);
    expect(
      referencesTable()
        .findRows()
        .map((row) => row.findAll('td, th').map((cell) => cell.getElement().textContent)),
    ).toEqual([
      ['lending', 'published r3', 'Big loans (big)'],
      ['lending', 'current', 'Big loans (big)'],
      ['mortgage', 'published', 'plain'],
      ['claims', 'published r2', 'Default outcome'],
    ]);
    expect(referencesSection().findHeader()!.getElement().textContent).toContain('(4)');
    // Each profile opens on its Routing Rules tab.
    expect(referencesTable().findBodyCell(1, 1)!.findLink()!.getElement().getAttribute('href')).toBe(
      '#/documents/config?version=lending&tab=routing',
    );
  });

  it('say what to do when no routing rule names the queue', async () => {
    await renderPage();

    const empty = referencesTable().findEmptySlot()!;
    expect(empty.getElement().textContent).toContain('No routing rule sends documents to this queue.');
    expect(empty.getElement().textContent).toContain('add a routing rule on the Routing Rules tab of a configuration profile');
    // The tab is named as the configuration page names it.
    expect(referencesSection().findHeader()!.getElement().textContent).toContain('Change them on the Routing Rules tab of the profile.');
    expect(empty.findButton()!.getElement().getAttribute('href')).toBe('#/documents/config');
    expect(referencesSection().findHeader()!.getElement().textContent).toContain('(0)');
  });

  it('show an error when they cannot be read, and read them again on Retry', async () => {
    getReviewQueueReferences.mockRejectedValueOnce(new Error('boom'));
    window.location.hash = '';
    renderWithIntl(
      <MemoryRouter initialEntries={['/review-queues/queues/default']}>
        <Routes>
          <Route path="/review-queues/queues/:queue" element={<ReviewQueuePage />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(referencesSection().findContent().findAlert()).not.toBeNull());
    const alert = referencesSection().findContent().findAlert()!;
    expect(alert.findHeader()!.getElement().textContent).toBe("Couldn't load the routing rules that use this queue");
    expect(alert.findContent().getElement().textContent).toBe('Failed to read the routing rules that use this queue: boom');

    act(() => alert.findActionSlot()!.findButton()!.click());

    await waitFor(() => expect(referencesSection().findContent().findAlert()).toBeNull());
    expect(getReviewQueueReferences).toHaveBeenCalledTimes(2);
    expect(referencesSection().findContent().findTable()!.findEmptySlot()).not.toBeNull();
  });

  it('are not read or shown for an Annotator', async () => {
    role.isAdmin = false;
    renderWithIntl(
      <MemoryRouter initialEntries={['/review-queues/queues/default']}>
        <Routes>
          <Route path="/review-queues/queues/:queue" element={<ReviewQueuePage />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(tables()[0].findRows()).toHaveLength(1));

    expect(tables()).toHaveLength(1);
    expect(referencesSection()).toBeUndefined();
    expect(getReviewQueueReferences).not.toHaveBeenCalled();
  });

  it('are read again by the page Refresh, not by the poll', async () => {
    await renderPage();
    expect(getReviewQueueReferences).toHaveBeenCalledTimes(1);

    const refresh = createWrapper(document.body)
      .findAllButtons()
      .find((b) => b.getElement().getAttribute('aria-label') === 'Refresh')!;
    await act(async () => {
      refresh.click();
    });

    await waitFor(() => expect(getReviewQueueReferences).toHaveBeenCalledTimes(2));
  });
});

describe("the Documents table's actions", () => {
  it('shows Assign and Unassign as buttons and groups the rest under More actions', async () => {
    await renderPage();
    const slot = itemsTable().findHeaderSlot()!;
    expect(slot.findAllButtons().map((b) => b.getElement().textContent)).toEqual(expect.arrayContaining(['Assign to…', 'Unassign']));
    const menu = slot.findButtonDropdown()!;
    expect(menu.getElement().textContent).toContain('More actions');
    menu.openDropdown();
    expect(menu.getElement().textContent).toMatch(/Queue.*Skip reviews/s);
    expect(menu.findItems().map((i) => i.getElement().getAttribute('data-testid'))).toEqual([
      'move',
      'remove',
      'skip-selected',
      'skip-by-profile',
    ]);
  });

  it('opens Skip All Reviews by profile from the menu', async () => {
    await renderPage();
    const menu = itemsTable().findHeaderSlot()!.findButtonDropdown()!;
    menu.openDropdown();
    act(() => menu.findItemById('skip-by-profile')!.click());
    await waitFor(() => expect(document.querySelector('[data-testid="bulk-skip"]')?.textContent).toBe('PROFILE'));
  });
});

describe('the Annotator column', () => {
  it("can be sorted, so each person's documents read together", async () => {
    await renderPage();
    const at = headers(itemsTable()).indexOf('Annotator');
    expect(at).toBeGreaterThan(-1);
    // Cloudscape renders a sorting control only on a column with a sortingField.
    expect(itemsTable().findColumnSortingArea(at + 1)).not.toBeNull();
  });
});

describe('the queue name', () => {
  it('is reported for the breadcrumb', async () => {
    const onName = vi.fn();
    await renderPage(onName);
    expect(onName).toHaveBeenCalledWith('Default');
  });
});

describe('queueAnnotatorRows', () => {
  it('puts the listed Annotators first, then anyone else holding items', () => {
    expect(
      queueAnnotatorRows(
        ['B@x.com', 'a@x.com'],
        [
          { email: 'c@x.com', claimed: 4 },
          { email: 'b@x.com', claimed: 1 },
        ],
      ),
    ).toEqual([
      { email: 'b@x.com', assigned: 1, listed: true },
      { email: 'a@x.com', assigned: 0, listed: true },
      { email: 'c@x.com', assigned: 4, listed: false },
    ]);
  });
});

describe('the queue Settings dialog', () => {
  const settingsModal = () =>
    createWrapper(document.body)
      .findAllModals()
      .find((m) => m.findHeader().getElement().textContent?.includes('Default'));
  const description = () => settingsModal()!.findContent().findTextarea()!;
  const refresh = async () => {
    const button = createWrapper(document.body)
      .findAllButtons()
      .find((b) => b.getElement().getAttribute('aria-label') === 'Refresh')!;
    await act(async () => {
      fireEvent.click(button.getElement());
    });
  };

  const openSettings = async () => {
    const settings = createWrapper(document.body)
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Settings')!;
    fireEvent.click(settings.getElement());
    await waitFor(() => expect(settingsModal()?.isVisible()).toBe(true));
  };

  it("does not carry one queue's definition over to another queue's page", async () => {
    await renderPage();
    await openSettings();
    await act(async () => {
      navigateTo('/review-queues/queues/claims');
    });
    // The listing has no 'claims', and the Default definition it held is not claims'.
    await waitFor(() => expect(settingsModal()?.isVisible() ?? false).toBe(false));
  });

  it('stays open, keeping what was typed, when a refresh briefly returns no definition', async () => {
    await renderPage();
    await openSettings();
    description().setTextareaValue('typed, not saved');

    listReviewQueues.mockResolvedValueOnce([]);
    await refresh();
    await waitFor(() => expect(listReviewQueues).toHaveBeenCalledTimes(2));
    expect(settingsModal()?.isVisible()).toBe(true);
    expect(description().getTextareaValue()).toBe('typed, not saved');

    // The definition comes back: still the same dialog, still what was typed.
    await refresh();
    await waitFor(() => expect(listReviewQueues).toHaveBeenCalledTimes(3));
    expect(settingsModal()?.isVisible()).toBe(true);
    expect(description().getTextareaValue()).toBe('typed, not saved');
  });
});
