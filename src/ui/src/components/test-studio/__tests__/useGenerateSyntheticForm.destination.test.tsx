// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A caller standing on a test set (its detail page) opens the generator already
 * pointed at that set. The form must start in "Add to existing test set" with the
 * set chosen, submit with its id rather than a new name, and return there on reset —
 * while a caller that passes nothing gets the unchanged "new set" default.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const generateFromPrompt = vi.fn();
const generateFromConfig = vi.fn();
vi.mock('../../../hooks/use-synthetic-data-generator', () => ({
  default: () => ({
    submitting: false,
    generateFromPrompt: (...a: unknown[]) => generateFromPrompt(...a),
    generateFromConfig: (...a: unknown[]) => generateFromConfig(...a),
    suggestScenario: async () => [],
    getEstimate: async () => null,
  }),
}));
vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({ versions: [], fetchVersion: async () => null }),
}));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: async () => ({ data: { getTestSets: [{ id: 'ts1', name: 'TS One' }] } }) }),
}));
vi.mock('../../../graphql/generated', () => ({ getTestSets: 'getTestSets' }));

// Imported after the mocks, which vitest hoists.
import { useGenerateSyntheticForm } from '../useGenerateSyntheticForm';

const Harness = ({ destination }: { destination?: { testSetId: string; label: string } }) => {
  const form = useGenerateSyntheticForm({ active: true, initialDestination: destination });
  return (
    <div>
      {form.fields}
      <button type="button" onClick={() => form.submit()}>
        go
      </button>
      <button type="button" onClick={() => form.reset()}>
        reset
      </button>
      <output data-testid="destination">{form.summary.find((row) => row.label === 'Destination')?.value}</output>
    </div>
  );
};

const destinationRadio = (value: 'new' | 'existing') => screen.getByDisplayValue(value) as HTMLInputElement;

describe('useGenerateSyntheticForm destination', () => {
  beforeEach(() => {
    generateFromPrompt.mockReset().mockResolvedValue('job-1');
    generateFromConfig.mockReset();
  });

  it('starts on "Add to existing test set" with the given set, and submits its id', async () => {
    render(<Harness destination={{ testSetId: 'ts1', label: 'TS One' }} />);
    expect(destinationRadio('existing').checked).toBe(true);
    expect(screen.getByTestId('destination').textContent).toBe('Add to "TS One"');

    fireEvent.click(screen.getByText('go'));
    await waitFor(() => expect(generateFromPrompt).toHaveBeenCalledTimes(1));
    const args = generateFromPrompt.mock.calls[0][0];
    expect(args.testSetId).toBe('ts1');
    expect(args.testSetName).toBeUndefined();
  });

  it('returns to that destination on reset rather than to a new set', async () => {
    render(<Harness destination={{ testSetId: 'ts1', label: 'TS One' }} />);
    fireEvent.click(destinationRadio('new'));
    expect(destinationRadio('new').checked).toBe(true);
    fireEvent.click(screen.getByText('reset'));
    await waitFor(() => expect(destinationRadio('existing').checked).toBe(true));
  });

  it('defaults to a new set when no destination is given', async () => {
    render(<Harness />);
    expect(destinationRadio('new').checked).toBe(true);
    fireEvent.click(screen.getByText('go'));
    await waitFor(() => expect(generateFromPrompt).toHaveBeenCalledTimes(1));
    const args = generateFromPrompt.mock.calls[0][0];
    expect(args.testSetId).toBeUndefined();
    expect(args).toHaveProperty('testSetName');
  });
});
