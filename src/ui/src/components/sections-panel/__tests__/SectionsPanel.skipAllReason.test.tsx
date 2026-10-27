// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Skip All Reviews asks why, and sends the reason and note with the skip, so the
 * document's review history says why nobody checked it.
 */

import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  updateDocumentSections: 'updateDocumentSections',
  processChanges: 'processChanges',
  skipAllSectionsReview: 'skipAllSectionsReview',
}));
vi.mock('../../../hooks/use-page-thumbnails', () => ({
  default: () => ({ urls: {}, failure: null }),
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

describe('Skip All Reviews', () => {
  it('sends the chosen reason and note with the skip', async () => {
    graphql.mockResolvedValue({ data: { skipAllSectionsReview: { HITLStatus: 'Review Skipped' } } });
    render(
      <SectionsPanel
        sections={[{ Id: '1', Class: 'Invoice', PageIds: [1] }] as never}
        pages={[{ Id: 1, ImageUri: 's3://out/1.jpg' }] as never}
        documentItem={{ objectKey: 'packet.pdf', objectStatus: 'COMPLETED', hitlTriggered: true, hitlStatus: 'Review Pending' } as never}
        mergedConfig={{ classes: [{ $id: 'Invoice' }] }}
        onDocumentUpdate={() => undefined}
      />,
    );
    const page = createWrapper(document.body);
    page
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip All Reviews' && !b.getElement().closest('[role="dialog"]'))!
      .click();
    const modal = page.findAllModals().find((m) => m.findHeader().getElement().textContent === 'Skip All Section Reviews')!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Duplicate document');
    modal.findContent().findTextarea()!.setTextareaValue('same as invoice-2');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip All Reviews')!
      .click();
    await waitFor(() => expect(graphql).toHaveBeenCalled());
    expect(graphql.mock.calls[0][0]).toEqual({
      query: 'skipAllSectionsReview',
      variables: { objectKey: 'packet.pdf', reason: 'Duplicate document', note: 'same as invoice-2' },
    });
  });

  it('shows the message the server refused the skip with', async () => {
    const message = 'REVIEW_FINISHED: this review is already finished, so there is nothing to skip';
    // The REST client rejects with the dispatcher's body, not an Error.
    graphql.mockRejectedValue({ errors: [{ message, errorType: 'BadRequest' }] });
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => undefined);
    render(
      <SectionsPanel
        sections={[{ Id: '1', Class: 'Invoice', PageIds: [1] }] as never}
        pages={[{ Id: 1, ImageUri: 's3://out/1.jpg' }] as never}
        documentItem={{ objectKey: 'packet.pdf', objectStatus: 'COMPLETED', hitlTriggered: true, hitlStatus: 'Review Pending' } as never}
        mergedConfig={{ classes: [{ $id: 'Invoice' }] }}
        onDocumentUpdate={() => undefined}
      />,
    );
    const page = createWrapper(document.body);
    page
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip All Reviews' && !b.getElement().closest('[role="dialog"]'))!
      .click();
    const modal = page.findAllModals().find((m) => m.findHeader().getElement().textContent === 'Skip All Section Reviews')!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Duplicate document');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip All Reviews')!
      .click();
    await waitFor(() => expect(alert).toHaveBeenCalled());
    expect(alert).toHaveBeenCalledWith(`Failed to skip all sections: ${message}`);
    alert.mockRestore();
  });
});
