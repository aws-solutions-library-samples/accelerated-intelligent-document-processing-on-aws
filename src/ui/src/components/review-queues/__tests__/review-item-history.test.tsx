// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** The work screen shows why an item was skipped or rejected, newest first. */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import ReviewItemHistory, { entryDetails, eventLabel } from '../ReviewItemHistory';

const HISTORY = [
  { at: '2026-10-20T09:00:00.000Z', event: 'claimed', actor: 'ann@example.com', details: null },
  {
    at: '2026-10-20T09:30:00.000Z',
    event: 'released',
    actor: 'ann@example.com',
    details: JSON.stringify({ reason: 'Needs expert review', note: 'tax schedule' }),
  },
];

describe('ReviewItemHistory', () => {
  it('shows a skip with its reason and note, newest first', () => {
    render(<ReviewItemHistory history={HISTORY} />);
    const rows = screen.getAllByRole('row').slice(1);
    expect(within(rows[0]).getByText('Skipped')).toBeInTheDocument();
    expect(within(rows[0]).getByText('Needs expert review')).toBeInTheDocument();
    expect(within(rows[0]).getByText('tax schedule')).toBeInTheDocument();
    expect(within(rows[1]).getByText('Claimed')).toBeInTheDocument();
  });

  it('reads details defensively', () => {
    expect(entryDetails({ at: 'x', event: 'moved', details: '{"from":"a","to":"b"}' })).toEqual({});
    expect(entryDetails({ at: 'x', event: 'moved', details: 'not json' })).toEqual({});
  });

  it('labels an event it does not know readably', () => {
    expect(eventLabel('something_new')).toBe('Something new');
  });
});
