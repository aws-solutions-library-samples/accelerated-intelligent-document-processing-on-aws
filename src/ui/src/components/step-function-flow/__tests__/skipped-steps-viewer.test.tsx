// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The step viewer shows the steps a run plan skipped. What has to hold: a run that
 * started at policy rule validation shows OCR, Classification, Extraction and Assessment
 * as Skipped, in step order, in the diagram and in the timeline; choosing one explains
 * that the run plan skipped it and where the run started; a step the configuration
 * switched off is still shown as that, not as skipped; and a run with no plan shows no
 * such step.
 */

import React from 'react';
import { act, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));

import StepFunctionFlowViewer from '../StepFunctionFlowViewer';

const ARN = 'arn:aws:states:us-east-1:111122223333:execution:workflow:run-1';
const at = (minute: number) => `2026-11-01T10:${String(minute).padStart(2, '0')}:00.000Z`;
const step = (name: string, minute: number, type = 'Task') => ({
  name,
  type,
  status: 'SUCCEEDED',
  startDate: at(minute),
  stopDate: at(minute + 1),
});
const runPlan = (over: Record<string, unknown> = {}) => ({
  v: 1,
  kind: 'reprocess',
  startStep: 'policy_rule_validation',
  startAt: 'policy_rule_validation',
  skipAssessment: false,
  skipPolicyRuleValidation: false,
  skipSummarization: false,
  skipEvaluation: false,
  policyRuleValidationEnabled: true,
  sections: [],
  reviewed: false,
  mode: 'pipeline',
  cycleId: '',
  ...over,
});
/** A run that started at policy rule validation. */
const LATE_START = [
  step('CheckRunPlan', 0, 'Choice'),
  step('StartAtPolicyRuleValidation', 1, 'Pass'),
  step('PolicyClassificationStep', 2),
  step('RoutingStep', 3),
  step('SummarizationStep', 4),
  step('EvaluationStep', 5),
];

const answer = (steps: unknown[], input: unknown) => ({
  data: { getStepFunctionExecution: { executionArn: ARN, status: 'SUCCEEDED', startDate: at(0), stopDate: at(6), input, steps } },
});

const open = async (props: Partial<React.ComponentProps<typeof StepFunctionFlowViewer>> = {}) => {
  renderWithIntl(<StepFunctionFlowViewer executionArn={ARN} visible onDismiss={() => undefined} {...props} />);
  await waitFor(() => expect(document.querySelector('.flow-step')).not.toBeNull());
};

/** The diagram's nodes, in order: the name and the status each shows. */
const nodes = () =>
  [...document.querySelectorAll('.flow-container > .flow-step')].map((node) => ({
    name: node.querySelector('.step-name > span')?.textContent ?? '',
    status: node.querySelector('.step-status-text')?.textContent ?? '',
    greyed: node.classList.contains('step-disabled'),
    element: node as HTMLElement,
  }));
const timeline = () =>
  [...document.querySelectorAll('.steps-timeline > .timeline-step')].map((entry) => ({
    name: (entry.querySelector('.timeline-step-name')?.childNodes[0]?.textContent ?? '').trim(),
    meta: entry.querySelector('.timeline-step-meta')?.textContent ?? '',
    greyed: entry.classList.contains('step-disabled'),
  }));
const skipped = () => nodes().filter((node) => node.status === 'Skipped');

beforeEach(() => {
  document.body.innerHTML = '';
  graphql.mockReset();
});

