// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The publish dialog states what publishing does before it happens, in the terms that
 * matter: which version number is being created, how many documents it freezes, and
 * whether the set's scoring baseline moves with it.
 *
 * It replaced a one-click menu item on the test-set table that silently repointed the
 * active reference and never collected the label or notes the mutation accepts
 * (GitHub #903).
 */

import { fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import PublishVersionModal from '../PublishVersionModal';

const renderModal = (overrides: Partial<React.ComponentProps<typeof PublishVersionModal>> = {}) => {
  const onConfirm = vi.fn();
  const onDismiss = vi.fn();
  const view = render(
    <PublishVersionModal
      visible
      testSetId="bank-statements"
      documentCount={12}
      latestVersion={2}
      submitting={false}
      onDismiss={onDismiss}
      onConfirm={onConfirm}
      {...overrides}
    />,
  );
  return { onConfirm, onDismiss, ...view };
};

describe('PublishVersionModal', () => {
  it('names the version it is about to create and what it freezes', () => {
    renderModal();
    expect(screen.getByText('Publish version 3 of bank-statements')).toBeTruthy();
    expect(screen.getByText(/these 12 document\(s\)/)).toBeTruthy();
    expect(screen.getByText(/never rewritten/)).toBeTruthy();
  });

  it('stays usable when the existing versions could not be read', () => {
    // The number is context; the server assigns the real one either way, so a failed
    // version read must not block publishing.
    renderModal({ latestVersion: null });
    expect(screen.getByText('Publish a version of bank-statements')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Publish version' })).not.toBeDisabled();
  });

  it('passes the label, notes and active-reference choice to the caller', () => {
    const { onConfirm } = renderModal();
    fireEvent.change(screen.getByPlaceholderText('Defaults to v3'), { target: { value: '  reviewed by finance  ' } });
    fireEvent.change(screen.getByRole('textbox', { name: /Notes/i }), { target: { value: 'Q3 corrections' } });
    fireEvent.click(screen.getByRole('button', { name: 'Publish version' }));
    expect(onConfirm).toHaveBeenCalledWith({
      label: 'reviewed by finance',
      notes: 'Q3 corrections',
      setAsActiveReference: true,
    });
  });

  it('omits an untouched label and notes rather than sending empty strings', () => {
    const { onConfirm } = renderModal();
    fireEvent.click(screen.getByRole('button', { name: 'Publish version' }));
    expect(onConfirm).toHaveBeenCalledWith({ label: undefined, notes: undefined, setAsActiveReference: true });
  });

  it('explains what is left alone when the active reference is not moved', () => {
    const { onConfirm } = renderModal();
    expect(screen.queryByText(/active reference is left as it is/)).toBeNull();

    fireEvent.click(screen.getByRole('checkbox', { name: /Make this the active reference/ }));
    expect(screen.getByText(/active reference is left as it is/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Publish version' }));
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ setAsActiveReference: false }));
  });

  it('refuses an empty set, and says why', () => {
    renderModal({ documentCount: 0 });
    expect(screen.getByText(/no documents\. Add documents before publishing/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Publish version' })).toBeDisabled();
  });

  it('does not carry a previous entry into the next publish', () => {
    const { rerender, onConfirm, onDismiss } = renderModal();
    fireEvent.change(screen.getByPlaceholderText('Defaults to v3'), { target: { value: 'first attempt' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /Make this the active reference/ }));

    const props = {
      testSetId: 'bank-statements',
      documentCount: 12,
      latestVersion: 3,
      submitting: false,
      onDismiss,
      onConfirm,
    };
    rerender(<PublishVersionModal visible={false} {...props} />);
    rerender(<PublishVersionModal visible {...props} />);

    expect((screen.getByPlaceholderText('Defaults to v4') as HTMLInputElement).value).toBe('');
    expect(screen.getByRole('checkbox', { name: /Make this the active reference/ })).toBeChecked();
  });
});
