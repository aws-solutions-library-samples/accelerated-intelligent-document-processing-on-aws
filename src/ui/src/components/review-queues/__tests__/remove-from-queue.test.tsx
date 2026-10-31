// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Remove from queue, the Admin action that takes production items out of review.
 * What has to hold: the row menu offers it for a waiting or assigned item; the
 * confirmation explains that the document completes with its machine results; each
 * selected item gets its own removeReviewItem call with the optional note; failures
 * are reported by document like the other bulk actions; and the list is reloaded.
 * The queue page offers it to Admins only, as a row and a bulk action.
 */

import React, { useEffect } from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../graphql/generated')>()),
  removeReviewItem: 'removeReviewItem',
}));
const setSuccessMessage = vi.fn();
const setErrorMessage = vi.fn();
vi.mock('../../../contexts/app', () => ({ default: () => ({ setSuccessMessage, setErrorMessage }) }));

import type { ButtonDropdownProps } from '@cloudscape-design/components';
import type { ReviewItem } from '../../../graphql/generated/operation-types';
import useSupervisorActions from '../use-supervisor-actions';

const item = (objectKey: string, state: string): ReviewItem =>
  ({ objectKey, cycleId: `c-${objectKey}`, state, queue: 'default', profile: 'p' }) as unknown as ReviewItem;

let api: ReturnType<typeof useSupervisorActions> | null = null;
const onChanged = vi.fn();

const Harness = () => {
  const supervisor = useSupervisorActions({ queues: [], onChanged });
  useEffect(() => {
    api = supervisor;
  });
  return supervisor.modals;
};

const removeDialog = () => {
  const m = createWrapper(document.body)
    .findAllModals()
    .find((w) => w.findHeader().getElement().textContent === 'Remove from queue');
  if (!m) throw new Error('no remove dialog');
  return m;
};

const adminItems = (i: ReviewItem): ButtonDropdownProps.Item[] =>
  ((api!.menuItems(i)[0] as ButtonDropdownProps.ItemGroup).items ?? []) as ButtonDropdownProps.Item[];

beforeEach(() => {
  document.body.innerHTML = '';
  api = null;
  graphql.mockReset();
  onChanged.mockReset();
  setSuccessMessage.mockReset();
  setErrorMessage.mockReset();
});

describe('Remove from queue', () => {
  it('is offered for a waiting or assigned item, and not for one being completed', () => {
    render(<Harness />);
    for (const state of ['OPEN', 'CLAIMED']) {
      const remove = adminItems(item('a.pdf', state)).find((e) => e.id === 'remove');
      expect(remove?.text).toBe('Remove from queue…');
      expect(remove?.disabled).toBe(false);
    }
    expect(adminItems(item('a.pdf', 'FINALIZING')).find((e) => e.id === 'remove')?.disabled).toBe(true);
  });

  it('confirms, calls removeReviewItem once per item with the note, reports failures and reloads', async () => {
    graphql.mockImplementation(async ({ query, variables }: { query: string; variables: { objectKey: string } }) => {
      if (query !== 'removeReviewItem') throw new Error(`unexpected ${query}`);
      if (variables.objectKey === 'bad.pdf') throw { errors: [{ message: 'INVALID_STATE: the item is FINALIZING' }] };
      return { data: { removeReviewItem: { objectKey: variables.objectKey } } };
    });
    render(<Harness />);
    await waitFor(() => expect(api).not.toBeNull());
    const selected = [item('a.pdf', 'OPEN'), item('b.pdf', 'CLAIMED'), item('bad.pdf', 'OPEN'), item('done.pdf', 'FINALIZING')];
    React.act(() => api!.open('remove', selected));

    const dialog = removeDialog();
    expect(dialog.findContent().getElement().textContent).toContain('The document completes with its machine results and leaves review.');
    expect(dialog.findContent().getElement().textContent).toContain('Remove 3 items from review.');
    dialog.findContent().findTextarea()!.setTextareaValue('  duplicate upload  ');
    fireEvent.click(dialog.findFooter()!.findAllButtons().at(-1)!.getElement());

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const calls = graphql.mock.calls.map(([arg]) => arg as { query: string; variables: Record<string, unknown> });
    expect(calls.map((c) => c.variables)).toEqual([
      { objectKey: 'a.pdf', cycleId: 'c-a.pdf', note: 'duplicate upload' },
      { objectKey: 'b.pdf', cycleId: 'c-b.pdf', note: 'duplicate upload' },
      { objectKey: 'bad.pdf', cycleId: 'c-bad.pdf', note: 'duplicate upload' },
    ]);
    expect(setSuccessMessage).toHaveBeenCalledWith('Removed 2 items from review.');
    expect(setErrorMessage).toHaveBeenCalledWith(expect.stringMatching(/^Couldn't remove 1 item: bad\.pdf/));
  });

  it('sends no note when none is written', async () => {
    graphql.mockResolvedValue({ data: { removeReviewItem: { objectKey: 'a.pdf' } } });
    render(<Harness />);
    await waitFor(() => expect(api).not.toBeNull());
    React.act(() => api!.open('remove', [item('a.pdf', 'OPEN')]));
    fireEvent.click(removeDialog().findFooter()!.findAllButtons().at(-1)!.getElement());
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(graphql.mock.calls[0][0].variables).toEqual({ objectKey: 'a.pdf', cycleId: 'c-a.pdf', note: undefined });
  });
});

describe('the queue page', () => {
  const source = readFileSync(resolve(__dirname, '../ReviewQueuePage.tsx'), 'utf8');

  it('offers Remove from queue as a bulk action in the Admin toolbar', () => {
    const start = source.indexOf('actions={\n            isAdmin ? (');
    expect(start).toBeGreaterThan(-1);
    const adminBranch = source.slice(start, source.indexOf(') : (', start));
    expect(adminBranch).toContain("supervisor.open('remove', removable)");
    expect(adminBranch).toContain("counted('Remove from queue…', removable.length)");
  });

  it('gives the row menu the Admin actions only to Admins', () => {
    expect(source).toContain('isAdmin ? supervisor.menuItems(item.reviewItem) : claimMenuItems(item)');
    expect(source).toContain('if (isAdmin && isSupervisorAction(action))');
  });
});
