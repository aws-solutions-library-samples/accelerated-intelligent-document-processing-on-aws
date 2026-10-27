// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen's action bar and title. What has to hold: the bar is one row, Edit |
 * View (where the mode offers it), the Reports menu (History, Summary, Processing
 * Report), the More Actions menu (Edit Page Class, Use As Evaluation Baseline), then Skip,
 * Cancel, Save and Process Changes, the primary action, last; a button that does not
 * apply is left out, while a menu item the mode offers but that does not apply now is
 * disabled with its reason; Claim mode shows the view switch, Reports and Claim only;
 * while an action runs every button and menu is disabled; each button and item reports
 * its own action; and the title reads "<document> (<status>)" in plain text over the
 * object key.
 */

import React from 'react';
import { fireEvent, render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

import EditScreen from '../EditScreen';
import type { EditActionsProps } from '../EditScreen';
import { barButtonLabels, barButtons, buttonLabel, chooseMenuItem, findMenu, menuItems, menuNames } from './edit-bar';

const renderScreen = (actions: Partial<EditActionsProps> = {}, status: string | undefined = undefined) => {
  const onAction = vi.fn();
  const onShowHistory = vi.fn();
  render(
    <EditScreen title="doc.pdf" status={status} objectKey="in/doc.pdf" actions={{ busy: null, onAction, onShowHistory, ...actions }}>
      <div />
    </EditScreen>,
  );
  return { onAction, onShowHistory };
};

const header = () => createWrapper(document.body).findHeader()!;
const button = (name: string) => {
  const found = barButtons().find((b) => buttonLabel(b) === name);
  if (!found) throw new Error(`no button ${name}`);
  return found;
};

/** Cloudscape marks a button with a disabled reason aria-disabled rather than disabled. */
const isDisabled = (b: HTMLElement) => (b as HTMLButtonElement).disabled || b.getAttribute('aria-disabled') === 'true';

const ALL = { showRegroup: true, showSkip: true, showCancel: true, showSave: true };

describe('the edit screen action bar', () => {
  it('is one row: the menus, then Skip, Cancel, Save and Process Changes, last', () => {
    renderScreen({ ...ALL, showBaseline: true });
    expect(menuNames()).toEqual(['Reports', 'More Actions']);
    expect(barButtonLabels()).toEqual(['Skip', 'Cancel', 'Save', 'Process Changes']);
    const reports = findMenu('Reports')!.getElement();
    expect(reports.compareDocumentPosition(button('Skip')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('puts the read-only views in Reports and the occasional changes in More Actions, in Title Case', () => {
    renderScreen({ ...ALL, showBaseline: true });
    expect(menuItems('Reports').map((i) => i.text)).toEqual(['History']);
    expect(menuItems('More Actions').map((i) => i.text)).toEqual(['Edit Page Class', 'Use As Evaluation Baseline']);
  });

  it('shows only Reports and Process Changes by default', () => {
    renderScreen();
    expect(menuNames()).toEqual(['Reports']);
    expect(barButtonLabels()).toEqual(['Process Changes']);
  });

  it('keeps a menu item that does not apply, disabled with its reason', () => {
    render(
      <EditScreen
        title="doc.pdf"
        objectKey="in/doc.pdf"
        summaryUnavailableReason="Available once the review is complete"
        actions={{
          busy: null,
          onAction: vi.fn(),
          onShowHistory: vi.fn(),
          showBaseline: true,
          baselineDisabledReason: 'Available once the review is complete',
        }}
      >
        <div />
      </EditScreen>,
    );
    expect(menuItems('More Actions').find((i) => i.text === 'Use As Evaluation Baseline')).toMatchObject({
      disabled: true,
      disabledReason: 'Available once the review is complete',
    });
    expect(menuItems('Reports')).toEqual([
      { text: 'History', disabled: false, disabledReason: null },
      { text: 'Summary', disabled: true, disabledReason: 'Available once the review is complete' },
    ]);
  });

  it.each([
    ['Edit Page Class', 'regroup'],
    ['Use As Evaluation Baseline', 'baseline'],
  ])('More Actions › %s reports %s', (text, action) => {
    const { onAction } = renderScreen({ ...ALL, showBaseline: true });
    chooseMenuItem('More Actions', text);
    expect(onAction).toHaveBeenCalledWith(action);
  });

  it('Reports › History opens its dialog, and reports no action', () => {
    const { onShowHistory, onAction } = renderScreen(ALL);
    chooseMenuItem('Reports', 'History');
    expect(onShowHistory).toHaveBeenCalledTimes(1);
    expect(onAction).not.toHaveBeenCalled();
  });

  it('puts the Edit | View switch first, and reports the view chosen', () => {
    const onViewChange = vi.fn();
    renderScreen({ ...ALL, view: 'edit', onViewChange });
    const control = header().findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((s) => s.getElement().textContent)).toEqual(['Edit', 'View']);
    expect(control.findSelectedSegment()!.getElement().textContent).toBe('Edit');
    expect(control.getElement().compareDocumentPosition(findMenu('Reports')!.getElement()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    control.findSegments()[1].click();
    expect(onViewChange).toHaveBeenCalledWith('source');
  });

  it('shows the view switch, Reports and Claim only in claim mode', () => {
    renderScreen({ ...ALL, showClaim: true, view: 'source', onViewChange: vi.fn() });
    expect(menuNames()).toEqual(['Reports']);
    expect(barButtonLabels()).toEqual(['Claim']);
    expect(header().findActions()!.findSegmentedControl()!.findSelectedSegment()!.getElement().textContent).toBe('View');
  });

  it('offers no view switch where the mode gives no view', () => {
    renderScreen(ALL);
    expect(header().findActions()!.findSegmentedControl()).toBeNull();
  });

  it('has no Processing Report or Summary without the data to show', () => {
    renderScreen(ALL);
    expect(menuItems('Reports').map((i) => i.text)).toEqual(['History']);
  });

  it('disables a button with its reason', () => {
    renderScreen({ ...ALL, primaryDisabledReason: 'Assigned to someone else' });
    expect(isDisabled(button('Process Changes'))).toBe(true);
    expect(isDisabled(button('Save'))).toBe(false);
    const primary = createWrapper(document.body)
      .findAllButtons()
      .find((b) => buttonLabel(b.getElement()) === 'Process Changes')!;
    primary.focus();
    expect(primary.findDisabledReason()?.getElement().textContent).toBe('Assigned to someone else');
  });

  it('disables every button and menu while an action runs', () => {
    renderScreen({ ...ALL, showBaseline: true, busy: 'save' });
    for (const b of barButtons()) expect(isDisabled(b), buttonLabel(b) ?? '').toBe(true);
    for (const name of ['Reports', 'More Actions'] as const)
      expect(isDisabled(findMenu(name)!.findNativeButton().getElement()), name).toBe(true);
  });

  it.each([
    ['Skip', 'skip'],
    ['Cancel', 'cancel'],
    ['Save', 'save'],
    ['Process Changes', 'process'],
  ])('%s reports %s', (name, action) => {
    const { onAction } = renderScreen(ALL);
    fireEvent.click(button(name));
    expect(onAction).toHaveBeenCalledWith(action);
  });
});

describe('the edit screen title', () => {
  it('reads "<document> (<status>)" in plain text, over the object key', () => {
    renderScreen({}, 'Claimed');
    expect(header().findHeadingText().getElement().textContent).toBe('doc.pdf (Claimed)');
    // The status is set apart from the title, in the subtitle's style.
    const status = [...header().findHeadingText().getElement().querySelectorAll('span')].find((s) => s.textContent === '(Claimed)');
    expect(status).toBeDefined();
    expect(header().findHeadingText().getElement().querySelector('svg')).toBeNull();
    expect(header().findDescription()?.getElement().textContent).toBe('in/doc.pdf');
  });

  it('reads the document name alone without a status', () => {
    renderScreen();
    expect(header().findHeadingText().getElement().textContent).toBe('doc.pdf');
  });
});
