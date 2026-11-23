// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import { describe, expect, it, vi } from 'vitest';

import PolicyGroupField from '../PolicyGroupField';

const renderField = (value: string, onChange = vi.fn()) =>
  render(
    <IntlProvider locale="en" defaultLocale="en">
      <PolicyGroupField value={value} onChange={onChange} />
    </IntlProvider>,
  );

describe('PolicyGroupField', () => {
  it('renders the label and the append-not-replace constraint, with no error for an empty value', () => {
    renderField('');
    expect(screen.getByText('Policy group (optional)')).toBeTruthy();
    expect(screen.getByText(/Rules are added to the group, never replaced\./)).toBeTruthy();
    expect(screen.queryByText(/must start with a letter or digit/)).toBeNull();
  });

  it('shows the character error for a name the server would refuse', () => {
    renderField('manual/ch1');
    expect(screen.getByText(/must start with a letter or digit/)).toBeTruthy();
  });

  it('shows the length error with the limit filled in', () => {
    renderField('a'.repeat(65));
    expect(screen.getByText('Policy group must be at most 64 characters.')).toBeTruthy();
  });

  it('reports what the user typed', () => {
    const onChange = vi.fn();
    renderField('', onChange);
    const input = screen.getByLabelText('Policy group (optional)') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'NCCI Manual' } });
    expect(onChange).toHaveBeenCalledWith('NCCI Manual');
  });
});
