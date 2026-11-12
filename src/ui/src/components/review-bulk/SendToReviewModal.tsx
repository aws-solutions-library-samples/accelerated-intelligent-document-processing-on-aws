// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Send to review: put finished production documents into the Default review queue,
// with a required reason (one document from Document Detail, or a selection from the
// Document List).
import React, { useState } from 'react';
import { Alert, Box, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import ReasonModal from '../common/ReasonModal';
import { SEND_TO_REVIEW_REASONS } from '../common/review-status';
import { getErrorMessage } from '../../utils/errorUtils';
import { sendToReview } from './bulk-review-api';
import type { SendToReviewResult } from '../../graphql/generated/schema-types';

const messages = defineMessages<{
  failed: { readonly error: string };
  header: NoMessageValues;
  confirm: NoMessageValues;
  noSelection: NoMessageValues;
  body: { readonly count: number };
}>({
  failed: {
    id: 'reviewBulk.sendToReviewModal.failed',
    defaultMessage: 'Send to review failed: {error}',
    description: 'Error in the Send to review dialog when the request fails. {error} is the error message from the server.',
  },
  header: {
    id: 'reviewBulk.sendToReviewModal.header',
    defaultMessage: 'Send to review',
    description: 'Title of the dialog that puts finished documents into the Default review queue.',
  },
  confirm: {
    id: 'reviewBulk.sendToReviewModal.confirm',
    defaultMessage: 'Send to review',
    description: 'Confirm button of the Send to review dialog.',
  },
  noSelection: {
    id: 'reviewBulk.sendToReviewModal.noSelection',
    defaultMessage: 'Select finished documents that are not in review',
    description: 'Why the Send to review confirm button is disabled: no selected document can be sent.',
  },
  body: {
    id: 'reviewBulk.sendToReviewModal.body',
    defaultMessage:
      '{count, plural, one {This document} other {These # documents}} will be added to the Default review queue. Reviewers see your reason with the item; completing the review reprocesses the document with their edits.',
    description:
      'Explanation in the Send to review dialog. {count} is the number of documents being sent; "Default review queue" is the name of the queue.',
  },
});

interface SendToReviewModalProps {
  objectKeys: string[];
  onDismiss: () => void;
  onDone: (result: SendToReviewResult) => void;
}

const SendToReviewModal = ({ objectKeys, onDismiss, onDone }: SendToReviewModalProps): React.JSX.Element => {
  const intl = useIntl();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async (reason: string, note: string | undefined) => {
    setBusy(true);
    setError(null);
    try {
      onDone(await sendToReview(objectKeys, reason, note));
    } catch (e) {
      setError(intl.formatMessage(messages.failed, { error: getErrorMessage(e) }));
    } finally {
      setBusy(false);
    }
  };

  const count = objectKeys.length;
  return (
    <ReasonModal
      visible
      header={intl.formatMessage(messages.header)}
      reasons={SEND_TO_REVIEW_REASONS}
      confirmLabel={intl.formatMessage(messages.confirm)}
      loading={busy}
      dismissDisabled={busy}
      confirmDisabledReason={count === 0 ? intl.formatMessage(messages.noSelection) : null}
      onDismiss={onDismiss}
      onConfirm={(reason, note) => void confirm(reason, note)}
    >
      <SpaceBetween size="s">
        {error && <Alert type="error">{error}</Alert>}
        <Box>
          <FormattedMessage {...messages.body} values={{ count }} />
        </Box>
      </SpaceBetween>
    </ReasonModal>
  );
};

export default SendToReviewModal;
