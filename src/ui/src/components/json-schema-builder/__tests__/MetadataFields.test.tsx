// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Default Value parsing for a property that references a shared class.
 *
 * A property written as a bare `{"$ref": "#/$defs/Address"}` carries no `type` of
 * its own, so `parseInputValue(input, attribute.type)` received `undefined`, fell
 * through to its `'string'` default, and WROTE a JSON default into the user's
 * schema as a raw string — `default: '{"street":"1 Main St"}'` instead of an
 * object. Resolving the `$ref` to its target type first fixes it (GitHub #906).
 *
 * This is the only change in that fix that alters what gets written into a
 * schema, hence a test of its own.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import MetadataFields from '../constraints/MetadataFields';

/** A designer class, as `availableClasses` holds it: body nested under `attributes`. */
const ADDRESS_CLASS = {
  id: 'class-Address',
  name: 'Address',
  attributes: { properties: {}, required: [] },
};

const DEFAULT_VALUE_PLACEHOLDER = 'e.g., 0, N/A, or a JSON value';

/** Type into Default Value and blur, which is when the value is parsed and stored. */
const typeDefaultValue = async (attribute: Record<string, unknown>, text: string, availableClasses?: Record<string, unknown>[]) => {
  const onUpdate = vi.fn();
  render(<MetadataFields attribute={attribute} onUpdate={onUpdate} availableClasses={availableClasses as never} />);

  // `{` and `[` are keyboard-descriptor syntax to userEvent; doubling them types
  // the literal character.
  const literal = text.replace(/[{[]/g, (ch) => ch + ch);
  await userEvent.type(screen.getByPlaceholderText(DEFAULT_VALUE_PLACEHOLDER), literal);
  await userEvent.tab();

  return onUpdate;
};

describe('MetadataFields Default Value', () => {
  it('parses a JSON default for a bare $ref property by resolving the ref (#906)', async () => {
    const onUpdate = await typeDefaultValue({ $ref: '#/$defs/Address' }, '{"street":"1 Main St"}', [ADDRESS_CLASS]);

    expect(onUpdate).toHaveBeenCalled();
    const [updates] = onUpdate.mock.lastCall as [Record<string, unknown>];
    // An object, NOT the raw string it used to be stored as.
    expect(updates.default).toEqual({ street: '1 Main St' });
  });

  it('is unchanged for an inline object type', async () => {
    const onUpdate = await typeDefaultValue({ type: 'object' }, '{"a":1}');

    expect((onUpdate.mock.lastCall as [Record<string, unknown>])[0].default).toEqual({ a: 1 });
  });

  it('is unchanged for an inline string type', async () => {
    const onUpdate = await typeDefaultValue({ type: 'string' }, 'N/A');

    expect((onUpdate.mock.lastCall as [Record<string, unknown>])[0].default).toBe('N/A');
  });

  it('keeps the raw string when the ref cannot be resolved', async () => {
    // No class list to resolve against: the type is genuinely unknown, so the
    // input is stored verbatim rather than guessed at.
    const onUpdate = await typeDefaultValue({ $ref: '#/$defs/Address' }, '{"street":"1 Main St"}');

    const [updates] = onUpdate.mock.lastCall as [Record<string, unknown>];
    expect(updates.default).toBe('{"street":"1 Main St"}');
  });
});
