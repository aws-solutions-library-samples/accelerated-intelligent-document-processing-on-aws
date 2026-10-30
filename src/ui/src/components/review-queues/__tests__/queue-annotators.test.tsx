// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A queue is worked by the Annotators listed on it; an empty list means none (Admins
 * still work every queue). Review Management's Annotators column shows how many are
 * listed, and Assign offers only those.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));

import { AnnotatorsCell } from '../QueuesPage';
import { eligibleAnnotators } from '../reviewer-assignment';

describe('the Annotators column', () => {
  it('shows the number of listed Annotators, with their names on hover', () => {
    render(<AnnotatorsCell eligible={['a@example.com', 'b@example.com']} />);
    const cell = screen.getByText('2');
    expect(cell.getAttribute('title')).toBe('a@example.com, b@example.com');
  });

  it('shows 0 when no Annotator is listed', () => {
    render(<AnnotatorsCell eligible={[]} />);
    expect(screen.getByText('0').getAttribute('title')).toBe('No Annotators listed');
  });

  it('treats a missing list as none listed', () => {
    render(<AnnotatorsCell eligible={null} />);
    expect(screen.getByText('0')).toBeTruthy();
  });
});

describe('eligibleAnnotators', () => {
  const all = ['a@example.com', 'B@example.com', 'c@example.com'];

  it('offers only the Annotators listed on the queue, case-insensitively', () => {
    expect(eligibleAnnotators(all, { eligibleAnnotators: ['b@example.com'] })).toEqual(['B@example.com']);
  });

  it('offers no one when the queue lists no Annotator', () => {
    expect(eligibleAnnotators(all, { eligibleAnnotators: [] })).toEqual([]);
    expect(eligibleAnnotators(all, null)).toEqual([]);
  });
});
