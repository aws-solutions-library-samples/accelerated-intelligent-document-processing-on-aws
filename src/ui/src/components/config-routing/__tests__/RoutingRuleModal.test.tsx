// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The routing rule dialog: its three groups, what it refuses before a routing rule is
 * kept, the controls a condition shows for each input, how a value is entered (guided
 * controls, or an expression) and what is stored, the outcome and its reason, what
 * another role sees, and how a refusal from the server is shown.
 */

import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { describe, expect, it, vi } from 'vitest';

import RoutingRuleModal from '../RoutingRuleModal';
import { MAX_CONDITIONS, MAX_REASON_BYTES, type RoutingCondition, type RoutingRule } from '../routing-types';

const CLASSES = [
  {
    'x-aws-idp-document-type': 'Invoice',
    properties: {
      Total: { type: 'number' },
      Currency: { type: 'string' },
      Paid: { type: 'boolean' },
      DueDate: { type: 'string', format: 'date' },
    },
  },
  { 'x-aws-idp-document-type': 'W2', properties: { Wages: { type: 'number' } } },
];
const POLICY_CLASSES = [{ 'x-aws-idp-policy-type': 'prior_auth', rule_properties: { coverage: {}, necessity: {} } }];
const QUEUES = [
  { id: 'default', name: 'Default' },
  { id: 'claims', name: 'Claims' },
];

const RULE: RoutingRule = {
  id: 'claims-over-1000',
  label: 'Claims over 1000',
  enabled: true,
  conditions: [{ input: 'field_value', path: 'Invoice:Total', aggregate: 'any', test: '> 1000' }],
  actions: [{ type: 'review_queue', queue: 'claims' }],
  reason: 'The total {field_value:Invoice:Total} is over {condition_1.value}.',
};
const condition = (input: string, test: string, path = '', aggregate = 'any'): RoutingCondition => ({ input, path, aggregate, test });
const ruleWith = (...conditions: RoutingCondition[]): RoutingRule => ({ ...RULE, conditions, reason: '' });

type ModalProps = React.ComponentProps<typeof RoutingRuleModal>;

const dialog = (props: Partial<ModalProps>, onSave: ModalProps['onSave'], onDismiss: ModalProps['onDismiss']) => (
  <IntlProvider locale="en">
    <RoutingRuleModal
      visible
      rule={null}
      otherIds={['taken']}
      queues={QUEUES}
      classes={CLASSES}
      policyClasses={POLICY_CLASSES}
      onSave={onSave}
      onDismiss={onDismiss}
      {...props}
    />
  </IntlProvider>
);

const renderModal = (props: Partial<ModalProps> = {}) => {
  const onSave = vi.fn();
  const onDismiss = vi.fn();
  const { rerender } = render(dialog(props, onSave, onDismiss));
  return { onSave, onDismiss, reopen: (next: Partial<ModalProps>) => rerender(dialog(next, onSave, onDismiss)) };
};

const modal = () => createWrapper(document.body).findModal()!;
const save = (): void => {
  fireEvent.click(screen.getByRole('button', { name: 'Save routing rule' }));
};
const saved = (onSave: ReturnType<typeof vi.fn>): RoutingRule => onSave.mock.calls.at(-1)![0] as RoutingRule;
const savedTests = (onSave: ReturnType<typeof vi.fn>): string[] => saved(onSave).conditions.map((entry) => entry.test);
const idInput = () => modal().findContent().findAll('input')[0];
const setId = (value: string): void => {
  act(() => {
    fireEvent.change(idInput().getElement(), { target: { value } });
  });
};
// The groups of the dialog are the containers with a heading of their own; the box of a
// condition is a container too, titled with its number.
const headingOf = (
  container: ReturnType<ReturnType<typeof modal>['findContent']>['findAllContainers'] extends () => (infer C)[] ? C : never,
) => container.findHeader()?.findHeader()?.findHeadingText().getElement().textContent;
const groups = () =>
  modal()
    .findContent()
    .findAllContainers()
    .filter((container) => headingOf(container) !== undefined);
const group = (heading: string) => groups().find((container) => headingOf(container) === heading)!;
const isDisabled = (element: Element): boolean =>
  (element as HTMLButtonElement).disabled ||
  element.matches('[aria-disabled="true"]') ||
  element.querySelector('[aria-disabled="true"]') !== null;

// ---- conditions: each is a box of its own; its first line holds one labelled field per control its input uses
const conditionBoxes = () => modal().findContent().findAll('[data-testid="routing-condition"]');
const conditionBox = (row: number) => conditionBoxes()[row - 1];
const addButton = () => modal().findContent().findButton('[data-testid="routing-condition-add"]')!;
const addCondition = (): void => {
  act(() => addButton().click());
};
const conditionFields = (row: number) => conditionBox(row).find('[data-testid="routing-condition-input"]')!.findAllFormFields();
const conditionLabels = (row: number): string[] => conditionFields(row).map((field) => field.findLabel()?.getElement().textContent ?? '');
const selectIn = (row: number, label: string) =>
  conditionFields(row)
    .find((field) => field.findLabel()?.getElement().textContent === label)!
    .findControl()!
    .findSelect()!;
const choose = (row: number, label: string, value: string): void => {
  const select = selectIn(row, label);
  act(() => select.openDropdown());
  act(() => select.selectOptionByValue(value));
};
const optionValues = (row: number, label: string): string[] => {
  const select = selectIn(row, label);
  act(() => select.openDropdown());
  const values = select
    .findDropdown()
    .findOptions()
    .map((option) => option.getElement().getAttribute('data-value') ?? option.getElement().textContent ?? '');
  act(() => select.closeDropdown());
  return values;
};
const rowMenu = (row: number) => conditionBox(row).findButtonDropdown()!;
const pick = (row: number, item: string): void => {
  act(() => rowMenu(row).openDropdown());
  act(() => rowMenu(row).findItemById(item)!.click());
};

