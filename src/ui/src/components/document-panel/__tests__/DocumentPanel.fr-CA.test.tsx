// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Document Details and the document page's help panel under `fr-CA`: with a fixture catalog
 * a field label, the Status field (through the shared status renderer), the configuration
 * source and the malware-scan verdict read French, and so do the help panel's heading and a
 * documentation link, which keeps its docs-site `href`. Under `en` the same renders read
 * today's English, where the status is the stored value.
 */
import React from 'react';
import { screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { formatDateTime } from '../../../i18n/format';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, as in DocumentPanel.layout.test.tsx.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  versions: { versions: [], loaded: true, loading: false, error: null },
  config: { mergedConfig: {}, loading: false },
  role: { isAdmin: true, allowedConfigVersions: null, loading: false },
  pricing: { pricing: null, nonChargeableUnits: [], loading: false },
  app: { currentCredentials: null, setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() },
  settings: { settings: {} },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => state.versions }));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => state.config }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => state.pricing }));
vi.mock('../../../hooks/use-polling', () => ({ default: () => undefined }));
vi.mock('../../../hooks/use-classification-comparison', () => ({ default: () => null }));
vi.mock('../../../contexts/app', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/app')>()),
  default: () => state.app,
}));
vi.mock('../../../contexts/settings', () => ({ default: () => state.settings }));
vi.mock('../../sections-panel', () => ({ default: () => null }));
vi.mock('../../pages-panel', () => ({ default: () => null }));
vi.mock('../../chat-panel', () => ({ default: () => null }));
vi.mock('../DocumentVersionsPanel', () => ({ default: () => null }));
vi.mock('../DocumentDownloadModals', () => ({ DownloadOptionsModal: () => null, DownloadProgressModal: () => null }));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/MarkdownViewer', () => ({ MarkdownReport: () => null }));
vi.mock('../../document-viewer/EvaluationReport', () => ({ default: () => null }));
vi.mock('../../step-function-flow/StepFunctionFlowViewer', () => ({ default: () => null }));
vi.mock('../TroubleshootModal', () => ({ default: () => null }));

import { DocumentAttributes } from '../DocumentPanel';
import DocumentDetailsToolsPanel from '../../document-details/tools-panel';

const DOCS_WEB_UI = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/web-ui/';

const ITEM = {
  objectKey: 'in/a.pdf',
  objectStatus: 'COMPLETED',
  configSource: 'prefix-mapping',
  malwareScanStatus: 'COMPLETED',
  malwareScanResult: 'NO_THREATS_FOUND',
  sections: [],
  pages: [],
};

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.panel.field.status': 'Statut',
  'documents.status.completed': 'Terminé',
  'documents.configSource.prefixMapping': 'Attribué par un mappage de préfixe',
  'documents.malwareScan.clean.label': 'Aucune menace',
  'helpPanel.documentDetails.title': 'Détails du document',
  'helpPanel.documentDetails.docsWebUi': 'Interface Web',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** The status indicator text of the Document Details field whose key label reads `label`. */
const statusOfField = (label: string): string | null => {
  const key = [...document.querySelectorAll('[class*="key-label"]')].find((e) => e.textContent === label);
  return key ? createWrapper(key.parentElement!).findStatusIndicator()!.getElement().textContent : null;
};

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
});

describe('Document Details under a locale', () => {
  it('labels a field, spells the status out and reads the configuration source and the malware verdict in French', async () => {
    renderWithIntl(<DocumentAttributes item={ITEM as never} versions={[]} />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Statut')).toBeInTheDocument();
    expect(statusOfField('Statut')).toBe('Terminé');
    expect(screen.getByText('Attribué par un mappage de préfixe')).toBeInTheDocument();
    expect(screen.getByText('Aucune menace')).toBeInTheDocument();
    expect(screen.queryByText('COMPLETED')).toBeNull();
  });

  it('reads the inline English under en, the status being the stored value', () => {
    renderWithIntl(<DocumentAttributes item={ITEM as never} versions={[]} />);
    expect(statusOfField('Status')).toBe('COMPLETED');
    expect(screen.getByText('Assigned by prefix mapping')).toBeInTheDocument();
    expect(screen.getByText('Clean')).toBeInTheDocument();
  });
});

describe('the Document Details help panel under a locale', () => {
  it('reads its heading and a link label from the French catalog, the link keeping its docs href', async () => {
    renderWithIntl(<DocumentDetailsToolsPanel />, FR_CA);
    expect(await screen.findByRole('heading', { name: 'Détails du document' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Interface Web' })).toHaveAttribute('href', DOCS_WEB_UI);
    expect(screen.queryByText('Web UI')).toBeNull();
  });

  it('reads the inline English under en', () => {
    renderWithIntl(<DocumentDetailsToolsPanel />);
    expect(screen.getByRole('heading', { name: 'Document Details' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Web UI' })).toHaveAttribute('href', DOCS_WEB_UI);
  });
});

describe('the Submitted field', () => {
  /** A fixed instant, stored as the document mapping stores it: the UTC ISO string. */
  const SUBMITTED = new Date(1_700_000_000_000).toISOString();
  const renderSubmitted = (initialEventTime: string, options?: typeof FR_CA) =>
    renderWithIntl(<DocumentAttributes item={{ ...ITEM, initialEventTime } as never} versions={[]} />, options).container;
  /** The Submitted field's value in one render (the fixture has no French label for it). */
  const submittedIn = (container: HTMLElement) =>
    [...container.querySelectorAll('[class*="key-label"]')].find((e) => e.textContent === 'Submitted')?.nextElementSibling?.textContent;

  it('shows the stored time in the UI locale, and no time as nothing', async () => {
    const french = renderSubmitted(SUBMITTED, FR_CA);
    const english = renderSubmitted(SUBMITTED);
    const empty = renderSubmitted('');
    // The helper is the oracle: the exact shape of each locale belongs to ICU. One comparison,
    // so a field that ignores the formatter shows every value it gets wrong. Awaited so the
    // French catalog settles inside the test.
    await waitFor(() =>
      expect({ french: submittedIn(french), english: submittedIn(english), empty: submittedIn(empty) }).toEqual({
        french: formatDateTime(new Date(SUBMITTED), 'fr-CA'),
        english: formatDateTime(new Date(SUBMITTED), 'en'),
        empty: '',
      }),
    );
    // Not vacuous: the French text is not the English text.
    expect(submittedIn(french)).not.toBe(submittedIn(english));
  });
});
