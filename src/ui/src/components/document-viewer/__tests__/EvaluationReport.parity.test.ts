// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The React evaluation report must show everything the markdown report shows.
 *
 * The component shipped covering the headline scores and the field tables, with
 * a "Markdown report" button standing in for the rest — document-split
 * analysis, section-level scores, excluded sections with reasons, weighting and
 * array-matching detail — and nested values flattened to one JSON string. This
 * test reads the generator's own section headings out of
 * `idp_common/evaluation/models.py` and requires each to have a counterpart in
 * the component source, so the two cannot drift apart again without a test
 * saying so.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const COMPONENT = readFileSync(join(HERE, 'EvaluationReport.tsx'), 'utf-8');
const GENERATOR = readFileSync(
  join(HERE, '..', '..', '..', '..', '..', 'lib', 'idp_common_pkg', 'idp_common', 'evaluation', 'models.py'),
  'utf-8',
);
const markdownBody = GENERATOR.slice(GENERATOR.indexOf('def to_markdown'), GENERATOR.indexOf('return "\\n".join(sections)'));

/**
 * Each heading the generator emits, and the string that proves the component
 * renders that section. Adding a heading to the generator without an entry
 * here fails the first test; removing a counterpart from the component fails
 * the second.
 */
const COUNTERPARTS: Record<string, RegExp> = {
  '## Summary': /label="Extraction accuracy"/,
  '**Document Split Classification:**': /label="Split accuracy"/,
  '## Excluded Sections (Not Evaluated)': /not evaluated`}/,
  '## Overall Metrics': /headerText="All metrics"/,
  '### Document Split Classification Metrics': /Document split classification<\/Box>/,
  '### Document Extraction Metrics': /Document extraction<\/Box>/,
  '## 📑 Section Split Analysis': /headerText="Section split analysis"/,
  '### ⚠️ Doc Split Errors': /header="Doc split errors"/,
  '## Extraction Attribute Evaluation': /<AttributeTable rows=/,
  '### Section:': /headerText=\{`Section \$\{section\.section_id\}/,
  '#### Attributes': /const AttributeTable/,
  '⚠️ **EVALUATION FAILED**': /header="This section was not evaluated"/,
  '**How to fix:**': /How to fix<\/Box>/,
  '### Metrics (Failure State)': /placeholders for a section that was\s+never scored/,
  '#### Metrics': /headerText="Section metrics"/,
  'Nested Field Comparisons': /expandableRows=\{\{/,
  'field(s) were excluded from scoring': /excluded from scoring because/,
  '## Evaluation Methods Used': /headerText="Comparison methods used"/,
  '### Field-Level Comparison Methods': /Field-level comparison methods<\/Box>/,
  '### Array-Level Matching': /Array-level matching<\/Box>/,
  '### Field Weighting': /Field weighting<\/Box>/,
  '## 📖 Metrics Explanation': /Split metrics<\/Box>/,
  '### Page Level Accuracy': /<b>Page-level accuracy<\/b>/,
  '### Document Split Accuracy (Without Page Order)': /<b>Split accuracy \(without order\)<\/b>/,
  '### Document Split Accuracy (With Page Order)': /<b>Split accuracy \(with order\)<\/b>/,
  'Execution time:': /Evaluation took \{executionTime\}/,
};

describe('evaluation report parity with the markdown generator', () => {
  it('knows every heading the generator emits', () => {
    const headings = [...markdownBody.matchAll(/sections\.append\(\s*f?"((?:## |### |#### )[^"]+)"/g)]
      .map((m) => m[1])
      .map((h) => h.replace(/\{[^}]*\}.*$/, '').trim());
    const covered = Object.keys(COUNTERPARTS);
    const missing = headings.filter((h) => !covered.some((c) => h.startsWith(c) || c.startsWith(h)));
    expect(missing, `generator headings with no counterpart declared: ${missing.join(' | ')}`).toEqual([]);
  });

  it('renders a counterpart for each section', () => {
    for (const [heading, pattern] of Object.entries(COUNTERPARTS)) {
      expect(GENERATOR, `generator no longer emits ${heading}`).toContain(heading);
      expect(COMPONENT, `component has no counterpart for ${heading}`).toMatch(pattern);
    }
  });

  it('shows the columns the markdown attribute table has', () => {
    for (const column of ['Confidence', 'Score', 'Weight', 'Method', 'Expected', 'Extracted']) {
      expect(COMPONENT).toMatch(new RegExp(`header: '${column}'`));
    }
  });

  it('no longer flattens a nested value into one JSON string', () => {
    expect(COMPONENT).not.toMatch(/JSON\.stringify\(value\)/);
    expect(COMPONENT).toMatch(/const StructuredValue/);
  });

  it('rates a never-evaluated section as not scored, not as poor', () => {
    // The markdown prints "❌ Failed" beside those zeros; painting them red "Poor"
    // contradicts the alert above them that calls them placeholders.
    expect(COMPONENT).toMatch(/notScored=\{failure !== null\}/);
  });

  it('keeps the markdown one click away', () => {
    expect(COMPONENT).toMatch(/Markdown report/);
  });
});
