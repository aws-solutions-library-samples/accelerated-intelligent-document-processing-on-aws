// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The edit page's section editor under `fr-CA`: with a fixture catalog its title, the Section and
 * Show fields menus, the Visual Editor and JSON Editor tabs, a field row's confidence line, badges
 * and value labels (for a prediction and for a test set's ground truth), a list's heading, the
 * bounding-box caption and a field's reason for having no box, the JSON Editor tab's top line and
 * line count, and a failed load's error read French. The section is read once: not again when the
 * catalog arrives, nor on a switch to English. Under `en` the same renders read today's English,
 * with `(1 item)` and `1 line` for counts of one.
 */
import React from 'react';
import { act, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import userEvent from '@testing-library/user-event';
import { I18n } from 'aws-amplify/utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../common/PageImageViewer', () => ({ default: () => null }));
vi.mock('../../common/ClassMismatchIndicator', () => ({ SectionClassEvaluation: () => null }));

import FormFieldRenderer from '../../document-viewer/FormFieldRenderer';
import SectionEditor from '../SectionEditor';
import type { LoadedSection, ReviewSectionRef } from '../types';
import type { SectionEvaluation } from '../use-section-evaluation';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'editScreen.shared.documentEditor': 'Éditeur de document',
  'editScreen.sectionEditor.sectionTab': 'Section {sectionId} ({documentClass})',
  'editScreen.sectionEditor.sectionTabNoClass': 'Section {sectionId} (sans classe)',
  'editScreen.sectionEditor.sectionAriaLabel': 'Section : {section}',
  'editScreen.sectionEditor.filterAriaLabel': 'Champs affichés : {filter}',
  'editScreen.sectionEditor.filter.all': 'Tous les champs',
  'editScreen.sectionEditor.filter.alerts': 'Alertes de confiance seulement',
  'editScreen.sectionEditor.filter.evalMismatches': 'Écarts d’évaluation seulement',
  'editScreen.sectionEditor.filterNeedsEvaluation': 'Activez d’abord Afficher l’évaluation',
  'editScreen.sectionEditor.showEvaluation': 'Afficher l’évaluation',
  'editScreen.sectionEditor.tab.visual': 'Éditeur visuel',
  'editScreen.sectionEditor.tab.json': 'Éditeur JSON',
  'editScreen.sectionEditor.loadFailed': 'Impossible de charger la vérité terrain : {error}',
  'editScreen.sectionEditor.noValues.header': 'Aucune valeur de champ pour cette section',
  'editScreen.sectionEditor.noValues.empty': 'Cette section a un résultat vide : l’extraction n’a produit aucun champ.',
  'documents.formField.rootHeading': 'Données du document',
  'documents.formField.edited': '✏️ Modifié',
  'documents.formField.baselineEdited': '✏️ Référence modifiée',
  'documents.formField.mismatch': '⚠ Écart',
  'documents.formField.match': '✓ Correspond',
  'documents.formField.confidence': 'Confiance : {confidence}',
  'documents.formField.editedNoConfidence': 'Modifié — la confiance du modèle ne s’applique plus',
  'documents.formField.expectedBaseline': 'Attendu (référence) :',
  'documents.formField.coverage': '{located} sur {total, plural, one {# champ a} other {# champs ont}} une zone de délimitation',
  'documents.formField.itemCount': '{label} ({count, plural, one {# élément} other {# éléments}})',
  'documents.jsonEditor.titleEditor': '<strong>Éditeur JSON</strong> - Afficher et modifier les données JSON brutes.',
  'documents.jsonEditor.lines': '{count, plural, one {# ligne} other {# lignes}}',
  'common.labels.valueSource.predicted': 'Prédit :',
  'common.labels.valueSource.groundTruth': 'Vérité terrain :',
  'common.labels.valueSource.yourValue': 'Votre valeur :',
  'common.labels.valueSource.predictedPanel': 'Résultats prédits',
  'common.labels.valueSource.groundTruthPanel': 'Vérité terrain',
  'common.geometry.highlight': 'Surligner {field} sur l’image de la page',
  'common.geometry.why': 'Pourquoi {field} n’a pas de zone de délimitation',
  'common.geometry.noValue': 'Aucune valeur extraite, donc aucune zone de délimitation.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;
type IntlOptions = Parameters<typeof renderWithIntl>[1];

const SECTIONS: ReviewSectionRef[] = [
  { sectionId: '1', documentClass: 'Invoice', resultUri: 's3://b/1', pageIds: [] },
  { sectionId: '2', documentClass: null, resultUri: 's3://b/2', pageIds: [] },
];
const BOX = [{ boundingBox: { left: 0.1, top: 0.2, width: 0.3, height: 0.04 }, page: 1 }];
/** Three fields: a total with a box, an empty note and a one-item list; only the total is located. */
const RESULT = {
  document_class: { type: 'Invoice' },
  inference_result: { Total: '100.00', Note: '', Items: ['A-1'] },
  explainability_info: [{ Total: { confidence: 1.0, geometry: BOX }, Note: { confidence: 0.4 } }],
};
/** A baseline that agrees on the total and not on the list's item, and whose note was edited here. */
const BASELINE = { document_class: { type: 'Invoice' }, inference_result: { Total: '100.00', Note: 'Due', Items: ['A-9'] } };
const EVALUATION = {
  available: true,
  loading: false,
  baseline: BASELINE,
  originalBaseline: { ...BASELINE, inference_result: { ...BASELINE.inference_result, Note: 'Paid' } },
  setBaseline: vi.fn(),
  markBaselineSaved: vi.fn(),
  evaluationResults: null,
  classificationIndex: null,
  canEditBaseline: false,
} as unknown as SectionEvaluation;

const loaded = (data: Record<string, unknown>) => async (): Promise<LoadedSection> => ({ data, raw: JSON.stringify(data) });

const renderEditor = (
  props: Partial<React.ComponentProps<typeof SectionEditor>> = {},
  options: IntlOptions = {},
  data: Record<string, unknown> = RESULT,
) => {
  const loadSection = vi.fn(loaded(data));
  const view = renderWithIntl(
    <SectionEditor
      objectKey="in/doc.pdf"
      sections={SECTIONS}
      pages={[]}
      isReadOnly={false}
      loadSection={loadSection}
      onSave={async () => undefined}
      {...props}
    />,
    options,
  );
  return { ...view, loadSection };
};

const panel = () => createWrapper(document.body).findContainer()!;
const inPanel = () => createWrapper(panel().getElement());
const title = () => createWrapper(panel().findHeader()!.getElement()).findHeader()!.findHeadingText().getElement().textContent;
/** A dropdown button of the panel's header, by its place: the Section menu first, then Show fields. */
const menu = (index: number) => createWrapper(panel().findHeader()!.getElement()).findAllButtonDropdowns()[index];
const menuName = (index: number) => menu(index).findNativeButton().getElement().getAttribute('aria-label');
/** A menu's items: their text, without the reason of a disabled one, and that reason. */
const menuItems = (index: number) => {
  act(() => menu(index).openDropdown());
  const items = menu(index)
    .findItems()
    .map((i) => {
      const el = i.getElement().querySelector('[role="menuitemcheckbox"]') ?? i.getElement();
      const describedBy = el.getAttribute('aria-describedby');
      const reason = describedBy ? (document.getElementById(describedBy)?.textContent ?? '') : '';
      const text = i.getElement().textContent ?? '';
      return { text: reason && text.endsWith(reason) ? text.slice(0, -reason.length) : text, reason };
    });
  act(() => menu(index).openDropdown());
  return items;
};
const tabLabels = () =>
  inPanel()
    .findTabs()!
    .findTabLinks()
    .map((t) => t.getElement().textContent);
const openJsonTab = () => act(() => inPanel().findTabs()!.findTabLinkById('json')!.click());
/** A field row of the Visual Editor tab, by the field's path. */
const row = (path: string) => document.querySelector(`[data-field-path="${path}"]`) as HTMLElement;
/**
 * An edited field, rendered on the field renderer itself, as its provenance test does: the renderer
 * does not pass `editedFieldPaths` on to nested fields.
 */
const editedTotal = (
  <FormFieldRenderer
    fieldKey="Total"
    value="105.00"
    onChange={vi.fn()}
    isReadOnly={false}
    explainabilityInfo={RESULT.explainability_info}
    path={['Total']}
    editedFieldPaths={new Set(['Total'])}
  />
);
const toggleEvaluation = () => act(() => createWrapper(panel().findHeader()!.getElement()).findToggle()!.findNativeInput().click());
/** The JSON Editor tab's top line and the values panel's line count. */
const jsonTopLine = () => inPanel().findTabs()!.findTabContent()!.find('strong')!.getElement().parentElement!.textContent;
const lineCount = () => inPanel().findTabs()!.findTabContent()!.findBadge()!.getElement().textContent;

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the section editor under fr-CA', () => {
  it('words its title, the Section and Show fields menus and the tabs', async () => {
    renderEditor({ evaluation: EVALUATION }, FR_CA);
    expect(await screen.findByText('Données du document')).toBeInTheDocument();
    expect(title()).toBe('Éditeur de document');
    expect(menuName(0)).toBe('Section : Section 1 (Invoice)');
    expect(menuItems(0).map((i) => i.text)).toEqual(['Section 1 (Invoice)', 'Section 2 (sans classe)']);
    expect(menuName(1)).toBe('Champs affichés : Tous les champs');
    expect(menuItems(1)).toEqual([
      { text: 'Tous les champs', reason: '' },
      { text: 'Alertes de confiance seulement', reason: '' },
      { text: 'Écarts d’évaluation seulement', reason: 'Activez d’abord Afficher l’évaluation' },
    ]);
    expect(createWrapper(panel().findHeader()!.getElement()).findToggle()!.getElement().textContent).toBe('Afficher l’évaluation');
    expect(tabLabels()).toEqual(['Éditeur visuel', 'Éditeur JSON']);
  });

  it('words a field row’s confidence, value label and bounding-box controls, a list’s heading and the caption', async () => {
    renderEditor({}, FR_CA);
    expect(await screen.findByText('Données du document')).toBeInTheDocument();
    expect(within(row('Total')).getByText(/^Confiance : 100,0\s%$/)).toBeInTheDocument();
    expect(screen.getAllByText('Prédit :')).toHaveLength(3);
    expect(within(row('Total')).getByRole('button', { name: 'Surligner Total sur l’image de la page' })).toBeInTheDocument();
    await userEvent.click(within(row('Note')).getByRole('button', { name: 'Pourquoi Note n’a pas de zone de délimitation' }));
    expect(await screen.findByText('Aucune valeur extraite, donc aucune zone de délimitation.')).toBeInTheDocument();
    expect(screen.getByText('Items (1 élément)')).toBeInTheDocument();
    expect(screen.getByText('1 sur 3 champs ont une zone de délimitation')).toBeInTheDocument();
  });

  it('words an edited field’s badge, value label and confidence note', async () => {
    renderWithIntl(editedTotal, FR_CA);
    expect(await screen.findByText('✏️ Modifié')).toBeInTheDocument();
    expect(screen.getByText('Votre valeur :')).toBeInTheDocument();
    expect(screen.getByText('Modifié — la confiance du modèle ne s’applique plus')).toBeInTheDocument();
  });

  it('words a test set’s ground truth in both tabs', async () => {
    renderEditor({}, FR_CA, { ...RESULT, labelSource: 'reviewed-human' });
    expect(await screen.findByText('Données du document')).toBeInTheDocument();
    expect(screen.getAllByText('Vérité terrain :')).toHaveLength(3);
    openJsonTab();
    expect(screen.getByText('Vérité terrain')).toBeInTheDocument();
  });

  it('words the comparison’s badges and the baseline’s label once Show Evaluation is on', async () => {
    renderEditor({ evaluation: EVALUATION }, FR_CA);
    expect(await screen.findByText('Données du document')).toBeInTheDocument();
    toggleEvaluation();
    expect(within(row('Total')).getByText('✓ Correspond')).toBeInTheDocument();
    expect(within(row('Items[0]')).getByText('⚠ Écart')).toBeInTheDocument();
    expect(within(row('Note')).getByText('✏️ Référence modifiée')).toBeInTheDocument();
    expect(within(row('Note')).getByText('Attendu (référence) :')).toBeInTheDocument();
  });

  it('words the JSON Editor tab’s top line and counts one line, and an empty section’s note', async () => {
    renderEditor({}, FR_CA, { inference_result: {} });
    expect(await screen.findByText('Aucune valeur de champ pour cette section')).toBeInTheDocument();
    expect(screen.getByText('Cette section a un résultat vide : l’extraction n’a produit aucun champ.')).toBeInTheDocument();
    openJsonTab();
    expect(jsonTopLine()).toBe('Éditeur JSON - Afficher et modifier les données JSON brutes.');
    expect(lineCount()).toBe('1 ligne');
    expect(screen.getByText('Résultats prédits')).toBeInTheDocument();
  });

  it('words a failed load’s error, reading the section once across the catalog’s arrival and a switch', async () => {
    const loadSection = vi.fn(async (): Promise<LoadedSection> => {
      throw new Error('Access denied');
    });
    const view = renderEditor({ loadSection }, FR_CA);
    expect(await screen.findByText('Impossible de charger la vérité terrain : Access denied')).toBeInTheDocument();
    expect(loadSection).toHaveBeenCalledTimes(1);
    view.setLocale('en');
    await waitFor(() => expect(screen.getByText('Failed to load ground truth: Access denied')).toBeInTheDocument());
    expect(loadSection).toHaveBeenCalledTimes(1);
  });
});

