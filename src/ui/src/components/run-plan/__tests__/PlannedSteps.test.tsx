// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What a run will do, shown before it starts. What has to hold: while the plan is read
 * a loading state shows, and a failed read shows a warning with the reason and a Retry;
 * a plan shows the step the run starts at, then every step in the server's order with
 * its own state (Runs, Skipped, Not enabled), named by the picker's messages (OCR for
 * Pipeline mode's first step), then the sections extracted again with the cause of each
 * in words. The hook that reads the plan reads only while its dialog is open, reports a
 * failure instead of throwing, reads again on retry, and never shows an answer that a
 * later read replaced. The API helpers send what names the run (a review item's key and
 * cycle; a document's key for Process Changes) and return what the server answers, and
 * fail when it answers nothing.
 */

import React from 'react';
import { act, render, renderHook, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { IntlProvider } from 'react-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }),
}));

import {
  getProcessChangesPlan as getProcessChangesPlanQuery,
  getReviewCompletionPlan as getReviewCompletionPlanQuery,
} from '../../../graphql/generated';
import type { PlannedRun } from '../../../graphql/generated/operation-types';
import PlannedSteps from '../PlannedSteps';
import { getProcessChangesPlan, getReviewCompletionPlan } from '../run-plan-api';
import usePlannedRun from '../use-planned-run';

type Props = React.ComponentProps<typeof PlannedSteps>;

/** A completed review that regrouped two sections, in Pipeline mode, with Review Routing switched off. */
const PIPELINE: PlannedRun = {
  mode: 'pipeline',
  startStep: 'extraction',
  steps: [
    { id: 'beginning', label: 'Beginning', state: 'skipped' },
    { id: 'classification', label: 'Classification', state: 'skipped' },
    { id: 'extraction', label: 'Extraction', state: 'run' },
    { id: 'assessment', label: 'Assessment', state: 'run' },
    { id: 'apply_review_changes', label: 'Apply Review Changes', state: 'run' },
    { id: 'policy_rule_validation', label: 'Policy Rule Validation', state: 'run' },
    { id: 'review_routing', label: 'Review Routing', state: 'not_enabled' },
    { id: 'summarization', label: 'Summarization', state: 'run' },
    { id: 'evaluation', label: 'Evaluation', state: 'skipped' },
  ],
  sections: [
    { sectionId: '2', cause: 'pages_changed' },
    { sectionId: '4', cause: 'added' },
  ],
};

/** A send-back in BDA mode: everything runs again, and no section is named. */
const BDA: PlannedRun = {
  mode: 'bda',
  startStep: 'beginning',
  steps: [
    { id: 'beginning', label: 'Beginning', state: 'run' },
    { id: 'policy_rule_validation', label: 'Policy Rule Validation', state: 'not_enabled' },
    { id: 'review_routing', label: 'Review Routing', state: 'run' },
    { id: 'summarization', label: 'Summarization', state: 'run' },
    { id: 'evaluation', label: 'Evaluation', state: 'run' },
  ],
  sections: [],
};

const show = (props: Partial<Props> = {}) => {
  const onRetry = vi.fn();
  const view = render(
    <IntlProvider locale="en">
      <PlannedSteps plan={null} loading={false} error={null} onRetry={onRetry} {...props} />
    </IntlProvider>,
  );
  return { onRetry, wrapper: createWrapper(view.container), container: view.container };
};

/** The kind of a status indicator (success, stopped, not-started, ...), read off the class Cloudscape gives it. */
const indicatorKind = (element: HTMLElement): string | undefined => /status-([a-z-]+?)_/.exec(element.className)?.[1];

/** Each step as shown, in order: its name, its state in words, and the kind of its status indicator. */
const rows = (wrapper: ReturnType<typeof createWrapper>): [string | null, string | null, string | undefined][] =>
  wrapper
    .findList()!
    .findItems()
    .map((item) => {
      const indicator = item.findActions()!.findStatusIndicator()!.getElement();
      return [item.findContent().getElement().textContent, indicator.textContent, indicatorKind(indicator)];
    });

beforeEach(() => {
  graphql.mockReset();
});

