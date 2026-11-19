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
import { resolveModifiedAfter } from '../modifiedAfter';

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

const English = ({ children }: { children: React.ReactNode }): React.JSX.Element => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const answerEveryCall = (): void => {
  graphql.mockImplementation(async ({ query }: { query: string }) => {
    if (query === 'listBucketFiles') return { data: { listBucketFiles: ['inv-1.pdf', 'inv-2.pdf'] } };
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

  it('passes a custom date that does not parse through for the server to refuse', () => {
    expect(resolveModifiedAfter('custom', 'not-a-date', '09:30:00')).toBe('not-a-dateT09:30:00Z');
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
});

describe('The create-test-set wizard', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    graphql.mockReset();
    answerEveryCall();
  });

  it('reads a custom date and time as UTC, as the add-documents dialog does, and says so', async () => {
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
    fireEvent.change(screen.getByPlaceholderText('YYYY-MM-DD'), { target: { value: '2026-11-18' } });
    fireEvent.change(screen.getByPlaceholderText('00:00:00'), { target: { value: '09:30:00' } });
    expect(screen.getByText('Date and time are in UTC.')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Check matching files' }));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());

    expect(cutoffsSent('listBucketFiles')).toEqual(['2026-11-18T09:30:00.000Z']);
  });
});
