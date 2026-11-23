// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The reason of a routing outcome: what the Insert value menu offers, where it writes a
 * placeholder, the size shown against the limit, and what another role sees.
 */

import React, { useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { describe, expect, it, vi } from 'vitest';

import ReasonField, { byteLength } from '../ReasonField';
import { MAX_REASON_PLACEHOLDERS, ROUTING_INPUTS, type RoutingCondition } from '../routing-types';

const CONDITIONS: RoutingCondition[] = [
  { input: 'field_value', path: 'Invoice:Total', aggregate: 'any', test: '> 1000' },
  { input: 'page_count', path: '', aggregate: 'any', test: '> 3' },
];

/** The field with its value held the way the dialog holds it, so an insert shows in the text. */
const Held = ({
  initial,
  onChange,
  ...props
}: Partial<React.ComponentProps<typeof ReasonField>> & { initial: string; onChange: (value: string) => void }) => {
  const [value, setValue] = useState(initial);
  return (
    <ReasonField
      readOnly={false}
      conditions={CONDITIONS}
      maxBytes={500}
      {...props}
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
};

const renderField = (initial = 'The total is over the limit.', props: Partial<React.ComponentProps<typeof ReasonField>> = {}) => {
  const onChange = vi.fn();
  render(
    <IntlProvider locale="en">
      <Held initial={initial} onChange={onChange} {...props} />
    </IntlProvider>,
  );
  return onChange;
};

const field = () => createWrapper(document.body).findFormField()!;
const textarea = (): HTMLTextAreaElement => createWrapper(document.body).findTextarea()!.findNativeTextarea().getElement();
const menu = () => field().findSecondaryControl()!.findButtonDropdown()!;
const offered = (): string[] => {
  act(() => menu().openDropdown());
  const ids = menu()
    .findItems()
    .map((item) => item.getElement().getAttribute('data-testid') ?? '');
  act(() => menu().findNativeButton().click());
  return ids;
};
const insert = async (placeholder: string): Promise<void> => {
  act(() => menu().openDropdown());
  act(() => menu().findItemById(placeholder)!.click());
  // The cursor is put after the inserted value on the next frame.
  await act(async () => {
    await new Promise((resolve) => window.requestAnimationFrame(resolve));
  });
};
/** Puts the cursor (or a selection) in the field the way a click or the arrow keys do. */
const select = (start: number, end = start): void => {
  textarea().setSelectionRange(start, end);
  fireEvent.select(textarea());
};

describe('the Insert value menu', () => {
  it("offers what each condition reads and compares with, then the document's values and the confidence threshold", () => {
    renderField();

    expect(offered()).toEqual([
      '{field_value:Invoice:Total}',
      '{condition_1.value}',
      '{page_count}',
      '{condition_2.value}',
      // Every input that needs nothing else to be named, but the one a condition already offers.
      ...ROUTING_INPUTS.filter((input) => !input.needs_path && input.name !== 'page_count').map((input) => `{${input.name}}`),
      '{confidence_threshold}',
    ]);
  });

  it('groups the values of the conditions apart from those of the document', () => {
    renderField();
    act(() => menu().openDropdown());

    expect(screen.getByText("This routing rule's conditions")).toBeTruthy();
    expect(screen.getByText('Document')).toBeTruthy();
  });

  it('shows an input by its name, with the placeholder it writes', () => {
    renderField();
    act(() => menu().openDropdown());

    const item = menu().findItemById('{field_value:Invoice:Total}')!.getElement();
    expect(item.textContent).toContain('Field value Invoice:Total');
    expect(item.textContent).toContain('{field_value:Invoice:Total}');
    expect(menu().findItemById('{condition_1.value}')!.getElement().textContent).toContain('Comparison value of condition 1');
  });

  it('offers only the document values when there is no condition (the default outcome)', () => {
    renderField('', { conditions: [] });

    const ids = offered();
    expect(ids[0]).toBe('{document_class}');
    expect(ids).not.toContain('{condition_1.value}');
    act(() => menu().openDropdown());
    expect(screen.queryByText("This routing rule's conditions")).toBeNull();
    expect(screen.getByText('Document')).toBeTruthy();
  });

  it('offers a condition that has no field yet by its value only, and one input once', () => {
    renderField('', {
      conditions: [
        { input: 'field_value', path: '', aggregate: 'any', test: '' },
        { input: 'page_count', path: '', aggregate: 'any', test: '> 3' },
        { input: 'page_count', path: '', aggregate: 'any', test: '< 9' },
        { input: '', path: '', aggregate: 'any', test: '' },
      ],
    });

    expect(offered().slice(0, 5)).toEqual([
      '{condition_1.value}',
      '{page_count}',
      '{condition_2.value}',
      '{condition_3.value}',
      '{condition_4.value}',
    ]);
  });

  it('reads the policy type of a count when the condition names one', () => {
    renderField('', { conditions: [{ input: 'failed_policy_rule_count', path: 'prior_auth', aggregate: 'any', test: '> 0' }] });

    const ids = offered();
    expect(ids[0]).toBe('{failed_policy_rule_count:prior_auth}');
    // The count over every policy type is still a value of the document.
    expect(ids).toContain('{failed_policy_rule_count}');
  });
});

describe('inserting a value', () => {
  it('writes it at the end of a field that was not used yet', async () => {
    const onChange = renderField('Total: ');

    await insert('{field_value:Invoice:Total}');

    expect(onChange).toHaveBeenLastCalledWith('Total: {field_value:Invoice:Total}');
  });

  it('writes it where the cursor is, and puts the cursor after it', async () => {
    const onChange = renderField('The total is over the limit.');
    select(10);

    await insert('{field_value:Invoice:Total}');

    expect(onChange).toHaveBeenLastCalledWith('The total {field_value:Invoice:Total}is over the limit.');
    const after = 'The total {field_value:Invoice:Total}'.length;
    expect([textarea().selectionStart, textarea().selectionEnd]).toEqual([after, after]);
    expect(document.activeElement).toBe(textarea());
  });

  it('replaces the text that is selected', async () => {
    const onChange = renderField('Over the limit of LIMIT.');
    select(18, 23);

    await insert('{condition_1.value}');

    expect(onChange).toHaveBeenLastCalledWith('Over the limit of {condition_1.value}.');
  });

  it('writes a second value after the first', async () => {
    const onChange = renderField('Pages: ');

    await insert('{page_count}');
    await insert('{confidence_threshold}');

    expect(onChange).toHaveBeenLastCalledWith('Pages: {page_count}{confidence_threshold}');
  });

  it('follows the cursor when it is moved with the keyboard', async () => {
    const onChange = renderField('ab');
    textarea().setSelectionRange(1, 1);
    fireEvent.keyUp(textarea(), { key: 'ArrowLeft' });

    await insert('{page_count}');

    expect(onChange).toHaveBeenLastCalledWith('a{page_count}b');
  });

  it(`is not offered once the reason has ${MAX_REASON_PLACEHOLDERS} values`, () => {
    renderField('{page_count}'.repeat(MAX_REASON_PLACEHOLDERS));

    const trigger = menu().findNativeButton().getElement();
    expect(trigger.disabled || trigger.getAttribute('aria-disabled') === 'true').toBe(true);
  });

  it('counts a doubled brace as text, not as a value', async () => {
    const reason = `${'{page_count}'.repeat(MAX_REASON_PLACEHOLDERS - 1)} {{not a value}}`;
    const onChange = renderField(reason);

    await insert('{page_count}');

    expect(onChange).toHaveBeenLastCalledWith(`${reason}{page_count}`);
  });
});

describe('the size', () => {
  it('is shown in bytes against the limit', () => {
    renderField('Total over the limit');

    expect(field().findConstraint()!.getElement().textContent).toBe('20 of 500 bytes');
  });

  it('counts bytes, not characters', () => {
    renderField('Montant élevé €');

    expect(byteLength('Montant élevé €')).toBe(19);
    expect(field().findConstraint()!.getElement().textContent).toBe('19 of 500 bytes');
  });

  it('is an error over the limit', () => {
    renderField('x'.repeat(21), { maxBytes: 20 });

    expect(field().findError()!.getElement().textContent).toContain('The reason is longer than 20 bytes.');
  });

  it('is no error at the limit', () => {
    renderField('x'.repeat(20), { maxBytes: 20 });

    expect(field().findError()).toBeNull();
    expect(field().findConstraint()!.getElement().textContent).toBe('20 of 20 bytes');
  });
});

describe('what the field says', () => {
  it('is labelled as optional and says failed policy rules are listed without being written', () => {
    renderField();

    expect(field().findLabel()!.getElement().textContent).toBe('Reason for the reviewer (optional)');
    expect(
      screen.getByText('Failed policy rules are listed for the reviewer automatically, with the reasons policy rule validation gave.'),
    ).toBeTruthy();
  });

  it('passes what is typed', () => {
    const onChange = renderField('');

    act(() => createWrapper(document.body).findTextarea()!.setTextareaValue('Check the total.'));

    expect(onChange).toHaveBeenLastCalledWith('Check the total.');
  });
});

describe('another role', () => {
  it('reads the reason and is offered no value to insert', () => {
    renderField('Total over {condition_1.value}.', { readOnly: true });

    expect(textarea().value).toBe('Total over {condition_1.value}.');
    expect(textarea().readOnly).toBe(true);
    expect(field().findSecondaryControl()).toBeNull();
  });
});
