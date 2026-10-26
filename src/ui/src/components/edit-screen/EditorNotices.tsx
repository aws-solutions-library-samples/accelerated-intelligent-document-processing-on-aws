// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The unified editor's notices as one alert, so the screen stays given over to the
// document: every info and warning notice in one Alert (a warning if any is one),
// with their actions together. An error keeps its own Alert, so it can't be missed.
import React from 'react';
import { Alert, Box, SpaceBetween } from '@cloudscape-design/components';

export interface EditorNotice {
  id: string;
  type: 'error' | 'warning' | 'info';
  content: React.ReactNode;
  /** A button the notice offers (Take over, Start editing, Review in queue). */
  action?: React.ReactNode;
  /** An error the reader can dismiss. */
  onDismiss?: () => void;
}

const EditorNotices = ({ notices }: { notices: readonly (EditorNotice | false | null | undefined)[] }): React.JSX.Element | null => {
  const shown = notices.filter((n): n is EditorNotice => !!n);
  const errors = shown.filter((n) => n.type === 'error');
  const others = shown.filter((n) => n.type !== 'error');
  if (shown.length === 0) return null;
  const actions = others.filter((n) => n.action);
  return (
    <SpaceBetween size="xs">
      {errors.map((n) => (
        <Alert key={n.id} type="error" action={n.action} dismissible={!!n.onDismiss} onDismiss={n.onDismiss}>
          {n.content}
        </Alert>
      ))}
      {others.length > 0 && (
        <Alert
          type={others.some((n) => n.type === 'warning') ? 'warning' : 'info'}
          action={
            actions.length > 0 ? (
              <SpaceBetween direction="horizontal" size="xs">
                {actions.map((n) => (
                  <React.Fragment key={n.id}>{n.action}</React.Fragment>
                ))}
              </SpaceBetween>
            ) : undefined
          }
        >
          {others.length === 1 ? (
            others[0].content
          ) : (
            <SpaceBetween size="xxs">
              {others.map((n) => (
                <Box key={n.id} data-notice={n.id}>
                  {n.content}
                </Box>
              ))}
            </SpaceBetween>
          )}
        </Alert>
      )}
    </SpaceBetween>
  );
};

export default EditorNotices;
