// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** ReasonModal's optional controls: a disabled-confirm reason, blocked dismissal, extras. */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

import ReasonModal from '../ReasonModal';

const renderModal = (props: Partial<React.ComponentProps<typeof ReasonModal>> = {}) => {
  const onConfirm = vi.fn();
  const onDismiss = vi.fn();
  render(
    <ReasonModal
      visible
      header="Skip All Reviews"
      reasons={['Duplicate document']}
      confirmLabel="Skip All Reviews"
      onDismiss={onDismiss}
      onConfirm={onConfirm}
      {...props}
    />,
  );
  const modal = createWrapper(document.body).findModal()!;
  const button = (label: string) =>
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => (b.getElement().textContent ?? '').startsWith(label))!;
  return { modal, button, onConfirm, onDismiss };
};

describe('ReasonModal extensions', () => {
  it('disables Confirm with a reason and never confirms', () => {
    const { modal, button, onConfirm } = renderModal({ confirmDisabledReason: 'Counting pending reviews…' });
    modal.findContent().findSelect()!.openDropdown();
    modal.findContent().findSelect()!.selectOption(1);
    const confirm = button('Skip All Reviews');
    expect(confirm.isDisabled()).toBe(true);
    confirm.click();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('blocks Cancel while dismissal is disabled', () => {
    const { button } = renderModal({ dismissDisabled: true });
    expect(button('Cancel').isDisabled()).toBe(true);
  });

  it('renders extra controls', () => {
    const { modal } = renderModal({ extra: <span>Resubmit for downstream processing</span> });
    expect(modal.findContent().getElement().textContent).toContain('Resubmit for downstream processing');
  });
});
