// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The layout's breadcrumbs under `fr-CA`: the crumbs and the landmark's name come from
 * the catalog of the active locale, whichever module builds the trail (the layout
 * itself, the document list's shared items, an area's own module), and every href is
 * the one `en` gives.
 */

import { render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { IntlProvider } from 'react-intl';
import { describe, expect, it } from 'vitest';

import Breadcrumbs from '../breadcrumbs';
import { DEFAULT_PATH, DOCUMENTS_PATH } from '../../../routes/constants';

const FR = {
  'breadcrumbs.layout.root': 'Traitement des documents',
  'breadcrumbs.layout.extensions': 'Extensions',
  'breadcrumbs.layout.ariaLabel': 'Fil d\u2019Ariane',
  'breadcrumbs.page.capacityPlanning': 'Planification de la capacité',
  'breadcrumbs.documentList.root': 'Traitement des documents',
  'breadcrumbs.documentList.documents': 'Documents',
  'breadcrumbs.documentList.ariaLabel': 'Fil d\u2019Ariane',
  'breadcrumbs.uploadDocument.root': 'Traitement des documents',
  'breadcrumbs.uploadDocument.documents': 'Documents',
  'breadcrumbs.uploadDocument.upload': 'Téléverser des documents',
  'breadcrumbs.uploadDocument.ariaLabel': 'Fil d\u2019Ariane',
};

/** The breadcrumb landmark at `entry`, found by its French name. */
const navAt = (entry: string): HTMLElement => {
  render(
    <IntlProvider locale="fr-CA" defaultLocale="en" messages={FR}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/features/*" element={<Breadcrumbs />} />
          <Route path="/documents/query/*" element={<Breadcrumbs />} />
          <Route path="/documents/*" element={<Breadcrumbs />} />
        </Routes>
      </MemoryRouter>
    </IntlProvider>,
  );
  return screen.getByRole('navigation', { name: 'Fil d\u2019Ariane' });
};

// Cloudscape renders the list twice (the second copy is hidden and used to measure
// overflow) and adds a responsive "..." item; read the first list's crumbs only.
const firstList = (nav: HTMLElement): Element => nav.querySelector('ol') ?? nav;

const crumbsOf = (nav: HTMLElement): string[] =>
  Array.from(firstList(nav).querySelectorAll(':scope > li'))
    .map((li) => li.textContent?.trim() ?? '')
    .filter((text) => text !== '' && text !== '...');

const hrefsOf = (nav: HTMLElement): (string | null)[] =>
  Array.from(firstList(nav).querySelectorAll(':scope > li a[href]')).map((a) => a.getAttribute('href'));

describe('the layout breadcrumbs under fr-CA', () => {
  it('names a page after the document list in French', () => {
    const crumbs = crumbsOf(navAt('/documents/capacity-planning'));

    expect(crumbs[0]).toBe('Traitement des documents');
    expect(crumbs[crumbs.length - 1]).toBe('Planification de la capacité');
  });

  it('names the Extensions catalog in French', () => {
    expect(crumbsOf(navAt('/features'))).toEqual(['Traitement des documents', 'Extensions']);
  });

  it('reads an area module’s own trail in French, at the same hrefs as under en', () => {
    const nav = navAt('/documents/upload');
    const crumbs = crumbsOf(nav);

    expect(crumbs[crumbs.length - 1]).toBe('Téléverser des documents');
    expect(hrefsOf(nav)).toEqual([`#${DEFAULT_PATH}`, `#${DOCUMENTS_PATH}`]);
  });
});
