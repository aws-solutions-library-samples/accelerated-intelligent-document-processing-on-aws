// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button, ContentLayout, Header } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import InboxMessagesTable from './InboxMessagesTable';
import InboxPreferences from './InboxPreferences';

const messages = defineMessages<{ title: NoMessageValues; description: NoMessageValues; settings: NoMessageValues }>({
  title: {
    id: 'inbox.page.title',
    defaultMessage: 'Inbox',
    description: 'Heading of the Inbox page (Home › Inbox), which lists the messages the platform sends the user.',
  },
  description: {
    id: 'inbox.page.description',
    defaultMessage: 'Messages addressed to you or your groups.',
    description: 'Line under the heading of the Inbox page.',
  },
  settings: {
    id: 'inbox.page.settings',
    defaultMessage: 'Inbox settings',
    description: 'Tooltip and screen-reader label of the settings (gear) button in the Inbox heading, which opens the Inbox settings.',
  },
});

/** The Inbox: messages, with the settings dialog (message types) behind the settings button. */
const InboxPage = (): React.JSX.Element => {
  const intl = useIntl();
  const [searchParams, setSearchParams] = useSearchParams();
  // `?tab=preferences` links (older bookmarks, the bell) open the settings dialog.
  const settingsOpen = searchParams.get('tab') === 'preferences';

  const setSettingsOpen = (open: boolean) => {
    const next = new URLSearchParams(searchParams);
    if (open) next.set('tab', 'preferences');
    else next.delete('tab');
    setSearchParams(next, { replace: true });
  };

  const settingsLabel = intl.formatMessage(messages.settings);

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={intl.formatMessage(messages.description)}
          actions={
            <span title={settingsLabel}>
              <Button iconName="settings" variant="normal" ariaLabel={settingsLabel} onClick={() => setSettingsOpen(true)} />
            </span>
          }
        >
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <InboxMessagesTable />
      <InboxPreferences visible={settingsOpen} onDismiss={() => setSettingsOpen(false)} />
    </ContentLayout>
  );
};

export default InboxPage;
