// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The SHIPPED extraction schema (`patterns/unified/template.yaml`), rendered.
 *
 * Multi-instance Section Detection runs in Simple extraction and in Advanced
 * extraction (`ExtractionService._build_wire_schema` and
 * `ExtractionService._agentic_probe_model`), so its panel must be reachable in
 * both modes. It was gated on `extraction.mode == simple`, which hid a setting
 * that works in Advanced mode from every Advanced-mode user (#1925). BDA mode
 * runs no extraction step, and the whole Extraction section is hidden there by
 * its own `use_bda` gate. These tests read the real schema rather than a copy,
 * because the defect was a value in that schema.
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

import ConfigBuilder from '../ConfigBuilder';

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

// The extraction section keeps its own `use_bda` gate, so BDA mode is tested
// through the schema as shipped rather than by assumption.
const extractionSchema = (() => {
  const doc = yaml.load(fs.readFileSync(TEMPLATE, 'utf8'), { schema: CFN_SCHEMA }) as {
    Resources: { UpdateSchemaConfig: { Properties: { Schema: { properties: { extraction: Record<string, unknown> } } } } };
  };
  return expandAll(doc.Resources.UpdateSchemaConfig.Properties.Schema.properties.extraction) as Record<string, unknown>;
})();

const PANEL = 'Multi-instance Section Detection';

const renderExtraction = (extraction: Record<string, unknown>, useBda = false) =>
  render(
    <MemoryRouter>
      <ConfigBuilder
        schema={{ type: 'object', properties: { extraction: extractionSchema } }}
        formValues={{ use_bda: useBda, extraction }}
      />
    </MemoryRouter>,
  );

const visible = (text: string) => screen.queryAllByText(text).length > 0;

describe('the shipped extraction schema: Multi-instance Section Detection', () => {
  it('is shown in Simple mode', () => {
    renderExtraction({ mode: 'simple', multi_instance_detection: { enabled: false } });
    expect(visible(PANEL)).toBe(true);
  });

  it('is shown in Advanced mode, which runs detection on its unsharded call', () => {
    renderExtraction({ mode: 'advanced', agentic: { enabled: true }, multi_instance_detection: { enabled: false } });
    expect(visible(PANEL)).toBe(true);
  });

  it('is not shown in BDA mode, which runs no extraction step', () => {
    renderExtraction({ mode: 'simple', multi_instance_detection: { enabled: false } }, true);
    expect(visible(PANEL)).toBe(false);
  });
});
