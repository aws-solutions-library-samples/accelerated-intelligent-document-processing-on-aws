// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// What a run will do, shown before it starts: the step it starts at, every step of the
// document's processing mode in order with whether the run does it, skips it or finds
// it switched off, and the sections it extracts again with the cause of each. The
// server decides all of it (a planned run); this component only shows it, with a
// loading state and, when the read failed, a warning with Retry. The dialog that
// finishes a review and the Process Changes confirmation both show it. The step names
// are the picker's (StartStepPicker.tsx), the web UI's one set of step labels.
import React from 'react';
import { Alert, Box, Button, List, SpaceBetween, StatusIndicator, type StatusIndicatorProps } from '@cloudscape-design/components';
import { defineMessages, useIntl, type MessageDescriptor, type NoMessageValues } from 'react-intl';

import type { PlannedRun } from '../../graphql/generated/operation-types';
import { stepMessageIn } from './StartStepPicker';

const messages = defineMessages<{
  heading: NoMessageValues;
  loading: NoMessageValues;
  loadFailed: NoMessageValues;
  retry: NoMessageValues;
  startsAt: { readonly step: string };
  listLabel: NoMessageValues;
  sections: { readonly sections: string };
  section: { readonly sectionId: string; readonly cause: string };
}>({
  heading: {
    id: 'runPlan.plannedSteps.heading',
    defaultMessage: 'Steps of the run',
    description:
      'Heading above the list of the processing steps a run will do, shown in a dialog before the run starts (finishing a review, or Process Changes).',
  },
  loading: {
    id: 'runPlan.plannedSteps.loading',
    defaultMessage: 'Loading the steps of the run',
    description: 'Shown while the list of the processing steps a run will do is being read from the server.',
  },
  loadFailed: {
    id: 'runPlan.plannedSteps.loadFailed',
    defaultMessage: 'The steps of the run could not be loaded',
    description:
      'Heading of the warning shown in place of the list of the processing steps a run will do, when reading it failed. The reason follows it.',
  },
  retry: {
    id: 'runPlan.plannedSteps.retry',
    defaultMessage: 'Retry',
    description: 'Button in the warning shown when the steps of a run could not be loaded: read them again.',
  },
  startsAt: {
    id: 'runPlan.plannedSteps.startsAt',
    defaultMessage: 'Starts at {step}',
    description:
      'Line above the list of the processing steps a run will do. {step} is the name of the step the run starts at, for example Policy Rule Validation.',
  },
  listLabel: {
    id: 'runPlan.plannedSteps.listLabel',
    defaultMessage: 'The processing steps in order, each with whether the run does it',
    description: 'Accessible name of the list of the processing steps a run will do. Read by screen readers; not shown.',
  },
  sections: {
    id: 'runPlan.plannedSteps.sections',
    defaultMessage: 'Sections extracted again: {sections}',
    description:
      'Line under the list of the processing steps a run will do. {sections} is a list of section ids, each with why it is extracted again, for example "2 (pages changed), 4 (added)".',
  },
  section: {
    id: 'runPlan.plannedSteps.section',
    defaultMessage: '{sectionId} ({cause})',
    description:
      'One entry of the list of sections a run extracts again. {sectionId} is the id of the section, for example 2; {cause} is why it is extracted again, for example "pages changed".',
  },
});

/** What a run does with a step: one message per state the server answers (the state in lower camel case). */
const stateMessages = defineMessages<{
  run: NoMessageValues;
  skipped: NoMessageValues;
  notEnabled: NoMessageValues;
}>({
  run: {
    id: 'runPlan.plannedSteps.stateRun',
    defaultMessage: 'Runs',
    description: 'State of a processing step in the list of the steps a run will do: the run does this step.',
  },
  skipped: {
    id: 'runPlan.plannedSteps.stateSkipped',
    defaultMessage: 'Skipped',
    description: 'State of a processing step in the list of the steps a run will do: the run does not do this step.',
  },
  notEnabled: {
    id: 'runPlan.plannedSteps.stateNotEnabled',
    defaultMessage: 'Not enabled',
    description:
      'State of a processing step in the list of the steps a run will do: the run reaches this step, and the configuration has it switched off.',
  },
});

