// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Send to review: put finished production documents into the Default review queue,
// with a required reason (one document from Document Detail, or a selection from the
// Document List).
import React, { useState } from 'react';
import { Alert, Box, SpaceBetween } from '@cloudscape-design/components';

import ReasonModal from '../common/ReasonModal';
import { SEND_TO_REVIEW_REASONS } from '../common/review-status';
import { getErrorMessage } from '../../utils/errorUtils';
import { sendToReview } from './bulk-review-api';
import type { SendToReviewResult } from '../../graphql/generated/schema-types';

interface SendToReviewModalProps {
  objectKeys: string[];
  onDismiss: () => void;
  onDone: (result: SendToReviewResult) => void;
}

const SendToReviewModal = ({ objectKeys, onDismiss, onDone }: SendToReviewModalProps): React.JSX.Element => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async (reason: string, note: string | undefined) => {
    setBusy(true);
    setError(null);
    try {
      onDone(await sendToReview(objectKeys, reason, note));
    } catch (e) {
      setError(`Send to review failed: ${getErrorMessage(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const count = objectKeys.length;
  return (
    <ReasonModal
      visible
      header="Send to review"
      reasons={SEND_TO_REVIEW_REASONS}
      confirmLabel="Send to review"
      loading={busy}
      dismissDisabled={busy}
      confirmDisabledReason={count === 0 ? 'Select finished documents that are not in review' : null}
      onDismiss={onDismiss}
      onConfirm={(reason, note) => void confirm(reason, note)}
    >
      <SpaceBetween size="s">
        {error && <Alert type="error">{error}</Alert>}
        <Box>
          {count === 1 ? 'This document' : `These ${count} documents`} will be added to the Default review queue. Reviewers see your reason
          with the item; completing the review reprocesses the document with their edits.
        </Box>
      </SpaceBetween>
    </ReasonModal>
  );
};

export default SendToReviewModal;