// ---- the value of a condition: its second line
const value = (row: number) => conditionBox(row).find('[data-testid="routing-condition-value"]')!;
const link = (row: number, name: string): HTMLElement | null => within(value(row).getElement()).queryByRole('button', { name });
const follow = (row: number, name: string): void => {
  fireEvent.click(link(row, name)!);
};
const guided = (row: number): boolean => link(row, 'Edit as expression') !== null;
const expression = (row: number): HTMLInputElement => value(row).findInput()!.findNativeInput().getElement();
const type = (input: Element, text: string): void => {
  act(() => {
    fireEvent.change(input, { target: { value: text } });
  });
};
const comparison = (row: number) => value(row).findSelect()!;
const compare = (row: number, by: string): void => {
  act(() => comparison(row).openDropdown());
  act(() => comparison(row).selectOptionByValue(by));
};
const numbers = (row: number): HTMLInputElement[] =>
  value(row)
    .findAll('input[type="number"]')
    .map((input) => input.getElement() as HTMLInputElement);
const values = (row: number) => value(row).findMultiselect()!;
const chooseValues = (row: number, ...chosen: string[]): void => {
  act(() => values(row).openDropdown());
  chosen.forEach((entry) => act(() => values(row).selectOptionByValue(entry)));
};
const tokens = (row: number): string[] =>
  value(row)
    .findTokenGroup()!
    .findTokens()
    .map((token) => token.findLabel().getElement().textContent ?? '');
const typed = (row: number): HTMLInputElement => within(value(row).getElement()).getByPlaceholderText('Type a value and press Enter');
const enter = (row: number, text: string): void => {
  type(typed(row), text);
  act(() => {
    fireEvent.keyDown(typed(row), { key: 'Enter', keyCode: 13 });
  });
};
const reason = () => modal().findContent().findTextarea();

describe('the dialog', () => {
  it('is the large size, in three labelled groups', () => {
    renderModal();

    expect(modal().find('[class*="awsui_large_"]')).not.toBeNull();
    expect(modal().find('[class*="x-large"]')).toBeNull();
    expect(groups().map(headingOf)).toEqual(['Routing rule', 'Conditions', 'Outcome and reason']);
  });

  it('has the id and the label side by side, each with a label and one line of help, and a labelled Enabled switch', () => {
    renderModal();

    const columns = group('Routing rule').findContent().findColumnLayout()!.getElement();
    const fields = group('Routing rule')
      .findContent()
      .findAllFormFields()
      .filter((field) => columns.contains(field.getElement()));
    expect(fields.map((field) => field.findLabel()?.getElement().textContent)).toEqual(['Routing rule id', 'Label']);
    expect(fields.every((field) => !!field.findDescription()?.getElement().textContent)).toBe(true);

    const enabled = group('Routing rule')
      .findContent()
      .findAllFormFields()
      .find((field) => field.findLabel()?.getElement().textContent === 'Enabled')!;
    const toggle = enabled.findControl()!.findToggle()!;
    expect(toggle.findLabel().getElement().textContent).toBe('Enabled');
    act(() => toggle.findNativeInput().click());
    expect(toggle.findLabel().getElement().textContent).toBe('Disabled');
  });

  it('counts the conditions against the most a routing rule can have', () => {
    renderModal();
    const counter = (): string => group('Conditions').findHeader()!.findHeader()!.findCounter()!.getElement().textContent?.trim() ?? '';

    expect(counter()).toBe(`(0/${MAX_CONDITIONS})`);
    addCondition();
    addCondition();
    expect(counter()).toBe(`(2/${MAX_CONDITIONS})`);
  });

  it('puts the queue right under the outcome, then the reason', () => {
    renderModal({ rule: RULE });

    const labels = group('Outcome and reason')
      .findContent()
      .findAllFormFields()
      .map((field) => field.findLabel()?.getElement().textContent);
    expect(labels.slice(0, 3)).toEqual(['When the routing rule matches', 'Review queue', 'Reason for the reviewer (optional)']);
  });
});

