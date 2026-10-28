// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The confirmation before Save and Re-extract. What has to hold: it lists each section it
 * will extract again, with its class and pages, and says what that does: their values are
 * replaced, their classes recorded as confirmed, a pending review completed, rule
 * validation cleared, and the other sections left as they are.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

import ProcessChangesConfirmModal from '../ProcessChangesConfirmModal';

describe('ProcessChangesConfirmModal for Save and Re-extract', () => {
  it('lists the sections and what re-extracting them does', () => {
    render(
      <ProcessChangesConfirmModal
        visible
        reextractSections={[
          { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3] },
          { sectionId: '3', documentClass: 'Invoice', pageIds: [5] },
        ]}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findHeader().getElement().textContent).toBe('Confirm Reprocessing');
    const items = [...modal.findContent().getElement().querySelectorAll('li')].map((li) => li.textContent);
    expect(items).toEqual([
      'Section 1: FieldTicket, pages 1, 2, 3',
      'Section 3: Invoice, pages 5',
      'Replace their extracted values, including any corrected by hand',
      'Record their classes as confirmed (class confidence 1.0)',
      "Mark this document's pending review as completed, if it has one",
      "Clear the document's rule validation results",
      "Leave the other sections' values as they are; OCR and classification are not repeated",
    ]);
    expect(modal.findContent().getElement().textContent).toContain(
      'You are about to save the new page grouping, then extract these sections again:',
    );
    expect(
      modal
        .findFooter()!
        .findAllButtons()
        .map((b) => b.getElement().textContent),
    ).toEqual(['Cancel', 'Confirm & Process']);
  });
});
