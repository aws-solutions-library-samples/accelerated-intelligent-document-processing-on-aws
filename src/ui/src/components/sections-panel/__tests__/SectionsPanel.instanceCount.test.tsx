// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Wiring test for the "Instances" column in the Document Sections table.
 *
 * `Section.InstanceCount` is how many separate documents of the section's Class
 * extraction found in it. The presentation contract this pins:
 *   > 1        -> emphasised, and explains itself on hover (a section holding
 *                 several documents is the thing a user must notice).
 *   1          -> present but quiet; the normal case must not draw the eye.
 *   0 / absent -> undetermined (older documents, or extraction that failed
 *                 before producing a result). Renders "-", never "0", and never
 *                 as a problem — reading it as an alert would flag every
 *                 document processed before the field existed.
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
];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = () =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <SectionsPanel {...({ sections: SECTIONS, pages: [], documentItem: DOC } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

/** Column index of the "Instances" header, resolved from the rendered table. */
const instancesColumnIndex = (): number => {
  const headers = Array.from(document.querySelectorAll('th'));
  const index = headers.findIndex((th) => th.textContent?.trim() === 'Instances');
  expect(index).toBeGreaterThanOrEqual(0);
  return index;
};

/** The Instances cell for the row containing the given section Id. */
const instancesCell = (sectionId: string): HTMLElement => {
  const row = screen.getByText(sectionId).closest('tr');
  expect(row).not.toBeNull();
  const cell = row!.querySelectorAll('td')[instancesColumnIndex()];
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

describe('SectionsPanel Instances column', () => {
  it('renders the column', () => {
    renderPanel();
    expect(screen.getByText('Instances')).toBeInTheDocument();
  });

  it('emphasises a multi-instance section and explains it on hover', () => {
    renderPanel();

    const cell = instancesCell('sec-multi');
    expect(cell.textContent).toBe('3');

    // The count alone is cryptic, so >1 is the only state that carries an
    // interactive explanation. Opening it is also what distinguishes the
    // emphasised state from the quiet ones without asserting on Cloudscape's
    // hashed style class names.
    expect(screen.queryByText(/Multiple documents in one section/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('3'));
    expect(screen.getByText(/Multiple documents in one section/)).toBeInTheDocument();
    expect(screen.getByText(/3 separate BankStatement documents/)).toBeInTheDocument();
  });

  it('renders a single-instance section quietly', () => {
    renderPanel();

    const cell = instancesCell('sec-single');
    expect(cell.textContent).toBe('1');
    // No popover trigger, and nothing that reads as a status/alert.
    fireEvent.click(cell);
    expect(screen.queryByText(/Multiple documents in one section/)).not.toBeInTheDocument();
  });

  it('renders an undetermined count as blank, not as "0" and not as an alert', () => {
    renderPanel();

    for (const sectionId of ['sec-zero', 'sec-absent']) {
      const cell = instancesCell(sectionId);
      expect(cell.textContent).toBe('-');
      expect(cell.textContent).not.toContain('0');
      expect(cell.textContent).not.toMatch(/instance/i);
      // Cloudscape renders a StatusIndicator with a role="img" icon; an
      // undetermined count must not produce one.
      expect(cell.querySelector('[role="img"]')).toBeNull();
    }
  });
});
