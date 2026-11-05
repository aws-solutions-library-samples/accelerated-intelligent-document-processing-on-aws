// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Document Editor labels values by where they come from: a section result with a
 * test-set label source is ground truth, so the field renderer and the JSON tab get
 * the ground-truth wording; a production result (no label source) keeps "Predicted".
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fieldProps = vi.fn();
const jsonProps = vi.fn();
vi.mock('../../common/PageImageViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/FormFieldRenderer', () => ({
  default: (props: Record<string, unknown>) => {
    fieldProps(props);
    return null;
  },
}));
vi.mock('../../document-viewer/JSONEditorTab', () => ({
  default: (props: Record<string, unknown>) => {
    jsonProps(props);
    return null;
  },
}));

import SectionEditor from '../SectionEditor';
import type { ReviewSectionRef } from '../types';

const SECTIONS: ReviewSectionRef[] = [{ sectionId: '1', documentClass: 'Letter', resultUri: 's3://b/1', pageIds: [] }];

const renderEditor = async (data: Record<string, unknown>) => {
  const loadSection = vi.fn(async () => ({ data, raw: JSON.stringify(data) }));
  render(
    <SectionEditor
      objectKey="in/doc.pdf"
      sections={SECTIONS}
      pages={[]}
      isReadOnly={false}
      loadSection={loadSection}
      onSave={async () => undefined}
    />,
  );
  await waitFor(() => expect(fieldProps).toHaveBeenCalled());
  act(() => createWrapper(document.body).findTabs()!.findTabLinkById('json')!.click());
  await waitFor(() => expect(jsonProps).toHaveBeenCalled());
};

const lastProps = (mock: ReturnType<typeof vi.fn>) => mock.mock.calls[mock.mock.calls.length - 1][0] as Record<string, unknown>;

beforeEach(() => {
  document.body.innerHTML = '';
  fieldProps.mockClear();
  jsonProps.mockClear();
});

describe('SectionEditor value labels', () => {
  it('labels a test-set section (labelSource reviewed-human) as ground truth', async () => {
    await renderEditor({ inference_result: { Name: 'Ann' }, labelSource: 'reviewed-human' });

    expect(lastProps(fieldProps).valueLabel).toMatchObject({ value: 'Ground truth:', changed: 'Your value:' });
    expect(lastProps(jsonProps).predictionTitle).toBe('Ground truth');
  });

  it('keeps prediction wording for a production section', async () => {
    await renderEditor({ inference_result: { Name: 'Ann' } });

    expect(lastProps(fieldProps).valueLabel).toMatchObject({ value: 'Predicted:', changed: 'Your value:' });
    expect(lastProps(jsonProps).predictionTitle).toBe('Predicted Results');
  });
});
