// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The format the document page's Document Sections and Document Pages tables share: the
// ⋮ Actions column last, fixed at ACTIONS_COLUMN_WIDTH on the right, and the visible data
// columns sharing the rest of the table's width evenly; and a ⚙ preferences control,
// as on the Documents list, that shows or hides the data columns, remembered per table.
import React from 'react';
import { CollectionPreferences } from '@cloudscape-design/components';
import type { CollectionPreferencesProps, TableProps } from '@cloudscape-design/components';

import useLocalStorage from './local-storage';

export const ACTIONS_COLUMN_ID = 'actions';
export const ACTIONS_COLUMN_WIDTH = 100;

/**
 * The columns with their widths: Actions fixed, each visible data column an even share of
 * what is left. Hidden columns are left as they are; the table does not render them.
 */
export const fillTableColumns = <T,>(
  columns: TableProps.ColumnDefinition<T>[],
  visibleColumns: readonly string[],
): TableProps.ColumnDefinition<T>[] => {
  const shown = columns.filter((c) => c.id !== ACTIONS_COLUMN_ID && visibleColumns.includes(c.id ?? ''));
  const share = `calc((100% - ${ACTIONS_COLUMN_WIDTH}px) / ${Math.max(shown.length, 1)})`;
  return columns.map((c) =>
    c.id === ACTIONS_COLUMN_ID
      ? { ...c, width: ACTIONS_COLUMN_WIDTH, minWidth: ACTIONS_COLUMN_WIDTH }
      : { ...c, width: share, minWidth: undefined },
  );
};

interface ColumnOption {
  id: string;
  label: string;
}

/**
 * The table's column preferences, kept in local storage under `storageKey`: which data
 * columns are visible (all, until changed). Actions is always visible.
 */
export const useColumnPreferences = (
  storageKey: string,
  options: readonly ColumnOption[],
): {
  preferences: CollectionPreferencesProps.Preferences;
  setPreferences: (next: CollectionPreferencesProps.Preferences) => void;
  visibleColumns: string[];
} => {
  const all = options.map((o) => o.id);
  const [preferences, setPreferences] = useLocalStorage<CollectionPreferencesProps.Preferences>(storageKey, { visibleContent: all });
  // A stored id no longer offered is dropped, and an empty choice falls back to every column.
  const chosen = (preferences.visibleContent ?? all).filter((id) => all.includes(id));
  const visibleColumns = [...(chosen.length > 0 ? chosen : all), ACTIONS_COLUMN_ID];
  return { preferences, setPreferences, visibleColumns };
};

interface ColumnPreferencesProps {
  /** The table's name, for the control's accessible label. */
  title: string;
  options: readonly ColumnOption[];
  preferences: CollectionPreferencesProps.Preferences;
  setPreferences: (next: CollectionPreferencesProps.Preferences) => void;
}

/** The ⚙ control: Select visible columns, with Actions always shown. */
export const ColumnPreferences = ({ title, options, preferences, setPreferences }: ColumnPreferencesProps): React.JSX.Element => (
  <CollectionPreferences
    title="Preferences"
    confirmLabel="Confirm"
    cancelLabel="Cancel"
    preferences={preferences}
    onConfirm={({ detail }) => setPreferences(detail)}
    visibleContentPreference={{
      title: 'Select visible columns',
      options: [{ label: `${title} columns`, options: options.map((o) => ({ id: o.id, label: o.label })) }],
    }}
  />
);
