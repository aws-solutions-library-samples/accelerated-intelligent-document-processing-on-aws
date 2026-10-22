// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The unified editor's action bar. What has to hold: the buttons come in the order
 * History, Skip, Discard Changes, Save, Edit Page Grouping, Processing Report,
 * Complete, each only where it applies; Claim mode shows History, Processing
 * Report and Claim only; a disabled button carries its reason; while an action runs
 * every button is disabled; and each button reports its own action.
 */

import React from 'react';
import { fireEvent, render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

import AnnotationFrame from '../AnnotationFrame';
import type { AnnotationActionsProps } from '../AnnotationFrame';

const renderBar = (actions: Partial<AnnotationActionsProps> = {}) => {
  const onAction = vi.fn();
  const onShowHistory = vi.fn();
  const onShowReport = vi.fn();
  render(
    <AnnotationFrame title="doc.pdf" objectKey="in/doc.pdf" actions={{ busy: null, onAction, onShowHistory, onShowReport, ...actions }}>
      <div />
    </AnnotationFrame>,
  );
  return { onAction, onShowHistory, onShowReport };
};

const buttons = () =>
  createWrapper(document.body)
    .findHeader()!
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

const ALL = { showSkip: true, showDiscard: true, showSave: true, showRegroup: true };

describe('the unified editor action bar', () => {
  it('renders every action in order', () => {
    renderBar(ALL);
    expect(labels()).toEqual(['History', 'Skip', 'Discard Changes', 'Save', 'Edit Page Grouping', 'Processing Report', 'Complete']);
  });

  it('shows only History, Processing Report and Complete by default', () => {
    renderBar();
    expect(labels()).toEqual(['History', 'Processing Report', 'Complete']);
  });

  it('shows History, Processing Report and Claim in claim mode', () => {
    renderBar({ ...ALL, showClaim: true });
    expect(labels()).toEqual(['History', 'Processing Report', 'Claim']);
  });

  it('disables a button with its reason', () => {
    renderBar({ ...ALL, regroupDisabledReason: 'Not while in review', processDisabledReason: 'Assigned to someone else' });
    expect(isDisabled(button('Edit Page Grouping'))).toBe(true);
    expect(isDisabled(button('Complete'))).toBe(true);
    expect(isDisabled(button('Save'))).toBe(false);
    const regroup = createWrapper(document.body)
      .findAllButtons()
      .find((b) => label(b.getElement()) === 'Edit Page Grouping')!;
    regroup.focus();
    expect(regroup.findDisabledReason()?.getElement().textContent).toBe('Not while in review');
  });

  it('disables every button while an action runs', () => {
    renderBar({ ...ALL, busy: 'save' });
    for (const b of buttons()) expect(isDisabled(b), label(b) ?? '').toBe(true);
  });

  it.each([
    ['Skip', 'skip'],
    ['Discard Changes', 'discard'],
    ['Save', 'save'],
    ['Edit Page Grouping', 'regroup'],
    ['Complete', 'process'],
  ])('%s reports %s', (name, action) => {
    const { onAction } = renderBar(ALL);
    fireEvent.click(button(name));
    expect(onAction).toHaveBeenCalledWith(action);
  });

  it('shows History without an icon', () => {
    renderBar(ALL);
    expect(button('History').querySelector('svg')).toBeNull();
  });

  it('History and Processing Report open their dialogs', () => {
    const { onShowHistory, onShowReport, onAction } = renderBar(ALL);
    fireEvent.click(button('History'));
    fireEvent.click(button('Processing Report'));
    expect(onShowHistory).toHaveBeenCalledTimes(1);
    expect(onShowReport).toHaveBeenCalledTimes(1);
    expect(onAction).not.toHaveBeenCalled();
  });
});
