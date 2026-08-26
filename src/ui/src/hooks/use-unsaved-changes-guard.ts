// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Confirm before in-app navigation discards unsaved edits.
 *
 * `beforeunload` covers closing the tab or reloading, and nothing more — an
 * in-app route change is not a page unload, so a reviewer who corrected ten
 * fields and then clicked a nav link lost all ten silently, with no prompt and no
 * toast.
 *
 * React Router's `useBlocker` would be the right tool and is not available here:
 * it requires a data router (`createHashRouter` / `createBrowserRouter`), and the
 * app mounts a plain `<HashRouter>`. Switching router styles to guard one form is
 * not a trade worth making, so this intercepts the click instead.
 *
 * Capture phase on `document`, so it runs before the router's own handler and can
 * still cancel. Scoped to in-app hash links (`href` starting `#/`), which is what
 * the side navigation and breadcrumbs render under HashRouter — an external link
 * or a plain button is left alone.
 */

import { useEffect } from 'react';

export const useUnsavedChangesGuard = (hasUnsavedChanges: boolean, message: string): void => {
  useEffect(() => {
    if (!hasUnsavedChanges) return undefined;

    // Tab close / reload. The browser shows its own wording; the message is only
    // used for the in-app case below.
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Assigning returnValue is what actually triggers the prompt in some
      // browsers; preventDefault alone is not universally honoured.
      event.returnValue = '';
    };

    const onClickCapture = (event: MouseEvent) => {
      // Only a plain left click can be a navigation we are able to cancel;
      // modifier-clicks open a new tab and leave this page (and its edits) intact.
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return;
      }
      const anchor = (event.target as HTMLElement | null)?.closest?.('a');
      const href = anchor?.getAttribute('href');
      if (!href || !href.startsWith('#/')) return;

      // Navigating to where we already are discards nothing.
      if (href.slice(1) === window.location.hash.slice(1)) return;

      if (!window.confirm(message)) {
        event.preventDefault();
        event.stopPropagation();
      }
    };

    window.addEventListener('beforeunload', onBeforeUnload);
    document.addEventListener('click', onClickCapture, true);
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload);
      document.removeEventListener('click', onClickCapture, true);
    };
  }, [hasUnsavedChanges, message]);
};

export default useUnsavedChangesGuard;
