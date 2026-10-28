// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A queued document's page offers one action into its review: Edit, which opens the
 * document's edit page (and from there its queue item) for every role that can work
 * queue items, not only Admins. There is no separate Review button, no link straight to
 * a queue item, and no legacy Start Review on a queued document.
 */

import React from 'react';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ role: {} as Record<string, unknown> }));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({ versions: [], loaded: true, loading: false, error: null }),
}));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => ({ mergedConfig: {}, loading: false }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => ({ pricing: null, nonChargeableUnits: [], loading: false }) }));
vi.mock('../../../hooks/use-polling', () => ({ default: () => undefined }));
vi.mock('../../../hooks/use-classification-comparison', () => ({ default: () => null }));
vi.mock('../../../contexts/app', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/app')>()),
  default: () => ({ currentCredentials: null, setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../sections-panel', () => ({ default: () => null }));
vi.mock('../../pages-panel', () => ({ default: () => null }));
vi.mock('../../chat-panel', () => ({ default: () => null }));
vi.mock('../DocumentVersionsPanel', () => ({ default: () => null }));
vi.mock('../TroubleshootModal', () => ({ default: () => null }));
vi.mock('../../step-function-flow/StepFunctionFlowViewer', () => ({ default: () => null }));

import DocumentPanel from '../DocumentPanel';
import { documentEditHref } from '../../../routes/constants';
import { barButtonLabels } from '../../edit-screen/__tests__/edit-bar';

const ROLE = { isAdmin: false, isAuthor: false, isReviewer: false, isAnnotator: false, isViewer: false, isReviewerOnly: false };
const QUEUED = {
  objectKey: 'in/queued.pdf',
  objectStatus: 'HITL_IN_PROGRESS',
  hitlStatus: 'in_review',
  hitlTriggered: true,
  sections: [],
  pages: [],
};

const edit = () =>
  createWrapper(document.body)
    .findHeader()!
    .findActions()!
    .findAllButtons()
    .find((b) => b.findTextRegion()?.getElement().textContent === 'Edit');

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('DocumentPanel Edit button on a queued document', () => {
  it.each([
    ['an Admin', { ...ROLE, isAdmin: true, canWorkReviewQueues: true }],
    ['an Annotator who is also a Viewer', { ...ROLE, isAnnotator: true, isViewer: true, canWorkReviewQueues: true }],
  ])('opens the queue item for %s, labelled Edit, at the document’s edit page', (_who, role) => {
    state.role = role;
    render(<DocumentPanel item={QUEUED as never} />);
    const button = edit()!;
    expect(button).toBeDefined();
    expect(button.getElement().getAttribute('href')).toBe(documentEditHref(QUEUED.objectKey));
    expect(button.getElement().getAttribute('aria-label')).toBe('Edit this document in its review queue');
  });

  it('does not offer the legacy Start Review on a queued document', () => {
    state.role = { ...ROLE, isAdmin: true, canWorkReviewQueues: true };
    render(<DocumentPanel item={{ ...QUEUED, objectStatus: 'COMPLETED' } as never} />);
    expect(barButtonLabels()).not.toContain('Start Review');
    expect(barButtonLabels()).not.toContain('Review');
  });

  it('has no link straight to a queue item and no separate Review button, in the page or its actions', () => {
    const actionsDir = join(__dirname, '..', '..', 'document-actions');
    const sources = [
      readFileSync(join(__dirname, '..', 'DocumentPanel.tsx'), 'utf-8'),
      ...readdirSync(actionsDir)
        .filter((f) => /\.tsx?$/.test(f))
        .map((f) => readFileSync(join(actionsDir, f), 'utf-8')),
    ];
    for (const source of sources) {
      expect(source).not.toMatch(/reviewItemHref|review-queues\/items/);
      expect(source).not.toMatch(/Open in review queue/);
      expect(source).not.toMatch(/text: 'Review'|>\s*Review\s*<\/Button>/);
    }
  });
});