describe('a run that started at policy rule validation', () => {
  beforeEach(() => {
    graphql.mockResolvedValue(answer(LATE_START, JSON.stringify({ document: {}, runPlan: runPlan() })));
  });

  it('shows the four steps before it as Skipped, in step order, ahead of the first step that ran', async () => {
    await open();

    expect(nodes().map((node) => `${node.name}: ${node.status}`)).toEqual([
      'CheckRunPlan: SUCCEEDED',
      'StartAtPolicyRuleValidation: SUCCEEDED',
      'OCR: Skipped',
      'Classification: Skipped',
      'Extraction: Skipped',
      'Assessment: Skipped',
      'PolicyClassificationStep: SUCCEEDED',
      'RoutingStep: SUCCEEDED',
      'SummarizationStep: SUCCEEDED',
      'EvaluationStep: SUCCEEDED',
    ]);
    expect(skipped().every((node) => node.greyed)).toBe(true);
    expect(nodes().filter((node) => node.status !== 'Skipped' && node.greyed)).toEqual([]);
  });

  it('shows them in the timeline too, with no duration', async () => {
    await open();

    const entries = timeline().filter((entry) => ['OCR', 'Classification', 'Extraction', 'Assessment'].includes(entry.name));
    expect(entries.map((entry) => entry.meta)).toEqual(['Skipped', 'Skipped', 'Skipped', 'Skipped']);
    // Greyed like a step the configuration switched off, and no entry that ran is.
    expect(entries.every((entry) => entry.greyed)).toBe(true);
    expect(timeline().filter((entry) => entry.greyed)).toHaveLength(4);
  });

  it('explains a skipped step when it is chosen, and shows no times, input or output for it', async () => {
    await open();

    act(() => skipped()[1].element.click());

    const details = document.querySelector('.step-details')!;
    expect(details.textContent).toContain('Classification');
    expect(details.textContent).toContain('Skipped by the run plan. This run started at Policy Rule Validation.');
    expect(details.textContent).not.toContain('Duration');
    expect(details.textContent).not.toContain('No input data available');
    // The hover text of the node says the same.
    expect(skipped()[1].element.getAttribute('title')).toBe('Skipped by the run plan. This run started at Policy Rule Validation.');
  });

  it('can be chosen from the keyboard', async () => {
    await open();

    fireEvent.keyDown(skipped()[0].element, { key: 'Enter' });

    expect(document.querySelector('.step-details')!.textContent).toContain('Skipped by the run plan.');
  });

  it('keeps a step the configuration switched off as not enabled, which is not skipped', async () => {
    await open({ mergedConfig: { summarization: { enabled: false } } });

    const summarization = nodes().find((node) => node.name.startsWith('SummarizationStep'))!;
    expect(summarization.status).toBe('SUCCEEDED');
    expect(summarization.greyed).toBe(true);
    expect(summarization.element.textContent).toContain('NOT ENABLED');
    expect(skipped().map((node) => node.name)).toEqual(['OCR', 'Classification', 'Extraction', 'Assessment']);
  });

  it('marks policy rule validation and Review Routing as not enabled when the configuration has them off', async () => {
    await open({ mergedConfig: { rule_validation: { enabled: true }, policy_classes: [], hitl: { enabled: false } } });

    const notEnabled = nodes()
      .filter((node) => node.element.textContent?.includes('NOT ENABLED'))
      .map((node) => node.name);
    expect(notEnabled).toEqual(['PolicyClassificationStep', 'RoutingStep']);
    expect(nodes().filter((node) => notEnabled.includes(node.name) && !node.greyed)).toEqual([]);
    // The timeline greys the same two out.
    expect(
      timeline()
        .filter((entry) => entry.greyed && !entry.meta.includes('Skipped'))
        .map((entry) => entry.name),
    ).toEqual(['PolicyClassificationStep', 'RoutingStep']);
  });

  it('marks neither as not enabled while they are on', async () => {
    await open({ mergedConfig: { rule_validation: { enabled: true }, policy_classes: [{ name: 'Policy' }], hitl: { enabled: true } } });

    expect(nodes().filter((node) => node.element.textContent?.includes('NOT ENABLED'))).toEqual([]);
  });
});

