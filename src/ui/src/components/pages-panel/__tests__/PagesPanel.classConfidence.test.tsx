// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Wiring test for the "Class conf." column in the Document Pages table.
 *
 * The presentation contract this pins:
 *   scored + detail   -> the percentage is a CLICKABLE trigger (button role) that
 *                        opens the model's reasoning and its ranked alternatives.
 *                        Clickable is the whole point: a plain number gives no
 *                        hint that "what else could this have been?" is one click
 *                        away.
 *   scored, no detail -> the percentage, not clickable — a link that opens nothing
 *                        is worse than plain text.
 *   not scored        -> an em-dash. Never "0%" (absence of a measurement is not a
 *                        measurement of zero) and never blank (which reads as a
 *                        rendering bug in a column that has values elsewhere).
 *   never coloured    -> no status/severity styling at any value; there is no
 *                        configured classification threshold, so a green/red band
 *                        would assert a verdict the system has not defined.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }),
}));
vi.mock('../../common/generate-s3-presigned-url', () => ({ default: () => Promise.resolve(null) }));

import PagesPanel from '../PagesPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';

const PAGES = [
  {
    Id: '1',
    Class: 'invoice',
    ClassConfidence: 0.95,
    ClassReason: 'Remittance block and invoice number present',
    ClassCandidates: [
      { Class: 'invoice', Probability: 0.95 },
      { Class: 'form', Probability: 0.04 },
    ],
  },
  // Scored, but the model returned neither reasoning nor candidates.
  { Id: '2', Class: 'form', ClassConfidence: 0.62 },
  // Not scored at all (mode: off, or a model that ignored the ask).
  { Id: '3', Class: 'handwritten' },
];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = () =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <PagesPanel {...({ pages: PAGES, documentItem: DOC } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

const columnIndex = (header: string): number => {
  const headers = Array.from(document.querySelectorAll('th'));
  const index = headers.findIndex((th) => th.textContent?.trim().startsWith(header));
  expect(index).toBeGreaterThanOrEqual(0);
  return index;
};

/** The Class conf. cell for the row whose Page ID cell is `pageId`. */
const confidenceCell = (pageId: string): HTMLElement => {
  const index = columnIndex('Class conf.');
  const row = Array.from(document.querySelectorAll('tbody tr')).find(
    (tr) => tr.querySelectorAll('td')[columnIndex('Page ID')]?.textContent?.trim() === pageId,
  );
  expect(row).toBeTruthy();
  return (row as HTMLElement).querySelectorAll('td')[index] as HTMLElement;
};

describe('Document Pages — Class conf. column', () => {
  it('has its own column, separate from Class/Type', () => {
    renderPanel();
    expect(columnIndex('Class conf.')).toBeGreaterThan(columnIndex('Class/Type'));
  });

  it('renders the confidence as a percentage', () => {
    renderPanel();
    expect(confidenceCell('1').textContent).toContain('95.0%');
    expect(confidenceCell('2').textContent).toContain('62.0%');
  });

  it('keeps the class label out of the confidence cell, and vice versa', () => {
    renderPanel();
    // The regression this guards: confidence used to be appended to the class
    // label, which is what made the two hard to tell apart.
    expect(confidenceCell('1').textContent).not.toContain('invoice');
    const classCell = (document.querySelectorAll('tbody tr')[0] as HTMLElement).querySelectorAll('td')[columnIndex('Class/Type')];
    expect(classCell.textContent).toContain('invoice');
    expect(classCell.textContent).not.toContain('95.0%');
  });

  it('makes a scored page with reasoning clickable, and opens the explanation', () => {
    renderPanel();
    const trigger = within(confidenceCell('1')).getByRole('button');
    expect(trigger.textContent).toContain('95.0%');

    fireEvent.click(trigger);

    expect(screen.getByText('Why this class?')).toBeTruthy();
    expect(screen.getByText(/Remittance block and invoice number present/)).toBeTruthy();
    // The ranked runner-up — the answer to "what else could this have been?".
    expect(screen.getByText(/form — 4.0%/)).toBeTruthy();
  });

  it('does not offer a click when there is nothing behind the number', () => {
    renderPanel();
    expect(within(confidenceCell('2')).queryByRole('button')).toBeNull();
    expect(confidenceCell('2').textContent).toContain('62.0%');
  });

  it('shows an em-dash for a page that was never scored', () => {
    renderPanel();
    const cell = confidenceCell('3');
    expect(cell.textContent).toContain('—');
    expect(cell.textContent).not.toContain('0');
    expect(cell.textContent).not.toContain('%');
  });

  it('applies no status colour at any value', () => {
    renderPanel();
    // Cloudscape status/severity styling would show up as a status-indicator
    // role or class; neutral rendering must use neither, at a high value or a low
    // one, because no classification threshold is configured.
    for (const pageId of ['1', '2']) {
      const cell = confidenceCell(pageId);
      expect(cell.querySelector('[class*="status-indicator"]')).toBeNull();
      expect(cell.querySelector('[class*="severity"]')).toBeNull();
    }
  });

  it('offers sorting on the column', () => {
    renderPanel();
    const header = Array.from(document.querySelectorAll('th')).find((th) =>
      th.textContent?.trim().startsWith('Class conf.'),
    ) as HTMLElement;
    // Sorting is what makes least-confident-first possible; the whole table used
    // to be sortingDisabled.
    expect(header.querySelector('button, [role="button"]')).toBeTruthy();
  });
});
