// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect, useCallback } from 'react';
import { generateClient } from '../../api/client-shim';
import { Modal, Box, SpaceBetween, Button, FormField, Input, Select, Checkbox, Alert } from '@cloudscape-design/components';

interface SelectOption {
  label: string;
  value: string;
  description?: string;
}

interface ConfigVersion {
  versionName: string;
  isActive?: boolean;
  description?: string;
}

interface CreateConfigVersionModalProps {
  visible: boolean;
  onDismiss: () => void;
  deploymentArn: string;
  jobName: string;
  onSuccess: (versionName: string) => void;
}

const CreateConfigVersionModal = ({
  visible,
  onDismiss,
  deploymentArn,
  jobName,
  onSuccess,
}: CreateConfigVersionModalProps): React.JSX.Element => {
  const [configVersions, setConfigVersions] = useState<ConfigVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [selectedSourceVersion, setSelectedSourceVersion] = useState<SelectOption | null>(null);
  const [newVersionName, setNewVersionName] = useState('');
  const [useForExtraction, setUseForExtraction] = useState(true);
  const [useForClassification, setUseForClassification] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [description, setDescription] = useState('');

  // Fetch configuration profiles when modal opens
  const fetchConfigVersions = useCallback(async () => {
    setVersionsLoading(true);
    try {
      const client = generateClient();
      const response = (await client.graphql({
        query: `
          query GetConfigVersions {
            getConfigVersions {
              success
              versions {
                versionName
                isActive
                description
              }
              error {
                type
                message
              }
            }
          }
        `,
      })) as { data: { getConfigVersions?: { success: boolean; versions?: ConfigVersion[]; error?: { message: string } } } };

      const data = response.data.getConfigVersions;
      if (data?.success && data.versions) {
        setConfigVersions(data.versions);
      } else {
        setError(data?.error?.message || 'Failed to fetch configuration profiles.');
      }
    } catch (err) {
      console.error('Error fetching configuration profiles:', err);
      setError('Failed to fetch configuration profiles.');
    } finally {
      setVersionsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (visible) {
      fetchConfigVersions();
      // Reset form state
      setSelectedSourceVersion(null);
      setNewVersionName('');
      setUseForExtraction(true);
      setUseForClassification(false);
      setCreating(false);
      setError(null);
      setDescription('');
    }
  }, [visible, fetchConfigVersions]);

  // Auto-generate a suggested version name based on job name
  useEffect(() => {
    if (visible && jobName && !newVersionName) {
      // Sanitize job name for use as version name: letters, numbers,
      // periods, hyphens, underscores
      const sanitized = jobName
        .replace(/[^a-zA-Z0-9._-]/g, '-')
        .replace(/-+/g, '-')
        .substring(0, 40);
      setNewVersionName(`${sanitized}-custom`);
    }
  }, [visible, jobName]);

  const validateForm = (): string | null => {
    if (!selectedSourceVersion) {
      return 'Please select a source configuration profile.';
    }
    if (!newVersionName.trim()) {
      return 'Please enter a name for the new configuration profile.';
    }
    if (!/^[a-zA-Z0-9._-]+$/.test(newVersionName)) {
      return 'Profile name can only contain letters, numbers, periods, hyphens, and underscores.';
    }
    if (newVersionName.length > 50) {
      return 'Profile name cannot exceed 50 characters.';
    }
    if (newVersionName === 'default') {
      return 'Cannot use "default" as a profile name — it is reserved.';
    }
    // Check if version name already exists
    if (configVersions.some((v) => v.versionName === newVersionName)) {
      return `A configuration profile named "${newVersionName}" already exists. Please choose a different name.`;
    }
    if (!useForExtraction && !useForClassification) {
      return 'Please select at least one option: Extraction or Classification.';
    }
    return null;
  };

  const handleCreate = async () => {
    const validationError = validateForm();
    if (validationError) {
      setError(validationError);
      return;
    }

    setCreating(true);
    setError(null);

    try {
      const client = generateClient();

      // Step 1: Fetch the source configuration profile's full configuration
      const configResponse = (await client.graphql({
        query: `
          query GetConfigVersion($versionName: String!) {
            getConfigVersion(versionName: $versionName) {
              success
              Schema
              Default
              Custom
              error {
                type
                message
              }
            }
          }
        `,
        variables: { versionName: selectedSourceVersion!.value },
      })) as {
        data: {
          getConfigVersion?: {
            success: boolean;
            Default: string;
            Custom: string;
            error?: { message: string };
          };
        };
      };

      const configData = configResponse.data.getConfigVersion;
      if (!configData?.success) {
        throw new Error(configData?.error?.message || 'Failed to fetch source configuration.');
      }

      // Parse the Default and Custom configs
      const defaultConfig = typeof configData.Default === 'string' ? JSON.parse(configData.Default) : configData.Default || {};
      const customConfig = typeof configData.Custom === 'string' ? JSON.parse(configData.Custom) : configData.Custom || {};

      // Deep merge default + custom to get the full config (same as frontend merge pattern)
      const fullConfig = deepMerge(defaultConfig, customConfig);

      // Step 2: Apply the custom model deployment ARN
      if (useForExtraction) {
        if (!fullConfig.extraction) {
          fullConfig.extraction = {};
        }
        (fullConfig.extraction as Record<string, unknown>).model = deploymentArn;
      }

      if (useForClassification) {
        if (!fullConfig.classification) {
          fullConfig.classification = {};
        }
        (fullConfig.classification as Record<string, unknown>).model = deploymentArn;
      }

      // Step 3: Save as a new profile using the existing updateConfiguration mutation
      // with the saveAsVersion flag
      const configWithFlag = {
        ...fullConfig,
        saveAsVersion: true,
      };

      const updateResponse = (await client.graphql({
        query: `
          mutation UpdateConfiguration($versionName: String!, $customConfig: AWSJSON!, $description: String) {
            updateConfiguration(versionName: $versionName, customConfig: $customConfig, description: $description) {
              success
              message
              error {
                type
                message
                validationErrors {
                  field
                  message
                  type
                }
              }
            }
          }
        `,
        variables: {
          versionName: newVersionName,
          customConfig: JSON.stringify(configWithFlag),
          description: description || `Created from ${selectedSourceVersion!.value} with custom model deployment from job "${jobName}"`,
        },
      })) as {
        data: {
          updateConfiguration?: {
            success: boolean;
            message?: string;
            error?: { message: string; validationErrors?: { field: string; message: string }[] };
          };
        };
      };

      const updateData = updateResponse.data.updateConfiguration;
      if (!updateData?.success) {
        const validationErrors = updateData?.error?.validationErrors;
        if (validationErrors && validationErrors.length > 0) {
          const errorDetails = validationErrors.map((e) => `${e.field}: ${e.message}`).join('; ');
          throw new Error(`Configuration validation failed: ${errorDetails}`);
        }
        throw new Error(updateData?.error?.message || 'Failed to create configuration profile.');
      }

      onSuccess(newVersionName);
    } catch (err) {
      console.error('Error creating configuration profile:', err);
      const errorMessage = err instanceof Error ? err.message : 'An unexpected error occurred.';
      setError(errorMessage);
    } finally {
      setCreating(false);
    }
  };

  const versionOptions: SelectOption[] = [...configVersions]
    .sort((a, b) => a.versionName.localeCompare(b.versionName, undefined, { numeric: true, sensitivity: 'base' }))
    .map((v) => ({
      label: v.isActive ? `${v.versionName} (Active)` : v.versionName,
      value: v.versionName,
      description: v.description || undefined,
    }));

  const atLeastOneChecked = useForExtraction || useForClassification;

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Create Configuration Profile with Custom Model"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={creating}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={handleCreate}
              loading={creating}
              disabled={!selectedSourceVersion || !newVersionName.trim() || !atLeastOneChecked}
            >
              Create profile
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        <Alert type="info">
          This will create a new configuration profile based on an existing one, with the custom model deployment ARN automatically set for
          the selected pipeline stages.
        </Alert>

        <FormField
          label="Source Configuration Profile"
          description="Select the existing configuration profile to copy as the base for the new profile"
        >
          <Select
            selectedOption={selectedSourceVersion}
            onChange={({ detail }) => setSelectedSourceVersion(detail.selectedOption as SelectOption)}
            options={versionOptions}
            placeholder="Select a configuration profile"
            loadingText="Loading configuration profiles..."
            statusType={versionsLoading ? 'loading' : 'finished'}
            empty="No configuration profiles available"
            filteringType="auto"
          />
        </FormField>

        <FormField
          label="New Profile Name"
          description="A unique name for the new configuration profile (letters, numbers, hyphens, underscores)"
          constraintText="Max 50 characters. Cannot be 'default'."
        >
          <Input
            value={newVersionName}
            onChange={({ detail }) => setNewVersionName(detail.value)}
            placeholder="e.g., my-config-with-custom-model"
          />
        </FormField>

        <FormField label="Description" description="Optional description for the new configuration profile">
          <Input
            value={description}
            onChange={({ detail }) => setDescription(detail.value)}
            placeholder={`Created from source config with custom model from "${jobName}"`}
          />
        </FormField>

        <FormField label="Apply Custom Model To" description="Select which pipeline stages should use the custom model deployment ARN">
          <SpaceBetween size="xs">
            <Checkbox checked={useForExtraction} onChange={({ detail }) => setUseForExtraction(detail.checked)}>
              <strong>Extraction</strong> — Set <code>extraction.model</code> to the custom model deployment ARN
            </Checkbox>
            <Checkbox checked={useForClassification} onChange={({ detail }) => setUseForClassification(detail.checked)}>
              <strong>Classification</strong> — Set <code>classification.model</code> to the custom model deployment ARN
            </Checkbox>
          </SpaceBetween>
        </FormField>

        {!atLeastOneChecked && <Alert type="warning">Please select at least one pipeline stage (Extraction or Classification).</Alert>}

        <FormField label="Custom Model Deployment ARN">
          <Box variant="code" fontSize="body-s">
            {deploymentArn}
          </Box>
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

/**
 * Deep merge utility - merges source into target recursively.
 * Source values override target values. Arrays are replaced, not merged.
 */
function deepMerge(target: Record<string, unknown>, source: Record<string, unknown>): Record<string, unknown> {
  const result = { ...target };

  for (const key of Object.keys(source)) {
    const sourceVal = source[key];
    const targetVal = result[key];

    if (
      sourceVal &&
      typeof sourceVal === 'object' &&
      !Array.isArray(sourceVal) &&
      targetVal &&
      typeof targetVal === 'object' &&
      !Array.isArray(targetVal)
    ) {
      result[key] = deepMerge(targetVal as Record<string, unknown>, sourceVal as Record<string, unknown>);
    } else {
      result[key] = sourceVal;
    }
  }

  return result;
}

export default CreateConfigVersionModal;
