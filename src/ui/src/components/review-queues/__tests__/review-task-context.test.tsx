// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The reasons panel shows the five worst alerts and counts the rest.
 *
 * A document with many low-confidence fields used to list every stored reason (up to
 * the server's cap of 20) as a wall of badges above the editor. The server sorts worst
 * first, so the first five are the ones to read; "+N more alerts" counts everything
 * else, including alerts beyond what the server stores.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ReviewTaskContext, { TOP_REASONS } from '../ReviewTaskContext';
import type { ReviewItemDetail } from '../../../graphql/generated/operation-types';

const reason = (i: number) => ({
  kind: 'low_confidence',
  sectionId: '1',
  field: `field_${i}`,
  label: `Field ${i}`,
  score: 0.1 + i / 100,
  threshold: 0.8,
});

const detail = (stored: number, total: number) =>
  ({
    item: { reasons: Array.from({ length: stored }, (_, i) => reason(i)), reasonCount: total, escalated: false },
    checklist: [],
  }) as unknown as ReviewItemDetail;

const renderPanel = (stored: number, total: number) => render(<ReviewTaskContext detail={detail(stored, total)} />);

const shownReasons = () => screen.queryAllByText(/^Low confidence: Field \d+/);

describe('ReviewTaskContext reasons', () => {
  it('is headed Review Reason and shows no checklist', () => {
    render(<ReviewTaskContext detail={{ ...detail(3, 3), checklist: ['Check every highlighted field'] } as unknown as ReviewItemDetail} />);
    expect(screen.getByRole('heading', { name: /Review Reason/ })).toBeInTheDocument();
    expect(screen.queryByText('Check every highlighted field')).toBeNull();
    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('shows only the top five and counts every other alert, stored or not', () => {
    renderPanel(20, 37);
    expect(shownReasons()).toHaveLength(TOP_REASONS);
    expect(screen.getByText('Low confidence: Field 0', { exact: false })).toBeInTheDocument();
    expect(screen.queryByText('Low confidence: Field 5', { exact: false })).toBeNull();
    expect(screen.getByRole('button', { name: 'Show 32 more alerts' })).toBeInTheDocument();
  });

  it('expands to every stored reason and collapses again', async () => {
    renderPanel(20, 37);
    await userEvent.click(screen.getByRole('button', { name: /more alerts/ }));
    expect(shownReasons()).toHaveLength(20);
    // What the server did not store stays a count.
    expect(screen.getByText('+17 more alerts')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
    expect(shownReasons()).toHaveLength(TOP_REASONS);
  });

  it('offers no toggle when five or fewer alerts exist', () => {
    renderPanel(3, 3);
    expect(shownReasons()).toHaveLength(3);
    expect(screen.queryByRole('button', { name: /more alert/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Show fewer' })).toBeNull();
  });
});
