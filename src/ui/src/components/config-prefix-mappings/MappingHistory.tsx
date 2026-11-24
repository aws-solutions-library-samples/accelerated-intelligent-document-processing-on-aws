// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The append-only change log for config prefix mappings, on the admin page.
 *
 * A mapping decides which Configuration Profile — and so which scoped users — a
 * document belongs to, so "who changed this prefix, and from what" has to outlive
 * the next edit. The entry itself only records its latest editor; this panel reads
 * the server's change log, which keeps every create, edit and delete, including
 * for mappings that have since been deleted and no longer appear in the table.
 *
 * Loaded on demand, when the section is expanded: the page's mount already issues
 * the mapping list request, and the log can be long.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import { Alert, Box, Button, ExpandableSection, SpaceBetween, Table } from '@cloudscape-design/components';
import {
  fetchMappingHistory,
  type ConfigPrefixMapping,
  type ConfigPrefixMappingHistoryEntry,
} from '../../hooks/use-config-prefix-mappings';
import { useFormatters } from '../../i18n/format';

const messages = defineMessages<{
  header: NoMessageValues;
  description: NoMessageValues;
  filteredTo: { prefix: string };
  showAll: NoMessageValues;
  colWhen: NoMessageValues;
  colWho: NoMessageValues;
  colMapping: NoMessageValues;
  colChange: NoMessageValues;
  colDetails: NoMessageValues;
  actionCreate: NoMessageValues;
  actionUpdate: NoMessageValues;
  actionDelete: NoMessageValues;
  published: NoMessageValues;
  revision: { revision: number };
  created: { profile: string; revision: string; precedence: string };
  deleted: { profile: string; revision: string };
  changedProfile: { from: string; to: string };
  changedRevision: { from: string; to: string };
  changedPrecedence: { from: string; to: string };
  changedEnabled: { from: string; to: string };
  changedDescription: NoMessageValues;
  precedenceMapping: NoMessageValues;
  precedenceMetadata: NoMessageValues;
  precedenceReject: NoMessageValues;
  enabledYes: NoMessageValues;
  enabledNo: NoMessageValues;
  noFieldChange: NoMessageValues;
  empty: NoMessageValues;
  loading: NoMessageValues;
  loadOlder: NoMessageValues;
  loadFailed: NoMessageValues;
  historyLink: NoMessageValues;
  historyLinkAria: { prefix: string };
}>({
  header: {
    id: 'configPrefixMappings.history.header',
    defaultMessage: 'Change history',
    description: 'Header of the collapsible section on the Prefix Mappings page that lists every change made to a mapping.',
  },
  description: {
    id: 'configPrefixMappings.history.description',
    defaultMessage:
      'Every create, edit and delete made to a mapping, newest first, with who made it. Changes to deleted mappings are kept.',
    description: 'Explanation under the Change history header on the Prefix Mappings page.',
  },
  filteredTo: {
    id: 'configPrefixMappings.history.filteredTo',
    defaultMessage: 'Showing changes to {prefix} only.',
    description: 'Shown when the change history is narrowed to one mapping. {prefix} is the mapping prefix, for example acme/invoices/.',
  },
  showAll: {
    id: 'configPrefixMappings.history.showAll',
    defaultMessage: 'Show all mappings',
    description: 'Button that removes the one-mapping filter from the change history.',
  },
  colWhen: {
    id: 'configPrefixMappings.history.col.when',
    defaultMessage: 'When',
    description: 'Change history column header: when the change was made.',
  },
  colWho: {
    id: 'configPrefixMappings.history.col.who',
    defaultMessage: 'Changed by',
    description: 'Change history column header: the signed-in administrator who made the change.',
  },
  colMapping: {
    id: 'configPrefixMappings.history.col.mapping',
    defaultMessage: 'Mapping',
    description: 'Change history column header: the S3 prefix of the mapping that changed.',
  },
  colChange: {
    id: 'configPrefixMappings.history.col.change',
    defaultMessage: 'Change',
    description: 'Change history column header: whether the mapping was created, edited or deleted.',
  },
  colDetails: {
    id: 'configPrefixMappings.history.col.details',
    defaultMessage: 'Before and after',
    description: 'Change history column header: what the change did to the mapping.',
  },
  actionCreate: {
    id: 'configPrefixMappings.history.action.create',
    defaultMessage: 'Created',
    description: 'Change history: the mapping was created.',
  },
  actionUpdate: {
    id: 'configPrefixMappings.history.action.update',
    defaultMessage: 'Edited',
    description: 'Change history: the mapping was edited.',
  },
  actionDelete: {
    id: 'configPrefixMappings.history.action.delete',
    defaultMessage: 'Deleted',
    description: 'Change history: the mapping was deleted.',
  },
  published: {
    id: 'configPrefixMappings.history.published',
    defaultMessage: 'published revision',
    description: 'Change history: the mapping follows its profile’s published revision rather than pinning one.',
  },
  revision: {
    id: 'configPrefixMappings.history.revision',
    defaultMessage: 'r{revision}',
    description: 'Change history: a pinned revision number of a Configuration Profile, for example r7.',
  },
  created: {
    id: 'configPrefixMappings.history.created',
    defaultMessage: 'Assigned {profile} ({revision}), conflict mode {precedence}',
    description:
      'Change history detail for a created mapping. {profile} is a Configuration Profile name, {revision} is either "published revision" or r7, {precedence} is the conflict mode, for example "mapping wins".',
  },
  deleted: {
    id: 'configPrefixMappings.history.deleted',
    defaultMessage: 'Was assigned {profile} ({revision})',
    description: 'Change history detail for a deleted mapping: what it assigned before it was deleted.',
  },
  changedProfile: {
    id: 'configPrefixMappings.history.changed.profile',
    defaultMessage: 'Profile: {from} → {to}',
    description: 'Change history detail: the mapping’s Configuration Profile changed from one name to another.',
  },
  changedRevision: {
    id: 'configPrefixMappings.history.changed.revision',
    defaultMessage: 'Revision: {from} → {to}',
    description: 'Change history detail: the mapping’s revision changed. Each side is "published revision" or r7.',
  },
  changedPrecedence: {
    id: 'configPrefixMappings.history.changed.precedence',
    defaultMessage: 'Conflict mode: {from} → {to}',
    description: 'Change history detail: the mapping’s conflict mode changed (mapping, metadata or reject).',
  },
  changedEnabled: {
    id: 'configPrefixMappings.history.changed.enabled',
    defaultMessage: 'Enabled: {from} → {to}',
    description: 'Change history detail: the mapping was turned on or off. Each side is yes or no.',
  },
  changedDescription: {
    id: 'configPrefixMappings.history.changed.description',
    defaultMessage: 'Description changed',
    description: 'Change history detail: the mapping’s free-text description was edited.',
  },
  precedenceMapping: {
    id: 'configPrefixMappings.history.precedence.mapping',
    defaultMessage: 'mapping wins',
    description: 'Change history: the conflict mode in which the mapping overrides a profile named by the upload.',
  },
  precedenceMetadata: {
    id: 'configPrefixMappings.history.precedence.metadata',
    defaultMessage: 'upload metadata wins',
    description: 'Change history: the conflict mode in which a profile named by the upload overrides the mapping.',
  },
  precedenceReject: {
    id: 'configPrefixMappings.history.precedence.reject',
    defaultMessage: 'refuse the conflict',
    description: 'Change history: the conflict mode in which a conflicting upload is refused.',
  },
  enabledYes: {
    id: 'configPrefixMappings.history.enabled.yes',
    defaultMessage: 'yes',
    description: 'Change history: the mapping was enabled.',
  },
  enabledNo: {
    id: 'configPrefixMappings.history.enabled.no',
    defaultMessage: 'no',
    description: 'Change history: the mapping was disabled.',
  },
  noFieldChange: {
    id: 'configPrefixMappings.history.noFieldChange',
    defaultMessage: 'Saved with no change to its settings',
    description: 'Change history detail: the mapping was saved again with the same profile, revision, conflict mode and description.',
  },
  empty: {
    id: 'configPrefixMappings.history.empty',
    defaultMessage: 'No changes recorded.',
    description: 'Change history empty state.',
  },
  loading: {
    id: 'configPrefixMappings.history.loading',
    defaultMessage: 'Loading change history...',
    description: 'Change history loading state.',
  },
  loadOlder: {
    id: 'configPrefixMappings.history.loadOlder',
    defaultMessage: 'Load older changes',
    description: 'Button under the change history that fetches the next page of older changes.',
  },
  loadFailed: {
    id: 'configPrefixMappings.history.loadFailed',
    defaultMessage: 'Failed to load the mapping history',
    description: 'Error shown when the change history could not be read and the server gave no message.',
  },
  historyLink: {
    id: 'configPrefixMappings.history.link',
    defaultMessage: 'History',
    description: 'Row action in the mappings table that opens the change history for that one mapping.',
  },
  historyLinkAria: {
    id: 'configPrefixMappings.history.linkAria',
    defaultMessage: 'Change history of {prefix}',
    description: 'Screen-reader name of the History row action. {prefix} is the mapping prefix.',
  },
});

