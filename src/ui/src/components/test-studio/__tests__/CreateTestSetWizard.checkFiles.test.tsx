// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * "Check matching files" in the Create test set wizard's "From files in S3" source.
 *
 * The count it shows is also what the wizard submits as `fileCount` and names in
 * its "created from N matching file(s)" message, so it must describe the bucket,
 * pattern and time filter currently in the form: it clears when any of them
 * changes, and a reply that lands after such a change is discarded rather than
 * painted over the edited form.
 */

import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { IntlProvider } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
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
import CreateTestSetWizard from '../CreateTestSetWizard';

/** A listBucketFiles reply the test releases by hand, so it can land after an edit. */
const deferredListing = (): { resolve: (files: string[]) => void; fail: (message: string) => void } => {
  let settle = { resolve: (_files: string[]): void => undefined, fail: (_message: string): void => undefined };
  graphql.mockImplementation(({ query }: { query: string }) => {
    if (query !== 'listBucketFiles') return Promise.resolve({ data: {} });
    return new Promise((resolve, reject) => {
      settle = {
        resolve: (files) => resolve({ data: { listBucketFiles: files } }),
        fail: (message) => reject(new Error(message)),
      };
    });
  });
  return { resolve: (files) => settle.resolve(files), fail: (message) => settle.fail(message) };
};

const renderWizard = (): void => {
  render(
    <IntlProvider locale="en" defaultLocale="en">
      <CreateTestSetWizard visible onDismiss={vi.fn()} onCreated={vi.fn()} generatorAvailable={false} onGenerationStarted={vi.fn()} />
    </IntlProvider>,
  );
};

/** From the source step: choose "From files in S3", go on, and enter a pattern. */
const goToPatternStep = (): void => {
  const wrapper = createWrapper(document.body);
  act(() => wrapper.findTiles()!.findItemByValue('existing-files')!.findNativeInput().click());
  act(() => wrapper.findWizard()!.findPrimaryButton().click());
  fireEvent.change(screen.getByPlaceholderText('*.pdf'), { target: { value: 'inv-*.pdf' } });
};

const openPatternStep = (): void => {
  renderWizard();
  goToPatternStep();
};

const check = (): void => {
  fireEvent.click(screen.getByRole('button', { name: 'Check matching files' }));
};

describe('Check matching files in the create wizard', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    graphql.mockReset();
  });

  it('shows the count for the pattern that was checked', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());
  });

  it('clears the count when the pattern changes', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText('*.pdf'), { target: { value: 'inv-2*.pdf' } });

    expect(screen.queryByText('2 files to add')).toBeNull();
  });

  it('clears the count when the bucket changes', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());

    // The classification-type select comes first on this step; the bucket is next.
    const bucket = createWrapper(document.body).findWizard()!.findContent()!.findAllSelects()[1];
    act(() => bucket.openDropdown());
    act(() => bucket.selectOptionByValue('testset'));

    expect(screen.queryByText('2 files to add')).toBeNull();
  });

  it('clears the count when the time filter changes', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());

    const timeFilter = createWrapper(document.body).findWizard()!.findContent()!.findAllSelects()[2];
    act(() => timeFilter.openDropdown());
    act(() => timeFilter.selectOptionByValue('24'));

    expect(screen.queryByText('2 files to add')).toBeNull();
  });

  it.each([
    ['date', '2026/11/01'],
    ['time', '12:00:00'],
  ])('clears the count when the custom %s changes', async (field, value) => {
    const listing = deferredListing();
    openPatternStep();
    const timeFilter = createWrapper(document.body).findWizard()!.findContent()!.findAllSelects()[2];
    act(() => timeFilter.openDropdown());
    act(() => timeFilter.selectOptionByValue('custom'));
    check();
    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());

    const content = createWrapper(document.body).findWizard()!.findContent()!;
    const input = field === 'date' ? content.findDatePicker()! : content.findTimeInput()!;
    act(() => input.setInputValue(value));

    expect(screen.queryByText('2 files to add')).toBeNull();
    expect(input.findNativeInput().getElement().value).toBe(value);
  });

  it('clears a "no files matched" result when the pattern changes', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    await act(async () => listing.resolve([]));
    await waitFor(() => expect(screen.getByText('No files matched that pattern')).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText('*.pdf'), { target: { value: 'inv-2*.pdf' } });

    expect(screen.queryByText('No files matched that pattern')).toBeNull();
  });

  it('discards a reply that lands after the pattern was edited', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    fireEvent.change(screen.getByPlaceholderText('*.pdf'), { target: { value: 'inv-2*.pdf' } });

    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));

    expect(screen.queryByText('2 files to add')).toBeNull();
    expect(screen.queryByText(/No files matched/)).toBeNull();
  });

  it('discards a stale "no files matched" too', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    fireEvent.change(screen.getByPlaceholderText('*.pdf'), { target: { value: 'inv-2*.pdf' } });

    await act(async () => listing.resolve([]));

    expect(screen.queryByText(/No files matched/)).toBeNull();
  });

  it('discards a failure that lands after the pattern was edited', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    fireEvent.change(screen.getByPlaceholderText('*.pdf'), { target: { value: 'inv-2*.pdf' } });

    await act(async () => listing.fail('throttled'));

    expect(screen.queryByText(/Could not check files/)).toBeNull();
  });

  it('discards a reply that lands after the wizard was cancelled', async () => {
    const listing = deferredListing();
    openPatternStep();
    check();
    act(() => createWrapper(document.body).findWizard()!.findCancelButton().click());

    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));
    // Back to the pattern step without typing, which would clear the count itself.
    const wrapper = createWrapper(document.body);
    act(() => wrapper.findTiles()!.findItemByValue('existing-files')!.findNativeInput().click());
    act(() => wrapper.findWizard()!.findPrimaryButton().click());

    expect(screen.getByPlaceholderText('*.pdf')).toBeTruthy();
    expect(screen.queryByText('2 files to add')).toBeNull();
  });
});

