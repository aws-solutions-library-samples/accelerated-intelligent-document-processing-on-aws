// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The edit page's frame under `fr-CA`: with a fixture catalog the action bar's buttons, their
 * hints and the Edit | View switch's name read French, as do the Reports and More Actions
 * menus, the Processing Report dialog's title above the report's own French text, the discard
 * prompts and the skip reason dialog's confirm button, History's tabs and the Field Edits
 * table, the page states the edit page and the document page share (with their back links and
 * Retry), the Info link beside a table's title and a review queue item's Next alert. The
 * document page reads its document once: not again when the catalog arrives, nor on a switch
 * to English. Under `en` the same renders read today's English.
 */
import React from 'react';
import { act, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { useIntl } from 'react-intl';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

const state = vi.hoisted(() => ({
  app: { setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() },
  exit: vi.fn(),
  thumbnails: { urls: {}, failure: null },
  controller: { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() },
  documents: {
    documents: [] as Record<string, unknown>[],
    getDocumentDetailsFromIds: vi.fn(),
    setToolsOpen: vi.fn(),
    deleteDocuments: vi.fn(),
    reprocessDocuments: vi.fn(),
    abortWorkflows: vi.fn(),
  },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../use-edit-exit', () => ({ default: () => state.exit }));
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({ default: () => state.thumbnails }));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => <div data-testid="source-viewer" /> }));
vi.mock('../SectionEditor', () => ({
  default: (props: { controllerRef: { current: unknown } }) => {
    props.controllerRef.current = state.controller;
    return <div data-testid="editor" />;
  },
}));
vi.mock('../../common/PageGroupingEditor', () => ({ default: () => null }));
// The document page, for the states it shares with the edit page.
vi.mock('../../../contexts/documents', () => ({ default: () => state.documents }));
vi.mock('../../document-panel', () => ({ default: () => <div data-testid="panel" /> }));
vi.mock('../../common/DeleteDocumentModal', () => ({ default: () => null }));
vi.mock('../../common/ReprocessDocumentModal', () => ({ default: () => null }));
vi.mock('../../common/AbortWorkflowModal', () => ({ default: () => null }));

