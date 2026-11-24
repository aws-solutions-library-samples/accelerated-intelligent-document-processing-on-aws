// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The document page's action bar under `fr-CA`. With a fixture catalog the view switch, the
 * Reports and More Actions menus and their items (the downloads included), the bar's buttons
 * and the Evaluation field's baseline state read French; so does the reason an action is
 * unavailable while the document is in a review queue, and Edit's screen-reader name there.
 * The Skip All Reviews dialog lists the fixed reasons in French, and choosing the French
 * "Duplicate document" sends the English reason, the one the server accepts. The page
 * requests the review settings once and does not read the document again when the catalog
 * arrives. Under `en` the same renders read today's English.
 */
import React from 'react';
import { act, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ButtonDropdownWrapper } from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, so no render hands out a fresh object.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  versions: { versions: [], loaded: true, loading: false, error: null },
  config: { mergedConfig: {}, loading: false },
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
vi.mock('../../document-panel/DocumentVersionsPanel', () => ({ default: () => null }));
vi.mock('../../document-panel/DocumentDownloadModals', () => ({ DownloadOptionsModal: () => null, DownloadProgressModal: () => null }));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => null }));
vi.mock('../../document-viewer/MarkdownViewer', () => ({ MarkdownReport: () => null }));
vi.mock('../../document-viewer/EvaluationReport', () => ({ default: () => null }));
vi.mock('../../step-function-flow/StepFunctionFlowViewer', () => ({ default: () => null }));
vi.mock('../../document-panel/TroubleshootModal', () => ({ default: () => null }));

import DocumentPanel from '../../document-panel/DocumentPanel';
import { resetReviewActionSettingsCache } from '../../review-bulk/use-review-action-settings';
import { buttonLabel } from '../../common/__tests__/action-bar-helpers';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documentActions.bar.viewSwitch': 'Afficher les détails du document ou sa source',
  'documentActions.bar.details': 'Détails',
  'documentActions.bar.source': 'Original',
  'documentActions.bar.reports': 'Rapports',
  'documentActions.bar.moreActions': 'Plus d’actions',
  'documentActions.bar.refresh': 'Actualiser',
  'documentActions.bar.troubleshoot': 'Dépanner',
  'documentActions.bar.startReview': 'Commencer la révision',
  'documentActions.bar.edit': 'Modifier',
  'documentActions.bar.editInQueue': 'Modifier ce document dans sa file de révision',
  'documentActions.reports.summary': 'Résumé',
  'documentActions.reports.evaluationReport': 'Rapport d’évaluation',
  'documentActions.reports.ruleValidationSummary': 'Synthèse de la validation des règles',
  'documentActions.reports.estimatedCost': 'Coût estimé',
  'documentActions.reports.processingFlow': 'Flux de traitement',
  'documentActions.moreActions.editPageGrouping': 'Modifier le regroupement des pages',
  'documentActions.moreActions.reextractSections': 'Réextraire des sections',
  'documentActions.moreActions.editPageText': 'Modifier le texte d’une page',
  'documentActions.moreActions.resetPageClass': 'Réinitialiser la classe de page',
  'documentActions.moreActions.useAsBaseline': 'Utiliser comme référence d’évaluation',
  'documentActions.moreActions.skipAllReviews': 'Ignorer toutes les révisions',
  'documentActions.moreActions.sendToReview': 'Envoyer en révision',
  'documentActions.moreActions.reprocess': 'Retraiter',
  'documentActions.moreActions.abort': 'Interrompre',
  'documentActions.moreActions.delete': 'Supprimer',
  'documentActions.download.all': 'Tout télécharger (ZIP)',
  'documentActions.download.predictions': 'Télécharger les prédictions (ZIP)',
  'documentActions.download.baselines': 'Télécharger les références (ZIP)',
  'documents.panel.field.evaluation': 'Évaluation',
  'documentActions.baseline.available': 'Référence disponible',
  'documentActions.baseline.notApplicable': 'S. O.',
  'common.reviewStatus.inReviewQueue.canWork': 'Ce document est dans une file de révision. Choisissez Modifier pour y travailler.',
  'documentActions.skipAllReviews.header': 'Ignorer toutes les révisions de section',
  'documentActions.skipAllReviews.confirm': 'Tout ignorer',
  'documentActions.skipAllReviews.skipped': 'Révision de {name} ignorée.',
  'common.reasonModal.reason': 'Motif',
  'common.reviewStatus.reason.valuesLookCorrect': 'Les valeurs extraites semblent exactes',
  'common.reviewStatus.reason.reviewNotNeeded': 'Révision inutile pour ce document',
  'common.reviewStatus.reason.duplicateDocument': 'Document en double',
  'common.reviewStatus.reason.otherSeeNote': 'Autre (voir la note)',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** A finished document with a pending legacy review, every report and some metering. */
