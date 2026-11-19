// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The "Modified after" cutoff of the two file-pattern flows. What has to hold:
 * the add-documents dialog sends the cutoff its Check Files counted against, not
 * one resolved again when Add Documents is pressed; and a custom date and time is
 * read as UTC (which the dialog has always said beside the time) in both the
 * dialog and the create wizard, so the same input gives the same cutoff.
 */

import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { IntlProvider } from 'react-intl';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  addDocumentsToTestSet: 'addDocumentsToTestSet',
  addDocumentsToTestSetFromUpload: 'addDocumentsToTestSetFromUpload',
  addTestSet: 'addTestSet',
  addTestSetFromUpload: 'addTestSetFromUpload',
  createEmptyTestSet: 'createEmptyTestSet',
  listBucketFiles: 'listBucketFiles',
  validateTestFileName: 'validateTestFileName',
}));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => ({ isAdmin: true }) }));
vi.mock('../../../hooks/use-quick-start-available', () => ({ default: () => false }));
vi.mock('../useGenerateSyntheticForm', () => ({
  default: () => ({ fields: null, summary: [], error: '', canSubmit: false, submitting: false, submit: async () => null }),
}));

// Imported after the mocks, which vitest hoists.
import AddDocumentsModals from '../AddDocumentsModals';
import CreateTestSetWizard from '../CreateTestSetWizard';
import { customCutoff, customCutoffIsInvalid, resolveModifiedAfter } from '../modifiedAfter';

const hour = 3600 * 1000;
const checkedAt = Date.parse('2026-11-18T12:00:00.000Z');
const submittedAt = checkedAt + 40 * 60 * 1000;

// A zone well away from UTC, so reading the custom time as local time would move
// the cutoff and the assertions below could tell.
let savedTz: string | undefined;
beforeAll(() => {
  savedTz = process.env.TZ;
  process.env.TZ = 'America/New_York';
  expect(new Date(checkedAt).getTimezoneOffset()).not.toBe(0);
  return () => {
    if (savedTz === undefined) delete process.env.TZ;
    else process.env.TZ = savedTz;
  };
});

/** The `modifiedAfter` each call of `query` was sent with, in call order. */
const cutoffsSent = (query: string, bucketType?: string): (string | undefined)[] =>
  graphql.mock.calls
    .map(([arg]) => arg as { query: string; variables?: { modifiedAfter?: string; bucketType?: string } })
    .filter((arg) => arg.query === query && (bucketType === undefined || arg.variables?.bucketType === bucketType))
    .map((arg) => arg.variables?.modifiedAfter);

const invalidText = 'Enter a real date as YYYY-MM-DD and a time as HH:mm:ss.';

/** Text of the error alerts shown, which is where a refused action says why. */
const errorAlerts = (): string[] =>
  createWrapper(document.body)
    .findAllAlerts()
    .filter((a) => a.getElement().textContent?.includes(invalidText))
    .map((a) => a.getElement().textContent ?? '');

const English = ({ children }: { children: React.ReactNode }): React.JSX.Element => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

/** Answer every operation; `duringListing` runs while a listing is in flight (e.g. to move the clock). */
const answerEveryCall = (duringListing: () => void = () => undefined): void => {
  graphql.mockImplementation(async ({ query }: { query: string }) => {
    if (query === 'listBucketFiles') {
      duringListing();
      return { data: { listBucketFiles: ['inv-1.pdf', 'inv-2.pdf'] } };
    }
    if (query === 'addDocumentsToTestSet') return { data: { addDocumentsToTestSet: { id: 'invoices', name: 'Invoices' } } };
    if (query === 'addTestSet') return { data: { addTestSet: { id: 'recent', name: 'recent' } } };
    return { data: {} };
  });
};

