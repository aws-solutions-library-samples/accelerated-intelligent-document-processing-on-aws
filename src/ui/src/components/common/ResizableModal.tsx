// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A Cloudscape Modal the user can widen or narrow. It opens at `size="max"`, which
// fits the window, and a handle on its right edge changes the width: drag it, use the
// arrow keys on it, or double-click it (or press Home) to fit the window again. The
// width is passed to Modal's own `width` prop, which Cloudscape clamps to the
// viewport, so the dialog can never be dragged off-screen.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { ModalProps } from '@cloudscape-design/components';
import { Modal } from '@cloudscape-design/components';

/** Narrowest width the handle drags to; Cloudscape's own floor is 320px. */
export const MIN_RESIZABLE_MODAL_WIDTH = 480;
/** Pixels one arrow-key press moves the edge by. */
const KEYBOARD_STEP = 40;

type ResizableModalProps = Omit<ModalProps, 'size' | 'width'>;

const handleStyle: React.CSSProperties = {
  position: 'absolute',
  top: 0,
  bottom: 0,
  // Sits in the dialog's right-hand content padding, on the edge being moved.
  right: -16,
  width: 12,
  cursor: 'col-resize',
  touchAction: 'none',
  display: 'flex',
  alignItems: 'flex-start',
  justifyContent: 'center',
};

const gripStyle: React.CSSProperties = {
  width: 4,
  height: 48,
  borderRadius: 2,
  backgroundColor: '#8d99a8',
  // Stays in view while a long comparison scrolls inside the dialog.
  position: 'sticky',
  top: 16,
};

// The dialog box is the widest child of the element Cloudscape gives role="dialog";
// the other children are zero-width focus sentinels.
const measureDialogWidth = (el: HTMLElement | null): number | undefined => {
  const root = el?.closest('[role="dialog"]');
  if (!root) return undefined;
  const widths = Array.from(root.children).map((c) => c.getBoundingClientRect().width);
  const widest = Math.max(0, ...widths);
  return widest > 0 ? widest : undefined;
};

const ResizableModal = ({ children, visible, ...rest }: ResizableModalProps): React.JSX.Element => {
  // undefined = fit the window (size="max"); a number = the width the user chose.
  const [width, setWidth] = useState<number | undefined>(undefined);
  const handleRef = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ startX: number; startWidth: number } | null>(null);

  useEffect(() => {
    // Every opening starts fitted to the window again.
    if (visible) setWidth(undefined);
  }, [visible]);

  const clamp = useCallback((w: number) => Math.max(MIN_RESIZABLE_MODAL_WIDTH, Math.min(Math.round(w), window.innerWidth)), []);

  const onPointerMove = useCallback(
    (e: PointerEvent) => {
      if (!drag.current) return;
      // The dialog is centred, so moving its right edge by dx widens it by 2 * dx.
      setWidth(clamp(drag.current.startWidth + 2 * (e.clientX - drag.current.startX)));
    },
    [clamp],
  );

  const onPointerUp = useCallback(() => {
    drag.current = null;
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', onPointerUp);
  }, [onPointerMove]);

  useEffect(() => onPointerUp, [onPointerUp]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    const current = width ?? measureDialogWidth(handleRef.current);
    if (current === undefined) return;
    e.preventDefault();
    drag.current = { startX: e.clientX, startWidth: current };
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>): void => {
    if (e.key === 'Home') {
      e.preventDefault();
      setWidth(undefined);
      return;
    }
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const current = width ?? measureDialogWidth(handleRef.current);
    if (current === undefined) return;
    e.preventDefault();
    setWidth(clamp(current + (e.key === 'ArrowRight' ? 2 : -2) * KEYBOARD_STEP));
  };

  return (
    <Modal {...rest} visible={visible} size="max" width={width}>
      <div style={{ position: 'relative' }}>
        {children}
        {/* A focusable role="separator" is the WAI-ARIA window-splitter pattern, an
            interactive widget; jsx-a11y classes every separator as static. */}
        {/* eslint-disable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
        <div
          ref={handleRef}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize dialog. Arrow keys change the width; Home fits the window."
          aria-valuenow={width}
          tabIndex={0}
          title="Drag to resize. Double-click to fit the window."
          style={handleStyle}
          onPointerDown={onPointerDown}
          onDoubleClick={() => setWidth(undefined)}
          onKeyDown={onKeyDown}
        >
          <span style={gripStyle} />
        </div>
        {/* eslint-enable jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex */}
      </div>
    </Modal>
  );
};

export default ResizableModal;
