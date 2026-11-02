// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Troubleshoot reopened on the same document. Each run is a paid agent job, so what has
 * to hold: a finished job (COMPLETED or FAILED) is shown again on reopen, with no new
 * job; Run again starts exactly one; a job still running is resumed, not restarted; and
 * with no job yet, opening starts one.
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../hooks/use-deployment-context', () => ({ default: () => ({}) }));
vi.mock('../../document-agents-layout/AgentResultDisplay', () => ({
  default: ({ result }: { result: string }) => <div data-testid="agent-result">{String(result)}</div>,
}));
vi.mock('../../document-agents-layout/AgentMessagesDisplay', () => ({ default: () => null }));

import TroubleshootModal from '../TroubleshootModal';

const sent = (op: string) => graphql.mock.calls.filter(([arg]) => String(arg.query).includes(op)).length;
const DOC = { objectKey: 'in/a.pdf', objectStatus: 'FAILED' };

beforeEach(() => {
  document.body.innerHTML = '';
  graphql.mockReset().mockImplementation(async ({ query }: { query: string }) => {
    if (String(query).includes('listAvailableAgents')) return { data: { listAvailableAgents: [{ agent_id: 'Error-Analyzer-Agent' }] } };
    if (String(query).includes('submitAgentQuery')) return { data: { submitAgentQuery: { jobId: 'job-2', status: 'PENDING' } } };
    return { data: {} };
  });
});

const runAgain = () =>
  createWrapper(document.body)
    .findModal()!
    .findFooter()!
    .findAllButtons()
    .find((b) => b.getElement().textContent === 'Run again');

describe('Troubleshoot reopened', () => {
  it('shows a finished job’s result without starting a new job, and Run again starts exactly one', async () => {
    render(
      <TroubleshootModal
        visible
        onDismiss={vi.fn()}
        documentItem={DOC}
        existingJob={{ jobId: 'job-1', status: 'COMPLETED', result: 'The OCR step timed out.' }}
      />,
    );
    await waitFor(() => expect(document.querySelector('[data-testid="agent-result"]')?.textContent).toBe('The OCR step timed out.'));
    expect(sent('listAvailableAgents')).toBe(0);
    expect(sent('submitAgentQuery')).toBe(0);
    act(() => runAgain()!.click());
    await waitFor(() => expect(sent('submitAgentQuery')).toBe(1));
  });

  it('shows a failed job’s error without starting a new job', async () => {
    render(
      <TroubleshootModal
        visible
        onDismiss={vi.fn()}
        documentItem={DOC}
        existingJob={{ jobId: 'job-1', status: 'FAILED', error: 'Agent unavailable' }}
      />,
    );
    await waitFor(() => expect(document.body.textContent).toContain('Agent unavailable'));
    expect(sent('submitAgentQuery')).toBe(0);
    expect(runAgain()).toBeDefined();
  });

  it('resumes a job still running', async () => {
    render(<TroubleshootModal visible onDismiss={vi.fn()} documentItem={DOC} existingJob={{ jobId: 'job-1', status: 'PROCESSING' }} />);
    await waitFor(() => expect(document.body.textContent).toContain('Status: PROCESSING'));
    expect(sent('submitAgentQuery')).toBe(0);
    expect(runAgain()).toBeUndefined();
  });

  it('starts one job when there is none yet', async () => {
    render(<TroubleshootModal visible onDismiss={vi.fn()} documentItem={DOC} />);
    await waitFor(() => expect(sent('submitAgentQuery')).toBe(1));
  });
});
