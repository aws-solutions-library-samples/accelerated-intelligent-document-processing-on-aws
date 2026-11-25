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
 * saying so. The component's headings are react-intl messages, so most
 * counterparts match the English a message declares (its `defaultMessage`).
 *
 * Consequently a heading rename on the Python side fails THIS test, not a Python
 * one, and the UI suite needs the full repository checkout, not `src/ui` alone. When
 * that happens: update the key in COUNTERPARTS below to the new heading text (and
 * the component, if the section's content changed). The failure message names the
 * heading that has no counterpart.
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
  '## Summary': /defaultMessage: 'Extraction accuracy'/,
  '**Document Split Classification:**': /defaultMessage: 'Split accuracy'/,
  '## Excluded Sections (Not Evaluated)': /# sections not evaluated\}\}'/,
  '## Overall Metrics': /defaultMessage: 'All metrics'/,
  '### Document Split Classification Metrics': /defaultMessage: 'Document split classification'/,
  '### Document Extraction Metrics': /defaultMessage: 'Document extraction'/,
  '## 📑 Section Split Analysis': /defaultMessage: 'Section split analysis'/,
  '### ⚠️ Doc Split Errors': /defaultMessage: 'Doc split errors'/,
  '## Extraction Attribute Evaluation': /<AttributeTable rows=/,
  '### Section:': /defaultMessage: 'Section \{sectionId\} — \{className\}'/,
  '#### Attributes': /const AttributeTable/,
  '⚠️ **EVALUATION FAILED**': /defaultMessage: 'This section was not evaluated'/,
  '**How to fix:**': /defaultMessage: 'How to fix'/,
  '### Metrics (Failure State)': /placeholders for a section that was\s+never scored/,
  '#### Metrics': /defaultMessage: 'Section metrics'/,
  'Nested Field Comparisons': /expandableRows=\{\{/,
  'field(s) were excluded from scoring': /excluded from scoring because/,
  '## Evaluation Methods Used': /defaultMessage: 'Comparison methods used'/,
  '### Field-Level Comparison Methods': /defaultMessage: 'Field-level comparison methods'/,
  '### Array-Level Matching': /defaultMessage: 'Array-level matching'/,
  '### Field Weighting': /defaultMessage: 'Field weighting'/,
  '## 📖 Metrics Explanation': /defaultMessage: 'Split metrics'/,
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
      expect(COMPONENT).toMatch(new RegExp(`defaultMessage: '${column}'`));
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
