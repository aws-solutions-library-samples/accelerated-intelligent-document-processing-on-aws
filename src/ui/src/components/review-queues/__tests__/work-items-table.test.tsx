// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The HITL work table's Document column: rows whose keys share a file name show
 * different text (the distinguishing trailing path), long names are cut in the
 * middle, and the full object key is the hover title. The Queue column shows the
 * queue's name, and its id when the API reports no name; the filter finds a row by
 * either. The Reason column shows the reason of the routing rule when the item has one,
 * else its first alert, else the routing rule; a long reason is cut, with the whole of
 * it in a popover that opens from the keyboard. The table shows the columns its page
 * names, offers the others in its settings, and keeps the choice in the browser under
 * the page's key.
 */

import React from 'react';
import { act, fireEvent, render as renderPlain } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it } from 'vitest';

import WorkItemsTable, { REASON_CELL_CHARS, WORK_ITEM_COLUMNS, columnDisplayOf } from '../WorkItemsTable';
import type { WorkItem } from '../work-items';

const KEY = 'test-work-items-preferences';

beforeEach(() => {
  localStorage.clear();
});

/** The table reads its messages (and a routing rule's) through react-intl. */
const English = ({ children }: { children: React.ReactNode }): React.JSX.Element => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);
const render = (ui: React.ReactElement) => renderPlain(ui, { wrapper: English });

const row = (objectKey: string, over: Partial<WorkItem> = {}): WorkItem => ({
  id: objectKey,
  source: 'production',
  objectKey,
  name: objectKey.split('/').pop() ?? objectKey,
  queue: 'default',
  priority: null,
  enqueuedAt: null,
  reason: null,
  extraReasons: 0,
  routingReason: null,
  policyFailureCount: 0,
  state: { label: 'Waiting', type: 'pending' },
  assignedTo: null,
  assignedToMe: false,
  href: '#/edit',
  ...over,
});

const documentCells = (keys: string[]): HTMLElement[] => {
  const { container } = render(
    <WorkItemsTable items={keys.map((key) => row(key))} title="Items" columns={['document']} preferencesKey={KEY} />,
  );
  const table = createWrapper(container).findTable()!;
  return keys.map((_, i) =>
    table
      .findBodyCell(i + 1, 1)!
      .find('span[title]')!
      .getElement(),
  );
};

describe('WorkItemsTable document names', () => {
  it('renders different text for two rows with the same file name, with the full key on hover', () => {
    const keys = ['inbound/2026/a/lending_package.pdf', 'inbound/2026/b/lending_package.pdf'];
    const cells = documentCells(keys);
    expect(cells[0].textContent).toBe('a/lending_package.pdf');
    expect(cells[1].textContent).toBe('b/lending_package.pdf');
    expect(cells[0].getAttribute('title')).toBe(keys[0]);
    expect(cells[1].getAttribute('title')).toBe(keys[1]);
  });

  it('cuts a long distinguishing name in the middle, keeping its start and end', () => {
    const keys = ['q3-customer-onboarding-archive/lending_package_final.pdf', 'q4-customer-onboarding-archive/lending_package_final.pdf'];
    const cells = documentCells(keys);
    const [first, second] = cells.map((c) => c.textContent ?? '');
    expect(first).toHaveLength(40);
    expect(first).toBe('q3-customer-onboardi…g_package_final.pdf');
    expect(second.startsWith('q4-')).toBe(true);
    expect(cells[0].getAttribute('title')).toBe(keys[0]);
  });
});

describe('WorkItemsTable queue names', () => {
  const ITEMS = [
    row('a.pdf', { queue: 'claims-high-value', queueName: 'High-value claims' }),
    row('b.pdf', { queue: 'legacy-queue', queueName: null }),
    row('c.pdf', { queue: 'default' }),
  ];
  const renderQueues = () => {
    const { container } = render(<WorkItemsTable items={ITEMS} title="Items" columns={['document', 'queue']} preferencesKey={KEY} />);
    return createWrapper(container).findTable()!;
  };
  const queues = (table: ReturnType<typeof renderQueues>): (string | null)[] =>
    table.findRows().map((_row, index) => table.findBodyCell(index + 1, 2)!.getElement().textContent);

  it('shows the name of the queue, and its id when there is no name', () => {
    expect(queues(renderQueues())).toEqual(['High-value claims', 'legacy-queue', 'default']);
  });

  it('sorts the column by what it shows', () => {
    const table = renderQueues();
    act(() => table.findColumnSortingArea(2)!.click());
    expect(queues(table)).toEqual(['default', 'High-value claims', 'legacy-queue']);
  });

  it('finds a row by the name of its queue and by its id', () => {
    const table = renderQueues();
    const filter = table.findTextFilter()!;

    act(() => filter.findInput().setInputValue('high-value claims'));
    expect(queues(table)).toEqual(['High-value claims']);

    act(() => filter.findInput().setInputValue('claims-high'));
    expect(queues(table)).toEqual(['High-value claims']);
  });
});

