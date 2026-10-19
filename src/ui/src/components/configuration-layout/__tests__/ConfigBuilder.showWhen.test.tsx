// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * `showWhen` — the n-condition visibility gate the classification panel needs.
 *
 * `dependsOn` takes exactly ONE field and one value set, which cannot express
 * "images are on AND granularity is page" — the shape that panel is built
 * around, where granularity and modality are deliberately independent settings
 * whose COMBINATIONS decide what applies. Rather than growing `dependsOn` into
 * something that means two different things depending on which keys it carries,
 * `showWhen` is the n-condition form, and `not` / `notValues` give it the
 * negation `dependsOn` also lacks.
 *
 * Two properties here are easy to lose and hard to notice:
 *
 * - **The scope walk.** `field` resolves in the nearest enclosing scope that has
 *   it, so a condition inside `classification.image` can reference
 *   `modality.images` (which lives at `classification.modality.images`) without
 *   spelling out the whole path. `dependsOn` tries sibling-then-root only, which
 *   would resolve that to `classification.image.modality.images` and then to a
 *   top-level `modality.images` — neither of which exists, so the field would
 *   vanish rather than being gated.
 * - **Absent-value polarity.** A field that is not in the form fails a positive
 *   condition and passes a negative one. Getting that backwards hides every
 *   `notValues` field on a configuration that has not set the key yet, which is
 *   the default state of a brand-new deployment.
 */

import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../../hooks/use-synthetic-data-generator', () => ({ default: () => ({ available: false }) }));

import ConfigBuilder from '../ConfigBuilder';

const renderConfig = (schema: Record<string, unknown>, formValues: Record<string, unknown>) =>
  render(
    <MemoryRouter>
      <ConfigBuilder schema={schema} formValues={formValues} />
    </MemoryRouter>,
  );

/**
 * A classification-shaped schema: `granularity` and `modality.images` as the
 * conditions, and `gated` as the field under test, placed either beside them or
 * one level deeper so the scope walk is exercised.
 */
const schemaWith = (showWhen: unknown, options: { nested?: boolean } = {}) => {
  const gated = {
    type: 'string',
    title: 'Gated field',
    showWhen,
  };
  return {
    type: 'object',
    properties: {
      classification: {
        type: 'object',
        sectionLabel: 'Classification',
        properties: {
          granularity: { type: 'string', title: 'Granularity', enum: ['', 'page', 'packet'] },
          always: { type: 'string', title: 'Always visible' },
          modality: {
            type: 'object',
            sectionLabel: 'Modality',
            properties: { images: { type: 'string', title: 'Send page images' } },
          },
          // `collapsible: false` is load-bearing in the nested case: a
          // collapsible nested section renders collapsed, so its children are
          // absent from the DOM for a reason that has nothing to do with
          // showWhen — which would make the "hidden" half of these cases pass
          // without the gate working at all.
          ...(options.nested ? { image: { type: 'object', sectionLabel: 'Image', collapsible: false, properties: { gated } } } : { gated }),
        },
      },
    },
  };
};

const values = (classification: Record<string, unknown>) => ({ classification });

const isVisible = () => screen.queryAllByText('Gated field').length > 0;

