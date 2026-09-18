// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Const parsing for a property that references a shared class.
 *
 * Same blind spot as the Evaluation Method dropdown and the Default Value parser
 * (GitHub #906): a property written as a bare `{"$ref": "#/$defs/Address"}` has no
 * `type` of its own, so `parseInputValue(input, attribute.type)` received
 * `undefined` and stored a JSON Const as a raw string.
 *
 * This panel is the one place it mattered most, because unlike every sibling
 * constraints panel (`StringConstraints`, `NumberConstraints`, `ArrayConstraints`,
 * `ObjectConstraints`) it does not early-return on a type mismatch — it is
 * rendered for every non-rule attribute and its inputs are not gated by type.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ValueConstraints from '../constraints/ValueConstraints';

/** A designer class, as `availableClasses` holds it: body nested under `attributes`. */
const ADDRESS_CLASS = {
  id: 'class-Address',
  name: 'Address',
  attributes: { properties: {}, required: [] },
};

/** Type into Const and blur, which is when the value is parsed and stored. */
const typeConst = async (attribute: Record<string, unknown>, text: string, availableClasses?: Record<string, unknown>[]) => {
  const onUpdate = vi.fn();
  render(<ValueConstraints attribute={attribute} onUpdate={onUpdate} availableClasses={availableClasses as never} />);

  // `{` and `[` are keyboard-descriptor syntax to userEvent; doubling them types
  // the literal character.
  const literal = text.replace(/[{[]/g, (ch) => ch + ch);
  await userEvent.type(screen.getByPlaceholderText('e.g., active'), literal);
  await userEvent.tab();

  return onUpdate;
};

describe('ValueConstraints Const', () => {
  it('parses a JSON const for a bare $ref property by resolving the ref (#906)', async () => {
    const onUpdate = await typeConst({ $ref: '#/$defs/Address' }, '{"street":"1 Main St"}', [ADDRESS_CLASS]);

    expect(onUpdate).toHaveBeenCalled();
    const [updates] = onUpdate.mock.lastCall as [Record<string, unknown>];
    // An object, NOT the raw string it used to be stored as.
    expect(updates.const).toEqual({ street: '1 Main St' });
  });

  it('is unchanged for an inline string type', async () => {
    const onUpdate = await typeConst({ type: 'string' }, 'active');

    expect((onUpdate.mock.lastCall as [Record<string, unknown>])[0].const).toBe('active');
  });
});