describe('WorkItemsTable reason', () => {
  const LONG =
    'Invoice total 1250.00 is over 1000. Check the total against the line items, and the vendor against the approved vendor list.';
  const ITEMS = [
    row('alert.pdf', {
      reason: 'Low confidence: Total 0.41 < 0.80',
      extraReasons: 2,
      routedBy: { ruleId: 'builtin-confidence-alerts', ruleLabel: 'Confidence alerts', fallbackReason: '' },
    }),
    row('rule.pdf', { routedBy: { ruleId: 'claims-over-1000', ruleLabel: 'Claims over 1000', fallbackReason: '' } }),
    row('no-label.pdf', { routedBy: { ruleId: 'plain', ruleLabel: null, fallbackReason: '' } }),
    row('default.pdf', { routedBy: { ruleId: '', ruleLabel: null, fallbackReason: '' } }),
    row('legacy.pdf', { routedBy: null }),
  ];
  const renderReasons = (items: WorkItem[] = ITEMS) => {
    const { container } = render(<WorkItemsTable items={items} title="Items" columns={['document', 'reason']} preferencesKey={KEY} />);
    return createWrapper(container).findTable()!;
  };
  const reasons = (table: ReturnType<typeof renderReasons>): (string | null)[] =>
    table.findRows().map((_row, index) => table.findBodyCell(index + 1, 2)!.getElement().textContent);

  it('is headed Reason', () => {
    expect(
      renderReasons()
        .findColumnHeaders()
        .map((header) => header.getElement().textContent),
    ).toEqual(['Document', 'Reason']);
  });

  it('shows the first alert when the item has no routing reason, and otherwise the rule that routed the item', () => {
    expect(reasons(renderReasons())).toEqual([
      'Low confidence: Total 0.41 < 0.80 +2 more',
      'Routed by rule: Claims over 1000',
      'Routed by rule: plain',
      'Routed by the default outcome',
      '—',
    ]);
  });

  it('shows the reason of the routing rule first, before any alert', () => {
    const table = renderReasons([
      row('routed.pdf', {
        routingReason: 'Invoice total 1250.00 is over 1000.',
        reason: 'Low confidence: Total 0.41 < 0.80',
        extraReasons: 2,
        routedBy: { ruleId: 'claims-over-1000', ruleLabel: 'Claims over 1000', fallbackReason: '' },
      }),
    ]);

    expect(reasons(table)).toEqual(['Invoice total 1250.00 is over 1000.']);
    // A reason that fits is plain text: there is nothing more to open.
    expect(table.findBodyCell(1, 2)!.findPopover()).toBeNull();
  });

  it('counts the failed policy rules after the reason', () => {
    const table = renderReasons([
      row('one.pdf', { routingReason: 'A policy rule failed.', policyFailureCount: 1 }),
      row('many.pdf', { routingReason: 'Policy rules failed.', policyFailureCount: 12 }),
    ]);

    expect(reasons(table)).toEqual(['A policy rule failed. · 1 failed policy rule', 'Policy rules failed. · 12 failed policy rules']);
  });

  it('cuts a long reason, and shows the whole of it in a popover that opens from the keyboard', () => {
    const table = renderReasons([row('long.pdf', { routingReason: LONG })]);
    const cell = table.findBodyCell(1, 2)!;

    expect(LONG.length).toBeGreaterThan(REASON_CELL_CHARS);
    // The first 60 characters, without the space the cut fell on, and an ellipsis.
    expect(LONG.slice(0, REASON_CELL_CHARS)).toBe('Invoice total 1250.00 is over 1000. Check the total against ');
    expect(cell.getElement().textContent).toBe('Invoice total 1250.00 is over 1000. Check the total against…');

    // The cut text is a button: it takes focus and opens with Enter, as a click does.
    const trigger = cell.findPopover()!.findTrigger().getElement();
    expect(trigger.tagName).toBe('BUTTON');
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    act(() => {
      fireEvent.click(trigger);
    });
    expect(cell.findPopover()!.findContent()!.getElement().textContent).toBe(LONG);
  });

  it('wraps in its column, so the table is no wider than its page for a reason', () => {
    const table = renderReasons([row('long.pdf', { routingReason: LONG })]);

    // Cloudscape marks the content of a cell that wraps; without the mark a cell is as wide as its text.
    expect(table.findBodyCell(1, 2)!.getElement().querySelector('[class*="body-cell-wrap"]')).not.toBeNull();
  });

  it('shows what a reason holds as text, not as markup', () => {
    const markup = '<img src=x onerror="alert(1)"> **bold** [link](https://example.com)';
    const table = renderReasons([row('markup.pdf', { routingReason: markup })]);
    const cell = table.findBodyCell(1, 2)!.getElement();

    expect(cell.textContent).toBe(`${markup.slice(0, REASON_CELL_CHARS)}…`);
    expect(cell.querySelector('img')).toBeNull();
    expect(cell.querySelector('a')).toBeNull();
    expect(cell.querySelector('strong')).toBeNull();
  });

  it('finds an item by the label or the id of its rule, and by its reason', () => {
    const table = renderReasons([...ITEMS, row('routed.pdf', { routingReason: 'The vendor is not on the approved list.' })]);
    const filter = table.findTextFilter()!;

    act(() => filter.findInput().setInputValue('claims over'));
    expect(reasons(table)).toEqual(['Routed by rule: Claims over 1000']);

    // By its id too, whether or not it has a label. No document key holds these words.
    act(() => filter.findInput().setInputValue('claims-over-1000'));
    expect(reasons(table)).toEqual(['Routed by rule: Claims over 1000']);

    act(() => filter.findInput().setInputValue('plain'));
    expect(reasons(table)).toEqual(['Routed by rule: plain']);

    act(() => filter.findInput().setInputValue('approved list'));
    expect(reasons(table)).toEqual(['The vendor is not on the approved list.']);
  });
});

