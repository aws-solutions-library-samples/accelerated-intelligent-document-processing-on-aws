// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/* eslint-disable react/no-array-index-key */

import React from 'react';
import { Table, Box, SpaceBetween, Badge, Header, ButtonDropdown } from '@cloudscape-design/components';

interface ConfigurationComparisonProps {
  versions: string[];
  configs: Record<string, unknown>;
}

// --- Word-level diff (used when exactly two versions are compared) -----------
// Tokenize keeping whitespace so re-joined output is faithful to the original.
const tokenize = (s: string): string[] => s.match(/\s+|\S+/g) ?? [];

type DiffToken = { value: string; type: 'common' | 'removed' | 'added' };

// Longest-common-subsequence word diff. Skipped for very large inputs to avoid
// the O(n*m) table blowing up (falls back to plain full-text rendering).
const MAX_DIFF_TOKENS = 4000;
const diffTokens = (a: string, b: string): DiffToken[] | null => {
  const at = tokenize(a);
  const bt = tokenize(b);
  if (at.length > MAX_DIFF_TOKENS || bt.length > MAX_DIFF_TOKENS) return null;

  const n = at.length;
  const m = bt.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = at[i] === bt[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out: DiffToken[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (at[i] === bt[j]) {
      out.push({ value: at[i], type: 'common' });
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ value: at[i], type: 'removed' });
      i += 1;
    } else {
      out.push({ value: bt[j], type: 'added' });
      j += 1;
    }
  }
  while (i < n) {
    out.push({ value: at[i], type: 'removed' });
    i += 1;
  }
  while (j < m) {
    out.push({ value: bt[j], type: 'added' });
    j += 1;
  }
  return out;
};

// Render one side of a two-version diff: `side='left'` shows common + removed
// tokens (what the first version has), `side='right'` shows common + added.
const renderDiffSide = (tokens: DiffToken[], side: 'left' | 'right'): React.ReactNode => {
  const dropType = side === 'left' ? 'added' : 'removed';
  const markType = side === 'left' ? 'removed' : 'added';
  const markStyle: React.CSSProperties =
    side === 'left'
      ? { backgroundColor: '#fdd8d8', textDecoration: 'line-through', color: '#8b0000' }
      : { backgroundColor: '#d6f5d6', color: '#0a5a0a' };
  return (
    <Box>
      <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'monospace', fontSize: '12px' }}>
        {tokens
          .filter((t) => t.type !== dropType)

          .map((t, idx) =>
            t.type === markType ? (
              <mark key={idx} style={markStyle}>
                {t.value}
              </mark>
            ) : (
              <span key={idx}>{t.value}</span>
            ),
          )}
      </span>
    </Box>
  );
};

// Scrollable, wrapping container for full-length text/JSON values (no truncation).
const scrollBoxStyle: React.CSSProperties = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  fontFamily: 'monospace',
  fontSize: '12px',
  maxHeight: '240px',
  overflow: 'auto',
  display: 'block',
};

