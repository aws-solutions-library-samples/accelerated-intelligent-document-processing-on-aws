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
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import PlannedSteps from '../run-plan/PlannedSteps';
import { getProcessChangesPlan } from '../run-plan/run-plan-api';
import usePlannedRun from '../run-plan/use-planned-run';
import { sharedLabelMessages } from './labels';

const messages = defineMessages<{
  cancel: NoMessageValues;
  confirm: NoMessageValues;
  reprocessHeader: NoMessageValues;
  versionHeader: NoMessageValues;
  versionBody: { readonly processChanges: string };
  regroupIntro: NoMessageValues;
  sectionLine: { readonly count: number; readonly id: string; readonly class: string; readonly pages: string };
  willDo: NoMessageValues;
  effectValues: NoMessageValues;
  effectClasses: NoMessageValues;
  effectReview: NoMessageValues;
  effectRules: NoMessageValues;
  effectOthers: NoMessageValues;
  reprocess: NoMessageValues;
}>({
  cancel: {
    id: 'common.processChangesModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the confirmation before Process Changes or Save and Re-extract: closes it and does nothing.',
  },
  confirm: {
    id: 'common.processChangesModal.confirm',
    defaultMessage: 'Confirm & Process',
    description:
      "Button of the Confirm Reprocessing dialog, shown before the edit page's Process Changes on a document and before Save and Re-extract in the document page's Edit Page Grouping: runs it.",
  },
  reprocessHeader: {
    id: 'common.processChangesModal.reprocessHeader',
    defaultMessage: 'Confirm Reprocessing',
    description:
      "Title of the dialog shown before the edit page's Process Changes on a document and before Save and Re-extract in the document page's Edit Page Grouping, which says what reprocessing will do.",
  },
  versionHeader: {
    id: 'common.processChangesModal.versionHeader',
    defaultMessage: 'Open a new version and save ground truth',
    description:
      "Title of the dialog shown before Process Changes on a test-set item's edit page while the test set has no draft version open.",
  },
  versionBody: {
    id: 'common.processChangesModal.versionBody',
    defaultMessage:
      '{processChanges} opens the next version of this test set (the current version stays as it is) and writes your labels as reviewed ground truth.',
    description:
      "Text of the dialog shown before Process Changes on a test-set item's edit page while the test set has no draft version open. {processChanges} is the name of the Process Changes button.",
  },
  regroupIntro: {
    id: 'common.processChangesModal.regroupIntro',
    defaultMessage: 'You are about to save the new page grouping, then extract these sections again:',
    description:
      'Text of the Confirm Reprocessing dialog before Save and Re-extract in Edit Page Grouping, above the list of the sections it re-extracts.',
  },
  sectionLine: {
    id: 'common.processChangesModal.sectionLine',
    defaultMessage: '{count, plural, one {Section {id}: {class}, page {pages}} other {Section {id}: {class}, pages {pages}}}',
    description:
      "A section in the list of the Confirm Reprocessing dialog before Save and Re-extract in Edit Page Grouping. {id} is the section's number; {class} is its document class, shown as is (empty for a section with no class); {count} is how many pages it has; {pages} lists their numbers, separated by commas.",
  },
  willDo: {
    id: 'common.processChangesModal.willDo',
    defaultMessage: 'This will:',
    description:
      'Text of the Confirm Reprocessing dialog before Save and Re-extract in Edit Page Grouping, above the list of what it does.',
  },
  effectValues: {
    id: 'common.processChangesModal.effect.values',
    defaultMessage: 'Replace their extracted values, including any corrected by hand',
    description:
      "What Save and Re-extract in Edit Page Grouping does to the sections it re-extracts, in the Confirm Reprocessing dialog's list.",
  },
  effectClasses: {
    id: 'common.processChangesModal.effect.classes',
    defaultMessage: 'Record their classes as confirmed (class confidence 1.0)',
    description:
      "What Save and Re-extract in Edit Page Grouping does to the sections it re-extracts, in the Confirm Reprocessing dialog's list. 1.0 is the confidence recorded for their classes.",
  },
  effectReview: {
    id: 'common.processChangesModal.effect.review',
    defaultMessage: "Mark this document's pending review as completed, if it has one",
    description: "What Save and Re-extract in Edit Page Grouping does to the document's review, in the Confirm Reprocessing dialog's list.",
  },
  effectRules: {
    id: 'common.processChangesModal.effect.rules',
    defaultMessage: "Clear the document's Policy Rule Validation results",
    description:
      "What Save and Re-extract in Edit Page Grouping does to the document's Policy Rule Validation results, in the Confirm Reprocessing dialog's list.",
  },
  effectOthers: {
    id: 'common.processChangesModal.effect.others',
    defaultMessage: "Leave the other sections' values as they are; OCR and classification are not repeated",
    description: "What Save and Re-extract in Edit Page Grouping leaves alone, in the Confirm Reprocessing dialog's list.",
  },
  reprocess: {
    id: 'common.processChangesModal.reprocess',
    defaultMessage:
      'The document is processed again with your saved changes. The steps before the one the run starts at are not repeated: what they produced is kept.',
    description:
      "Text of the Confirm Reprocessing dialog before the edit page's Process Changes on a document outside a review. The list of the processing steps the run will do follows it.",
  },
});

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
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" loading={busy} onClick={onConfirm}>
              {variant === 'version' ? (
                <FormattedMessage {...sharedLabelMessages.processChanges} />
              ) : (
                <FormattedMessage {...messages.confirm} />
              )}
            </Button>
          </SpaceBetween>
        </Box>
      }
      header={intl.formatMessage(variant === 'version' ? messages.versionHeader : messages.reprocessHeader)}
    >
      {variant === 'version' ? (
        <Box>
          <FormattedMessage {...messages.versionBody} values={{ processChanges: intl.formatMessage(sharedLabelMessages.processChanges) }} />
        </Box>
      ) : reextractSections ? (
        <SpaceBetween size="s">
          <Box>
            <FormattedMessage {...messages.regroupIntro} />
          </Box>
          <ul>
            {reextractSections.map((s) => (
              <li key={s.sectionId}>
                <FormattedMessage
                  {...messages.sectionLine}
                  values={{ count: s.pageIds.length, id: s.sectionId, class: s.documentClass ?? '', pages: s.pageIds.join(', ') }}
                />
              </li>
            ))}
          </ul>
          <Box>
            <FormattedMessage {...messages.willDo} />
          </Box>
          <ul>
            <li>
              <FormattedMessage {...messages.effectValues} />
            </li>
            <li>
              <FormattedMessage {...messages.effectClasses} />
            </li>
            <li>
              <FormattedMessage {...messages.effectReview} />
            </li>
            <li>
              <FormattedMessage {...messages.effectRules} />
            </li>
            <li>
              <FormattedMessage {...messages.effectOthers} />
            </li>
          </ul>
        </SpaceBetween>
      ) : (
        <SpaceBetween size="m">
          <Box>
            <FormattedMessage {...messages.reprocess} />
          </Box>
          {/* Confirming does not wait for this: a failed read shows as a warning with Retry. */}
          <PlannedSteps plan={planned.plan} loading={planned.loading} error={planned.error} onRetry={planned.retry} />
        </SpaceBetween>
      )}
    </Modal>
  );
};

export default ProcessChangesConfirmModal;