describe('other runs', () => {
  it('shows a step the plan skips after the start, where it belongs', async () => {
    const steps = [
      step('CheckRunPlan', 0, 'Choice'),
      step('OCRStep', 1),
      step('RoutingStep', 2),
      step('SkipSummarization', 3, 'Pass'),
      step('EvaluationStep', 4),
    ];
    graphql.mockResolvedValue(
      answer(steps, JSON.stringify({ runPlan: runPlan({ startStep: 'beginning', startAt: 'beginning', skipSummarization: true }) })),
    );
    await open();

    expect(nodes().map((node) => `${node.name}: ${node.status}`)).toEqual([
      'CheckRunPlan: SUCCEEDED',
      'OCRStep: SUCCEEDED',
      'RoutingStep: SUCCEEDED',
      'SkipSummarization: SUCCEEDED',
      'Summarization: Skipped',
      'EvaluationStep: SUCCEEDED',
    ]);
    act(() => skipped()[0].element.click());
    // The run started at the beginning, which in Pipeline mode is OCR.
    expect(document.querySelector('.step-details')!.textContent).toContain('Skipped by the run plan. This run started at OCR.');
  });

  it('names the first step of a BDA run after what it does', async () => {
    const steps = [step('CheckRunPlan', 0, 'Choice'), step('SummarizationStep', 1)];
    graphql.mockResolvedValue(
      answer(steps, JSON.stringify({ runPlan: runPlan({ mode: 'bda', startStep: 'summarization', skipPolicyRuleValidation: true }) })),
    );
    await open();

    expect(skipped().map((node) => node.name)).toEqual(['Bedrock Data Automation', 'Policy Rule Validation']);
  });

  it('says a run after a completed review started at Apply Review Changes', async () => {
    const steps = [step('CheckRunPlan', 0, 'Choice'), step('ApplyReviewChangesStep', 1), step('PolicyClassificationStep', 2)];
    graphql.mockResolvedValue(
      answer(
        steps,
        JSON.stringify({
          runPlan: runPlan({ kind: 'review_completed', startStep: 'apply_review_changes', startAt: 'apply_review_changes' }),
        }),
      ),
    );
    await open();

    expect(skipped().map((node) => node.name)).toEqual(['OCR', 'Classification', 'Extraction', 'Assessment']);
    act(() => skipped()[0].element.click());
    expect(document.querySelector('.step-details')!.textContent).toContain(
      'Skipped by the run plan. This run started at Apply Review Changes.',
    );
  });

  it('says a run that recorded a rejected review did no step', async () => {
    graphql.mockResolvedValue(
      answer(
        [step('CheckRunPlan', 0, 'Choice')],
        JSON.stringify({ runPlan: runPlan({ kind: 'review_rejected', startStep: 'rejected', startAt: 'rejected' }) }),
      ),
    );
    await open();

    expect(skipped()).toHaveLength(7);
    act(() => skipped()[0].element.click());
    expect(document.querySelector('.step-details')!.textContent).toContain(
      'Skipped by the run plan. This run recorded a rejected review and did no processing step.',
    );
  });

  it.each([
    ['an execution with no run plan', JSON.stringify({ document: {} })],
    ['an execution with no input', null],
  ])('shows no skipped step for %s', async (_what, input) => {
    graphql.mockResolvedValue(answer(LATE_START, input));
    await open();

    expect(skipped()).toEqual([]);
    expect(nodes()).toHaveLength(LATE_START.length);
  });

  it('shows no skipped step for a run from the beginning that is still running', async () => {
    const running = { ...step('OCRStep', 1), status: 'RUNNING', stopDate: undefined };
    graphql.mockResolvedValue({
      data: {
        getStepFunctionExecution: {
          executionArn: ARN,
          status: 'RUNNING',
          startDate: at(0),
          input: JSON.stringify({ runPlan: runPlan({ startStep: 'beginning', startAt: 'beginning' }) }),
          steps: [step('CheckRunPlan', 0, 'Choice'), running],
        },
      },
    });
    await open();

    expect(skipped()).toEqual([]);
  });
});
