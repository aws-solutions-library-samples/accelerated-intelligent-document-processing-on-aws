// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The step details' Started and Completed cells follow the UI locale.
 *
 * Under `en` (and with no `LocaleProvider` at all, as the existing English tests render)
 * the cell is what the bare `toLocaleString()` has always shown: the runtime default. Under
 * `fr-CA` it is what the `formatDateTime` helper gives for that locale; the helper is the
 * oracle, because the exact French shape belongs to ICU and changes between releases. The
 * third check, that the two differ, is what stops the test passing when the component
 * ignores the locale.
 */

import { render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it } from 'vitest';

import { formatDateTime } from '../../../i18n/format';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import StepDetails from '../StepDetails';

const STARTED = '2026-10-30T17:40:05Z';
const COMPLETED = '2026-10-30T17:41:35Z';

const PROPS = {
  step: { name: 'OCRStep', type: 'Task', status: 'SUCCEEDED', startDate: STARTED, stopDate: COMPLETED },
  formatDuration: () => '1m 30s',
  getStepIcon: () => null,
};

/** The value cell under a key label: the label's next sibling. */
const cellAfter = (label: string): string => screen.getByText(label).nextElementSibling?.textContent ?? '';

describe('StepDetails dates', () => {
  it('shows the runtime default under en, as the bare toLocaleString() did', () => {
    render(<StepDetails {...PROPS} />);
    expect(cellAfter('Started')).toBe(new Date(STARTED).toLocaleString());
    expect(cellAfter('Completed')).toBe(new Date(COMPLETED).toLocaleString());
  });

  it('shows the French shape under fr-CA', () => {
    renderWithIntl(<StepDetails {...PROPS} />, { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'] });
    const started = cellAfter('Started');
    expect(started).toBe(formatDateTime(new Date(STARTED), 'fr-CA'));
    expect(cellAfter('Completed')).toBe(formatDateTime(new Date(COMPLETED), 'fr-CA'));
    // Not vacuous: the French text is not the English text.
    expect(started).not.toBe(new Date(STARTED).toLocaleString());
  });
});