describe('resolveModifiedAfter', () => {
  it('reads a custom date and time as UTC', () => {
    expect(resolveModifiedAfter('custom', '2026-11-18', '09:30:00')).toBe('2026-11-18T09:30:00.000Z');
  });

  it('takes midnight when the custom time is empty', () => {
    expect(resolveModifiedAfter('custom', '2026-11-18', '')).toBe('2026-11-18T00:00:00.000Z');
  });

  it('has no cutoff for no filter, or a custom filter with no date', () => {
    expect(resolveModifiedAfter('', '2026-11-18', '09:30:00')).toBeUndefined();
    expect(resolveModifiedAfter(undefined, '', '')).toBeUndefined();
    expect(resolveModifiedAfter('custom', '', '09:30:00')).toBeUndefined();
  });

  it('refuses a custom date and time that are not real, rather than sending or rolling them over', () => {
    for (const [date, time] of [
      ['not-a-date', '09:30:00'],
      ['2026/11/18', '09:30:00'],
      ['2026-02-30', '00:00:00'],
      ['2026-13-01', '00:00:00'],
      ['2026-11-18', '24:00:00'],
      ['2026-11-18', '09:60:00'],
      ['2026-11-18', '09:30:60'],
      ['2026-11-18', '9:30'],
    ]) {
      expect(customCutoff(date, time), `${date} ${time}`).toBeNull();
      expect(customCutoffIsInvalid('custom', date, time), `${date} ${time}`).toBe(true);
      expect(() => resolveModifiedAfter('custom', date, time), `${date} ${time}`).toThrow();
    }
  });

  it('accepts the last moment of a day, a leap day and a time without seconds', () => {
    expect(customCutoff('2026-11-18', '23:59:59')).toBe('2026-11-18T23:59:59.000Z');
    expect(customCutoff('2028-02-29', '00:00:00')).toBe('2028-02-29T00:00:00.000Z');
    expect(customCutoff('2026-11-18', '09:30')).toBe('2026-11-18T09:30:00.000Z');
  });

  it('only calls a custom filter with a date entered invalid', () => {
    expect(customCutoffIsInvalid('24', 'not-a-date', '')).toBe(false);
    expect(customCutoffIsInvalid('custom', '', 'garbage')).toBe(false);
    expect(customCutoffIsInvalid('custom', '2026-11-18', '09:30:00')).toBe(false);
  });

  it('resolves a relative window against the time given', () => {
    expect(resolveModifiedAfter('24', '', '', checkedAt)).toBe(new Date(checkedAt - 24 * hour).toISOString());
  });
});

describe('The add-documents dialog', () => {
  const target = { id: 'invoices', name: 'Invoices', filePattern: 'inv-*.pdf' };

  beforeEach(() => {
    document.body.innerHTML = '';
    graphql.mockReset();
    answerEveryCall();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** The pattern dialog's body; the Matching Files list opens as a second modal. */
  const dialog = () =>
    createWrapper(document.body)
      .findAllModals()
      .find((m) => m.findHeader().getElement().textContent === 'Add Documents to "Invoices"')!
      .findContent();
  // The bucket is the first select, the time filter the second.
  const timeFilter = () => dialog().findAllSelects()[1];
  const chooseTimeFilter = (value: string): void => {
    act(() => timeFilter().openDropdown());
    act(() => timeFilter().selectOptionByValue(value));
  };
  const checkFiles = async (): Promise<void> => {
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());
  };
  const addDocuments = async (): Promise<void> => {
    fireEvent.click(screen.getByRole('button', { name: 'Add Documents' }));
    await waitFor(() => expect(cutoffsSent('addDocumentsToTestSet')).toHaveLength(1));
  };
  const renderDialog = (): void => {
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
  };

  it('sends the cutoff the check counted against, not one recomputed at submit', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(checkedAt);
    // The clock moves while the listing is answered, so a cutoff worked out again
    // when the reply lands (rather than the one sent) would differ too.
    answerEveryCall(() => now.mockReturnValue(checkedAt + 7 * 1000));
    renderDialog();
    chooseTimeFilter('24');
    await checkFiles();

    now.mockReturnValue(submittedAt);
    await addDocuments();

    const counted = new Date(checkedAt - 24 * hour).toISOString();
    expect(cutoffsSent('listBucketFiles', 'input')).toEqual([counted]);
    expect(cutoffsSent('addDocumentsToTestSet')).toEqual([counted]);
  });

  it('sends the cutoff of the latest check after the time filter changed', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(checkedAt);
    renderDialog();
    chooseTimeFilter('24');
    await checkFiles();

    chooseTimeFilter('168');
    expect(screen.queryByText('2 files to add')).toBeNull();
    const recheckedAt = checkedAt + 5 * 60 * 1000;
    now.mockReturnValue(recheckedAt);
    await checkFiles();
    now.mockReturnValue(submittedAt);
    await addDocuments();

    expect(cutoffsSent('addDocumentsToTestSet')).toEqual([new Date(recheckedAt - 168 * hour).toISOString()]);
  });

  it('reads a custom date and time as UTC, and says so', async () => {
    renderDialog();
    chooseTimeFilter('custom');
    const content = dialog();
    act(() => content.findDatePicker()!.setInputValue('2026/11/18'));
    act(() => content.findTimeInput()!.setInputValue('09:30:00'));
    expect(screen.getByText('Date and time are in UTC.')).toBeTruthy();
    await checkFiles();
    await addDocuments();

    expect(cutoffsSent('listBucketFiles', 'input')).toEqual(['2026-11-18T09:30:00.000Z']);
    expect(cutoffsSent('addDocumentsToTestSet')).toEqual(['2026-11-18T09:30:00.000Z']);
  });

  it('will not check a custom time typed only in part, and says so beside the field', async () => {
    renderDialog();
    chooseTimeFilter('custom');
    const content = dialog();
    act(() => content.findDatePicker()!.setInputValue('2026/11/18'));
    act(() => content.findTimeInput()!.setInputValue('09:3'));
    expect(screen.getByText('Enter a real date as YYYY-MM-DD and a time as HH:mm:ss.')).toBeTruthy();
    expect(errorAlerts()).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    expect(errorAlerts()).toHaveLength(1);
    expect(cutoffsSent('listBucketFiles')).toEqual([]);
    expect(screen.getByRole('button', { name: 'Add Documents' })).toHaveProperty('disabled', true);
  });
});

