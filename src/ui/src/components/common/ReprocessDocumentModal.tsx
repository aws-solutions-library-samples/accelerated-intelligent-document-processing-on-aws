// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect, useRef } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { Box, Modal, SpaceBetween, Button, Select, FormField, Alert } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';
import { ConsoleLogger } from 'aws-amplify/utils';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import ConfigRevisionSelector from './ConfigRevisionSelector';
import useConfiguration from '../../hooks/use-configuration';
import { isBdaModeConfig } from '../../utils/processing-mode';

const logger = new ConsoleLogger('ReprocessDocumentModal');

const messages = defineMessages<{
  title: { readonly count: number };
  confirm: { readonly count: number };
  cancel: NoMessageValues;
  reprocess: NoMessageValues;
  bdaNote: { readonly strong: MessageTag };
  profileLabel: NoMessageValues;
  profileDescription: NoMessageValues;
  profileLoading: NoMessageValues;
  profilePlaceholder: NoMessageValues;
  revisionDescription: NoMessageValues;
  documentsList: { readonly count: number };
}>({
  title: {
    id: 'documents.reprocessModal.title',
    defaultMessage: '{count, plural, =0 {Reprocess document} one {Reprocess document} other {Reprocess # documents}}',
    description: 'Title of the dialog that reprocesses documents. {count} is the number of documents selected.',
  },
  confirm: {
    id: 'documents.reprocessModal.confirm',
    defaultMessage: 'Are you sure you want to reprocess {count, plural, =0 {this document} one {this document} other {# documents}}?',
    description: 'Question at the top of the Reprocess dialog. {count} is the number of documents selected.',
  },
  cancel: {
    id: 'documents.reprocessModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the Reprocess dialog without reprocessing.',
  },
  reprocess: {
    id: 'documents.reprocessModal.reprocess',
    defaultMessage: 'Reprocess',
    description: 'Confirm button of the Reprocess dialog.',
  },
  bdaNote: {
    id: 'documents.reprocessModal.bdaNote',
    defaultMessage:
      '<strong>NOTE:</strong> To ensure that BDA project blueprints are aligned with your selected configuration profile, be sure to execute "Sync To BDA" for your configuration profile from the View/Edit Configuration page.',
    description:
      'Reminder in the Reprocess dialog for a profile in BDA mode; "Sync To BDA" names a button and "View/Edit Configuration" a page.',
  },
  profileLabel: {
    id: 'documents.reprocessModal.profile.label',
    defaultMessage: 'Configuration Profile',
    description: 'Label of the selector of the configuration profile the documents are reprocessed with.',
  },
  profileDescription: {
    id: 'documents.reprocessModal.profile.description',
    defaultMessage: 'Select which configuration profile to use for reprocessing these documents',
    description: 'Help text under the Configuration Profile label in the Reprocess dialog.',
  },
  profileLoading: {
    id: 'documents.reprocessModal.profile.loading',
    defaultMessage: 'Loading profiles...',
    description: 'Placeholder and loading text of the configuration profile selector while the profiles load.',
  },
  profilePlaceholder: {
    id: 'documents.reprocessModal.profile.placeholder',
    defaultMessage: 'Select configuration profile',
    description: 'Placeholder of the configuration profile selector when no profile is selected.',
  },
  revisionDescription: {
    id: 'documents.reprocessModal.revisionDescription',
    defaultMessage: 'Defaults to the profile’s current configuration. Pick an earlier revision to reproduce what an earlier run produced.',
    description: 'Help text under the Configuration revision selector in the Reprocess dialog.',
  },
  documentsList: {
    id: 'documents.reprocessModal.documentsList',
    defaultMessage:
      'This will trigger workflow reprocessing for the following {count, plural, =0 {document} one {document} other {documents}}:',
    description: 'Introduces the list of documents in the Reprocess dialog. {count} is the number of documents selected.',
  },
});

/** Renders a `<strong>...</strong>` element value: the lead-in of the BDA reminder. */
const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

interface ReprocessDocumentItem {
  objectKey: string;
}

interface ReprocessDocumentModalProps {
  visible: boolean;
  onDismiss: () => void;
  onConfirm: (versionName?: string, revision?: number) => void;
  selectedItems?: readonly ReprocessDocumentItem[];
  isLoading?: boolean;
}

