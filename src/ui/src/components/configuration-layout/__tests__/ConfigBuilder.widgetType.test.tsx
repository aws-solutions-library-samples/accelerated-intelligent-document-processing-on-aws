// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which widget the config form gives a field is decided by the schema, and one
 * heuristic in that decision used to override the schema.
 *
 * Free-text fields get a roomy textarea, and the rule for "free text" includes a
 * PATH-NAME test (`prompt` / `description` anywhere in the path) so that a prompt
 * template gets one without every prompt in every schema having to declare
 * `format: textarea`. That test looked at the name before the declared `type`, so
 * a boolean whose key merely ENDS in "prompt" rendered as a free-text box:
 * `extraction.forced_tool.fallback_to_prompt` (Schema Enforcement) and
 * `extraction.agentic.restate_schema_in_system_prompt` both shipped that way,
 * while every other true/false setting is a toggle.
 *
 * That is worse than cosmetic. The field then stores a STRING, and the backend
 * config model only accepts the spellings pydantic reads as a bool — so `off`
 * happened to work while an empty box, `disabled`, or `yes ` with a trailing space
 * is a config ValidationError rather than a setting.
 *
 * These are the two real schema nodes from `patterns/unified/template.yaml`.
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

/** A one-property schema section, so each case renders exactly one field. */
const sectionWith = (key: string, property: Record<string, unknown>) => ({
  type: 'object',
  properties: {
    extraction: {
      type: 'object',
      sectionLabel: 'Extraction',
      properties: { [key]: property },
    },
  },
});

/**
 * The widgets inside ONE field's FormField, found by its label. Scoped rather than
 * document-wide because the page chrome around the form has inputs of its own, and
 * a document-wide count would fail for reasons unrelated to the field under test.
 */
const widgetKinds = (label: string): { toggles: number; textareas: number; inputs: number } => {
  const field = screen.getByText(label).closest('.compact-form-field');
  if (!field) throw new Error(`no field rendered for ${label}`);
  return {
    toggles: field.querySelectorAll('input[type="checkbox"]').length,
    textareas: field.querySelectorAll('textarea').length,
    inputs: field.querySelectorAll('input[type="text"], input[type="number"]').length,
  };
};

describe('ConfigBuilder widget selection', () => {
  it('renders fallback_to_prompt as a toggle, not a text box', () => {
    renderConfig(
      sectionWith('fallback_to_prompt', {
        type: 'boolean',
        description: 'If the model accepts the tool but replies in prose anyway, read the prose reply as usual.',
        default: true,
      }),
      { extraction: { fallback_to_prompt: true } },
    );

    expect(widgetKinds('Fallback To Prompt')).toEqual({ toggles: 1, textareas: 0, inputs: 0 });
  });

  it('renders restate_schema_in_system_prompt as a toggle, not a text box', () => {
    renderConfig(
      sectionWith('restate_schema_in_system_prompt', {
        type: 'boolean',
        description: "Repeat the document schema in the agent's system prompt.",
        default: true,
      }),
      { extraction: { restate_schema_in_system_prompt: true } },
    );

    expect(widgetKinds('Restate Schema In System Prompt')).toEqual({ toggles: 1, textareas: 0, inputs: 0 });
  });

  it('reflects the stored value, so a toggle cannot read as ON while the config says off', () => {
    renderConfig(sectionWith('fallback_to_prompt', { type: 'boolean', default: true }), {
      extraction: { fallback_to_prompt: false },
    });
    const field = screen.getByText('Fallback To Prompt').closest('.compact-form-field');
    expect(field?.querySelector('input[type="checkbox"]')).not.toBeChecked();
  });

  it('still gives an actual prompt TEMPLATE a textarea', () => {
    // The heuristic is the reason no prompt in the schema has to declare
    // `format: textarea`, so narrowing it must not cost that.
    renderConfig(sectionWith('task_prompt', { type: 'string', description: 'The extraction task prompt.' }), {
      extraction: { task_prompt: 'Extract {DOCUMENT_CLASS}' },
    });

    expect(widgetKinds('Task Prompt')).toEqual({ toggles: 0, textareas: 1, inputs: 0 });
  });

  it('gives a numeric field whose name contains "prompt" a number input, not a textarea', () => {
    renderConfig(sectionWith('prompt_retries', { type: 'number', minimum: 0, maximum: 5 }), {
      extraction: { prompt_retries: 3 },
    });

    expect(widgetKinds('Prompt Retries')).toEqual({ toggles: 0, textareas: 0, inputs: 1 });
    const field = screen.getByText('Prompt Retries').closest('.compact-form-field');
    expect(field?.querySelector('input[type="number"]')).toBeTruthy();
  });
});
