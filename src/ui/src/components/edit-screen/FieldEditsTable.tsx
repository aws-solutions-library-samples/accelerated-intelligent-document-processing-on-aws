// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// History › Field Edits on the edit screen: every saved change to the open section's
// fields, one row per field, newest first, in the same table as Review Activity.
//
// It reads the `_editHistory` entries each save path writes, whichever wrote them: the
// edit screen itself (a direct edit, a production draft), and the review API for a Test
// Studio label, which records its changes as `baselineEdits` (the label is ground truth).
// An entry that changed no field (a label confirmed as correct) is not a field edit.
import React from 'react';
import { Box, Table } from '@cloudscape-design/components';
import type { TableProps } from '@cloudscape-design/components';

import { formatDateTime } from '../../utils/date-time';

interface EditSide {
  changedFields?: string[];
  changeCount?: number;
  diffs?: Record<string, { originalValue?: unknown; newValue?: unknown }>;
}

interface EditEntry {
  timestamp?: string;
  editedBy?: string;
  editedByEmail?: string;
  predictionEdits?: EditSide;
  baselineEdits?: EditSide;
}

export interface FieldEditRow {
  id: string;
  at: string;
  by: string;
  field: string;
  from: unknown;
  to: unknown;
  /** Which values the edit changed: the extracted prediction or the evaluation baseline. */
  appliesTo: 'Prediction' | 'Baseline';
  /** False for an entry that recorded which fields changed but not their values. */
  hasValues: boolean;
}

const historyOf = (data: Record<string, unknown> | null | undefined): EditEntry[] =>
  Array.isArray(data?._editHistory) ? (data._editHistory as EditEntry[]) : [];

const rowsOf = (entry: EditEntry, index: string, side: EditSide | undefined, appliesTo: FieldEditRow['appliesTo']): FieldEditRow[] => {
  if (!side) return [];
  const base = { at: entry.timestamp ?? '', by: entry.editedByEmail || entry.editedBy || 'Unknown', appliesTo };
  const diffs = side.diffs ?? {};
  const fields = Object.keys(diffs).length > 0 ? Object.keys(diffs) : (side.changedFields ?? []);
  return fields.map((field) => ({
    ...base,
    id: `${index}-${appliesTo}-${field}`,
    field,
    from: diffs[field]?.originalValue,
    to: diffs[field]?.newValue,
    hasValues: field in diffs,
  }));
};

/**
 * The field edit rows of a section, newest first, from its prediction's and its
 * baseline's `_editHistory`. A direct edit writes the same entry to both files, so an
 * entry already read from the prediction is not read again from the baseline.
 */
export const fieldEditRows = (
  sectionData: Record<string, unknown> | null | undefined,
  baselineData?: Record<string, unknown> | null,
): FieldEditRow[] => {
  const seen = new Set<string>();
  const rows: FieldEditRow[] = [];
  const read = (entries: EditEntry[], file: string) =>
    entries.forEach((entry, i) => {
      const key = `${entry.timestamp ?? ''}|${entry.editedBy ?? ''}`;
      if (seen.has(key)) return;
      seen.add(key);
      rows.push(...rowsOf(entry, `${file}${i}`, entry.predictionEdits, 'Prediction'));
      rows.push(...rowsOf(entry, `${file}${i}`, entry.baselineEdits, 'Baseline'));
    });
  read(historyOf(sectionData), 'p');
  read(historyOf(baselineData), 'b');
  return rows.sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime());
};

const formatValue = (value: unknown): string => {
  if (value === undefined || value === null || value === '') return '(empty)';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
};

interface FieldEditsTableProps {
  /** The open section's result; null while no section is open. */
  sectionData: Record<string, unknown> | null;
  baselineData?: Record<string, unknown> | null;
}

const FieldEditsTable = ({ sectionData, baselineData }: FieldEditsTableProps): React.JSX.Element => {
  const rows = fieldEditRows(sectionData, baselineData);
  const showAppliesTo = rows.some((r) => r.appliesTo === 'Baseline');
  const columns: TableProps.ColumnDefinition<FieldEditRow>[] = [
    { id: 'at', header: 'When', cell: (r) => (r.at ? formatDateTime(r.at) : '-') },
    { id: 'by', header: 'By', cell: (r) => r.by },
    { id: 'field', header: 'Field', cell: (r) => <code>{r.field}</code> },
    {
      id: 'from',
      header: 'From',
      cell: (r) => (r.hasValues ? <Box color="text-body-secondary">{formatValue(r.from)}</Box> : '-'),
    },
    { id: 'to', header: 'To', cell: (r) => (r.hasValues ? formatValue(r.to) : '-') },
    ...(showAppliesTo ? [{ id: 'appliesTo', header: 'Applies to', cell: (r: FieldEditRow) => r.appliesTo }] : []),
  ];
  return (
    <Table
      variant="embedded"
      items={rows}
      trackBy="id"
      columnDefinitions={columns}
      empty={
        <Box color="text-body-secondary">
          {sectionData ? 'No field has been edited in this section yet.' : 'Open a section to see its field edits.'}
        </Box>
      }
    />
  );
};

export default FieldEditsTable;
