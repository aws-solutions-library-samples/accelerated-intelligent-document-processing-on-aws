// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The preview Statistics tab's Type Distribution counts.
 *
 * A property written as a bare `{"$ref": "#/$defs/Address"}` carries no `type` of
 * its own, so it fell through every branch of the type tally and landed in NO
 * bucket: the buckets silently did not add up to Total Attributes. Resolving the
 * `$ref` to its target type first fixes it (GitHub #906).
 *
 * `getSchemaStats` is module-private, so this exercises it through the rendered
 * tab rather than by exporting it just for the test.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import SchemaPreviewTabs from '../SchemaPreviewTabs';

const ADDRESS_CLASS = {
  id: 'class-Address',
  name: 'Address',
  attributes: { type: 'object', properties: {}, required: [] },
};

/** One class with a bare-$ref property and a plain string property. */
const INVOICE_CLASS = {
  id: 'class-Invoice',
  name: 'Invoice',
  attributes: {
    type: 'object',
    properties: {
      billing_address: { $ref: '#/$defs/Address' },
      invoice_number: { type: 'string' },
    },
    required: ['invoice_number'],
  },
};

/** The count rendered next to a Type Distribution label, e.g. `Object: 1`. */
const countFor = (label: string): string | undefined => screen.getByText(`${label}:`).parentElement?.textContent?.trim();

describe('SchemaPreviewTabs Statistics tab', () => {
  it('counts a bare $ref property in the Object bucket (#906)', async () => {
    render(<SchemaPreviewTabs classes={[INVOICE_CLASS, ADDRESS_CLASS] as never} selectedClassId="class-Invoice" />);

    await userEvent.click(screen.getByText('Statistics'));

    expect(countFor('Total Attributes')).toBe('Total Attributes: 2');
    // The $ref property used to be counted in neither bucket, leaving the type
    // distribution one short of the total.
    expect(countFor('Object')).toBe('Object: 1');
    expect(countFor('String')).toBe('String: 1');
  });
});
