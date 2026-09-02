// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * CreateConfigProfileModal
 *
 * Creates a brand-new configuration profile as a copy of an existing one:
 * pick a source profile, name the copy, save. The new profile inherits the
 * source's full configuration (Default merged with Custom), so it starts as a
 * true clone rather than an empty shell.
 *
 * Used from the Configuration page's profile table ("Create profile") and from
 * the Discovery panels, so discovered schema can be saved to a fresh profile
 * without leaving the Discovery workflow.
 *
 * This copies the source profile's LAST SAVED state. To capture unsaved edits
 * sitting in the configuration editor, use the editor's
 * "Save current edits as new profile…" action instead.
 */
import React, { useState, useEffect, useCallback } from 'react';
import { Modal, Box, SpaceBetween, Button, FormField, Input, Select, Alert } from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { deepMerge } from '../../utils/configUtils';

interface CreateConfigProfileModalProps {
  visible: boolean;
  onDismiss: () => void;
  /** Profile to preselect as the source to copy from (e.g. the currently selected profile). */
  defaultSourceVersion?: string | null;
  /** Called with the new profile name after it is successfully created. */
  onCreated: (versionName: string) => void;
  /** Replaces the default info-alert wording for callers with extra context. */
  infoText?: React.ReactNode;
}

const CreateConfigProfileModal = ({
  visible,
  onDismiss,
  defaultSourceVersion,
  onCreated,
  infoText,
}: CreateConfigProfileModalProps): React.JSX.Element => {
  const { versions, getVersionOptions, fetchVersion, saveAsNewVersion } = useConfigurationVersions();
  const [selectedSource, setSelectedSource] = useState<SelectProps.Option | null>(null);
  const [newVersionName, setNewVersionName] = useState('');
  const [description, setDescription] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset form and preselect the source profile each time the modal opens.
  useEffect(() => {
    if (!visible) return;
    setNewVersionName('');
    setDescription('');
    setCreating(false);
    setError(null);
    const options = getVersionOptions();
    const preselect = defaultSourceVersion
      ? options.find((o) => o.value === defaultSourceVersion)
      : options.find((o) => versions.find((v) => v.versionName === o.value)?.isActive) || options[0];
    setSelectedSource(preselect || null);
    // Intentionally scoped to open events (visible / defaultSourceVersion).
  }, [visible, defaultSourceVersion]);

  const validate = useCallback((): string | null => {
    if (!selectedSource) return 'Please select a source configuration profile to copy from.';
    const name = newVersionName.trim();
    if (!name) return 'Please enter a name for the new configuration profile.';
    if (!/^[a-zA-Z0-9._-]+$/.test(name)) {
      return 'Profile name can only contain letters, numbers, periods, hyphens, and underscores.';
    }
    if (name.length > 50) return 'Profile name cannot exceed 50 characters.';
    if (name === 'default') return 'Cannot use "default" as a profile name — it is reserved.';
    if (versions.some((v) => v.versionName === name)) {
      return `A configuration profile named "${name}" already exists. Please choose a different name.`;
    }
    return null;
  }, [selectedSource, newVersionName, versions]);

  const handleCreate = async () => {
    const validationError = validate();
    if (validationError) {
      setError(validationError);
      return;
    }
    setCreating(true);
    setError(null);
    try {
      // Fetch the source profile's full config (Default + Custom) and deep-merge
      // so the new profile inherits the source's classes and settings.
      const { default: def, custom } = await fetchVersion(selectedSource!.value!);
      const defaultConfig = (typeof def === 'string' ? JSON.parse(def || '{}') : def || {}) as Record<string, unknown>;
      const customConfig = (typeof custom === 'string' ? JSON.parse(custom || '{}') : custom || {}) as Record<string, unknown>;
      const fullConfig = deepMerge(defaultConfig, customConfig);

      const name = newVersionName.trim();
      const result = await saveAsNewVersion(fullConfig, name, description.trim() || `Copied from ${selectedSource!.value}`);
      if (!result.success) {
        throw new Error(result.error || 'Failed to create configuration profile.');
      }
      onCreated(name);
    } catch (err) {
      console.error('Error creating configuration profile:', err);
      setError(err instanceof Error ? err.message : 'An unexpected error occurred.');
    } finally {
      setCreating(false);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Create New Configuration Profile"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={creating}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleCreate} loading={creating} disabled={!selectedSource || !newVersionName.trim()}>
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
          {infoText ||
            'Creates a new, editable configuration profile that inherits its settings and document classes from the selected source profile. The source profile is not modified.'}
        </Alert>

        <FormField label="Copy from profile" description="The existing configuration profile to use as the base for the new profile">
          <Select
            selectedOption={selectedSource}
            onChange={({ detail }) => setSelectedSource(detail.selectedOption)}
            options={getVersionOptions()}
            placeholder="Select a source profile"
            filteringType="auto"
            empty="No configuration profiles available"
          />
        </FormField>

        <FormField
          label="New profile name"
          description="A unique name for the new configuration profile"
          constraintText="Letters, numbers, periods, hyphens, underscores. Max 50 characters. Cannot be 'default'."
        >
          <Input value={newVersionName} onChange={({ detail }) => setNewVersionName(detail.value)} placeholder="e.g., my-custom-config" />
        </FormField>

        <FormField label="Description" description="Optional description for the new configuration profile">
          <Input value={description} onChange={({ detail }) => setDescription(detail.value)} placeholder="Optional description" />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

export default CreateConfigProfileModal;
