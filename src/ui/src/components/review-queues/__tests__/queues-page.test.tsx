// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The queue tables in Review Studio › Review Management. What has to hold: Create queue
 * opens the queue dialog with no queue and creates a production queue; both tables show
 * exactly Queue, Items, Pending, Assigned, Annotators and the actions menu, in that
 * order (Assigned counts every assigned item, started or not; the other settings
 * stay in the queue's Settings dialog); every production queue but Default can be
 * deleted, even one that holds items, and the confirmation says those items move to
 * the Default queue; so can a queue that is no longer defined but still holds items
 * (what a delete that stopped part-way leaves), moving the rest; a refusal while an item is being completed is shown in the
 * dialog; and the confirmation lists the routing rules that name the queue, says where
 * their documents will go, and never holds the deletion back on that lookup. Above the
 * production table a warning says when routing rules are not evaluated, and why: Review
 * Routing is off in the active configuration profile (with a link to the configuration
 * page), or the Default queue is off (with a button that opens its settings); a state
 * that is not known gets no warning, and the Test Studio view gets none.
 */

import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const listReviewQueues = vi.fn();
const deleteReviewQueue = vi.fn();
const fetchQueuesOverview = vi.fn();
const listTestSetQueues = vi.fn();
const createReviewQueue = vi.fn();
const getReviewQueueReferences = vi.fn();
const fetchMyReviewQueues = vi.fn();
vi.mock('../reviewer-assignment', () => ({
  listReviewQueues: () => listReviewQueues(),
  deleteReviewQueue: (id: string) => deleteReviewQueue(id),
  listAnnotatorEmails: async () => [],
  listTestSetQueues: () => listTestSetQueues(),
  createReviewQueue: (input: unknown) => createReviewQueue(input),
  getReviewQueueReferences: (queueId: string) => getReviewQueueReferences(queueId),
  updateReviewQueue: vi.fn(),
  updateTestSetQueue: vi.fn(),
}));
vi.mock('../review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../review-api')>()),
  fetchQueuesOverview: () => fetchQueuesOverview(),
  fetchMyReviewQueues: () => fetchMyReviewQueues(),
}));
// Review Management is for Admins, who may read the review settings of the active profile.
vi.mock('../../../hooks/use-user-role', () => ({ default: () => ({ isAdmin: true, canReview: true, loading: false }) }));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
const setSuccessMessage = vi.fn();
vi.mock('../../../contexts/app', () => ({ default: () => ({ setSuccessMessage, setErrorMessage: vi.fn() }) }));

import { resetReviewActionSettingsCache } from '../../review-bulk/use-review-action-settings';
import QueuesPage, { deleteConfirmText, deleteDisabledReason } from '../QueuesPage';

const def = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  name: id[0].toUpperCase() + id.slice(1),
  description: '',
  priority: 0,
  checklist: [],
  rejectReasons: [],
  skipReasons: [],
  timeLimitMinutes: 60,
  eligibleAnnotators: [],
  isDefaultQueue: id === 'default',
  enabled: true,
  ...over,
});

/** What getMyReviewQueues answers, as far as the page reads it: the review settings in force. */
const mySettings = (over: Record<string, unknown> = {}) => ({
  queues: [],
  myClaimCount: 0,
  items: { items: [], nextToken: null },
  defaultQueueEnabled: true,
  bulkSkipSyncLimit: 50,
  reviewRoutingEnabled: true,
  ...over,
});

const health = (name: string, depth: number, claimed = 0, inProgress = 0) => ({
  reviewers: [],
  summary: { name, displayName: name, depth, unclaimed: depth - claimed, claimed, inProgress },
});

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);
const renderPage = async (path = '/') => {
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueuesPage />
    </MemoryRouter>,
    { wrapper: English },
  );
  await waitFor(() => expect(table().findRows().length).toBeGreaterThan(0));
};

const table = () => createWrapper(document.body).findTable()!;
const testSetTable = () => createWrapper(document.body).findTable()!;
const tableTitles = () =>
  createWrapper(document.body)
    .findAllTables()
    .map((t) => t.findHeaderSlot()?.findHeader()?.findHeadingText().getElement().textContent);
