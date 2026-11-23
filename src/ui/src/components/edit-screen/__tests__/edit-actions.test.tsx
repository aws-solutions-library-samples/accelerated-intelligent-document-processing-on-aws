// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen's action bar and title. What has to hold: the bar is one row, Edit |
 * View (where the mode offers it), the Reports menu (History, Summary, Policy Rule
 * Validation Summary, Processing Report), the More Actions menu (Edit Page Grouping, Use As Evaluation Baseline), then Skip,
 * Cancel, Save and Process Changes, the primary action, last; a button that does not
 * apply is left out, while a menu item the mode offers but that does not apply now is
 * disabled with its reason; Claim mode shows the view switch, Reports and Claim only;
 * while an action runs every button and menu is disabled; each button and item reports
 * its own action; the Policy Rule Validation Summary opens the document's policy report
 * in the dialog the document page uses; and the title reads "<document> (<status>)" in
 * plain text over the object key.
 */

import React from 'react';
import { fireEvent } from '@testing-library/react';
import { renderWithIntl as render } from '../../../test-utils/renderWithIntl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

// The report dialog's content reads the report from the server; here it shows what it was given.
vi.mock('../../document-viewer/MarkdownViewer', () => ({
  MarkdownReport: ({ reportUri, documentId, title }: { reportUri: string; documentId: string; title: string }) => (
    <div data-testid="markdown-report">{[title, documentId, reportUri].join('|')}</div>
  ),
}));

import EditScreen, { CANCEL_ACCESSIBLE_NAME } from '../EditScreen';
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

  it('names Cancel for what it does to unsaved edits, the marker the UX test commit guard reads', () => {
    renderScreen(ALL);
    expect(button('Cancel').getAttribute('aria-label')).toBe(CANCEL_ACCESSIBLE_NAME);
    // The guard recognises an editor that saves by itself from this name; a bare
    // Cancel would not do, since every dialog has one.
    const guard = readFileSync(join(__dirname, '..', '..', '..', '..', '..', '..', 'scripts', 'ux_runner', 'story_relay.py'), 'utf-8');
    expect(guard).toContain(`"${CANCEL_ACCESSIBLE_NAME.toLowerCase()}"`);
  });

  it('puts the read-only views in Reports and the evaluation baseline in More Actions, in Title Case', () => {
    renderScreen({ ...ALL, showBaseline: true });
    expect(menuItems('Reports').map((i) => i.text)).toEqual(['History']);
    // Edit Page Grouping is on the Document Editor panel, not in the bar.
    expect(menuItems('More Actions').map((i) => i.text)).toEqual(['Use As Evaluation Baseline']);
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

  it.each([['Use As Evaluation Baseline', 'baseline']])('More Actions › %s reports %s', (text, action) => {
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
    ['Reject', 'reject'],
    ['Cancel', 'cancel'],
    ['Save', 'save'],
    ['Process Changes', 'process'],
  ])('%s reports %s', (name, action) => {
    const { onAction } = renderScreen({ ...ALL, showReject: true });
    fireEvent.click(button(name));
    expect(onAction).toHaveBeenCalledWith(action);
  });

  it('offers Reject only where the mode asks for it, between Skip and Cancel', () => {
    renderScreen(ALL);
    expect(barButtonLabels()).not.toContain('Reject');

    document.body.innerHTML = '';
    renderScreen({ ...ALL, showReject: true });
    expect(barButtonLabels()).toEqual(['Skip', 'Reject', 'Cancel', 'Save', 'Process Changes']);
  });

  it('says what Reject does on hover, and why it is unavailable when it is', () => {
    renderScreen({ ...ALL, showReject: true });
    expect(isDisabled(button('Reject'))).toBe(false);
    expect(button('Reject').closest('[title]')?.getAttribute('title')).toBe(
      'End the review and reject this document. A reason is asked for first.',
    );

    document.body.innerHTML = '';
    renderScreen({ ...ALL, showReject: true, rejectDisabledReason: 'This queue lists no reject reasons' });
    expect(isDisabled(button('Reject'))).toBe(true);
    const reject = createWrapper(document.body)
      .findAllButtons()
      .find((b) => buttonLabel(b.getElement()) === 'Reject')!;
    reject.focus();
    expect(reject.findDisabledReason()?.getElement().textContent).toBe('This queue lists no reject reasons');
    expect(button('Reject').closest('[title]')).toBeNull();
  });

  it('shows Reject working while a document is rejected, and Process Changes while one is sent back', () => {
    const loading = (name: string) =>
      createWrapper(document.body)
        .findAllButtons()
        .find((b) => buttonLabel(b.getElement()) === name)!
        .findLoadingIndicator() !== null;

    renderScreen({ ...ALL, showReject: true, busy: 'reject' });
    expect([loading('Reject'), loading('Process Changes'), loading('Skip')]).toEqual([true, false, false]);
    for (const b of barButtons()) expect(isDisabled(b), buttonLabel(b) ?? '').toBe(true);

    document.body.innerHTML = '';
    renderScreen({ ...ALL, showReject: true, busy: 'send-back' });
    expect([loading('Reject'), loading('Process Changes')]).toEqual([false, true]);
  });
});

