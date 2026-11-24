// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The confirmation before Process Changes. The 'reprocess' variant ("Confirm
// Reprocessing") says what reprocessing will do: shared by the edit page's Process Changes
// on a direct edit and the document page's Edit Page Grouping › Save and Re-extract, which
// lists the sections it re-extracts. The 'version' variant is Test Studio's Process Changes
// while no draft version of the set is open: it opens one and writes reviewed ground truth.
import React from 'react';
import { Alert, Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

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
  noStructure: { readonly strong: MessageTag };
  useWhen: NoMessageValues;
  useEvaluate: NoMessageValues;
  useSummary: NoMessageValues;
  skipBda: NoMessageValues;
  skipPipeline: NoMessageValues;
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
    defaultMessage: "Clear the document's rule validation results",
    description:
      "What Save and Re-extract in Edit Page Grouping does to the document's rule validation results, in the Confirm Reprocessing dialog's list.",
  },
  effectOthers: {
    id: 'common.processChangesModal.effect.others',
    defaultMessage: "Leave the other sections' values as they are; OCR and classification are not repeated",
    description: "What Save and Re-extract in Edit Page Grouping leaves alone, in the Confirm Reprocessing dialog's list.",
  },
  noStructure: {
    id: 'common.processChangesModal.noStructure',
    defaultMessage: 'No section structure changes detected. This will trigger <strong>evaluation and summarization</strong> reprocessing.',
    description:
      "Text of the Confirm Reprocessing dialog before the edit page's Process Changes on a document. The <strong> tags mark the words shown in bold.",
  },
  useWhen: {
    id: 'common.processChangesModal.useWhen',
    defaultMessage: 'Use this when you have edited extraction data (predictions or baseline) and want to:',
    description:
      "Text of the Confirm Reprocessing dialog before the edit page's Process Changes on a document, above a list of two reasons.",
  },
  useEvaluate: {
    id: 'common.processChangesModal.useEvaluate',
    defaultMessage: 'Re-run evaluation to compare predictions against baseline',
    description:
      "A reason to use Process Changes, in the list of the Confirm Reprocessing dialog before the edit page's Process Changes on a document.",
  },
  useSummary: {
    id: 'common.processChangesModal.useSummary',
    defaultMessage: 'Update the document summary report',
    description:
      "A reason to use Process Changes, in the list of the Confirm Reprocessing dialog before the edit page's Process Changes on a document.",
  },
  skipBda: {
    id: 'common.processChangesModal.skipBda',
    defaultMessage: 'BDA (Bedrock Data Automation) processing will be automatically skipped since existing data is preserved.',
    description:
      "Note in the Confirm Reprocessing dialog before the edit page's Process Changes on a document processed with BDA (Bedrock Data Automation).",
  },
  skipPipeline: {
    id: 'common.processChangesModal.skipPipeline',
    defaultMessage: 'OCR, Classification, Extraction, and Assessment steps will be automatically skipped since existing data is preserved.',
    description:
      "Note in the Confirm Reprocessing dialog before the edit page's Process Changes on a document not processed with BDA (Bedrock Data Automation).",
  },
});

/** Renders a `<strong>...</strong>` element value: the words shown in bold. */
const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

interface ProcessChangesConfirmModalProps {
  visible: boolean;
  /** What is confirmed: reprocessing the document (default), or opening the test set's next version. */
  variant?: 'reprocess' | 'version';
  /** Save and Re-extract: the sections saved, then extracted again. */
  reextractSections?: { sectionId: string; documentClass?: string | null; pageIds: number[] }[];
  /** BDA mode, whose processing is skipped rather than the Pipeline mode steps. */
  bdaMode?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

const ProcessChangesConfirmModal = ({
  visible,
  variant = 'reprocess',
  reextractSections,
  bdaMode = false,
  busy = false,
  onConfirm,
  onCancel,
}: ProcessChangesConfirmModalProps): React.JSX.Element => {
  const intl = useIntl();
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
      <SpaceBetween size="s">
        {variant === 'version' ? (
          <Box>
            <FormattedMessage
              {...messages.versionBody}
              values={{ processChanges: intl.formatMessage(sharedLabelMessages.processChanges) }}
            />
          </Box>
        ) : reextractSections ? (
          <>
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
          </>
        ) : (
          <>
            <Box>
              <FormattedMessage {...messages.noStructure} values={{ strong }} />
            </Box>
            <Box>
              <FormattedMessage {...messages.useWhen} />
            </Box>
            <ul>
              <li>
                <FormattedMessage {...messages.useEvaluate} />
              </li>
              <li>
                <FormattedMessage {...messages.useSummary} />
              </li>
            </ul>
            <Alert type="info">
              {bdaMode ? <FormattedMessage {...messages.skipBda} /> : <FormattedMessage {...messages.skipPipeline} />}
            </Alert>
          </>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default ProcessChangesConfirmModal;
