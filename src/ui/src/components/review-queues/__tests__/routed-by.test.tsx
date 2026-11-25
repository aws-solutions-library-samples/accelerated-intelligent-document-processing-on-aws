// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * How a routing decision reads to a reviewer (with how a long text is shortened and how
 * the failed policy rules are counted), and the two calls that end a review other than
 * by completing it: reject, and send back (which is refused, not silently dropped, when
 * the server queues no run).
 */

import { createIntl } from 'react-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }),
}));

import { rejectReviewItem, sendBackReviewItem } from '../../../graphql/generated';
import * as routedByModule from '../RoutedBy';
import { policyFailureTotal, routedByFallbackText, routedByText, shortened } from '../RoutedBy';
import { SendBackRefused, rejectItem, sendBackItem } from '../review-api';

const intl = createIntl({ locale: 'en' });
const ITEM = { objectKey: 'in/w2.pdf', cycleId: 'c1', queue: 'claims', somethingElse: true };
const sent = () => graphql.mock.calls[0][0] as { query: unknown; variables: Record<string, unknown> };

beforeEach(() => {
  graphql.mockReset();
});

describe('a routing decision in words', () => {
  it('names the rule by its label, or by its id when it has none', () => {
    expect(routedByText(intl, { ruleId: 'claims-over-1000', ruleLabel: 'Claims over 1000' })).toBe('Routed by rule: Claims over 1000');
    expect(routedByText(intl, { ruleId: 'claims-over-1000', ruleLabel: null })).toBe('Routed by rule: claims-over-1000');
    expect(routedByText(intl, { ruleId: 'claims-over-1000', ruleLabel: '' })).toBe('Routed by rule: claims-over-1000');
  });

  it('says the default outcome decided when no rule matched', () => {
    expect(routedByText(intl, { ruleId: '', ruleLabel: null })).toBe('Routed by the default outcome');
  });

  it.each([
    ['queue_missing', 'The queue the routing rule names does not exist. The document is in the Default queue instead.'],
    ['queue_disabled', 'The queue the routing rule names is disabled. The document is in the Default queue instead.'],
    ['evaluation_error', 'The table could not be evaluated. The document is in the Default queue instead.'],
    // A reason this UI has no sentence for is shown as the server sent it.
    ['something_new', 'something_new The document is in the Default queue instead.'],
  ])('says why the document is in the Default queue instead (%s)', (fallbackReason, expected) => {
    expect(routedByFallbackText(intl, { fallbackReason })).toBe(expected);
  });

  it.each([[''], [null], [undefined]])('says nothing more when the document is where the rule sent it (%j)', (fallbackReason) => {
    expect(routedByFallbackText(intl, { fallbackReason })).toBeNull();
  });

  it('is said through the one function: the module has no table-cell component of its own for it', () => {
    // The work items table shortens whichever text it shows, so it formats the text itself.
    expect(Object.keys(routedByModule)).not.toContain('RoutedByText');
  });
});

describe('a text that is shortened', () => {
  it('is left as it is when it fits', () => {
    expect(shortened('Over 1000.', 10)).toBe('Over 1000.');
    expect(shortened('', 10)).toBe('');
  });

  it('is cut to its first characters and ended with an ellipsis, without the space the cut fell on', () => {
    expect(shortened('Over 1000. Check the total.', 10)).toBe('Over 1000.…');
    expect(shortened('Over 1000. Check the total.', 11)).toBe('Over 1000.…');
    expect(shortened('Over 1000. Check the total.', 12)).toBe('Over 1000. C…');
  });

  it('counts characters, so one written with two code units is never cut in half', () => {
    expect(shortened('😀😀😀😀', 2)).toBe('😀😀…');
    expect(shortened('😀😀', 2)).toBe('😀😀');
  });
});

