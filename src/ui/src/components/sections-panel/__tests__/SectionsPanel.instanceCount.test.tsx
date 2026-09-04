// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Wiring test for the multi-instance annotation in the Document Sections table.
 *
 * `Section.InstanceCount` is how many separate documents of the section's Class
 * extraction found in it. It used to have its own column; it is now a badge inside
 * the Class/Type cell, shown ONLY when the count exceeds 1. The presentation
 * contract this pins:
 *   > 1          -> a badge beside the class name, explaining itself on hover (a
 *                   section holding several documents is the thing a user must
 *                   notice).
 *   1, 0, absent -> NOTHING. A column rendered "1" on every row of a normal
 *                   document and cost width the table did not have — it wrapped
 *                   its own header and pushed Actions off the panel.
 *
 * This drops one distinction on purpose: the old column separated "1"
 * (determined) from "-" (undetermined, i.e. older documents or extraction that
 * failed before producing a result). Neither is actionable in this table, and both
 * remain on the API and in the Processing Report.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }),
}));

import SectionsPanel from '../SectionsPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';
import { getDocument, getDocumentVersion, listDocumentsByDateRange, onUpdateDocument } from '../../../graphql/generated';

// One section per case. Page IDs are chosen so no page number collides with an
// instance count, keeping the per-cell assertions unambiguous.
const SECTIONS = [
  { Id: 'sec-single', Class: 'W2', PageIds: [11], InstanceCount: 1 },
  { Id: 'sec-multi', Class: 'BankStatement', PageIds: [12, 13], InstanceCount: 3 },
  { Id: 'sec-zero', Class: 'Invoice', PageIds: [14], InstanceCount: 0 },
  { Id: 'sec-absent', Class: 'Payslip', PageIds: [15] },
  // #753: the count came from the model's own answer, and the extra documents
  // were NOT extracted — the wording for the "recovered" case would be untrue.
  {
    Id: 'sec-suspected',
    Class: 'PayStatement',
    PageIds: [16, 17],
    InstanceCount: 5,
    ProcessingIssues: [
      {
        stage: 'extraction',
        severity: 'warning',
        code: 'extraction_multi_instance_suspected',
        message: 'These pages appear to contain 5 separate PayStatement documents, but extraction returned 1.',
      },
    ],
  },
];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = () =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <SectionsPanel {...({ sections: SECTIONS, pages: [], documentItem: DOC } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

/** Column index of a header, resolved from the rendered table. */
const columnIndex = (header: string): number => {
  const headers = Array.from(document.querySelectorAll('th'));
  const index = headers.findIndex((th) => th.textContent?.trim() === header);
  expect(index).toBeGreaterThanOrEqual(0);
  return index;
};

/** The Class/Type cell for the row containing the given section Id. */
const classCell = (sectionId: string): HTMLElement => {
  const row = screen.getByText(sectionId).closest('tr');
  expect(row).not.toBeNull();
  const cell = row!.querySelectorAll('td')[columnIndex('Class/Type')];
  expect(cell).toBeTruthy();
  return cell as HTMLElement;
};

describe('InstanceCount GraphQL selection sets', () => {
  // The selection set is the silently-breakable link: dropping InstanceCount
  // from a .graphql document raises no type error and would leave the column
  // permanently blank with every other test green. This exact regression
  // already shipped once for confidence alerts (see CHANGELOG).
  it.each([
    ['getDocument', getDocument],
    ['getDocumentVersion', getDocumentVersion],
    ['listDocumentsByDateRange', listDocumentsByDateRange],
    ['onUpdateDocument', onUpdateDocument],
  ])('%s requests Sections.InstanceCount', (_name, operation) => {
    expect(String(operation)).toContain('InstanceCount');
  });
});

describe('SectionsPanel multi-instance annotation', () => {
  it('has no Instances column', () => {
    renderPanel();
    const headers = Array.from(document.querySelectorAll('th')).map((th) => th.textContent?.trim());
    expect(headers).not.toContain('Instances');
  });

  it('emphasises a multi-instance section beside its class, and explains it on hover', () => {
    renderPanel();

    const cell = classCell('sec-multi');
    expect(cell.textContent).toContain('BankStatement');
    expect(cell.textContent).toContain('3');

    // The count alone is cryptic, so >1 is the only state that carries an
    // interactive explanation. Opening it is also what distinguishes the
    // emphasised state from the quiet ones without asserting on Cloudscape's
    // hashed style class names.
    expect(screen.queryByText(/Multiple documents in one section/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('3'));
    expect(screen.getByText(/Multiple documents in one section/)).toBeInTheDocument();
    expect(screen.getByText(/3 separate BankStatement documents/)).toBeInTheDocument();
  });

  it('says nothing at all for a single-instance section', () => {
    renderPanel();

    const cell = classCell('sec-single');
    // Just the class name — the normal case must not spend a pixel.
    expect(cell.textContent).toBe('W2');
    fireEvent.click(cell);
    expect(screen.queryByText(/Multiple documents in one section/)).not.toBeInTheDocument();
  });

  it('says nothing for an undetermined count either', () => {
    renderPanel();

    for (const [sectionId, className] of [
      ['sec-zero', 'Invoice'],
      ['sec-absent', 'Payslip'],
    ]) {
      const cell = classCell(sectionId);
      // No count, no "0", and nothing that reads as a status or a problem: an
      // undetermined count is what every document processed before the field
      // existed has, and it is not news.
      expect(cell.textContent).toBe(className);
      expect(cell.textContent).not.toContain('0');
      expect(cell.querySelector('[role="img"]')).toBeNull();
    }
  });
});

describe('SectionsPanel suspected multi-instance annotation (#753)', () => {
  it('does not claim the extra documents were extracted', () => {
    renderPanel();

    const cell = classCell('sec-suspected');
    expect(cell.textContent).toContain('PayStatement');
    expect(cell.textContent).toContain('5');

    fireEvent.click(screen.getByText('5'));
    expect(screen.getByText(/Documents may be missing from this section/)).toBeInTheDocument();
    expect(screen.getByText(/only the first was extracted/)).toBeInTheDocument();
    // The "recovered" wording asserts every instance was kept, which is false here.
    expect(screen.queryByText(/Each one was extracted as its own instance/)).not.toBeInTheDocument();
  });

  it('still uses the factual wording when the count is a real recovered count', () => {
    renderPanel();
    fireEvent.click(screen.getByText('3'));
    expect(screen.getByText(/Multiple documents in one section/)).toBeInTheDocument();
    expect(screen.queryByText(/only the first was extracted/)).not.toBeInTheDocument();
  });
});
