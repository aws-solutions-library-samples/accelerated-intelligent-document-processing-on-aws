// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A Reviewer-only user cannot change a section's class on the page-grouping board
 * once the document's review has finished, whichever flow finished it.
 *
 * The Sections panel's Reviewer-only lock gates the board's class control. As in the
 * Pages panel, the case that matters is a Reviewer still holding a legacy claim: a
 * queue review that ended `complete` or `rejected` used to leave the control usable,
 * because the lock knew only the legacy completed and skipped spellings. The document
 * item goes through the same mapping the document page uses.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../graphql/generated', () => ({
  updateDocumentSections: 'updateDocumentSections',
  processChanges: 'processChanges',
  skipAllSectionsReview: 'skipAllSectionsReview',
}));
vi.mock('../../../hooks/use-page-thumbnails', () => ({
  default: () => ({ urls: { '1': 'blob:1' }, failure: null }),
  THUMBNAIL_FAILURE_MESSAGES: { 'access-denied': 'access denied', failed: 'failed' },
}));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ canWrite: false, canReview: true, isReviewerOnly: true, canWorkReviewQueues: false }),
}));
vi.mock('../../../contexts/document-version', () => ({
  useDocumentVersion: () => ({ versionIdForUri: () => undefined, runId: null, isHistorical: false }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { IDPPattern: 'Pattern 2' } }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));

import SectionsPanel from '../SectionsPanel';
import mapDocumentsAttributes from '../../common/map-document-attributes';
import { LegacyHitlStatus, ReviewStatus } from '../../common/review-status';

/** Whether another class can be picked for the section on the grouping board. */
const classIsChangeable = async (HITLStatus: string) => {
  const [documentItem] = mapDocumentsAttributes([
    { ObjectKey: 'packet.pdf', ObjectStatus: 'COMPLETED', HITLStatus, HITLReviewOwner: 'rev' } as never,
  ]);
  render(
    <SectionsPanel
      sections={[{ Id: '1', Class: 'Invoice', PageIds: [1] }] as never}
      pages={[{ Id: 1, ImageUri: 's3://out/1.jpg' }] as never}
      documentItem={documentItem as never}
      mergedConfig={{ classes: [{ $id: 'Invoice' }, { $id: 'W2' }] }}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: /Edit Page Grouping/i }));
  // Asserted through behaviour, as PageGroupingEditor's own test does: a locked
  // control still shows the class but offers no other. "Invoice" also appears in the
  // sections table, so the board's select trigger is picked out by its popup role.
  const trigger = screen.getAllByText('Invoice').find((el) => el.closest('button[aria-haspopup="listbox"]'));
  expect(trigger).toBeDefined();
  await userEvent.click(trigger as HTMLElement);
  return screen.queryByText('W2') !== null;
};

describe('SectionsPanel Reviewer-only lock on a finished review', () => {
  it.each([ReviewStatus.COMPLETE, ReviewStatus.REJECTED])('locks the class once a queue review ended %s', async (status) => {
    expect(await classIsChangeable(status)).toBe(false);
  });

  it.each([LegacyHitlStatus.REVIEW_COMPLETED, 'COMPLETED'])('locks the class once a legacy review finished (%s)', async (status) => {
    expect(await classIsChangeable(status)).toBe(false);
  });

  it('leaves the class changeable for the Reviewer who holds an open legacy review', async () => {
    expect(await classIsChangeable(LegacyHitlStatus.IN_PROGRESS)).toBe(true);
  });
});
