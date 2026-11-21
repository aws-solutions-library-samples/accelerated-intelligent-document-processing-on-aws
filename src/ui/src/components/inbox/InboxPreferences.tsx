// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Box, Button, FormField, Modal, SpaceBetween, StatusIndicator, Toggle } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { generateClient } from '../../api/client-shim';
import useAppContext from '../../contexts/app';
import {
  getInboxPreferences as getInboxPreferencesQuery,
  updateInboxPreferences as updateInboxPreferencesMutation,
} from '../../graphql/generated';
import type { InboxMessageType } from '../../graphql/generated/operation-types';
import { describeApiError } from '../../hooks/utils/graphql-error';

const client = generateClient();
const logger = new ConsoleLogger('InboxPreferences');

const messages = defineMessages<{
  empty: NoMessageValues;
  saved: NoMessageValues;
  loading: NoMessageValues;
  loadFailed: NoMessageValues;
  retry: NoMessageValues;
  receiveAriaLabel: { readonly type: string };
  receive: NoMessageValues;
  header: NoMessageValues;
  cancel: NoMessageValues;
  save: NoMessageValues;
  intro: NoMessageValues;
}>({
  empty: {
    id: 'inbox.preferences.empty',
    defaultMessage: 'No message types yet. Types appear here when a platform feature that sends Inbox messages is in use.',
    description: 'Shown in the Inbox settings dialog when no message type exists yet.',
  },
  saved: {
    id: 'inbox.preferences.saved',
    defaultMessage: 'Inbox settings saved',
    description: 'Notice shown at the top of the page after the Inbox settings are saved.',
  },
  loading: {
    id: 'inbox.preferences.loading',
    defaultMessage: 'Loading preferences',
    description: 'Shown in the Inbox settings dialog while the message types and their settings load.',
  },
  loadFailed: {
    id: 'inbox.preferences.loadFailed',
    defaultMessage: 'Could not load preferences',
    description: 'Title of the error shown in the Inbox settings dialog when the settings could not be loaded.',
  },
  retry: {
    id: 'inbox.preferences.retry',
    defaultMessage: 'Retry',
    description: 'Button of the load error in the Inbox settings dialog that loads the settings again.',
  },
  receiveAriaLabel: {
    id: 'inbox.preferences.receiveAriaLabel',
    defaultMessage: 'Receive {type}',
    description:
      'Screen-reader label of the toggle that turns one Inbox message type on or off. {type} is the type name, as the platform writes it.',
  },
  receive: {
    id: 'inbox.preferences.receive',
    defaultMessage: 'Receive',
    description: 'Text of the toggle, under each message type in the Inbox settings dialog, that turns that type on or off.',
  },
  header: {
    id: 'inbox.preferences.header',
    defaultMessage: 'Inbox settings',
    description: 'Title of the dialog, opened from the Inbox heading, where the user chooses which message types they receive.',
  },
  cancel: {
    id: 'inbox.preferences.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the Inbox settings dialog without saving.',
  },
  save: {
    id: 'inbox.preferences.save',
    defaultMessage: 'Save',
    description: 'Button that saves the Inbox settings.',
  },
  intro: {
    id: 'inbox.preferences.intro',
    defaultMessage:
      "Turn off the message types you don't want. Muted types aren't delivered; turning one back on affects new messages only.",
    description: 'Introduction at the top of the Inbox settings dialog, above the list of message types.',
  },
});

/** The empty-state text in English, written once in `messages.empty`. */
export const PREFERENCES_EMPTY_TEXT = String(messages.empty.defaultMessage);

const sameSet = (a: ReadonlySet<string>, b: ReadonlySet<string>): boolean => a.size === b.size && [...a].every((v) => b.has(v));

interface InboxPreferencesProps {
  visible: boolean;
  onDismiss: () => void;
}

