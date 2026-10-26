// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen's Processing Report, a tab of History. What has to hold: it renders
 * the open section's report from the loaded result, says what to do when no section is
 * open, and is reachable from History.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it } from 'vitest';

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

  it('is a tab of History', () => {
    render(<EditorHistoryModal visible onDismiss={() => undefined} sectionData={SECTION} />);
    const tabs = createWrapper(document.body).findTabs()!;
    expect(tabs.findTabLinkByIndex(3)?.getElement().textContent).toBe('Processing Report');
    tabs.findTabLinkByIndex(3)!.click();
    expect(tabs.findTabContent()?.getElement().textContent).toContain('Extracted with the invoice schema');
  });
});