const ConfigurationComparison = ({ versions, configs }: ConfigurationComparisonProps): React.JSX.Element => {
  // Safety checks
  if (!versions || !Array.isArray(versions) || !configs) {
    return (
      <Box textAlign="center" color="inherit">
        <SpaceBetween size="m">
          <b>Invalid comparison data</b>
          <Box variant="p">Unable to compare configurations due to missing data.</Box>
        </SpaceBetween>
      </Box>
    );
  }

  // Find differences between configurations with deep diff support for classes and rules
  const findDifferences = (configsToCompare: Record<string, unknown>) => {
    const differences: { field: string; values: Record<string, string> }[] = [];
    const ignoredFields = new Set(['UpdatedAt', 'Description', 'CreatedAt', 'IsActive', 'Configuration', 'version_name']);

    // Check if an array contains identity-keyed objects (e.g., classes, rule_classes with $id fields)
    const isIdentityKeyedArray = (arr: unknown): boolean => {
      return Array.isArray(arr) && arr.length > 0 && arr.every((item) => typeof item === 'object' && item !== null && '$id' in item);
    };

    // Recursively extract all leaf paths from a value, using $id-based keys for identity arrays
    const getPathsFromValue = (value: unknown, currentPath: string): string[] => {
      const paths: string[] = [];

      if (Array.isArray(value)) {
        if (isIdentityKeyedArray(value)) {
          // Identity-keyed array (classes, rule_classes): use $id as key segment
          for (const item of value) {
            const itemPrefix = `${currentPath}[${item['$id']}]`;
            for (const [propKey, propValue] of Object.entries(item)) {
              if (propKey === '$id') continue; // Skip the identity key itself
              getPathsFromValue(propValue, `${itemPrefix}.${propKey}`).forEach((p) => paths.push(p));
            }
          }
        } else {
          // Regular array: treat as leaf value (will be stringified for comparison)
          paths.push(currentPath);
        }
      } else if (typeof value === 'object' && value !== null) {
        for (const [key, val] of Object.entries(value)) {
          if (ignoredFields.has(key)) continue;
          const childPath = currentPath ? `${currentPath}.${key}` : key;
          getPathsFromValue(val, childPath).forEach((p) => paths.push(p));
        }
      } else {
        paths.push(currentPath);
      }

      return paths;
    };

    // Get nested value using dot notation path with identity-keyed array bracket support
    // Supports paths like: "classes[Payslip].description" or "rule_classes[global_periods].rule_properties.field"
    const getNestedValue = (dictionary: Record<string, unknown>, path: string): unknown => {
      const parts = path.split('.');
      let current: Record<string, unknown> | null = dictionary;

      for (const part of parts) {
        if (current === null || current === undefined) return null;

        // Check for identity bracket notation: fieldName[id]
        const bracketMatch = part.match(/^(.+?)\[(.+)\]$/);
        if (bracketMatch) {
          const arrayKey = bracketMatch[1];
          const id = bracketMatch[2];
          const arr = current[arrayKey];
          if (!Array.isArray(arr)) return null;
          current = (arr.find((item) => item && item['$id'] === id) || null) as Record<string, unknown> | null;
        } else if (typeof current === 'object' && current !== null && part in current) {
          // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop
          current = current[part] as Record<string, unknown> | null;
        } else {
          return null;
        }
      }
      return current;
    };

    // Get all possible paths from all configs
    const allPaths = new Set<string>();
    Object.values(configsToCompare).forEach((config) => {
      getPathsFromValue(config || {}, '').forEach((path) => {
        if (path) allPaths.add(path);
      });
    });

    // Sort paths for consistent ordering
    const sortedPaths = Array.from(allPaths).sort();

    // Stringify a value for comparison (handles arrays, objects, primitives)
    const stringifyValue = (value: unknown): string => {
      if (value === null || value === undefined) return '<missing>';
      if (typeof value === 'string') return value.trim();
      if (typeof value === 'object') return JSON.stringify(value);
      return String(value).trim();
    };

    // Numeric-aware comparison: treats "5" and "5.0" as equal
    const isNumeric = (v: unknown): boolean => {
      if (v === '<missing>') return false;
      if (typeof v === 'number') return true;
      if (typeof v === 'string' && v.trim() !== '') return !Number.isNaN(Number(v));
      return false;
    };
    const areStrValuesEqual = (a: string, b: string): boolean => {
      if (a === b) return true;
      if (isNumeric(a) && isNumeric(b)) return Number(a) === Number(b);
      return false;
    };

    // Check each path for differences
    sortedPaths.forEach((path) => {
      const values: Record<string, string> = {};
      let hasDifferences = false;
      let firstValue: string | null = null;
      let firstValueSet = false;

      versions.forEach((version) => {
        const config = configsToCompare[version];
        const value = getNestedValue((config || {}) as Record<string, unknown>, path);
        const strValue = stringifyValue(value);

        values[version] = strValue;

        // Check for differences using numeric-aware comparison
        if (!firstValueSet) {
          firstValue = strValue;
          firstValueSet = true;
        } else if (!areStrValuesEqual(firstValue ?? '', strValue)) {
          hasDifferences = true;
        }
      });

      // Include if there are differences (including missing vs present)
      if (hasDifferences) {
        differences.push({
          field: path,
          values,
        });
      }
    });

    return differences.sort((a, b) => a.field.localeCompare(b.field));
  };

  // Pretty-print a stored comparison string: if it's JSON, expand it with
  // indentation; otherwise return as-is. Keeps full content (no truncation).
  const prettify = (value: string): string => {
    if ((value.startsWith('{') && value.endsWith('}')) || (value.startsWith('[') && value.endsWith(']'))) {
      try {
        return JSON.stringify(JSON.parse(value), null, 2);
      } catch {
        // not valid JSON — fall through
      }
    }
    return value;
  };

  // Format a single value for display (used for 3+ version comparisons, where a
  // pairwise diff doesn't apply). Shows the full value in a scrollable box.
  const formatValue = (value: unknown): React.ReactNode => {
    if (value === '<missing>') return <Badge color="grey">Missing</Badge>;
    if (value === undefined) return <Badge color="grey">Not set</Badge>;
    if (value === null) return <Badge color="grey">null</Badge>;
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    const text = prettify(String(value));
    return <span style={scrollBoxStyle}>{text}</span>;
  };

  // Whether a stored value is "present" (renders as an absence badge otherwise).
  const isAbsent = (value: string): boolean => value === '<missing>';

  // Render a diff cell for the two-version case. `side` picks which version's
  // perspective this column shows.
  const renderDiffCell = (item: { field: string; values: Record<string, string> }, side: 'left' | 'right'): React.ReactNode => {
    const [va, vb] = versions;
    const aVal = item.values[va];
    const bVal = item.values[vb];
    const own = side === 'left' ? aVal : bVal;
    if (isAbsent(own)) return <Badge color="grey">Missing</Badge>;
    // If either side is missing, there's nothing to diff against — show full value.
    if (isAbsent(aVal) || isAbsent(bVal)) return <span style={scrollBoxStyle}>{prettify(own)}</span>;
    const tokens = diffTokens(prettify(aVal), prettify(bVal));
    if (!tokens) return <span style={scrollBoxStyle}>{prettify(own)}</span>;
    return <Box>{renderDiffSide(tokens, side)}</Box>;
  };

  // Now configs are already merged, no need to parse old format
  const differences = findDifferences(configs);

  // Export functions
  const exportToCSV = () => {
    const headers = ['Field', ...versions];
    const rows = differences.map((diff) => [diff.field, ...versions.map((version) => diff.values[version] || 'Not set')]);

    const csvContent = [headers, ...rows].map((row) => row.map((cell) => `"${cell}"`).join(',')).join('\n');

    const blob = new Blob([csvContent], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `config-differences-${versions.join('-')}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const exportToJSON = () => {
    const exportData = {
      versions,
      differences: differences.map((diff) => ({
        field: diff.field,
        values: diff.values,
      })),
    };

    const jsonContent = JSON.stringify(exportData, null, 2);
    const blob = new Blob([jsonContent], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `config-differences-${versions.join('-')}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  // Create column definitions with equal width distribution
  const totalColumns = versions.length + 1; // +1 for field column
  const equalWidth = Math.floor(100 / totalColumns);

  // Two-version comparisons get a word-level diff (added=green, removed=red);
  // 3+ versions fall back to showing each full value side by side.
  const isPairwiseDiff = versions.length === 2;

  const columnDefinitions = [
    {
      id: 'field',
      header: 'Field',
      cell: (item: { field: string; values: Record<string, string> }) => item.field,
      sortingField: 'field',
      width: `${equalWidth}%`,
    },
    ...versions.map((version, index) => ({
      id: version,
      header: version,
      cell: (item: { field: string; values: Record<string, string> }) =>
        isPairwiseDiff ? renderDiffCell(item, index === 0 ? 'left' : 'right') : formatValue(item.values[version]),
      width: `${equalWidth}%`,
    })),
  ];

  return (
    <SpaceBetween size="m">
      {differences.length === 0 ? (
        <Box textAlign="center" color="inherit">
          <SpaceBetween size="m">
            <b>No differences found</b>
            <Box variant="p">The selected configuration profiles are identical.</Box>
          </SpaceBetween>
        </Box>
      ) : (
        <Table
          resizableColumns
          wrapLines={true}
          columnDefinitions={columnDefinitions}
          items={differences}
          sortingDisabled={false}
          header={
            <Header
              actions={
                <ButtonDropdown
                  items={[
                    { text: 'Export as CSV', id: 'csv' },
                    { text: 'Export as JSON', id: 'json' },
                  ]}
                  onItemClick={({ detail }) => {
                    if (detail.id === 'csv') exportToCSV();
                    if (detail.id === 'json') exportToJSON();
                  }}
                >
                  Export
                </ButtonDropdown>
              }
            >
              Config differences between versions: [{versions.join(', ')}]
            </Header>
          }
          empty={
            <Box textAlign="center" color="inherit">
              No differences found
            </Box>
          }
        />
      )}
    </SpaceBetween>
  );
};

export default ConfigurationComparison;