/** Inbox settings: which message types you receive, in a dialog opened from the Inbox header. */
const InboxPreferences = ({ visible, onDismiss }: InboxPreferencesProps): React.JSX.Element => {
  // 1. Hooks
  const intl = useIntl();
  const { setSuccessMessage, setErrorMessage } = useAppContext();
  const [types, setTypes] = useState<readonly InboxMessageType[]>([]);
  const [savedMuted, setSavedMuted] = useState<ReadonlySet<string>>(new Set());
  const [muted, setMuted] = useState<ReadonlySet<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = async (): Promise<void> => {
    setLoading(true);
    try {
      const result = await client.graphql({ query: getInboxPreferencesQuery });
      const prefs = result.data?.getInboxPreferences;
      const nextMuted = new Set(prefs?.mutedTypes ?? []);
      setTypes(prefs?.types ?? []);
      setSavedMuted(nextMuted);
      setMuted(nextMuted);
      setLoadError(null);
    } catch (err) {
      logger.error('getInboxPreferences failed', err);
      setLoadError(describeApiError(err, 'load Inbox preferences'));
    } finally {
      setLoading(false);
    }
  };

  // 2. Effects
  useEffect(() => {
    if (visible) void load();
  }, [visible]);

  // 3. Memos
  const unchanged = useMemo(() => sameSet(muted, savedMuted), [muted, savedMuted]);

  // 4. Handlers
  const setReceive = (typeId: string, receive: boolean) => {
    setMuted((prev) => {
      const next = new Set(prev);
      if (receive) next.delete(typeId);
      else next.add(typeId);
      return next;
    });
  };

  const save = async (): Promise<void> => {
    setSaving(true);
    try {
      const result = await client.graphql({ query: updateInboxPreferencesMutation, variables: { mutedTypes: [...muted] } });
      const prefs = result.data?.updateInboxPreferences;
      const nextMuted = new Set(prefs?.mutedTypes ?? muted);
      if (prefs?.types) setTypes(prefs.types);
      setSavedMuted(nextMuted);
      setMuted(nextMuted);
      setSuccessMessage(intl.formatMessage(messages.saved));
      onDismiss();
    } catch (err) {
      logger.error('updateInboxPreferences failed', err);
      setErrorMessage(describeApiError(err, 'save Inbox preferences'));
    } finally {
      setSaving(false);
    }
  };

  // 5. Render
  const renderBody = (): React.ReactNode => {
    if (loading) {
      return (
        <StatusIndicator type="loading">
          <FormattedMessage {...messages.loading} />
        </StatusIndicator>
      );
    }
    if (loadError) {
      return (
        <Alert
          type="error"
          header={intl.formatMessage(messages.loadFailed)}
          action={
            <Button onClick={() => void load()}>
              <FormattedMessage {...messages.retry} />
            </Button>
          }
        >
          {loadError}
        </Alert>
      );
    }
    if (types.length === 0) {
      return (
        <Box textAlign="center" color="text-body-secondary">
          <FormattedMessage {...messages.empty} />
        </Box>
      );
    }
    return (
      <SpaceBetween size="l">
        {types.map((t) => (
          <FormField key={t.id} label={t.displayName} description={t.description}>
            <Toggle
              checked={!muted.has(t.id)}
              onChange={({ detail }) => setReceive(t.id, detail.checked)}
              ariaLabel={intl.formatMessage(messages.receiveAriaLabel, { type: t.displayName })}
            >
              <FormattedMessage {...messages.receive} />
            </Toggle>
          </FormField>
        ))}
      </SpaceBetween>
    );
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={intl.formatMessage(messages.header)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" disabled={unchanged || saving || loading || !!loadError} loading={saving} onClick={() => void save()}>
              <FormattedMessage {...messages.save} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box variant="p" color="text-body-secondary">
          <FormattedMessage {...messages.intro} />
        </Box>
        {renderBody()}
      </SpaceBetween>
    </Modal>
  );
};

export default InboxPreferences;
