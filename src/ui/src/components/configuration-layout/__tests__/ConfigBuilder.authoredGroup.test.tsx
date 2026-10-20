// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * An authored group (`sectionLabel`) renders even when its own key is absent
 * from the stored configuration.
 *
 * The form writes nothing into a nested object until one of its fields is
 * edited, and `updateValue` deletes the object again once its last field is
 * cleared — so "key absent" is an ordinary state rather than an error. Hiding
 * the group then makes every field inside it unreachable, with no control to
 * click and nothing saying why. `classification.modality` is permanently in that
 * state: both its flags default to empty, so nothing ever writes the object.
 *
 * ⚠️ **The two tests that matter here are the ones NOT about Modality.** The gate
 * is `sectionLabel` itself, so the behaviour applies to every authored group in
 * the schema — 58 of them in `patterns/unified/template.yaml` — and that breadth
 * is deliberate rather than incidental: the reasoning above holds identically
 * for all of them, so a per-group opt-in flag would just be the same bug waiting
 * on whichever group was written next without one. If someone narrows the gate to
 * a flag, `renders an authored group the classification panel knows nothing
 * about` is the test that fails.
 *
 * A plain object group with no `sectionLabel` keeps the old behaviour and stays
 * hidden when absent — that is the other half, and without it this test would
 * pass on a build that simply rendered every object unconditionally.
 */

import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../../hooks/use-synthetic-data-generator', () => ({ default: () => ({ available: false }) }));

import ConfigBuilder from '../ConfigBuilder';

const renderConfig = (schema: Record<string, unknown>, formValues: Record<string, unknown>) =>
  render(
    <MemoryRouter>
      <ConfigBuilder schema={schema} formValues={formValues} />
    </MemoryRouter>,
  );

const groupSchema = (groupName: string, extras: Record<string, unknown> = {}) => ({
  type: 'object',
  properties: {
    classification: {
      type: 'object',
      sectionLabel: 'Classification',
      properties: {
        [groupName]: {
          type: 'object',
          sectionLabel: 'Group label',
          collapsible: false,
          ...extras,
          properties: {
            inner: { type: 'string', title: 'Inner field' },
          },
        },
      },
    },
  },
});

describe('authored groups render when their key is absent', () => {
  it('renders Modality with no modality object in the configuration', () => {
    renderConfig(groupSchema('modality'), { classification: {} });
    expect(screen.getByText('Inner field')).toBeInTheDocument();
  });

  it('renders an authored group the classification panel knows nothing about', () => {
    // The name is arbitrary on purpose: nothing in the component may special-case
    // which groups get this treatment.
    renderConfig(groupSchema('someGroupAddedLater'), { classification: {} });
    expect(screen.getByText('Inner field')).toBeInTheDocument();
  });

  it('renders an authored group at the top level of the form', () => {
    renderConfig(
      {
        type: 'object',
        properties: {
          topLevelGroup: {
            type: 'object',
            sectionLabel: 'Group label',
            collapsible: false,
            properties: { inner: { type: 'string', title: 'Inner field' } },
          },
        },
      },
      {},
    );
    expect(screen.getByText('Inner field')).toBeInTheDocument();
  });

  it('still renders the group once its key exists', () => {
    renderConfig(groupSchema('modality'), { classification: { modality: { inner: 'x' } } });
    expect(screen.getByText('Inner field')).toBeInTheDocument();
  });

  it('keeps hiding a plain object group whose key is absent', () => {
    // The other half: without this, a build that rendered every object group
    // unconditionally would pass every test above.
    renderConfig(
      {
        type: 'object',
        properties: {
          plainGroup: {
            type: 'object',
            properties: { inner: { type: 'string', title: 'Inner field' } },
          },
        },
      },
      {},
    );
    expect(screen.queryByText('Inner field')).not.toBeInTheDocument();
  });
});

describe('editing a field inside an absent authored group', () => {
  /**
   * ⚠️ Rendering the group is only half the fix. If `updateValue` could not
   * create the missing parent object, the control would render and the edit
   * would go nowhere — a worse failure than the invisible group, because it
   * looks like it worked. This asserts the parent is created.
   */
  it('creates the parent object on the first edit', () => {
    const changes: Record<string, unknown>[] = [];
    render(
      <MemoryRouter>
        <ConfigBuilder schema={groupSchema('modality')} formValues={{ classification: {} }} onChange={(v) => changes.push(v)} />
      </MemoryRouter>,
    );
    const inputs = screen.getAllByRole('textbox');
    // The deepest control is the group's own field; the outer ones belong to the
    // enclosing section's chrome.
    fireEvent.change(inputs[inputs.length - 1], { target: { value: 'true' } });

    expect(changes.length).toBeGreaterThan(0);
    const last = changes[changes.length - 1] as { classification?: { modality?: { inner?: string } } };
    expect(last.classification?.modality?.inner).toBe('true');
  });
});
