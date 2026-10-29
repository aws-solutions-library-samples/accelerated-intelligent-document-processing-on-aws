// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen's bar renders the same DOM through the shared ActionBar as it did
 * before the bar was shared. The snapshot was written on the bar as it was, before
 * EditActions mapped onto ActionBar; each prop set covers one of its branches. Ids
 * Cloudscape generates (and the attributes that reference them) and its analytics
 * attributes (render order, timestamps) are normalised, since they don't depend on the bar.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

import EditScreen from '../EditScreen';
import type { EditActionsProps } from '../EditScreen';

const normalize = (html: string): string =>
  html.replace(/\b(id|aria-describedby|aria-labelledby|aria-controls|for|data-analytics-[a-z-]+)="[^"]*"/g, '$1="X"');

const ALL = { showRegroup: true, showSkip: true, showCancel: true, showSave: true };

const CASES: [string, Partial<EditActionsProps>, { summaryReportUri?: string; summaryUnavailableReason?: string; report?: boolean }?][] = [
  ['default', {}],
  ['every action, with baseline', { ...ALL, showBaseline: true }],
  ['claim', { ...ALL, showClaim: true }],
  ["busy='save'", { ...ALL, showBaseline: true, busy: 'save' }],
  ["busy='claim'", { showClaim: true, busy: 'claim' }],
  [
    'every reason set',
    {
      ...ALL,
      showBaseline: true,
      baselineDisabledReason: 'baseline reason',
      regroupDisabledReason: 'regroup reason',
      skipDisabledReason: 'skip reason',
      cancelDisabledReason: 'cancel reason',
      saveDisabledReason: 'save reason',
      primaryDisabledReason: 'primary reason',
    },
  ],
  [
    'view switch',
    { ...ALL, view: 'source', onViewChange: () => undefined, primaryHint: 'primary hint' },
    { summaryReportUri: 's3://b/s.md', report: true },
  ],
  ['summary disabled', { ...ALL }, { summaryUnavailableReason: 'Available once the review is complete' }],
];

describe('the edit screen bar DOM', () => {
  it.each(CASES)('is unchanged: %s', (_name, actions, extra = {}) => {
    render(
      <EditScreen
        title="doc.pdf"
        objectKey="in/doc.pdf"
        summaryReportUri={extra.summaryReportUri}
        summaryUnavailableReason={extra.summaryUnavailableReason}
        reportSectionData={extra.report ? null : undefined}
        actions={{ busy: null, onAction: vi.fn(), onShowHistory: vi.fn(), ...actions }}
      >
        <div />
      </EditScreen>,
    );
    const bar = createWrapper(document.body).findHeader()!.findActions()!.getElement();
    expect(normalize(bar.outerHTML)).toMatchSnapshot();
  });
});
