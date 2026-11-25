// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The pipeline steps a run can start at, and the display steps a run does that it can
// neither start at nor skip, typed. run-steps.json is generated from
// idp_common.run_plan.steps by scripts/generate_run_step_catalog.py (never edited by
// hand), and scripts/tests/test_run_step_catalog.py holds it to that list. What a step
// is called on screen is the picker's (StartStepPicker.tsx): nothing here is shown.
import catalog from './run-steps.json';

export type ProcessingMode = 'pipeline' | 'bda';

export interface RunStep {
  id: string;
  /** A required step cannot be skipped. */
  required: boolean;
  /** The processing modes that have the step. */
  modes: readonly ProcessingMode[];
  /** The state machine states that do the step's work, by mode. */
  states: Readonly<Partial<Record<ProcessingMode, readonly string[]>>>;
}

/** Every step, in pipeline order. */
export const RUN_STEPS: readonly RunStep[] = catalog.steps.map((step) => ({
  id: step.id,
  required: step.required,
  modes: step.modes as ProcessingMode[],
  states: step.states as RunStep['states'],
}));

/** A step a run does that it can neither start at nor skip. */
export interface DisplayStep {
  id: string;
  /** The run step it comes right before, in both processing modes. */
  before: string;
  /** Only the run that follows a completed review does the step. */
  reviewCompletedOnly: boolean;
}

/** The display steps, in pipeline order. */
export const DISPLAY_STEPS: readonly DisplayStep[] = catalog.display_steps.map((step) => ({
  id: step.id,
  before: step.before,
  reviewCompletedOnly: step.review_completed_only,
}));

/** The start value that means "the first step with no output". */
export const RESUME: string = catalog.resume;

/** The steps a processing mode has, in pipeline order. */
export const stepsForMode = (mode: ProcessingMode): RunStep[] => RUN_STEPS.filter((step) => step.modes.includes(mode));

/** The states that do a step's work in a mode; none when the mode has no such step. */
export const stepStates = (stepId: string, mode: ProcessingMode): readonly string[] =>
  RUN_STEPS.find((step) => step.id === stepId)?.states[mode] ?? [];
