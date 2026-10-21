// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Box, Button, FormField, Modal, SpaceBetween, StatusIndicator, Toggle } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

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

export const PREFERENCES_EMPTY_TEXT =
  'No message types yet. Types appear here when a platform feature that sends Inbox messages is in use.';

const sameSet = (a: ReadonlySet<string>, b: ReadonlySet<string>): boolean => a.size === b.size && [...a].every((v) => b.has(v));

interface InboxPreferencesProps {
  visible: boolean;
  onDismiss: () => void;
}

/** Inbox settings: which message types you receive, in a dialog opened from the Inbox header. */
const InboxPreferences = ({ visible, onDismiss }: InboxPreferencesProps): React.JSX.Element => {
  // 1. Hooks
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
      setSuccessMessage('Inbox settings saved');
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
    if (loading) return <StatusIndicator type="loading">Loading preferences</StatusIndicator>;
    if (loadError) {
      return (
        <Alert type="error" header="Could not load preferences" action={<Button onClick={() => void load()}>Retry</Button>}>
          {loadError}
        </Alert>
      );
    }
    if (types.length === 0) {
      return (
        <Box textAlign="center" color="text-body-secondary">
          {PREFERENCES_EMPTY_TEXT}
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
              ariaLabel={`Receive ${t.displayName}`}
            >
              Receive
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
      header="Inbox settings"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" disabled={unchanged || saving || loading || !!loadError} loading={saving} onClick={() => void save()}>
              Save
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box variant="p" color="text-body-secondary">
          Turn off the message types you don&apos;t want. Muted types aren&apos;t delivered; turning one back on affects new messages only.
        </Box>
        {renderBody()}
      </SpaceBetween>
    </Modal>
  );
};

export default InboxPreferences;
