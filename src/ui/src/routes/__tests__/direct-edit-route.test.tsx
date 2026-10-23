// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The direct-edit route. What has to hold: #/documents/<key>/edit opens the unified
 * editor on that document (the key decoded) and the `?section=` section, with the
 * breadcrumb Home › Documents › <document>; and #/documents/<key> still opens the
 * document page.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../contexts/app', () => ({ default: () => ({ navigationOpen: false, setNavigationOpen: vi.fn() }) }));
vi.mock('../../hooks/use-notifications', () => ({ default: () => [] }));
vi.mock('../../components/genaiidp-layout/navigation', () => ({ default: () => null }));
vi.mock('../../components/genai-idp-top-navigation', () => ({ default: () => null }));
vi.mock('../../components/genaiidp-layout', () => ({ default: () => <div data-testid="document-page" /> }));
vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../components/review-workspace/DirectEditAnnotation', () => ({
  default: ({ objectKey, initialSectionId }: { objectKey: string; initialSectionId?: string | null }) => (
    <div data-testid="direct-edit" data-key={objectKey} data-section={initialSectionId ?? ''} />
  ),
}));

import DocumentsRoutes from '../DocumentsRoutes';

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/documents/*" element={<DocumentsRoutes />} />
      </Routes>
    </MemoryRouter>,
  );

describe('the direct-edit route', () => {
  it('opens the unified editor on the decoded key and section', () => {
    renderAt('/documents/a%2Fb.pdf/edit?section=2');
    const editor = screen.getByTestId('direct-edit');
    expect(editor.getAttribute('data-key')).toBe('a/b.pdf');
    expect(editor.getAttribute('data-section')).toBe('2');
    expect(screen.queryByTestId('document-page')).toBeNull();
  });

  it('has the breadcrumb Home › Documents › document', () => {
    renderAt('/documents/a%2Fb.pdf/edit');
    const crumbs = createWrapper(document.body)
      .findBreadcrumbGroup()!
      .findBreadcrumbLinks()
      .map((l) => ({ text: l.getElement().textContent, href: l.getElement().getAttribute('href') }));
    expect(crumbs.map((c) => c.text)).toEqual(['Home', 'Documents', 'b.pdf']);
    expect(crumbs[1].href).toBe('#/documents');
  });

  it('leaves the document page on its own route', () => {
    renderAt('/documents/a%2Fb.pdf');
    expect(screen.getByTestId('document-page')).toBeTruthy();
    expect(screen.queryByTestId('direct-edit')).toBeNull();
  });
});