import type { ReviewItemDetail } from '../../../graphql/generated/operation-types';
import { TableHeader } from '../../common/table';
import DocumentDetails from '../../document-details/DocumentDetails';
import ReviewTaskContext from '../../review-queues/ReviewTaskContext';
import EditPage from '../EditPage';
import EditorHistoryModal from '../EditorHistoryModal';
import { itemFailedModel } from '../edit-mode-states';
import type { EditModel, EditReady } from '../edit-model';
import { barButtonLabels, barButtons, buttonLabel } from './edit-bar';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'editScreen.shared.working': 'Traitement en cours…',
  'editScreen.shared.history': 'Historique',
  'editScreen.shared.documentNotFound': 'Document introuvable',
  'editScreen.shared.couldNotOpenDocument': 'Impossible d’ouvrir ce document',
  'editScreen.shared.backToDocuments': 'Retour aux documents',
  'editScreen.bar.viewSwitch': 'Modifier le document ou afficher sa source',
  'editScreen.bar.viewEdit': 'Modifier',
  'editScreen.bar.viewSource': 'Afficher',
  'editScreen.bar.reports': 'Rapports',
  'editScreen.bar.summary': 'Résumé',
  'editScreen.bar.processingReport': 'Rapport de traitement',
  'editScreen.bar.refresh': 'Actualiser',
  'editScreen.bar.claim': 'Réclamer',
  'editScreen.bar.claimHint': 'Vous attribuer cet élément, puis le traiter ici ou depuis Accueil › Tâches',
  'editScreen.bar.moreActions': 'Plus d’actions',
  'editScreen.bar.useAsBaseline': 'Utiliser comme référence d’évaluation',
  'editScreen.bar.skip': 'Passer',
  'editScreen.bar.skipHint': 'Rendre cet élément à sa file, non attribué et non terminé.',
  'editScreen.bar.cancel': 'Annuler',
  'editScreen.bar.cancelHint': 'Abandonner vos modifications non enregistrées et quitter',
  'editScreen.bar.cancelAriaLabel': 'Annuler et abandonner les modifications',
  'editScreen.bar.save': 'Enregistrer',
  'editScreen.page.claim': 'Réclamer l’élément',
  'editScreen.page.refreshDiscard.header': 'Abandonner vos modifications et recharger?',
  'editScreen.page.refreshDiscard.confirm': 'Abandonner et recharger',
  'editScreen.discard.body':
    'Les modifications faites depuis le dernier enregistrement sont perdues. Ce qui est déjà enregistré est conservé.',
  'editScreen.discard.keepEditing': 'Continuer à modifier',
  'editScreen.discard.cancel.header': 'Abandonner vos modifications non enregistrées?',
  'editScreen.discard.cancel.confirm': 'Abandonner et quitter',
  'editScreen.states.documentMissing.body': 'Ce document n’existe plus. {backLink}',
  'editScreen.states.documentFailed.body': '{error} {backLink}',
  'editScreen.states.itemFailed.header': 'Impossible d’ouvrir cet élément de révision',
  'editScreen.states.itemFailed.action': 'charger l’élément de révision',
  'editScreen.history.tab.fieldEdits': 'Modifications des champs',
  'editScreen.history.tab.reviewActivity': 'Activité de révision',
  'editScreen.fieldEdits.column.when': 'Quand',
  'editScreen.fieldEdits.column.by': 'Par',
  'editScreen.fieldEdits.column.field': 'Champ',
  'editScreen.fieldEdits.column.from': 'De',
  'editScreen.fieldEdits.column.to': 'À',
  'editScreen.fieldEdits.column.appliesTo': 'S’applique à',
  'editScreen.fieldEdits.appliesTo.prediction': 'Prédiction',
  'editScreen.fieldEdits.appliesTo.baseline': 'Référence',
  'editScreen.fieldEdits.unknownEditor': 'Inconnu',
  'editScreen.fieldEdits.emptyValue': '(vide)',
  'common.labels.processChanges': 'Traiter les modifications',
  'common.labels.nextAlert': 'Alerte suivante',
  'common.loadState.retry': 'Réessayer',
  'common.infoLink.info': 'Infos',
  'common.reportModal.close': 'Fermer',
  'common.apiError.failed': 'Impossible de {action} : {error}',
  'documents.processingReport.noSection': 'Ouvrez une section pour voir son rapport de traitement.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const ready = (over: Partial<EditReady> = {}): EditModel => ({
  navigation: { trail: [] },
  state: {
    kind: 'ready',
    header: {
      title: 'a.pdf',
      status: 'Done',
      objectKey: 'in/a.pdf',
      source: { objectKey: 'in/a.pdf' },
      offerReport: true,
      summary: { reportUri: 's3://b/summary.md' },
    },
    notices: [],
    bar: { showSkip: true, showCancel: true, showSave: true, showRefresh: true, showBaseline: true, offerHistory: true },
    editor: {
      sections: [{ sectionId: '1', resultUri: 's3://o/1', pageIds: [] }],
      pages: [],
      readOnly: false,
      loadSection: vi.fn(),
      saveSection: vi.fn(),
      emptyText: 'Nothing to edit.',
    },
    board: null,
    history: { reviewActivity: [] },
    skip: { header: 'Skip this item?', reasons: ['Other'], body: 'Why is it skipped?', run: vi.fn() },
    process: { run: vi.fn().mockResolvedValue(null) },
    refresh: vi.fn().mockResolvedValue(undefined),
    cancelTo: '/documents/in%2Fa.pdf',
    failures: { surface: 'flash', describe: () => null },
    ...over,
  },
});