describe('ConfigBuilder showWhen', () => {
  it('shows a field with no showWhen', () => {
    renderConfig(schemaWith(undefined), values({ granularity: 'page' }));
    expect(isVisible()).toBe(true);
  });

  it('shows a field whose single condition matches', () => {
    renderConfig(schemaWith([{ field: 'granularity', value: 'packet' }]), values({ granularity: 'packet' }));
    expect(isVisible()).toBe(true);
  });

  it('hides a field whose single condition does not match', () => {
    renderConfig(schemaWith([{ field: 'granularity', value: 'packet' }]), values({ granularity: 'page' }));
    expect(isVisible()).toBe(false);
  });

  it('requires EVERY condition to hold', () => {
    const showWhen = [
      { field: 'granularity', value: 'page' },
      { field: 'modality.images', value: 'true' },
    ];
    renderConfig(schemaWith(showWhen), values({ granularity: 'page', modality: { images: 'true' } }));
    expect(isVisible()).toBe(true);
  });

  it('hides the field when only one of two conditions holds', () => {
    const showWhen = [
      { field: 'granularity', value: 'page' },
      { field: 'modality.images', value: 'true' },
    ];
    renderConfig(schemaWith(showWhen), values({ granularity: 'page', modality: { images: 'false' } }));
    expect(isVisible()).toBe(false);
  });

  it('accepts a values array', () => {
    renderConfig(schemaWith([{ field: 'granularity', values: ['', 'page'] }]), values({ granularity: '' }));
    expect(isVisible()).toBe(true);
  });

  it('hides on a notValues match', () => {
    renderConfig(schemaWith([{ field: 'granularity', notValues: ['packet'] }]), values({ granularity: 'packet' }));
    expect(isVisible()).toBe(false);
  });

  it('shows on a notValues miss', () => {
    renderConfig(schemaWith([{ field: 'granularity', notValues: ['packet'] }]), values({ granularity: 'page' }));
    expect(isVisible()).toBe(true);
  });

  it('treats an absent value as failing a positive condition', () => {
    renderConfig(schemaWith([{ field: 'granularity', value: 'packet' }]), values({}));
    expect(isVisible()).toBe(false);
  });

  it('treats an absent value as passing a negative condition', () => {
    // ⚠️ The default state of a new deployment: the key is not in the form at
    // all. Getting this backwards hides every notValues-gated field.
    renderConfig(schemaWith([{ field: 'modality.images', notValues: ['false'] }]), values({}));
    expect(isVisible()).toBe(true);
  });

  it('resolves a condition field in the nearest enclosing scope', () => {
    // The gated field is at classification.image.gated and the condition names
    // `modality.images`, which lives at classification.modality.images.
    renderConfig(
      schemaWith([{ field: 'modality.images', notValues: ['false'] }], { nested: true }),
      values({ modality: { images: 'false' }, image: { target_width: 1000 } }),
    );
    expect(isVisible()).toBe(false);
  });

  it('shows a nested field when the enclosing-scope condition passes', () => {
    renderConfig(
      schemaWith([{ field: 'modality.images', notValues: ['false'] }], { nested: true }),
      values({ modality: { images: 'true' }, image: { target_width: 1000 } }),
    );
    expect(isVisible()).toBe(true);
  });

  it('normalizes string true/false against real booleans', () => {
    // Schema values deserialize from DynamoDB as strings while form state holds
    // real booleans, so both sides are normalized.
    renderConfig(schemaWith([{ field: 'modality.images', value: 'false' }]), values({ modality: { images: false } }));
    expect(isVisible()).toBe(true);
  });

  it('leaves other fields in the section alone', () => {
    renderConfig(schemaWith([{ field: 'granularity', value: 'packet' }]), values({ granularity: 'page' }));
    expect(screen.queryAllByText('Always visible').length).toBeGreaterThan(0);
  });

  it('shows a "not yet set" field for both an empty value and an absent key', () => {
    // The real gate on the deprecated `classificationMethod` field, which must
    // be visible exactly while `granularity` has not been set. An absent key is
    // the state of every configuration written before granularity existed, and
    // it does NOT equal '' — so `values: ['', null]` would hide the field in
    // precisely the case it exists for, besides being a literal null that
    // CloudFormation refuses in a template.
    const showWhen = [{ field: 'granularity', notValues: ['page', 'packet'] }];

    renderConfig(schemaWith(showWhen), values({ granularity: '' }));
    expect(isVisible()).toBe(true);
  });

  it('hides the "not yet set" field once granularity is chosen', () => {
    const showWhen = [{ field: 'granularity', notValues: ['page', 'packet'] }];
    renderConfig(schemaWith(showWhen), values({ granularity: 'packet' }));
    expect(isVisible()).toBe(false);
  });

  it('ignores a malformed condition rather than hiding the field', () => {
    // A guard that fails closed on bad schema makes a typo invisible instead of
    // loud, and the schema is reviewed while the rendered form is not.
    renderConfig(schemaWith([{ value: 'packet' }]), values({ granularity: 'page' }));
    expect(isVisible()).toBe(true);
  });
});
