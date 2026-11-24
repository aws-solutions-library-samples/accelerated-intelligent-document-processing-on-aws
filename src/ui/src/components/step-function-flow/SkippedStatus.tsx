// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// How the step viewer shows the status of a step the run plan skipped: in its diagram,
// its timeline and its details panel.
import React from 'react';
import { StatusIndicator } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

const messages = defineMessages<{ skipped: NoMessageValues }>({
  skipped: {
    id: 'documents.processingFlow.status.skipped',
    defaultMessage: 'Skipped',
    description:
      'Status of a processing step, in the document processing flow viewer, that the run did not do because its run plan skipped it.',
  },
});

const SkippedStatus = (): React.JSX.Element => {
  const intl = useIntl();
  return <StatusIndicator type="stopped">{intl.formatMessage(messages.skipped)}</StatusIndicator>;
};

export default SkippedStatus;
