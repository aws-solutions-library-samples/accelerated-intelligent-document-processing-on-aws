// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { TableEmptyState } from '../table';

describe('TableEmptyState', () => {
  it('keeps the default wording when no overrides are given', () => {
    const { container } = render(<TableEmptyState resourceName="Document" />);
    expect(screen.getByText('No documents')).toBeTruthy();
    expect(screen.getByText('No documents found.')).toBeTruthy();
    expect(container.querySelectorAll('button')).toHaveLength(0);
  });

  it('renders a custom title, description and action', () => {
    render(
      <TableEmptyState
        resourceName="Message"
        title="No messages"
        description="Nothing needs your attention."
        action={<button type="button">Refresh</button>}
      />,
    );
    expect(screen.getByText('No messages')).toBeTruthy();
    expect(screen.getByText('Nothing needs your attention.')).toBeTruthy();
    expect(screen.queryByText('No messages found.')).toBeNull();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy();
  });
});
