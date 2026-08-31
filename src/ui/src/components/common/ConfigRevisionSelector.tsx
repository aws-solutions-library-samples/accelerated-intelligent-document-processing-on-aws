// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useMemo } from 'react';
import { FormField, Select, SelectProps, StatusIndicator } from '@cloudscape-design/components';
import useConfigProfileRevisions from '../../hooks/use-config-profile-revisions';

interface ConfigRevisionSelectorProps {
  /** Configuration Profile whose revisions are offered. */
  profileName?: string | null;
  /** Selected revision, or null for "the profile's current configuration". */
  value: number | null;
  onChange: (revision: number | null) => void;
  label?: string;
  description?: string;
  disabled?: boolean;
}

/** Sentinel for "current" so the Select has a stable non-null option value. */
const CURRENT = '__current__';

/**
 * Picks a revision of a Configuration Profile, defaulting to its current one.
 *
 * Rendered next to a profile picker wherever a profile is chosen for work. It
 * renders nothing when the profile has no revision history (an older deployment,
 * or a profile untouched since the upgrade) — an empty dropdown offering only
 * "Current" is noise, not a choice.
 */
const ConfigRevisionSelector = ({
  profileName,
  value,
  onChange,
  label = 'Configuration revision',
  description = 'Defaults to the profile’s current configuration. Pick an earlier revision to run against exactly what it recorded.',
  disabled = false,
}: ConfigRevisionSelectorProps): React.JSX.Element | null => {
  const { revisions, loading, error, loadRevisions } = useConfigProfileRevisions();

  useEffect(() => {
    if (profileName) loadRevisions(profileName);
  }, [profileName, loadRevisions]);

  // Changing profile invalidates any revision chosen under the previous one.
  useEffect(() => {
    onChange(null);
  }, [profileName]);

  const options: SelectProps.Option[] = useMemo(() => {
    const published = revisions.find((r) => r.published);
    const current: SelectProps.Option = {
      value: CURRENT,
      label: published ? `Current (r${published.revision})` : 'Current',
      description: 'Whatever the profile holds when the work runs',
    };
    const older = revisions
      .filter((r) => !r.published)
      .map((r) => ({
        value: String(r.revision),
        label: `r${r.revision}`,
        description: [r.label, r.notes, r.createdAt ? new Date(r.createdAt).toLocaleString() : null].filter(Boolean).join(' — '),
      }));
    return [current, ...older];
  }, [revisions]);

  if (!profileName) return null;
  if (loading) return <StatusIndicator type="loading">Loading revisions…</StatusIndicator>;
  // No history: nothing meaningful to choose between.
  if (!error && revisions.length <= 1) return null;

  const selectedOption = options.find((o) => o.value === (value === null ? CURRENT : String(value))) ?? options[0];

  return (
    <FormField label={label} description={description} errorText={error ?? undefined}>
      <Select
        selectedOption={selectedOption}
        options={options}
        disabled={disabled}
        onChange={({ detail }) => {
          const picked = detail.selectedOption?.value;
          onChange(!picked || picked === CURRENT ? null : Number(picked));
        }}
        placeholder="Current"
      />
    </FormField>
  );
};

export default ConfigRevisionSelector;