/** A review queue item an Annotator views unassigned: the bar offers Claim, as does the read-only notice. */
const unclaimed = (): EditModel =>
  ready({
    bar: { showClaim: true, offerHistory: true },
    claim: vi.fn().mockResolvedValue('Claimed.'),
    editor: { ...(ready().state as EditReady).editor!, readOnlyReason: 'Claim this item to edit it.' },
  });

type IntlOptions = Parameters<typeof renderWithIntl>[1];

const renderPage = (model: EditModel, options: IntlOptions = {}) =>
  renderWithIntl(
    <MemoryRouter>
      <EditPage model={model} />
    </MemoryRouter>,
    options,
  );

/** The item failure as the edit page shows it, worded with the page's `intl`. */
const ItemFailure = ({ error }: { error: unknown }): React.JSX.Element => {
  const intl = useIntl();
  return <EditPage model={itemFailedModel(error, vi.fn(), intl)} />;
};

const actions = () => createWrapper(document.body).findHeader()!.findActions()!;
const dropdowns = () => actions().findAllButtonDropdowns();
const menuNames = () => dropdowns().map((d) => buttonLabel(d.findNativeButton().getElement()));
/** The text of a menu's items, by their ids. */
const menuItemTexts = (index: number, ids: string[]) => {
  const menu = dropdowns()[index];
  act(() => menu.openDropdown());
  const texts = ids.map((id) => menu.findItemById(id)?.getElement().textContent ?? null);
  act(() => menu.openDropdown());
  return texts;
};
const chooseItem = (index: number, id: string) => {
  const menu = dropdowns()[index];
  act(() => menu.openDropdown());
  act(() => menu.findItemById(id)!.click());
};
const button = (name: string) => {
  const found = barButtons().find((b) => buttonLabel(b) === name);
  if (!found) throw new Error(`no button ${name}`);
  return found;
};
/** A button's hover text: the title of the span its hint wraps it in. */
const hintOf = (name: string) => button(name).closest('span[title]')?.getAttribute('title');
const modal = () => createWrapper(document.body).findModal()!;
const modalButtons = () =>
  modal()
    .findFooter()!
    .findAllButtons()
    .map((b) => b.getElement().textContent);

/** A Test Studio label edit to the baseline, by e-mail, and a prediction edit by nobody, to an empty value. */
const EDITED = {
  inference_result: {},
  _editHistory: [
    {
      timestamp: '2026-10-22T12:00:00Z',
      editedBy: 'ann',
      editedByEmail: 'ann@example.com',
      baselineEdits: { changedFields: ['total'], diffs: { total: { originalValue: '100.00', newValue: '105.00' } } },
    },
    {
      timestamp: '2026-10-22T11:00:00Z',
      predictionEdits: { changedFields: ['name'], diffs: { name: { originalValue: 'Ann', newValue: '' } } },
    },
  ],
};

const reason = { kind: 'low_confidence', sectionId: '1', field: 'total', label: 'Total', score: 0.2, threshold: 0.8 };
const DETAIL = { item: { reasons: [reason], reasonCount: 1, escalated: false }, checklist: [] } as unknown as ReviewItemDetail;

const renderDocumentPage = (options: IntlOptions = {}) =>
  renderWithIntl(
    <MemoryRouter initialEntries={['/documents/a.pdf']}>
      <Routes>
        <Route path="/documents/*" element={<DocumentDetails />} />
      </Routes>
    </MemoryRouter>,
    options,
  );

/** The document page's reads of its document. */
const documentReads = () => state.documents.getDocumentDetailsFromIds.mock.calls.length;

