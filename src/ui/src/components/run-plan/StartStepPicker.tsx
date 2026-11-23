// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Choosing the step a run starts at. The steps come from run-steps.ts (the generated
// catalogue), filtered to the document's processing mode; which of them a document can
// start at is the server's answer (getReprocessOptions), passed in as `options`.
import React from 'react';
import { FormField, RadioGroup, type RadioGroupProps } from '@cloudscape-design/components';
import { defineMessages, useIntl, type MessageDescriptor, type NoMessageValues } from 'react-intl';

import { RESUME, RUN_STEPS, stepsForMode, type ProcessingMode } from './run-steps';

const messages = defineMessages<{
  label: NoMessageValues;
  description: NoMessageValues;
  pipelineBeginning: NoMessageValues;
  unavailable: NoMessageValues;
  otherReason: { readonly code: string };
  blockedFor: { readonly count: number; readonly total: number; readonly why: string };
}>({
  label: {
    id: 'runPlan.picker.label',
    defaultMessage: 'Start at',
    description: 'Label of the group of options that chooses the pipeline step a reprocess starts at.',
  },
  description: {
    id: 'runPlan.picker.description',
    defaultMessage: 'The run does this step and every later step again. What the earlier steps produced is kept.',
    description: 'Help text under the label of the start step options.',
  },
  pipelineBeginning: {
    id: 'runPlan.stepPipeline.beginning',
    defaultMessage: 'OCR (beginning)',
    description: 'Start step option for a document processed in Pipeline mode: the run starts at OCR, the first step.',
  },
  unavailable: {
    id: 'runPlan.picker.unavailable',
    defaultMessage: 'Not available for this document.',
    description: 'Shown under a start step option that cannot be chosen, when the server gave no reason.',
  },
  otherReason: {
    id: 'runPlan.reason.other',
    defaultMessage: 'Not available ({code}).',
    description:
      'Shown under a start step option that cannot be chosen, for a reason code this screen has no sentence for. {code} is the code.',
  },
  blockedFor: {
    id: 'runPlan.picker.blockedFor',
    defaultMessage: 'Not available for {count, number} of {total, number} documents. {why}',
    description:
      'Shown under a start step option that cannot be chosen for a selection of several documents. {count} of the {total} selected documents cannot start there; {why} is a sentence with the reason of the first of them.',
  },
});

/**
 * One message per step id (the id in lower camel case), and one for resume. The display
 * steps (Apply Review Changes, Review Routing) have one too: a run does them, and no
 * picker offers them as a start.
 */
const stepMessages = defineMessages<{
  beginning: NoMessageValues;
  classification: NoMessageValues;
  extraction: NoMessageValues;
  assessment: NoMessageValues;
  applyReviewChanges: NoMessageValues;
  policyRuleValidation: NoMessageValues;
  reviewRouting: NoMessageValues;
  summarization: NoMessageValues;
  evaluation: NoMessageValues;
  resume: NoMessageValues;
}>({
  beginning: {
    id: 'runPlan.step.beginning',
    defaultMessage: 'Beginning',
    description: 'Start step option: the run starts at the first step. Shown for a document processed in BDA mode.',
  },
  classification: {
    id: 'runPlan.step.classification',
    defaultMessage: 'Classification',
    description: 'Start step option: the run starts at classification.',
  },
  extraction: {
    id: 'runPlan.step.extraction',
    defaultMessage: 'Extraction',
    description: 'Start step option: the run starts at extraction.',
  },
  assessment: {
    id: 'runPlan.step.assessment',
    defaultMessage: 'Assessment',
    description: 'Start step option: the run starts at assessment (confidence scoring).',
  },
  applyReviewChanges: {
    id: 'runPlan.step.applyReviewChanges',
    defaultMessage: 'Apply Review Changes',
    description:
      "Name of a processing step: the run that follows a completed review applies the reviewer's corrections to the document. Shown in lists of the steps of a run; a run cannot be told to start at it.",
  },
  policyRuleValidation: {
    id: 'runPlan.step.policyRuleValidation',
    defaultMessage: 'Policy Rule Validation',
    description: 'Start step option: the run starts at policy rule validation.',
  },
  reviewRouting: {
    id: 'runPlan.step.reviewRouting',
    defaultMessage: 'Review Routing',
    description:
      'Name of a processing step: the run decides whether the document goes to a review queue. Shown in lists of the steps of a run; a run cannot be told to start at it.',
  },
  summarization: {
    id: 'runPlan.step.summarization',
    defaultMessage: 'Summarization',
    description: 'Start step option: the run starts at summarization.',
  },
  evaluation: {
    id: 'runPlan.step.evaluation',
    defaultMessage: 'Evaluation',
    description: 'Start step option: the run starts at evaluation.',
  },
  resume: {
    id: 'runPlan.step.resume',
    defaultMessage: 'Resume from the failed step',
    description: 'Start step option for a failed document: the run starts at the first step that has no output.',
  },
});

