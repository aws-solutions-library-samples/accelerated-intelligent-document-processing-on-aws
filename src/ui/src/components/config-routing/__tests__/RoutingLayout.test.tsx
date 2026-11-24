// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Routing Rules tab: the routing rules in evaluation order, what an Admin can change
 * and what each change passes to `onChange`, what another role sees, and the warnings
 * (Review Routing off, the Default queue off, a policy rule result read while policy
 * rule validation is off, queues that do not exist, routing rules that never match).
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const listReviewQueues = vi.fn();
vi.mock('../../review-queues/reviewer-assignment', () => ({ listReviewQueues: () => listReviewQueues() }));
const convert = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...args: unknown[]) => convert(...args) }) }));
const exportToTextFile = vi.fn();
vi.mock('../../common/download-func', () => ({ exportToTextFile: (...args: unknown[]) => exportToTextFile(...args) }));

import RoutingLayout from '../RoutingLayout';
import {
  MAX_RULES,
  RULE_ID_PATTERN,
  readRouting,
  tableOf,
  type RoutingAction,
  type RoutingConfig,
  type RoutingRule,
} from '../routing-types';

const toQueue = (queue: string): RoutingAction[] => [{ type: 'review_queue', queue }];

const FAILED: RoutingRule = {
  id: 'builtin-extraction-failed',
  label: 'Failed extractions',
  enabled: true,
  conditions: [
    { input: 'reviewed', path: '', aggregate: 'any', test: 'false' },
    { input: 'extraction_failed_count', path: '', aggregate: 'any', test: '> 0' },
  ],
  actions: toQueue('default'),
  reason: 'Sections whose extraction failed: {extraction_failed_count}. They have no extracted values: enter them from the document.',
};
const ALERTS: RoutingRule = {
  id: 'builtin-confidence-alerts',
  label: 'Confidence alerts',
  enabled: true,
  conditions: [
    { input: 'reviewed', path: '', aggregate: 'any', test: 'false' },
    { input: 'confidence_alert_count', path: '', aggregate: 'any', test: '> 0' },
  ],
  actions: toQueue('default'),
  reason: 'Fields below the confidence threshold: {confidence_alert_count}.',
};
const REQUIRED: RoutingRule = {
  id: 'builtin-missing-required',
  label: 'Missing required fields',
  enabled: true,
  conditions: [{ input: 'missing_required_field_count', path: '', aggregate: 'any', test: '> 0' }],
  actions: toQueue('default'),
  reason: '',
};
const CLAIMS: RoutingRule = {
  id: 'claims-over-1000',
  label: 'Claims over 1000',
  enabled: true,
  conditions: [
    { input: 'field_value', path: 'Invoice:Total', aggregate: 'any', test: '> 1000' },
    { input: 'section_classes', path: '', aggregate: 'all', test: '"Invoice"' },
  ],
  actions: toQueue('claims'),
  reason: 'The total is {field_value:Invoice:Total}.',
};
const QUIET: RoutingRule = {
  id: 'w2-no-review',
  label: '',
  enabled: false,
  conditions: [{ input: 'document_class', path: '', aggregate: 'any', test: '"W2"' }],
  actions: [],
  reason: '',
};
const POLICY: RoutingRule = {
  id: 'coverage-failed',
  label: 'Coverage failed',
  enabled: true,
  conditions: [{ input: 'policy_rule_verdict', path: 'prior_auth.coverage', aggregate: 'any', test: '"fail"' }],
  actions: toQueue('claims'),
  reason: '',
};

const section = (rules: RoutingRule[], extra: Record<string, unknown> = {}) => ({
  tables: [
    {
      id: 'review-routing',
      stage: 'after_policy_rule_validation',
      hit_policy: 'FIRST',
      engine: 'builtin',
      rules,
      default_outcome: { actions: [], reason: '' },
      ...extra,
    },
  ],
  max_consecutive_placements: 3,
});
// The built-in routing rules of the system defaults, in their order.
const DEFAULTS = section([FAILED, ALERTS, REQUIRED]);
const QUEUES = [
  { id: 'default', name: 'Default', isDefaultQueue: true, enabled: true },
  { id: 'claims', name: 'Claims team', isDefaultQueue: false, enabled: true },
];

const onOpenProcessingSettings = vi.fn();

const renderTab = async (props: Partial<React.ComponentProps<typeof RoutingLayout>> = {}) => {
  const onChange = vi.fn();
  render(
    <IntlProvider locale="en">
      <RoutingLayout
        routing={section([CLAIMS, ALERTS, QUIET])}
        onChange={onChange}
        readOnly={false}
        reviewRoutingEnabled
        policyRuleValidationEnabled
        onOpenProcessingSettings={onOpenProcessingSettings}
        defaults={DEFAULTS}
        profileName="claims"
        {...props}
      />
    </IntlProvider>,
  );
  if (!props.readOnly) await waitFor(() => expect(listReviewQueues).toHaveBeenCalled());
  // Let the queue list settle into state.
  await act(async () => {});
  return onChange;
};