const ReprocessDocumentModal = ({
  visible,
  onDismiss,
  onConfirm,
  selectedItems = [],
  isLoading = false,
}: ReprocessDocumentModalProps): React.JSX.Element => {
  const intl = useIntl();
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option | null>(null);
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  const { versions, getVersionOptions } = useConfigurationVersions();
  // BDA mode is a property of the selected configuration profile (`use_bda`), not of
  // the stack: every stack deploys the unified pattern. `useConfiguration` keeps the
  // previous profile's configuration while a new one loads and after a failed read,
  // so the answer is used only once a load for the selected profile has finished.
  const configVersionName = visible ? ((selectedVersion?.value as string | undefined) ?? null) : null;
  const { mergedConfig: selectedConfig, loading: configLoading, error: configError } = useConfiguration(configVersionName);
  const loadingFor = useRef<string | null>(null);
  const [configLoadedFor, setConfigLoadedFor] = useState<string | null>(null);
  useEffect(() => {
    if (configLoading) {
      loadingFor.current = configVersionName;
    } else if (!configError) {
      setConfigLoadedFor(loadingFor.current);
    }
  }, [configLoading, configError, configVersionName]);
  const isBdaProfile =
    configVersionName !== null && configLoadedFor === configVersionName && !configError && isBdaModeConfig(selectedConfig);

  // Reset selected version when modal closes so it picks up the correct version next time
  useEffect(() => {
    if (!visible) {
      setSelectedVersion(null);
      setSelectedRevision(null);
    }
  }, [visible]);

  // Set default version when modal opens: prefer document's configVersion, then active, then first scoped
  useEffect(() => {
    if (visible && versions.length > 0 && !selectedVersion) {
      const versionOptions = getVersionOptions();

      // Try to use the document's current ConfigVersion first
      if (selectedItems.length > 0) {
        const docVersion = ((selectedItems[0] as unknown as Record<string, unknown>).configVersion ||
          (selectedItems[0] as unknown as Record<string, unknown>).ConfigVersion) as string | undefined;
        if (docVersion) {
          const docVersionOption = versionOptions.find((option) => option.value === docVersion);
          if (docVersionOption) {
            setSelectedVersion(docVersionOption);
            return;
          }
        }
      }

      // Fallback: active version
      const activeVersion = versions.find((v) => v.isActive);
      if (activeVersion) {
        const activeVersionOption = versionOptions.find((option) => option.value === activeVersion.versionName);
        if (activeVersionOption) {
          setSelectedVersion(activeVersionOption);
          return;
        }
      }

      // Fallback: first available (scoped) version
      if (versionOptions.length > 0) {
        setSelectedVersion(versionOptions[0]);
      }
    }
  }, [visible, versions, selectedVersion, selectedItems, getVersionOptions]);

  const count = selectedItems.length;

  const handleConfirm = () => {
    logger.debug('Reprocessing documents', selectedItems, 'with version', selectedVersion?.value, 'r', selectedRevision);
    onConfirm(selectedVersion?.value, selectedRevision ?? undefined);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={isLoading ? undefined : onDismiss}
      header={intl.formatMessage(messages.title, { count })}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" onClick={handleConfirm} loading={isLoading} disabled={!selectedVersion}>
              <FormattedMessage {...messages.reprocess} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <p>
          <FormattedMessage {...messages.confirm} values={{ count }} />
        </p>

        {isBdaProfile && (
          <Alert type="info">
            <FormattedMessage {...messages.bdaNote} values={{ strong }} />
          </Alert>
        )}

        <FormField label={intl.formatMessage(messages.profileLabel)} description={intl.formatMessage(messages.profileDescription)}>
          <Select
            selectedOption={selectedVersion}
            onChange={({ detail }) => setSelectedVersion(detail.selectedOption)}
            options={getVersionOptions()}
            placeholder={intl.formatMessage(versions.length === 0 ? messages.profileLoading : messages.profilePlaceholder)}
            disabled={isLoading || versions.length === 0}
            loadingText={intl.formatMessage(messages.profileLoading)}
          />
        </FormField>

        <ConfigRevisionSelector
          profileName={selectedVersion?.value}
          value={selectedRevision}
          onChange={setSelectedRevision}
          description={intl.formatMessage(messages.revisionDescription)}
          disabled={isLoading}
        />

        <p>
          <FormattedMessage {...messages.documentsList} values={{ count }} />
        </p>
        <ul>
          {selectedItems.map((item) => (
            <li key={item.objectKey}>{item.objectKey}</li>
          ))}
        </ul>
      </SpaceBetween>
    </Modal>
  );
};

export default ReprocessDocumentModal;