const viewSwitch = () => createWrapper(document.body).findSegmentedControl()!;
const headers = (t = table()) => t.findColumnHeaders().map((h) => h.getElement().textContent);
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
const typeInto = (modal: ReturnType<typeof deleteModal>, label: string, value: string) => {
  const formField = modal
    .findContent()
    .findAllFormFields()
    .find((f) => f.findLabel()?.getElement().textContent === label)!;
  fireEvent.change(formField.findControl()!.findTextarea()!.findNativeTextarea().getElement(), { target: { value } });
};
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
  listReviewQueues.mockReset().mockResolvedValue([def('default', { enabled: false }), def('claims', { priority: 5 })]);
  fetchQueuesOverview.mockReset().mockResolvedValue({ queues: [health('default', 0), health('claims', 3, 2, 1)] });
  listTestSetQueues.mockReset().mockResolvedValue([
    {
      id: 'ts-1',
      name: 'Invoices',
      fileCount: 6,
      pendingDocs: 2,
      assignedDocs: 1,
      inProgressDocs: 2,
      eligibleAnnotators: ['ann@example.com'],
      priority: 3,
      summaryStatus: 'READY',
    },
  ]);
  deleteReviewQueue.mockReset().mockResolvedValue(true);
  createReviewQueue.mockReset();
  getReviewQueueReferences.mockReset().mockResolvedValue([]);
  fetchMyReviewQueues.mockReset().mockResolvedValue(mySettings());
  resetReviewActionSettingsCache();
  setSuccessMessage.mockReset();
});

