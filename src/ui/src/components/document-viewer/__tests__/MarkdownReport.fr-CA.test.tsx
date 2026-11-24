// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The markdown reports under `fr-CA`: Reports › Summary, the Evaluation Report's markdown and
 * Reports › Rule Validation Summary. With a fixture catalog the Document Summary dialog's title,
 * empty text and Close button, each report's loading, binary-content, access-denied and failed
 * sentences, the viewer's toolbar and the page text's empty view read French. A report is
 * requested once: neither the catalog's arrival nor a language switch requests it again, while
 * an error already shown rewords. The printed page keeps its English title, and the report's own
 * content stays as generated. Under `en` the same renders read today's English.
 */
import React from 'react';
import { act, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render. The viewer calls generateClient() at import time,
// so the client's graphql function is hoisted, and so is the document version it reads.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  version: { isHistorical: false, runId: null, versionIdForUri: () => undefined },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../contexts/document-version', () => ({ useDocumentVersion: () => state.version }));

import MarkdownViewer, { MarkdownReport, type MarkdownReportKind } from '../MarkdownViewer';
import { SummaryReportModal } from '../../common/ReportModal';

type Sentences = Record<'loading' | 'binary' | 'failed', string>;

interface ReportCase {
  report: MarkdownReportKind;
  /** The title the printed page carries, in English whatever the UI's language. */
  printTitle: string;
  /** The report as generated. */
  content: string;
  toolbar: { fr: string[]; en: string[] };
  fr: Sentences;
  en: Sentences;
}

const REPORTS: readonly ReportCase[] = [
  {
    report: 'summary',
    printTitle: 'Document Summary',
    content: '# Document Summary\n\nThree pages, one invoice.',
    toolbar: { fr: ['Télécharger', 'Imprimer'], en: ['Download', 'Print'] },
    fr: {
      loading: 'Chargement du résumé du document...',
      binary: 'Ce fichier contient des données binaires qui ne peuvent pas être affichées dans le résumé du document.',
      failed: 'Échec du chargement du résumé du document. Veuillez réessayer.',
    },
    en: {
      loading: 'Loading document summary...',
      binary: 'This file contains binary content that cannot be viewed in the document summary.',
      failed: 'Failed to load document summary. Please try again.',
    },
  },
  {
    report: 'evaluation',
    printTitle: 'Evaluation Report',
    // The marker that gives the toolbar its two evaluation buttons.
    content: '<!-- eval-report-v2 -->\n\n# Evaluation Report\n\nOverall accuracy: 0.92',
    toolbar: {
      fr: ['Éléments non concordants', 'Développer les détails', 'Télécharger', 'Imprimer'],
      en: ['Unmatched Items', 'Expand Details', 'Download', 'Print'],
    },
    fr: {
      loading: 'Chargement du rapport d’évaluation...',
      binary: 'Ce fichier contient des données binaires qui ne peuvent pas être affichées dans le rapport d’évaluation.',
      failed: 'Échec du chargement du rapport d’évaluation. Veuillez réessayer.',
    },
    en: {
      loading: 'Loading evaluation report...',
      binary: 'This file contains binary content that cannot be viewed in the evaluation report.',
      failed: 'Failed to load evaluation report. Please try again.',
    },
  },
  {
    report: 'ruleValidation',
    printTitle: 'Rule Validation Summary',
    content: '# Rule Validation Summary: doc.pdf\n\nAll rules passed.',
    toolbar: { fr: ['Télécharger', 'Imprimer'], en: ['Download', 'Print'] },
    fr: {
      loading: 'Chargement du sommaire de validation des règles...',
      binary: 'Ce fichier contient des données binaires qui ne peuvent pas être affichées dans le sommaire de validation des règles.',
      failed: 'Échec du chargement du sommaire de validation des règles. Veuillez réessayer.',
    },
    en: {
      loading: 'Loading rule validation summary...',
      binary: 'This file contains binary content that cannot be viewed in the rule validation summary.',
      failed: 'Failed to load rule validation summary. Please try again.',
    },
  },
];

const DENIED_FR =
  'Vous n’avez pas la permission de consulter ce fichier. Si votre compte est nouveau, demandez à un administrateur de lui attribuer un rôle.';
const DENIED_EN = 'You do not have permission to view this file. If your account is new, ask an administrator to assign it a role.';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'common.reportModal.close': 'Fermer',
  'common.summaryReport.title': 'Résumé du document',
  'common.summaryReport.empty': 'Aucun résumé n’est disponible pour ce document',
  'common.fileAccess.denied': DENIED_FR,
  'documents.markdownViewer.noContent': 'Aucun contenu à afficher',
  'documents.markdownViewer.unmatchedItems': 'Éléments non concordants',
  'documents.markdownViewer.expandDetails': 'Développer les détails',
  'documents.markdownViewer.download': 'Télécharger',
  'documents.markdownViewer.print': 'Imprimer',
  // Each report's three sentences, from the table above.
  ...Object.fromEntries(
    REPORTS.flatMap(({ report, fr }) =>
      Object.entries(fr).map(([sentence, text]) => [`documents.markdownReport.${report}.${sentence}`, text]),
    ),
  ),
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const URI = 's3://output-bkt/in/doc.pdf/report.md';
const contents = (content: string) => ({ data: { getFileContents: { content, contentType: 'text/markdown', isBinary: false } } });
const BINARY = { data: { getFileContents: { content: '', contentType: 'application/octet-stream', isBinary: true } } };
// A refusal, as the dispatcher reports one.
const DENIED = { errors: [{ errorType: 'Unauthorized', message: 'Unauthorized' }] };
// Any other failure: the client's own rejection of a reply that is not OK and names no error.
const FAILED = { errors: [{ errorType: 'HttpError', message: 'Request failed (500)' }] };

const markdownReport = (report: MarkdownReportKind) => (
  <MarkdownReport reportUri={URI} documentId="doc.pdf" report={report} emptyMessage="No report for this document" />
);

/** A request that settles only when the test says so. */
const deferred = () => {
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise((_resolve, fail) => {
    reject = fail;
  });
  return { promise, reject };
};

const buttonLabels = () => screen.getAllByRole('button').map((button) => button.textContent);

/** Presses the toolbar's Print button and returns the title of the page it writes for printing. */
const printedTitle = (printLabel: string) => {
  const written: string[] = [];
  const printWindow = { document: { open: vi.fn(), write: (html: string) => written.push(html), close: vi.fn() }, print: vi.fn() };
  const open = vi.spyOn(window, 'open').mockReturnValue(printWindow as unknown as Window);
  act(() => screen.getByRole('button', { name: printLabel }).click());
  open.mockRestore();
  return new DOMParser().parseFromString(written.join(''), 'text/html').title;
};

const dialog = () => createWrapper().findModal()!;

beforeEach(() => {
  state.graphql.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the markdown reports under fr-CA', () => {
  it("titles the Document Summary dialog and its Close button in French, and leaves the summary's content as generated", async () => {
    state.graphql.mockResolvedValue(contents(REPORTS[0].content));
    renderWithIntl(<SummaryReportModal reportUri={URI} objectKey="doc.pdf" onDismiss={() => undefined} />, FR_CA);
    // Awaited: the catalog and the report both arrive asynchronously.
    expect(await screen.findByRole('button', { name: 'Imprimer' })).toBeInTheDocument();
    expect(dialog().findHeader().getElement().textContent).toBe('Résumé du document');
    expect(dialog().findFooter()!.findButton()!.getElement().textContent).toBe('Fermer');
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Document Summary');
  });

  it('gives the Document Summary dialog its French empty text when the document has no summary', async () => {
    renderWithIntl(<SummaryReportModal reportUri="" objectKey="doc.pdf" onDismiss={() => undefined} />, FR_CA);
    expect(await screen.findByText('Aucun résumé n’est disponible pour ce document')).toBeInTheDocument();
    expect(state.graphql).not.toHaveBeenCalled();
  });

  describe.each(REPORTS)('$report', ({ report, printTitle, content, toolbar, fr }) => {
    it('reads its loading sentence in French', async () => {
      state.graphql.mockReturnValue(new Promise(() => undefined));
      renderWithIntl(markdownReport(report), FR_CA);
      expect(await screen.findByText(fr.loading)).toBeInTheDocument();
    });

    it('reads its binary-content sentence in French', async () => {
      state.graphql.mockResolvedValue(BINARY);
      renderWithIntl(markdownReport(report), FR_CA);
      expect(await screen.findByText(fr.binary)).toBeInTheDocument();
    });

    it('reads the access-denied sentence in French', async () => {
      state.graphql.mockRejectedValue(DENIED);
      renderWithIntl(markdownReport(report), FR_CA);
      expect(await screen.findByText(DENIED_FR)).toBeInTheDocument();
    });

    it('reads its failed sentence in French', async () => {
      state.graphql.mockRejectedValue(FAILED);
      renderWithIntl(markdownReport(report), FR_CA);
      expect(await screen.findByText(fr.failed)).toBeInTheDocument();
    });

    it('labels its toolbar in French and prints under its English title', async () => {
      state.graphql.mockResolvedValue(contents(content));
      renderWithIntl(markdownReport(report), FR_CA);
      expect(await screen.findByRole('button', { name: 'Imprimer' })).toBeInTheDocument();
      expect(buttonLabels()).toEqual(toolbar.fr);
      expect(printedTitle('Imprimer')).toBe(`${printTitle} - doc.pdf`);
    });
  });

  // The catalog arrives after the first render, as a new `intl`; neither that nor a language
  // switch may request the report again.
  it('requests a report once, and a language switch rewords its error without a new request', async () => {
    const request = deferred();
    state.graphql.mockImplementation(() => request.promise);
    const { fr, en } = REPORTS[2];
    const view = renderWithIntl(markdownReport('ruleValidation'), FR_CA);
    expect(await screen.findByText(fr.loading)).toBeInTheDocument();
    expect(state.graphql).toHaveBeenCalledTimes(1);
    await act(async () => request.reject(FAILED));
    expect(await screen.findByText(fr.failed)).toBeInTheDocument();
    view.setLocale('en');
    await waitFor(() => expect(screen.getByText(en.failed)).toBeInTheDocument());
    expect(state.graphql).toHaveBeenCalledTimes(1);
  });

  it("says in French that a page's text is empty", async () => {
    renderWithIntl(<MarkdownViewer simple content="" />, FR_CA);
    expect(await screen.findByText('Aucun contenu à afficher')).toBeInTheDocument();
  });
});

describe('the markdown reports under en', () => {
  it('titles the Document Summary dialog and its Close button with the inline English', async () => {
    state.graphql.mockResolvedValue(contents(REPORTS[0].content));
    renderWithIntl(<SummaryReportModal reportUri={URI} objectKey="doc.pdf" onDismiss={() => undefined} />);
    expect(await screen.findByRole('button', { name: 'Print' })).toBeInTheDocument();
    expect(dialog().findHeader().getElement().textContent).toBe('Document Summary');
    expect(dialog().findFooter()!.findButton()!.getElement().textContent).toBe('Close');
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Document Summary');
  });

  it("gives the Document Summary dialog today's empty text when the document has no summary", async () => {
    renderWithIntl(<SummaryReportModal reportUri="" objectKey="doc.pdf" onDismiss={() => undefined} />);
    expect(await screen.findByText('Summary report not available for this document')).toBeInTheDocument();
    expect(state.graphql).not.toHaveBeenCalled();
  });

  describe.each(REPORTS)('$report', ({ report, printTitle, content, toolbar, en }) => {
    it("reads today's loading sentence", async () => {
      state.graphql.mockReturnValue(new Promise(() => undefined));
      renderWithIntl(markdownReport(report));
      expect(await screen.findByText(en.loading)).toBeInTheDocument();
    });

    it("reads today's binary-content sentence", async () => {
      state.graphql.mockResolvedValue(BINARY);
      renderWithIntl(markdownReport(report));
      expect(await screen.findByText(en.binary)).toBeInTheDocument();
    });

    it("reads today's access-denied sentence", async () => {
      state.graphql.mockRejectedValue(DENIED);
      renderWithIntl(markdownReport(report));
      expect(await screen.findByText(DENIED_EN)).toBeInTheDocument();
    });

    it("reads today's failed sentence", async () => {
      state.graphql.mockRejectedValue(FAILED);
      renderWithIntl(markdownReport(report));
      expect(await screen.findByText(en.failed)).toBeInTheDocument();
    });

    it("labels its toolbar with today's English and prints under its title", async () => {
      state.graphql.mockResolvedValue(contents(content));
      renderWithIntl(markdownReport(report));
      expect(await screen.findByRole('button', { name: 'Print' })).toBeInTheDocument();
      expect(buttonLabels()).toEqual(toolbar.en);
      expect(printedTitle('Print')).toBe(`${printTitle} - doc.pdf`);
    });
  });

  it("says that a page's text is empty with today's English", async () => {
    renderWithIntl(<MarkdownViewer simple content="" />);
    expect(await screen.findByText('No content to display')).toBeInTheDocument();
  });
});
