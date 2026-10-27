// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen's action bar and title. What has to hold: the bar comes in the order
 * Edit | View (where the mode offers it), History, Edit Page Grouping,
 * Skip, Cancel, Save and Reprocess, each only where it applies; Claim mode shows the
 * view switch, History and Claim only; there is no Processing Report button (it is a History
 * tab); a disabled button carries its reason; while an action runs every button is
 * disabled; each button reports its own action; and the title reads
 * "<document> (<status>)" in plain text over the object key.
 */

import React from 'react';
import { fireEvent, render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

import EditScreen from '../EditScreen';
import type { EditActionsProps } from '../EditScreen';

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
const buttons = () =>
  header()
    .findActions()!
    .findAllButtons()
    .map((b) => b.getElement());
/** A button's own label: a disabled reason renders inside the button, after it. */
const label = (b: HTMLElement) => b.querySelector('[class*="content"]')?.textContent ?? b.textContent;
const labels = () => buttons().map(label);
const button = (name: string) => {
  const found = buttons().find((b) => label(b) === name);
  if (!found) throw new Error(`no button ${name}`);
  return found;
};

/** Cloudscape marks a button with a disabled reason aria-disabled rather than disabled. */
const isDisabled = (b: HTMLElement) => (b as HTMLButtonElement).disabled || b.getAttribute('aria-disabled') === 'true';

const ALL = { showRegroup: true, showSkip: true, showCancel: true, showSave: true };

describe('the edit screen action bar', () => {
  it('renders every action in order', () => {
    renderScreen(ALL);
    expect(labels()).toEqual(['History', 'Edit Page Class', 'Skip', 'Cancel', 'Save', 'Process Changes']);
  });

  it('shows only History and Reprocess by default', () => {
    renderScreen();
    expect(labels()).toEqual(['History', 'Process Changes']);
  });

  it('puts the Edit | View switch first, and reports the view chosen', () => {
    const onViewChange = vi.fn();
    renderScreen({ ...ALL, view: 'edit', onViewChange });
    expect(labels()).toEqual(['History', 'Edit Page Class', 'Skip', 'Cancel', 'Save', 'Process Changes']);
    const control = header().findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((s) => s.getElement().textContent)).toEqual(['Edit', 'View']);
    expect(control.findSelectedSegment()!.getElement().textContent).toBe('Edit');
    // First in the bar: before History.
    expect(control.getElement().compareDocumentPosition(button('History')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    control.findSegments()[1].click();
    expect(onViewChange).toHaveBeenCalledWith('source');
  });

  it('keeps the view switch beside Claim', () => {
    renderScreen({ ...ALL, showClaim: true, view: 'source', onViewChange: vi.fn() });
    expect(labels()).toEqual(['History', 'Claim']);
    expect(header().findActions()!.findSegmentedControl()!.findSelectedSegment()!.getElement().textContent).toBe('View');
  });

  it('offers no view switch where the mode gives no view', () => {
    renderScreen(ALL);
    expect(header().findActions()!.findSegmentedControl()).toBeNull();
  });

  it('labels the primary action Reprocess in every mode', () => {
    renderScreen(ALL);
    expect(labels()).not.toContain('Complete');
  });

  it('shows History and Claim in claim mode', () => {
    renderScreen({ ...ALL, showClaim: true });
    expect(labels()).toEqual(['History', 'Claim']);
  });

  it('has no Processing Report button: the report is a History tab', () => {
    renderScreen(ALL);
    expect(labels()).not.toContain('Processing Report');
  });

  it('disables a button with its reason', () => {
    renderScreen({ ...ALL, regroupDisabledReason: 'Not while in review', primaryDisabledReason: 'Assigned to someone else' });
    expect(isDisabled(button('Edit Page Class'))).toBe(true);
    expect(isDisabled(button('Process Changes'))).toBe(true);
    expect(isDisabled(button('Save'))).toBe(false);
    const regroup = createWrapper(document.body)
      .findAllButtons()
      .find((b) => label(b.getElement()) === 'Edit Page Class')!;
    regroup.focus();
    expect(regroup.findDisabledReason()?.getElement().textContent).toBe('Not while in review');
  });

  it('disables every button while an action runs', () => {
    renderScreen({ ...ALL, busy: 'save' });
    for (const b of buttons()) expect(isDisabled(b), label(b) ?? '').toBe(true);
  });

  it.each([
    ['Edit Page Class', 'regroup'],
    ['Skip', 'skip'],
    ['Cancel', 'cancel'],
    ['Save', 'save'],
    ['Process Changes', 'process'],
  ])('%s reports %s', (name, action) => {
    const { onAction } = renderScreen(ALL);
    fireEvent.click(button(name));
    expect(onAction).toHaveBeenCalledWith(action);
  });

  it('shows History without an icon, and it opens its dialog', () => {
    const { onShowHistory, onAction } = renderScreen(ALL);
    expect(button('History').querySelector('svg')).toBeNull();
    fireEvent.click(button('History'));
    expect(onShowHistory).toHaveBeenCalledTimes(1);
    expect(onAction).not.toHaveBeenCalled();
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
