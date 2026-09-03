// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Document Sections table deliberately does NOT show class confidence.
 *
 * It briefly did, and the cost was visible: with a "Class conf." column beside an
 * "Instances" column the table ran out of width — its own header wrapped to
 * "Instanc/es" and the Actions column fell off the panel. The number it showed was
 * an aggregate (the minimum across the section's pages) of values already listed
 * per page in the table directly below, where the model's reasoning lives too.
 *
 * The data is untouched: `Section.Confidence` is still on the API and still lands
 * in the reporting lake as `section_confidence`. This is a presentation decision,
 * so it is pinned here rather than left to be "restored" as an oversight.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }),
}));

import SectionsPanel from '../SectionsPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';

const SECTIONS = [{ Id: 'sec-1', Class: 'W2', PageIds: [11], Confidence: 0.87 }];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = () =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <SectionsPanel {...({ sections: SECTIONS, pages: [], documentItem: DOC } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

const headers = (): string[] => Array.from(document.querySelectorAll('th')).map((th) => th.textContent?.trim() ?? '');

describe('Document Sections — class confidence is not shown', () => {
  it('has no class-confidence column', () => {
    renderPanel();
    expect(headers().some((h) => h.startsWith('Class conf.'))).toBe(false);
  });

  it('does not render the score anywhere in the row, even when the section has one', () => {
    renderPanel();
    const row = document.querySelector('tbody tr') as HTMLElement;
    expect(row.textContent).toContain('W2');
    expect(row.textContent).not.toContain('87.0%');
  });

  it('keeps the table narrow enough for its own columns', () => {
    renderPanel();
    // The regression this guards is width, so it counts columns rather than
    // inspecting styles: Section ID, Class/Type, Page IDs, Low-conf. fields,
    // Status, Actions.
    expect(headers().filter((h) => h.length > 0)).toHaveLength(6);
  });
});
