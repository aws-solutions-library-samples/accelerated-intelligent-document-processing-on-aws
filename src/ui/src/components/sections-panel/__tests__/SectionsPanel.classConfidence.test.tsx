// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Wiring test for the "Class conf." column in the Document Sections table.
 *
 * `Section.Confidence` is confidence in the section's CLASS — the minimum across
 * its pages. The contract this pins:
 *   scored     -> a badge, visually distinct from the class label beside it.
 *   NOT a link -> a section aggregate has no reasoning of its own; the per-page
 *                 explanations live in the Document Pages table. A trigger that
 *                 opened nothing would be a worse lie than plain text.
 *   not scored -> an em-dash, never "0%".
 *
 * It also pins the header wording. This table already had a "Low Confidence
 * Fields" column, which is per-EXTRACTED-FIELD confidence — a different
 * measurement. Two adjacent columns both reading "confidence" is the confusion
 * this layout exists to resolve, so the field one is "Low-conf. fields" and the
 * class one says "Class".
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, within } from '@testing-library/react';

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

const SECTIONS = [
  { Id: 'sec-scored', Class: 'W2', PageIds: [11], Confidence: 0.87 },
  { Id: 'sec-low', Class: 'BankStatement', PageIds: [12], Confidence: 0.41 },
  { Id: 'sec-unscored', Class: 'Invoice', PageIds: [13] },
];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = () =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <SectionsPanel {...({ sections: SECTIONS, pages: [], documentItem: DOC } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

const columnIndex = (header: string): number => {
  const headers = Array.from(document.querySelectorAll('th'));
  const index = headers.findIndex((th) => th.textContent?.trim().startsWith(header));
  expect(index).toBeGreaterThanOrEqual(0);
  return index;
};

const confidenceCell = (sectionId: string): HTMLElement => {
  const index = columnIndex('Class conf.');
  const row = Array.from(document.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes(sectionId));
  expect(row).toBeTruthy();
  return (row as HTMLElement).querySelectorAll('td')[index] as HTMLElement;
};

describe('Document Sections — Class conf. column', () => {
  it('has its own column, separate from Class/Type', () => {
    renderPanel();
    expect(columnIndex('Class conf.')).toBeGreaterThan(columnIndex('Class/Type'));
  });

  it('distinguishes itself from the per-field confidence column', () => {
    renderPanel();
    const headers = Array.from(document.querySelectorAll('th')).map((th) => th.textContent?.trim());
    expect(headers.some((h) => h?.startsWith('Class conf.'))).toBe(true);
    expect(headers.some((h) => h?.startsWith('Low-conf. fields'))).toBe(true);
    // The old ambiguous wording must not come back alongside the new column.
    expect(headers.some((h) => h === 'Low Confidence Fields')).toBe(false);
  });

  it('renders the score as a badge, not as a link', () => {
    renderPanel();
    const cell = confidenceCell('sec-scored');
    expect(cell.textContent).toContain('87.0%');
    // No trigger: there is no per-section reasoning to open.
    expect(within(cell).queryByRole('button')).toBeNull();
    expect(cell.querySelector('[class*="badge"]')).toBeTruthy();
  });

  it('renders a low score exactly like a high one', () => {
    renderPanel();
    const high = confidenceCell('sec-scored');
    const low = confidenceCell('sec-low');
    expect(low.textContent).toContain('41.0%');
    // Neutral at every value — no threshold is configured for class confidence,
    // so styling must not imply a verdict.
    expect(low.querySelector('[class*="badge"]')?.className).toBe(high.querySelector('[class*="badge"]')?.className);
  });

  it('shows an em-dash for an unscored section', () => {
    renderPanel();
    const cell = confidenceCell('sec-unscored');
    expect(cell.textContent).toContain('—');
    expect(cell.textContent).not.toContain('%');
  });

  it('keeps confidence out of the class label cell', () => {
    renderPanel();
    const row = Array.from(document.querySelectorAll('tbody tr')).find((tr) => tr.textContent?.includes('sec-scored')) as HTMLElement;
    const classCell = row.querySelectorAll('td')[columnIndex('Class/Type')];
    expect(classCell.textContent).toContain('W2');
    expect(classCell.textContent).not.toContain('87.0%');
  });
});
