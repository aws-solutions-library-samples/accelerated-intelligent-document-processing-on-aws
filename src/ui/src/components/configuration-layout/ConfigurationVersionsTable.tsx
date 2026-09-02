// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useMemo } from 'react';
import {
  Table,
  Box,
  SpaceBetween,
  Link,
  Button,
  Header,
  Pagination,
  TextFilter,
  Alert,
  Badge,
  SegmentedControl,
  CollectionPreferences,
  Modal,
} from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';

interface ConfigVersion {
  versionName: string;
  isActive?: boolean;
  createdAt?: string;
  updatedAt?: string;
  description?: string;
  managed?: boolean;
  /** Highest revision cut for this profile (null before revision history). */
  latestRevision?: number | null;
  /** Revision the profile's current configuration reflects. */
  publishedRevision?: number | null;
}

interface ConfigurationVersionsTableProps {
  versions?: ConfigVersion[];
  loading?: boolean;
  onVersionSelect?: (versionName: string) => void;
  selectedVersionsForCompare?: string[];
  currentlyOpenVersion?: string | null;
  onVersionSelectForCompare?: (versionName: string, selected: boolean) => void;
  onCompareVersions?: () => void;
  onActivateVersion?: (versionName: string) => void;
  onDeleteVersions?: (versionNames: string[]) => void;
  onImportAsNewVersion?: () => void;
  /** Open the "create a profile as a copy of an existing one" modal. */
  onCreateProfile?: () => void;
  /** Open the revision history for one profile. */
  onShowHistory?: (versionName: string) => void;
  isAdmin?: boolean;
}

type TypeFilter = 'all' | 'managed' | 'custom';

// One-line explanations shown on hover for the version Type/state badges.
const BADGE_TOOLTIPS = {
  managed:
    'Stack-managed: shipped with the solution. A stack update records a new revision of it rather than overwriting silently; not directly editable.',
  custom: 'Custom: a user-created profile you can freely edit, save, and delete.',
  active: 'Active: the profile used to process newly uploaded documents.',
};

const PAGE_SIZE_OPTIONS = [
  { value: 5, label: '5 profiles' },
  { value: 10, label: '10 profiles' },
  { value: 20, label: '20 profiles' },
  { value: 50, label: '50 profiles' },
];

const VISIBLE_CONTENT_OPTIONS = [
  { id: 'versionName', label: 'Profile Name', editable: false },
  { id: 'type', label: 'Type' },
  { id: 'description', label: 'Description' },
  { id: 'createdAt', label: 'Created' },
  { id: 'updatedAt', label: 'Updated' },
  { id: 'history', label: 'History' },
];

const DEFAULT_PREFERENCES = {
  pageSize: 10,
  // Created is available in the preferences gear but off by default: two
  // timestamps cost a whole column for information the Updated column already
  // answers, and the extra column is what squeezed the others into wrapping.
  visibleContent: ['versionName', 'type', 'description', 'updatedAt', 'history'],
  wrapLines: false,
};

