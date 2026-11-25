// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The side navigation under `fr-CA`: its labels come from the catalog of the active
 * locale (an id the fixture lacks falls back to the English), every href is the one
 * `en` gives, and the items the navigation hides or greys out are found by their href,
 * so that logic holds whatever the labels say.
 */

import React from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IntlProvider } from 'react-intl';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ nav: null as SideNavigationProps | null }));

vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ loading: false, isAdmin: true, isAuthor: false, isViewerOnly: false, isQueueWorker: false, canReview: true }),
}));
vi.mock('../../../hooks/use-installed-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-catalog-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-latest-version', () => ({
  default: () => ({ latestVersion: null, templateUrl: null, isUpdateAvailable: false }),
}));
// Pattern-1 hides Capacity Planning; the tests run with no deployed Region, which greys out Custom Models.
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { StackName: 'idp-stack', IDPPattern: 'Pattern-1' } }) }));
vi.mock('../../../hooks/use-inbox', () => ({ default: () => ({ unreadCount: 0 }) }));
vi.mock('@cloudscape-design/components', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@cloudscape-design/components')>()),
  SideNavigation: (props: SideNavigationProps) => {
    state.nav = props;
    return null;
  },
}));

import Navigation from '../navigation';
import { CAPACITY_PLANNING_PATH, CUSTOM_MODELS_PATH, JOBS_PATH, UPLOAD_DOCUMENT_PATH } from '../../../routes/constants';

const FR = {
  'navigation.documents.section': 'Documents',
  'navigation.documents.upload': 'Téléverser des documents',
  'navigation.configuration.capacityPlanning': 'Planification de la capacité',
  'navigation.configuration.customModels': 'Modèles personnalisés',
  'navigation.deploymentInfo.section': 'Informations de déploiement',
  'navigation.deploymentInfo.stackName': 'Nom de la pile : {stackName}',
  'inbox.navLink.text': 'Boîte de réception',
  'navigation.home.jobs': 'Tâches',
};

const renderNav = (locale = 'en', messages: Record<string, string> = {}): SideNavigationProps => {
  state.nav = null;
  render(
    <IntlProvider locale={locale} defaultLocale="en" messages={messages}>
      <MemoryRouter initialEntries={['/documents']}>
        <Navigation />
      </MemoryRouter>
    </IntlProvider>,
  );
  if (!state.nav) throw new Error('SideNavigation was not rendered');
  return state.nav;
};

/** Every link in the navigation, sections flattened. */
const allLinks = (items: ReadonlyArray<SideNavigationProps.Item>): SideNavigationProps.Link[] =>
  items.flatMap((i) => {
    if (i.type === 'link') return [i as SideNavigationProps.Link];
    const nested = (i as { items?: ReadonlyArray<SideNavigationProps.Item> }).items;
    return nested ? allLinks(nested) : [];
  });

const linkAt = (nav: SideNavigationProps, href: string): SideNavigationProps.Link | undefined =>
  allLinks(nav.items ?? []).find((l) => l.href === href);

const sectionTexts = (nav: SideNavigationProps): string[] =>
  (nav.items ?? []).filter((i) => i.type === 'section').map((i) => (i as SideNavigationProps.Section).text);

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('the side navigation under fr-CA', () => {
  it('reads its labels from the French catalog, at the same hrefs as under en', () => {
    const nav = renderNav('fr-CA', FR);
    expect(sectionTexts(nav)).toContain('Documents');
    expect(sectionTexts(nav).at(-1)).toBe('Informations de déploiement');
    expect(linkAt(nav, `#${UPLOAD_DOCUMENT_PATH}`)?.text).toBe('Téléverser des documents');
    expect(linkAt(nav, '#/inbox')?.text).toBe('Boîte de réception');
    expect((nav.items?.[0] as SideNavigationProps.Section).items[0]).toMatchObject({ href: '#/inbox', text: 'Boîte de réception' });
    expect(linkAt(nav, `#${JOBS_PATH}`)?.text).toBe('Tâches');
    expect(linkAt(nav, '#stackname')?.text).toBe('Nom de la pile : idp-stack');
    const hrefs = (n: SideNavigationProps) => allLinks(n.items ?? []).map((l) => l.href);
    expect(hrefs(nav)).toEqual(hrefs(renderNav()));
  });

  it('still hides Capacity Planning and greys out Custom Models, which it finds by href', () => {
    const nav = renderNav('fr-CA', FR);
    expect(linkAt(nav, `#${CAPACITY_PLANNING_PATH}`)).toBeUndefined();
    expect(linkAt(nav, `#${CUSTOM_MODELS_PATH}`)).toBeUndefined();
    const greyedOut = linkAt(nav, '#custom-models-unavailable');
    expect(greyedOut?.text).toBe('Modèles personnalisés');
    expect(greyedOut?.info).toBeDefined();
  });

  it('reads the inline English under en', () => {
    const nav = renderNav();
    expect(linkAt(nav, `#${UPLOAD_DOCUMENT_PATH}`)?.text).toBe('Upload Documents');
    expect(linkAt(nav, '#stackname')?.text).toBe('Stack Name: idp-stack');
    expect(sectionTexts(nav).at(-1)).toBe('Deployment Info');
  });
});