// The key codes Cloudscape's menu reads.
const KeyCode = { enter: 13, up: 38, down: 40 } as const;

const wrapper = () => createWrapper(document.body);
// The tab itself; the dialogs are rendered outside it.
const tab = () => wrapper().findContainer()!;
const table = () => wrapper().findTable()!;
const header = (): string[] =>
  table()
    .findColumnHeaders()
    .map((cell) => cell.getElement().textContent?.trim() ?? '');
const column = (name: string): number => header().indexOf(name) + 1;
const cellText = (row: number, name: string): string => table().findBodyCell(row, column(name))!.getElement().textContent ?? '';
const ids = (): string[] =>
  table()
    .findRows()
    .map((_row, index) =>
      cellText(index + 1, 'Id')
        .replace('Built-in', '')
        .trim(),
    );
const lastRules = (onChange: ReturnType<typeof vi.fn>): RoutingRule[] => tableOf(onChange.mock.calls.at(-1)![0] as RoutingConfig).rules;
const menu = (row: number) => table().findBodyCell(row, column('Actions'))!.findButtonDropdown()!;
const pick = (row: number, item: string): void => {
  act(() => menu(row).openDropdown());
  act(() => menu(row).findItemById(item)!.click());
};
const disabledItem = (element: Element): boolean =>
  element.matches('[aria-disabled="true"]') || element.querySelector('[aria-disabled="true"]') !== null;
const itemDisabled = (row: number, item: string): boolean => disabledItem(menu(row).findItemById(item)!.getElement());
// The menu beside "Add routing rule".
const moreActions = () => tab().findHeader()!.findButtonDropdown()!;
const pickAction = (item: string): void => {
  act(() => moreActions().openDropdown());
  act(() => moreActions().findItemById(item)!.click());
};
const labelLink = (row: number) => table().findBodyCell(row, column('Label'))!.findLink()!;
// The tab holds three dialogs (the routing rule, the import confirmation, the delete
// confirmation); at most one is open.
const openModals = () =>
  wrapper()
    .findAllModals()
    .filter((modal) => modal.isVisible());
const openModal = () => {
  const [open, ...others] = openModals();
  expect(others).toHaveLength(0);
  return open;
};
const defaultOutcome = () => tab().findContent().findRadioGroup()!;
// The reason of the default outcome, the only reason on the tab itself.
const reasonField = () =>
  tab()
    .findContent()
    .findAllFormFields()
    .find((field) => field.findLabel()?.getElement().textContent === 'Reason for the reviewer (optional)') ?? null;
// Lets a request the tab started be answered.
const settle = (): Promise<void> => act(async () => {});
const fileInput = (): HTMLInputElement => screen.getByLabelText('DMN file to import') as HTMLInputElement;

beforeEach(() => {
  listReviewQueues.mockReset().mockResolvedValue(QUEUES);
  convert.mockReset();
  exportToTextFile.mockReset();
  onOpenProcessingSettings.mockReset();
});

describe('the routing rules', () => {
  it('are headed Routing Rules, with how many there are', async () => {
    await renderTab();

    const heading = tab().findHeader()!.findHeader()!;
    expect(heading.findHeadingText().getElement().textContent).toBe('Routing Rules');
    expect(heading.findCounter()!.getElement().textContent?.trim()).toBe('(3)');
  });

  it('are listed under these columns', async () => {
    await renderTab();

    expect(header()).toEqual(['Reorder', 'Order', 'Id', 'Label', 'Conditions', 'Outcome', 'Enabled', 'Actions']);
  });

  it('are shown in the order they are evaluated', async () => {
    await renderTab();

    expect(ids()).toEqual(['claims-over-1000', 'builtin-confidence-alerts', 'w2-no-review']);
    expect([1, 2, 3].map((row) => cellText(row, 'Order'))).toEqual(['1', '2', '3']);
  });

  it('read their conditions as one sentence: each input, what it reads, how a list is read, and the test', async () => {
    const three = { ...CLAIMS, id: 'three', conditions: [...CLAIMS.conditions, QUIET.conditions[0]] };
    await renderTab({ routing: section([CLAIMS, three, QUIET]) });

    expect(cellText(1, 'Conditions')).toBe('Field value Invoice:Total > 1000 and Section classes (All values) "Invoice"');
    expect(cellText(2, 'Conditions')).toBe(
      'Field value Invoice:Total > 1000, Section classes (All values) "Invoice", and Document class "W2"',
    );
    expect(cellText(3, 'Conditions')).toBe('Document class "W2"');
  });

  it('say that a routing rule with no conditions matches every document', async () => {
    await renderTab({ routing: section([{ ...CLAIMS, conditions: [] }]) });

    expect(cellText(1, 'Conditions')).toBe('Every document');
  });

  it('say where a document goes: the review queue by name to an Admin, or that it is not reviewed', async () => {
    await renderTab();

    expect([1, 2, 3].map((row) => cellText(row, 'Outcome'))).toEqual([
      'Review queue: Claims team',
      'Review queue: Default',
      'Not reviewed',
    ]);
    // A destination, not a status.
    expect(table().findBodyCell(1, column('Outcome'))!.findStatusIndicator()).toBeNull();
  });

  it('mark the routing rules that ship with the product', async () => {
    await renderTab();

    expect(cellText(2, 'Id')).toContain('Built-in');
    expect(cellText(1, 'Id')).not.toContain('Built-in');
  });

  it('show a dash for a routing rule with no label', async () => {
    await renderTab();

    expect(cellText(3, 'Label')).toBe('—');
  });
});