describe('WorkItemsTable columns', () => {
  const COLUMNS = ['document', 'source', 'queue', 'priority', 'age', 'reason', 'state', 'owner'];
  const VISIBLE = ['document', 'queue', 'reason', 'state'];
  const ITEMS = [row('a.pdf'), row('b.pdf')];
  const renderTable = (props: Partial<React.ComponentProps<typeof WorkItemsTable>> = {}) => {
    const { container, unmount } = render(
      <WorkItemsTable items={ITEMS} title="Items" columns={COLUMNS} defaultVisible={VISIBLE} preferencesKey={KEY} {...props} />,
    );
    return { table: createWrapper(container).findTable()!, unmount };
  };
  const headers = (table: ReturnType<typeof renderTable>['table']): (string | null)[] =>
    table.findColumnHeaders().map((header) => header.getElement().textContent);
  const openSettings = (table: ReturnType<typeof renderTable>['table']) => {
    const preferences = table.findCollectionPreferences()!;
    act(() => preferences.findTriggerButton().click());
    return preferences.findModal()!;
  };
  const option = (modal: ReturnType<typeof openSettings>, label: string) =>
    modal
      .findContentDisplayPreference()!
      .findOptions()
      .find((entry) => entry.findLabel().getElement().textContent === label)!;
  const confirm = (modal: ReturnType<typeof openSettings>): void => {
    act(() => modal.findConfirmButton()!.click());
  };

  it('shows the columns its page names as essential, in the order of the columns it offers', () => {
    expect(headers(renderTable().table)).toEqual(['Document', 'Queue', 'Reason', 'State']);
  });

  it('shows every column it offers when the page names none as essential', () => {
    expect(headers(renderTable({ defaultVisible: undefined }).table)).toEqual([
      'Document',
      'Source',
      'Queue',
      'Priority',
      'Age',
      'Reason',
      'State',
      'Annotator',
    ]);
  });

  it('keeps the row menu last, whatever columns are shown', () => {
    expect(headers(renderTable({ onRowAction: () => undefined }).table)).toEqual(['Document', 'Queue', 'Reason', 'State', 'Actions']);
  });

  it('offers every column in its settings, with the document always shown', () => {
    const modal = openSettings(renderTable().table);
    const options = modal.findContentDisplayPreference()!.findOptions();

    expect(options.map((entry) => entry.findLabel().getElement().textContent)).toEqual([
      'Document',
      'Source',
      'Queue',
      'Priority',
      'Age',
      'Reason',
      'State',
      'Annotator',
    ]);
    expect(options.map((entry) => entry.findVisibilityToggle().findNativeInput().getElement().checked)).toEqual([
      true,
      false,
      true,
      false,
      false,
      true,
      true,
      false,
    ]);
    expect(option(modal, 'Document').findVisibilityToggle().findNativeInput().getElement().disabled).toBe(true);
    expect(option(modal, 'Source').findVisibilityToggle().findNativeInput().getElement().disabled).toBe(false);
  });

  it('offers only the columns its page offers', () => {
    const modal = openSettings(renderTable({ columns: ['document', 'reason'], defaultVisible: undefined }).table);

    expect(
      modal
        .findContentDisplayPreference()!
        .findOptions()
        .map((entry) => entry.findLabel().getElement().textContent),
    ).toEqual(['Document', 'Reason']);
  });

  it('shows a column that is switched on in the settings, and keeps the choice in the browser under its key', () => {
    const first = renderTable();
    const modal = openSettings(first.table);
    act(() => option(modal, 'Priority').findVisibilityToggle().findNativeInput().click());
    act(() => option(modal, 'State').findVisibilityToggle().findNativeInput().click());
    confirm(modal);

    expect(headers(first.table)).toEqual(['Document', 'Queue', 'Priority', 'Reason']);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toMatchObject({
      contentDisplay: expect.arrayContaining([
        { id: 'priority', visible: true },
        { id: 'state', visible: false },
      ]),
    });

    // The same page, opened again, shows the same columns; another page keeps its own.
    first.unmount();
    expect(headers(renderTable().table)).toEqual(['Document', 'Queue', 'Priority', 'Reason']);
    expect(headers(renderTable({ preferencesKey: 'another-page' }).table)).toEqual(['Document', 'Queue', 'Reason', 'State']);
  });

  it('changes nothing when the settings are cancelled', () => {
    const { table } = renderTable();
    const modal = openSettings(table);
    act(() => option(modal, 'Priority').findVisibilityToggle().findNativeInput().click());
    act(() => modal.findCancelButton()!.click());

    expect(headers(table)).toEqual(['Document', 'Queue', 'Reason', 'State']);
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it('shows as many rows a page as the settings say', () => {
    const many = Array.from({ length: 30 }, (_, index) => row(`doc-${index}.pdf`));
    const { table } = renderTable({ items: many });
    expect(table.findRows()).toHaveLength(25);

    const modal = openSettings(table);
    const sizes = modal.findPageSizePreference()!.findOptions();
    expect(sizes.map((size) => size.findLabel().getElement().textContent)).toEqual(['10 items', '25 items', '50 items']);
    act(() => sizes[0].findNativeInput().click());
    confirm(modal);

    expect(table.findRows()).toHaveLength(10);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toMatchObject({ pageSize: 10 });
  });

  it('reads what the browser kept defensively', () => {
    // Kept by an earlier version: a column that is gone, no reason column, a page size that is not offered.
    localStorage.setItem(
      KEY,
      JSON.stringify({
        pageSize: 7,
        contentDisplay: [{ id: 'state', visible: true }, { id: 'gone', visible: true }, { id: 'document', visible: false }, null, 'text'],
      }),
    );
    const many = Array.from({ length: 30 }, (_, index) => row(`doc-${index}.pdf`));
    const { table, unmount } = renderTable({ items: many });

    // The kept order first, the document shown all the same, then the columns the settings did not know.
    expect(headers(table)).toEqual(['State', 'Document', 'Queue', 'Reason']);
    expect(table.findRows()).toHaveLength(25);

    unmount();
    localStorage.setItem(KEY, JSON.stringify('not an object'));
    expect(headers(renderTable().table)).toEqual(['Document', 'Queue', 'Reason', 'State']);
  });

  it('gives the queue a width and no least width, so a short name leaves its room to the reason', () => {
    const { table } = renderTable();
    const queue = table.findColumnHeaders()[1].getElement();

    expect(queue.textContent).toBe('Queue');
    expect(queue.style.width).toBe('200px');
    expect(queue.style.minWidth).toBe('');
  });
});

describe('columnDisplayOf', () => {
  const COLUMNS = ['document', 'queue', 'reason', 'state'];

  it('is the columns in their order, the default ones shown, when nothing is kept', () => {
    expect(columnDisplayOf(undefined, COLUMNS, ['document', 'reason'])).toEqual([
      { id: 'document', visible: true },
      { id: 'queue', visible: false },
      { id: 'reason', visible: true },
      { id: 'state', visible: false },
    ]);
  });

  it('keeps the kept order and choices, drops what is gone or repeated, and adds what is new', () => {
    const kept = [
      { id: 'state', visible: false },
      { id: 'gone', visible: true },
      { id: 'queue', visible: true },
      { id: 'state', visible: true },
    ];

    expect(columnDisplayOf(kept, COLUMNS, ['document', 'reason'])).toEqual([
      { id: 'state', visible: false },
      { id: 'queue', visible: true },
      { id: 'document', visible: true },
      { id: 'reason', visible: true },
    ]);
  });

  it('never hides the document', () => {
    expect(columnDisplayOf([{ id: 'document', visible: false }], ['document'], [])).toEqual([{ id: 'document', visible: true }]);
    expect(WORK_ITEM_COLUMNS.filter((column) => 'alwaysVisible' in column).map((column) => column.id)).toEqual(['document']);
  });
});
