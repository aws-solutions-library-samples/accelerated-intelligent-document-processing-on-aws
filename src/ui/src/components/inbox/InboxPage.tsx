// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button, ContentLayout, Header } from '@cloudscape-design/components';

import InboxMessagesTable from './InboxMessagesTable';
import InboxPreferences from './InboxPreferences';

/** The Inbox: messages, with the settings dialog (message types) behind the settings button. */
const InboxPage = (): React.JSX.Element => {
  const [searchParams, setSearchParams] = useSearchParams();
  // `?tab=preferences` links (older bookmarks, the bell) open the settings dialog.
  const settingsOpen = searchParams.get('tab') === 'preferences';

  const setSettingsOpen = (open: boolean) => {
    const next = new URLSearchParams(searchParams);
    if (open) next.set('tab', 'preferences');
    else next.delete('tab');
    setSearchParams(next, { replace: true });
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Messages addressed to you or your groups."
          actions={
            <span title="Inbox settings">
              <Button iconName="settings" variant="normal" ariaLabel="Inbox settings" onClick={() => setSettingsOpen(true)} />
            </span>
          }
        >
          Inbox
        </Header>
      }
    >
      <InboxMessagesTable />
      <InboxPreferences visible={settingsOpen} onDismiss={() => setSettingsOpen(false)} />
    </ContentLayout>
  );
};

export default InboxPage;