const doc = (over: Record<string, unknown> = {}) => ({
  objectKey: 'in/a.pdf',
  objectStatus: 'COMPLETED',
  hitlTriggered: true,
  hitlStatus: 'PendingReview',
  evaluationStatus: 'BASELINE_AVAILABLE',
  summaryReportUri: 's3://o/in/a.pdf/summary.md',
  evaluationReportUri: 's3://o/in/a.pdf/evaluation.md',
  ruleValidationResultUri: 's3://o/in/a.pdf/rules.md',
  executionArn: 'arn:aws:states:us-east-1:000000000000:execution:sm:x',
  metering: { 'OCR/textract/pages': { pages: 1 } },
  sections: [{ Id: '1', Class: 'W2', PageIds: [1] }],
  pages: [{ Id: 1, TextUri: 's3://o/in/a.pdf/pages/1/text.txt' }],
  ...over,
});

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

const actions = () => createWrapper(document.body).findHeader()!.findActions()!;
const menus = () => actions().findAllButtonDropdowns();
const menuNames = () => menus().map((m) => buttonLabel(m.findNativeButton().getElement()));
const menuNamed = (name: string) => shown(menus().find((m) => buttonLabel(m.findNativeButton().getElement()) === name));
/** The bar's plain buttons, in order, without the menus' triggers. */
const barButtons = () => {
  const triggers = new Set(menus().map((m) => m.findNativeButton().getElement()));
  return actions()
    .findAllButtons()
    .map((b) => b.getElement())
    .filter((b) => !triggers.has(b) && !b.closest('[role="menu"]'));
};
const segments = () =>
  actions()
    .findSegmentedControl()!
    .findSegments()
    .map((s) => s.getElement().textContent);

/** A menu's items as the DOM shows them, in order: each label, and the reason a screen reader reads for a disabled one. */
const itemsOf = (menu: ButtonDropdownWrapper) => {
  act(() => menu.openDropdown());
  const items = menu.findItems().map((item) => {
    // The label alone: a disabled item also holds its reason.
    const el = item.find('[role="menuitem"]')?.getElement() ?? item.getElement();
    const describedBy = el.getAttribute('aria-describedby');
    const disabled = el.getAttribute('aria-disabled') === 'true';
    return { text: el.textContent, reason: disabled && describedBy ? (document.getElementById(describedBy)?.textContent ?? null) : null };
  });
  act(() => menu.openDropdown());
  return items;
};

/** The value of the Document Details field whose label reads `label`. */
const fieldValue = (label: string) =>
  [...document.querySelectorAll('[class*="key-label"]')].find((e) => e.textContent === label)?.nextElementSibling?.textContent;

/** What the page has requested: every request, the review settings among them, and reads of the document. */
const requests = () => ({
  all: state.graphql.mock.calls.length,
  reviewSettings: state.graphql.mock.calls.filter(([arg]) => String((arg as { query: string }).query).includes('getMyReviewQueues')).length,
  documentReads: getDocumentDetailsFromIds.mock.calls.length,
});

/** Opens Skip All Reviews from More Actions; the dialog titled `title`. */
const openSkipAll = async (moreActions: string, title: string) => {
  const menu = menuNamed(moreActions);
  act(() => menu.openDropdown());
  act(() => menu.findItemById('skip-all')!.click());
  return waitFor(() =>
    shown(
      createWrapper(document.body)
        .findAllModals()
        .find((m) => m.findHeader().getElement().textContent === title),
    ),
  );
};

/** In the open dialog: the Reason list's options, as shown. */
const reasonOptions = (modal: Awaited<ReturnType<typeof openSkipAll>>) => {
  const select = modal.findContent().findSelect()!;
  act(() => select.openDropdown());
  const labels = select
    .findDropdown()
    .findOptions()
    .map((o) => o.findLabel().getElement().textContent);
  act(() => select.closeDropdown());
  return labels;
};