describe('what the dialog refuses', () => {
  it('needs an id', () => {
    const { onSave } = renderModal();

    save();

    expect(screen.getByText('Enter an id.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('shows no error before a save is tried', () => {
    renderModal();

    expect(screen.queryByText('Enter an id.')).toBeNull();
  });

  it.each(['Bad Id', '-starts-with-a-hyphen', 'x'.repeat(65)])('refuses the id %s', (id) => {
    const { onSave } = renderModal();
    setId(id);

    save();

    expect(screen.getByText(/Use 1 to 64 lower-case letters/)).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('refuses an id another routing rule has', () => {
    const { onSave } = renderModal();
    setId('taken');

    save();

    expect(screen.getByText('Another routing rule has this id.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('refuses a condition with no input, and one with no value', () => {
    const { onSave } = renderModal();
    setId('new-rule');
    addCondition();

    save();
    expect(screen.getByText('Choose an input.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();

    choose(1, 'Input', 'page_count');
    save();
    expect(screen.getByText('Enter a value.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('refuses an input that needs a field and has none', () => {
    const { onSave } = renderModal();
    setId('new-rule');
    addCondition();
    choose(1, 'Input', 'field_value');

    save();

    expect(screen.getByText('Choose what this input reads.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('refuses a reason over the size limit, and takes one at the limit', () => {
    const { onSave } = renderModal();
    setId('new-rule');

    act(() => reason()!.setTextareaValue('x'.repeat(MAX_REASON_BYTES + 1)));
    save();
    expect(screen.getByText(`The reason is longer than ${MAX_REASON_BYTES} bytes.`)).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();

    act(() => reason()!.setTextareaValue('x'.repeat(MAX_REASON_BYTES)));
    save();
    expect(saved(onSave).reason).toHaveLength(MAX_REASON_BYTES);
  });

  it('keeps a valid new routing rule, trimmed, with the Default queue as its outcome and no reason', () => {
    const { onSave } = renderModal();
    setId('  new-rule ');

    save();

    expect(onSave).toHaveBeenCalledWith({
      id: 'new-rule',
      label: '',
      enabled: true,
      conditions: [],
      actions: [{ type: 'review_queue', queue: 'default' }],
      reason: '',
    });
  });
});

describe('the conditions', () => {
  it('offers every input of the catalogue', () => {
    renderModal();
    addCondition();

    expect(optionValues(1, 'Input')).toEqual(
      expect.arrayContaining(['document_class', 'class_present', 'field_value', 'policy_rule_verdict', 'failed_policy_rule_count']),
    );
  });

  it("offers the classes' fields for a field value", () => {
    renderModal();
    addCondition();
    choose(1, 'Input', 'field_value');

    expect(optionValues(1, 'Field')).toEqual(expect.arrayContaining(['Invoice:Total', 'Invoice:Currency', 'W2:Wages']));
  });

  it('offers the policy rule ids for a policy rule verdict', () => {
    renderModal();
    addCondition();
    choose(1, 'Input', 'policy_rule_verdict');

    expect(optionValues(1, 'Policy rule')).toEqual(['prior_auth.coverage', 'prior_auth.necessity']);
  });

  it('offers the policy types, and all of them, for a failed policy rule count', () => {
    renderModal();
    addCondition();
    choose(1, 'Input', 'failed_policy_rule_count');

    expect(optionValues(1, 'Policy type')).toEqual(['', 'prior_auth']);
  });

  it("offers the profile's classes for a class that is present or missing", () => {
    renderModal();
    addCondition();
    choose(1, 'Input', 'class_present');

    expect(optionValues(1, 'Class')).toEqual(['Invoice', 'W2']);
  });

  it.each([
    ['page_count', ['Input']],
    ['document_class', ['Input']],
    ['section_classes', ['Input', 'Match']],
    ['class_present', ['Input', 'Class']],
    ['field_value', ['Input', 'Field', 'Match']],
    ['policy_rule_verdict', ['Input', 'Policy rule']],
    ['failed_policy_rule_count', ['Input', 'Policy type']],
  ])('shows only the controls the input %s uses, each with its label', (input, labels) => {
    renderModal();
    addCondition();
    expect(conditionLabels(1)).toEqual(['Input']);

    choose(1, 'Input', input);

    expect(conditionLabels(1)).toEqual(labels);
    // A control an input does not use is left out, not shown disabled.
    expect(conditionBox(1).find('[data-testid="routing-condition-input"]')!.findAll('input:disabled, button:disabled')).toHaveLength(0);
  });

  it('puts each condition in a box titled with its number, the menu at its top', () => {
    renderModal({ rule: ruleWith(condition('page_count', '> 3'), condition('section_count', '> 1')) });

    expect(conditionBoxes()).toHaveLength(2);
    expect(conditionBoxes().map((box) => box.find('h4')!.getElement().textContent)).toEqual(['Condition 1', 'Condition 2']);
    // The menu comes before the fields of its condition: it is in the head of the box.
    const menu = rowMenu(1).getElement();
    const firstField = conditionFields(1)[0].getElement();
    expect(menu.compareDocumentPosition(firstField) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // And after the number of the condition, on the same head.
    const title = conditionBox(1).find('h4')!.getElement();
    expect(title.compareDocumentPosition(menu) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('labels every field for what it holds, with no heading over a line', () => {
    renderModal();
    addCondition();
    const labels = (row: number): string[] =>
      conditionBox(row)
        .findAllFormFields()
        .map((field) => field.findLabel()?.getElement().textContent ?? '');

    expect(labels(1)).toEqual(['Input']);
    choose(1, 'Input', 'page_count');
    expect(labels(1)).toEqual(['Input', 'Comparison', 'Number']);
    follow(1, 'Edit as expression');
    expect(labels(1)).toEqual(['Input', 'Expression']);
    choose(1, 'Input', 'reviewed');
    // (The yes or no control also holds the list it turns into on a small screen, with a name of its own.)
    expect(labels(1).slice(0, 2)).toEqual(['Input', 'Value']);
    // Nothing is labelled "Condition": the box has its number, and each field its own label.
    expect(labels(1)).not.toContain('Condition');
  });

  it('asks for an input before a value', () => {
    renderModal();
    addCondition();

    expect(value(1).getElement().textContent).toBe('Choose an input to enter its value.');
    expect(value(1).findInput()).toBeNull();
  });

  it('clears what the input read when the input changes, and keeps the value', () => {
    const { onSave } = renderModal({ rule: RULE });
    choose(1, 'Input', 'page_count');

    save();

    expect(saved(onSave).conditions).toEqual([{ input: 'page_count', path: '', aggregate: 'any', test: '> 1000' }]);
  });

  it('lets a list be read by any value, all values or their number, and nothing else', () => {
    renderModal();
    addCondition();
    choose(1, 'Input', 'section_classes');

    expect(optionValues(1, 'Match')).toEqual(['any', 'all', 'count']);
    choose(1, 'Input', 'page_count');
    expect(conditionLabels(1)).not.toContain('Match');
  });

  it(`takes at most ${MAX_CONDITIONS} conditions`, () => {
    renderModal();
    expect(addButton().isDisabled()).toBe(false);
    for (let added = 0; added < MAX_CONDITIONS; added += 1) addCondition();

    expect(conditionBoxes()).toHaveLength(MAX_CONDITIONS);
    expect(addButton().isDisabled()).toBe(true);
    expect(screen.getByText(`A routing rule has at most ${MAX_CONDITIONS} conditions.`)).toBeTruthy();
  });

  it('says a routing rule with no condition matches every document', () => {
    renderModal();

    expect(modal().findContent().find('[data-testid="routing-conditions-empty"]')!.getElement().textContent).toBe(
      'No conditions. The routing rule matches every document.',
    );
  });
});

describe('the menu of a condition', () => {
  const TWO = ruleWith(condition('page_count', '> 3'), condition('section_count', '> 1'));

  it('offers duplicate, move up, move down and remove', () => {
    renderModal({ rule: TWO });
    act(() => rowMenu(1).openDropdown());

    expect(
      rowMenu(1)
        .findItems()
        .map((item) => item.getElement().textContent?.trim()),
    ).toEqual(['Duplicate', 'Move up', 'Move down', 'Remove']);
  });

  it('removes a condition', () => {
    const { onSave } = renderModal({ rule: RULE });
    pick(1, 'remove');

    save();

    expect(saved(onSave).conditions).toEqual([]);
  });

  it('duplicates a condition right under it', () => {
    const { onSave } = renderModal({ rule: TWO });
    pick(1, 'duplicate');

    save();

    expect(savedTests(onSave)).toEqual(['> 3', '> 3', '> 1']);
    expect(saved(onSave).conditions[1]).toEqual(TWO.conditions[0]);
  });

  it('moves a condition down and up', () => {
    const { onSave } = renderModal({ rule: TWO });

    pick(1, 'down');
    save();
    expect(savedTests(onSave)).toEqual(['> 1', '> 3']);

    pick(2, 'up');
    save();
    expect(savedTests(onSave)).toEqual(['> 3', '> 1']);
  });

  it('offers no move past either end', () => {
    renderModal({ rule: TWO });
    const disabled = (row: number, item: string): boolean => isDisabled(rowMenu(row).findItemById(item)!.getElement());

    act(() => rowMenu(1).openDropdown());
    expect([disabled(1, 'up'), disabled(1, 'down')]).toEqual([true, false]);
    act(() => rowMenu(1).findNativeButton().click());
    act(() => rowMenu(2).openDropdown());
    expect([disabled(2, 'up'), disabled(2, 'down')]).toEqual([false, true]);
  });

  it('leaves the keyboard on the condition that moved, and on Add condition when the last one is removed', () => {
    renderModal({ rule: TWO });

    pick(1, 'down');
    expect(document.activeElement).toBe(rowMenu(2).findNativeButton().getElement());

    pick(2, 'remove');
    expect(document.activeElement).toBe(rowMenu(1).findNativeButton().getElement());
    pick(1, 'remove');
    expect(conditionBoxes()).toHaveLength(0);
    expect(document.activeElement).toBe(addButton().getElement());
  });

  it('does not duplicate past the most conditions', () => {
    renderModal({ rule: ruleWith(...Array.from({ length: MAX_CONDITIONS }, () => condition('page_count', '> 3'))) });

    act(() => rowMenu(1).openDropdown());

    expect(isDisabled(rowMenu(1).findItemById('duplicate')!.getElement())).toBe(true);
  });
});

describe('the value of a condition, with guided controls', () => {
  it('is a comparison and a number for a count', () => {
    const { onSave } = renderModal({ rule: ruleWith() });
    addCondition();
    choose(1, 'Input', 'page_count');

    expect(guided(1)).toBe(true);
    expect(comparison(1).findTrigger().getElement().textContent).toContain('Greater than');
    compare(1, '>=');
    type(numbers(1)[0], '3');
    save();

    expect(savedTests(onSave)).toEqual(['>= 3']);
  });

  it.each([
    ['=', '7'],
    ['!=', '!= 7'],
    ['>', '> 7'],
    ['>=', '>= 7'],
    ['<', '< 7'],
    ['<=', '<= 7'],
  ])('writes the comparison %s as %s', (by, test) => {
    const { onSave } = renderModal({ rule: ruleWith(condition('page_count', '> 7')) });

    compare(1, by);
    save();

    expect(savedTests(onSave)).toEqual([test]);
  });

  it('is two numbers for a range, which is a value only once both are entered', () => {
    const { onSave } = renderModal({ rule: ruleWith(condition('page_count', '> 2')) });

    compare(1, 'between');
    expect(numbers(1).map((input) => input.value)).toEqual(['2', '']);
    save();
    expect(screen.getByText('Enter a value.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();

    type(numbers(1)[1], '9');
    save();
    expect(savedTests(onSave)).toEqual(['[2..9]']);
  });

  it('is yes or no for a yes or no input', () => {
    const { onSave } = renderModal({ rule: ruleWith() });
    addCondition();
    choose(1, 'Input', 'reviewed');
    const control = value(1).findSegmentedControl()!;

    expect(control.findSegments().map((segment) => segment.getElement().textContent)).toEqual(['Yes', 'No']);
    expect(control.findSelectedSegment()).toBeNull();
    act(() => control.findSegmentById('false')!.click());
    save();

    expect(savedTests(onSave)).toEqual(['false']);
  });

  it('is present or missing for a class', () => {
    const { onSave } = renderModal({ rule: ruleWith() });
    addCondition();
    choose(1, 'Input', 'class_present');
    choose(1, 'Class', 'W2');
    const control = value(1).findSegmentedControl()!;

    expect(control.findSegments().map((segment) => segment.getElement().textContent)).toEqual(['Present', 'Missing']);
    act(() => control.findSegmentById('false')!.click());
    save();

    expect(saved(onSave).conditions).toEqual([{ input: 'class_present', path: 'W2', aggregate: 'any', test: 'false' }]);
  });

  it("is a pick list of the profile's classes for a class, one or several, or none of them", () => {
    const { onSave } = renderModal({ rule: ruleWith() });
    addCondition();
    choose(1, 'Input', 'document_class');

    chooseValues(1, 'Invoice', 'W2');
    save();
    expect(savedTests(onSave)).toEqual(['"Invoice","W2"']);

    compare(1, 'not');
    save();
    expect(savedTests(onSave)).toEqual(['not("Invoice","W2")']);
  });

  it.each([
    ['policy_rule_verdict', ['pass', 'fail', 'not_found', 'unknown']],
    ['processing_mode', ['pipeline', 'bda']],
  ])('is a pick list of the values of %s', (input, offered) => {
    renderModal({ rule: ruleWith() });
    addCondition();
    choose(1, 'Input', input);

    act(() => values(1).openDropdown());
    expect(
      values(1)
        .findDropdown()
        .findOptions()
        .map((option) => option.getElement().getAttribute('data-value')),
    ).toEqual(offered);
  });

  it('keeps a chosen value the profile no longer offers', () => {
    const { onSave } = renderModal({ rule: ruleWith(condition('document_class', '"Receipt","W2"')) });

    expect(
      values(1)
        .findTokens()
        .map((token) => token.findLabel().getElement().textContent),
    ).toEqual(['Receipt', 'W2']);
    chooseValues(1, 'Invoice');
    save();

    expect(savedTests(onSave)).toEqual(['"Receipt","W2","Invoice"']);
  });

  it('takes typed values for a text field, entered one at a time', () => {
    const { onSave } = renderModal({ rule: ruleWith() });
    addCondition();
    choose(1, 'Input', 'field_value');
    choose(1, 'Field', 'Invoice:Currency');

    enter(1, 'USD');
    enter(1, ' EUR ');
    enter(1, 'USD');
    expect(tokens(1)).toEqual(['USD', 'EUR']);
    save();

    expect(savedTests(onSave)).toEqual(['"USD","EUR"']);
  });

  it('keeps a value that was typed and not entered when the field is left', () => {
    const { onSave } = renderModal({ rule: ruleWith(condition('field_value', '"USD"', 'Invoice:Currency')) });

    type(typed(1), 'say "hi"');
    act(() => {
      fireEvent.blur(typed(1));
    });
    save();

    expect(savedTests(onSave)).toEqual(['"USD","say \\"hi\\""']);
  });

  it('removes a typed value, and a condition left with none has no value', () => {
    const { onSave } = renderModal({ rule: ruleWith(condition('field_value', '"USD"', 'Invoice:Currency')) });

    act(() => value(1).findTokenGroup()!.findToken(1)!.findDismiss().click());
    save();

    expect(screen.getByText('Enter a value.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();
  });

  it.each([
    ['Invoice:Total', 'number'],
    ['Invoice:Paid', 'yes or no'],
  ])('has the control of the type of the field %s (%s)', (path, kind) => {
    renderModal({ rule: ruleWith() });
    addCondition();
    choose(1, 'Input', 'field_value');
    choose(1, 'Field', path);

    expect(guided(1)).toBe(true);
    expect(numbers(1)).toHaveLength(kind === 'number' ? 1 : 0);
    expect(value(1).findSegmentedControl() !== null).toBe(kind === 'yes or no');
  });

  it('is a number for a list read by its number of values', () => {
    const { onSave } = renderModal({ rule: ruleWith(condition('section_classes', '"Invoice"', '', 'any')) });
    expect(value(1).findMultiselect()).not.toBeNull();

    choose(1, 'Match', 'count');

    // The class is not a number of values: it is shown as the expression it is.
    expect(guided(1)).toBe(false);
    expect(expression(1).value).toBe('"Invoice"');
    type(expression(1), '');
    follow(1, 'Use guided controls');
    type(numbers(1)[0], '2');
    save();
    expect(saved(onSave).conditions).toEqual([{ input: 'section_classes', path: '', aggregate: 'count', test: '> 2' }]);
  });
});

describe('what is stored', () => {
  it('is the test as it was when no control of the value is edited, even one written another way', () => {
    const rule = ruleWith(
      condition('page_count', '>3'),
      condition('document_class', '"Invoice", "W2"'),
      condition('section_count', '= 2'),
      condition('reviewed', 'false'),
    );
    const { onSave } = renderModal({ rule });
    expect([1, 2, 3, 4].map(guided)).toEqual([true, true, true, true]);

    // Another control of the dialog is edited; the values are not.
    act(() => {
      fireEvent.change(modal().findContent().findAll('input')[1].getElement(), { target: { value: 'Renamed' } });
    });
    save();

    expect(saved(onSave)).toEqual({ ...rule, label: 'Renamed' });
  });

  it('is rewritten in the usual form once its control is edited', () => {
    const { onSave } = renderModal({ rule: ruleWith(condition('page_count', '>3')) });

    type(numbers(1)[0], '4');
    save();

    expect(savedTests(onSave)).toEqual(['> 4']);
  });

  it('shows the stored comparison and number of an existing condition', () => {
    renderModal({ rule: RULE });

    expect(guided(1)).toBe(true);
    expect(comparison(1).findTrigger().getElement().textContent).toContain('Greater than');
    expect(numbers(1).map((input) => input.value)).toEqual(['1000']);
  });
});

describe('the value of a condition, as an expression', () => {
  it.each([
    ['page_count', '> 1, < 5'],
    ['page_count', '-'],
    ['page_count', '(1..5)'],
    ['document_class', '!= "W2"'],
    ['reviewed', 'not(true)'],
  ])('opens a test of %s the guided controls cannot show, %s, as it is, and stores it unchanged', (input, test) => {
    const { onSave } = renderModal({ rule: ruleWith(condition(input, test)) });

    expect(guided(1)).toBe(false);
    expect(expression(1).value).toBe(test);
    expect(isDisabled(link(1, 'Use guided controls')!)).toBe(true);
    save();

    expect(savedTests(onSave)).toEqual([test]);
  });

  it('is the only way for a date, and for a field the profile does not describe', () => {
    renderModal({
      rule: ruleWith(
        condition('field_value', 'date("2026-01-31")', 'Invoice:DueDate'),
        condition('field_value', '> 5', 'Invoice:Lines[0].Amount'),
      ),
    });

    expect([guided(1), guided(2)]).toEqual([false, false]);
    expect([1, 2].map((row) => isDisabled(link(row, 'Use guided controls')!))).toEqual([true, true]);
    expect(expression(2).value).toBe('> 5');
  });

  it('shows the stored test of a guided value, with two examples for the type of the input', () => {
    renderModal({ rule: ruleWith(condition('page_count', '> 3'), condition('document_class', '"Invoice"')) });

    follow(1, 'Edit as expression');
    expect(expression(1).value).toBe('> 3');
    expect(screen.getByText('For example > 0 or [10..20]')).toBeTruthy();

    follow(2, 'Edit as expression');
    expect(expression(2).value).toBe('"Invoice"');
    expect(screen.getByText('For example "Invoice" or "Invoice","Receipt"')).toBeTruthy();
  });

  it('is stored as typed, and stays an expression while it is typed', () => {
    const { onSave } = renderModal({ rule: ruleWith(condition('page_count', '> 3')) });
    follow(1, 'Edit as expression');

    type(expression(1), '> 3, < 1');
    expect(isDisabled(link(1, 'Use guided controls')!)).toBe(true);
    // A text the controls could show does not switch back by itself.
    type(expression(1), '<= 9');
    expect(guided(1)).toBe(false);
    save();

    expect(savedTests(onSave)).toEqual(['<= 9']);
  });

  it('goes back to the guided controls, which show what was typed', () => {
    renderModal({ rule: ruleWith(condition('page_count', '> 3')) });
    follow(1, 'Edit as expression');
    type(expression(1), '[2..9]');

    follow(1, 'Use guided controls');

    expect(guided(1)).toBe(true);
    expect(comparison(1).findTrigger().getElement().textContent).toContain('Between');
    expect(numbers(1).map((input) => input.value)).toEqual(['2', '9']);
  });

  it('stays with its condition when conditions are moved or removed', () => {
    renderModal({ rule: ruleWith(condition('page_count', '> 3'), condition('section_count', '> 1'), condition('reviewed', 'false')) });
    follow(1, 'Edit as expression');
    expect([1, 2, 3].map(guided)).toEqual([false, true, true]);

    pick(1, 'down');
    expect([1, 2, 3].map(guided)).toEqual([true, false, true]);
    expect(expression(2).value).toBe('> 3');

    pick(1, 'remove');
    expect([1, 2].map(guided)).toEqual([false, true]);
  });

  it('starts afresh each time the dialog opens', () => {
    const first = ruleWith(condition('page_count', '> 3'));
    const { reopen } = renderModal({ rule: first });
    follow(1, 'Edit as expression');
    expect(guided(1)).toBe(false);

    reopen({ rule: first, visible: false });
    reopen({ rule: { ...ruleWith(condition('page_count', '> 8')), id: 'other' } });

    expect(guided(1)).toBe(true);
    expect(numbers(1).map((input) => input.value)).toEqual(['8']);
  });
});

describe('the outcome', () => {
  const queue = () => group('Outcome and reason').findContent().findSelect()!;

  it('keeps one choice: a review queue, or no review', () => {
    renderModal({ rule: RULE });

    expect(screen.getAllByRole('radio').map((radio) => (radio as HTMLInputElement).value)).toEqual(['queue', 'no_review']);
    expect(queue().findTrigger().getElement().textContent).toContain('Claims');
  });

  it('is stored as one review queue action for the queue that is chosen', () => {
    const { onSave } = renderModal({ rule: RULE });

    act(() => queue().openDropdown());
    act(() => queue().selectOptionByValue('default'));
    save();

    expect(saved(onSave).actions).toEqual([{ type: 'review_queue', queue: 'default' }]);
  });

  it('is stored as no action for no review, which drops the reason', () => {
    const { onSave } = renderModal({ rule: RULE });
    fireEvent.click(screen.getByRole('radio', { name: /No review/ }));

    expect(group('Outcome and reason').findContent().findSelect()).toBeNull();
    expect(reason()).toBeNull();
    save();

    expect(saved(onSave).actions).toEqual([]);
    expect(saved(onSave).reason).toBe('');
  });

  it('asks for a queue when a routing rule with no review is sent to one', () => {
    const { onSave } = renderModal({ rule: { ...RULE, actions: [], reason: '' } });
    expect((screen.getByRole('radio', { name: /No review/ }) as HTMLInputElement).checked).toBe(true);

    fireEvent.click(screen.getByRole('radio', { name: /Send to a review queue/ }));
    save();
    expect(screen.getByText('Choose the queue the routing rule sends documents to.')).toBeTruthy();
    expect(onSave).not.toHaveBeenCalled();

    act(() => queue().openDropdown());
    act(() => queue().selectOptionByValue('claims'));
    save();
    expect(saved(onSave).actions).toEqual([{ type: 'review_queue', queue: 'claims' }]);
  });

  it('keeps a queue the list does not have, so opening the dialog changes nothing', () => {
    const rule = { ...RULE, actions: [{ type: 'review_queue', queue: 'gone' }] };
    const { onSave } = renderModal({ rule });

    save();

    expect(saved(onSave).actions).toEqual([{ type: 'review_queue', queue: 'gone' }]);
  });

  it('keeps the actions as they were read when the choice is not changed', () => {
    const actions = [
      { type: 'review_queue', queue: 'claims', note: 'kept' },
      { type: 'notify', queue: '' },
    ];
    const { onSave } = renderModal({ rule: { ...RULE, actions } });

    save();

    expect(saved(onSave).actions).toEqual(actions);
  });
});

describe('the reason', () => {
  it('is shown for a routing rule that sends to a queue, and stored as typed', () => {
    const { onSave } = renderModal({ rule: RULE });

    expect(reason()!.getTextareaValue()).toBe(RULE.reason);
    act(() => reason()!.setTextareaValue('Check the total: {field_value:Invoice:Total}.'));
    save();

    expect(saved(onSave).reason).toBe('Check the total: {field_value:Invoice:Total}.');
  });

  it('is kept while the outcome is switched to no review and back', () => {
    const { onSave } = renderModal({ rule: RULE });

    fireEvent.click(screen.getByRole('radio', { name: /No review/ }));
    fireEvent.click(screen.getByRole('radio', { name: /Send to a review queue/ }));
    save();

    expect(saved(onSave)).toEqual(RULE);
  });

  it('offers the values of the conditions as they are in the dialog', () => {
    renderModal({ rule: ruleWith() });
    addCondition();
    choose(1, 'Input', 'page_count');
    const menu = group('Outcome and reason').findContent().findButtonDropdown()!;

    act(() => menu.openDropdown());

    expect(
      menu
        .findItems()
        .slice(0, 2)
        .map((item) => item.getElement().getAttribute('data-testid')),
    ).toEqual(['{page_count}', '{condition_1.value}']);
  });

  it('says that failed policy rules are listed for the reviewer without being written', () => {
    renderModal();

    expect(screen.getByText(/Failed policy rules are listed for the reviewer automatically/)).toBeTruthy();
  });
});

describe('the reason of the document last tested', () => {
  const REASON = 'The total 1250.00 is over 1000.';
  const tested = (over: Record<string, unknown> = {}): NonNullable<ModalProps['lastTest']> =>
    ({
      decision: 'hold',
      matchedRuleId: RULE.id,
      ruleLabel: RULE.label,
      outcomeKind: 'queue',
      outcomeQueue: 'claims',
      placedQueue: 'claims',
      reason: REASON,
      reasonSource: 'admin',
      ...over,
    }) as NonNullable<ModalProps['lastTest']>;
  const outcome = () => group('Outcome and reason').findContent().getElement();

  it('is shown under the reason field when the test matched this routing rule', () => {
    renderModal({ rule: RULE, lastTest: tested() });
    const text = outcome().textContent ?? '';

    expect(within(outcome()).getByText('Reason for the document last tested')).toBeTruthy();
    expect(within(outcome()).getByText(REASON)).toBeTruthy();
    expect(within(outcome()).getByText('The reason as written, with the values of this document.')).toBeTruthy();
    expect(
      within(outcome()).getByText('From the last test on this tab, with the routing rule as it was then. Test again to see a change.'),
    ).toBeTruthy();
    // Under the field and its note about failed policy rules.
    expect(text.indexOf('Failed policy rules are listed for the reviewer automatically')).toBeLessThan(
      text.indexOf('Reason for the document last tested'),
    );
  });

  it('says when the reason shown was generated', () => {
    renderModal({
      rule: { ...RULE, reason: '' },
      lastTest: tested({ reason: 'Claims over 1000: Field value > 1000', reasonSource: 'fallback' }),
    });

    expect(within(outcome()).getByText('Claims over 1000: Field value > 1000')).toBeTruthy();
    expect(
      within(outcome()).getByText('Generated: the outcome has no reason, or its reason could not be filled in for this document.'),
    ).toBeTruthy();
  });

  it('is shown as text, not as markup', () => {
    const markup = 'Vendor <img src=x onerror="alert(1)"> **Acme** [site](https://example.com)';
    renderModal({ rule: RULE, lastTest: tested({ reason: markup }) });

    expect(within(outcome()).getByText(markup)).toBeTruthy();
    expect(outcome().querySelector('img')).toBeNull();
    expect(outcome().querySelector('a')).toBeNull();
  });

  it.each([
    ['there was no test', null],
    ['the test matched another routing rule', tested({ matchedRuleId: 'another' })],
    ['the default outcome applied', tested({ matchedRuleId: '' })],
    ['the answer holds no reason', tested({ reason: null })],
    ['the answer holds an empty reason', tested({ reason: '  ' })],
  ])('is not shown when %s', (_when, lastTest) => {
    renderModal({ rule: RULE, lastTest });

    expect(screen.queryByText('Reason for the document last tested')).toBeNull();
    expect(screen.queryByText(/From the last test on this tab/)).toBeNull();
  });

  it('is not shown for a routing rule that is being added, which no test can have matched', () => {
    renderModal({ rule: null, lastTest: tested({ matchedRuleId: '' }) });

    expect(screen.queryByText('Reason for the document last tested')).toBeNull();
  });

  it('goes with the reason field when the outcome is switched to no review', () => {
    renderModal({ rule: RULE, lastTest: tested() });

    fireEvent.click(screen.getByRole('radio', { name: /No review/ }));
    expect(screen.queryByText('Reason for the document last tested')).toBeNull();

    fireEvent.click(screen.getByRole('radio', { name: /Send to a review queue/ }));
    expect(screen.getByText('Reason for the document last tested')).toBeTruthy();
  });

  it('changes nothing that is saved', () => {
    const { onSave } = renderModal({ rule: RULE, lastTest: tested({ reason: 'Another text.' }) });

    save();

    expect(saved(onSave)).toEqual(RULE);
  });
});

describe('editing a routing rule', () => {
  it('opens on the routing rule, with an id that cannot be changed', () => {
    renderModal({ rule: RULE });

    expect(modal().findHeader().getElement().textContent).toContain('Edit routing rule claims-over-1000');
    expect((idInput().getElement() as HTMLInputElement).value).toBe('claims-over-1000');
    expect((idInput().getElement() as HTMLInputElement).disabled).toBe(true);
  });

  it('keeps the routing rule as it was when nothing is changed', () => {
    const { onSave } = renderModal({ rule: RULE });

    save();

    expect(onSave).toHaveBeenCalledWith(RULE);
  });

  it('does not ask an existing id to be unique against itself', () => {
    const { onSave } = renderModal({ rule: { ...RULE, id: 'taken' }, otherIds: [] });

    save();

    expect(onSave).toHaveBeenCalled();
  });
});

describe('another role', () => {
  const POLICY_RULE: RoutingRule = {
    ...RULE,
    conditions: [
      ...RULE.conditions,
      condition('document_class', '"Invoice"'),
      condition('reviewed', 'false'),
      condition('page_count', '> 1, < 5'),
    ],
  };

  it('reads the routing rule under its id, and can only close the dialog', () => {
    const { onSave, onDismiss } = renderModal({ rule: POLICY_RULE, readOnly: true, queues: [] });

    expect(modal().findHeader().getElement().textContent).toContain('Routing rule claims-over-1000');
    expect(modal().findHeader().getElement().textContent).not.toContain('Edit');
    expect(
      modal()
        .findFooter()!
        .findAllButtons()
        .map((button) => button.getElement().textContent),
    ).toEqual(['Close']);

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('sees the three groups, the conditions with their values, the queue by id and the reason', () => {
    renderModal({ rule: POLICY_RULE, readOnly: true, queues: [] });

    expect(groups()).toHaveLength(3);
    expect(conditionBoxes()).toHaveLength(4);
    expect(conditionLabels(1)).toEqual(['Input', 'Field', 'Match']);
    expect(numbers(1).map((input) => input.value)).toEqual(['1000']);
    expect(
      values(2)
        .findTokens()
        .map((token) => token.findLabel().getElement().textContent),
    ).toEqual(['Invoice']);
    expect(value(3).findSegmentedControl()!.findSelectedSegment()!.getElement().textContent).toBe('No');
    expect(expression(4).value).toBe('> 1, < 5');
    expect(group('Outcome and reason').findContent().findSelect()!.findTrigger().getElement().textContent).toContain('claims');
    expect(reason()!.getTextareaValue()).toBe(RULE.reason);
  });

  it('is offered nothing that changes the routing rule', () => {
    renderModal({ rule: POLICY_RULE, readOnly: true, queues: [] });
    const content = modal().findContent();

    // No condition to add, no menu on a condition, no value to insert, no way to switch a value to an expression.
    expect(within(content.getElement()).queryByRole('button', { name: 'Add condition' })).toBeNull();
    expect(content.findButtonDropdown()).toBeNull();
    expect(conditionBox(1).findButtonDropdown()).toBeNull();
    expect(link(1, 'Edit as expression')).toBeNull();
    expect(link(4, 'Use guided controls')).toBeNull();
    // Every field can be read, and none can be typed in or opened.
    const typedIn = [idInput().getElement() as HTMLInputElement, content.findAll('input')[1].getElement() as HTMLInputElement];
    expect([...typedIn, numbers(1)[0], expression(4)].map((field) => field.readOnly)).toEqual([true, true, true, true]);
    expect((reason()!.findNativeTextarea().getElement() as HTMLTextAreaElement).readOnly).toBe(true);
    const lists = [selectIn(1, 'Input'), selectIn(1, 'Field'), selectIn(1, 'Match'), comparison(1), comparison(2)];
    expect(lists.map((list) => isDisabled(list.findTrigger().getElement()))).toEqual([true, true, true, true, true]);
    expect(isDisabled(values(2).findTrigger().getElement())).toBe(true);
    expect(
      value(3)
        .findSegmentedControl()!
        .findSegments()
        .map((segment) => isDisabled(segment.getElement())),
    ).toEqual([true, true]);
  });

  it('cannot switch the routing rule off or change its outcome', () => {
    renderModal({ rule: POLICY_RULE, readOnly: true, queues: [] });

    const toggle = group('Routing rule').findContent().findToggle()!.findNativeInput();
    act(() => toggle.click());
    expect(toggle.getElement().checked).toBe(true);

    fireEvent.click(screen.getByRole('radio', { name: /No review/ }));
    expect((screen.getByRole('radio', { name: /Send to a review queue/ }) as HTMLInputElement).checked).toBe(true);
    expect(reason()).not.toBeNull();
  });
});

describe('what the server refused', () => {
  const REFUSAL =
    "Routing table 'review-routing', rule 'claims-over-1000', input 'field_value Invoice:Total': the test '>> 1000' is not valid";

  it('is shown in the dialog, which stays open with the edits', () => {
    const { onDismiss } = renderModal({ rule: RULE, serverError: REFUSAL });

    expect(screen.getByText(REFUSAL)).toBeTruthy();
    expect(screen.getByText('The configuration was not saved')).toBeTruthy();
    expect(modal().isVisible()).toBe(true);
    expect((idInput().getElement() as HTMLInputElement).value).toBe('claims-over-1000');
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it('is absent when there is none', () => {
    renderModal({ rule: RULE });

    expect(screen.queryByText('The configuration was not saved')).toBeNull();
  });
});

describe('closing', () => {
  it('cancels without keeping anything', () => {
    const { onSave, onDismiss } = renderModal({ rule: RULE });

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onDismiss).toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
  });
});
