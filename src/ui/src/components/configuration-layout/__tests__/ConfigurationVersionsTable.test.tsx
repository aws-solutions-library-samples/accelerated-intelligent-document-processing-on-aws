// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for the Configuration Profiles table.
 *
 * Why a *source-inspecting* test sits alongside the render tests
 * -------------------------------------------------------------
 * This table shipped a layout bug that every automated check missed: its column
 * widths asked for 90% in percentages PLUS another 300px in fixed columns, so
 * every column was squeezed below its content and the rows wrapped — eating
 * vertical space above the configuration editor. Lint passed, `tsc` passed, all
 * 352 UI tests passed, and a live deploy test passed. A human looking at the page
 * found it.
 *
 * It is invisible to jsdom too: there is no real layout engine, so a render test
 * cannot observe a squeezed column. The width budget is therefore asserted
 * against the source, which is the only place the over-commitment is visible.
 * (Static source assertions have precedent here — see the vendored-module drift
 * test and scripts/sdlc/tests/test_resolver_layer_coverage.py.)
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import ConfigurationVersionsTable from '../ConfigurationVersionsTable';

const SOURCE = readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), '../ConfigurationVersionsTable.tsx'), 'utf8');

const PROFILES = [
  {
    versionName: 'default',
    isActive: true,
    managed: true,
    description: 'Lending package sample - payslips, licenses, bank statements, W2s',
    createdAt: '2026-08-28T20:00:00Z',
    updatedAt: '2026-08-29T15:38:00Z',
    latestRevision: 2,
    publishedRevision: 2,
  },
  {
    versionName: 'lending',
    description: 'Tuned for commercial lending packets',
    createdAt: '2026-08-29T10:00:00Z',
    updatedAt: '2026-08-29T16:02:00Z',
    latestRevision: 7,
    publishedRevision: 7,
  },
];

describe('ConfigurationVersionsTable — width budget', () => {
  /** Every `width:` declaration in the column definitions. */
  const widths = [...SOURCE.matchAll(/width:\s*(?:'(\d+)%'|(\d+))/g)].map(([, pct, px]) =>
    pct ? { kind: 'percent' as const, value: Number(pct) } : { kind: 'pixels' as const, value: Number(px) },
  );

  it('declares no percentage widths', () => {
    // The rule the fix established: fixed widths ONLY for columns whose content
    // has a predictable size; free-text columns get none and flex into what is
    // left. Mixing percentages with fixed columns is what over-committed the
    // budget, and it cannot be bounded because the two units do not add up.
    const percentages = widths.filter((w) => w.kind === 'percent');
    expect(percentages, `found percentage widths: ${JSON.stringify(percentages)}`).toHaveLength(0);
  });

  it('leaves at least two columns free to absorb the remaining space', () => {
    // Profile Name and Description carry free text. If every column were pinned,
    // the row would either overflow or squeeze regardless of the totals.
    const columnIds = [...SOURCE.matchAll(/^\s{4,6}id: '([a-zA-Z]+)',$/gm)].map(([, id]) => id);
    const withWidth = widths.length;
    expect(columnIds.length).toBeGreaterThanOrEqual(5);
    expect(columnIds.length - withWidth).toBeGreaterThanOrEqual(2);
  });

  it('keeps the fixed columns within a sane share of a narrow viewport', () => {
    // Guards the other direction: pinning too much leaves nothing for the text
    // columns on a 1024px-wide window.
    const fixedTotal = widths.filter((w) => w.kind === 'pixels').reduce((sum, w) => sum + w.value, 0);
    expect(fixedTotal).toBeLessThan(1024 * 0.75);
  });

  it('renders rows at compact density', () => {
    // The table sits ABOVE the editor, so row height is what pushes the thing you
    // came to edit off-screen.
    expect(SOURCE).toContain('contentDensity="compact"');
  });
});

describe('ConfigurationVersionsTable — default columns', () => {
  it('shows Updated but not Created by default', () => {
    render(<ConfigurationVersionsTable versions={PROFILES} />);
    expect(screen.getByText('Updated')).toBeInTheDocument();
    // Two timestamps cost a whole column to answer what Updated already answers,
    // and the extra column is what squeezed the others.
    expect(screen.queryByText('Created')).not.toBeInTheDocument();
  });

  it('still offers Created in the preferences gear', () => {
    // Hidden by default is not the same as removed — someone auditing when a
    // profile was first created still needs it.
    expect(SOURCE).toMatch(/id: 'createdAt', label: 'Created'/);
    const defaults = SOURCE.match(/visibleContent: \[([^\]]+)\]/);
    expect(defaults?.[1]).not.toContain('createdAt');
  });

  it('does not wrap lines by default', () => {
    expect(SOURCE).toMatch(/wrapLines: false/);
  });

  it('renders the expected default column set', () => {
    render(<ConfigurationVersionsTable versions={PROFILES} />);
    for (const header of ['Profile Name', 'Type', 'Description', 'Updated', 'History']) {
      expect(screen.getByText(header)).toBeInTheDocument();
    }
  });
});

describe('ConfigurationVersionsTable — create profile action', () => {
  it('offers Create profile to admins', () => {
    // Creating a profile from an existing one used to be reachable only from the
    // editor's Actions menu, which meant opening the source profile first.
    const onCreateProfile = vi.fn();
    render(<ConfigurationVersionsTable versions={PROFILES} isAdmin onCreateProfile={onCreateProfile} />);
    screen.getByText('Create profile').click();
    expect(onCreateProfile).toHaveBeenCalled();
  });

  it('hides Create profile from non-admins', () => {
    // Profile creation writes configuration, so it carries the same Admin gate as
    // Import and the editor's save-as action.
    render(<ConfigurationVersionsTable versions={PROFILES} onCreateProfile={vi.fn()} />);
    expect(screen.queryByText('Create profile')).not.toBeInTheDocument();
  });

  it('does not make the destructive Delete action the primary button', () => {
    // Delete was variant="primary", making the visual default on a table whose
    // main job is creating and opening profiles a destructive one.
    const labelAt = SOURCE.indexOf('Delete Selected ({selectedVersionsForCompare.length})');
    const deleteButton = SOURCE.slice(SOURCE.lastIndexOf('<Button', labelAt), labelAt);
    expect(deleteButton).toContain('variant="normal"');
    expect(deleteButton).not.toContain('variant="primary"');
  });
});

describe('ConfigurationVersionsTable — revision history entry point', () => {
  it('shows the retained revision count per profile', () => {
    render(<ConfigurationVersionsTable versions={PROFILES} />);
    expect(screen.getByText('2 revisions')).toBeInTheDocument();
    expect(screen.getByText('7 revisions')).toBeInTheDocument();
  });

  it('says "History" for a profile with no revisions rather than "0 revisions"', () => {
    // A profile untouched since the upgrade has no history; "0 revisions" would
    // read as data loss rather than "nothing recorded yet".
    render(<ConfigurationVersionsTable versions={[{ versionName: 'untouched' }]} />);
    expect(screen.getByText('History', { selector: 'span' })).toBeInTheDocument();
  });

  it('opens the history for the profile whose row was clicked', () => {
    const onShowHistory = vi.fn();
    render(<ConfigurationVersionsTable versions={PROFILES} onShowHistory={onShowHistory} />);
    screen.getByText('7 revisions').click();
    expect(onShowHistory).toHaveBeenCalledWith('lending');
  });
});
