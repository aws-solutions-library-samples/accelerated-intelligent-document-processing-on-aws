// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A page's state before its content can be shown: a spinner while it loads, or an Alert
// (a missing or unreadable document) with Retry where the read can be tried again. The
// edit page and the document page share it, so both read the same.
import React from 'react';
import { Alert, Box, Button, Spinner } from '@cloudscape-design/components';
import type { AlertProps } from '@cloudscape-design/components';

export type LoadStateValue =
  | { kind: 'loading'; text: string }
  | { kind: 'failure'; type: AlertProps.Type; header: string; body: React.ReactNode; retry?: () => void };

const LoadState = ({ state }: { state: LoadStateValue }): React.JSX.Element => {
  if (state.kind === 'loading') {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" /> {state.text}
      </Box>
    );
  }
  const { type, header, body, retry } = state;
  return (
    <Alert type={type} header={header} action={retry && <Button onClick={retry}>Retry</Button>}>
      {body}
    </Alert>
  );
};

export default LoadState;
