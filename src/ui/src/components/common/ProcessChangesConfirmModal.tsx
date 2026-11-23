// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The confirmation before Process Changes. The 'reprocess' variant ("Confirm
// Reprocessing") says what reprocessing will do: shared by the edit page's Process Changes
// on a direct edit, which shows the steps the server says the run will do
// (getProcessChangesPlan, read while the dialog is open: the step the run starts at, each
// step with whether it runs, and the sections extracted again), and the document page's
// Edit Page Grouping › Save and Re-extract, which lists the sections it re-extracts. Where
// a run starts is the server's decision: this dialog holds no step of its own. When the
// steps cannot be read the dialog says so and offers Retry, and the changes can still be
// processed. The 'version' variant is Test Studio's Process Changes while no draft version
// of the set is open: it opens one and writes reviewed ground truth.
import React, { useCallback } from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import PlannedSteps from '../run-plan/PlannedSteps';
import { getProcessChangesPlan } from '../run-plan/run-plan-api';
import usePlannedRun from '../run-plan/use-planned-run';
import { PROCESS_CHANGES_LABEL } from './labels';

const messages = defineMessages<{
  reprocess: NoMessageValues;
  clearsPolicyRuleValidation: NoMessageValues;
}>({
  reprocess: {
    id: 'common.processChangesConfirm.reprocess',
    defaultMessage:
      'The document is processed again with your saved changes. The steps before the one the run starts at are not repeated: what they produced is kept.',
    description:
      'Text of the Confirm Reprocessing dialog of Process Changes on a document outside a review. The list of the processing steps the run will do follows it.',
  },
  clearsPolicyRuleValidation: {
    id: 'common.processChangesConfirm.clearsPolicyRuleValidation',
    defaultMessage: "Clear the document's Policy Rule Validation results",
    description: 'One of the effects listed in the Confirm Reprocessing dialog of Save and Re-extract.',
  },
});

export const VERSION_CONFIRM_HEADER = 'Open a new version and save ground truth';
export const VERSION_CONFIRM_BODY = `${PROCESS_CHANGES_LABEL} opens the next version of this test set (the current version stays as it is) and writes your labels as reviewed ground truth.`;

interface ProcessChangesConfirmModalProps {
  visible: boolean;
  /** What is confirmed: reprocessing the document (default), or opening the test set's next version. */
  variant?: 'reprocess' | 'version';
  /** Save and Re-extract: the sections saved, then extracted again. */
  reextractSections?: { sectionId: string; documentClass?: string | null; pageIds: number[] }[];
  /** Process Changes on a document: the document, whose planned steps the dialog reads and shows. */
  objectKey?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

const ProcessChangesConfirmModal = ({
  visible,
  variant = 'reprocess',
  reextractSections,
  objectKey,
  busy = false,
  onConfirm,
  onCancel,
}: ProcessChangesConfirmModalProps): React.JSX.Element => {
  const intl = useIntl();
  // What Process Changes will run, read while the dialog asks about it. Save and
  // Re-extract and the new test-set version say what they do themselves.
  const showsPlan = variant === 'reprocess' && !reextractSections;
  const planKey = showsPlan ? objectKey : undefined;
  const loadPlan = useCallback(() => getProcessChangesPlan(planKey ?? ''), [planKey]);
  const planned = usePlannedRun(loadPlan, visible && !!planKey);

  return (
    <Modal
      onDismiss={onCancel}
      visible={visible}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onCancel}>
              Cancel
            </Button>
            <Button variant="primary" loading={busy} onClick={onConfirm}>
              {variant === 'version' ? PROCESS_CHANGES_LABEL : 'Confirm & Process'}
            </Button>
          </SpaceBetween>
        </Box>
      }
      header={variant === 'version' ? VERSION_CONFIRM_HEADER : 'Confirm Reprocessing'}
    >
      {variant === 'version' ? (
        <Box>{VERSION_CONFIRM_BODY}</Box>
      ) : reextractSections ? (
        <SpaceBetween size="s">
          <Box>You are about to save the new page grouping, then extract these sections again:</Box>
          <ul>
            {reextractSections.map((s) => (
              <li key={s.sectionId}>
                Section {s.sectionId}: {s.documentClass}, pages {s.pageIds.join(', ')}
              </li>
            ))}
          </ul>
          <Box>This will:</Box>
          <ul>
            <li>Replace their extracted values, including any corrected by hand</li>
            <li>Record their classes as confirmed (class confidence 1.0)</li>
            <li>Mark this document&apos;s pending review as completed, if it has one</li>
            <li>{intl.formatMessage(messages.clearsPolicyRuleValidation)}</li>
            <li>Leave the other sections&apos; values as they are; OCR and classification are not repeated</li>
          </ul>
        </SpaceBetween>
      ) : (
        <SpaceBetween size="m">
          <Box>{intl.formatMessage(messages.reprocess)}</Box>
          {/* Confirming does not wait for this: a failed read shows as a warning with Retry. */}
          <PlannedSteps plan={planned.plan} loading={planned.loading} error={planned.error} onRetry={planned.retry} />
        </SpaceBetween>
      )}
    </Modal>
  );
};

export default ProcessChangesConfirmModal;
