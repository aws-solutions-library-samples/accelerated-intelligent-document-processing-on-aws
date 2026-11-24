// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The server answers, per document, which steps a reprocess can start at
// (getReprocessOptions). A dialog that reprocesses several documents with one choice
// needs the steps every one of them can start at: this combines the answers into the
// options of one start-step picker. No strings.
import type { ReprocessDocumentResult, ReprocessOption } from '../../graphql/generated/operation-types';
import { RESUME, stepsForMode, type ProcessingMode } from './run-steps';
import type { StartStepOption } from './StartStepPicker';

/** The step a reprocess starts at unless another is chosen. */
export const DEFAULT_START_STEP = 'beginning';
/** The status of a document a run was queued for (run_plan's QUEUED). */
export const RUN_QUEUED = 'QUEUED';
/** Why a step a document's mode does not have cannot be chosen (the eligibility code). */
export const STEP_NOT_IN_MODE = 'STEP_NOT_IN_MODE';
/** The status of a document that failed, the only one a run can resume. */
export const FAILED_STATUS = 'FAILED';

export interface CombinedStartOptions {
  /** The mode whose steps the picker lists: Pipeline mode's when any document is in it. */
  mode: ProcessingMode;
  options: StartStepOption[];
}

const isBda = (mode: string): boolean => mode === 'bda';

/**
 * One option per step of the listed mode: available when every document can start
 * there. A step some documents cannot start at carries how many, and the first of
 * their reasons. A document the server did not answer for can start nowhere.
 *
 * `includeResume` adds "resume from the failed step", which the server does not list:
 * it is offered when every selected document failed.
 */
export const combineReprocessOptions = (
  objectKeys: readonly string[],
  answers: readonly ReprocessOption[],
  includeResume = false,
): CombinedStartOptions => {
  const byKey = new Map(answers.map((answer) => [answer.objectKey, answer]));
  const perDocument = objectKeys.map((objectKey) => byKey.get(objectKey) ?? null);
  // BDA mode's steps are a subset of Pipeline mode's, so the Pipeline list shows every
  // step any document has; a document that lacks one is counted against it below.
  const mode: ProcessingMode = perDocument.every((answer) => answer !== null && isBda(answer.mode)) ? 'bda' : 'pipeline';

  const options: StartStepOption[] = stepsForMode(mode).map((step) => {
    const blocking: (string | null)[] = [];
    for (const answer of perDocument) {
      const stepAnswer = answer?.steps.find((candidate) => candidate.id === step.id);
      if (!answer) blocking.push(null);
      else if (!stepAnswer) blocking.push(STEP_NOT_IN_MODE);
      else if (!stepAnswer.available) blocking.push(stepAnswer.reason ?? null);
    }
    return blocking.length === 0
      ? { id: step.id, available: true }
      : { id: step.id, available: false, reason: blocking[0], blocked: { count: blocking.length, total: perDocument.length } };
  });
  if (includeResume) options.push({ id: RESUME, available: true });
  return { mode, options };
};

/** Whether a start step can be chosen under the combined options. */
export const canStartAt = (options: readonly StartStepOption[], step: string): boolean =>
  options.some((option) => option.id === step && option.available);

/** The documents of a reprocess answer that no run was queued for. */
export const notQueued = (results: readonly ReprocessDocumentResult[]): ReprocessDocumentResult[] =>
  results.filter((result) => result.status !== RUN_QUEUED);