describe('the label of a routing rule', () => {
  it('opens the routing rule for an Admin to edit', async () => {
    const onChange = await renderTab();

    act(() => labelLink(1).click());

    expect(openModal().findHeader().getElement().textContent).toContain('Edit routing rule claims-over-1000');
    expect(screen.getByRole('button', { name: 'Save routing rule' })).toBeTruthy();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('is named by the id when the routing rule has no label', async () => {
    await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Routing rule w2-no-review' }));

    expect(openModal().findHeader().getElement().textContent).toContain('Edit routing rule w2-no-review');
  });
});

describe('what an Admin changes', () => {
  it('adds a routing rule at the end', async () => {
    const onChange = await renderTab();

    fireEvent.click(screen.getByRole('button', { name: 'Add routing rule' }));
    const modal = openModal();
    act(() => {
      fireEvent.change(modal.findContent().findAll('input')[0].getElement(), { target: { value: 'new-rule' } });
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save routing rule' }));

    expect(lastRules(onChange).map((rule) => rule.id)).toEqual([
      'claims-over-1000',
      'builtin-confidence-alerts',
      'w2-no-review',
      'new-rule',
    ]);
    expect(lastRules(onChange)[3]).toEqual({
      id: 'new-rule',
      label: '',
      enabled: true,
      conditions: [],
      actions: toQueue('default'),
      reason: '',
    });
    expect(openModals()).toHaveLength(0);
  });

  it('edits a routing rule in place, and changes nothing else of it', async () => {
    const onChange = await renderTab();

    pick(1, 'edit');
    const modal = openModal();
    expect(modal.findHeader().getElement().textContent).toContain('claims-over-1000');
    act(() => {
      fireEvent.change(modal.findContent().findAll('input')[1].getElement(), { target: { value: 'Large claims' } });
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save routing rule' }));

    expect(lastRules(onChange).map((rule) => rule.id)).toEqual(['claims-over-1000', 'builtin-confidence-alerts', 'w2-no-review']);
    expect(lastRules(onChange)[0]).toEqual({ ...CLAIMS, label: 'Large claims' });
  });

  it('deletes a routing rule once the deletion is confirmed', async () => {
    const onChange = await renderTab();

    pick(2, 'delete');

    expect(onChange).not.toHaveBeenCalled();
    const confirmation = openModal();
    expect(confirmation.findHeader().getElement().textContent).toContain('Delete routing rule builtin-confidence-alerts?');
    expect(confirmation.findContent().getElement().textContent).toContain('Nothing is saved until you save the configuration.');
    fireEvent.click(within(confirmation.getElement()).getByRole('button', { name: 'Delete' }));

    expect(lastRules(onChange).map((rule) => rule.id)).toEqual(['claims-over-1000', 'w2-no-review']);
    expect(openModals()).toHaveLength(0);
  });

  it('keeps a routing rule when its deletion is cancelled', async () => {
    const onChange = await renderTab();

    pick(2, 'delete');
    fireEvent.click(within(openModal().getElement()).getByRole('button', { name: 'Cancel' }));

    expect(onChange).not.toHaveBeenCalled();
    expect(openModals()).toHaveLength(0);
  });

  it('duplicates a routing rule right after it, under a new id', async () => {
    const onChange = await renderTab();

    pick(1, 'duplicate');

    expect(lastRules(onChange).map((rule) => rule.id)).toEqual([
      'claims-over-1000',
      'claims-over-1000-copy',
      'builtin-confidence-alerts',
      'w2-no-review',
    ]);
    expect(lastRules(onChange)[1]).toEqual({ ...CLAIMS, id: 'claims-over-1000-copy' });
  });

  it('numbers a second copy, and does not copy a built-in routing rule as a built-in one', async () => {
    const onChange = await renderTab({ routing: section([CLAIMS, { ...CLAIMS, id: 'claims-over-1000-copy' }, ALERTS]) });

    pick(1, 'duplicate');
    expect(lastRules(onChange)[1].id).toBe('claims-over-1000-copy-2');

    pick(3, 'duplicate');
    expect(lastRules(onChange)[3]).toEqual({ ...ALERTS, id: 'confidence-alerts-copy' });
  });

  it('gives the copy of a routing rule with the longest id an id the server accepts', async () => {
    const longest = 'x'.repeat(64);
    const onChange = await renderTab({ routing: section([{ ...CLAIMS, id: longest }]) });

    pick(1, 'duplicate');

    const copy = lastRules(onChange)[1].id;
    expect(copy).toBe(`${'x'.repeat(59)}-copy`);
    expect(RULE_ID_PATTERN.test(copy)).toBe(true);
  });

  it(`does not duplicate, or add, past ${MAX_RULES} routing rules`, async () => {
    const full = Array.from({ length: MAX_RULES }, (_unused, index) => ({ ...QUIET, id: `rule-${index}` }));
    await renderTab({ routing: section(full) });

    act(() => menu(1).openDropdown());
    expect(itemDisabled(1, 'duplicate')).toBe(true);
    expect(disabledItem(screen.getByRole('button', { name: 'Add routing rule' }))).toBe(true);
  });

  it('disables and enables a routing rule', async () => {
    const onChange = await renderTab();

    act(() => table().findBodyCell(1, column('Enabled'))!.findToggle()!.findNativeInput().click());
    expect(lastRules(onChange)[0].enabled).toBe(false);

    act(() => table().findBodyCell(3, column('Enabled'))!.findToggle()!.findNativeInput().click());
    expect(lastRules(onChange)[2].enabled).toBe(true);
  });

  it('walks the five items of a routing rule menu from the keyboard, and moves the routing rule down', async () => {
    const onChange = await renderTab();
    const highlighted = (): string => menu(1).findHighlightedItem()?.getElement().textContent?.trim() ?? '';
    const press = (key: number): void => act(() => menu(1).findOpenDropdown()!.keydown(key));

    act(() => menu(1).findNativeButton().keydown(KeyCode.enter));
    const walked = [highlighted()];
    for (let presses = 0; presses < 4; presses += 1) {
      press(KeyCode.down);
      walked.push(highlighted());
    }
    expect(walked).toEqual(['Edit', 'Duplicate', 'Move up', 'Move down', 'Delete']);

    press(KeyCode.up);
    expect(highlighted()).toBe('Move down');
    press(KeyCode.enter);

    expect(lastRules(onChange).map((rule) => rule.id)).toEqual(['builtin-confidence-alerts', 'claims-over-1000', 'w2-no-review']);
  });

  it('moves a routing rule up, and offers no move past either end', async () => {
    const onChange = await renderTab();

    pick(3, 'up');
    expect(lastRules(onChange).map((rule) => rule.id)).toEqual(['claims-over-1000', 'w2-no-review', 'builtin-confidence-alerts']);

    act(() => menu(1).openDropdown());
    expect([itemDisabled(1, 'up'), itemDisabled(1, 'down')]).toEqual([true, false]);
    act(() => menu(1).findNativeButton().click());
    act(() => menu(3).openDropdown());
    expect([itemDisabled(3, 'up'), itemDisabled(3, 'down')]).toEqual([false, true]);
  });

  it('has a handle on each row that the keyboard can pick up', async () => {
    await renderTab();

    const handle = screen.getByLabelText(/Reorder routing rule claims-over-1000/);
    expect(handle.getAttribute('tabindex')).toBe('0');
    expect(handle.getAttribute('role')).toBe('button');
    expect(screen.getAllByLabelText(/Reorder routing rule /)).toHaveLength(3);
  });

  it('changes the placement limit only to a whole number from 1 to 10', async () => {
    const onChange = await renderTab();
    const input = within(tab().getElement()).getByRole('spinbutton');

    act(() => {
      fireEvent.change(input, { target: { value: '5' } });
    });
    expect(onChange.mock.calls.at(-1)![0].max_consecutive_placements).toBe(5);
    const calls = onChange.mock.calls.length;

    for (const value of ['0', '11', '2.5', '']) {
      act(() => {
        fireEvent.change(input, { target: { value } });
      });
    }
    expect(onChange.mock.calls.length).toBe(calls);
    expect(screen.getByText('Enter a whole number from 1 to 10.')).toBeTruthy();
  });
});

describe('the default outcome', () => {
  const TO_CLAIMS = { default_outcome: { actions: toQueue('claims'), reason: 'No routing rule matched; check the class.' } };
  const reason = () => tab().findContent().findTextarea();

  it('is changed to the Default queue', async () => {
    const onChange = await renderTab();

    act(() => defaultOutcome().findInputByValue('queue')!.click());

    expect(tableOf(onChange.mock.calls.at(-1)![0]).default_outcome).toEqual({ actions: toQueue('default'), reason: '' });
    expect(tab().findContent().findSelect()).toBeNull();
  });

  it('shows its queue by name, and changes it, keeping the reason', async () => {
    const onChange = await renderTab({ routing: section([ALERTS], TO_CLAIMS) });
    const select = tab().findContent().findSelect()!;

    expect(select.findTrigger().getElement().textContent).toContain('Claims team');
    act(() => select.openDropdown());
    act(() => select.selectOptionByValue('default'));

    expect(tableOf(onChange.mock.calls.at(-1)![0]).default_outcome).toEqual({
      actions: toQueue('default'),
      reason: 'No routing rule matched; check the class.',
    });
  });

  it('is changed back to no review, which drops the queue and the reason', async () => {
    const onChange = await renderTab({ routing: section([ALERTS], TO_CLAIMS) });

    act(() => defaultOutcome().findInputByValue('no_review')!.click());

    expect(tableOf(onChange.mock.calls.at(-1)![0]).default_outcome).toEqual({ actions: [], reason: '' });
  });

  it('has a reason when it sends to a queue, and none when it does not', async () => {
    const onChange = await renderTab({ routing: section([ALERTS], TO_CLAIMS) });

    expect(reason()!.getTextareaValue()).toBe('No routing rule matched; check the class.');
    act(() => reason()!.setTextareaValue('Nothing matched.'));

    expect(tableOf(onChange.mock.calls.at(-1)![0]).default_outcome).toEqual({ actions: toQueue('claims'), reason: 'Nothing matched.' });
  });

  it('has no reason field while it is no review', async () => {
    await renderTab();

    expect(reason()).toBeNull();
  });

  it('offers the values of the document for its reason, and no condition', async () => {
    await renderTab({ routing: section([ALERTS], TO_CLAIMS) });
    const insert = reasonField()!.findSecondaryControl()!.findButtonDropdown()!;

    act(() => insert.openDropdown());

    const offered = insert.findItems().map((item) => item.getElement().getAttribute('data-testid'));
    expect(offered[0]).toBe('{document_class}');
    expect(offered.at(-1)).toBe('{confidence_threshold}');
    expect(offered.some((placeholder) => placeholder?.startsWith('{condition_'))).toBe(false);
  });
});

describe('the More actions menu', () => {
  it('holds the DMN import and exports and the restoring of the built-in routing rules', async () => {
    await renderTab();
    act(() => moreActions().openDropdown());

    expect(moreActions().findNativeButton().getElement().textContent).toContain('More actions');
    expect(
      moreActions()
        .findItems()
        .map((item) => item.getElement().getAttribute('data-testid')),
    ).toEqual(['import', 'export-1.5', 'export-1.3', 'restore']);
    expect(moreActions().findItemById('import')!.getElement().textContent).toContain('Import DMN');
    expect(moreActions().findItemById('export-1.5')!.getElement().textContent).toContain('Export DMN 1.5');
    expect(moreActions().findItemById('export-1.3')!.getElement().textContent).toContain('Export DMN 1.3');
    expect(moreActions().findItemById('restore')!.getElement().textContent).toContain('Restore built-in routing rules');
  });

  it('leaves the header with one button beside it: Add routing rule', async () => {
    await renderTab();

    expect(
      tab()
        .findHeader()!
        .findAllButtons()
        .map((found) => found.getElement().textContent)
        .filter((text) => text !== 'More actions'),
    ).toEqual(['Add routing rule']);
  });

  it('restores the built-in routing rules the table does not have, at the end', async () => {
    const onChange = await renderTab();

    pickAction('restore');

    expect(lastRules(onChange).map((rule) => rule.id)).toEqual([
      'claims-over-1000',
      'builtin-confidence-alerts',
      'w2-no-review',
      'builtin-extraction-failed',
      'builtin-missing-required',
    ]);
    expect(lastRules(onChange).slice(3)).toEqual([FAILED, REQUIRED]);
  });

  it('does not offer to restore them when the table has them all', async () => {
    await renderTab({ routing: section([FAILED, ALERTS, REQUIRED]) });

    act(() => moreActions().openDropdown());

    expect(disabledItem(moreActions().findItemById('restore')!.getElement())).toBe(true);
  });

  it.each([
    ['export-1.5', '1.5'],
    ['export-1.3', '1.3'],
  ])('exports the table as edited through %s', async (item, version) => {
    convert.mockResolvedValue({ data: { convertRoutingDmn: { xml: '<definitions/>', routing: null } } });
    await renderTab();

    pickAction(item);
    await settle();

    const { variables } = convert.mock.calls[0][0] as { variables: { direction: string; dmnVersion: string; content: string } };
    expect(variables).toMatchObject({ direction: 'to_dmn', dmnVersion: version });
    expect(tableOf(readRouting(JSON.parse(variables.content))).rules).toEqual([CLAIMS, ALERTS, QUIET]);
    expect(exportToTextFile).toHaveBeenCalledWith('<definitions/>', 'routing-claims', 'dmn', 'application/xml');
  });

  it('shows why an export failed, above the table', async () => {
    convert.mockRejectedValue(new Error('INVALID_ROUTING: the content is not a routing section'));
    await renderTab();

    pickAction('export-1.5');
    await settle();

    expect(within(tab().findContent().getElement()).getByText('The table could not be exported')).toBeTruthy();
    expect(screen.getByText('INVALID_ROUTING: the content is not a routing section')).toBeTruthy();
  });

  it('opens the file picker for an import, and replaces the table with what the file holds, once confirmed', async () => {
    const imported = section([QUIET]);
    convert.mockResolvedValue({ data: { convertRoutingDmn: { routing: JSON.stringify(imported), xml: null } } });
    const onChange = await renderTab();
    const click = vi.spyOn(fileInput(), 'click').mockImplementation(() => {});
    const file = new File(['<definitions/>'], 'routing.dmn', { type: 'application/xml' });

    pickAction('import');
    expect(click).toHaveBeenCalledTimes(1);
    await act(async () => {
      fireEvent.change(fileInput(), { target: { files: [file] } });
    });
    await waitFor(() => expect(openModals()).toHaveLength(1));
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(within(openModals()[0].getElement()).getByRole('button', { name: /Replace/ }));

    expect(lastRules(onChange).map((rule) => rule.id)).toEqual(['w2-no-review']);
    expect(readRouting(onChange.mock.calls.at(-1)![0]).max_consecutive_placements).toBe(3);
  });
});

describe('an empty table', () => {
  it('says every document gets the default outcome and offers to restore the built-in routing rules', async () => {
    const onChange = await renderTab({ routing: section([]) });

    const empty = table().findEmptySlot()!.getElement();
    expect(within(empty).getByText('No routing rules')).toBeTruthy();
    expect(within(empty).getByText('Every document gets the default outcome.')).toBeTruthy();
    fireEvent.click(within(empty).getByRole('button', { name: 'Restore built-in routing rules' }));

    expect(lastRules(onChange).map((rule) => rule.id)).toEqual([
      'builtin-extraction-failed',
      'builtin-confidence-alerts',
      'builtin-missing-required',
    ]);
  });

  it('reads a profile with no routing section as an empty table', async () => {
    await renderTab({ routing: undefined });

    expect(table().findRows()).toHaveLength(0);
    expect(tab().findHeader()!.findHeader()!.findCounter()!.getElement().textContent?.trim()).toBe('(0)');
  });
});

describe('another role', () => {
  it('sees the routing rules and a notice, and no way to change them', async () => {
    const onChange = await renderTab({ readOnly: true });

    expect(screen.getByText('Only Admins can change routing rules.')).toBeTruthy();
    expect(ids()).toEqual(['claims-over-1000', 'builtin-confidence-alerts', 'w2-no-review']);
    expect(screen.queryByRole('button', { name: 'Add routing rule' })).toBeNull();
    expect(tab().findHeader()!.findButtonDropdown()).toBeNull();
    expect(screen.queryByLabelText('DMN file to import')).toBeNull();
    expect(header()).toEqual(['Order', 'Id', 'Label', 'Conditions', 'Outcome', 'Enabled']);
    expect(screen.queryAllByLabelText(/Reorder routing rule /)).toHaveLength(0);
    expect(wrapper().findExpandableSection()).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('cannot switch a routing rule on or off, or change the default outcome or the limit', async () => {
    await renderTab({ readOnly: true });
    const onTab = within(tab().getElement());

    expect(table().findBodyCell(1, column('Enabled'))!.findToggle()!.findNativeInput().getElement().disabled).toBe(true);
    expect((onTab.getByRole('radio', { name: 'Send to a review queue' }) as HTMLInputElement).disabled).toBe(true);
    expect((onTab.getByRole('spinbutton') as HTMLInputElement).disabled).toBe(true);
  });

  it('reads the reason of a default outcome that sends to a queue, and is offered no value to insert', async () => {
    await renderTab({
      readOnly: true,
      routing: section([ALERTS], { default_outcome: { actions: toQueue('claims'), reason: 'Nothing matched.' } }),
    });

    const reason = tab().findContent().findTextarea()!;
    expect(reason.getTextareaValue()).toBe('Nothing matched.');
    expect(reason.findNativeTextarea().getElement().readOnly).toBe(true);
    expect(reasonField()!.findSecondaryControl()).toBeNull();
  });

  it('sees queue ids, and the queue list is not asked for', async () => {
    await renderTab({ readOnly: true });

    expect(cellText(1, 'Outcome')).toBe('Review queue: claims');
    expect(listReviewQueues).not.toHaveBeenCalled();
  });

  it('opens a routing rule through its label, to read it and nothing more', async () => {
    const onChange = await renderTab({ readOnly: true });

    act(() => labelLink(1).click());

    const modal = openModal();
    expect(modal.findHeader().getElement().textContent).toContain('Routing rule claims-over-1000');
    expect(modal.findHeader().getElement().textContent).not.toContain('Edit');
    expect(modal.findContent().findAll('[data-testid="routing-condition"]')).toHaveLength(2);
    expect(modal.findContent().findTextarea()!.getTextareaValue()).toBe(CLAIMS.reason);
    expect(
      modal
        .findFooter()!
        .findAllButtons()
        .map((found) => found.getElement().textContent),
    ).toEqual(['Close']);

    fireEvent.click(within(modal.getElement()).getByRole('button', { name: 'Close' }));
    expect(openModals()).toHaveLength(0);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('is offered nothing in an empty table', async () => {
    await renderTab({ readOnly: true, routing: section([]) });

    const empty = table().findEmptySlot()!.getElement();
    expect(within(empty).getByText('No routing rules')).toBeTruthy();
    expect(within(empty).queryByRole('button')).toBeNull();
  });

  it('sees what the server refused, and no button to edit the routing rule', async () => {
    const refusal = "Routing table 'review-routing', rule 'claims-over-1000': the queue 'claims' does not exist";
    await renderTab({ readOnly: true, serverError: refusal });

    expect(screen.getByText(refusal)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Edit routing rule/ })).toBeNull();
  });

  it('gets no warning about queues it cannot list', async () => {
    await renderTab({ readOnly: true, routing: section([{ ...CLAIMS, actions: toQueue('gone') }]) });

    expect(screen.queryByText('Queues that do not exist')).toBeNull();
    expect(screen.queryByText(/The Default review queue is off/)).toBeNull();
  });
});

describe('when routing rules are not evaluated', () => {
  const ROUTING_OFF =
    'Review Routing is off for this profile, so no document is held for review and these routing rules are not evaluated.';
  const DEFAULT_OFF =
    /The Default review queue is off, so routing rules are not evaluated and documents use the document-page review flow\./;

  it('says Review Routing is off for the profile, and opens Processing Settings', async () => {
    await renderTab({ reviewRoutingEnabled: false });

    expect(screen.getByText(ROUTING_OFF)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open Processing Settings' }));

    expect(onOpenProcessingSettings).toHaveBeenCalledTimes(1);
  });

  it('says so to another role too', async () => {
    await renderTab({ reviewRoutingEnabled: false, readOnly: true });

    expect(screen.getByText(ROUTING_OFF)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open Processing Settings' })).toBeTruthy();
  });

  it('says nothing while Review Routing is on and the Default queue is on', async () => {
    await renderTab();

    expect(screen.queryByText(ROUTING_OFF)).toBeNull();
    expect(screen.queryByText(DEFAULT_OFF)).toBeNull();
    expect(wrapper().findAllAlerts()).toHaveLength(0);
  });

  it('says the Default review queue is off, with a link to Review Management', async () => {
    listReviewQueues.mockResolvedValue([{ ...QUEUES[0], enabled: false }, QUEUES[1]]);
    await renderTab();

    expect(screen.getByText(DEFAULT_OFF)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Review Management' }).getAttribute('href')).toBe('#/review-queues');
  });

  it('says nothing about another queue that is off', async () => {
    listReviewQueues.mockResolvedValue([QUEUES[0], { ...QUEUES[1], enabled: false }]);
    await renderTab();

    expect(screen.queryByText(DEFAULT_OFF)).toBeNull();
  });

  it('shows both warnings when both are off', async () => {
    listReviewQueues.mockResolvedValue([{ ...QUEUES[0], enabled: false }]);
    await renderTab({ reviewRoutingEnabled: false });

    expect(screen.getByText(ROUTING_OFF)).toBeTruthy();
    expect(screen.getByText(DEFAULT_OFF)).toBeTruthy();
  });
});

describe('a routing rule that reads a policy rule result', () => {
  const BADGE = 'Tests a policy rule result; policy rule validation is off';
  const IN_REASON = { ...CLAIMS, id: 'in-reason', reason: 'Failed policy rules: {failed_policy_rule_count:prior_auth}.' };

  it('is marked while policy rule validation is off, whether a condition or the reason reads it', async () => {
    await renderTab({ routing: section([POLICY, IN_REASON, CLAIMS]), policyRuleValidationEnabled: false });

    const badges = [1, 2, 3].map((row) => table().findBodyCell(row, column('Conditions'))!.findStatusIndicator());
    expect(badges.map((badge) => badge?.getElement().textContent ?? null)).toEqual([BADGE, BADGE, null]);
    // The conditions are still read under the mark.
    expect(cellText(1, 'Conditions')).toContain('Policy rule verdict prior_auth.coverage "fail"');
  });

  it('is not marked while policy rule validation is on', async () => {
    await renderTab({ routing: section([POLICY, IN_REASON, CLAIMS]) });

    expect(screen.queryByText(BADGE)).toBeNull();
  });
});

describe('the other warnings', () => {
  it('name a routing rule whose queue is not in the queue list', async () => {
    await renderTab({ routing: section([{ ...CLAIMS, actions: toQueue('gone') }, ALERTS]) });

    expect(screen.getByText('Queues that do not exist')).toBeTruthy();
    expect(screen.getByText(/claims-over-1000 sends documents to gone, which is not a review queue/)).toBeTruthy();
    expect(cellText(1, 'Outcome')).toBe('Review queue: gone');
  });

  it('name a default outcome whose queue is not in the queue list', async () => {
    await renderTab({ routing: section([ALERTS], { default_outcome: { actions: toQueue('gone'), reason: '' } }) });

    expect(screen.getByText('The default outcome sends documents to gone, which is not a review queue.')).toBeTruthy();
  });

  it('do not name a routing rule that is not reviewed', async () => {
    await renderTab({ routing: section([QUIET]) });

    expect(screen.queryByText('Queues that do not exist')).toBeNull();
  });

  it('do not appear when every queue exists', async () => {
    await renderTab();

    expect(screen.queryByText('Queues that do not exist')).toBeNull();
    expect(screen.queryByText('Routing rules that can never match')).toBeNull();
  });

  it('name a routing rule that can never match and the one that wins', async () => {
    await renderTab({ routing: section([ALERTS, { ...CLAIMS, conditions: [...ALERTS.conditions, ...CLAIMS.conditions] }]) });

    expect(screen.getByText('Routing rules that can never match')).toBeTruthy();
    expect(
      screen.getByText(/claims-over-1000 never matches: every document it would match is taken by builtin-confidence-alerts/),
    ).toBeTruthy();
  });

  it('say so when the queue list fails to load, and then check no queue', async () => {
    listReviewQueues.mockRejectedValue(new Error('boom'));
    await renderTab({ routing: section([{ ...CLAIMS, actions: toQueue('gone') }]) });

    await waitFor(() => expect(screen.getByText(/The review queues could not be loaded/)).toBeTruthy());
    expect(screen.queryByText('Queues that do not exist')).toBeNull();
    expect(screen.queryByText(/The Default review queue is off/)).toBeNull();
    expect(cellText(1, 'Outcome')).toBe('Review queue: gone');
  });
});

describe('what the server refused', () => {
  const REFUSAL =
    "Routing table 'review-routing', rule 'claims-over-1000', input 'field_value Invoice:Total': 'Invoice' is not a class of this profile";

  it('is shown above the table, and opens the routing rule it names with the message in the dialog', async () => {
    const onChange = await renderTab({ serverError: REFUSAL });

    expect(screen.getByText('The configuration was not saved: the routing table was refused')).toBeTruthy();
    expect(screen.getAllByText(REFUSAL)).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'Edit routing rule claims-over-1000' }));

    const modal = openModal();
    expect(modal.findHeader().getElement().textContent).toContain('claims-over-1000');
    expect(within(modal.findContent().getElement()).getByText(REFUSAL)).toBeTruthy();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('offers no routing rule to open when the message names none, or one the table does not have', async () => {
    await renderTab({ serverError: "Routing table 'review-routing': the table has 101 rules; the limit is 100" });

    expect(screen.getByText(/the table has 101 rules/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Edit routing rule/ })).toBeNull();
  });

  it('does not show the message in the dialog of a routing rule opened from its menu', async () => {
    await renderTab({ serverError: REFUSAL });

    pick(2, 'edit');

    expect(openModal().findHeader().getElement().textContent).toContain('builtin-confidence-alerts');
    expect(within(openModal().findContent().getElement()).queryByText(REFUSAL)).toBeNull();
  });
});
