// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen's Processing Report, its own button in the bar. What has to hold: it
 * renders the open section's report from the loaded result, says what to do when no
 * section is open, opens from the bar right after History, and is not a History tab.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it } from 'vitest';

import EditScreen from '../EditScreen';
import EditorHistoryModal from '../EditorHistoryModal';
import ProcessingReportContent from '../ProcessingReportContent';

const SECTION = {
  inference_result: { Name: 'A' },
  metadata: { extraction_method: 'traditional' },
  processing_report: 'Extracted with the invoice schema',
};

describe('ProcessingReportContent', () => {
  it('renders the loaded section report', () => {
    const { container } = render(<ProcessingReportContent sectionData={SECTION} />);
    expect(container.textContent).toContain('Extracted with the invoice schema');
  });

  it('has an empty state without a section', () => {
    const { container } = render(<ProcessingReportContent sectionData={null} />);
    expect(container.textContent).toContain('Open a section to see its processing report.');
  });

  it('opens from the edit screen bar, after History, and is not a History tab', () => {
    render(
      <EditScreen
        title="doc.pdf"
        objectKey="in/doc.pdf"
        reportSectionData={SECTION}
        actions={{ busy: null, onAction: () => undefined, onShowHistory: () => undefined }}
      >
        <div />
      </EditScreen>,
    );
    const bar = createWrapper(document.body).findHeader()!.findActions()!;
    const labels = bar.findAllButtons().map((b) => b.getElement().textContent);
    expect(labels.indexOf('Processing Report')).toBe(labels.indexOf('History') + 1);
    bar
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Processing Report')!
      .click();
    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findHeader().getElement().textContent).toBe('Processing Report');
    expect(modal.findContent().getElement().textContent).toContain('Extracted with the invoice schema');
  });

  it("is not offered without the open section's data to show", () => {
    render(
      <EditScreen title="doc.pdf" objectKey="in/doc.pdf" actions={{ busy: null, onAction: () => undefined }}>
        <div />
      </EditScreen>,
    );
    const labels = createWrapper(document.body)
      .findHeader()!
      .findActions()!
      .findAllButtons()
      .map((b) => b.getElement().textContent);
    expect(labels).not.toContain('Processing Report');
  });

  it('is not a tab of History', () => {
    render(<EditorHistoryModal visible onDismiss={() => undefined} sectionData={SECTION} />);
    const tabs = createWrapper(document.body)
      .findTabs()!
      .findTabLinks()
      .map((l) => l.getElement().textContent);
    expect(tabs).toEqual(['Field Edits', 'Review Activity']);
  });
});