/** In the open dialog: choose the reason shown as `label`, then confirm with the button labelled `confirm`. */
const chooseAndConfirm = (modal: Awaited<ReturnType<typeof openSkipAll>>, label: string, confirm: string) => {
  const select = modal.findContent().findSelect()!;
  const index = reasonOptions(modal).indexOf(label);
  expect(index, `${label} is not offered`).toBeGreaterThan(-1);
  act(() => select.openDropdown());
  act(() => select.selectOption(index + 1));
  expect(select.findTrigger().getElement().textContent).toBe(label);
  act(() =>
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === confirm)!
      .click(),
  );
};

const skipCall = () => state.graphql.mock.calls.find(([arg]) => String((arg as { query: string }).query).includes('skipAllSectionsReview'));

beforeEach(() => {
  document.body.innerHTML = '';
  resetReviewActionSettingsCache();
  state.app.setErrorMessage.mockReset();
  state.app.setSuccessMessage.mockReset();
  getDocumentDetailsFromIds.mockReset().mockResolvedValue([]);
  state.graphql.mockReset().mockImplementation(async ({ query }: { query: string }) => {
    if (String(query).includes('getMyReviewQueues')) return { data: { getMyReviewQueues: { defaultQueueEnabled: true, queues: [] } } };
    if (String(query).includes('skipAllSectionsReview')) return { data: { skipAllSectionsReview: { HITLStatus: 'Review Skipped' } } };
    return { data: {} };
  });
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the document page bar under fr-CA', () => {
  it('reads its switch, menus, items and buttons and the Evaluation field in French, loading nothing again', async () => {
    renderPanel(doc(), FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() => expect(menuNames()).toEqual(['Rapports', 'Plus d’actions']));
    expect(segments()).toEqual(['Détails', 'Original']);
    expect(actions().findSegmentedControl()!.find('[aria-label]')?.getElement().getAttribute('aria-label')).toBe(
      'Afficher les détails du document ou sa source',
    );
    expect(barButtons().map(buttonLabel)).toEqual(['Actualiser', 'Dépanner', 'Commencer la révision', 'Modifier']);
    expect(itemsOf(menuNamed('Rapports')).map((i) => i.text)).toEqual([
      'Résumé',
      'Rapport d’évaluation',
      'Synthèse de la validation des règles',
      'Coût estimé',
      'Flux de traitement',
    ]);
    expect(itemsOf(menuNamed('Plus d’actions')).map((i) => i.text)).toEqual([
      'Modifier le regroupement des pages',
      'Réextraire des sections',
      'Modifier le texte d’une page',
      'Réinitialiser la classe de page',
      'Utiliser comme référence d’évaluation',
      'Ignorer toutes les révisions',
      'Envoyer en révision',
      'Tout télécharger (ZIP)',
      'Télécharger les prédictions (ZIP)',
      'Télécharger les références (ZIP)',
      'Retraiter',
      'Interrompre',
      'Supprimer',
    ]);
    expect(fieldValue('Évaluation')).toBe('Référence disponible');
    // The catalog arrived after the first render, as a new `intl`: the page requested the review
    // settings once, nothing else, and did not read the document again.
    expect(requests()).toEqual({ all: 1, reviewSettings: 1, documentReads: 0 });
  });

  it('gives the reason an action is unavailable in a review queue, and Edit’s name there, in French', async () => {
    renderPanel(doc({ hitlStatus: 'in_review', evaluationStatus: undefined }), FR_CA);
    await waitFor(() => expect(menuNames()).toEqual(['Rapports', 'Plus d’actions']));
    expect(itemsOf(menuNamed('Plus d’actions')).find((i) => i.text === 'Réextraire des sections')).toEqual({
      text: 'Réextraire des sections',
      reason: 'Ce document est dans une file de révision. Choisissez Modifier pour y travailler.',
    });
    const edit = barButtons().find((b) => buttonLabel(b) === 'Modifier')!;
    expect(edit.getAttribute('aria-label')).toBe('Modifier ce document dans sa file de révision');
    expect(fieldValue('Évaluation')).toBe('S. O.');
  });

  it('lists the fixed reasons of Skip All Reviews in French, and sends the English one', async () => {
    renderPanel(doc(), FR_CA);
    await waitFor(() => expect(menuNames()).toEqual(['Rapports', 'Plus d’actions']));
    const modal = await openSkipAll('Plus d’actions', 'Ignorer toutes les révisions de section');
    expect(modal.findContent().findFormField()!.findLabel()!.getElement().textContent).toBe('Motif');
    expect(reasonOptions(modal)).toEqual([
      'Les valeurs extraites semblent exactes',
      'Révision inutile pour ce document',
      'Document en double',
      'Autre (voir la note)',
    ]);
    chooseAndConfirm(modal, 'Document en double', 'Tout ignorer');
    await waitFor(() => expect(skipCall()).toBeDefined());
    expect(skipCall()![0].variables).toEqual({ objectKey: 'in/a.pdf', reason: 'Duplicate document', note: undefined, resubmit: true });
    await waitFor(() => expect(state.app.setSuccessMessage).toHaveBeenCalledWith('Révision de a.pdf ignorée.'));
  });
});

