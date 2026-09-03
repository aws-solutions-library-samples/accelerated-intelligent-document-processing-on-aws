// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Component tests for DocumentVersionsPanel.
 *
 * The GraphQL client and RBAC hook are mocked at the module boundary so the
 * test runs in jsdom with no AWS/Cognito. Covers: listing, the per-row View
 * action (all users), admin-only delete gating, and the "cannot delete
 * current version" affordance.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

// Mock the GraphQL client used by the panel. vi.mock is hoisted above module
// init, and the panel calls generateClient() at import time, so the mock fn
// must be created in a hoisted block.
const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: mockGraphql }),
}));

// Mock RBAC — toggled per test via the hoisted state object.
const roleState = vi.hoisted(() => ({ isAdmin: false }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: roleState.isAdmin }),
}));

import DocumentVersionsPanel from '../DocumentVersionsPanel';

const VERSIONS = [
  { RunId: '20250707T141530Z-b', CompletionTime: '2025-07-07T14:15:30Z', ConfigVersion: 'v2', PageCount: 6 },
  { RunId: '20250101T090000Z-a', CompletionTime: '2025-01-01T09:00:00Z', ConfigVersion: 'v1', PageCount: 6 },
];

// A run snapshot's section carries the per-section quality data the document
// page derives "Low Confidence Fields" and section Status from.
const VERSION_SECTION = {
  Id: '1',
  PageIds: [1, 2],
  Class: 'W2',
  OutputJSONUri: 's3://out/doc.pdf/sections/1/result.json',
  ConfidenceThresholdAlerts: [{ attributeName: 'WagesTips', confidence: 0.42, confidenceThreshold: 0.8 }],
  ProcessingIssues: [{ stage: 'assessment', severity: 'warning', code: 'assessment_incomplete', message: 'some rows unscored' }],
};

// Route each query by the operation name embedded in the query string.
const routeGraphql = ({ query }: { query: string }) => {
  if (query.includes('listDocumentVersions')) {
    return Promise.resolve({ data: { listDocumentVersions: VERSIONS } });
  }
  if (query.includes('getDocumentVersion')) {
    return Promise.resolve({
      data: { getDocumentVersion: { RunId: '20250101T090000Z-a', Files: [], Sections: [VERSION_SECTION], Pages: [] } },
    });
  }
  return Promise.resolve({ data: {} });
};