describe('while the plan is not known', () => {
  it('shows a loading state, and no list', () => {
    const { wrapper } = show({ loading: true });

    const indicator = wrapper.findStatusIndicator()!.getElement();
    expect(indicator.textContent).toBe('Loading the steps of the run');
    expect(indicatorKind(indicator)).toBe('loading');
    expect(wrapper.findList()).toBeNull();
    expect(wrapper.findAlert()).toBeNull();
  });

  it('shows a warning with the reason and a Retry when the read failed', () => {
    const { wrapper, onRetry } = show({ error: 'NOT_LEASE_HOLDER: this item is assigned to someone else' });

    const alert = wrapper.findAlert()!;
    expect(alert.findRootElement().getElement().className).toMatch(/type-warning/);
    expect(alert.findHeader()!.getElement().textContent).toBe('The steps of the run could not be loaded');
    expect(alert.findContent().getElement().textContent).toBe('NOT_LEASE_HOLDER: this item is assigned to someone else');
    expect(wrapper.findList()).toBeNull();

    const retry = alert.findActionSlot()!.findButton()!;
    expect(retry.getElement().textContent).toBe('Retry');
    retry.click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('shows the loading state, not a stale failure, while a retry is read', () => {
    const { wrapper } = show({ loading: true, error: 'offline' });

    expect(wrapper.findAlert()).toBeNull();
    expect(wrapper.findStatusIndicator()!.getElement().textContent).toBe('Loading the steps of the run');
  });

  it('shows nothing when there is no plan, no read and no failure', () => {
    const { container } = show();

    expect(container.textContent).toBe('');
  });
});

describe('a plan', () => {
  it('lists every step in order with its own state: Runs, Skipped or Not enabled', () => {
    const { wrapper } = show({ plan: PIPELINE });

    expect(rows(wrapper)).toEqual([
      ['OCR (beginning)', 'Skipped', 'stopped'],
      ['Classification', 'Skipped', 'stopped'],
      ['Extraction', 'Runs', 'success'],
      ['Assessment', 'Runs', 'success'],
      ['Apply Review Changes', 'Runs', 'success'],
      ['Policy Rule Validation', 'Runs', 'success'],
      ['Review Routing', 'Not enabled', 'not-started'],
      ['Summarization', 'Runs', 'success'],
      ['Evaluation', 'Skipped', 'stopped'],
    ]);
  });

  it('is an ordered list with a name, under a heading', () => {
    const { wrapper, container } = show({ plan: PIPELINE });

    const list = wrapper.findList()!.getElement();
    expect(list.tagName).toBe('OL');
    expect(list.getAttribute('aria-label')).toBe('The processing steps in order, each with whether the run does it');
    expect(container.textContent).toContain('Steps of the run');
  });

  it('names the step the run starts at', () => {
    expect(show({ plan: PIPELINE }).container.textContent).toContain('Starts at Extraction');
    document.body.innerHTML = '';
    expect(show({ plan: { ...PIPELINE, startStep: 'apply_review_changes' } }).container.textContent).toContain(
      'Starts at Apply Review Changes',
    );
  });

  it('names the first step for its mode: OCR in Pipeline mode, the beginning in BDA mode', () => {
    const pipeline = show({ plan: { ...PIPELINE, startStep: 'beginning' } });
    expect(pipeline.container.textContent).toContain('Starts at OCR (beginning)');
    expect(rows(pipeline.wrapper)[0][0]).toBe('OCR (beginning)');
    document.body.innerHTML = '';

    const bda = show({ plan: BDA });
    expect(bda.container.textContent).toContain('Starts at Beginning');
    expect(rows(bda.wrapper)).toEqual([
      ['Beginning', 'Runs', 'success'],
      ['Policy Rule Validation', 'Not enabled', 'not-started'],
      ['Review Routing', 'Runs', 'success'],
      ['Summarization', 'Runs', 'success'],
      ['Evaluation', 'Runs', 'success'],
    ]);
  });

  it('says which sections are extracted again, each with its cause in words', () => {
    const { container } = show({ plan: PIPELINE });

    expect(container.textContent).toContain('Sections extracted again: 2 (pages changed), 4 (added)');
  });

  it('words a section that has no result, and shows a cause it has no words for as the server sent it', () => {
    const { container } = show({
      plan: {
        ...PIPELINE,
        sections: [
          { sectionId: '3', cause: 'no_result' },
          { sectionId: '7', cause: 'something_new' },
        ],
      },
    });

    expect(container.textContent).toContain('Sections extracted again: 3 (no result), 7 (something_new)');
  });

  it('has no sections line when the run names no section', () => {
    const { container } = show({ plan: BDA });

    expect(container.textContent).not.toContain('Sections extracted again');
  });

  it('shows a step and a state it has no message for as the server sent them', () => {
    const { wrapper } = show({
      plan: { ...BDA, steps: [{ id: 'redaction', label: 'Redaction', state: 'deferred' }] },
    });

    expect(rows(wrapper)).toEqual([['Redaction', 'deferred', 'info']]);
  });
});

describe('reading the plan', () => {
  it('reads while the dialog is open: loading first, then the plan', async () => {
    const load = vi.fn().mockResolvedValue(PIPELINE);
    const hook = renderHook(() => usePlannedRun(load, true));

    expect(hook.result.current).toMatchObject({ plan: null, loading: true, error: null });
    await waitFor(() => expect(hook.result.current.loading).toBe(false));
    expect(hook.result.current).toMatchObject({ plan: PIPELINE, error: null });
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('reads nothing while the dialog is closed, and reads when it opens', async () => {
    const load = vi.fn().mockResolvedValue(BDA);
    const hook = renderHook(({ active }) => usePlannedRun(load, active), { initialProps: { active: false } });

    expect(hook.result.current).toMatchObject({ plan: null, loading: false, error: null });
    expect(load).not.toHaveBeenCalled();

    hook.rerender({ active: true });
    await waitFor(() => expect(hook.result.current.plan).toEqual(BDA));
    expect(load).toHaveBeenCalledTimes(1);

    // Closed again, nothing is kept for the next opening.
    hook.rerender({ active: false });
    expect(hook.result.current).toMatchObject({ plan: null, loading: false, error: null });
  });

  it('reports a failed read in words instead of throwing, and reads again on retry', async () => {
    const load = vi
      .fn()
      .mockRejectedValueOnce({ errors: [{ message: 'NOT_LEASE_HOLDER: this item is assigned to someone else' }] })
      .mockResolvedValueOnce(PIPELINE);
    const hook = renderHook(() => usePlannedRun(load, true));

    await waitFor(() => expect(hook.result.current.loading).toBe(false));
    expect(hook.result.current).toMatchObject({ plan: null, error: 'NOT_LEASE_HOLDER: this item is assigned to someone else' });

    act(() => hook.result.current.retry());
    // The failure is cleared as soon as the retry starts.
    expect(hook.result.current).toMatchObject({ plan: null, loading: true, error: null });
    await waitFor(() => expect(hook.result.current.plan).toEqual(PIPELINE));
    expect(hook.result.current.error).toBeNull();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it('keeps the answer of the latest read when an earlier one arrives after it', async () => {
    const answers: ((plan: PlannedRun) => void)[] = [];
    const load = vi.fn(() => new Promise<PlannedRun>((resolve) => answers.push(resolve)));
    const hook = renderHook(() => usePlannedRun(load, true));

    act(() => hook.result.current.retry());
    expect(load).toHaveBeenCalledTimes(2);
    await act(async () => answers[1](BDA));
    await act(async () => answers[0](PIPELINE));

    expect(hook.result.current.plan).toEqual(BDA);
  });

  it('reads again when what it reads changes (another item)', async () => {
    const first = vi.fn().mockResolvedValue(PIPELINE);
    const second = vi.fn().mockResolvedValue(BDA);
    const hook = renderHook(({ load }) => usePlannedRun(load, true), { initialProps: { load: first } });
    await waitFor(() => expect(hook.result.current.plan).toEqual(PIPELINE));

    hook.rerender({ load: second });
    await waitFor(() => expect(hook.result.current.plan).toEqual(BDA));
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });
});

describe('getReviewCompletionPlan', () => {
  it('asks for the item by its key and cycle, and returns both endings', async () => {
    graphql.mockResolvedValue({ data: { getReviewCompletionPlan: { complete: PIPELINE, sendBack: BDA } } });

    await expect(getReviewCompletionPlan('in/w2.pdf', 'c1')).resolves.toEqual({ complete: PIPELINE, sendBack: BDA });

    expect(graphql).toHaveBeenCalledTimes(1);
    const sent = graphql.mock.calls[0][0] as { query: unknown; variables: unknown };
    expect(sent.query).toBe(getReviewCompletionPlanQuery);
    expect(sent.variables).toEqual({ objectKey: 'in/w2.pdf', cycleId: 'c1' });
  });

  it('fails when the server returns nothing', async () => {
    graphql.mockResolvedValue({ data: {} });

    await expect(getReviewCompletionPlan('in/w2.pdf', 'c1')).rejects.toThrow('getReviewCompletionPlan returned no result');
  });
});

describe('getProcessChangesPlan', () => {
  it('asks for the document by its key, and returns the planned run', async () => {
    graphql.mockResolvedValue({ data: { getProcessChangesPlan: PIPELINE } });

    await expect(getProcessChangesPlan('in/w2.pdf')).resolves.toEqual(PIPELINE);

    expect(graphql).toHaveBeenCalledTimes(1);
    const sent = graphql.mock.calls[0][0] as { query: unknown; variables: unknown };
    expect(sent.query).toBe(getProcessChangesPlanQuery);
    expect(sent.variables).toEqual({ objectKey: 'in/w2.pdf' });
  });

  it('fails when the server returns nothing', async () => {
    graphql.mockResolvedValue({ data: {} });

    await expect(getProcessChangesPlan('in/w2.pdf')).rejects.toThrow('getProcessChangesPlan returned no result');
  });

  it('passes on a refusal: the server answers an error, not a plan, for a document in a review queue', async () => {
    const refusal = { errors: [{ message: 'QUEUE_MANAGED: this document is in a review queue; work it from Review Queues' }] };
    graphql.mockRejectedValue(refusal);

    await expect(getProcessChangesPlan('in/w2.pdf')).rejects.toBe(refusal);
  });
});