describe('the document page bar under en', () => {
  it('reads its switch, menus, items and buttons and the Evaluation field in English, loading nothing again', async () => {
    renderPanel(doc());
    await settle();
    expect(menuNames()).toEqual(['Reports', 'More Actions']);
    expect(segments()).toEqual(['Details', 'Source']);
    expect(actions().findSegmentedControl()!.find('[aria-label]')?.getElement().getAttribute('aria-label')).toBe(
      "Show the document's details or its source",
    );
    expect(barButtons().map(buttonLabel)).toEqual(['Refresh', 'Troubleshoot', 'Start Review', 'Edit']);
    expect(itemsOf(menuNamed('Reports')).map((i) => i.text)).toEqual([
      'Summary',
      'Evaluation Report',
      'Rule Validation Summary',
      'Estimated Cost',
      'Processing Flow',
    ]);
    expect(itemsOf(menuNamed('More Actions')).map((i) => i.text)).toEqual([
      'Edit Page Grouping',
      'Re-extract Sections',
      'Edit Page Text',
      'Reset Page Class',
      'Use As Evaluation Baseline',
      'Skip All Reviews',
      'Send to review',
      'Download All (ZIP)',
      'Download Predictions (ZIP)',
      'Download Baselines (ZIP)',
      'Reprocess',
      'Abort',
      'Delete',
    ]);
    expect(fieldValue('Evaluation')).toBe('Baseline available');
    expect(requests()).toEqual({ all: 1, reviewSettings: 1, documentReads: 0 });
  });

  it('gives the reason an action is unavailable in a review queue, and Edit’s name there, in English', async () => {
    renderPanel(doc({ hitlStatus: 'in_review', evaluationStatus: undefined }));
    await settle();
    expect(itemsOf(menuNamed('More Actions')).find((i) => i.text === 'Re-extract Sections')).toEqual({
      text: 'Re-extract Sections',
      reason: 'This document is in a review queue. Choose Edit to work on it there.',
    });
    const edit = barButtons().find((b) => buttonLabel(b) === 'Edit')!;
    expect(edit.getAttribute('aria-label')).toBe('Edit this document in its review queue');
    expect(fieldValue('Evaluation')).toBe('N/A');
  });

  it('lists the fixed reasons of Skip All Reviews in English, and sends the one chosen', async () => {
    renderPanel(doc());
    await settle();
    const modal = await openSkipAll('More Actions', 'Skip All Section Reviews');
    expect(modal.findContent().findFormField()!.findLabel()!.getElement().textContent).toBe('Reason');
    expect(reasonOptions(modal)).toEqual([
      'Extracted values look correct',
      'Review not needed for this document',
      'Duplicate document',
      'Other (see note)',
    ]);
    chooseAndConfirm(modal, 'Duplicate document', 'Skip All Reviews');
    await waitFor(() => expect(skipCall()).toBeDefined());
    expect(skipCall()![0].variables).toEqual({ objectKey: 'in/a.pdf', reason: 'Duplicate document', note: undefined, resubmit: true });
    await waitFor(() => expect(state.app.setSuccessMessage).toHaveBeenCalledWith('Skipped the review of a.pdf.'));
  });
});
