// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit page's Document Editor panel (SectionEditor). What has
 * to hold: the panel is titled Document Editor; with more than one section, a Section
 * dropdown at its top left lists every section as "Section <id> (<class>)" and opening
 * one loads it; with one section there is no dropdown; the panel has no Save, Discard or
 * Edit history of its own; and `body` (the source document) replaces the panes and hides the section
 * controls while the editor stays mounted.
 *
 * The field pane's height is not pinned here: it is tracked in the separate edit-page
 * defects item, and the edit page uses the fixed `WIDGET_PANE_HEIGHT`
 * (`SectionEditor.tsx`). The retired Visual Editor dialog's field-pane height test went
 * with that dialog.
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../common/PageImageViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/FormFieldRenderer', () => ({ default: () => null }));
vi.mock('../../document-viewer/JSONEditorTab', () => ({ default: () => null }));

import SectionEditor from '../SectionEditor';
import type { ReviewSectionRef } from '../types';

const RESULT = { inference_result: { Name: 'Ann' } };
const SECTIONS: ReviewSectionRef[] = [
  { sectionId: '1', documentClass: 'Letter', resultUri: 's3://b/1', pageIds: [] },
  { sectionId: '2', documentClass: 'Form', resultUri: 's3://b/2', pageIds: [] },
  { sectionId: '3', documentClass: null, resultUri: 's3://b/3', pageIds: [] },
];

const renderWidget = async (props: Partial<React.ComponentProps<typeof SectionEditor>> = {}) => {
  // Each section's result carries its own class, as a loaded result does.
  const loadSection = vi.fn(async (id: string) => {
    const documentClass = SECTIONS.find((s) => s.sectionId === id)?.documentClass;
    const data = { ...RESULT, ...(documentClass ? { document_class: { type: documentClass } } : {}) };
    return { data, raw: JSON.stringify(data) };
  });
  render(
    <SectionEditor
      objectKey="in/doc.pdf"
      sections={SECTIONS}
      pages={[]}
      isReadOnly={false}
      loadSection={loadSection}
      onSave={async () => undefined}
      {...props}
    />,
  );
  await waitFor(() => expect(loadSection).toHaveBeenCalled());
  return { loadSection };
};

const panel = () => createWrapper(document.body).findContainer()!;
/** The Section dropdown, by its inline label (the panes hold the Fields to show dropdown too). */
const sectionSelect = () =>
  createWrapper(panel().getElement())
    .findAllSelects()
    .find((s) => (s.getElement().textContent ?? '').startsWith('Section'));

describe('the Document Editor panel', () => {
  it('is titled Document Editor', async () => {
    await renderWidget();
    expect(panel().findHeader()!.getElement().textContent).toBe('Document Editor');
  });

  it('lists the sections in a dropdown, and opens the one chosen', async () => {
    const { loadSection } = await renderWidget();
    expect(createWrapper(panel().getElement()).findSegmentedControl()).toBeNull();
    const select = sectionSelect()!;
    select.openDropdown();
    const labels = select
      .findDropdown()
      .findOptions()
      .map((o) => o.findLabel().getElement().textContent);
    expect(labels).toEqual(['Section 1 (Letter)', 'Section 2 (Form)', 'Section 3 (no class)']);
    await act(async () => select.selectOptionByValue('2'));
    await waitFor(() => expect(loadSection).toHaveBeenLastCalledWith('2'));
  });

  it('has no Save, Discard or Edit history tab of its own: those are the page’s', async () => {
    await renderWidget();
    const labels = [...document.querySelectorAll('button')].map((b) => b.textContent);
    expect(labels).not.toContain('Save changes');
    expect(labels).not.toContain('Discard changes');
    expect(document.body.textContent).not.toContain('Edit history');
  });

  it('has no dropdown for a single section', async () => {
    await renderWidget({ sections: [SECTIONS[0]] });
    expect(sectionSelect()).toBeUndefined();
  });

  it('shows `body` in place of the panes, without the section controls', async () => {
    await renderWidget({ body: <div data-testid="source">source</div> });
    expect(document.querySelector('[data-testid="source"]')).not.toBeNull();
    expect(sectionSelect()).toBeUndefined();
    expect(panel().findHeader()!.getElement().textContent).toBe('Document Editor');
  });
});
