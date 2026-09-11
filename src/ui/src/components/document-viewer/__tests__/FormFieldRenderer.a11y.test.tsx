// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The accessibility shape of a rendered field.
 *
 * Every field value used to be a textbox inside an element claiming
 * `role="button"` — interactive content nested in a control, which ARIA forbids
 * and which made each field announce twice: once as a button named after its
 * label, once as the textbox with the same label. The click-to-locate capability
 * was also mouse-only and had no visible affordance at all.
 *
 * The double-announcement itself is not asserted here: it is a name-from-content
 * effect, and jsdom does not compute accessible names from subtrees, so any test
 * for it would pass against the broken tree too. Removing the nesting is what
 * removes the doubling, and the nesting IS asserted.
 *
 * These assertions are about the tree, not the styling, so they survive layout
 * changes and fail if the wrapper ever goes back to being a button.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import FormFieldRenderer from '../FormFieldRenderer';

const GEOMETRY = { boundingBox: { Left: 0.1, Top: 0.2, Width: 0.3, Height: 0.05 }, page: 1 };

const renderField = (overrides: Record<string, unknown> = {}) =>
  render(
    <FormFieldRenderer
      fieldKey="AccountNumber"
      value="123456"
      onChange={vi.fn()}
      isReadOnly={false}
      geometry={GEOMETRY}
      path={['AccountNumber']}
      {...overrides}
    />,
  );

describe('FormFieldRenderer accessibility', () => {
  it('does not nest the value input inside a button', () => {
    renderField();

    const input = screen.getByRole('textbox');
    // Walking ancestors, rather than checking one known element, so the assertion
    // holds wherever a future `role="button"` might be reintroduced.
    let ancestor: HTMLElement | null = input.parentElement;
    while (ancestor) {
      expect(ancestor.getAttribute('role')).not.toBe('button');
      expect(ancestor.tagName).not.toBe('BUTTON');
      ancestor = ancestor.parentElement;
    }
  });

  it('offers the locate action as a real, named control rather than a click target with no affordance', async () => {
    const onFieldFocus = vi.fn();
    renderField({ onFieldFocus });

    const locate = screen.getByRole('button', { name: /Show AccountNumber on the page/i });
    await userEvent.click(locate);

    expect(onFieldFocus).toHaveBeenCalledWith(GEOMETRY);
  });

  it('offers no locate control when the field has no bounding box', () => {
    renderField({ geometry: undefined });

    // Caught on a live stack: a baseline with no geometry showed the banner "No
    // field geometry available — bounding-box highlighting is disabled" and a
    // magnifier on every field regardless. A control that cannot do the thing it
    // depicts is worse than no control.
    expect(screen.queryByRole('button', { name: /on the page/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /AccountNumber/i })).not.toBeInTheDocument();
  });

  it('still selects the field on focus when there is no bounding box to show', async () => {
    // Removing the button must not remove keyboard reach: selection is what drives
    // the copy-link affordance, and it does not need geometry.
    const onFieldPathSelect = vi.fn();
    renderField({ geometry: undefined, onFieldPathSelect });

    await userEvent.tab();

    expect(onFieldPathSelect).toHaveBeenCalledWith('AccountNumber');
  });

  it('keeps the locate control reachable in read-only mode, where there is no input to focus', () => {
    renderField({ isReadOnly: true });

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Show AccountNumber on the page/i })).toBeInTheDocument();
  });

  it('selects the field when the input takes focus, so keyboard users get what clicking gives', async () => {
    const onFieldPathSelect = vi.fn();
    const onFieldFocus = vi.fn();
    renderField({ onFieldPathSelect, onFieldFocus });

    // Tabbing into the value is the keyboard equivalent of clicking it. Before,
    // the wrapper was the tab stop and focus alone did not select the field.
    await userEvent.tab();
    await userEvent.tab();

    expect(onFieldPathSelect).toHaveBeenCalledWith('AccountNumber');
    expect(onFieldFocus).toHaveBeenCalledWith(GEOMETRY);
  });
});
