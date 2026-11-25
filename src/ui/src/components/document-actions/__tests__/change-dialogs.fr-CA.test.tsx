// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The document page's change dialogs and the reasons an action is unavailable, under `fr-CA`.
 * With a fixture catalog the Re-extract Sections and Reset Page Class dialogs read French: their
 * titles, fields, options (and why a section can't be chosen), buttons, what a change replaces
 * and the notice once it is sent. So do the reasons the gates give the disabled items of a
 * document still processing, the Edit Page Grouping board's consequence and the reasons its Save
 * and Re-extract is unavailable, and a Pages row's "No text available". The page requests the
 * review settings once and does not read the document again when the catalog arrives, and the
 * Pages table signs its thumbnails once. Under `en` the same renders read today's English.
 */
import React from 'react';
import { act, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ButtonWrapper, ModalWrapper } from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, so no render hands out a fresh object.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  presignBatch: vi.fn(),
  versions: { versions: [], loaded: true, loading: false, error: null },
  config: { mergedConfig: { classes: [{ $id: 'FieldTicket' }, { $id: 'Invoice' }] }, loading: false },
  role: {
    isAdmin: true,
    isAuthor: false,
    isReviewer: false,
    isAnnotator: false,
    isViewer: false,
    isReviewerOnly: false,
    canWorkReviewQueues: true,
    canWrite: true,
    canReview: true,
    allowedConfigVersions: null,
    loading: false,
  },
  pricing: { pricing: null, nonChargeableUnits: [], loading: false },
  app: { currentCredentials: null, user: null, setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() },
  settings: { settings: {} },
  board: null as null | Record<string, unknown>,
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../api/presign-batch', () => ({ presignBatch: (...a: unknown[]) => state.presignBatch(...a) }));
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
// The Edit Page Grouping board: its props, and the consequence it shows.
vi.mock('../../common/PageGroupingEditor', () => ({
  default: (props: Record<string, unknown>) => {
    state.board = props;
    return <div data-testid="board-consequence">{props.consequence as React.ReactNode}</div>;
  },
}));
vi.mock('../../sections-panel', () => ({ default: () => null }));
// The document page's own Pages table; the test renders PagesPanel by itself.
vi.mock('../../pages-panel', () => ({ default: () => null }));
vi.mock('../../chat-panel', () => ({ default: () => null }));
vi.mock('../../document-panel/DocumentVersionsPanel', () => ({ default: () => null }));
vi.mock('../../document-panel/DocumentDownloadModals', () => ({ DownloadOptionsModal: () => null, DownloadProgressModal: () => null }));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/MarkdownViewer', () => ({ MarkdownReport: () => null }));
vi.mock('../../document-viewer/EvaluationReport', () => ({ default: () => null }));
vi.mock('../../step-function-flow/StepFunctionFlowViewer', () => ({ default: () => null }));
vi.mock('../../document-panel/TroubleshootModal', () => ({ default: () => null }));

import DocumentPanel from '../../document-panel/DocumentPanel';
import PagesPanel from '../../pages-panel/PagesPanel';
import { resetReviewActionSettingsCache } from '../../review-bulk/use-review-action-settings';
import { barHelpers } from '../../common/__tests__/action-bar-helpers';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documentActions.bar.reports': 'Rapports',
  'documentActions.bar.moreActions': 'Plus d’actions',
  'documentActions.reextract.header': 'Réextraire des sections',
  'documentActions.reextract.fieldLabel': 'Sections à réextraire',
  'documentActions.reextract.fieldDescription': 'Chaque section est extraite de nouveau avec sa classe et ses pages actuelles.',
  'documentActions.reextract.placeholder': 'Choisissez des sections',
  'documentActions.reextract.sectionLabel': 'Section n° {id}',
  'documentActions.reextract.sectionDescription': '{class} · p. {pages}',
  'documentActions.reextract.noClass': 'Aucune classe',
  'documentActions.reextract.cancel': 'Annuler',
  'documentActions.reextract.reextract': 'Réextraire',
  'documentActions.reextract.warning':
    'Ces sections sont extraites de nouveau : {sections}. Leurs valeurs extraites, y compris celles corrigées à la main, sont remplacées, et leurs classes sont enregistrées comme confirmées. Si le document attend une révision, celle-ci est marquée comme terminée et ses résultats de validation des règles sont effacés. Les autres sections gardent leurs valeurs; l’OCR et la classification ne sont pas refaites.',
  'documentActions.reextract.reextracting': 'Réextraction {count, plural, one {de la section} other {des sections}} {ids} de {name}.',
  'documentActions.reasons.sectionNoClass': 'Cette section n’a pas de classe',
  'documentActions.reasons.sectionSkipped': 'Cette section est ignorée par sa configuration',
  'documentActions.reasons.chooseSections': 'Choisissez au moins une section',
  'documentActions.resetPageClass.header': 'Réinitialiser la classe de page',
  'documentActions.resetPageClass.fieldLabel': 'Pages à réinitialiser',
  'documentActions.resetPageClass.placeholder': 'Choisissez des pages',
  'documentActions.resetPageClass.pageLabel': 'Page n° {id}',
  'documentActions.resetPageClass.unclassified': 'Non classée',
  'documentActions.resetPageClass.cancel': 'Annuler',
  'documentActions.resetPageClass.confirm': 'Réinitialiser la classe',
  'documentActions.resetPageClass.intro': 'Réinitialiser la classe d’une page classifie de nouveau tout le document :',
  'documentActions.resetPageClass.everyPage':
    'Chaque page est classifiée de nouveau : des pages peuvent changer de section, et des sections peuvent être ajoutées ou retirées.',
  'documentActions.resetPageClass.everySection':
    'Chaque section est reconstruite et extraite de nouveau. Les valeurs corrigées à la main dans toute section, pas seulement celles qui contiennent ces pages, sont remplacées.',
  'documentActions.resetPageClass.reviewCompleted':
    'Si le document attend une révision, celle-ci est marquée comme terminée et ses résultats de validation des règles sont effacés.',
  'documentActions.resetPageClass.resetting':
    'Réinitialisation de la classe {count, plural, one {de la page} other {des pages}} {ids} de {name} : le document est classifié et extrait de nouveau.',
  'documentActions.reasons.choosePages': 'Choisissez au moins une page',
  'editScreen.documentStatus.processing': 'Disponible à la fin du traitement',
  'documentActions.reasons.noEvaluationReport': 'Ce document n’a pas de rapport d’évaluation',
  'documentActions.reasons.noRuleValidation': 'Ce document n’a pas de synthèse de la validation des règles',
  'documentActions.reasons.noCost': 'Ce document n’a pas encore de données de coût',
  'documentActions.reasons.noExecution': 'Ce document n’a aucune exécution de traitement à afficher',
  'documentActions.reasons.noPendingReview': 'Ce document n’a aucune révision en attente',
  'documentActions.reasons.notFinished': 'Le traitement de ce document n’est pas terminé',
  'documentActions.pageGrouping.consequence':
    '<b>Enregistrer le regroupement</b> réécrit le regroupement des sections de ce document et garde les valeurs extraites, y compris celles qu’un réviseur a corrigées; le document n’est pas retraité, elles peuvent donc ne plus correspondre à leurs pages. <b>Enregistrer et réextraire</b> enregistre le regroupement, puis extrait de nouveau les sections dont la classe ou les pages ont changé, en remplaçant leurs valeurs.',
  'documentActions.reasons.reextractNoClass':
    'Choisissez une classe pour {count, plural, one {la section} other {les sections}} {ids} avant de réextraire',
  'documentActions.reasons.reextractOrderOnly':
    'Seul l’ordre des pages a changé : rien n’est à réextraire. Choisissez Enregistrer le regroupement.',
  'documents.pages.actionsAriaLabel': 'Actions de la page {page}',
  'documents.pages.viewText': 'Afficher le texte de la page',
  'documentActions.reasons.pageNoText': 'Aucun texte disponible',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** Four sections: two that can be chosen, one with no class and one its configuration skips. */
const SECTIONS = [
  { Id: '1', Class: 'FieldTicket', PageIds: ['1', '2'] },
  { Id: '2', Class: 'Invoice', PageIds: ['3'] },
  { Id: '3', Class: null, PageIds: ['4'] },
  { Id: '4', Class: 'Cover', PageIds: ['5'], Excluded: true },
];
/** Two sections, for the board. */
const TWO_SECTIONS = [
  { Id: '1', Class: 'FieldTicket', PageIds: ['1', '2', '3'] },
  { Id: '2', Class: 'Invoice', PageIds: ['4'] },
];

/** A finished document with no reports, its fifth page unclassified. */
const doc = (over: Record<string, unknown> = {}) => ({
  objectKey: 'in/packet.pdf',
  objectStatus: 'COMPLETED',
  evaluationStatus: 'COMPLETED',
  sections: SECTIONS,
  pages: [1, 2, 3, 4, 5].map((Id) => ({
    Id,
    Class: Id < 5 ? 'FieldTicket' : null,
    TextUri: `s3://out/in/packet.pdf/pages/${Id}/result.json`,
  })),
  ...over,
});

/** Page 2 has no text. */
const PAGES = [
  { Id: '1', Class: 'W2', TextUri: 's3://out/1.json' },
  { Id: '2', Class: 'W2' },
];

const getDocumentDetailsFromIds = vi.fn();
const renderPanel = (item: Record<string, unknown>, options?: typeof FR_CA) =>
  renderWithIntl(
    <DocumentPanel
      item={item as never}
      getDocumentDetailsFromIds={getDocumentDetailsFromIds}
      onDelete={vi.fn()}
      onReprocess={vi.fn()}
      onAbort={vi.fn()}
    />,
    options,
  );

/** For waitFor: the element, or a throw until it is there. */
const shown = <T,>(found: T | null | undefined): T => {
  if (!found) throw new Error('not shown yet');
  return found;
};

/** Lets the review settings' request finish inside act, as it does while a French render awaits its catalog. */
const settle = () => act(() => new Promise<void>((resolve) => setTimeout(resolve, 0)));

/** The bar's menus in each language; every item is named by its id. */
const ITEM_IDS = Object.fromEntries(
  [
    'summary',
    'evaluation',
    'rule-validation',
    'cost',
    'flow',
    'regroup',
    'reextract',
    'page-text',
    'reset-class',
    'baseline',
    'skip-all',
    'send-to-review',
    'reprocess',
    'abort',
    'delete',
  ].map((id) => [id, id]),
);
const frBar = barHelpers(['Rapports', 'Plus d’actions'] as const, ITEM_IDS);
const enBar = barHelpers(['Reports', 'More Actions'] as const, ITEM_IDS);

/** A menu's items by id: the reason a screen reader reads for a disabled one, or null. */
const reasonsOf = (items: { text: string; disabledReason: string | null }[]) =>
  Object.fromEntries(items.map((i) => [i.text, i.disabledReason]));

/** What the page has requested: every request, the review settings among them, and reads of the document. */
const requests = () => ({
  all: state.graphql.mock.calls.length,
  reviewSettings: state.graphql.mock.calls.filter(([arg]) => String((arg as { query: string }).query).includes('getMyReviewQueues')).length,
  documentReads: getDocumentDetailsFromIds.mock.calls.length,
});

/** The variables of each call that sent `op`, in order. */
const sent = (op: string) =>
  state.graphql.mock.calls
    .filter(([arg]) => String((arg as { query: string }).query).includes(`${op}(`))
    .map(([arg]) => (arg as { variables: Record<string, unknown> }).variables);

const modalTitled = (title: string) =>
  createWrapper(document.body)
    .findAllModals()
    .find((m) => m.findHeader().getElement().textContent === title);

/** Chooses a menu item that opens a dialog; the dialog titled `title`. */
const openDialog = (chooseItem: () => void, title: string) => {
  chooseItem();
  return waitFor(() => shown(modalTitled(title)));
};

/** The reason a screen reader reads for a control, or null. */
const describedBy = (el: Element | null | undefined) => {
  const id = el?.getAttribute('aria-describedby');
  return id ? (document.getElementById(id)?.textContent ?? null) : null;
};

const buttonText = (b: ButtonWrapper) => b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent;
/** The dialog's footer buttons: each label, and why it is unavailable. */
const footer = (modal: ModalWrapper) =>
  modal
    .findFooter()!
    .findAllButtons()
    .map((b) => ({ text: buttonText(b), reason: describedBy(b.getElement()) }));
const footerButton = (modal: ModalWrapper, label: string) =>
  modal
    .findFooter()!
    .findAllButtons()
    .find((b) => buttonText(b) === label)!;

/** The dialog's field: its label, description and, before anything is chosen, what its list's trigger reads. */
const field = (modal: ModalWrapper) => {
  const formField = modal.findContent().findFormField()!;
  return {
    label: formField.findLabel()?.getElement().textContent ?? null,
    description: formField.findDescription()?.getElement().textContent ?? null,
    placeholder: modal.findContent().findMultiselect()!.findTrigger().getElement().textContent,
  };
};

/** The list's options as shown: label, description, and why a disabled one can't be chosen. */
const optionsOf = (modal: ModalWrapper) => {
  const select = modal.findContent().findMultiselect()!;
  act(() => select.openDropdown());
  const options = select
    .findDropdown()
    .findOptions()
    .map((o) => ({
      label: o.findLabel().getElement().textContent,
      description: o.findDescription()?.getElement().textContent ?? null,
      reason: describedBy(o.getElement().closest('[role="option"]')),
    }));
  act(() => select.closeDropdown());
  return options;
};

/** Chooses the options with these values, in this order; then the tokens the list shows. */
const choose = (modal: ModalWrapper, ...values: string[]) => {
  const select = modal.findContent().findMultiselect()!;
  // The list stays open after each choice.
  act(() => select.openDropdown());
  for (const value of values) act(() => select.selectOptionByValue(value));
  act(() => select.closeDropdown());
  return select.findTokens().map((t) => t.findLabel().getElement().textContent);
};

/** The dialog's warning: its text, and its list's items. */
const warning = (modal: ModalWrapper) => {
  const content = modal.findContent().findAlert()!.findContent().getElement();
  return { text: content.textContent, items: [...content.querySelectorAll('li')].map((li) => li.textContent) };
};

type Draft = { sectionId: string; documentClass: string | null; pageIds: number[] }[];
/** The board's second save: why it is unavailable for a draft. */
const reextractReason = (draft: Draft) =>
  (state.board as unknown as { secondarySave: { disabledReason: (d: Draft) => string | undefined } }).secondarySave.disabledReason(draft);
/** Section 1 gives page 3 to a new section 3, which has no class. */
const ONE_WITHOUT_CLASS: Draft = [
  { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
  { sectionId: '3', documentClass: null, pageIds: [3, 4] },
];
/** Two new sections, 3 and 4, have no class. */
const TWO_WITHOUT_CLASS: Draft = [
  { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1] },
  { sectionId: '3', documentClass: null, pageIds: [2, 3] },
  { sectionId: '4', documentClass: null, pageIds: [4] },
];
/** Only the order of section 1's pages changed. */
const REORDERED: Draft = [
  { sectionId: '1', documentClass: 'FieldTicket', pageIds: [3, 1, 2] },
  { sectionId: '2', documentClass: 'Invoice', pageIds: [4] },
];
const consequence = () => document.querySelector('[data-testid="board-consequence"]');

/** A Pages row's View Page Text: its label, whether it is disabled, and why. */
const viewText = (menuName: string) => {
  const menu = shown(
    createWrapper(document.body)
      .findAllButtonDropdowns()
      .find((d) => d.findNativeButton().getElement().getAttribute('aria-label') === menuName),
  );
  act(() => menu.openDropdown());
  const found = menu.findItemById('view-text')!;
  const el = found.find('[role="menuitem"]')?.getElement() ?? found.getElement();
  const item = { text: el.textContent, disabled: el.getAttribute('aria-disabled') === 'true', reason: describedBy(el) };
  act(() => menu.openDropdown());
  return item;
};

beforeEach(() => {
  document.body.innerHTML = '';
  resetReviewActionSettingsCache();
  state.board = null;
  state.app.setErrorMessage.mockReset();
  state.app.setSuccessMessage.mockReset();
  getDocumentDetailsFromIds.mockReset().mockResolvedValue([]);
  state.presignBatch
    .mockReset()
    .mockResolvedValue({ get: () => 'https://thumbnails.invalid/page.jpg', size: 0, failed: false, accessDenied: false });
  state.graphql.mockReset().mockImplementation(async ({ query }: { query: string }) => {
    const q = String(query);
    if (q.includes('getMyReviewQueues')) return { data: { getMyReviewQueues: { defaultQueueEnabled: true, queues: [] } } };
    if (q.includes('processChanges(')) return { data: { processChanges: { success: true } } };
    return { data: {} };
  });
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the change dialogs and action reasons under fr-CA', () => {
  it('reads the Re-extract Sections dialog in French, loading nothing again', async () => {
    renderPanel(doc(), FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() => expect(frBar.menuNames()).toEqual(['Rapports', 'Plus d’actions']));
    // The catalog arrived after the first render, as a new `intl`: the page requested the review
    // settings once, nothing else, and did not read the document again.
    expect(requests()).toEqual({ all: 1, reviewSettings: 1, documentReads: 0 });
    const modal = await openDialog(() => frBar.chooseMenuItem('Plus d’actions', 'reextract'), 'Réextraire des sections');
    expect(field(modal)).toEqual({
      label: 'Sections à réextraire',
      description: 'Chaque section est extraite de nouveau avec sa classe et ses pages actuelles.',
      placeholder: 'Choisissez des sections',
    });
    expect(optionsOf(modal)).toEqual([
      { label: 'Section n° 1', description: 'FieldTicket · p. 1, 2', reason: null },
      { label: 'Section n° 2', description: 'Invoice · p. 3', reason: null },
      { label: 'Section n° 3', description: 'Aucune classe · p. 4', reason: 'Cette section n’a pas de classe' },
      { label: 'Section n° 4', description: 'Cover · p. 5', reason: 'Cette section est ignorée par sa configuration' },
    ]);
    expect(footer(modal)).toEqual([
      { text: 'Annuler', reason: null },
      { text: 'Réextraire', reason: 'Choisissez au moins une section' },
    ]);
    // The tokens keep the order chosen; the warning and the request follow the document's order.
    expect(choose(modal, '2', '1')).toEqual(['Section n° 2', 'Section n° 1']);
    expect(warning(modal).text).toBe(
      'Ces sections sont extraites de nouveau : Section n° 1, Section n° 2. Leurs valeurs extraites, y compris celles corrigées à la main, sont remplacées, et leurs classes sont enregistrées comme confirmées. Si le document attend une révision, celle-ci est marquée comme terminée et ses résultats de validation des règles sont effacés. Les autres sections gardent leurs valeurs; l’OCR et la classification ne sont pas refaites.',
    );
    await act(async () => footerButton(modal, 'Réextraire').click());
    await waitFor(() => expect(state.app.setSuccessMessage).toHaveBeenCalledWith('Réextraction des sections 1, 2 de packet.pdf.'));
    expect(sent('processChanges')).toEqual([
      {
        objectKey: 'in/packet.pdf',
        modifiedSections: [
          { sectionId: '1', classification: 'FieldTicket', pageIds: [1, 2], isNew: false, isDeleted: false },
          { sectionId: '2', classification: 'Invoice', pageIds: [3], isNew: false, isDeleted: false },
        ],
      },
    ]);
    await waitFor(() => expect(modalTitled('Réextraire des sections')).toBeUndefined());
    await settle();
  });

  it('reads the Reset Page Class dialog in French', async () => {
    renderPanel(doc(), FR_CA);
    await waitFor(() => expect(frBar.menuNames()).toEqual(['Rapports', 'Plus d’actions']));
    const modal = await openDialog(() => frBar.chooseMenuItem('Plus d’actions', 'reset-class'), 'Réinitialiser la classe de page');
    expect(field(modal)).toEqual({ label: 'Pages à réinitialiser', description: null, placeholder: 'Choisissez des pages' });
    expect(optionsOf(modal)).toEqual([
      { label: 'Page n° 1', description: 'FieldTicket', reason: null },
      { label: 'Page n° 2', description: 'FieldTicket', reason: null },
      { label: 'Page n° 3', description: 'FieldTicket', reason: null },
      { label: 'Page n° 4', description: 'FieldTicket', reason: null },
      { label: 'Page n° 5', description: 'Non classée', reason: null },
    ]);
    expect(warning(modal)).toEqual({
      text:
        'Réinitialiser la classe d’une page classifie de nouveau tout le document :' +
        'Chaque page est classifiée de nouveau : des pages peuvent changer de section, et des sections peuvent être ajoutées ou retirées.' +
        'Chaque section est reconstruite et extraite de nouveau. Les valeurs corrigées à la main dans toute section, pas seulement celles qui contiennent ces pages, sont remplacées.' +
        'Si le document attend une révision, celle-ci est marquée comme terminée et ses résultats de validation des règles sont effacés.',
      items: [
        'Chaque page est classifiée de nouveau : des pages peuvent changer de section, et des sections peuvent être ajoutées ou retirées.',
        'Chaque section est reconstruite et extraite de nouveau. Les valeurs corrigées à la main dans toute section, pas seulement celles qui contiennent ces pages, sont remplacées.',
        'Si le document attend une révision, celle-ci est marquée comme terminée et ses résultats de validation des règles sont effacés.',
      ],
    });
    expect(footer(modal)).toEqual([
      { text: 'Annuler', reason: null },
      { text: 'Réinitialiser la classe', reason: 'Choisissez au moins une page' },
    ]);
    expect(choose(modal, '3', '1')).toEqual(['Page n° 3', 'Page n° 1']);
    await act(async () => footerButton(modal, 'Réinitialiser la classe').click());
    await waitFor(() =>
      expect(state.app.setSuccessMessage).toHaveBeenCalledWith(
        'Réinitialisation de la classe des pages 3, 1 de packet.pdf : le document est classifié et extrait de nouveau.',
      ),
    );
    expect(sent('processChanges')).toEqual([
      {
        objectKey: 'in/packet.pdf',
        modifiedSections: [],
        modifiedPages: [
          { pageId: 3, classReset: true, textModified: false },
          { pageId: 1, classReset: true, textModified: false },
        ],
      },
    ]);
    await waitFor(() => expect(modalTitled('Réinitialiser la classe de page')).toBeUndefined());
    await settle();
  });

  it('gives the reasons the gates find for a document still processing in French, loading nothing again', async () => {
    renderPanel(doc({ objectStatus: 'RUNNING' }), FR_CA);
    await waitFor(() => expect(frBar.menuNames()).toEqual(['Rapports', 'Plus d’actions']));
    expect(requests()).toEqual({ all: 1, reviewSettings: 1, documentReads: 0 });
    expect(reasonsOf(frBar.menuItems('Rapports'))).toEqual({
      summary: 'Disponible à la fin du traitement',
      evaluation: 'Ce document n’a pas de rapport d’évaluation',
      'rule-validation': 'Ce document n’a pas de synthèse de la validation des règles',
      cost: 'Ce document n’a pas encore de données de coût',
      flow: 'Ce document n’a aucune exécution de traitement à afficher',
    });
    expect(reasonsOf(frBar.menuItems('Plus d’actions'))).toEqual({
      regroup: 'Disponible à la fin du traitement',
      reextract: 'Disponible à la fin du traitement',
      'page-text': 'Disponible à la fin du traitement',
      'reset-class': 'Disponible à la fin du traitement',
      baseline: 'Disponible à la fin du traitement',
      'skip-all': 'Ce document n’a aucune révision en attente',
      'send-to-review': 'Le traitement de ce document n’est pas terminé',
      reprocess: null,
      abort: null,
      delete: null,
    });
  });

  it('words the Edit Page Grouping board’s consequence and why Save and Re-extract is unavailable in French', async () => {
    renderPanel(doc({ sections: TWO_SECTIONS }), FR_CA);
    await waitFor(() => expect(frBar.menuNames()).toEqual(['Rapports', 'Plus d’actions']));
    frBar.chooseMenuItem('Plus d’actions', 'regroup');
    await waitFor(() => expect(consequence()).not.toBeNull());
    expect([...consequence()!.querySelectorAll('b')].map((b) => b.textContent)).toEqual([
      'Enregistrer le regroupement',
      'Enregistrer et réextraire',
    ]);
    expect(consequence()!.textContent).toBe(
      'Enregistrer le regroupement réécrit le regroupement des sections de ce document et garde les valeurs extraites, y compris celles qu’un réviseur a corrigées; le document n’est pas retraité, elles peuvent donc ne plus correspondre à leurs pages. Enregistrer et réextraire enregistre le regroupement, puis extrait de nouveau les sections dont la classe ou les pages ont changé, en remplaçant leurs valeurs.',
    );
    expect(reextractReason(ONE_WITHOUT_CLASS)).toBe('Choisissez une classe pour la section 3 avant de réextraire');
    expect(reextractReason(TWO_WITHOUT_CLASS)).toBe('Choisissez une classe pour les sections 3, 4 avant de réextraire');
    expect(reextractReason(REORDERED)).toBe(
      'Seul l’ordre des pages a changé : rien n’est à réextraire. Choisissez Enregistrer le regroupement.',
    );
    await settle();
  });

  it('gives a Pages row’s reason for a page with no text in French, signing the thumbnails once', async () => {
    renderWithIntl(<PagesPanel pages={PAGES} />, FR_CA);
    await waitFor(() => expect(viewText('Actions de la page 2').text).toBe('Afficher le texte de la page'));
    expect(viewText('Actions de la page 2')).toEqual({
      text: 'Afficher le texte de la page',
      disabled: true,
      reason: 'Aucun texte disponible',
    });
    expect(viewText('Actions de la page 1')).toEqual({ text: 'Afficher le texte de la page', disabled: false, reason: null });
    expect(state.presignBatch).toHaveBeenCalledTimes(1);
  });
});

describe('the change dialogs and action reasons under en', () => {
  it('reads the Re-extract Sections dialog in English, loading nothing again', async () => {
    renderPanel(doc());
    await settle();
    expect(requests()).toEqual({ all: 1, reviewSettings: 1, documentReads: 0 });
    const modal = await openDialog(() => enBar.chooseMenuItem('More Actions', 'reextract'), 'Re-extract Sections');
    expect(field(modal)).toEqual({
      label: 'Sections to re-extract',
      description: 'Each section is extracted again with its current class and pages.',
      placeholder: 'Choose sections',
    });
    expect(optionsOf(modal)).toEqual([
      { label: 'Section 1', description: 'FieldTicket · pages 1, 2', reason: null },
      { label: 'Section 2', description: 'Invoice · pages 3', reason: null },
      { label: 'Section 3', description: 'No class · pages 4', reason: 'This section has no class' },
      { label: 'Section 4', description: 'Cover · pages 5', reason: 'This section is skipped by its configuration' },
    ]);
    expect(footer(modal)).toEqual([
      { text: 'Cancel', reason: null },
      { text: 'Re-extract', reason: 'Choose at least one section' },
    ]);
    expect(choose(modal, '2', '1')).toEqual(['Section 2', 'Section 1']);
    expect(warning(modal).text).toBe(
      'These sections are extracted again: Section 1, Section 2. Their extracted values, including any corrected by hand, are replaced, and their classes are recorded as confirmed. If the document is waiting for review, its review is marked as completed, and its policy rule validation results are cleared. Other sections keep their values; OCR and classification are not repeated.',
    );
    await act(async () => footerButton(modal, 'Re-extract').click());
    await waitFor(() => expect(state.app.setSuccessMessage).toHaveBeenCalledWith('Re-extracting sections 1, 2 of packet.pdf.'));
    expect(sent('processChanges')).toHaveLength(1);
    await waitFor(() => expect(modalTitled('Re-extract Sections')).toBeUndefined());
    await settle();
  });

  it('reads the Reset Page Class dialog in English', async () => {
    renderPanel(doc());
    await settle();
    const modal = await openDialog(() => enBar.chooseMenuItem('More Actions', 'reset-class'), 'Reset Page Class');
    expect(field(modal)).toEqual({ label: 'Pages to reset', description: null, placeholder: 'Choose pages' });
    expect(optionsOf(modal)).toEqual([
      { label: 'Page 1', description: 'FieldTicket', reason: null },
      { label: 'Page 2', description: 'FieldTicket', reason: null },
      { label: 'Page 3', description: 'FieldTicket', reason: null },
      { label: 'Page 4', description: 'FieldTicket', reason: null },
      { label: 'Page 5', description: 'Unclassified', reason: null },
    ]);
    expect(warning(modal)).toEqual({
      text:
        "Resetting a page's class classifies the whole document again:" +
        'Every page is classified again, so pages can move to other sections and sections can be added or removed.' +
        'Every section is rebuilt and extracted again. Values corrected by hand in any section, not only the ones holding these pages, are replaced.' +
        'If the document is waiting for review, its review is marked as completed, and its policy rule validation results are cleared.',
      items: [
        'Every page is classified again, so pages can move to other sections and sections can be added or removed.',
        'Every section is rebuilt and extracted again. Values corrected by hand in any section, not only the ones holding these pages, are replaced.',
        'If the document is waiting for review, its review is marked as completed, and its policy rule validation results are cleared.',
      ],
    });
    expect(footer(modal)).toEqual([
      { text: 'Cancel', reason: null },
      { text: 'Reset Page Class', reason: 'Choose at least one page' },
    ]);
    expect(choose(modal, '3', '1')).toEqual(['Page 3', 'Page 1']);
    await act(async () => footerButton(modal, 'Reset Page Class').click());
    await waitFor(() =>
      expect(state.app.setSuccessMessage).toHaveBeenCalledWith(
        'Resetting the class of pages 3, 1 of packet.pdf: the document is being classified and extracted again.',
      ),
    );
    await waitFor(() => expect(modalTitled('Reset Page Class')).toBeUndefined());
    await settle();
  });

  it('gives the reasons the gates find for a document still processing in English, loading nothing again', async () => {
    renderPanel(doc({ objectStatus: 'RUNNING' }));
    await settle();
    expect(requests()).toEqual({ all: 1, reviewSettings: 1, documentReads: 0 });
    expect(reasonsOf(enBar.menuItems('Reports'))).toEqual({
      summary: 'Available when processing finishes',
      evaluation: 'This document has no evaluation report',
      'rule-validation': 'This document has no policy rule validation summary',
      cost: 'This document has no cost data yet',
      flow: 'This document has no processing run to show',
    });
    expect(reasonsOf(enBar.menuItems('More Actions'))).toEqual({
      regroup: 'Available when processing finishes',
      reextract: 'Available when processing finishes',
      'page-text': 'Available when processing finishes',
      'reset-class': 'Available when processing finishes',
      baseline: 'Available when processing finishes',
      'skip-all': 'This document has no pending review',
      'send-to-review': 'This document has not finished processing',
      reprocess: null,
      abort: null,
      delete: null,
    });
  });

  it('words the Edit Page Grouping board’s consequence and why Save and Re-extract is unavailable in English', async () => {
    renderPanel(doc({ sections: TWO_SECTIONS }));
    await settle();
    enBar.chooseMenuItem('More Actions', 'regroup');
    await waitFor(() => expect(consequence()).not.toBeNull());
    expect([...consequence()!.querySelectorAll('b')].map((b) => b.textContent)).toEqual(['Save page grouping', 'Save and Re-extract']);
    expect(consequence()!.textContent).toBe(
      "Save page grouping rewrites this document's section grouping and keeps the extracted field values, including any a reviewer corrected; the document is not reprocessed, so they may no longer match their pages. Save and Re-extract saves the grouping, then extracts again the sections whose class or pages changed, replacing their values.",
    );
    expect(reextractReason(ONE_WITHOUT_CLASS)).toBe('Choose a class for section 3 before re-extracting');
    expect(reextractReason(TWO_WITHOUT_CLASS)).toBe('Choose a class for sections 3, 4 before re-extracting');
    expect(reextractReason(REORDERED)).toBe('Only the page order changed, so nothing needs re-extracting. Choose Save page grouping.');
    await settle();
  });

  it('gives a Pages row’s reason for a page with no text in English, signing the thumbnails once', async () => {
    renderWithIntl(<PagesPanel pages={PAGES} />);
    await settle();
    expect(viewText('Actions for page 2')).toEqual({ text: 'View Page Text', disabled: true, reason: 'No text available' });
    expect(viewText('Actions for page 1')).toEqual({ text: 'View Page Text', disabled: false, reason: null });
    expect(state.presignBatch).toHaveBeenCalledTimes(1);
  });
});
