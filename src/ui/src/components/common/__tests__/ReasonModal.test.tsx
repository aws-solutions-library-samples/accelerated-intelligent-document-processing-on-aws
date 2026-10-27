// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** A review action that is recorded in history cannot be confirmed without a reason. */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import ReasonModal from '../ReasonModal';

const renderModal = (onConfirm = vi.fn()) => {
  render(
    <ReasonModal
      visible
      header="Skip this item"
      reasons={['Out of time', 'Other (see note)']}
      confirmLabel="Skip"
      onDismiss={() => undefined}
      onConfirm={onConfirm}
    >
      What skipping does.
    </ReasonModal>,
  );
  const modal = createWrapper(document.body).findModal()!;
  const confirm = () =>
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip')!
      .click();
  return { modal, confirm, onConfirm };
};

describe('ReasonModal', () => {
  it('refuses to confirm until a reason is chosen', () => {
    const { modal, confirm, onConfirm } = renderModal();
    confirm();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(modal.findContent().getElement().textContent).toContain('Choose a reason.');
  });

  it('confirms with the chosen reason and the trimmed note', () => {
    const { modal, confirm, onConfirm } = renderModal();
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Out of time');
    modal.findContent().findTextarea()!.setTextareaValue('  back tomorrow  ');
    confirm();
    expect(onConfirm).toHaveBeenCalledWith('Out of time', 'back tomorrow');
  });

  it('sends no note when it is blank', () => {
    const { modal, confirm, onConfirm } = renderModal();
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Other (see note)');
    modal.findContent().findTextarea()!.setTextareaValue('   ');
    confirm();
    expect(onConfirm).toHaveBeenCalledWith('Other (see note)', undefined);
  });
});
