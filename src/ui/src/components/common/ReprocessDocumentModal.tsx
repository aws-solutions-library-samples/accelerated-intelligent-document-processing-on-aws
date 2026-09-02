// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { Box, Modal, SpaceBetween, Button, Select, FormField, Alert } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import ConfigRevisionSelector from './ConfigRevisionSelector';
import useSettingsContext from '../../contexts/settings';

const logger = new ConsoleLogger('ReprocessDocumentModal');

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
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option | null>(null);
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  const { versions, getVersionOptions } = useConfigurationVersions();
  const { settings } = useSettingsContext();

  // Helper function to check if Pattern-1 is selected
  const isPattern1 = ((settings as Record<string, unknown>)?.IDPPattern as string)?.includes('Pattern1');

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

  let title = 'Reprocess document';
  let message = 'Are you sure you want to reprocess this document?';

  if (selectedItems.length > 1) {
    title = `Reprocess ${selectedItems.length} documents`;
    message = `Are you sure you want to reprocess ${selectedItems.length} documents?`;
  }

  const handleConfirm = () => {
    logger.debug('Reprocessing documents', selectedItems, 'with version', selectedVersion?.value, 'r', selectedRevision);
    onConfirm(selectedVersion?.value, selectedRevision ?? undefined);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={isLoading ? undefined : onDismiss}
      header={title}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleConfirm} loading={isLoading} disabled={!selectedVersion}>
              Reprocess
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <p>{message}</p>

        {isPattern1 && (
          <Alert type="info">
            <strong>NOTE:</strong> To ensure that BDA project blueprints are aligned with your selected configuration profile, be sure to
            execute &quot;Sync To BDA&quot; for your configuration profile from the View/Edit Configuration page.
          </Alert>
        )}

        <FormField label="Configuration Profile" description="Select which configuration profile to use for reprocessing these documents">
          <Select
            selectedOption={selectedVersion}
            onChange={({ detail }) => setSelectedVersion(detail.selectedOption)}
            options={getVersionOptions()}
            placeholder={versions.length === 0 ? 'Loading profiles...' : 'Select configuration profile'}
            disabled={isLoading || versions.length === 0}
            loadingText="Loading profiles..."
          />
        </FormField>

        <ConfigRevisionSelector
          profileName={selectedVersion?.value}
          value={selectedRevision}
          onChange={setSelectedRevision}
          description="Defaults to the profile’s current configuration. Pick an earlier revision to reproduce what an earlier run produced."
          disabled={isLoading}
        />

        <p>This will trigger workflow reprocessing for the following {selectedItems.length > 1 ? 'documents' : 'document'}:</p>
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
