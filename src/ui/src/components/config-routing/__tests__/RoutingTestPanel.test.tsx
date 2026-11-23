// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Testing the routing table on a document: the request carries the table as edited,
 * each kind of answer (a matched routing rule, the default outcome, a fallback, stale
 * references, an error) is shown, with the reason a reviewer would read as text and
 * where that text came from, and the tab is told each answer.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));

import RoutingTestPanel from '../RoutingTestPanel';
import { readRouting } from '../routing-types';

const ROUTING = readRouting({
  tables: [
    { rules: [{ id: 'claims', conditions: [{ input: 'page_count', test: '> 1' }], actions: [{ type: 'review_queue', queue: 'claims' }] }] },
  ],
});
const QUEUE_NAMES: Record<string, string> = { claims: 'Claims', default: 'Default' };

const answer = (result: Record<string, unknown>) => ({
  data: {
    testRoutingTable: {
      decision: 'hold',
      matchedRuleId: 'claims',
      ruleLabel: 'Claims over 1000',
      outcomeKind: 'queue',
      outcomeQueue: 'claims',
      placedQueue: 'claims',
      fallbackReason: null,
      inputs: JSON.stringify({ page_count: 3 }),
      staleReferences: [],
      ...result,
    },
  },
});

const renderPanel = () => {
  const onResult = vi.fn();
  render(
    <IntlProvider locale="en">
      <RoutingTestPanel routing={ROUTING} queueName={(id) => QUEUE_NAMES[id] ?? id} onResult={onResult} />
    </IntlProvider>,
  );
  act(() => createWrapper(document.body).findExpandableSection()!.findExpandButton().click());
  return onResult;
};

const test = async (key = 'batch-1/invoice.pdf'): Promise<void> => {
  const input = createWrapper(document.body).findExpandableSection()!.findContent().findInput()!.findNativeInput().getElement();
  act(() => {
    fireEvent.change(input, { target: { value: key } });
  });
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Test' }));
  });
};

