// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { test as base, expect, type Page } from '@playwright/test';

export interface UatMetrics {
  clicks: number;
  /**
   * Count of `input` events — i.e. how many times a field's value changed.
   *
   * NOT keystrokes: Playwright's `fill()` sets the value and dispatches `input`
   * without ever firing `keydown`, so a keydown counter reads 0 for the idiomatic
   * way tests enter text. `input` fires for both `fill()` and `type()`, so this
   * is the metric that actually measures "how many fields must a user populate".
   */
  fieldEdits: number;
  /** Real key presses. Non-zero only where a spec uses type()/press() deliberately. */
  keystrokes: number;
  navigations: number;
  /** Distinct hash routes visited, in order. Shows the path a user had to walk. */
  routes: string[];
  /** Uncaught exceptions in page context. Any value > 0 is a defect. */
  pageErrors: string[];
  /** console.error output. Often noisy, so reported but not asserted on. */
  consoleErrors: string[];
}

/**
 * Counts interactions in the browser rather than by wrapping Playwright calls.
 *
 * WHY IN-BROWSER: Playwright dispatches real DOM events, so a capture-phase
 * listener sees every click a test causes, including ones triggered indirectly
 * (a Cloudscape dropdown that needs two clicks to reach an option, a modal that
 * has to be dismissed first). Counting `locator.click()` calls in the test would
 * undercount exactly the friction we want to measure.
 *
 * Injected via addInitScript so it survives the reloads that
 * waitForDocumentTerminal() performs; counters live on a property that persists
 * across same-document navigation and is re-seeded (not reset) on full reload.
 */
const INSTRUMENT = () => {
  interface UatWindow extends Window {
    __uat?: {
      clicks: number;
      fieldEdits: number;
      keystrokes: number;
      navigations: number;
      routes: string[];
      pageErrors: string[];
      consoleErrors: string[];
    };
  }
  const w = window as UatWindow;
  // sessionStorage carries counters across full page reloads within the test.
  const prior = (() => {
    try {
      return JSON.parse(sessionStorage.getItem('__uat_metrics') ?? 'null');
    } catch {
      return null;
    }
  })();

  w.__uat = prior ?? {
    clicks: 0,
    fieldEdits: 0,
    keystrokes: 0,
    navigations: 0,
    routes: [],
    pageErrors: [],
    consoleErrors: [],
  };

  const persist = () => {
    try {
      sessionStorage.setItem('__uat_metrics', JSON.stringify(w.__uat));
    } catch {
      /* storage disabled; in-memory counts still work for a single page */
    }
  };

  const recordRoute = () => {
    const r = location.hash || '#/';
    const routes = w.__uat!.routes;
    if (routes[routes.length - 1] !== r) routes.push(r);
    persist();
  };

  document.addEventListener('click', () => { w.__uat!.clicks += 1; persist(); }, true);
  // `input` (not keydown) is what Playwright's fill() dispatches — see UatMetrics.
  document.addEventListener('input', () => { w.__uat!.fieldEdits += 1; persist(); }, true);
  document.addEventListener('keydown', () => { w.__uat!.keystrokes += 1; persist(); }, true);
  window.addEventListener('hashchange', () => { w.__uat!.navigations += 1; recordRoute(); });
  window.addEventListener('popstate', () => { w.__uat!.navigations += 1; recordRoute(); });
  recordRoute();
};

async function readMetrics(page: Page): Promise<UatMetrics | null> {
  try {
    return await page.evaluate(() => {
      const w = window as unknown as { __uat?: UatMetrics };
      return w.__uat ?? null;
    });
  } catch {
    // Page already closed / navigated away — metrics are best-effort.
    return null;
  }
}

/**
 * Extended `test` that instruments every page and attaches per-scenario metrics.
 * The custom reporter reads the `uat-metrics` attachment to build the report.
 */
export const test = base.extend<{ instrumented: void }>({
  instrumented: [
    async ({ page }, use, testInfo) => {
      const pageErrors: string[] = [];
      const consoleErrors: string[] = [];

      await page.addInitScript(INSTRUMENT);
      page.on('pageerror', (e) => pageErrors.push(e.message));
      page.on('console', (m) => {
        if (m.type() === 'error') consoleErrors.push(m.text());
      });

      await use();

      const browser = await readMetrics(page);
      const metrics: UatMetrics = {
        clicks: browser?.clicks ?? 0,
        fieldEdits: browser?.fieldEdits ?? 0,
        keystrokes: browser?.keystrokes ?? 0,
        navigations: browser?.navigations ?? 0,
        routes: browser?.routes ?? [],
        // Node-side listeners are authoritative for errors: they capture events
        // that fire before the init script runs.
        pageErrors,
        consoleErrors,
      };

      await testInfo.attach('uat-metrics', {
        body: JSON.stringify(metrics, null, 2),
        contentType: 'application/json',
      });
    },
    { auto: true },
  ],
});

export { expect };
