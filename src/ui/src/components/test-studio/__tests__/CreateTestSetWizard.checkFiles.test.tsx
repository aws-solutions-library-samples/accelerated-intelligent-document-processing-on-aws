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
import { beforeEach, describe, expect, it, vi } from 'vitest';

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
const deferredListing = (): { resolve: (files: string[]) => void } => {
  let release: (files: string[]) => void = () => undefined;
  graphql.mockImplementation(({ query }: { query: string }) => {
    if (query !== 'listBucketFiles') return Promise.resolve({ data: {} });
    return new Promise((resolve) => {
      release = (files) => resolve({ data: { listBucketFiles: files } });
    });
  });
  return { resolve: (files) => release(files) };
};

const openPatternStep = (): void => {
  render(
    <IntlProvider locale="en" defaultLocale="en">
      <CreateTestSetWizard visible onDismiss={vi.fn()} onCreated={vi.fn()} generatorAvailable={false} onGenerationStarted={vi.fn()} />
    </IntlProvider>,
  );
  const wrapper = createWrapper(document.body);
  act(() => wrapper.findTiles()!.findItemByValue('existing-files')!.findNativeInput().click());
  act(() => wrapper.findWizard()!.findPrimaryButton().click());
  fireEvent.change(screen.getByPlaceholderText('*.pdf'), { target: { value: 'inv-*.pdf' } });
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
});