describe('the routing test panel', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('cannot be run without a document key', () => {
    renderPanel();

    expect(createWrapper(document.body).findExpandableSection()!.findContent().findButton()!.isDisabled()).toBe(true);
  });

  it('sends the document key, trimmed, and the table as edited', async () => {
    graphql.mockResolvedValue(answer({}));
    renderPanel();

    await test('  batch-1/invoice.pdf ');

    expect(graphql).toHaveBeenCalledTimes(1);
    const { variables } = graphql.mock.calls[0][0] as { variables: { objectKey: string; routing: string } };
    expect(variables.objectKey).toBe('batch-1/invoice.pdf');
    expect(JSON.parse(variables.routing)).toEqual(ROUTING);
  });

  it('shows the matched rule, the queue by name, and the inputs read', async () => {
    graphql.mockResolvedValue(answer({}));
    renderPanel();

    await test();

    expect(screen.getByText('Held for review')).toBeTruthy();
    expect(screen.getByText('Claims over 1000')).toBeTruthy();
    expect(screen.getByText('Claims')).toBeTruthy();
    expect(screen.getByText('page_count')).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
    expect(screen.queryByText('Why the Default queue')).toBeNull();
  });

  it('shows the rule id when the rule has no label', async () => {
    graphql.mockResolvedValue(answer({ ruleLabel: '' }));
    renderPanel();

    await test();

    expect(screen.getByText('claims')).toBeTruthy();
  });

  it('says when the default outcome applied and no review follows', async () => {
    graphql.mockResolvedValue(
      answer({
        decision: 'continue',
        matchedRuleId: '',
        ruleLabel: null,
        outcomeKind: 'no_review',
        outcomeQueue: '',
        placedQueue: '',
        inputs: '{}',
      }),
    );
    renderPanel();

    await test();

    expect(screen.getByText('No review: processing continues')).toBeTruthy();
    expect(screen.getByText('None: the default outcome applied')).toBeTruthy();
    expect(screen.getByText('None')).toBeTruthy();
    expect(screen.getByText('None: no enabled routing rule reads an input.')).toBeTruthy();
  });

  it.each([
    ['queue_missing', 'The queue the routing rule names does not exist.'],
    ['queue_disabled', 'The queue the routing rule names is disabled.'],
    ['evaluation_error', 'The table could not be evaluated.'],
    ['something_new', 'something_new'],
  ])('explains the fallback %s and shows where the document lands', async (reason, text) => {
    graphql.mockResolvedValue(answer({ placedQueue: 'default', fallbackReason: reason }));
    renderPanel();

    await test();

    expect(screen.getByText('Why the Default queue')).toBeTruthy();
    expect(screen.getByText(text)).toBeTruthy();
    expect(screen.getByText('Default')).toBeTruthy();
  });

  it('warns about what the table names and the document does not have', async () => {
    graphql.mockResolvedValue(answer({ staleReferences: ["class 'Invoice'", "section result of 'W2'"] }));
    renderPanel();

    await test();

    expect(screen.getByText('The table refers to things this document does not have')).toBeTruthy();
    expect(screen.getByText("class 'Invoice'")).toBeTruthy();
    expect(screen.getByText("section result of 'W2'")).toBeTruthy();
  });

  it('shows no warning when nothing is stale', async () => {
    graphql.mockResolvedValue(answer({ staleReferences: null }));
    renderPanel();

    await test();

    expect(screen.queryByText('The table refers to things this document does not have')).toBeNull();
  });

  it("shows the server's message when the test fails, and no result", async () => {
    graphql.mockRejectedValue(new Error("INVALID_ROUTING: Routing table 'review-routing', rule 'claims': the test is not valid"));
    renderPanel();

    await test();

    await waitFor(() => expect(screen.getByText('The table could not be tested')).toBeTruthy());
    expect(screen.getByText(/INVALID_ROUTING: Routing table 'review-routing'/)).toBeTruthy();
    expect(screen.queryByText('Held for review')).toBeNull();
  });

  it("shows the message inside the client's error envelope, never the envelope itself", async () => {
    // What the REST client rejects with when the server refuses: the dispatcher's body, not an Error.
    graphql.mockRejectedValue({ errors: [{ message: 'NOT_FOUND: no such document', errorType: 'NotFound' }] });
    renderPanel();

    await test();

    await waitFor(() => expect(screen.getByText('The table could not be tested')).toBeTruthy());
    expect(screen.getByText('NOT_FOUND: no such document')).toBeTruthy();
    expect(screen.queryByText(/object Object/)).toBeNull();
  });

  it('clears the earlier result when a new test fails', async () => {
    graphql.mockResolvedValueOnce(answer({}));
    graphql.mockRejectedValueOnce(new Error('NOT_FOUND: no such document'));
    renderPanel();

    await test();
    expect(screen.getByText('Held for review')).toBeTruthy();
    await test('batch-1/other.pdf');

    expect(screen.queryByText('Held for review')).toBeNull();
    expect(screen.getByText('NOT_FOUND: no such document')).toBeTruthy();
  });

  it('reads inputs that arrive as text that is not an object as none', async () => {
    graphql.mockResolvedValue(answer({ inputs: 'not json' }));
    renderPanel();

    await test();

    expect(screen.getByText('None: no enabled routing rule reads an input.')).toBeTruthy();
  });

  it('labels the routing rule that matched', async () => {
    graphql.mockResolvedValue(answer({}));
    renderPanel();

    await test();

    expect(screen.getByText('Matched routing rule')).toBeTruthy();
  });
});