describe('the Policy Rule Validation Summary in Reports', () => {
  const POLICY_REPORT = 's3://out/in/doc.pdf/rule_validation/consolidated/consolidated_summary.md';
  const renderWithReports = (props: Partial<React.ComponentProps<typeof EditScreen>>) =>
    render(
      <EditScreen
        title="doc.pdf"
        objectKey="in/doc.pdf"
        summaryUnavailableReason="Available once the review is complete"
        reportSectionData={{}}
        actions={{ busy: null, onAction: vi.fn(), onShowHistory: vi.fn() }}
        {...props}
      >
        <div />
      </EditScreen>,
    );
  const item = () => menuItems('Reports').find((i) => i.text === 'Policy Rule Validation Summary');
  const reportModal = () =>
    createWrapper(document.body)
      .findAllModals()
      .find((m) => m.isVisible() && m.findHeader().getElement().textContent === 'Policy Rule Validation Summary');

  it('is offered after Summary and before Processing Report when the document has the report', () => {
    renderWithReports({ ruleValidationReportUri: POLICY_REPORT });

    expect(menuItems('Reports')).toEqual([
      { text: 'History', disabled: false, disabledReason: null },
      { text: 'Summary', disabled: true, disabledReason: 'Available once the review is complete' },
      { text: 'Policy Rule Validation Summary', disabled: false, disabledReason: null },
      { text: 'Processing Report', disabled: false, disabledReason: null },
    ]);
    // The label the menu shows is the one the document page uses.
    const menu = findMenu('Reports')!;
    menu.openDropdown();
    expect(menu.findItemById('rule-validation')!.getElement().textContent).toBe('Policy Rule Validation Summary');
  });

  it("opens the document's policy report in the report dialog, and closes again", () => {
    renderWithReports({ ruleValidationReportUri: POLICY_REPORT });
    // Mounted only while open.
    expect(reportModal()).toBeUndefined();

    chooseMenuItem('Reports', 'Policy Rule Validation Summary');

    const modal = reportModal()!;
    expect(modal.findContent().getElement().querySelector('[data-testid="markdown-report"]')!.textContent).toBe(
      `Policy Rule Validation Summary|in/doc.pdf|${POLICY_REPORT}`,
    );
    modal.findDismissButton().click();
    expect(reportModal()).toBeUndefined();
  });

  it('stays in the menu, disabled with the reason, for a document with no policy report', () => {
    renderWithReports({ ruleValidationUnavailableReason: 'This document has no policy rule validation summary' });

    expect(item()).toEqual({
      text: 'Policy Rule Validation Summary',
      disabled: true,
      disabledReason: 'This document has no policy rule validation summary',
    });
  });

  it('is left out where the mode offers no policy report', () => {
    renderWithReports({});

    expect(menuItems('Reports').map((i) => i.text)).toEqual(['History', 'Summary', 'Processing Report']);
  });

  it('is disabled with the rest of the menu while an action runs', () => {
    renderWithReports({
      ruleValidationReportUri: POLICY_REPORT,
      actions: { busy: 'save', onAction: vi.fn(), onShowHistory: vi.fn() },
    });

    expect(isDisabled(findMenu('Reports')!.findNativeButton().getElement())).toBe(true);
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
