// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The SHIPPED classification schema (`patterns/unified/template.yaml`), rendered.
 *
 * Config format 0.9 nests every classification setting inside the call that uses
 * it, and the panel shows only the calls the selected strategy makes. Those
 * gates are `showWhen` conditions resolved by a scope walk from several levels
 * down (`strategy` from inside `split_then_classify`, `method` from inside
 * `splitting.llm`, `granularity` from inside a page group), so a typo in a
 * field name or a level the walk does not reach hides a block silently. These
 * tests read the real schema rather than a copy of it for that reason.
 *
 * Every group is rendered expanded: a collapsed section omits its children from
 * the DOM for a reason that has nothing to do with the gate under test.
 */

import React from 'react';
import fs from 'node:fs';
import path from 'node:path';
import yaml from 'js-yaml';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

vi.mock('../../../hooks/use-synthetic-data-generator', () => ({ default: () => ({ available: false }) }));

import ConfigBuilder, { enumOptionLabel } from '../ConfigBuilder';

// vitest runs from src/ui.
const TEMPLATE = path.resolve(process.cwd(), '../../patterns/unified/template.yaml');

/** CloudFormation tags, read as plain data: the schema only needs their shape. */
const CFN_SCHEMA = yaml.DEFAULT_SCHEMA.extend(
  ['Ref', 'Sub', 'GetAtt', 'If', 'Equals', 'Not', 'And', 'Or', 'Join', 'Select', 'Split', 'FindInMap', 'ImportValue', 'Condition'].flatMap(
    (tag) => [
      new yaml.Type(`!${tag}`, { kind: 'scalar', construct: (data) => ({ [tag]: data }) }),
      new yaml.Type(`!${tag}`, { kind: 'sequence', construct: (data) => ({ [tag]: data }) }),
      new yaml.Type(`!${tag}`, { kind: 'mapping', construct: (data) => ({ [tag]: data }) }),
    ],
  ),
);

const expandAll = (node: unknown): unknown => {
  if (Array.isArray(node)) return node.map(expandAll);
  if (!node || typeof node !== 'object') return node;
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(node)) {
    out[key] = key === 'collapsible' ? false : expandAll(value);
  }
  // An enum entry that is a CloudFormation `!If` is not a string; drop it so the
  // dropdown renders the literal model IDs.
  if (Array.isArray(out.enum)) out.enum = (out.enum as unknown[]).filter((v) => typeof v === 'string');
  return out;
};

const classificationSchema = (() => {
  const doc = yaml.load(fs.readFileSync(TEMPLATE, 'utf8'), { schema: CFN_SCHEMA }) as {
    Resources: { UpdateSchemaConfig: { Properties: { Schema: { properties: { classification: Record<string, unknown> } } } } };
  };
  const cls = doc.Resources.UpdateSchemaConfig.Properties.Schema.properties.classification;
  return expandAll({ ...cls, dependsOn: undefined }) as Record<string, unknown>;
})();

const renderClassification = (classification: Record<string, unknown>) =>
  render(
    <MemoryRouter>
      <ConfigBuilder schema={{ type: 'object', properties: { classification: classificationSchema } }} formValues={{ classification }} />
    </MemoryRouter>,
  );

const visible = (text: string) => screen.queryAllByText(text).length > 0;

describe('the shipped classification schema', () => {
  it('shows the combined block, and not the split-then-classify one, for combined', () => {
    renderClassification({ strategy: 'combined', combined: { granularity: 'page' } });
    expect(visible('Split and classify together')).toBe(true);
    expect(visible('Splitting')).toBe(false);
    expect(visible('Section classification')).toBe(false);
  });

  it('shows the combined block when no strategy is stored, which is the default', () => {
    renderClassification({});
    expect(visible('Split and classify together')).toBe(true);
    expect(visible('Splitting')).toBe(false);
  });

  it('shows both split-then-classify calls, and not the combined block, for split_then_classify', () => {
    renderClassification({
      strategy: 'split_then_classify',
      split_then_classify: { splitting: { method: 'llm' }, section_classification: { method: 'llm' } },
    });
    expect(visible('Splitting')).toBe(true);
    // Each split-then-classify call carries its own Inference settings group.
    expect(screen.queryAllByText('Inference settings')).toHaveLength(2);
    expect(visible('Split and classify together')).toBe(false);
  });

  it('gates page and packet settings on the granularity of their own call', () => {
    renderClassification({ strategy: 'combined', combined: { granularity: 'packet' } });
    expect(visible('Window size (pages)')).toBe(true);
    expect(visible('Context pages')).toBe(false);
  });

  it('gives section classification validation and confidence, and splitting neither', () => {
    renderClassification({
      strategy: 'split_then_classify',
      split_then_classify: { splitting: { method: 'llm', llm: {} }, section_classification: { method: 'llm', llm: {} } },
    });
    // One "Class validation" and one "Class confidence" group: section classification's.
    expect(screen.getAllByText('Class validation')).toHaveLength(1);
    expect(screen.getAllByText('Class confidence')).toHaveLength(1);
    // But a model picker on both calls.
    expect(screen.getAllByText('Model', { exact: true }).length).toBeGreaterThanOrEqual(2);
  });

  it('shows Pages to classify only for a one-document file', () => {
    const { unmount } = renderClassification({ document_shape: { sectionSplitting: 'llm_determined' } });
    expect(visible('Pages to classify')).toBe(false);
    unmount();
    renderClassification({ document_shape: { sectionSplitting: 'disabled' } });
    expect(visible('Pages to classify')).toBe(true);
  });

  it('labels the strategy values without changing what is stored', () => {
    const strategy = (classificationSchema.properties as Record<string, Record<string, unknown>>).strategy;
    expect(strategy.enum).toEqual(['combined', 'split_then_classify']);
    expect(enumOptionLabel(strategy, 'combined')).toBe('Split and classify together (one call)');
    expect(enumOptionLabel(strategy, 'split_then_classify')).toBe('Split first, then classify each section');
  });
});

describe('enumOptionLabel', () => {
  it('falls back to the value itself', () => {
    expect(enumOptionLabel({}, 'page')).toBe('page');
    expect(enumOptionLabel({ enumLabels: { other: 'Other' } }, 'page')).toBe('page');
  });
});