beforeEach(() => {
  state.controller.isDirty.mockReset().mockReturnValue(false);
  state.documents.getDocumentDetailsFromIds.mockReset().mockResolvedValue([]);
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the edit page under fr-CA', () => {
  it('words the action bar, its hints and the Edit | View switch in French, then in English after a switch', async () => {
    const view = renderPage(ready(), FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Traiter les modifications')).toBeInTheDocument();
    expect(barButtonLabels()).toEqual(['Actualiser', 'Passer', 'Annuler', 'Enregistrer', 'Traiter les modifications']);
    expect(menuNames()).toEqual(['Rapports', 'Plus d’actions']);
    expect(hintOf('Passer')).toBe('Rendre cet élément à sa file, non attribué et non terminé.');
    expect(hintOf('Annuler')).toBe('Abandonner vos modifications non enregistrées et quitter');
    expect(button('Annuler').getAttribute('aria-label')).toBe('Annuler et abandonner les modifications');
    const viewSwitch = screen.getByRole('toolbar', { name: 'Modifier le document ou afficher sa source' });
    expect(
      within(viewSwitch)
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual(['Modifier', 'Afficher']);
    view.setLocale('en');
    await waitFor(() => expect(barButtonLabels()).toEqual(['Refresh', 'Skip', 'Cancel', 'Save', 'Process Changes']));
    expect(menuNames()).toEqual(['Reports', 'More Actions']);
    expect(screen.getByRole('toolbar', { name: 'Edit the document or view its source' })).toBeInTheDocument();
  });

  it('words the Reports and More Actions menus, and the Processing Report dialog’s title above its French text', async () => {
    renderPage(ready(), FR_CA);
    expect(await screen.findByText('Traiter les modifications')).toBeInTheDocument();
    expect(menuItemTexts(0, ['history', 'summary', 'report'])).toEqual(['Historique', 'Résumé', 'Rapport de traitement']);
    expect(menuItemTexts(1, ['baseline'])).toEqual(['Utiliser comme référence d’évaluation']);
    chooseItem(0, 'report');
    const dialog = modal();
    expect(dialog.findHeader().getElement().textContent).toBe('Rapport de traitement');
    expect(dialog.findContent().getElement().textContent).toContain('Ouvrez une section pour voir son rapport de traitement.');
  });

  it('words Claim, its hint and the read-only notice’s Claim in French', async () => {
    renderPage(unclaimed(), FR_CA);
    expect(await screen.findByText('Réclamer l’élément')).toBeInTheDocument();
    expect(barButtonLabels()).toEqual(['Réclamer']);
    expect(hintOf('Réclamer')).toBe('Vous attribuer cet élément, puis le traiter ici ou depuis Accueil › Tâches');
  });

  it('words the discard prompts of Refresh and Cancel, and the skip reason dialog’s confirm button', async () => {
    state.controller.isDirty.mockReturnValue(true);
    renderPage(ready(), FR_CA);
    expect(await screen.findByText('Traiter les modifications')).toBeInTheDocument();
    act(() => button('Actualiser').click());
    expect(modal().findHeader().getElement().textContent).toBe('Abandonner vos modifications et recharger?');
    expect(modal().findContent().getElement().textContent).toBe(FR['editScreen.discard.body']);
    expect(modalButtons()).toEqual(['Continuer à modifier', 'Abandonner et recharger']);
    act(() => modal().findFooter()!.findAllButtons()[0].click());
    act(() => button('Annuler').click());
    expect(modal().findHeader().getElement().textContent).toBe('Abandonner vos modifications non enregistrées?');
    expect(modalButtons()).toEqual(['Continuer à modifier', 'Abandonner et quitter']);
    act(() => modal().findFooter()!.findAllButtons()[0].click());
    act(() => button('Passer').click());
    expect(modalButtons()).toContain('Passer');
  });

  it('words History’s title, tabs and Close, and the Field Edits headers and cells', async () => {
    renderWithIntl(<EditorHistoryModal visible onDismiss={vi.fn()} sectionData={EDITED} />, FR_CA);
    expect(await screen.findByText('Modifications des champs')).toBeInTheDocument();
    expect(modal().findHeader().getElement().textContent).toBe('Historique');
    expect(screen.getByText('Activité de révision')).toBeInTheDocument();
    expect(modalButtons()).toEqual(['Fermer']);
    const table = modal().findContent().findTable()!;
    expect(table.findColumnHeaders().map((h) => h.getElement().textContent)).toEqual(['Quand', 'Par', 'Champ', 'De', 'À', 'S’applique à']);
    const rows = table.findRows().map((r) => r.getElement().textContent);
    expect(rows[0]).toMatch(/ann@example\.com.*total.*100\.00.*105\.00.*Référence/);
    expect(rows[1]).toMatch(/Inconnu.*name.*Ann.*\(vide\).*Prédiction/);
  });

  it('words the item failure through the shared error sentence, with a French phrase', async () => {
    renderWithIntl(
      <MemoryRouter>
        <ItemFailure error={new Error('Network down')} />
      </MemoryRouter>,
      FR_CA,
    );
    expect(await screen.findByText('Impossible d’ouvrir cet élément de révision')).toBeInTheDocument();
    const alert = createWrapper(document.body).findAlert()!;
    expect(alert.findContent().getElement().textContent).toBe('Impossible de charger l’élément de révision : Network down');
    expect(alert.findActionSlot()!.findButton()!.getElement().textContent).toBe('Réessayer');
  });

  it('shows the Info link beside a table’s title, and a review queue item’s Next alert, in French', async () => {
    renderWithIntl(
      <>
        <TableHeader title="Documents" updateTools={vi.fn()} />
        <ReviewTaskContext detail={DETAIL} onReasonClick={vi.fn()} />
      </>,
      FR_CA,
    );
    expect(await screen.findByText('Infos')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Alerte suivante' })).toBeInTheDocument();
  });
});

describe('the document page’s states under fr-CA', () => {
  it('words a missing document and its back link in French, reading the document once', async () => {
    const view = renderDocumentPage(FR_CA);
    expect(await screen.findByText('Document introuvable')).toBeInTheDocument();
    const alert = createWrapper(document.body).findAlert()!;
    expect(alert.findContent().getElement().textContent).toBe('Ce document n’existe plus. Retour aux documents');
    expect(screen.getByRole('link', { name: 'Retour aux documents' }).getAttribute('href')).toBe('#/documents');
    expect(documentReads()).toBe(1);
    view.setLocale('en');
    await waitFor(() => expect(screen.getByText('Document not found')).toBeInTheDocument());
    expect(documentReads()).toBe(1);
  });

  it('words a failed read, its back link and Retry in French, keeping the server’s text', async () => {
    state.documents.getDocumentDetailsFromIds.mockImplementation(
      async (_keys: string[], opts: { onError: (k: string, e: unknown) => void }) => {
        opts.onError('a.pdf', new Error('Access denied by the server'));
        return [];
      },
    );
    const view = renderDocumentPage(FR_CA);
    expect(await screen.findByText('Impossible d’ouvrir ce document')).toBeInTheDocument();
    const alert = createWrapper(document.body).findAlert()!;
    expect(alert.findContent().getElement().textContent).toBe('Access denied by the server Retour aux documents');
    expect(alert.findActionSlot()!.findButton()!.getElement().textContent).toBe('Réessayer');
    expect(documentReads()).toBe(1);
    view.setLocale('en');
    await waitFor(() => expect(screen.getByText("Couldn't open this document")).toBeInTheDocument());
    expect(createWrapper(document.body).findAlert()!.findActionSlot()!.findButton()!.getElement().textContent).toBe('Retry');
    expect(documentReads()).toBe(1);
  });
});

describe('the edit page under en', () => {
  it('reads today’s English in the bar, its hints, its menus and the Processing Report dialog', async () => {
    renderPage(ready());
    expect(await screen.findByText('Process Changes')).toBeInTheDocument();
    expect(barButtonLabels()).toEqual(['Refresh', 'Skip', 'Cancel', 'Save', 'Process Changes']);
    expect(hintOf('Skip')).toBe('Give this item back to its queue, unassigned and not finished.');
    expect(hintOf('Cancel')).toBe('Discard your unsaved changes and leave');
    expect(button('Cancel').getAttribute('aria-label')).toBe('Cancel and discard changes');
    const viewSwitch = screen.getByRole('toolbar', { name: 'Edit the document or view its source' });
    expect(
      within(viewSwitch)
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual(['Edit', 'View']);
    expect(menuItemTexts(0, ['history', 'summary', 'report'])).toEqual(['History', 'Summary', 'Processing Report']);
    expect(menuItemTexts(1, ['baseline'])).toEqual(['Use As Evaluation Baseline']);
    chooseItem(0, 'report');
    expect(modal().findHeader().getElement().textContent).toBe('Processing Report');
  });

  it('reads today’s English for Claim, its hint and the read-only notice’s Claim', async () => {
    renderPage(unclaimed());
    expect(await screen.findAllByText('Claim')).toHaveLength(2);
    expect(hintOf('Claim')).toBe('Assign this item to you, then work it here or from Home › Tasks');
  });

  it('reads today’s English in the discard prompts and the skip reason dialog', async () => {
    state.controller.isDirty.mockReturnValue(true);
    renderPage(ready());
    expect(await screen.findByText('Process Changes')).toBeInTheDocument();
    act(() => button('Refresh').click());
    expect(modal().findHeader().getElement().textContent).toBe('Discard your changes and reload?');
    expect(modal().findContent().getElement().textContent).toBe(
      'The changes you made since the last save are lost. Anything already saved is kept.',
    );
    expect(modalButtons()).toEqual(['Keep editing', 'Discard and reload']);
    act(() => modal().findFooter()!.findAllButtons()[0].click());
    act(() => button('Cancel').click());
    expect(modal().findHeader().getElement().textContent).toBe('Discard your unsaved changes?');
    expect(modalButtons()).toEqual(['Keep editing', 'Discard and leave']);
    act(() => modal().findFooter()!.findAllButtons()[0].click());
    act(() => button('Skip').click());
    expect(modalButtons()).toContain('Skip');
  });

  it('reads today’s English in History and the Field Edits table', async () => {
    renderWithIntl(<EditorHistoryModal visible onDismiss={vi.fn()} sectionData={EDITED} />);
    expect(await screen.findByText('Field Edits')).toBeInTheDocument();
    expect(modal().findHeader().getElement().textContent).toBe('History');
    expect(modalButtons()).toEqual(['Close']);
    const table = modal().findContent().findTable()!;
    expect(table.findColumnHeaders().map((h) => h.getElement().textContent)).toEqual(['When', 'By', 'Field', 'From', 'To', 'Applies to']);
    const rows = table.findRows().map((r) => r.getElement().textContent);
    expect(rows[0]).toMatch(/ann@example\.com.*total.*100\.00.*105\.00.*Baseline/);
    expect(rows[1]).toMatch(/Unknown.*name.*Ann.*\(empty\).*Prediction/);
  });

  it('reads today’s English for the item failure, Info and Next alert', async () => {
    renderWithIntl(
      <MemoryRouter>
        <ItemFailure error={new Error('Network down')} />
        <TableHeader title="Documents" updateTools={vi.fn()} />
        <ReviewTaskContext detail={DETAIL} onReasonClick={vi.fn()} />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Couldn't open this review item")).toBeInTheDocument();
    expect(createWrapper(document.body).findAlert()!.findContent().getElement().textContent).toBe(
      'Failed to load the review item: Network down',
    );
    expect(screen.getByText('Info')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next alert' })).toBeInTheDocument();
  });

  it('reads today’s English on the document page’s missing state', async () => {
    renderDocumentPage();
    expect(await screen.findByText('Document not found')).toBeInTheDocument();
    expect(createWrapper(document.body).findAlert()!.findContent().getElement().textContent).toBe(
      'This document no longer exists. Back to Documents',
    );
    expect(documentReads()).toBe(1);
  });
});