describe('how many policy rules a document failed', () => {
  const failure = { ruleId: 'prior_auth.coverage', policy: 'prior_auth', name: 'coverage', reason: 'Not met.', pages: [1] };

  it('is the count the server kept, which can be more than it lists', () => {
    expect(policyFailureTotal({ policyFailures: [failure], policyFailureCount: 12 })).toBe(12);
  });

  it('is never fewer than are listed', () => {
    expect(policyFailureTotal({ policyFailures: [failure, failure], policyFailureCount: null })).toBe(2);
    expect(policyFailureTotal({ policyFailures: [failure, failure], policyFailureCount: 1 })).toBe(2);
  });

  it.each([
    [null],
    [undefined],
    [{}],
    [{ policyFailures: null, policyFailureCount: null }],
    [{ policyFailures: [], policyFailureCount: 0 }],
  ])('is none for %j', (routedBy) => {
    expect(policyFailureTotal(routedBy)).toBe(0);
  });
});

describe('rejectItem', () => {
  it('sends the item, the reason and the note, and returns the item', async () => {
    graphql.mockResolvedValue({ data: { rejectReviewItem: { objectKey: 'in/w2.pdf', state: 'FINALIZING' } } });

    const item = await rejectItem(ITEM, 'Illegible', 'page 2');

    expect(sent().query).toBe(rejectReviewItem);
    expect(sent().variables).toEqual({ objectKey: 'in/w2.pdf', cycleId: 'c1', reason: 'Illegible', note: 'page 2' });
    expect(item).toMatchObject({ state: 'FINALIZING' });
  });

  it('fails when the server returns nothing', async () => {
    graphql.mockResolvedValue({ data: { rejectReviewItem: null } });

    await expect(rejectItem(ITEM, 'Illegible')).rejects.toThrow('rejectReviewItem returned no result');
  });
});

describe('sendBackItem', () => {
  it('sends the item and the note, and no start step: the server decides where the run starts', async () => {
    graphql.mockResolvedValue({ data: { sendBackReviewItem: { objectKey: 'in/w2.pdf', status: 'QUEUED', startStep: 'classification' } } });

    await expect(sendBackItem(ITEM, 'wrong prompt')).resolves.toBeUndefined();

    expect(sent().query).toBe(sendBackReviewItem);
    expect(sent().variables).toEqual({ objectKey: 'in/w2.pdf', cycleId: 'c1', note: 'wrong prompt' });
    expect(sent().variables).not.toHaveProperty('startStep');
  });

  it('sends the item alone when there is no note', async () => {
    graphql.mockResolvedValue({ data: { sendBackReviewItem: { objectKey: 'in/w2.pdf', status: 'QUEUED', startStep: 'beginning' } } });

    await sendBackItem(ITEM);

    expect(sent().variables).toEqual({ objectKey: 'in/w2.pdf', cycleId: 'c1' });
  });

  it.each([
    [
      { status: 'REFUSED', reason: 'CYCLE_CHANGED', message: 'the document has a newer review cycle' },
      'CYCLE_CHANGED',
      'the document has a newer review cycle',
    ],
    [{ status: 'REFUSED', reason: 'RUN_IN_PROGRESS', message: null }, 'RUN_IN_PROGRESS', 'RUN_IN_PROGRESS'],
    [
      { status: 'FAILED', reason: null, message: 'Could not queue in/w2.pdf to run again.' },
      null,
      'Could not queue in/w2.pdf to run again.',
    ],
    [{ status: 'FAILED', reason: null, message: null }, null, 'FAILED'],
  ])('throws when no run was queued: %j', async (result, code, message) => {
    graphql.mockResolvedValue({ data: { sendBackReviewItem: { objectKey: 'in/w2.pdf', startStep: null, ...result } } });

    const refused = await sendBackItem(ITEM).catch((err: unknown) => err);

    expect(refused).toBeInstanceOf(SendBackRefused);
    expect((refused as SendBackRefused).code).toBe(code);
    expect((refused as SendBackRefused).message).toBe(message);
  });

  it('fails when the server returns nothing', async () => {
    graphql.mockResolvedValue({ data: {} });

    await expect(sendBackItem(ITEM)).rejects.toThrow('sendBackReviewItem returned no result');
  });
});
