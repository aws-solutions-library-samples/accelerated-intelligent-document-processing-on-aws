// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { render, screen } from '@testing-library/react';
import { createIntl, IntlProvider } from 'react-intl';
import { describe, expect, it } from 'vitest';

import { TableEmptyState, TableNoMatchState, filterCounterText } from '../table';

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

describe('TableEmptyState', () => {
  it('renders the title and description it is given, without an action', () => {
    const { container } = render(<TableEmptyState title="No documents" description="No documents found." />);

    expect(screen.getByText('No documents')).toBeTruthy();
    expect(screen.getByText('No documents found.')).toBeTruthy();
    expect(container.querySelectorAll('button')).toHaveLength(0);
  });

  it('renders a title, description and action', () => {
    render(
      <TableEmptyState title="No messages" description="Nothing needs your attention." action={<button type="button">Refresh</button>} />,
    );

    expect(screen.getByText('No messages')).toBeTruthy();
    expect(screen.getByText('Nothing needs your attention.')).toBeTruthy();
    expect(screen.queryByText('No messages found.')).toBeNull();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy();
  });
});

describe('TableNoMatchState', () => {
  it('says no row matches and offers to clear the filter', () => {
    render(<TableNoMatchState />, { wrapper: English });

    expect(screen.getByText('No matches')).toBeTruthy();
    expect(screen.getByText("We can't find a match.")).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Clear filter' })).toBeTruthy();
  });
});

describe('filterCounterText', () => {
  const intl = createIntl({ locale: 'en', defaultLocale: 'en' });

  it('singularises one match', () => {
    expect(filterCounterText(intl, 1)).toBe('1 match');
  });

  it('pluralises every other count, including zero', () => {
    expect(filterCounterText(intl, 0)).toBe('0 matches');
    expect(filterCounterText(intl, 2)).toBe('2 matches');
  });
});
