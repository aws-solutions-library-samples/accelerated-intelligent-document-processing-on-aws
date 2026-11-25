// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Troubleshoot's error across a language switch. The minimized chip keeps a job running while
 * the language menu stays usable, so a job can fail under one language and be read under
 * another: the error the dialog words itself must read in the language shown, not in the one
 * it failed under.
 */

import React from 'react';
import { act, screen } from '@testing-library/react';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import { setFixtureCatalog } from '../../../test-utils/i18nCatalogsMock';

const graphql = vi.fn();
vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../hooks/use-deployment-context', () => ({ default: () => ({}) }));
vi.mock('../../document-agents-layout/AgentResultDisplay', () => ({ default: () => null }));
vi.mock('../../document-agents-layout/AgentMessagesDisplay', () => ({ default: () => null }));

import TroubleshootModal from '../TroubleshootModal';

const DOC = { objectKey: 'in/a.pdf', objectStatus: 'FAILED' };

beforeEach(() => {
  vi.useFakeTimers();
  // The job fails without a reason of its own, so the dialog shows its own text.
  graphql.mockReset().mockResolvedValue({ data: { getAgentJobStatus: { status: 'FAILED', agent_messages: null } } });
});

afterEach(() => {
  vi.useRealTimers();
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('en');
});

describe('Troubleshoot after a switch from en to fr-CA', () => {
  it('rewords the error of a job that failed without a reason', async () => {
    const view = renderWithIntl(
      <TroubleshootModal visible onDismiss={vi.fn()} documentItem={DOC} existingJob={{ jobId: 'job-1', status: 'PROCESSING' }} />,
      { enabledLocales: ['en', 'fr-CA'] },
    );
    // One poll, two seconds after the reopen. Real timers return before the first findBy*, whose
    // retries run on timers.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    vi.useRealTimers();
    expect(await screen.findByText('Job processing failed')).toBeInTheDocument();

    setFixtureCatalog('fr-CA', { 'documents.troubleshoot.jobFailed': 'Le traitement de la tâche a échoué' });
    view.setLocale('fr-CA');
    expect(await screen.findByText('Le traitement de la tâche a échoué')).toBeInTheDocument();
    expect(screen.queryByText('Job processing failed')).toBeNull();
  });
});