const ConfigurationVersionsTable = ({
  versions = [],
  loading = false,
  onVersionSelect,
  selectedVersionsForCompare = [],
  currentlyOpenVersion = null,
  onVersionSelectForCompare,
  onCompareVersions,
  onActivateVersion,
  onDeleteVersions,
  onImportAsNewVersion,
  onCreateProfile,
  onShowHistory,
  isAdmin = false,
}: ConfigurationVersionsTableProps): React.JSX.Element => {
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all');
  const [preferences, setPreferences] = useState(DEFAULT_PREFERENCES);
  // Versions confirmed for deletion (drives the confirmation modal).
  const [pendingDeleteVersions, setPendingDeleteVersions] = useState<string[] | null>(null);

  // Helper: treat both managed flag and 'default' version as managed
  const isVersionManaged = (v: ConfigVersion): boolean => v.managed === true || v.versionName === 'default';

  // Filter versions by type (managed/custom) before passing to useCollection
  const filteredByType = useMemo(() => {
    if (typeFilter === 'all') return versions;
    if (typeFilter === 'managed') return versions.filter((v) => isVersionManaged(v));
    return versions.filter((v) => !isVersionManaged(v));
  }, [versions, typeFilter]);

  // Compute type counts for the segmented control labels
  const managedCount = useMemo(() => versions.filter((v) => isVersionManaged(v)).length, [versions]);
  const customCount = useMemo(() => versions.filter((v) => !isVersionManaged(v)).length, [versions]);

  const allColumnDefinitions = [
    {
      id: 'versionName',
      header: 'Profile Name',
      cell: (item: ConfigVersion) => (
        <Box
          fontWeight={item.versionName === currentlyOpenVersion ? 'bold' : 'normal'}
          color={item.isActive ? 'text-status-success' : item.versionName === currentlyOpenVersion ? 'text-status-info' : 'inherit'}
        >
          <Link
            href="#"
            onFollow={(event) => {
              event.preventDefault();
              onVersionSelect?.(item.versionName);
            }}
          >
            {item.versionName}
          </Link>
        </Box>
      ),
      sortingField: 'versionName',
    },
    {
      id: 'type',
      header: 'Type',
      cell: (item: ConfigVersion) => (
        <SpaceBetween direction="horizontal" size="xxs">
          {isVersionManaged(item) ? (
            <span title={BADGE_TOOLTIPS.managed}>
              <Badge color="blue">Managed</Badge>
            </span>
          ) : (
            <span title={BADGE_TOOLTIPS.custom}>
              <Badge color="grey">Custom</Badge>
            </span>
          )}
          {item.isActive && (
            <span title={BADGE_TOOLTIPS.active}>
              <Badge color="green">Active</Badge>
            </span>
          )}
        </SpaceBetween>
      ),
      sortingComparator: (a: ConfigVersion, b: ConfigVersion) => {
        const aType = isVersionManaged(a) ? 'managed' : 'custom';
        const bType = isVersionManaged(b) ? 'managed' : 'custom';
        return aType.localeCompare(bType);
      },
      width: 150,
    },
    {
      id: 'description',
      header: 'Description',
      cell: (item: ConfigVersion) => item.description || '-',
    },
    {
      id: 'createdAt',
      header: 'Created',
      cell: (item: ConfigVersion) => (item.createdAt ? new Date(item.createdAt).toLocaleString() : '-'),
      sortingField: 'createdAt',
      width: 180,
    },
    {
      id: 'updatedAt',
      header: 'Updated',
      cell: (item: ConfigVersion) => (item.updatedAt ? new Date(item.updatedAt).toLocaleString() : '-'),
      sortingField: 'updatedAt',
      width: 180,
    },
    {
      id: 'history',
      header: 'History',
      cell: (item: ConfigVersion) => (
        <Button
          variant="inline-link"
          iconName="undo"
          onClick={() => onShowHistory?.(item.versionName)}
          ariaLabel={`Revision history for ${item.versionName}`}
        >
          {item.latestRevision ? `${item.latestRevision} revision${item.latestRevision === 1 ? '' : 's'}` : 'History'}
        </Button>
      ),
      width: 130,
    },
  ];

  // Filter column definitions based on visible content preferences.
  // (Row selection is handled by the Table's native selectionType, not a column.)
  const columnDefinitions = allColumnDefinitions.filter((col) => preferences.visibleContent.includes(col.id));

  // Map the parent's selectedVersionsForCompare (names) to the item objects the
  // Cloudscape Table expects for controlled multi-selection.
  const selectedItems = useMemo(
    () => filteredByType.filter((v) => selectedVersionsForCompare.includes(v.versionName)),
    [filteredByType, selectedVersionsForCompare],
  );

  const { items, collectionProps, paginationProps, filteredItemsCount, filterProps } = useCollection(filteredByType, {
    pagination: { pageSize: preferences.pageSize },
    sorting: {
      defaultState: {
        sortingColumn: allColumnDefinitions.find((col) => col.id === 'updatedAt')!,
        isDescending: true,
      },
    },
    filtering: {
      empty: (
        <Box margin={{ vertical: 'xs' }} textAlign="center" color="inherit">
          <SpaceBetween size="m">
            <b>No matches</b>
            <Box variant="p" color="inherit">
              We can&apos;t find a match.
            </Box>
          </SpaceBetween>
        </Box>
      ),
      noMatch: (
        <Box margin={{ vertical: 'xs' }} textAlign="center" color="inherit">
          <SpaceBetween size="m">
            <b>No matches</b>
            <Box variant="p" color="inherit">
              We can&apos;t find a match.
            </Box>
          </SpaceBetween>
        </Box>
      ),
    },
  });

  return (
    <SpaceBetween size="s">
      {deleteError && (
        <Alert type="error" dismissible onDismiss={() => setDeleteError(null)} header="Cannot Delete Profile">
          {deleteError}
        </Alert>
      )}
      <Table
        {...collectionProps}
        columnDefinitions={columnDefinitions}
        items={items}
        loading={loading}
        loadingText="Loading profiles..."
        resizableColumns
        stripedRows
        // The profile table sits ABOVE the configuration editor, so its vertical
        // footprint is what pushes the thing you came to edit off-screen.
        contentDensity="compact"
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => {
          const newNames = detail.selectedItems.map((v) => v.versionName);
          // Diff against current selection and emit per-item toggles so the
          // parent's existing handler contract is preserved.
          filteredByType.forEach((v) => {
            const wasSelected = selectedVersionsForCompare.includes(v.versionName);
            const isSelected = newNames.includes(v.versionName);
            if (wasSelected !== isSelected) {
              onVersionSelectForCompare?.(v.versionName, isSelected);
            }
          });
        }}
        trackBy="versionName"
        ariaLabels={{
          selectionGroupLabel: 'Profile selection',
          allItemsSelectionLabel: () => 'Select all profiles',
          itemSelectionLabel: (_sel, item) => `Select profile ${item.versionName}`,
        }}
        wrapLines={preferences.wrapLines}
        empty={
          <Box margin={{ vertical: 'xs' }} textAlign="center" color="inherit">
            <SpaceBetween size="m">
              <b>No profiles</b>
              <Box variant="p" color="inherit">
                No configuration profiles found.
              </Box>
            </SpaceBetween>
          </Box>
        }
        header={
          <SpaceBetween size="s">
            <Header {...({ variant: 'h4' } as Record<string, unknown>)}>Configuration Profiles ({filteredItemsCount})</Header>
            {/* Action buttons row */}
            <SpaceBetween direction="horizontal" size="xs">
              {/* "profiles" is explicit because the revision-history panel has its
                  own Compare button that compares WITHIN one profile. Two
                  identically-labelled compare actions on different axes is the
                  ambiguity this feature exists to remove. */}
              <Button onClick={onCompareVersions} disabled={selectedVersionsForCompare.length < 2}>
                Compare profiles ({selectedVersionsForCompare.length})
              </Button>
              <Button
                onClick={() => onActivateVersion?.(selectedVersionsForCompare[0])}
                disabled={
                  selectedVersionsForCompare.length !== 1 || versions.find((v) => v.versionName === selectedVersionsForCompare[0])?.isActive
                }
              >
                Activate
              </Button>
              {isAdmin && (
                <Button variant="normal" onClick={() => onImportAsNewVersion?.()} iconName="upload">
                  Import
                </Button>
              )}
              {/* Delete is deliberately NOT the primary button: a destructive action
                  should not be the visual default on a table whose main job is
                  creating and opening profiles. */}
              <Button
                variant="normal"
                onClick={() => {
                  // Check if any selected versions are active, default, or managed
                  const activeVersions = selectedVersionsForCompare.filter((vId) => {
                    const version = versions.find((v) => v.versionName === vId);
                    return version?.isActive || vId === 'default';
                  });
                  const managedVersions = selectedVersionsForCompare.filter((vId) => {
                    const version = versions.find((v) => v.versionName === vId);
                    return version?.managed === true;
                  });

                  if (activeVersions.length > 0) {
                    setDeleteError(`Cannot delete active or default profiles: ${activeVersions.join(', ')}`);
                    return;
                  }
                  if (managedVersions.length > 0) {
                    setDeleteError(`Cannot delete stack-managed profiles: ${managedVersions.join(', ')}`);
                    return;
                  }

                  setDeleteError(null);
                  // Confirm before the destructive delete (S5).
                  setPendingDeleteVersions(selectedVersionsForCompare);
                }}
                disabled={selectedVersionsForCompare.length === 0}
              >
                Delete Selected ({selectedVersionsForCompare.length})
              </Button>
              {/* Creating a profile from an existing one used to require opening the
                  source profile in the editor and finding "Save as Profile" in the
                  Actions menu. It is the most common way a profile gets made, so it
                  belongs here, on the profile-management surface. */}
              {isAdmin && (
                <Button variant="primary" onClick={() => onCreateProfile?.()} iconName="add-plus">
                  Create profile
                </Button>
              )}
            </SpaceBetween>
          </SpaceBetween>
        }
        filter={
          <SpaceBetween direction="horizontal" size="m">
            <TextFilter {...filterProps} {...({ placeholder: 'Search profiles...' } as Record<string, unknown>)} />
            <SegmentedControl
              selectedId={typeFilter}
              onChange={({ detail }) => setTypeFilter(detail.selectedId as TypeFilter)}
              options={[
                { id: 'all', text: `All (${versions.length})` },
                { id: 'managed', text: `Managed (${managedCount})` },
                { id: 'custom', text: `Custom (${customCount})` },
              ]}
            />
          </SpaceBetween>
        }
        pagination={<Pagination {...paginationProps} />}
        preferences={
          <CollectionPreferences
            title="Preferences"
            confirmLabel="Confirm"
            cancelLabel="Cancel"
            preferences={preferences}
            onConfirm={({ detail }) =>
              setPreferences({
                pageSize: detail.pageSize ?? DEFAULT_PREFERENCES.pageSize,
                visibleContent: (detail.visibleContent as string[]) ?? DEFAULT_PREFERENCES.visibleContent,
                wrapLines: detail.wrapLines ?? DEFAULT_PREFERENCES.wrapLines,
              })
            }
            pageSizePreference={{
              title: 'Page size',
              options: PAGE_SIZE_OPTIONS,
            }}
            visibleContentPreference={{
              title: 'Visible columns',
              options: [
                {
                  label: 'Profile properties',
                  options: VISIBLE_CONTENT_OPTIONS,
                },
              ],
            }}
            wrapLinesPreference={{
              label: 'Wrap lines',
              description: 'Select to wrap long text in table cells',
            }}
          />
        }
      />

      {/* Delete confirmation (S5): destructive delete requires explicit confirm. */}
      <Modal
        visible={!!pendingDeleteVersions}
        onDismiss={() => setPendingDeleteVersions(null)}
        header="Delete configuration profiles"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setPendingDeleteVersions(null)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  if (pendingDeleteVersions) {
                    onDeleteVersions?.(pendingDeleteVersions);
                  }
                  setPendingDeleteVersions(null);
                }}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            {pendingDeleteVersions && pendingDeleteVersions.length === 1
              ? `Permanently delete the configuration profile "${pendingDeleteVersions[0]}" and its revision history? This action cannot be undone.`
              : `Permanently delete these ${pendingDeleteVersions?.length ?? 0} configuration profiles and their revision histories? This action cannot be undone.`}
          </Box>
          {pendingDeleteVersions && pendingDeleteVersions.length > 1 && (
            <ul>
              {pendingDeleteVersions.map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default ConfigurationVersionsTable;
