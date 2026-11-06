// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The resize handle on the configuration-comparison dialog. jsdom has no layout, so
 * the dialog's measured width is stubbed: the handle reads it at the start of every
 * resize, and a modal that cannot be measured must not jump to the minimum width.
 */

import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import ResizableModal, { MIN_RESIZABLE_MODAL_WIDTH } from '../ResizableModal';

const HANDLE = /Resize dialog/;

const stubDialogWidth = (width: number): void => {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
    width,
    height: 400,
    top: 0,
    left: 0,
    right: width,
    bottom: 400,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
};

const renderModal = (): void => {
  render(
    <ResizableModal visible header="Compare" onDismiss={() => undefined}>
      <div>diff</div>
    </ResizableModal>,
  );
};

describe('ResizableModal', () => {
  afterEach(() => vi.restoreAllMocks());

  it('opens fitted to the window, with no explicit width', () => {
    renderModal();
    expect(screen.getByRole('separator', { name: HANDLE })).not.toHaveAttribute('aria-valuenow');
  });

  it('widens and narrows from the measured width with the arrow keys, twice the step because it is centred', () => {
    stubDialogWidth(1000);
    renderModal();
    const handle = screen.getByRole('separator', { name: HANDLE });
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    expect(handle).toHaveAttribute('aria-valuenow', '920');
    fireEvent.keyDown(handle, { key: 'ArrowRight' });
    expect(handle).toHaveAttribute('aria-valuenow', '1000');
    // ...and never wider than the window (jsdom's is 1024px).
    fireEvent.keyDown(handle, { key: 'ArrowRight' });
    expect(handle).toHaveAttribute('aria-valuenow', String(window.innerWidth));
  });

  it('never narrows below the minimum', () => {
    stubDialogWidth(MIN_RESIZABLE_MODAL_WIDTH + 20);
    renderModal();
    const handle = screen.getByRole('separator', { name: HANDLE });
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    expect(handle).toHaveAttribute('aria-valuenow', String(MIN_RESIZABLE_MODAL_WIDTH));
  });

  it('fits the window again on Home and on double-click', () => {
    stubDialogWidth(1000);
    renderModal();
    const handle = screen.getByRole('separator', { name: HANDLE });
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    fireEvent.keyDown(handle, { key: 'Home' });
    expect(handle).not.toHaveAttribute('aria-valuenow');
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    fireEvent.doubleClick(handle);
    expect(handle).not.toHaveAttribute('aria-valuenow');
  });

  it('does nothing when the dialog cannot be measured', () => {
    stubDialogWidth(0);
    renderModal();
    const handle = screen.getByRole('separator', { name: HANDLE });
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    expect(handle).not.toHaveAttribute('aria-valuenow');
  });
});
