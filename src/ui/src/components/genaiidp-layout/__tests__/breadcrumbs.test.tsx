// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The layout's breadcrumbs name the page you are on, wherever the layout is mounted.
 *
 * GenAIIDPLayout is mounted under `/documents/*`, `/documents/query/*` and
 * `/features/*`, and its crumbs were matched relative to that mount: the Knowledge
 * Base and the Extensions catalog read "Document Processing > Documents", and under
 * `/documents/*` a page such as Capacity Planning reached the `:objectKey` route and
 * was shown as though "capacity-planning" were a document id. Each case below mounts
 * the crumbs exactly as the route tree in AuthRoutes.tsx does.
 */

import { render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import Breadcrumbs from '../breadcrumbs';

const crumbsAt = (entry: string): string[] => {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/features/*" element={<Breadcrumbs />} />
        <Route path="/documents/query/*" element={<Breadcrumbs />} />
        <Route path="/documents/*" element={<Breadcrumbs />} />
      </Routes>
    </MemoryRouter>,
  );
  const nav = screen.getByRole('navigation', { name: 'Breadcrumbs' });
  // Cloudscape renders the list twice (the second copy is hidden and used to measure
  // overflow) and adds a responsive "..." item; read the first list's crumbs only.
  const list = nav.querySelector('ol') ?? nav;
  return Array.from(list.querySelectorAll(':scope > li'))
    .map((li) => li.textContent?.trim() ?? '')
    .filter((text) => text !== '' && text !== '...');
};

describe('layout breadcrumbs', () => {
  it.each([
    ['/documents/capacity-planning', 'Capacity Planning'],
    ['/documents/discovery', 'Discovery'],
    ['/documents/discovery/job/abc', 'Discovery'],
    ['/documents/users', 'User Management'],
    ['/documents/pricing', 'Pricing'],
    ['/documents/model-limits', 'Model Limits'],
    ['/documents/custom-models', 'Custom Models'],
    ['/documents/query', 'Knowledge Base'],
  ])('names %s as its page, not as a document', (entry, page) => {
    const crumbs = crumbsAt(entry);

    expect(crumbs[crumbs.length - 1]).toBe(page);
    expect(crumbs).not.toContain(entry.split('/')[2]);
  });

  it('names the Extensions catalog and its pages as Extensions', () => {
    expect(crumbsAt('/features')).toEqual(['Document Processing', 'Extensions']);
  });

  it('still shows a document id for a document', () => {
    const crumbs = crumbsAt('/documents/docs-samples%2Flending_package.pdf');

    expect(crumbs[crumbs.length - 1]).toBe('docs-samples/lending_package.pdf');
  });

  it('still shows the Document List crumbs on the list', () => {
    expect(crumbsAt('/documents')).toEqual(['Document Processing', 'Documents']);
  });
});