type Side = Partial<ConfigPrefixMapping> | null | undefined;

const PRECEDENCE_MESSAGES = {
  mapping: messages.precedenceMapping,
  metadata: messages.precedenceMetadata,
  reject: messages.precedenceReject,
} as const;

const ACTION_MESSAGES = {
  create: messages.actionCreate,
  update: messages.actionUpdate,
  delete: messages.actionDelete,
} as const;

/** Row action for the mappings table: open this mapping's history. */
export const MappingHistoryLink = ({ prefix, onClick }: { prefix: string; onClick: () => void }): React.JSX.Element => {
  const intl = useIntl();
  return (
    <Button variant="inline-link" onClick={onClick} ariaLabel={intl.formatMessage(messages.historyLinkAria, { prefix })}>
      {intl.formatMessage(messages.historyLink)}
    </Button>
  );
};

export interface MappingHistoryProps {
  /** Narrow the log to one mapping's changes; null shows every mapping. */
  prefixFilter: string | null;
  onClearFilter: () => void;
  expanded: boolean;
  onExpandedChange: (expanded: boolean) => void;
  /**
   * Reloads the first page whenever this changes identity while the panel is open.
   * The page passes its mapping list, which is replaced after every save and
   * delete, so a change just made appears at the top of the log.
   */
  reloadOn?: unknown;
}

