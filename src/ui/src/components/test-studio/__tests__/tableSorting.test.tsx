// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Sorting the Test Studio tables.
 *
 * Cloudscape renders the sort chevron on any column that declares a sorting
 * basis, but the sorting state belongs to the caller: a table that declares
 * `sortingField`/`sortingComparator` and passes no `onSortingChange` shows
 * chevrons on every such header and does nothing when they are clicked. Three of
 * this directory's tables were in that state (#1317), so the last test here is
 * over the directory rather than over those three — the defect is invisible in
 * review precisely because the column definition looks complete on its own.
 */

import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it } from 'vitest';

import ClassificationErrorsPanel from '../ClassificationErrorsPanel';
import { compareByAlertCount, compareByLabelSource, compareByObjectKey, compareByReviewState } from '../TestSetDetail';
import type { TestSetDocumentItem } from '../TestSetDetail';

const HERE = join(__dirname, '..');

const doc = (fields: Partial<TestSetDocumentItem>): TestSetDocumentItem => ({
  objectKey: 'doc.pdf',
  inputKey: 'input/doc.pdf',
  sections: [],
  ...fields,
});

const documentColumn = (items: readonly TestSetDocumentItem[], by: (a: TestSetDocumentItem, b: TestSetDocumentItem) => number): string[] =>
  [...items].sort(by).map((item) => item.objectKey);

describe('the classification errors table', () => {
  it('reorders its rows when a sortable header is clicked', async () => {
    const user = userEvent.setup();
    render(
      <ClassificationErrorsPanel
        classificationErrors={{
          errors: [
            { doc_key: 'c.pdf', kind: 'class', expected_class: 'Invoice', predicted_class: 'Receipt' },
            { doc_key: 'a.pdf', kind: 'class', expected_class: 'Invoice', predicted_class: 'Receipt' },
            { doc_key: 'b.pdf', kind: 'class', expected_class: 'Invoice', predicted_class: 'Receipt' },
          ],
          total: 3,
        }}
        testSetId="ts1"
      />,
    );

    const docKeys = (): string[] =>
      screen
        .getAllByRole('link')
        .map((link) => link.textContent ?? '')
        .filter((text) => text.endsWith('.pdf'));

    // The column's own sorting control: the header cells all expose role
    // "button", so matching on the header text alone is ambiguous.
    const sortByDocument = (): HTMLElement => {
      const control = document.querySelector('[data-focus-id="sorting-control-document"]');
      expect(control).not.toBeNull();
      return control as HTMLElement;
    };

    // The server's order, which is the one the panel opens in.
    expect(docKeys()).toEqual(['c.pdf', 'a.pdf', 'b.pdf']);

    await user.click(sortByDocument());
    expect(docKeys()).toEqual(['a.pdf', 'b.pdf', 'c.pdf']);

    // A second click is the descending pass, not a no-op.
    await user.click(sortByDocument());
    expect(docKeys()).toEqual(['c.pdf', 'b.pdf', 'a.pdf']);
  });
});

describe('the document comparators on the set detail page', () => {
  it('orders documents by name', () => {
    const items = [doc({ objectKey: 'b.pdf' }), doc({ objectKey: 'A.pdf' }), doc({ objectKey: 'c.pdf' })];
    expect(documentColumn(items, compareByObjectKey)).toEqual(['A.pdf', 'b.pdf', 'c.pdf']);
  });

  it('sorts a document with no alert count below one with none flagged', () => {
    // `-` and "None of 12 fields flagged" are different statements: an unknown
    // count must not rank as a clean document.
    const items = [
      doc({ objectKey: 'clean.pdf', alertCount: 0 }),
      doc({ objectKey: 'unknown.pdf' }),
      doc({ objectKey: 'bad.pdf', alertCount: 4 }),
    ];
    expect(documentColumn(items, compareByAlertCount)).toEqual(['unknown.pdf', 'clean.pdf', 'bad.pdf']);
  });

  it('orders review state by review progress, not by the raw label source', () => {
    const items = [
      doc({ objectKey: 'reviewed.pdf', labelSource: 'reviewed-human' }),
      doc({ objectKey: 'authored.pdf', labelSource: 'uploaded' }),
      doc({ objectKey: 'unlabeled.pdf' }),
      doc({ objectKey: 'awaiting.pdf', labelSource: 'draft-machine' }),
    ];
    // Ascending leads with what needs work. Alphabetically by labelSource this
    // would be draft-machine, reviewed-human, uploaded, '' — which puts the
    // unlabeled documents last, the opposite of useful in a review queue.
    expect(documentColumn(items, compareByReviewState)).toEqual(['unlabeled.pdf', 'awaiting.pdf', 'reviewed.pdf', 'authored.pdf']);
  });

  it('keeps the two renderings of labelSource on separate sorting bases', () => {
    // Cloudscape matches the sorted header by sorting field or comparator
    // identity, so sharing one `sortingField: 'labelSource'` between the
    // "Extraction labels" and "Review state" columns marked both as sorted at
    // once and left neither able to order by what it shows.
    expect(compareByLabelSource).not.toBe(compareByReviewState);
    const detail = readFileSync(join(HERE, 'TestSetDetail.tsx'), 'utf-8');
    expect(detail).not.toMatch(/sortingField: 'labelSource'/);
  });
});

describe('every sortable table in this directory', () => {
  const sources = readdirSync(HERE)
    .filter((name) => name.endsWith('.tsx'))
    .map((name) => ({ name, text: readFileSync(join(HERE, name), 'utf-8') }))
    .filter(({ text }) => /sortingField:|sortingComparator:/.test(text));

  it('has tables to check', () => {
    // A regex that matches nothing would pass every assertion below.
    expect(sources.length).toBeGreaterThan(0);
  });

  it.each(sources.map(({ name }) => name))('handles a header click in %s', (name) => {
    const { text } = sources.find((source) => source.name === name)!;
    // Either wired directly or through a collection hook's props bundle.
    expect(text).toMatch(/onSortingChange|\{\.\.\.collectionProps\}/);
  });
});