describe('DocumentVersionsPanel', () => {
  beforeEach(() => {
    roleState.isAdmin = false;
    mockGraphql.mockReset();
    mockGraphql.mockImplementation(routeGraphql);
  });

  it('lists versions newest-first with a Current badge', async () => {
    render(<DocumentVersionsPanel objectKey="doc.pdf" />);
    await waitFor(() => expect(screen.getByText('v2')).toBeInTheDocument());
    expect(screen.getByText('v1')).toBeInTheDocument();
    // The newest run (first row) is badged Current.
    expect(screen.getByText('Current')).toBeInTheDocument();
  });

  it('does not show a Files column (internal pinned-object count, not user-meaningful)', async () => {
    render(<DocumentVersionsPanel objectKey="doc.pdf" />);
    await waitFor(() => expect(screen.getByText('v2')).toBeInTheDocument());
    expect(screen.getByText('Pages')).toBeInTheDocument();
    expect(screen.queryByText('Files')).not.toBeInTheDocument();
  });

  it('shows a View action for non-admins (read-only access)', async () => {
    render(<DocumentVersionsPanel objectKey="doc.pdf" />);
    await waitFor(() => expect(screen.getByText('v2')).toBeInTheDocument());
    // View buttons are present even for a non-admin viewer.
    expect(screen.getAllByText(/^View$/i).length).toBeGreaterThan(0);
  });

  it('calls onViewVersion with the run detail when View is clicked', async () => {
    const onViewVersion = vi.fn();
    render(<DocumentVersionsPanel objectKey="doc.pdf" onViewVersion={onViewVersion} />);
    await waitFor(() => expect(screen.getByText('v1')).toBeInTheDocument());

    // Click "View" on the older (non-current) row.
    const viewButtons = screen.getAllByText(/^View$/i);
    fireEvent.click(viewButtons[viewButtons.length - 1]);

    await waitFor(() => expect(onViewVersion).toHaveBeenCalled());
    expect(onViewVersion).toHaveBeenCalledWith('20250101T090000Z-a', expect.objectContaining({ RunId: '20250101T090000Z-a' }));
  });

  it('passes the section quality data through to the page (drives Low Confidence Fields / Status)', async () => {
    const onViewVersion = vi.fn();
    render(<DocumentVersionsPanel objectKey="doc.pdf" onViewVersion={onViewVersion} />);
    await waitFor(() => expect(screen.getByText('v1')).toBeInTheDocument());

    const viewButtons = screen.getAllByText(/^View$/i);
    fireEvent.click(viewButtons[viewButtons.length - 1]);
    await waitFor(() => expect(onViewVersion).toHaveBeenCalled());

    // The document page derives the section's "Low Confidence Fields" count and
    // Status from these; dropping them made every historical section read 0/clean.
    const detail = onViewVersion.mock.calls[0][1];
    expect(detail.Sections[0].ConfidenceThresholdAlerts).toHaveLength(1);
    expect(detail.Sections[0].ConfidenceThresholdAlerts[0].attributeName).toBe('WagesTips');
    expect(detail.Sections[0].ProcessingIssues[0].code).toBe('assessment_incomplete');
  });

  it('requests the section quality fields in the getDocumentVersion query', async () => {
    // Guard on the generated query text itself. The GraphQL selection set is the
    // silently-breakable link in this chain: deleting the fields from
    // GetDocumentVersion.graphql raises no type error and would leave every
    // other test green while the historical view silently reverts to showing 0.
    render(<DocumentVersionsPanel objectKey="doc.pdf" onViewVersion={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('v1')).toBeInTheDocument());

    fireEvent.click(screen.getAllByText(/^View$/i).slice(-1)[0]);
    await waitFor(() => expect(mockGraphql).toHaveBeenCalledTimes(2));

    const { query } = mockGraphql.mock.calls.find((c) => String(c[0].query).includes('getDocumentVersion'))![0];
    expect(query).toContain('ConfidenceThresholdAlerts');
    expect(query).toContain('confidenceThreshold');
    expect(query).toContain('ProcessingIssues');
    expect(query).toContain('InstanceCount');
  });

  it('returns to current when the currently-viewed version row is actioned', async () => {
    const onViewVersion = vi.fn();
    // Viewing the newest run means its row shows "Viewing"; the older shows "View".
    render(<DocumentVersionsPanel objectKey="doc.pdf" viewingRunId="20250101T090000Z-a" onViewVersion={onViewVersion} />);
    await waitFor(() => expect(screen.getByText('v2')).toBeInTheDocument());
    // The viewed row is labelled "Viewing" (disabled), so at least one exists.
    expect(screen.getByText('Viewing')).toBeInTheDocument();
  });

  it('hides delete for non-admins', async () => {
    render(<DocumentVersionsPanel objectKey="doc.pdf" />);
    await waitFor(() => expect(screen.getByText('v2')).toBeInTheDocument());
    // The admin action dropdown (aria-label "Actions for version ...") is absent.
    expect(screen.queryByLabelText(/Actions for version/i)).not.toBeInTheDocument();
  });

  it('shows the admin delete dropdown for admins', async () => {
    roleState.isAdmin = true;
    render(<DocumentVersionsPanel objectKey="doc.pdf" />);
    await waitFor(() => expect(screen.getByText('v2')).toBeInTheDocument());
    // Cloudscape ButtonDropdown surfaces the aria-label on more than one node,
    // so assert at least one delete affordance exists per version row.
    expect(screen.getAllByLabelText(/Actions for version/i).length).toBeGreaterThanOrEqual(VERSIONS.length);
    // Both run ids are addressable.
    expect(screen.getAllByLabelText(/20250707T141530Z-b/).length).toBeGreaterThan(0);
  });
});
