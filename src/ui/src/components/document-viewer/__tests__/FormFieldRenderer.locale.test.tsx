// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A number shown next to a field follows the UI locale; a number the user edits does not.
 *
 * The confidence line is prose: under `fr-CA` it reads `100,0 %`, the French shape `Intl`
 * gives for the ratio, with the locale's decimal mark and a (no-break) space before the sign.
 * The numeric input beside it is parsed back (`Number(detail.value)`), so its value stays
 * `String(value)` in every locale: `1234.5`, never `1 234,5`, which `Number` would read as
 * `NaN`. The second case renders under an English provider and checks the English text is what
 * it has always been.
 */

import React from 'react';
import { IntlProvider } from 'react-intl';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import FormFieldRenderer from '../FormFieldRenderer';

/** Confidence reaches the renderer through the assessment's explainability data, keyed by field name. */
const EXPLAINABILITY = [{ Amount: { confidence: 1.0 } }];

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const field = (onChange: (value: unknown) => void): React.JSX.Element => (
  <FormFieldRenderer
    fieldKey="Amount"
    value={1234.5}
    onChange={onChange}
    isReadOnly={false}
    explainabilityInfo={EXPLAINABILITY}
    path={['Amount']}
  />
);

describe('FormFieldRenderer numbers under a locale', () => {
  it('shows the confidence in French while the editable number stays locale-neutral', async () => {
    const onChange = vi.fn();
    renderWithIntl(field(onChange), { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'] });

    // Awaited, as the helper's notes ask of the first assertion: the provider's catalog load
    // settles inside the wait instead of after the test body.
    expect(await screen.findByText(/Confidence: 100,0\s%/)).toBeInTheDocument();

    const input = screen.getByRole('spinbutton');
    expect(input).toHaveValue(1234.5);
    expect((input as HTMLInputElement).value).toBe('1234.5');

    fireEvent.change(input, { target: { value: '1234.6' } });
    expect(onChange).toHaveBeenCalledWith(1234.6);
  });

  it('shows the English confidence under an English provider, as before', () => {
    render(field(vi.fn()), { wrapper: English });
    expect(screen.getByText(/Confidence: 100\.0%/)).toBeInTheDocument();
  });
});