/** Why a run extracts a section again: one message per cause the server answers (the cause in lower camel case). */
const causeMessages = defineMessages<{
  added: NoMessageValues;
  pagesChanged: NoMessageValues;
  noResult: NoMessageValues;
}>({
  added: {
    id: 'runPlan.plannedSteps.causeAdded',
    defaultMessage: 'added',
    description: 'Why a run extracts a section again, in parentheses after the section id: the section was added to the document.',
  },
  pagesChanged: {
    id: 'runPlan.plannedSteps.causePagesChanged',
    defaultMessage: 'pages changed',
    description: 'Why a run extracts a section again, in parentheses after the section id: the pages of the section were changed.',
  },
  noResult: {
    id: 'runPlan.plannedSteps.causeNoResult',
    defaultMessage: 'no result',
    description: 'Why a run extracts a section again, in parentheses after the section id: the section has no extracted values.',
  },
});

/** `not_enabled` or `pages_changed` as the key its message is declared under. */
const messageKey = (value: string): string => value.replace(/_([a-z0-9])/g, (_match, letter: string) => letter.toUpperCase());

/** How each state shows. A state this screen has no entry for shows as the server sent it. */
const STATE_INDICATORS: Record<string, StatusIndicatorProps.Type> = {
  run: 'success',
  skipped: 'stopped',
  notEnabled: 'not-started',
};

interface PlannedStepsProps {
  /** What the run will do, as the server answered; null while it is not known. */
  plan: PlannedRun | null;
  loading: boolean;
  /** Why the plan could not be read, in words. */
  error: string | null;
  onRetry: () => void;
}

const PlannedSteps = ({ plan, loading, error, onRetry }: PlannedStepsProps): React.JSX.Element | null => {
  const intl = useIntl();

  /** A message of a group declared above, by the value the server sent; the value itself when there is none. */
  const worded = (group: Record<string, MessageDescriptor>, value: string): string => {
    const message = group[messageKey(value)];
    return message ? intl.formatMessage(message) : value;
  };

  let body: React.ReactNode;
  if (loading) {
    body = <StatusIndicator type="loading">{intl.formatMessage(messages.loading)}</StatusIndicator>;
  } else if (error) {
    body = (
      <Alert
        type="warning"
        header={intl.formatMessage(messages.loadFailed)}
        action={<Button onClick={onRetry}>{intl.formatMessage(messages.retry)}</Button>}
      >
        {error}
      </Alert>
    );
  } else if (plan) {
    /** A step's name in the plan's processing mode; the server's own label for a step this screen has no message for. */
    const stepLabel = (stepId: string): string => {
      const message = stepMessageIn(stepId, plan.mode);
      return message ? intl.formatMessage(message) : (plan.steps.find((step) => step.id === stepId)?.label ?? stepId);
    };
    const sections = plan.sections.map((section) =>
      intl.formatMessage(messages.section, { sectionId: section.sectionId, cause: worded(causeMessages, section.cause) }),
    );
    body = (
      <>
        <Box>{intl.formatMessage(messages.startsAt, { step: stepLabel(plan.startStep) })}</Box>
        <List
          tagOverride="ol"
          ariaLabel={intl.formatMessage(messages.listLabel)}
          disablePaddings
          disableItemPaddings
          items={plan.steps}
          renderItem={(step) => ({
            id: step.id,
            content: stepLabel(step.id),
            actions: (
              <StatusIndicator type={STATE_INDICATORS[messageKey(step.state)] ?? 'info'}>
                {worded(stateMessages, step.state)}
              </StatusIndicator>
            ),
          })}
        />
        {sections.length > 0 && (
          <Box>{intl.formatMessage(messages.sections, { sections: intl.formatList(sections, { type: 'unit' }) })}</Box>
        )}
      </>
    );
  } else {
    return null;
  }

  return (
    <SpaceBetween size="xs">
      <Box variant="awsui-key-label">{intl.formatMessage(messages.heading)}</Box>
      {body}
    </SpaceBetween>
  );
};

export default PlannedSteps;
