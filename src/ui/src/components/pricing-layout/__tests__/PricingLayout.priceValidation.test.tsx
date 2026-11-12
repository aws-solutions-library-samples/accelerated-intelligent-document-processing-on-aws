// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Pricing page refuses a price the backend will refuse (#1903).
 *
 * The page is rendered for real under react-intl; only `usePricing` and
 * `useUserRole` are mocked, at the module boundary. A table holding `0x10` (which
 * `Number()` reads as 16 and the cost reports cannot read) is what a stack saved
 * before prices were validated can return, so it is the input here.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { IntlProvider } from 'react-intl';
import createWrapper, { InputWrapper } from '@cloudscape-design/components/test-utils/dom';

vi.mock('@monaco-editor/react', () => ({ default: () => null }));

const pricingState = vi.hoisted(() => ({
  pricing: null as unknown,
  updatePricing: null as unknown as ReturnType<typeof vi.fn>,
}));
vi.mock('../../../hooks/use-pricing', () => ({
  default: () => ({
    pricing: pricingState.pricing,
    defaultPricing: pricingState.pricing,
    nonChargeableUnits: {},
    loading: false,
    refreshing: false,
    error: null,
    fetchPricing: vi.fn(),
    updatePricing: pricingState.updatePricing,
    restoreDefaultPricing: vi.fn(),
  }),
}));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: true, loading: false, sessionError: false, retrySession: vi.fn() }),
}));

import PricingLayout from '../PricingLayout';

const table = (price: string) => ({
  pricing: [
    {
      name: 'bedrock/m',
      units: [
        { name: 'inputTokens', price },
        { name: 'outputTokens', price: '2.0E-6' },
      ],
    },
  ],
});

const renderWith = (price: string) => {
  pricingState.pricing = table(price);
  const result = render(
    <IntlProvider locale="en" defaultLocale="en">
      <PricingLayout />
    </IntlProvider>,
  );
  return createWrapper(result.container);
};

const INLINE = 'Enter a non-negative decimal number with no spaces, such as 0.0015 or 6.0E-8.';

describe('price validation on the Pricing page', () => {
  beforeEach(() => {
    pricingState.updatePricing = vi.fn().mockResolvedValue(true);
  });

  it('shows no price error for a valid table and saves it', async () => {
    renderWith('3.0E-7');
    expect(screen.queryByText('Invalid prices')).toBeNull();
    await act(async () => {
      screen.getByRole('button', { name: 'Save changes' }).click();
    });
    expect(pricingState.updatePricing).toHaveBeenCalledTimes(1);
  });

  it('lists an invalid price, marks its field and refuses to save', async () => {
    const wrapper = renderWith('0x10');
    expect(screen.getByText('Invalid prices')).toBeTruthy();
    expect(
      screen.getByText(
        'bedrock/m / inputTokens: "0x10" is not a valid price. Use a non-negative decimal number with no spaces, such as 0.0015 or 6.0E-8.',
      ),
    ).toBeTruthy();

    // The section is collapsed by default; expand it to reach the field.
    act(() => {
      wrapper.findExpandableSection()!.findExpandButton().click();
    });
    const errors = wrapper.findAllFormFields().map((f) => f.findError()?.getElement().textContent ?? null);
    expect(errors.filter((e) => e === INLINE)).toHaveLength(1);

    await act(async () => {
      screen.getByRole('button', { name: 'Save changes' }).click();
    });
    expect(pricingState.updatePricing).not.toHaveBeenCalled();
    expect(
      screen.getByText('Cannot save: 1 price is not valid. Correct the prices listed under Invalid prices and save again.'),
    ).toBeTruthy();
  });

  it('will not add a service whose price is invalid', () => {
    const wrapper = renderWith('3.0E-7');
    act(() => {
      screen.getByRole('button', { name: 'Add Service/API' }).click();
    });
    // Modals render into document.body, outside the page's container.
    expect(wrapper.findModal()).toBeNull();
    const modal = createWrapper()
      .findAllModals()
      .find((m) => m.isVisible())!;
    expect(modal.findHeader().getElement().textContent).toBe('Add Service/API Pricing');
    const inputs = modal.findAllByClassName(InputWrapper.rootSelector).map((w) => new InputWrapper(w.getElement()));
    expect(inputs).toHaveLength(3);
    act(() => {
      inputs[0].setInputValue('bedrock/new');
      inputs[1].setInputValue('inputTokens');
      inputs[2].setInputValue('0x10');
    });
    const add = modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Add')!;
    expect(add.isDisabled()).toBe(true);
    expect(modal.getElement().textContent).toContain(INLINE);
    act(() => {
      inputs[2].setInputValue('6.0E-8');
    });
    expect(add.isDisabled()).toBe(false);
    expect(modal.getElement().textContent).not.toContain(INLINE);
  });
});
