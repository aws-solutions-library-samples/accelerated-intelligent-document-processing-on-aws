// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Document List's "Add to test set" toolbar action. It only opens the dialog;
 * the dialog is Test Studio's own "From processed documents" one, so the two entry
 * points cannot drift. Enabled while at least one selected document has finished
 * processing, because nothing else can be added.
 */

import React from 'react';
import { Button } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import { isAddableStatus } from '../test-studio/AddProcessedDocumentsModal';

const messages = defineMessages<{ label: NoMessageValues; enabledHint: NoMessageValues; disabledHint: NoMessageValues }>({
  label: {
    id: 'documents.list.addToTestSet.label',
    defaultMessage: 'Add to test set',
    description: 'Document List toolbar button that adds the selected documents to a Test Studio test set.',
  },
  enabledHint: {
    id: 'documents.list.addToTestSet.enabledHint',
    defaultMessage: 'Add the selected documents to a test set',
    description: 'Tooltip of the Add to test set button when it can be used.',
  },
  disabledHint: {
    id: 'documents.list.addToTestSet.disabledHint',
    defaultMessage: 'Select at least one document that has finished processing',
    description: 'Tooltip of the Add to test set button when no selected document can be added.',
  },
});

interface AddToTestSetButtonProps {
  selectedItems: readonly { objectStatus: string }[];
  onClick: () => void;
}

const AddToTestSetButton = ({ selectedItems, onClick }: AddToTestSetButtonProps): React.JSX.Element => {
  const intl = useIntl();
  const enabled = selectedItems.some((item) => isAddableStatus(item.objectStatus));

  return (
    <span title={intl.formatMessage(enabled ? messages.enabledHint : messages.disabledHint)}>
      <Button variant="normal" disabled={!enabled} onClick={onClick}>
        {intl.formatMessage(messages.label)}
      </Button>
    </span>
  );
};

export default AddToTestSetButton;
