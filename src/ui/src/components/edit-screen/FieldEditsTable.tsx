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
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import { useFormatters } from '../../i18n/format';
import { formatDateTime } from '../../utils/date-time';

const messages = defineMessages<{
  columnWhen: NoMessageValues;
  columnBy: NoMessageValues;
  columnField: NoMessageValues;
  columnFrom: NoMessageValues;
  columnTo: NoMessageValues;
  columnAppliesTo: NoMessageValues;
  appliesToPrediction: NoMessageValues;
  appliesToBaseline: NoMessageValues;
  unknownEditor: NoMessageValues;
  emptyValue: NoMessageValues;
  emptyNoEdits: NoMessageValues;
  emptyNoSection: NoMessageValues;
}>({
  columnWhen: {
    id: 'editScreen.fieldEdits.column.when',
    defaultMessage: 'When',
    description: 'Column header of History › Field Edits on the edit page: when the edit was saved.',
  },
  columnBy: {
    id: 'editScreen.fieldEdits.column.by',
    defaultMessage: 'By',
    description: 'Column header of History › Field Edits on the edit page: who saved the edit.',
  },
  columnField: {
    id: 'editScreen.fieldEdits.column.field',
    defaultMessage: 'Field',
    description: 'Column header of History › Field Edits on the edit page: the path of the field that was changed.',
  },
  columnFrom: {
    id: 'editScreen.fieldEdits.column.from',
    defaultMessage: 'From',
    description: "Column header of History › Field Edits on the edit page: the field's value before the edit.",
  },
  columnTo: {
    id: 'editScreen.fieldEdits.column.to',
    defaultMessage: 'To',
    description: "Column header of History › Field Edits on the edit page: the field's value after the edit.",
  },
  columnAppliesTo: {
    id: 'editScreen.fieldEdits.column.appliesTo',
    defaultMessage: 'Applies to',
    description:
      'Column header of History › Field Edits on the edit page, shown when an edit changed the evaluation baseline: which values the edit changed.',
  },
  appliesToPrediction: {
    id: 'editScreen.fieldEdits.appliesTo.prediction',
    defaultMessage: 'Prediction',
    description: "Applies to cell of History › Field Edits on the edit page: the edit changed the section's extracted values.",
  },
  appliesToBaseline: {
    id: 'editScreen.fieldEdits.appliesTo.baseline',
    defaultMessage: 'Baseline',
    description: "Applies to cell of History › Field Edits on the edit page: the edit changed the section's evaluation baseline.",
  },
  unknownEditor: {
    id: 'editScreen.fieldEdits.unknownEditor',
    defaultMessage: 'Unknown',
    description: 'By cell of History › Field Edits on the edit page when the saved edit names nobody.',
  },
  emptyValue: {
    id: 'editScreen.fieldEdits.emptyValue',
    defaultMessage: '(empty)',
    description: 'From or To cell of History › Field Edits on the edit page when the field had no value.',
  },
  emptyNoEdits: {
    id: 'editScreen.fieldEdits.empty.noEdits',
    defaultMessage: 'No field has been edited in this section yet.',
    description: 'Shown in History › Field Edits on the edit page when no field of the open section has been edited.',
  },
  emptyNoSection: {
    id: 'editScreen.fieldEdits.empty.noSection',
    defaultMessage: 'Open a section to see its field edits.',
    description: 'Shown in History › Field Edits on the edit page while no section is open.',
  },
});

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
  /** Who saved the edit: their e-mail address or user id, as the entry records it; null when it names nobody. */
  by: string | null;
  field: string;
  from: unknown;
  to: unknown;
  /** Which values the edit changed: the extracted prediction or the evaluation baseline. */
  appliesTo: 'Prediction' | 'Baseline';
  /** False for an entry that recorded which fields changed but not their values. */
  hasValues: boolean;
}

/** How the Applies to column words a row's `appliesTo`. */
const appliesToMessages: Record<FieldEditRow['appliesTo'], TypedMessageDescriptor<NoMessageValues>> = {
  Prediction: messages.appliesToPrediction,
  Baseline: messages.appliesToBaseline,
};

const historyOf = (data: Record<string, unknown> | null | undefined): EditEntry[] =>
  Array.isArray(data?._editHistory) ? (data._editHistory as EditEntry[]) : [];

const rowsOf = (entry: EditEntry, index: string, side: EditSide | undefined, appliesTo: FieldEditRow['appliesTo']): FieldEditRow[] => {
  if (!side) return [];
  const base = { at: entry.timestamp ?? '', by: entry.editedByEmail || entry.editedBy || null, appliesTo };
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

const formatValue = (value: unknown, intl: IntlShape): string => {
  if (value === undefined || value === null || value === '') return intl.formatMessage(messages.emptyValue);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
};

interface FieldEditsTableProps {
  /** The open section's result; null while no section is open. */
  sectionData: Record<string, unknown> | null;
  baselineData?: Record<string, unknown> | null;
}

const FieldEditsTable = ({ sectionData, baselineData }: FieldEditsTableProps): React.JSX.Element => {
  const intl = useIntl();
  const { locale } = useFormatters();
  const rows = fieldEditRows(sectionData, baselineData);
  const showAppliesTo = rows.some((r) => r.appliesTo === 'Baseline');
  const columns: TableProps.ColumnDefinition<FieldEditRow>[] = [
    { id: 'at', header: intl.formatMessage(messages.columnWhen), cell: (r) => (r.at ? formatDateTime(r.at, locale) : '-') },
    { id: 'by', header: intl.formatMessage(messages.columnBy), cell: (r) => r.by ?? intl.formatMessage(messages.unknownEditor) },
    { id: 'field', header: intl.formatMessage(messages.columnField), cell: (r) => <code>{r.field}</code> },
    {
      id: 'from',
      header: intl.formatMessage(messages.columnFrom),
      cell: (r) => (r.hasValues ? <Box color="text-body-secondary">{formatValue(r.from, intl)}</Box> : '-'),
    },
    { id: 'to', header: intl.formatMessage(messages.columnTo), cell: (r) => (r.hasValues ? formatValue(r.to, intl) : '-') },
    ...(showAppliesTo
      ? [
          {
            id: 'appliesTo',
            header: intl.formatMessage(messages.columnAppliesTo),
            cell: (r: FieldEditRow) => intl.formatMessage(appliesToMessages[r.appliesTo]),
          },
        ]
      : []),
  ];
  return (
    <Table
      variant="embedded"
      items={rows}
      trackBy="id"
      columnDefinitions={columns}
      empty={
        <Box color="text-body-secondary">
          {sectionData ? <FormattedMessage {...messages.emptyNoEdits} /> : <FormattedMessage {...messages.emptyNoSection} />}
        </Box>
      }
    />
  );
};

export default FieldEditsTable;
