// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Selecting specific documents across a paginated set.
 *
 * Two properties this dialog has to hold, both of which are silent when broken:
 * a selection made on one page must survive turning the page, and the dialog must
 * page for itself rather than inheriting the caller's page — otherwise document
 * 75 of 100 can only be chosen if the list behind the dialog happens to be
 * showing it.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({ getConfigVersions: 'getConfigVersions', getTestSetDocuments: 'getTestSetDocuments' }));
vi.mock('../TestSetDetail', () => ({
  renderConfidence: () => null,
  renderLabelSource: (s: string | null) => s ?? '',
}));

// Imported after the mocks, which vitest hoists.
import GenerateDraftLabelsModal from '../GenerateDraftLabelsModal';

const doc = (name: string) => ({ objectKey: name, inputKey: name, labelSource: null, minConfidence: null, confidenceThreshold: null });

/**
 * Three rows per page, not fifty.
 *
 * Paging is driven by the mock's `nextToken`, not by a page being full, and the
 * bug these tests guard — Cloudscape reporting only the current page's ticks — is
 * independent of row count. Fifty-row fixtures rendered a Cloudscape table twice
 * per test and cost ~1s against a 5s timeout, which passed locally and timed out
 * on CI's slower runner. The real page size is asserted directly instead, below.
 */
const PAGE_1 = Array.from({ length: 3 }, (_, i) => doc(`doc${i + 1}.pdf`));
const PAGE_2 = Array.from({ length: 3 }, (_, i) => doc(`doc${i + 51}.pdf`));

const mockPages = () => {
  graphql.mockImplementation((args: { query: string; variables?: { nextToken?: string } }) => {
    if (args.query === 'getConfigVersions') {
      return Promise.resolve({ data: { getConfigVersions: { versions: [] } } });
    }
    const onPage2 = args.variables?.nextToken === 'token-1';
    return Promise.resolve({
      data: {
        getTestSetDocuments: {
          documents: onPage2 ? PAGE_2 : PAGE_1,
          nextToken: onPage2 ? null : 'token-1',
          totalCount: 100,
        },
      },
    });
  });
};

const renderModal = (onSubmit = vi.fn()) => {
  render(<GenerateDraftLabelsModal visible testSetId="ts1" setTotalCount={100} onDismiss={vi.fn()} onSubmit={onSubmit} />);
  return onSubmit;
};

describe('GenerateDraftLabelsModal', () => {
  beforeEach(() => {
    graphql.mockReset();
    mockPages();
  });

  it('reports the set size, not the page size', async () => {
    renderModal();
    // The bug this replaced: a 100-document set described as 50.
    await waitFor(() => expect(screen.getByText(/ts1 · 100 document\(s\)/)).toBeInTheDocument());
  });

  it('fetches its own first page rather than inheriting the callers', async () => {
    renderModal();
    await waitFor(() => expect(graphql).toHaveBeenCalled());
    const docCalls = graphql.mock.calls.filter((c) => c[0].query === 'getTestSetDocuments');
    expect(docCalls.length).toBeGreaterThan(0);
    // No continuation token: always page 1, whatever page the list behind it is on.
    expect(docCalls[0][0].variables.nextToken).toBeUndefined();
    // The real MODAL_PAGE_SIZE, asserted here because the fixtures above are
    // deliberately smaller than a page.
    expect(docCalls[0][0].variables.limit).toBe(50);
  });

  it('keeps a selection made on page 1 after paging to page 2', async () => {
    // The property that makes cross-page selection real. A Cloudscape Table only
    // knows the items it is given, so a selection held in the page would appear
    // to be cleared the moment the user turned it.
    const onSubmit = renderModal();
    await waitFor(() => expect(screen.getByText(/ts1 · 100/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole('checkbox', { name: /Extract labels for every document/i }));
    await waitFor(() => expect(screen.getByText('doc1.pdf')).toBeInTheDocument());

    // Tick one document on page 1.
    // Hoisted out of the filter: calling getByRole inside the callback re-scans the
    // whole accessibility tree once per checkbox.
    const selectAll = screen.getByRole('checkbox', { name: /Extract labels/i });
    const rowCheckboxes = screen.getAllByRole('checkbox').filter((c) => c !== selectAll);
    fireEvent.click(rowCheckboxes[1]);
    await waitFor(() => expect(screen.getByText(/1 document\(s\) selected/)).toBeInTheDocument());

    // Turn the page. Cloudscape renders numbered page buttons.
    fireEvent.click(screen.getByRole('button', { name: '2' }));
    await waitFor(() => expect(screen.getByText('doc51.pdf')).toBeInTheDocument());

    // Still counted after the page turn.
    expect(screen.getByText(/1 document\(s\) selected/)).toBeInTheDocument();

    // And now tick one on page 2. This is the step that actually exercises the
    // merge: the change event reports only THIS page's ticks, so a handler that
    // assigned them would wipe doc1 and nothing on screen would say so.
    const page2Checkboxes = screen.getAllByRole('checkbox').filter((c) => c !== selectAll);
    fireEvent.click(page2Checkboxes[1]);
    await waitFor(() => expect(screen.getByText(/2 document\(s\) selected/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Label 2 document/i }));
    // Third arg is the pinned Configuration Profile revision, which develop added.
    // Asserted as undefined rather than ignored: the default has to stay "the profile's
    // current configuration", not some revision the reviewer never chose.
    expect(onSubmit).toHaveBeenCalledWith(undefined, expect.arrayContaining(['doc1.pdf', 'doc51.pdf']), undefined);
    expect(onSubmit.mock.calls[0][1]).toHaveLength(2);
  });

  it('sends no object keys in select-all mode, so the server decides the scope', async () => {
    // Select-all must NOT enumerate the page: the server walks the whole set, and
    // sending 50 keys for a 100-document set would silently halve the job.
    const onSubmit = renderModal();
    await waitFor(() => expect(screen.getByText(/ts1 · 100/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /Label every document that needs it/i }));
    expect(onSubmit).toHaveBeenCalledWith(undefined, undefined, undefined);
  });
});
