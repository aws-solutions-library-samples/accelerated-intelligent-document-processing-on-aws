// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Few-shot example field keys: read either spelling, write the canonical one.
 *
 * The examples container has always been `x-aws-idp-examples`, but this editor
 * used to store each entry's fields under the legacy camelCase names
 * (`classPrompt` / `attributesPrompt` / `imagePath`) — the spelling the docs never
 * described. Editing must therefore display legacy configs correctly while
 * emitting the canonical `x-aws-idp-*` keys (and dropping the legacy alias, so an
 * example never carries both).
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ExamplesEditor from '../constraints/ExamplesEditor';

const LEGACY_EXAMPLE = {
  id: 'e1',
  name: 'Letter1',
  classPrompt: 'legacy class body',
  attributesPrompt: 'legacy attrs body',
  imagePath: 's3://bucket/letter1.jpg',
};

const CANONICAL_EXAMPLE = {
  id: 'e1',
  name: 'Letter1',
  'x-aws-idp-class-prompt': 'canonical class body',
  'x-aws-idp-attributes-prompt': 'canonical attrs body',
  'x-aws-idp-image-path': 's3://bucket/letter1.jpg',
};

/** Render with the single example expanded so its fields are in the DOM. */
const renderExpanded = async (example: Record<string, unknown>) => {
  const onChange = vi.fn();
  render(<ExamplesEditor examples={[example]} onChange={onChange} />);
  await userEvent.click(screen.getByText('Letter1'));
  return onChange;
};

describe('ExamplesEditor example field keys', () => {
  it('displays values stored under the legacy camelCase keys', async () => {
    await renderExpanded(LEGACY_EXAMPLE);

    expect(screen.getByDisplayValue('legacy class body')).toBeInTheDocument();
    expect(screen.getByDisplayValue('legacy attrs body')).toBeInTheDocument();
    expect(screen.getByDisplayValue('s3://bucket/letter1.jpg')).toBeInTheDocument();
  });

  it('displays values stored under the canonical keys', async () => {
    await renderExpanded(CANONICAL_EXAMPLE);

    expect(screen.getByDisplayValue('canonical class body')).toBeInTheDocument();
    expect(screen.getByDisplayValue('canonical attrs body')).toBeInTheDocument();
  });

  it('writes the canonical key and drops the legacy alias when edited', async () => {
    const onChange = await renderExpanded(LEGACY_EXAMPLE);

    await userEvent.type(screen.getByDisplayValue('legacy attrs body'), '!');

    expect(onChange).toHaveBeenCalled();
    const [updated] = onChange.mock.lastCall as [Record<string, unknown>[]];
    expect(updated[0]['x-aws-idp-attributes-prompt']).toBe('legacy attrs body!');
    expect(updated[0]).not.toHaveProperty('attributesPrompt');
    // Untouched fields keep whatever spelling they already had.
    expect(updated[0].classPrompt).toBe('legacy class body');
  });

  it('creates new examples with canonical keys only', async () => {
    const onChange = vi.fn();
    render(<ExamplesEditor examples={[]} onChange={onChange} />);

    await userEvent.click(screen.getByRole('button', { name: /Add Example/i }));

    const [created] = onChange.mock.lastCall as [Record<string, unknown>[]];
    expect(Object.keys(created[0]).sort()).toEqual([
      'id',
      'name',
      'x-aws-idp-attributes-prompt',
      'x-aws-idp-class-prompt',
      'x-aws-idp-image-path',
    ]);
  });
});
