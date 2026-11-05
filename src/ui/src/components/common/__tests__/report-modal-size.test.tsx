// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The dialogs the Reports menus open share one width, REPORT_MODAL_SIZE: Processing
 * Report, Summary, Estimated Cost and the editor's History. Modal is replaced by a stub
 * that records the size each dialog asks for, so a dialog that drifts to its own size
 * fails here by name. Evaluation Report keeps its own, wider size.
 */

import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@cloudscape-design/components', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@cloudscape-design/components')>();
  const Modal = ({ header, size, children }: { header?: React.ReactNode; size?: string; children?: React.ReactNode }) => (
    <div data-testid="modal" data-size={size}>
      <h2>{header}</h2>
      {children}
    </div>
  );
  return { ...actual, Modal };
});
vi.mock('../../document-viewer/MarkdownViewer', () => ({ MarkdownReport: () => <div>report</div> }));
vi.mock('../../document-viewer/EvaluationReport', () => ({ default: () => <div>evaluation</div> }));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => ({ pricing: null }) }));
vi.mock('../../document-panel/MeteringTable', () => ({ default: () => <div>metering</div> }));

import { REPORT_MODAL_SIZE, ReportModal, SummaryReportModal } from '../ReportModal';
import EstimatedCostModal from '../../document-panel/EstimatedCostModal';
import EditorHistoryModal from '../../edit-screen/EditorHistoryModal';
import DocumentReportDialog from '../../document-actions/DocumentReportDialog';

const modalSize = () => screen.getByTestId('modal').getAttribute('data-size');
const noop = () => undefined;

describe('Reports dialog width', () => {
  afterEach(cleanup);

  it('is x-large, one step wider than the large dialogs these used to be', () => {
    expect(REPORT_MODAL_SIZE).toBe('x-large');
  });

  it.each<[string, () => React.JSX.Element]>([
    [
      'Processing Report',
      () => (
        <ReportModal header="Processing Report" onDismiss={noop}>
          content
        </ReportModal>
      ),
    ],
    ['Document Summary', () => <SummaryReportModal reportUri="s3://b/summary.md" objectKey="doc.pdf" onDismiss={noop} />],
    ['Estimated Cost', () => <EstimatedCostModal meteringData={{}} pageCount={1} onDismiss={noop} />],
    ['History', () => <EditorHistoryModal visible onDismiss={noop} sectionData={{ inference_result: {} }} />],
  ])('%s uses the shared size', (header, dialog) => {
    render(dialog());
    expect(screen.getByRole('heading', { level: 2 }).textContent).toContain(header);
    expect(modalSize()).toBe(REPORT_MODAL_SIZE);
  });

  it('leaves Evaluation Report at its own wider size', () => {
    render(
      <DocumentReportDialog
        report="evaluation"
        displayed={{ objectKey: 'doc.pdf', evaluationReportUri: 's3://b/e.json' }}
        onDismiss={noop}
      />,
    );
    expect(modalSize()).toBe('max');
  });
});
