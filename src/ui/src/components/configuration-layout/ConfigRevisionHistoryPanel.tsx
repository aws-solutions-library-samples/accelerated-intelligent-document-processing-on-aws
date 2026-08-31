// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  FormField,
  Header,
  Input,
  Modal,
  SpaceBetween,
  StatusIndicator,
  Table,
  Textarea,
} from '@cloudscape-design/components';
import useConfigProfileRevisions, { ConfigProfileRevision } from '../../hooks/use-config-profile-revisions';
import useUserRole from '../../hooks/use-user-role';
import ConfigurationComparison from './ConfigurationComparison';

interface ConfigRevisionHistoryPanelProps {
  /** Configuration Profile whose history is shown. */
  profileName: string;
  visible: boolean;
  onDismiss: () => void;
  /** Called after a restore so the parent can reload the open configuration. */
  onRestored?: (newRevision: number) => void;
}

const BADGE_TOOLTIPS = {
  current: 'Current: the configuration this profile is running now.',
  pinned: 'Pinned by a test run — kept regardless of the retention limit so the run stays comparable.',
  labeled: 'Labeled — kept regardless of the retention limit.',
};

const formatSize = (bytes?: number | null): string => {
  if (!bytes) return '-';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

const formatWhen = (iso?: string | null): string => (iso ? new Date(iso).toLocaleString() : '-');

/**
 * Revision history for one Configuration Profile.
 *
 * Every save cuts a revision, so this is where a user recovers the previous
 * configuration — the reason an in-place save is safe. Restore is forward-only:
 * it saves the chosen revision as a NEW revision rather than rewriting history.
 */
const ConfigRevisionHistoryPanel = ({
  profileName,
  visible,
  onDismiss,
  onRestored,
}: ConfigRevisionHistoryPanelProps): React.JSX.Element => {
  const { isAdmin, canWrite } = useUserRole();
  const { revisions, loading, error, setError, loadRevisions, fetchRevisionConfig, restoreRevision, labelRevision, deleteRevision } =
    useConfigProfileRevisions();

  const [selected, setSelected] = useState<ConfigProfileRevision[]>([]);
  const [compareConfigs, setCompareConfigs] = useState<Record<string, unknown> | null>(null);
  const [comparing, setComparing] = useState(false);
  const [busyRevision, setBusyRevision] = useState<number | null>(null);

  const [restoreTarget, setRestoreTarget] = useState<ConfigProfileRevision | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ConfigProfileRevision | null>(null);
  const [labelTarget, setLabelTarget] = useState<ConfigProfileRevision | null>(null);
  const [labelValue, setLabelValue] = useState('');
  const [notesValue, setNotesValue] = useState('');

  useEffect(() => {
    if (visible && profileName) {
      setSelected([]);
      setCompareConfigs(null);
      loadRevisions(profileName);
    }
  }, [visible, profileName, loadRevisions]);

  const handleCompare = useCallback(async () => {
    if (selected.length !== 2) return;
    // Compare oldest → newest so the left column is the earlier revision.
    const [a, b] = [...selected].sort((x, y) => x.revision - y.revision);
    setComparing(true);
    setCompareConfigs(null);
    const [configA, configB] = await Promise.all([
      fetchRevisionConfig(profileName, a.revision),
      fetchRevisionConfig(profileName, b.revision),
    ]);
    setComparing(false);
    if (configA && configB) {
      setCompareConfigs({ [`r${a.revision}`]: configA, [`r${b.revision}`]: configB });
    }
  }, [selected, profileName, fetchRevisionConfig]);

  const handleRestore = async () => {
    if (!restoreTarget) return;
    setBusyRevision(restoreTarget.revision);
    const newRevision = await restoreRevision(profileName, restoreTarget.revision);
    setBusyRevision(null);
    setRestoreTarget(null);
    if (newRevision !== null) {
      setSelected([]);
      setCompareConfigs(null);
      onRestored?.(newRevision);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setBusyRevision(deleteTarget.revision);
    await deleteRevision(profileName, deleteTarget.revision);
    setBusyRevision(null);
    setDeleteTarget(null);
    setSelected([]);
  };

  const handleLabelSave = async () => {
    if (!labelTarget) return;
    setBusyRevision(labelTarget.revision);
    await labelRevision(profileName, labelTarget.revision, labelValue, notesValue);
    setBusyRevision(null);
    setLabelTarget(null);
  };

  const compareVersionKeys = compareConfigs ? Object.keys(compareConfigs) : [];

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="max"
      header={`Configuration revisions — ${profileName}`}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            Close
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        <Table
          variant="embedded"
          loading={loading}
          loadingText="Loading configuration revisions..."
          selectionType="multi"
          selectedItems={selected}
          onSelectionChange={({ detail }) => {
            // Cap at two: comparison is pairwise.
            setSelected((detail.selectedItems as ConfigProfileRevision[]).slice(-2));
          }}
          trackBy="revision"
          items={revisions}
          empty={
            <Box textAlign="center" color="inherit">
              <b>No revisions yet</b>
              <Box variant="p" color="inherit">
                A revision is recorded each time this profile&apos;s configuration is saved.
              </Box>
            </Box>
          }
          header={
            <Header
              variant="h3"
              counter={revisions.length ? `(${revisions.length})` : undefined}
              description="Every save records an immutable revision. Compare any two, restore an earlier one, or label a revision to keep it beyond the retention limit."
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" onClick={() => loadRevisions(profileName)} loading={loading}>
                    Refresh
                  </Button>
                  <Button variant="primary" disabled={selected.length !== 2} loading={comparing} onClick={handleCompare}>
                    Compare revisions ({selected.length})
                  </Button>
                </SpaceBetween>
              }
            >
              Configuration revisions
            </Header>
          }
          columnDefinitions={[
            {
              id: 'revision',
              header: 'Revision',
              cell: (item: ConfigProfileRevision) => (
                <SpaceBetween direction="horizontal" size="xxs">
                  <Box fontWeight={item.published ? 'bold' : 'normal'}>r{item.revision}</Box>
                  {item.published && (
                    <span title={BADGE_TOOLTIPS.current}>
                      <Badge color="green">Current</Badge>
                    </span>
                  )}
                  {item.pinned && (
                    <span title={BADGE_TOOLTIPS.pinned}>
                      <Badge color="blue">Pinned</Badge>
                    </span>
                  )}
                  {item.label && (
                    <span title={BADGE_TOOLTIPS.labeled}>
                      <Badge color="grey">{item.label}</Badge>
                    </span>
                  )}
                </SpaceBetween>
              ),
            },
            { id: 'createdAt', header: 'Saved', cell: (item: ConfigProfileRevision) => formatWhen(item.createdAt) },
            { id: 'createdBy', header: 'By', cell: (item: ConfigProfileRevision) => item.createdBy || '-' },
            { id: 'notes', header: 'Notes', cell: (item: ConfigProfileRevision) => item.notes || '-' },
            { id: 'size', header: 'Size', cell: (item: ConfigProfileRevision) => formatSize(item.sizeBytes) },
            {
              id: 'actions',
              header: 'Actions',
              cell: (item: ConfigProfileRevision) => (
                <ButtonDropdown
                  variant="inline-icon"
                  expandToViewport
                  loading={busyRevision === item.revision}
                  ariaLabel={`Actions for revision r${item.revision}`}
                  items={[
                    {
                      id: 'restore',
                      text: 'Restore this revision',
                      disabled: !canWrite || !!item.published,
                      disabledReason: item.published
                        ? 'This is already the current configuration'
                        : 'You do not have permission to change this configuration',
                    },
                    {
                      id: 'label',
                      text: item.label ? 'Edit label' : 'Add label',
                      disabled: !canWrite,
                      disabledReason: 'You do not have permission to change this configuration',
                    },
                    {
                      id: 'delete',
                      text: 'Delete revision',
                      disabled: !isAdmin || !!item.published,
                      disabledReason: item.published
                        ? 'Cannot delete the current configuration'
                        : 'Deleting a revision is an Admin-only operation',
                    },
                  ]}
                  onItemClick={({ detail }) => {
                    if (detail.id === 'restore') setRestoreTarget(item);
                    if (detail.id === 'delete') setDeleteTarget(item);
                    if (detail.id === 'label') {
                      setLabelValue(item.label || '');
                      setNotesValue(item.notes || '');
                      setLabelTarget(item);
                    }
                  }}
                />
              ),
            },
          ]}
        />
      </SpaceBetween>

      {/* Comparison in its own modal: with a long history the diff would
          otherwise sit below a table the user has to scroll past to reach it. */}
      <Modal
        visible={comparing || compareConfigs !== null}
        onDismiss={() => setCompareConfigs(null)}
        size="max"
        header={
          compareVersionKeys.length === 2
            ? `Compare ${compareVersionKeys[0]} → ${compareVersionKeys[1]} — ${profileName}`
            : 'Compare revisions'
        }
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setCompareConfigs(null)}>
              Close
            </Button>
          </Box>
        }
      >
        {comparing && <StatusIndicator type="loading">Loading both revisions...</StatusIndicator>}
        {!comparing && compareConfigs && compareVersionKeys.length === 2 && (
          <ConfigurationComparison versions={compareVersionKeys} configs={compareConfigs} />
        )}
      </Modal>

      {/* Restore confirmation */}
      <Modal
        visible={restoreTarget !== null}
        onDismiss={() => setRestoreTarget(null)}
        header="Restore revision"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setRestoreTarget(null)} disabled={busyRevision !== null}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleRestore} loading={busyRevision !== null}>
                Restore
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Alert type="info">
            Restoring saves r{restoreTarget?.revision} as a new revision — the configuration being replaced stays in this history and can be
            restored again.
          </Alert>
          <Box>
            Restore <strong>r{restoreTarget?.revision}</strong> as the current configuration of <strong>{profileName}</strong>?
          </Box>
        </SpaceBetween>
      </Modal>

      {/* Delete confirmation */}
      <Modal
        visible={deleteTarget !== null}
        onDismiss={() => setDeleteTarget(null)}
        header="Delete revision"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteTarget(null)} disabled={busyRevision !== null}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleDelete} loading={busyRevision !== null}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Alert type="warning">
            This permanently deletes the stored configuration for this revision. It cannot be undone, and any test run that scored against
            it will no longer be reproducible.
          </Alert>
          <Box>
            Delete <strong>r{deleteTarget?.revision}</strong> of <strong>{profileName}</strong>?
          </Box>
        </SpaceBetween>
      </Modal>

      {/* Label editor */}
      <Modal
        visible={labelTarget !== null}
        onDismiss={() => setLabelTarget(null)}
        header={`Label r${labelTarget?.revision}`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setLabelTarget(null)} disabled={busyRevision !== null}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleLabelSave} loading={busyRevision !== null}>
                Save
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="info">A labeled revision is kept regardless of the retention limit.</Alert>
          <FormField label="Label" description="Short marker shown in the history, e.g. “known good”.">
            <Input value={labelValue} onChange={({ detail }) => setLabelValue(detail.value)} placeholder="known good" />
          </FormField>
          <FormField label="Notes" description="Optional detail about what changed in this revision.">
            <Textarea value={notesValue} onChange={({ detail }) => setNotesValue(detail.value)} rows={3} />
          </FormField>
        </SpaceBetween>
      </Modal>
    </Modal>
  );
};

export default ConfigRevisionHistoryPanel;
