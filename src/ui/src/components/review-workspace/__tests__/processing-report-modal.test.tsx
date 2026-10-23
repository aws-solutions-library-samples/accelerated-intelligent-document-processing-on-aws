// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The unified editor's Processing Report. What has to hold: it renders the open
 * section's report from the loaded result, and says what to do when no section is open.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it } from 'vitest';

import ProcessingReportModal from '../ProcessingReportModal';

const content = () => createWrapper(document.body).findModal()!.findContent().getElement().textContent ?? '';

describe('ProcessingReportModal', () => {
  it('renders the loaded section report', () => {
    render(
      <ProcessingReportModal
        visible
        onDismiss={() => undefined}
        sectionData={{
          inference_result: { Name: 'A' },
          metadata: { extraction_method: 'traditional' },
          processing_report: 'Extracted with the invoice schema',
        }}
      />,
    );
    expect(content()).toContain('Extracted with the invoice schema');
  });

  it('has an empty state without a section', () => {
    render(<ProcessingReportModal visible onDismiss={() => undefined} sectionData={null} />);
    expect(content()).toContain('Open a section to see its processing report.');
  });
});