const MappingHistory = ({ prefixFilter, onClearFilter, expanded, onExpandedChange, reloadOn }: MappingHistoryProps): React.JSX.Element => {
  const intl = useIntl();
  const { formatDateTime } = useFormatters();
  const [entries, setEntries] = useState<ConfigPrefixMappingHistoryEntry[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Only the latest request may write: switching the filter while a page is in
  // flight must not append the previous filter's entries to the new one's.
  const requestId = useRef(0);

  const load = useCallback(
    async (before: number | null) => {
      const id = ++requestId.current;
      setLoading(true);
      setError(null);
      try {
        const page = await fetchMappingHistory(prefixFilter, before);
        if (id !== requestId.current) return;
        setEntries((current) => (before === null ? page.entries : [...current, ...page.entries]));
        setNextBefore(page.nextBefore);
      } catch (err) {
        if (id !== requestId.current) return;
        setError(err instanceof Error && err.message ? err.message : intl.formatMessage(messages.loadFailed));
        if (before === null) setEntries([]);
      } finally {
        if (id === requestId.current) setLoading(false);
      }
    },
    [prefixFilter, intl],
  );

  // A fresh first page whenever the panel opens or the filter changes. Nothing is
  // fetched while it is closed.
  useEffect(() => {
    if (expanded) void load(null);
  }, [expanded, load, reloadOn]);

  const revisionText = (side: Side): string =>
    side?.configRevision === null || side?.configRevision === undefined
      ? intl.formatMessage(messages.published)
      : intl.formatMessage(messages.revision, { revision: side.configRevision });

  // An unrecognised value is shown as stored rather than hidden: it is data the
  // server returned, and dropping it would misreport the change.
  const precedenceText = (value: string | null | undefined): string => {
    const message = PRECEDENCE_MESSAGES[(value ?? 'mapping') as keyof typeof PRECEDENCE_MESSAGES];
    return message ? intl.formatMessage(message) : (value ?? '');
  };

  const details = (entry: ConfigPrefixMappingHistoryEntry): string[] => {
    const { before, after } = entry;
    if (!before && after) {
      return [
        intl.formatMessage(messages.created, {
          profile: after.configProfile ?? '',
          revision: revisionText(after),
          precedence: precedenceText(after.metadataPrecedence),
        }),
      ];
    }
    if (before && !after) {
      return [intl.formatMessage(messages.deleted, { profile: before.configProfile ?? '', revision: revisionText(before) })];
    }
    const lines: string[] = [];
    if (before?.configProfile !== after?.configProfile) {
      lines.push(intl.formatMessage(messages.changedProfile, { from: before?.configProfile ?? '', to: after?.configProfile ?? '' }));
    }
    if ((before?.configRevision ?? null) !== (after?.configRevision ?? null)) {
      lines.push(intl.formatMessage(messages.changedRevision, { from: revisionText(before), to: revisionText(after) }));
    }
    if (before?.metadataPrecedence !== after?.metadataPrecedence) {
      lines.push(
        intl.formatMessage(messages.changedPrecedence, {
          from: precedenceText(before?.metadataPrecedence),
          to: precedenceText(after?.metadataPrecedence),
        }),
      );
    }
    if ((before?.enabled ?? true) !== (after?.enabled ?? true)) {
      const yesNo = (v: boolean | null | undefined) => intl.formatMessage((v ?? true) ? messages.enabledYes : messages.enabledNo);
      lines.push(intl.formatMessage(messages.changedEnabled, { from: yesNo(before?.enabled), to: yesNo(after?.enabled) }));
    }
    if ((before?.description ?? null) !== (after?.description ?? null)) {
      lines.push(intl.formatMessage(messages.changedDescription));
    }
    return lines.length ? lines : [intl.formatMessage(messages.noFieldChange)];
  };

  const columnDefinitions = [
    {
      id: 'when',
      header: intl.formatMessage(messages.colWhen),
      cell: (e: ConfigPrefixMappingHistoryEntry) => (e.timestamp ? formatDateTime(new Date(e.timestamp)) : '—'),
    },
    { id: 'who', header: intl.formatMessage(messages.colWho), cell: (e: ConfigPrefixMappingHistoryEntry) => e.actor || '—' },
    {
      id: 'mapping',
      header: intl.formatMessage(messages.colMapping),
      cell: (e: ConfigPrefixMappingHistoryEntry) => <code>{e.prefix}</code>,
    },
    {
      id: 'change',
      header: intl.formatMessage(messages.colChange),
      cell: (e: ConfigPrefixMappingHistoryEntry) => {
        const message = ACTION_MESSAGES[e.action as keyof typeof ACTION_MESSAGES];
        return message ? intl.formatMessage(message) : e.action;
      },
    },
    {
      id: 'details',
      header: intl.formatMessage(messages.colDetails),
      cell: (e: ConfigPrefixMappingHistoryEntry) => (
        <SpaceBetween size="xxxs">
          {details(e).map((line) => (
            <Box key={line} fontSize="body-s">
              {line}
            </Box>
          ))}
        </SpaceBetween>
      ),
    },
  ];

  return (
    <ExpandableSection
      variant="container"
      headerText={intl.formatMessage(messages.header)}
      headerDescription={intl.formatMessage(messages.description)}
      expanded={expanded}
      onChange={({ detail }) => onExpandedChange(detail.expanded)}
    >
      <SpaceBetween size="m">
        {prefixFilter && (
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <Box>{intl.formatMessage(messages.filteredTo, { prefix: prefixFilter })}</Box>
            <Button variant="inline-link" onClick={onClearFilter}>
              {intl.formatMessage(messages.showAll)}
            </Button>
          </SpaceBetween>
        )}
        {error && <Alert type="error">{error}</Alert>}
        <Table
          columnDefinitions={columnDefinitions}
          items={entries}
          trackBy={(e) => `${e.seq}:${e.prefix}`}
          loading={loading && entries.length === 0}
          loadingText={intl.formatMessage(messages.loading)}
          variant="embedded"
          empty={
            <Box textAlign="center" padding="s">
              {intl.formatMessage(messages.empty)}
            </Box>
          }
        />
        {nextBefore !== null && (
          <Button onClick={() => void load(nextBefore)} loading={loading}>
            {intl.formatMessage(messages.loadOlder)}
          </Button>
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
};

export default MappingHistory;
