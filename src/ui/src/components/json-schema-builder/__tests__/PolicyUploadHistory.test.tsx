// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { render, screen } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import { describe, expect, it } from 'vitest';

import PolicyUploadHistory from '../PolicyUploadHistory';

const renderHistory = (uploads: unknown) =>
  render(
    <IntlProvider locale="en" defaultLocale="en">
      <PolicyUploadHistory uploads={uploads} />
    </IntlProvider>,
  );

describe('PolicyUploadHistory', () => {
  it('lists each upload with the rules it supplied and whose rules it superseded', () => {
    renderHistory([
      {
        uploadId: 'u1',
        document: 'ch1.pdf',
        savedAt: '2026-11-20T10:00:00+00:00',
        mode: 'append',
        rules: ['limit', 'modifier'],
        superseded: [],
      },
      {
        uploadId: 'r1',
        document: 'rider.pdf',
        savedAt: '2026-11-20T11:00:00+00:00',
        mode: 'supersede',
        rules: ['limit'],
        superseded: [
          { rule: 'limit', uploadId: 'u1', document: 'ch1.pdf' },
          { rule: 'by_hand', uploadId: null, document: null },
        ],
      },
    ]);
    expect(screen.getByText('Policy Discovery uploads')).toBeTruthy();
    expect(screen.getByText('ch1.pdf')).toBeTruthy();
    expect(screen.getByText('rider.pdf')).toBeTruthy();
    expect(screen.getByText('limit, modifier')).toBeTruthy();
    expect(screen.getByText('Superseded chosen rules')).toBeTruthy();
    expect(screen.getByText('limit (from ch1.pdf), by_hand (from an unrecorded upload)')).toBeTruthy();
  });

  it('renders nothing for a class with no recorded uploads', () => {
    const { container } = renderHistory(undefined);
    expect(container.textContent).toBe('');
  });
});
