// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The class verdict on a document with an evaluation baseline is not behind Show
 * Evaluation. The toggle defaults to off and only enables once the evaluation resolves,
 * so a verdict gated on it is one a reviewer never sees, and the class decides which
 * schema every field was extracted against. What has to hold, on the edit page's
 * editor: the verdict renders with Show Evaluation off; a hint says what the toggle
 * would add; and the per-field comparison stays opt-in, off until the toggle is on.
 */

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ fieldProps: null as Record<string, unknown> | null }));
vi.mock('../../common/PageImageViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/JSONEditorTab', () => ({ default: () => null }));
vi.mock('../../document-viewer/FormFieldRenderer', () => ({
  default: (props: Record<string, unknown>) => {
    state.fieldProps = props;
    return null;
  },
}));
vi.mock('../../common/ClassMismatchIndicator', () => ({
  SectionClassEvaluation: ({ predictedClass, baselineClass }: { predictedClass?: string; baselineClass?: string }) => (
    <div data-testid="class-verdict">
      {predictedClass} vs {baselineClass}
    </div>
  ),
}));

import SectionEditor from '../SectionEditor';
import type { SectionEvaluation } from '../use-section-evaluation';

const RESULT = { document_class: { type: 'W2' }, inference_result: { Name: 'Ann' } };
const evaluation = {
  available: true,
  loading: false,
  baseline: { document_class: { type: 'Paystub' }, inference_result: { Name: 'Anne' } },
  originalBaseline: null,
  setBaseline: vi.fn(),
  markBaselineSaved: vi.fn(),
  evaluationResults: null,
  classificationIndex: null,
  canEditBaseline: false,
} as unknown as SectionEvaluation;

beforeEach(() => {
  document.body.innerHTML = '';
  state.fieldProps = null;
});

describe('the class verdict on the edit page', () => {
  it('shows with Show Evaluation off, with a hint, and keeps the field comparison opt-in', async () => {
    render(
      <SectionEditor
        objectKey="in/doc.pdf"
        sections={[{ sectionId: '1', documentClass: 'W2', resultUri: 's3://b/1', pageIds: [] }]}
        pages={[]}
        isReadOnly={false}
        loadSection={async () => ({ data: RESULT, raw: JSON.stringify(RESULT) })}
        onSave={async () => undefined}
        evaluation={evaluation}
      />,
    );
    await waitFor(() => expect(state.fieldProps).not.toBeNull());
    expect(screen.getByTestId('class-verdict').textContent).toBe('W2 vs Paystub');
    expect(document.body.textContent).toContain('This document has an evaluation baseline.');
    expect(state.fieldProps!.showComparison).toBe(false);
    expect(state.fieldProps!.baselineValue).toBeNull();

    const toggle = createWrapper(document.body).findToggle()!;
    await act(async () => toggle.findNativeInput().click());
    expect(state.fieldProps!.showComparison).toBe(true);
    expect(state.fieldProps!.baselineValue).toEqual({ Name: 'Anne' });
    expect(screen.getByTestId('class-verdict')).toBeTruthy();
  });
});
