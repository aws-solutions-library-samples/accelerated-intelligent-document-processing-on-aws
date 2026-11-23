// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The steps a run plan skipped: those before the step the run started at and those the
 * plan names, as long as the execution entered none of their states. A run with no plan
 * skipped nothing this can tell, and a step that is merely not reached yet is not
 * skipped. A run that starts at a display step of the step catalogue (Apply Review
 * Changes, after a completed review) skipped the steps before the run step that display
 * step comes right before; the ids of the display steps are the catalogue's.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { DISPLAY_STEPS, stepsForMode } from '../../run-plan/run-steps';
import { readRunPlan, skippedStepIds, stepIndexOfState, withSkippedSteps, type ExecutionRunPlan } from '../skipped-steps';

const block = (over: Record<string, unknown> = {}) => ({
  v: 1,
  kind: 'reprocess',
  startStep: 'beginning',
  startAt: 'beginning',
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
const input = (over: Record<string, unknown> = {}) => JSON.stringify({ document: { compressed: true }, runPlan: block(over) });
const plan = (over: Record<string, unknown> = {}): ExecutionRunPlan => readRunPlan(input(over))!;
const entered = (...states: string[]) => new Set(states);

describe('readRunPlan', () => {
  it('reads the mode, the start step and the steps the plan skips', () => {
    const read = plan({ startStep: 'extraction', skipSummarization: true, skipEvaluation: true });

    expect(read.mode).toBe('pipeline');
    expect(read.startStep).toBe('extraction');
    expect([...read.skipped].sort()).toEqual(['evaluation', 'summarization']);
  });

  it('reads the input as text or as the object it parses to', () => {
    expect(readRunPlan(JSON.parse(input({ mode: 'bda' })))).toMatchObject({ mode: 'bda', startStep: 'beginning' });
  });

  it('knows each skip flag by the step it skips', () => {
    expect([...plan({ skipAssessment: true }).skipped]).toEqual(['assessment']);
    expect([...plan({ skipPolicyRuleValidation: true }).skipped]).toEqual(['policy_rule_validation']);
    // BDA mode has no assessment step: its flag skips nothing there.
    expect([...plan({ mode: 'bda', skipAssessment: true }).skipped]).toEqual([]);
    // Only true skips: a flag that is absent or not a boolean does not.
    expect([...plan({ skipSummarization: 'true', skipEvaluation: 1 }).skipped]).toEqual([]);
  });

  it.each([
    ['no input', null],
    ['an empty input', ''],
    ['text that is not JSON', 'not json'],
    ['an input with no run plan', JSON.stringify({ document: {} })],
    ['a run plan that is not an object', JSON.stringify({ runPlan: 'x' })],
    ['a mode it does not know', input({ mode: 'other' })],
    ['no start step', input({ startStep: '' })],
    ['a start step that is not text', input({ startStep: 3 })],
  ])('reads %s as no plan', (_what, raw) => {
    expect(readRunPlan(raw)).toBeNull();
  });
});

describe('skippedStepIds', () => {
  it('is nothing for a run from the beginning that skips nothing', () => {
    expect(skippedStepIds(plan(), entered())).toEqual([]);
  });

  it('is every step before the start', () => {
    const started = plan({ startStep: 'policy_rule_validation', startAt: 'policy_rule_validation' });

    expect(skippedStepIds(started, entered('CheckRunPlan', 'StartAtPolicyRuleValidation', 'PolicyClassificationStep'))).toEqual([
      'beginning',
      'classification',
      'extraction',
      'assessment',
    ]);
  });

  it('includes the steps a later start forces the run to skip', () => {
    // A run from summarization enters at policy rule validation with that step skipped.
    const started = plan({ startStep: 'summarization', startAt: 'policy_rule_validation', skipPolicyRuleValidation: true });

    expect(skippedStepIds(started, entered('SkipPolicyRuleValidation', 'RoutingStep', 'SummarizationStep'))).toEqual([
      'beginning',
      'classification',
      'extraction',
      'assessment',
      'policy_rule_validation',
    ]);
  });

  it('includes an optional step the plan skips after the start', () => {
    const skipping = plan({ skipSummarization: true, skipEvaluation: true });

    expect(skippedStepIds(skipping, entered('OCRStep', 'ClassificationStep', 'SkipSummarization', 'SkipEvaluation'))).toEqual([
      'summarization',
      'evaluation',
    ]);
  });

  it('leaves out a step the execution entered, whatever the plan says', () => {
    const started = plan({ startStep: 'extraction', skipSummarization: true });

    // A plan that says extraction is the start, and a history that shows OCR ran.
    expect(skippedStepIds(started, entered('OCRStep', 'ExtractionStep', 'SummarizationStep'))).toEqual(['classification']);
  });

  it('counts any of a step’s states as the step having run', () => {
    const started = plan({ startStep: 'assessment' });

    expect(skippedStepIds(started, entered('ShardExtractionStep'))).toEqual(['beginning', 'classification']);
  });

  it('does not count a step the run has not reached yet as skipped', () => {
    // A run from the beginning that is still at OCR: later steps are pending, not skipped.
    expect(skippedStepIds(plan(), entered('CheckRunPlan', 'OCRStep'))).toEqual([]);
  });

  it('is the steps before policy rule validation for a run that follows a completed review', () => {
    const afterReview = plan({
      kind: 'review_completed',
      startStep: 'apply_review_changes',
      startAt: 'apply_review_changes',
      reviewed: true,
    });

    expect(skippedStepIds(afterReview, entered('CheckRunPlan', 'ApplyReviewChangesStep'))).toEqual([
      'beginning',
      'classification',
      'extraction',
      'assessment',
    ]);
  });

  it.each(DISPLAY_STEPS.map((step) => [step.id, step.before] as const))(
    'reads a start at the display step %s as a start at %s, the run step the catalogue puts it before',
    (displayStep, before) => {
      const started = plan({ startStep: displayStep, startAt: displayStep });
      const ids = stepsForMode('pipeline').map((step) => step.id);

      expect(ids).toContain(before);
      expect(skippedStepIds(started, entered())).toEqual(ids.slice(0, ids.indexOf(before)));
      expect(skippedStepIds(started, entered())).toEqual(skippedStepIds(plan({ startStep: before }), entered()));
    },
  );

  it('holds no display step id of its own: they come from the step catalogue', () => {
    const source = readFileSync(join(__dirname, '..', 'skipped-steps.ts'), 'utf-8');

    expect(DISPLAY_STEPS.length).toBeGreaterThan(0);
    for (const step of DISPLAY_STEPS) expect(source).not.toContain(`'${step.id}'`);
  });

  it('is every step for a run that records a rejected review', () => {
    const rejected = plan({ kind: 'review_rejected', startStep: 'rejected', startAt: 'rejected' });

    expect(skippedStepIds(rejected, entered('CheckRunPlan'))).toEqual([
      'beginning',
      'classification',
      'extraction',
      'assessment',
      'policy_rule_validation',
      'summarization',
      'evaluation',
    ]);
  });

  it('uses the steps of BDA mode for a BDA run', () => {
    const started = plan({ mode: 'bda', startStep: 'summarization', skipPolicyRuleValidation: true });

    expect(skippedStepIds(started, entered('SummarizationStep'))).toEqual(['beginning', 'policy_rule_validation']);
    // The assembly state is the BDA beginning: a run that entered it did not skip it.
    expect(skippedStepIds(started, entered('AssembleExistingDocumentStep', 'SummarizationStep'))).toEqual(['policy_rule_validation']);
  });

  it('reads a start it does not know as the beginning', () => {
    expect(skippedStepIds(plan({ startStep: 'something_new' }), entered())).toEqual([]);
  });
});

describe('where a skipped step is shown', () => {
  const names = (entries: { name: string }[]) => entries.map((entry) => entry.name);
  const steps = (...stateNames: string[]) => stateNames.map((name) => ({ name }));
  const make = (stepId: string) => ({ name: `skipped:${stepId}` });

  it('knows which step a state belongs to', () => {
    expect(stepIndexOfState('OCRStep', 'pipeline')).toBe(0);
    expect(stepIndexOfState('ExtractionMergeStep', 'pipeline')).toBe(2);
    expect(stepIndexOfState('SummarizationStep', 'bda')).toBe(2);
    expect(stepIndexOfState('CheckRunPlan', 'pipeline')).toBe(-1);
    // A Pipeline state is of no step of BDA mode.
    expect(stepIndexOfState('OCRStep', 'bda')).toBe(-1);
  });

  it('puts the steps before the start ahead of the first step that ran, in step order', () => {
    const started = plan({ startStep: 'policy_rule_validation' });
    const shown = withSkippedSteps(
      steps(
        'CheckRunPlan',
        'StartAtPolicyRuleValidation',
        'PolicyClassificationStep',
        'RoutingStep',
        'SummarizationStep',
        'EvaluationStep',
      ),
      started,
      make,
    );

    expect(names(shown)).toEqual([
      'CheckRunPlan',
      'StartAtPolicyRuleValidation',
      'skipped:beginning',
      'skipped:classification',
      'skipped:extraction',
      'skipped:assessment',
      'PolicyClassificationStep',
      'RoutingStep',
      'SummarizationStep',
      'EvaluationStep',
    ]);
  });

  it('puts a skipped step between the steps around it, and at the end when no later step ran', () => {
    const skipping = plan({ skipSummarization: true });
    expect(names(withSkippedSteps(steps('OCRStep', 'RoutingStep', 'SkipSummarization', 'EvaluationStep'), skipping, make))).toEqual([
      'OCRStep',
      'RoutingStep',
      'SkipSummarization',
      'skipped:summarization',
      'EvaluationStep',
    ]);

    const both = plan({ skipSummarization: true, skipEvaluation: true });
    expect(names(withSkippedSteps(steps('OCRStep', 'RoutingStep'), both, make))).toEqual([
      'OCRStep',
      'RoutingStep',
      'skipped:summarization',
      'skipped:evaluation',
    ]);
  });

  it('returns the steps as they are when nothing was skipped', () => {
    const entries = steps('CheckRunPlan', 'OCRStep');

    expect(withSkippedSteps(entries, plan(), make)).toEqual(entries);
  });
});
