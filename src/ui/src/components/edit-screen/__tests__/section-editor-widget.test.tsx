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
const panelHeader = () => createWrapper(panel().findHeader()!.getElement()).findHeader()!;
/** The Section dropdown, by its inline label (the panes hold the Fields to show dropdown too). */
const sectionSelect = () =>
  createWrapper(panel().getElement())
    .findAllSelects()
    .find((s) => (s.getElement().textContent ?? '').startsWith('Section'));

/** The panel header's controls, in order, by label. */
const widgetControls = () => {
  const actions = panelHeader().findActions();
  if (!actions) return [];
  const labels: string[] = [];
  if (actions.findToggle()) labels.push(actions.findToggle()!.getElement().textContent ?? '');
  actions
    .findAllSelects()
    .forEach((s) => labels.push((s.getElement().textContent ?? '').startsWith('Section') ? 'Section' : 'Show Fields'));
  actions.findAllButtons().forEach((b) => {
    if (!b.getElement().closest('[class*="select"]')) labels.push(b.getElement().textContent ?? '');
  });
  return labels.filter((t) => t !== '');
};
const fieldsSelect = () =>
  createWrapper(panel().getElement())
    .findAllSelects()
    .find((s) => (s.getElement().textContent ?? '').startsWith('Show Fields'))!;
const regroupButton = () =>
  panelHeader()
    .findActions()
    ?.findAllButtons()
    .find((b) => (b.getElement().textContent ?? '').startsWith('Edit Page Grouping'));
const EVALUATION = {
  available: true,
  loading: false,
  baseline: { inference_result: { Name: 'Ann' } },
  originalBaseline: null,
  setBaseline: vi.fn(),
  markBaselineSaved: vi.fn(),
  evaluationResults: null,
  classificationIndex: {} as never,
  canEditBaseline: true,
};

describe('the Document Editor panel', () => {
  it('is titled Document Editor', async () => {
    await renderWidget();
    expect(panelHeader().findHeadingText().getElement().textContent).toBe('Document Editor');
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

  it('shows the Section dropdown for a single section too, disabled since there is nothing to choose', async () => {
    await renderWidget({ sections: [SECTIONS[0]] });
    expect(sectionSelect()!.isDisabled()).toBe(true);
  });

  it('shows `body` in place of the panes, without the section controls', async () => {
    await renderWidget({ body: <div data-testid="source">source</div> });
    expect(document.querySelector('[data-testid="source"]')).not.toBeNull();
    expect(sectionSelect()).toBeUndefined();
    expect(widgetControls()).toEqual([]);
    expect(panel().findHeader()!.getElement().textContent).toBe('Document Editor');
  });
});

describe('the Document Editor panel’s buttons', () => {
  const regroup = (over: { disabledReason?: string } = {}) => ({ onClick: vi.fn(), ...over });

  it('are, right of the title: Show Evaluation where there is a baseline, Section, Show Fields, Edit Page Grouping', async () => {
    await renderWidget({ evaluation: EVALUATION, regroup: regroup() });
    expect(widgetControls()).toEqual(['Show Evaluation', 'Section', 'Show Fields', 'Edit Page Grouping']);
  });

  it('leave out Show Evaluation without a baseline, and Edit Page Grouping where the mode offers none', async () => {
    await renderWidget();
    expect(widgetControls()).toEqual(['Section', 'Show Fields']);
  });

  it('open the board from Edit Page Grouping, or show why it is unavailable', async () => {
    const on = regroup();
    await renderWidget({ regroup: on });
    regroupButton()!.click();
    expect(on.onClick).toHaveBeenCalledTimes(1);
    document.body.innerHTML = '';
    await renderWidget({ regroup: regroup({ disabledReason: 'Available when processing finishes' }) });
    expect(regroupButton()!.isDisabled()).toBe(true);
    act(() =>
      regroupButton()!
        .getElement()
        .dispatchEvent(new FocusEvent('focus', { bubbles: false })),
    );
    expect(document.body.textContent).toContain('Available when processing finishes');
  });

  it('disable Show Fields on the JSON tab, where the filter does not apply', async () => {
    await renderWidget();
    await waitFor(() => expect(fieldsSelect().isDisabled()).toBe(false));
    act(() => createWrapper(panel().getElement()).findTabs()!.findTabLinkById('json')!.click());
    expect(fieldsSelect().isDisabled()).toBe(true);
  });
});