/** One message per code the server refuses a start step with (the code in lower camel case). */
const reasonMessages = defineMessages<{
  malwareGate: NoMessageValues;
  runInProgress: NoMessageValues;
  notFailed: NoMessageValues;
  unknownStep: NoMessageValues;
  stepNotInMode: NoMessageValues;
  outputMissing: NoMessageValues;
  otherProfileBeginningOnly: NoMessageValues;
  revisionHasPreprocessingHook: NoMessageValues;
  revisionChangesMode: NoMessageValues;
}>({
  malwareGate: {
    id: 'runPlan.reason.malwareGate',
    defaultMessage: 'The document was held or refused by the malware scan.',
    description: 'Why a start step cannot be chosen: the document is still being scanned for malware, or the scan refused it.',
  },
  runInProgress: {
    id: 'runPlan.reason.runInProgress',
    defaultMessage: 'The document is being processed. Wait for the run to finish, or abort it.',
    description: 'Why a start step cannot be chosen: a run of the document is in progress.',
  },
  notFailed: {
    id: 'runPlan.reason.notFailed',
    defaultMessage: 'The document did not fail, so there is no failed run to resume.',
    description: 'Why "Resume from the failed step" cannot be chosen: the document is not in the failed status.',
  },
  unknownStep: {
    id: 'runPlan.reason.unknownStep',
    defaultMessage: 'This is not a step a run can start at.',
    description: 'Why a start step cannot be chosen: the server does not know the step.',
  },
  stepNotInMode: {
    id: 'runPlan.reason.stepNotInMode',
    defaultMessage: "This step does not exist in the document's processing mode.",
    description: 'Why a start step cannot be chosen: the step is not part of BDA mode or Pipeline mode, whichever the document uses.',
  },
  outputMissing: {
    id: 'runPlan.reason.outputMissing',
    defaultMessage: 'The document does not have the output of the earlier steps.',
    description: 'Why a start step cannot be chosen: a step before it has not produced what this step reads.',
  },
  otherProfileBeginningOnly: {
    id: 'runPlan.reason.otherProfileBeginningOnly',
    defaultMessage: 'Under a different configuration profile, a run starts at the beginning.',
    description:
      'Why a later start step cannot be chosen: the reprocess names a configuration profile other than the one the document was processed with.',
  },
  revisionHasPreprocessingHook: {
    id: 'runPlan.reason.revisionHasPreprocessingHook',
    defaultMessage: 'The chosen revision has a preprocessing hook, which a later start would skip.',
    description: 'Why a later start step cannot be chosen: the chosen configuration revision runs a hook before the first step.',
  },
  revisionChangesMode: {
    id: 'runPlan.reason.revisionChangesMode',
    defaultMessage: 'The chosen revision changes the processing mode, so the run starts at the beginning.',
    description: 'Why a later start step cannot be chosen: the chosen configuration revision switches between BDA mode and Pipeline mode.',
  },
});

/** `policy_rule_validation` or `OUTPUT_MISSING` as the key its message is declared under. */
const messageKey = (value: string): string => value.toLowerCase().replace(/_([a-z0-9])/g, (_match, letter: string) => letter.toUpperCase());

export const stepMessage = (stepId: string): MessageDescriptor | undefined =>
  (stepMessages as Record<string, MessageDescriptor>)[messageKey(stepId)];

/** The mode whose first step is named for what it does there: OCR. */
const PIPELINE: ProcessingMode = 'pipeline';

/**
 * The message that names a step among the steps of a processing mode: the picker's
 * options, and a list of the steps a run does. Pipeline mode's first step is OCR; every
 * other step has one name in every mode.
 */
export const stepMessageIn = (stepId: string, mode: string): MessageDescriptor | undefined =>
  mode === PIPELINE && stepId === RUN_STEPS[0]?.id ? messages.pipelineBeginning : stepMessage(stepId);

export const reasonMessage = (code: string): MessageDescriptor | undefined =>
  (reasonMessages as Record<string, MessageDescriptor>)[messageKey(code)];

/** Whether a document can start at a step, as getReprocessOptions answers it. */
export interface StartStepOption {
  id: string;
  available: boolean;
  /** The code that says why the step is unavailable. */
  reason?: string | null;
  /** For a selection of documents: how many of them cannot start at the step. */
  blocked?: { count: number; total: number };
}

interface StartStepPickerProps {
  /** The processing mode of the documents; it decides which steps exist. */
  mode: ProcessingMode;
  value: string;
  onChange: (step: string) => void;
  /** What the server allows. Without it every step of the mode can be chosen. */
  options?: readonly StartStepOption[];
  /** Offer "Resume from the failed step" as well. */
  includeResume?: boolean;
  disabled?: boolean;
}

const StartStepPicker = ({
  mode,
  value,
  onChange,
  options,
  includeResume = false,
  disabled = false,
}: StartStepPickerProps): React.JSX.Element => {
  const intl = useIntl();

  const stepLabel = (stepId: string): string => {
    const message = stepMessageIn(stepId, mode);
    return message ? intl.formatMessage(message) : stepId;
  };

  const unavailableText = (code?: string | null): string => {
    if (!code) return intl.formatMessage(messages.unavailable);
    const message = reasonMessage(code);
    return message ? intl.formatMessage(message) : intl.formatMessage(messages.otherReason, { code });
  };

  const item = (stepId: string): RadioGroupProps.RadioButtonDefinition => {
    const option = options?.find((candidate) => candidate.id === stepId);
    // With an answer from the server, a step it does not list cannot be chosen.
    const available = options ? (option?.available ?? false) : true;
    const why = unavailableText(option?.reason);
    // Of several documents, say how many are in the way before the reason of the first.
    const blocked = option?.blocked && option.blocked.total > 1 ? option.blocked : null;
    return {
      value: stepId,
      label: stepLabel(stepId),
      description: available ? undefined : blocked ? intl.formatMessage(messages.blockedFor, { ...blocked, why }) : why,
      disabled: disabled || !available,
    };
  };

  const items = stepsForMode(mode).map((step) => item(step.id));
  if (includeResume) items.push(item(RESUME));

  return (
    <FormField label={intl.formatMessage(messages.label)} description={intl.formatMessage(messages.description)}>
      <RadioGroup value={value} items={items} onChange={({ detail }) => onChange(detail.value)} />
    </FormField>
  );
};

export default StartStepPicker;