describe('the section editor under en', () => {
  it('reads today’s English in its title, menus and tabs', async () => {
    renderEditor({ evaluation: EVALUATION });
    expect(await screen.findByText('Document Data')).toBeInTheDocument();
    expect(title()).toBe('Document Editor');
    expect(menuName(0)).toBe('Section: Section 1 (Invoice)');
    expect(menuItems(0).map((i) => i.text)).toEqual(['Section 1 (Invoice)', 'Section 2 (no class)']);
    expect(menuName(1)).toBe('Show fields: All fields');
    expect(menuItems(1)).toEqual([
      { text: 'All fields', reason: '' },
      { text: 'Confidence alerts only', reason: '' },
      { text: 'Evaluation mismatches only', reason: 'Turn on Show Evaluation first' },
    ]);
    expect(tabLabels()).toEqual(['Visual Editor', 'JSON Editor']);
  });

  it('reads today’s English in a field row, the bounding-box controls and caption, and a list’s heading with one item', async () => {
    renderEditor();
    expect(await screen.findByText('Document Data')).toBeInTheDocument();
    expect(within(row('Total')).getByText(/^Confidence: 100\.0%$/)).toBeInTheDocument();
    expect(screen.getAllByText('Predicted:')).toHaveLength(3);
    expect(within(row('Total')).getByRole('button', { name: 'Highlight Total on the page image' })).toBeInTheDocument();
    await userEvent.click(within(row('Note')).getByRole('button', { name: 'Why Note has no bounding box' }));
    expect(await screen.findByText('No value extracted, so no bounding box.')).toBeInTheDocument();
    expect(screen.getByText('Items (1 item)')).toBeInTheDocument();
    expect(screen.getByText('1 of 3 fields have a bounding box')).toBeInTheDocument();
  });

  it('reads today’s English for an edited field and for ground truth', async () => {
    renderWithIntl(editedTotal);
    expect(await screen.findByText('✏️ Edited')).toBeInTheDocument();
    expect(screen.getByText('Your value:')).toBeInTheDocument();
    expect(screen.getByText("Edited — the model's confidence no longer applies")).toBeInTheDocument();
    document.body.innerHTML = '';
    renderEditor({}, {}, { ...RESULT, labelSource: 'reviewed-human' });
    expect(await screen.findAllByText('Ground truth:')).toHaveLength(3);
    openJsonTab();
    expect(screen.getByText('Ground truth')).toBeInTheDocument();
  });

  it('reads today’s English for the comparison’s badges', async () => {
    renderEditor({ evaluation: EVALUATION });
    expect(await screen.findByText('Document Data')).toBeInTheDocument();
    toggleEvaluation();
    expect(within(row('Total')).getByText('✓ Match')).toBeInTheDocument();
    expect(within(row('Items[0]')).getByText('⚠ Mismatch')).toBeInTheDocument();
    expect(within(row('Note')).getByText('✏️ Baseline Edited')).toBeInTheDocument();
    expect(within(row('Note')).getByText('Expected (baseline):')).toBeInTheDocument();
  });

  it('reads today’s English on the JSON Editor tab, with 1 line for one line', async () => {
    renderEditor({}, {}, { inference_result: {} });
    expect(await screen.findByText('No field values for this section')).toBeInTheDocument();
    openJsonTab();
    expect(jsonTopLine()).toBe('JSON Editor - View and edit the raw JSON data.');
    expect(lineCount()).toBe('1 line');
    expect(screen.getByText('Predicted Results')).toBeInTheDocument();
  });

  it('reads today’s English for a failed load, reading the section once', async () => {
    const loadSection = vi.fn(async (): Promise<LoadedSection> => {
      throw new Error('Access denied');
    });
    renderEditor({ loadSection });
    expect(await screen.findByText('Failed to load ground truth: Access denied')).toBeInTheDocument();
    expect(loadSection).toHaveBeenCalledTimes(1);
  });
});