describe('the reason of a tested document', () => {
  const REASON = 'Invoice total 1250.00 is over 1000. Check the total against the line items.';

  beforeEach(() => {
    graphql.mockReset();
  });

  it('is shown as the reviewer would read it, and said to be the reason as written', async () => {
    graphql.mockResolvedValue(answer({ reason: REASON, reasonSource: 'admin' }));
    renderPanel();

    await test();

    expect(screen.getByText('Reason for the reviewer')).toBeTruthy();
    expect(screen.getByText(REASON)).toBeTruthy();
    expect(screen.getByText('The reason as written, with the values of this document.')).toBeTruthy();
    expect(screen.queryByText(/^Generated:/)).toBeNull();
  });

  it('is said to be generated when the outcome has no reason of its own', async () => {
    graphql.mockResolvedValue(answer({ reason: 'Claims over 1000: Page count > 1 (found 3)', reasonSource: 'fallback' }));
    renderPanel();

    await test();

    expect(screen.getByText('Claims over 1000: Page count > 1 (found 3)')).toBeTruthy();
    expect(screen.getByText('Generated: the outcome has no reason, or its reason could not be filled in for this document.')).toBeTruthy();
    expect(screen.queryByText('The reason as written, with the values of this document.')).toBeNull();
  });

  it('is shown without a source when the server names one this UI does not know', async () => {
    graphql.mockResolvedValue(answer({ reason: REASON, reasonSource: 'something_new' }));
    renderPanel();

    await test();

    expect(screen.getByText(REASON)).toBeTruthy();
    expect(screen.queryByText(/as written/)).toBeNull();
    expect(screen.queryByText(/^Generated:/)).toBeNull();
  });

  it.each([[null], [undefined], [''], ['   ']])('is not shown when the answer holds the reason %j', async (reason) => {
    graphql.mockResolvedValue(answer({ reason, reasonSource: 'admin' }));
    renderPanel();

    await test();

    expect(screen.getByText('Held for review')).toBeTruthy();
    expect(screen.queryByText('Reason for the reviewer')).toBeNull();
    expect(screen.queryByText(/as written/)).toBeNull();
  });

  it('is shown as text, not as markup', async () => {
    const markup = 'Vendor <img src=x onerror="alert(1)"> **Acme** [site](https://example.com) <b>bold</b>';
    graphql.mockResolvedValue(answer({ reason: markup, reasonSource: 'admin' }));
    renderPanel();

    await test();

    const content = createWrapper(document.body).findExpandableSection()!.findContent().getElement();
    expect(screen.getByText(markup)).toBeTruthy();
    expect(content.querySelector('img')).toBeNull();
    expect(content.querySelector('a')).toBeNull();
    expect(content.querySelector('b')).toBeNull();
  });

  it('comes after the decision and before the inputs read', async () => {
    graphql.mockResolvedValue(answer({ reason: REASON, reasonSource: 'admin' }));
    renderPanel();

    await test();

    const text = createWrapper(document.body).findExpandableSection()!.findContent().getElement().textContent ?? '';
    expect(text.indexOf('Review queue')).toBeLessThan(text.indexOf('Reason for the reviewer'));
    expect(text.indexOf('Reason for the reviewer')).toBeLessThan(text.indexOf('Inputs read'));
  });

  it('is told to the tab with the answer', async () => {
    graphql.mockResolvedValue(answer({ reason: REASON, reasonSource: 'admin' }));
    const onResult = renderPanel();

    await test();

    expect(onResult).toHaveBeenLastCalledWith(expect.objectContaining({ reason: REASON, reasonSource: 'admin' }));
  });
});

describe('what the tab is told', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('is nothing before a test', () => {
    const onResult = renderPanel();

    expect(onResult).not.toHaveBeenCalled();
  });

  it('is that a test started, then its answer', async () => {
    graphql.mockResolvedValue(answer({ matchedRuleId: 'claims' }));
    const onResult = renderPanel();

    await test();

    expect(onResult.mock.calls.map(([result]) => result?.matchedRuleId ?? null)).toEqual([null, 'claims']);
    expect(onResult.mock.calls[1][0]).toMatchObject({ decision: 'hold', placedQueue: 'claims' });
  });

  it('is that there is no answer when a test fails after one that answered', async () => {
    graphql.mockResolvedValueOnce(answer({}));
    graphql.mockRejectedValueOnce(new Error('NOT_FOUND: no such document'));
    const onResult = renderPanel();

    await test();
    await test('batch-1/other.pdf');

    expect(onResult).toHaveBeenLastCalledWith(null);
    expect(onResult).toHaveBeenCalledTimes(3);
  });

  it('can be left out', async () => {
    graphql.mockResolvedValue(answer({}));
    render(
      <IntlProvider locale="en">
        <RoutingTestPanel routing={ROUTING} queueName={(id) => id} />
      </IntlProvider>,
    );
    act(() => createWrapper(document.body).findExpandableSection()!.findExpandButton().click());

    await test();

    expect(screen.getByText('Held for review')).toBeTruthy();
  });
});
