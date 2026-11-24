// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The labels Reports › Processing Flow shows in more than one of its files: execution and step
// statuses, the Status, Duration, Started and Completed fields with N/A, and the badges and
// tooltip of a step. Also the wording of the Malware Scan step, which the viewer builds without
// the UI language. FlowDiagram and StepDetails import them from here, so neither imports the
// viewer, which imports both.
import { defineMessages, type IntlShape, type NoMessageValues } from 'react-intl';

import { getMalwareScanView, type MalwareScanFields } from '../common/malware-scan';

/** What `stepName` and `stepOutput` read of a step, whichever of the three files' step types it is. */
interface NamedStep {
  name: string;
  output?: string;
  /** Set on the Malware Scan step only: the document's verdict, which the step is recognised and worded by. */
  malwareScan?: MalwareScanFields;
}

export const stepLabelMessages = defineMessages<{
  status: NoMessageValues;
  duration: NoMessageValues;
  started: NoMessageValues;
  completed: NoMessageValues;
  notAvailable: NoMessageValues;
  notEnabled: NoMessageValues;
  disabledTitle: NoMessageValues;
  iterations: { readonly count: number };
}>({
  status: {
    id: 'documents.processingFlow.field.status',
    defaultMessage: 'Status',
    description: 'Label of the status of the execution, a step or a map iteration in Reports › Processing Flow on the document page.',
  },
  duration: {
    id: 'documents.processingFlow.field.duration',
    defaultMessage: 'Duration',
    description:
      'Label of how long the execution, a step or a map iteration ran, in minutes and seconds, in Reports › Processing Flow on the document page.',
  },
  started: {
    id: 'documents.processingFlow.field.started',
    defaultMessage: 'Started',
    description: 'Label of the start time of the execution, a step or a map iteration in Reports › Processing Flow on the document page.',
  },
  completed: {
    id: 'documents.processingFlow.field.completed',
    defaultMessage: 'Completed',
    description:
      'Label of the end time of the execution, a step or a map iteration in Reports › Processing Flow on the document page, shown once it has one.',
  },
  notAvailable: {
    id: 'documents.processingFlow.notAvailable',
    defaultMessage: 'N/A',
    description:
      'Shown in Reports › Processing Flow on the document page as the duration and the start time of the execution, a step or a map iteration that has no start time.',
  },
  notEnabled: {
    id: 'documents.processingFlow.notEnabled',
    defaultMessage: 'NOT ENABLED',
    description:
      'Badge on a step in the flow diagram and the Steps Timeline of Reports › Processing Flow on the document page, when the configuration the document was processed with turned the step off.',
  },
  disabledTitle: {
    id: 'documents.processingFlow.disabledTitle',
    defaultMessage: 'This step was disabled in configuration and performed no processing',
    description:
      'Tooltip of a step in the flow diagram and the Steps Timeline of Reports › Processing Flow on the document page, when the configuration the document was processed with turned the step off.',
  },
  iterations: {
    id: 'documents.processingFlow.iterations',
    defaultMessage: '{count, plural, one {# iteration} other {# iterations}}',
    description:
      'Badge on a Map step in the flow diagram and the Steps Timeline of Reports › Processing Flow on the document page. {count} is the number of iterations of the Map step. Map is a Step Functions state type.',
  },
});

const messages = defineMessages<{ malwareScanStep: NoMessageValues }>({
  malwareScanStep: {
    id: 'documents.processingFlow.malwareScanStep',
    defaultMessage: 'Malware Scan',
    description:
      'Name of the first step in Reports › Processing Flow on the document page: the malware scan of the uploaded file, which runs before the processing workflow starts.',
  },
});

// One label per status the processing flow shows, keyed by the value as the API returns it: the
// execution statuses Step Functions reports, the step statuses the execution resolver derives from
// the execution's history (RUNNING, SUCCEEDED, FAILED), and ERROR, which the resolver returns when
// it cannot read the execution. Its English is the value itself, so English shows the status as the
// API returns it; a translation may give a short readable label. A value the map does not hold
// shows as it is.
const stepStatusMessages = defineMessages<Record<string, NoMessageValues>>({
  SUCCEEDED: {
    id: 'documents.processingFlow.status.succeeded',
    defaultMessage: 'SUCCEEDED',
    description:
      'Status of the execution, a step or a map iteration in Reports › Processing Flow on the document page, in Step Functions terms: it finished successfully. English is the Step Functions value itself; translate it as a short status label.',
  },
  FAILED: {
    id: 'documents.processingFlow.status.failed',
    defaultMessage: 'FAILED',
    description:
      'Status of the execution, a step or a map iteration in Reports › Processing Flow on the document page, in Step Functions terms: it failed. English is the Step Functions value itself; translate it as a short status label.',
  },
  RUNNING: {
    id: 'documents.processingFlow.status.running',
    defaultMessage: 'RUNNING',
    description:
      'Status of the execution, a step or a map iteration in Reports › Processing Flow on the document page, in Step Functions terms: it is still running. English is the Step Functions value itself; translate it as a short status label.',
  },
  TIMED_OUT: {
    id: 'documents.processingFlow.status.timedOut',
    defaultMessage: 'TIMED_OUT',
    description:
      'Status of the execution in the Execution Overview of Reports › Processing Flow on the document page, as Step Functions reports it: the execution timed out. English is the Step Functions value itself; translate it as a short status label.',
  },
  ABORTED: {
    id: 'documents.processingFlow.status.aborted',
    defaultMessage: 'ABORTED',
    description:
      'Status of the execution in the Execution Overview of Reports › Processing Flow on the document page, as Step Functions reports it: the execution was stopped before it finished. English is the Step Functions value itself; translate it as a short status label.',
  },
  PENDING_REDRIVE: {
    id: 'documents.processingFlow.status.pendingRedrive',
    defaultMessage: 'PENDING_REDRIVE',
    description:
      'Status of the execution in the Execution Overview of Reports › Processing Flow on the document page, as Step Functions reports it: a redrive of the execution, a rerun from where it failed, is waiting to start. English is the Step Functions value itself; translate it as a short status label.',
  },
  ERROR: {
    id: 'documents.processingFlow.status.error',
    defaultMessage: 'ERROR',
    description:
      "Status in the Execution Overview of Reports › Processing Flow on the document page when the execution's details could not be read. It is the application's own value, not a Step Functions status. English is the value itself; translate it as a short status label.",
  },
});

/**
 * A status of an execution or a step as its label: the map's message for a value it holds, looked
 * up as stored, and the value as it is for one it does not.
 */
export const stepStatusLabel = (status: string, intl: IntlShape): string =>
  Object.hasOwn(stepStatusMessages, status) ? intl.formatMessage(stepStatusMessages[status]) : status;

/**
 * The name a step shows: for the Malware Scan step, recognised by its verdict, the step's name in
 * the UI's language; for any other step, its name as the execution reports it.
 */
export const stepName = (step: NamedStep, intl: IntlShape): string =>
  step.malwareScan ? intl.formatMessage(messages.malwareScanStep) : step.name;

/**
 * What a step's Output Data shows: for the Malware Scan step, the explanation of its verdict in the
 * UI's language; for any other step, its output as the execution reports it.
 */
export const stepOutput = (step: NamedStep, intl: IntlShape): string | undefined =>
  step.malwareScan ? getMalwareScanView(step.malwareScan, intl).detail : step.output;