describe('The create-test-set wizard', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    graphql.mockReset();
    answerEveryCall();
  });

  /** Open the pattern step and enter a custom date and time. */
  const enterCustomDateTime = (date: string, time: string): void => {
    render(
      <English>
        <CreateTestSetWizard visible onDismiss={vi.fn()} onCreated={vi.fn()} generatorAvailable={false} onGenerationStarted={vi.fn()} />
      </English>,
    );
    const wrapper = createWrapper(document.body);
    act(() => wrapper.findTiles()!.findItemByValue('existing-files')!.findNativeInput().click());
    act(() => wrapper.findWizard()!.findPrimaryButton().click());
    fireEvent.change(screen.getByPlaceholderText('*.pdf'), { target: { value: 'inv-*.pdf' } });
    const timeFilter = wrapper.findWizard()!.findContent()!.findAllSelects()[2];
    act(() => timeFilter.openDropdown());
    act(() => timeFilter.selectOptionByValue('custom'));
    fireEvent.change(screen.getByPlaceholderText('YYYY-MM-DD'), { target: { value: date } });
    fireEvent.change(screen.getByPlaceholderText('00:00:00'), { target: { value: time } });
  };

  it('will not go on to Create with a custom date that is not real, and says so beside the field', async () => {
    enterCustomDateTime('2026/11/18', '00:00:00');
    expect(screen.getAllByText(invalidText).length).toBeGreaterThan(0);
    expect(errorAlerts()).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Check matching files' }));
    expect(errorAlerts()).toHaveLength(1);
    expect(cutoffsSent('listBucketFiles')).toEqual([]);

    fireEvent.change(screen.getByPlaceholderText('my-invoices-benchmark'), { target: { value: 'recent-invoices' } });
    const wizard = createWrapper(document.body).findWizard()!;
    act(() => wizard.findPrimaryButton().click());
    // Still on the pattern step: no Review, and nothing was created.
    expect(screen.getByPlaceholderText('YYYY-MM-DD')).toBeTruthy();
    expect(errorAlerts()).toHaveLength(1);
    expect(cutoffsSent('addTestSet')).toEqual([]);
  });

  it('reads a custom date and time as UTC, as the add-documents dialog does, and says so', async () => {
    enterCustomDateTime('2026-11-18', '09:30:00');
    expect(screen.getByText('Date and time are in UTC.')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Check matching files' }));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());

    expect(cutoffsSent('listBucketFiles')).toEqual(['2026-11-18T09:30:00.000Z']);
  });
});
