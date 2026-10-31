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
/** A dropdown button of the panel, by the start of its accessible name ("Section:", "Show fields:"). */
const dropdown = (name: string) =>
  createWrapper(panel().getElement())
    .findAllButtonDropdowns()
    .find((d) => (d.findNativeButton().getElement().getAttribute('aria-label') ?? '').startsWith(name));
const sectionSelect = () => dropdown('Section:');
const isDisabled = (d: ReturnType<typeof dropdown>) => d!.findNativeButton().getElement().disabled;
/** A dropdown's items: label, and whether it is the current choice (ticked) or disabled. */
const choices = (d: NonNullable<ReturnType<typeof dropdown>>) => {
  act(() => d.openDropdown());
  const items = d.findItems().map((i) => {
    const el = i.getElement().querySelector('[role="menuitemcheckbox"]') ?? i.getElement();
    const describedBy = el.getAttribute('aria-describedby');
    const reason = describedBy ? (document.getElementById(describedBy)?.textContent ?? '') : '';
    const text = i.getElement().textContent ?? '';
    return {
      text: reason && text.endsWith(reason) ? text.slice(0, -reason.length) : text,
      checked: el.getAttribute('aria-checked') === 'true',
      disabled: el.getAttribute('aria-disabled') === 'true',
    };
  });
  act(() => d.openDropdown());
  return items;
};

/** The panel header's controls, in order, by label. */
const widgetControls = () => {
  const actions = panelHeader().findActions();
  if (!actions) return [];
  const labels: string[] = [];
  if (actions.findToggle()) labels.push(actions.findToggle()!.getElement().textContent ?? '');
  actions.findAllButtonDropdowns().forEach((d) => {
    const name = d.findNativeButton().getElement().getAttribute('aria-label') ?? '';
    labels.push(name.startsWith('Section:') ? 'Section' : 'Show Fields');
  });
  actions.findAllButtons().forEach((b) => {
    if (!b.getElement().closest('[aria-haspopup]') && !b.getElement().hasAttribute('aria-haspopup'))
      labels.push(b.getElement().textContent ?? '');
  });
  return labels.filter((t) => t !== '');
};
const fieldsSelect = () => dropdown('Show fields:')!;
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
    expect(choices(select).map((c) => c.text)).toEqual(['Section 1 (Letter)', 'Section 2 (Form)', 'Section 3 (no class)']);
    act(() => select.openDropdown());
    await act(async () => select.findItemById('2')!.click());
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
    expect(isDisabled(sectionSelect())).toBe(true);
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
    await waitFor(() => expect(isDisabled(fieldsSelect())).toBe(false));
    act(() => createWrapper(panel().getElement()).findTabs()!.findTabLinkById('json')!.click());
    expect(isDisabled(fieldsSelect())).toBe(true);
  });

  it('are dropdown buttons with no title, each reading its current choice, which the menu ticks', async () => {
    await renderWidget({ evaluation: EVALUATION });
    expect(panelHeader().findActions()!.findAllSelects()).toHaveLength(0);
    expect(panelHeader().findActions()!.getElement().textContent).not.toMatch(/Show Fields|Section:/);
    const section = sectionSelect()!;
    expect(section.findNativeButton().getElement().textContent).toBe('Section 1 (Letter)');
    expect(choices(section).map((c) => c.checked)).toEqual([true, false, false]);
    await waitFor(() => expect(isDisabled(fieldsSelect())).toBe(false));
    expect(fieldsSelect().findNativeButton().getElement().textContent).toBe('All fields');
    expect(choices(fieldsSelect())).toEqual([
      { text: 'All fields', checked: true, disabled: false },
      { text: 'Confidence alerts only', checked: false, disabled: false },
      // Needs the comparison on.
      { text: 'Evaluation mismatches only', checked: false, disabled: true },
    ]);
  });

  it('switch Show Fields to the choice made, and the button reads it', async () => {
    await renderWidget();
    await waitFor(() => expect(isDisabled(fieldsSelect())).toBe(false));
    act(() => fieldsSelect().openDropdown());
    act(() => fieldsSelect().findItemById('confidence-alerts')!.click());
    expect(fieldsSelect().findNativeButton().getElement().textContent).toBe('Confidence alerts only');
    expect(choices(fieldsSelect()).find((c) => c.checked)?.text).toBe('Confidence alerts only');
  });
});