/** The `modifiedAfter` each call of `query` was sent with, in call order. */
const cutoffsSent = (query: string): (string | undefined)[] =>
  graphql.mock.calls
    .map(([arg]) => arg as { query: string; variables?: { modifiedAfter?: string } })
    .filter((arg) => arg.query === query)
    .map((arg) => arg.variables?.modifiedAfter);

const chooseTimeFilter = (value: string): void => {
  const timeFilter = createWrapper(document.body).findWizard()!.findContent()!.findAllSelects()[2];
  act(() => timeFilter.openDropdown());
  act(() => timeFilter.selectOptionByValue(value));
};

/** Name the set, go on to Review, and press Create. */
const submit = async (): Promise<void> => {
  fireEvent.change(screen.getByPlaceholderText('my-invoices-benchmark'), { target: { value: 'recent-invoices' } });
  const wizard = createWrapper(document.body).findWizard()!;
  act(() => wizard.findPrimaryButton().click());
  await act(async () => wizard.findPrimaryButton().click());
  await waitFor(() => expect(cutoffsSent('addTestSet')).toHaveLength(1));
};

describe('The "Modified after" cutoff the wizard submits', () => {
  const checkedAt = Date.parse('2026-11-18T12:00:00.000Z');
  const submittedAt = checkedAt + 40 * 60 * 1000;
  const hour = 3600 * 1000;

  beforeEach(() => {
    document.body.innerHTML = '';
    graphql.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('is the cutoff the check counted against, not one recomputed at submit', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(checkedAt);
    const listing = deferredListing();
    openPatternStep();
    chooseTimeFilter('24');
    check();
    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());

    now.mockReturnValue(submittedAt);
    await submit();

    const counted = new Date(checkedAt - 24 * hour).toISOString();
    expect(cutoffsSent('listBucketFiles')).toEqual([counted]);
    expect(cutoffsSent('addTestSet')).toEqual([counted]);
  });

  it('is resolved at submit when nothing was checked', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(submittedAt);
    deferredListing();
    openPatternStep();
    chooseTimeFilter('24');

    await submit();

    expect(cutoffsSent('addTestSet')).toEqual([new Date(submittedAt - 24 * hour).toISOString()]);
  });

  it('ignores the cutoff of a check whose reply landed after the time filter changed', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(checkedAt);
    const listing = deferredListing();
    openPatternStep();
    chooseTimeFilter('24');
    check();
    chooseTimeFilter('168');
    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));

    await submit();

    expect(cutoffsSent('addTestSet')).toEqual([new Date(checkedAt - 168 * hour).toISOString()]);
  });

  it('follows a time filter changed after the check', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(checkedAt);
    const listing = deferredListing();
    openPatternStep();
    chooseTimeFilter('24');
    check();
    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());

    chooseTimeFilter('168');
    now.mockReturnValue(submittedAt);
    await submit();

    expect(cutoffsSent('addTestSet')).toEqual([new Date(submittedAt - 168 * hour).toISOString()]);
  });
});
