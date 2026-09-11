// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Guarding unsaved edits against in-app navigation.
 *
 * `beforeunload` was already handled and covers only tab close and reload. An
 * in-app route change is not a page unload, so a reviewer who corrected ten fields
 * and clicked a nav link lost all ten with no prompt — which is the case this
 * exists for, and the case the previous handler could never have caught.
 */

import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import useUnsavedChangesGuard from '../use-unsaved-changes-guard';

const clickAnchor = (href: string, init: MouseEventInit = {}) => {
  const a = document.createElement('a');
  a.setAttribute('href', href);
  document.body.appendChild(a);
  const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...init });
  a.dispatchEvent(event);
  a.remove();
  return event;
};

describe('useUnsavedChangesGuard', () => {
  beforeEach(() => {
    window.location.hash = '#/test-studio/sets/ts1/annotate';
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('cancels an in-app navigation when the user declines', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderHook(() => useUnsavedChangesGuard(true, 'discard?'));

    const event = clickAnchor('#/documents');

    expect(window.confirm).toHaveBeenCalledWith('discard?');
    expect(event.defaultPrevented).toBe(true);
  });

  it('lets the navigation through when the user accepts', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderHook(() => useUnsavedChangesGuard(true, 'discard?'));

    const event = clickAnchor('#/documents');

    // Both halves matter. Asserting only that the click went through would pass
    // just as well against a hook that never asked — which is the bug.
    expect(confirm).toHaveBeenCalledWith('discard?');
    expect(event.defaultPrevented).toBe(false);
  });

  it('does not prompt when there is nothing unsaved', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderHook(() => useUnsavedChangesGuard(false, 'discard?'));

    const event = clickAnchor('#/documents');

    expect(confirm).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it('ignores links that are not in-app routes', () => {
    // An external link or a download leaves this page intact, so there is nothing
    // to protect and a prompt would just be noise.
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderHook(() => useUnsavedChangesGuard(true, 'discard?'));

    clickAnchor('https://example.com/docs');
    clickAnchor('#version');

    expect(confirm).not.toHaveBeenCalled();
  });

  it('ignores a modifier-click, which opens a new tab and discards nothing', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderHook(() => useUnsavedChangesGuard(true, 'discard?'));

    clickAnchor('#/documents', { metaKey: true });
    clickAnchor('#/documents', { ctrlKey: true });

    expect(confirm).not.toHaveBeenCalled();
  });

  it('does not prompt for a link to where we already are', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderHook(() => useUnsavedChangesGuard(true, 'discard?'));

    clickAnchor('#/test-studio/sets/ts1/annotate');

    expect(confirm).not.toHaveBeenCalled();
  });

  it('is actually listening in each of the cases the negative tests rely on', () => {
    // The tests above assert an ABSENCE of prompting, which a hook that listens to
    // nothing satisfies for free. This pins the discriminator: the same setup,
    // with the one thing changed that should make it prompt.
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderHook(() => useUnsavedChangesGuard(true, 'discard?'));

    clickAnchor('#/somewhere-else');

    expect(confirm).toHaveBeenCalledTimes(1);
  });

  it('stops guarding once unmounted', () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const { unmount } = renderHook(() => useUnsavedChangesGuard(true, 'discard?'));
    unmount();

    const event = clickAnchor('#/documents');

    expect(confirm).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });
});