describe('the Production Queues table', () => {
  it('says that routing rules send documents to the queue a routing rule names, and what the Default queue receives', async () => {
    await renderPage();
    const header = table().findHeaderSlot()!.getElement().textContent ?? '';
    expect(header).toContain(
      "While Review Routing is on in a document's configuration profile and the Default queue is enabled, the routing rules of that profile send the document to the queue a routing rule names.",
    );
    expect(header).toContain(
      'The Default queue receives the documents the built-in routing rules hold, and documents whose queue is missing or disabled.',
    );
    expect(document.body.textContent).not.toContain('HITL');
    expect(document.body.textContent).not.toMatch(/human review/i);
    expect(document.body.textContent).toContain('Create and set up the production review queues');
  });

  it('shows exactly Queue, Items, Pending, Assigned, Annotators and the actions menu', async () => {
    await renderPage();
    expect(headers()).toEqual(['Queue', 'Items', 'Pending', 'Assigned', 'Annotators', 'Actions']);
    expect(cell('Claims', 'Items')).toBe('3');
    expect(cell('Claims', 'Pending')).toBe('1');
    // Every assigned item, whether or not its Annotator has started.
    expect(cell('Claims', 'Assigned')).toBe('2');
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

  it('lets a queue that is no longer defined but holds items be deleted again, moving them', async () => {
    // What a delete that stopped part-way leaves: the definition is gone first, the items after.
    fetchQueuesOverview.mockResolvedValue({
      queues: [
        health('default', 0),
        health('claims', 3, 2, 1),
        { ...health('gone', 2), summary: { ...health('gone', 2).summary, displayName: 'Gone Queue' } },
      ],
    });
    await renderPage();
    await waitFor(() => expect(table().findRows().length).toBe(3));
    const menu = openMenu('Gone Queue');
    expect(itemDisabled(menu.findItemById('delete')!)).toBe(false);
    expect(itemDisabled(menu.findItemById('edit')!)).toBe(true);
    menu.findItemById('delete')!.click();
    const modal = deleteModal();
    // The dialog says it is checking the routing rules until that lookup answers.
    await waitFor(() => expect(getReviewQueueReferences).toHaveBeenCalledWith('gone'));
    await act(async () => {});
    expect(modal.findContent().getElement().textContent).toBe(
      "Delete the queue Gone Queue (gone)? This can't be undone. 2 items will move to the Default queue.",
    );
    fireEvent.click(modal.findFooter()!.findAllButtons().at(-1)!.getElement());
    await waitFor(() => expect(deleteReviewQueue).toHaveBeenCalledWith('gone'));
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

describe('the warnings that routing rules are not evaluated', () => {
  /** The warnings on the page itself, not in a dialog, in order. */
  const pageAlerts = () =>
    createWrapper(document.body)
      .findAllAlerts()
      .filter((alert) => !alert.getElement().closest('[role="dialog"]'));
  const warnings = () => pageAlerts().map((alert) => alert.findContent().getElement().textContent);
  const ROUTING_OFF =
    "Review Routing is off in the active configuration profile, so no document is held for review. Turn it on in the profile's Processing Settings, on the Configuration page.";
  const DEFAULT_OFF = 'The Default review queue is off, so routing rules are not evaluated.';
  const queuesOn = () => listReviewQueues.mockResolvedValue([def('default'), def('claims', { priority: 5 })]);
  const settingsDialog = () =>
    createWrapper(document.body)
      .findAllModals()
      .find((w) => w.isVisible() && (w.findHeader().getElement().textContent ?? '').startsWith('Queue Settings'));

  it('shows none while Review Routing is on and the Default queue is enabled', async () => {
    queuesOn();
    await renderPage();
    await waitFor(() => expect(fetchMyReviewQueues).toHaveBeenCalledTimes(1));
    await act(async () => {});

    expect(warnings()).toEqual([]);
  });

  it('says Review Routing is off in the active profile, and links to the configuration page', async () => {
    queuesOn();
    fetchMyReviewQueues.mockResolvedValue(mySettings({ reviewRoutingEnabled: false }));
    await renderPage();

    await waitFor(() => expect(warnings()).toEqual([ROUTING_OFF]));
    const alert = pageAlerts()[0];
    expect(alert.findRootElement().getElement().className).toMatch(/type-warning/);
    const links = alert.findContent().findAllLinks();
    expect(links.map((link) => [link.getElement().textContent, link.getElement().getAttribute('href')])).toEqual([
      ['Configuration', '#/documents/config'],
    ]);
    // Nothing to press: the switch is on another page.
    expect(alert.findActionSlot()).toBeNull();
  });

  it('says the Default queue is off, with a button that opens its settings', async () => {
    await renderPage();

    await waitFor(() => expect(warnings()).toEqual([DEFAULT_OFF]));
    const alert = pageAlerts()[0];
    expect(alert.findRootElement().getElement().className).toMatch(/type-warning/);
    const open = alert.findActionSlot()!.findButton()!;
    expect(open.getElement().textContent).toBe('Open Default queue settings');
    expect(settingsDialog()).toBeUndefined();

    act(() => open.click());

    const dialog = settingsDialog()!;
    expect(dialog.findHeader().getElement().textContent).toContain('Default');
    // The switch the warning is about, as the queue has it: off.
    const enabled = dialog
      .findContent()
      .findAllFormFields()
      .find((f) => f.findLabel()?.getElement().textContent === 'Enabled')!;
    expect(enabled.findControl()!.findToggle()!.findNativeInput().getElement().checked).toBe(false);
  });

  it('shows both when both hold, Review Routing first', async () => {
    fetchMyReviewQueues.mockResolvedValue(mySettings({ reviewRoutingEnabled: false, defaultQueueEnabled: false }));
    await renderPage();

    await waitFor(() => expect(warnings()).toEqual([ROUTING_OFF, DEFAULT_OFF]));
  });

  it('reads the Default queue from the queues listed, not from a queue that merely is disabled', async () => {
    listReviewQueues.mockResolvedValue([def('default'), def('claims', { enabled: false })]);
    await renderPage();
    await waitFor(() => expect(fetchMyReviewQueues).toHaveBeenCalledTimes(1));
    await act(async () => {});

    expect(warnings()).toEqual([]);
  });

  it.each([
    [
      'the server could not read the active profile',
      () => fetchMyReviewQueues.mockResolvedValue(mySettings({ reviewRoutingEnabled: null })),
    ],
    [
      'an older server does not answer it',
      () => fetchMyReviewQueues.mockResolvedValue({ ...mySettings(), reviewRoutingEnabled: undefined }),
    ],
    ['the settings cannot be read', () => fetchMyReviewQueues.mockRejectedValue(new Error('offline'))],
  ])('shows no Review Routing warning on a guess: %s', async (_what, arrange) => {
    queuesOn();
    arrange();
    await renderPage();
    await waitFor(() => expect(fetchMyReviewQueues).toHaveBeenCalledTimes(1));
    await act(async () => {});

    expect(warnings()).toEqual([]);
  });

  it('keeps a failed load of the queues above them', async () => {
    fetchMyReviewQueues.mockResolvedValue(mySettings({ reviewRoutingEnabled: false }));
    await renderPage();
    await waitFor(() => expect(warnings()).toEqual([ROUTING_OFF, DEFAULT_OFF]));

    listReviewQueues.mockRejectedValue(new Error('boom'));
    await act(async () =>
      createWrapper(document.body)
        .findAllButtons()
        .find((b) => b.getElement().textContent === 'Refresh')!
        .click(),
    );

    await waitFor(() => expect(pageAlerts()).toHaveLength(3));
    expect(pageAlerts()[0].findHeader()!.getElement().textContent).toBe('Showing the last loaded queues');
    expect(warnings().slice(1)).toEqual([ROUTING_OFF, DEFAULT_OFF]);
  });

  it('shows none among the Test Studio queues, which no routing rule sends documents to', async () => {
    fetchMyReviewQueues.mockResolvedValue(mySettings({ reviewRoutingEnabled: false }));
    await renderPage();
    await waitFor(() => expect(warnings()).toEqual([ROUTING_OFF, DEFAULT_OFF]));

    viewSwitch().findSegments()[1].click();

    await waitFor(() => expect(tableTitles()).toEqual(['Test Studio Queues']));
    expect(warnings()).toEqual([]);
  });
});

describe('Create queue', () => {
  const createButton = () =>
    createWrapper(document.body)
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Create queue');
  const createModal = () =>
    createWrapper(document.body)
      .findAllModals()
      .find((w) => w.isVisible() && w.findHeader().getElement().textContent === 'Create Queue');
  const field = (label: string) =>
    createModal()!
      .findContent()
      .findAllFormFields()
      .find((f) => f.findLabel()?.getElement().textContent === label)!;
  const type = (label: string, value: string) => {
    act(() => {
      fireEvent.change(field(label).findControl()!.findInput()!.findNativeInput().getElement(), { target: { value } });
    });
  };

  it('opens the queue dialog with no queue, and creates a production queue', async () => {
    createReviewQueue.mockResolvedValue(def('escalations', { name: 'Escalations', version: 1 }));
    await renderPage();
    expect(createModal()).toBeUndefined();

    act(() => createButton()!.click());

    expect(createModal()).toBeDefined();
    // A new queue's id can be typed; an existing queue's cannot.
    expect(field('Queue id').findControl()!.findInput()!.findNativeInput().getElement().disabled).toBe(false);
    type('Queue id', 'escalations');
    type('Name', 'Escalations');
    fireEvent.click(createModal()!.findFooter()!.findAllButtons().at(-1)!.getElement());

    await waitFor(() => expect(createReviewQueue).toHaveBeenCalledTimes(1));
    expect(createReviewQueue.mock.calls[0][0]).toMatchObject({ id: 'escalations', name: 'Escalations', enabled: true, version: undefined });
    await waitFor(() => expect(setSuccessMessage).toHaveBeenCalledWith('Created the queue Escalations.'));
    // The new queue is listed at once, before the next read returns it.
    await waitFor(() => expect(table().getElement().textContent).toContain('Escalations'));
    expect(createModal()).toBeUndefined();
  });

  it('refuses an id that is already a queue', async () => {
    await renderPage();
    act(() => createButton()!.click());
    type('Queue id', 'claims');
    type('Name', 'Second claims');
    fireEvent.click(createModal()!.findFooter()!.findAllButtons().at(-1)!.getElement());

    expect(createModal()!.findContent().getElement().textContent).toContain('A queue with this id already exists.');
    expect(createReviewQueue).not.toHaveBeenCalled();
  });

  it('says a saved queue was saved, not created', async () => {
    const { updateReviewQueue } = await import('../reviewer-assignment');
    vi.mocked(updateReviewQueue).mockResolvedValue(def('claims', { name: 'Claims', version: 2 }) as never);
    await renderPage();
    openMenu('Claims').findItemById('edit')!.click();
    const settings = createWrapper(document.body)
      .findAllModals()
      .find((w) => w.isVisible() && (w.findHeader().getElement().textContent ?? '').startsWith('Queue Settings'))!;
    typeInto(settings, 'Skip reasons', 'Out of time');
    fireEvent.click(settings.findFooter()!.findAllButtons().at(-1)!.getElement());

    await waitFor(() => expect(setSuccessMessage).toHaveBeenCalledWith('Saved the queue Claims.'));
  });

  it('is not offered among the Test Studio queues, which come with their test sets', async () => {
    await renderPage('/?view=test');

    expect(createButton()).toBeUndefined();
  });
});

describe('the routing rules in the delete confirmation', () => {
  const REFERENCES = [
    { profile: 'lending', revision: 3, published: true, ruleId: 'big', ruleLabel: 'Big loans' },
    { profile: 'lending', revision: 4, published: false, ruleId: 'bigger', ruleLabel: null },
  ];
  const openDelete = async () => {
    await renderPage();
    openMenu('Claims').findItemById('delete')!.click();
    return deleteModal();
  };
  const deleteButton = (modal: ReturnType<typeof deleteModal>) => modal.findFooter()!.findAllButtons().at(-1)!;

  it('lists each rule that names the queue and says where its documents will go', async () => {
    getReviewQueueReferences.mockResolvedValue(REFERENCES);
    const modal = await openDelete();
    await waitFor(() => expect(modal.findContent().findTable()).not.toBeNull());

    expect(getReviewQueueReferences.mock.calls).toEqual([['claims']]);
    const alert = modal.findContent().findAlert()!;
    expect(alert.findHeader()!.getElement().textContent).toBe('2 routing rules send documents to this queue');
    expect(
      modal
        .findContent()
        .findTable()!
        .findRows()
        .map((row) => row.findAll('td, th').map((found) => found.getElement().textContent)),
    ).toEqual([
      ['lending', 'published r3', 'Big loans (big)'],
      ['lending', 'current', 'bigger'],
    ]);
    expect(alert.getElement().textContent).toContain(
      'Documents these routing rules send to this queue will go to the Default queue instead.',
    );
    // The confirmation's own text stays.
    expect(modal.findContent().getElement().textContent).toContain('3 items will move to the Default queue.');
    fireEvent.click(deleteButton(modal).getElement());
    await waitFor(() => expect(deleteReviewQueue).toHaveBeenCalledWith('claims'));
  });

  it('counts one rule in the singular', async () => {
    getReviewQueueReferences.mockResolvedValue(REFERENCES.slice(0, 1));
    const modal = await openDelete();

    await waitFor(() => expect(modal.findContent().findAlert()).not.toBeNull());
    expect(modal.findContent().findAlert()!.findHeader()!.getElement().textContent).toBe('1 routing rule sends documents to this queue');
  });

  it('shows the confirmation alone when no rule names the queue', async () => {
    const modal = await openDelete();
    await waitFor(() => expect(getReviewQueueReferences).toHaveBeenCalledWith('claims'));
    await act(async () => {});

    expect(modal.findContent().getElement().textContent).toBe(
      "Delete the queue Claims (claims)? This can't be undone. 3 items will move to the Default queue.",
    );
  });

  it('says it is checking while the lookup runs, and Delete works meanwhile', async () => {
    getReviewQueueReferences.mockReturnValue(new Promise(() => undefined));
    const modal = await openDelete();

    expect(modal.findContent().findStatusIndicator()!.getElement().textContent).toBe('Checking which routing rules use this queue');
    expect(deleteButton(modal).isDisabled()).toBe(false);
    fireEvent.click(deleteButton(modal).getElement());
    await waitFor(() => expect(deleteReviewQueue).toHaveBeenCalledWith('claims'));
  });

  it('shows a failed lookup, offers to retry it, and still allows Delete', async () => {
    getReviewQueueReferences.mockRejectedValueOnce(new Error('boom')).mockResolvedValue(REFERENCES);
    const modal = await openDelete();
    await waitFor(() => expect(modal.findContent().findAlert()).not.toBeNull());

    const alert = modal.findContent().findAlert()!;
    expect(alert.findHeader()!.getElement().textContent).toBe("Couldn't check which routing rules use this queue");
    expect(alert.findContent().getElement().textContent).toContain('Failed to read the routing rules that use this queue: boom');
    expect(alert.findContent().getElement().textContent).toContain(
      'You can still delete the queue. Documents a routing rule sends to it will go to the Default queue instead.',
    );
    expect(deleteButton(modal).isDisabled()).toBe(false);

    act(() => alert.findActionSlot()!.findButton()!.click());
    await waitFor(() => expect(modal.findContent().findTable()).not.toBeNull());
    expect(getReviewQueueReferences).toHaveBeenCalledTimes(2);

    fireEvent.click(deleteButton(modal).getElement());
    await waitFor(() => expect(deleteReviewQueue).toHaveBeenCalledWith('claims'));
  });

  it('does not show one queue the rules of the queue opened before it', async () => {
    getReviewQueueReferences.mockImplementation(async (queueId: string) => (queueId === 'claims' ? REFERENCES : []));
    listReviewQueues.mockResolvedValue([def('default'), def('claims'), def('other')]);
    const modal = await openDelete();
    await waitFor(() => expect(modal.findContent().findTable()).not.toBeNull());
    fireEvent.click(modal.findFooter()!.findAllButtons()[0].getElement());

    openMenu('Other').findItemById('delete')!.click();

    expect(deleteModal().findContent().findTable()).toBeNull();
    await waitFor(() => expect(getReviewQueueReferences).toHaveBeenLastCalledWith('other'));
    await act(async () => {});
    expect(deleteModal().findContent().findTable()).toBeNull();
    expect(deleteModal().findContent().findAlert()).toBeNull();
  });
});

describe('the Production | Test Studio switch', () => {
  it('shows the Production Queues alone by default, and reads only them', async () => {
    await renderPage();
    expect(tableTitles()).toEqual(['Production Queues']);
    expect(viewSwitch().findSelectedSegment()!.getElement().textContent).toBe('Production');
    expect(listTestSetQueues).not.toHaveBeenCalled();
  });

  it('shows the Test Studio Queues alone once switched, and reads them', async () => {
    await renderPage();
    viewSwitch().findSegments()[1].click();
    await waitFor(() => expect(tableTitles()).toEqual(['Test Studio Queues']));
    expect(listTestSetQueues).toHaveBeenCalled();
  });

  it('opens on the Test Studio Queues for ?view=test, as the queue pages’ breadcrumbs link', async () => {
    await renderPage('/?view=test');
    expect(tableTitles()).toEqual(['Test Studio Queues']);
    expect(listReviewQueues).not.toHaveBeenCalled();
  });
});

describe('the Test Studio Queues table', () => {
  it('shows the same columns as the production table, Assigned counting started documents too', async () => {
    await renderPage('/?view=test');
    await waitFor(() => expect(testSetTable().findRows().length).toBe(1));
    expect(headers(testSetTable())).toEqual(['Queue', 'Items', 'Pending', 'Assigned', 'Annotators', 'Actions']);
    const row = (header: string) =>
      testSetTable()
        .findBodyCell(1, headers(testSetTable()).indexOf(header) + 1)!
        .getElement().textContent;
    expect(row('Items')).toBe('6');
    expect(row('Pending')).toBe('2');
    expect(row('Assigned')).toBe('3');
  });
});

describe('the table helpers', () => {
  it('refuses deleting only Default and undefined queues', () => {
    expect(deleteDisabledReason(def('default'))).toBe("The default queue can't be deleted");
    expect(deleteDisabledReason(def('escalation'))).toBeUndefined();
    expect(deleteDisabledReason(null)).toBe('This queue is not defined');
    expect(deleteDisabledReason(null, 0)).toBe('This queue is not defined');
    expect(deleteDisabledReason(null, 1)).toBeUndefined();
    expect(deleteDisabledReason(null, 2)).toBeUndefined();
    expect(deleteDisabledReason(def('default'), 2)).toBe("The default queue can't be deleted");
  });

  it('names the items that will move only when there are some', () => {
    expect(deleteConfirmText({ id: 'claims', name: 'Claims' }, 0)).not.toContain('Default queue');
    expect(deleteConfirmText({ id: 'claims', name: 'Claims' }, 1)).toContain('1 item will move to the Default queue.');
  });
});
