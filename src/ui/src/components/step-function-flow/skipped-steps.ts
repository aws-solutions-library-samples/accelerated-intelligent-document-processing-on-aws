// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The steps a run did not do because its run plan said so. A run can start at a later
// step (a reprocess from Extraction, a run after a completed review) and can skip an
// optional one; the workflow then never enters the states that do those steps, so they
// are absent from the execution history. The step viewer shows each of them as one
// "Skipped" node, so the flow still reads from the first step to the last.
//
// The plan is the `runPlan` block of the execution input. A run with none (one started
// before run plans existed) skipped nothing that this can tell. No strings.
import { parseStepFunctionPayload } from '../../graphql/awsjson-parsers';
import { DISPLAY_STEPS, stepStates, stepsForMode, type ProcessingMode } from '../run-plan/run-steps';

/** The status a skipped step is shown with; no state of the workflow has it. */
export const SKIPPED_STATUS = 'SKIPPED';

/** A run that records a rejected review does no step at all. */
const REJECTED = 'rejected';

/**
 * The first run step a run does. A run can start at a display step of the step
 * catalogue (the run after a completed review starts at Apply Review Changes), which is
 * no run step itself: the run step it comes right before is then the first one done.
 */
const firstRunStep = (startStep: string): string => DISPLAY_STEPS.find((step) => step.id === startStep)?.before ?? startStep;

/** What the viewer reads of an execution's run plan. */
export interface ExecutionRunPlan {
  mode: ProcessingMode;
  /** The step the run started at: a run step or a display step of the step catalogue, or `rejected`. */
  startStep: string;
  /** The steps the plan skips besides those before the start. */
  skipped: ReadonlySet<string>;
}

/** `policy_rule_validation` as `skipPolicyRuleValidation`: the execution input's flag for a step. */
const skipFlag = (stepId: string): string =>
  `skip${stepId
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join('')}`;

/** The run plan of an execution input (AWSJSON text or an object); null when it has none this can read. */
export const readRunPlan = (input: unknown): ExecutionRunPlan | null => {
  const parsed = (typeof input === 'string' ? parseStepFunctionPayload(input) : input) as { runPlan?: unknown } | null;
  const plan = parsed && typeof parsed === 'object' ? parsed.runPlan : null;
  if (!plan || typeof plan !== 'object') return null;
  const { mode, startStep } = plan as { mode?: unknown; startStep?: unknown };
  if ((mode !== 'pipeline' && mode !== 'bda') || typeof startStep !== 'string' || !startStep) return null;
  const flags = plan as Record<string, unknown>;
  const skipped = new Set(
    stepsForMode(mode)
      .map((step) => step.id)
      .filter((id) => flags[skipFlag(id)] === true),
  );
  return { mode, startStep, skipped };
};

/** The position, among the steps of the mode, of the first step the run does. */
const firstStepIndex = (plan: ExecutionRunPlan): number => {
  const ids = stepsForMode(plan.mode).map((step) => step.id);
  if (plan.startStep === REJECTED) return ids.length;
  // A start this viewer does not know gives -1: no step is before it, so none is skipped for it.
  return ids.indexOf(firstRunStep(plan.startStep));
};

/**
 * The ids of the steps the plan skipped, in step order: those before the start and those
 * the plan names, none of whose states the execution entered.
 */
export const skippedStepIds = (plan: ExecutionRunPlan, enteredStates: ReadonlySet<string>): string[] => {
  const first = firstStepIndex(plan);
  return stepsForMode(plan.mode)
    .filter((step, index) => index < first || plan.skipped.has(step.id))
    .filter((step) => !stepStates(step.id, plan.mode).some((state) => enteredStates.has(state)))
    .map((step) => step.id);
};

/** The position of the step a state belongs to, among the steps of the mode; -1 for a state of no step. */
export const stepIndexOfState = (state: string, mode: ProcessingMode): number =>
  stepsForMode(mode).findIndex((step) => stepStates(step.id, mode).includes(state));

/**
 * `entries` (the execution's steps, in the order they are shown) with one `make(stepId)`
 * entry per skipped step, each placed before the first entry of a later step, so the
 * list reads in step order.
 */
export const withSkippedSteps = <T extends { name: string }>(
  entries: readonly T[],
  plan: ExecutionRunPlan,
  make: (stepId: string) => T,
): T[] => {
  const skipped = skippedStepIds(plan, new Set(entries.map((entry) => entry.name)));
  if (skipped.length === 0) return [...entries];
  const order = stepsForMode(plan.mode).map((step) => step.id);
  const result = [...entries];
  for (const stepId of skipped) {
    const position = order.indexOf(stepId);
    const at = result.findIndex((entry) => stepIndexOfState(entry.name, plan.mode) > position);
    result.splice(at < 0 ? result.length : at, 0, make(stepId));
  }
  return result;
};
