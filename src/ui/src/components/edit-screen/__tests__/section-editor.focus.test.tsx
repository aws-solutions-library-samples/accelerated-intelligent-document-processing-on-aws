// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A field focus request (a review reason's chip, Next alert) moves the Document Editor to
 * the field: it opens the request's section, expands the groups above the field, scrolls
 * the field to the centre and focuses its input. The same request with a new nonce
 * does it again, so Next alert on a one-alert document still moves focus back.
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let fieldProps: Record<string, unknown> | null = null;
vi.mock('../../common/PageImageViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/JSONEditorTab', () => ({ default: () => null }));
vi.mock('../../document-viewer/FormFieldRenderer', () => ({
  default: (props: Record<string, unknown>) => {
    fieldProps = props;
    return (
      <div>
        <div data-field-path="Total">
          <input aria-label="Total" />
        </div>
        <div data-field-path="LineItems[2].Amount">
          <input aria-label="Amount" />
        </div>
      </div>
    );
  },
}));

import SectionEditor from '../SectionEditor';
import { collapseKeysAbove } from '../field-focus';
import type { FieldFocusRequest } from '../field-focus';
import type { ReviewSectionRef } from '../types';

const SECTIONS: ReviewSectionRef[] = [
  { sectionId: '1', documentClass: 'Invoice', resultUri: 's3://b/1', pageIds: [] },
  { sectionId: '2', documentClass: 'Invoice', resultUri: 's3://b/2', pageIds: [] },
];
const scrollIntoView = vi.fn();
const loadSection = vi.fn(async (sectionId: string) => {
  const data = { inference_result: { Total: sectionId, LineItems: [{}, {}, { Amount: 3 }] } };
  return { data, raw: JSON.stringify(data) };
});

const editor = (focusRequest: FieldFocusRequest | null) => (
  <SectionEditor
    objectKey="in/doc.pdf"
    sections={SECTIONS}
    pages={[]}
    isReadOnly={false}
    loadSection={loadSection}
    onSave={async () => undefined}
    focusRequest={focusRequest}
  />
);

beforeEach(() => {
  document.body.innerHTML = '';
  fieldProps = null;
  loadSection.mockClear();
  scrollIntoView.mockReset();
  Element.prototype.scrollIntoView = scrollIntoView;
});

describe('SectionEditor focus requests', () => {
  it('keys the groups above a nested list field the way the field renderer does', () => {
    expect(collapseKeysAbove('LineItems[2].Amount')).toEqual(['Document Data', 'Document Data.LineItems', 'Document Data.LineItems.[2]']);
    expect(collapseKeysAbove('Total')).toEqual(['Document Data']);
  });

  it('opens the section, expands the groups above the field, scrolls to it and focuses it', async () => {
    const { rerender } = render(editor(null));
    await waitFor(() => expect(fieldProps).not.toBeNull());
    // Collapse the list the field is in, as a reviewer might have.
    act(() => {
      (fieldProps!.onToggleCollapse as (k: string) => void)('Document Data.LineItems');
    });
    await waitFor(() => expect((fieldProps!.collapsedPaths as Set<string>).has('Document Data.LineItems')).toBe(true));

    rerender(editor({ sectionId: '2', fieldPath: 'LineItems[2].Amount', nonce: 1 }));
    await waitFor(() => expect(loadSection).toHaveBeenCalledWith('2'));
    await waitFor(() => expect(document.activeElement?.getAttribute('aria-label')).toBe('Amount'));
    expect(scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: 'center' }));
    expect((fieldProps!.collapsedPaths as Set<string>).has('Document Data.LineItems')).toBe(false);
  });

  it('repeats the same request when its nonce changes', async () => {
    const { rerender } = render(editor({ sectionId: '1', fieldPath: 'Total', nonce: 1 }));
    await waitFor(() => expect(document.activeElement?.getAttribute('aria-label')).toBe('Total'));
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    (document.activeElement as HTMLElement).blur();
    rerender(editor({ sectionId: '1', fieldPath: 'Total', nonce: 2 }));
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalledTimes(2));
    expect(document.activeElement?.getAttribute('aria-label')).toBe('Total');
  });
});
