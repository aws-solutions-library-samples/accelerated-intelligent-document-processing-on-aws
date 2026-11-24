// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Edit Page Grouping under `fr-CA`: with a fixture catalog the board's title, buttons, menu items
 * and screen-reader labels, its page and section counts, the selection's count and labels, the
 * drag label, and its grouping problems (returned as codes, worded by the editor) read French. So
 * do the dialog the edit page and the document page open it in, with its thumbnails alert, the
 * confirmation before Save and Re-extract with its list of sections, the dialog that reports a
 * re-extract that failed after the save, and the confirmation's version form. The board signs its
 * thumbnails once: not again when the catalog arrives, nor on a switch to English. Under `en` the
 * same renders read today's English, with `page 5` for a one-page section.
 */
import React, { useEffect } from 'react';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import userEvent from '@testing-library/user-event';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));

const state = vi.hoisted(() => ({
  app: { setSuccessMessage: () => undefined },
  presign: (() => undefined) as unknown as (...args: unknown[]) => Promise<unknown>,
}));
vi.mock('../../../contexts/app', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/app')>()),
  default: () => state.app,
}));
vi.mock('../../../api/presign-batch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../api/presign-batch')>()),
  presignBatch: (...args: unknown[]) => state.presign(...args),
}));

import PageGroupingEditor from '../PageGroupingEditor';
import ProcessChangesConfirmModal from '../ProcessChangesConfirmModal';
import type { GroupedSection } from '../section-grouping';
import type { EditBoard } from '../../edit-screen/edit-model';
import usePageClassBoard from '../../edit-screen/use-page-class-board';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'common.pageGrouping.pageLabel': 'Page n° {id}',
  'common.pageGrouping.movePage': 'Déplacer la page {id}',
  'common.pageGrouping.movePageWithSelection':
    '{others, plural, one {Déplacer la page {id} et # autre page sélectionnée} other {Déplacer la page {id} et # autres pages sélectionnées}}',
  'common.pageGrouping.drag': 'Page {id}, glissez-la pour la réordonner ou la déplacer vers une autre section',
  'common.pageGrouping.dragWithSelection':
    '{others, plural, one {Page {id}, glissez-la pour la déplacer avec # autre page sélectionnée} other {Page {id}, glissez-la pour la déplacer avec # autres pages sélectionnées}}',
  'common.pageGrouping.selectPage': 'Sélectionner la page {id}',
  'common.pageGrouping.viewFullPage': 'Voir la page entière',
  'common.pageGrouping.orderWithin': 'Ordre dans cette section',
  'common.pageGrouping.moveEarlier': 'Déplacer plus tôt',
  'common.pageGrouping.moveLater': 'Déplacer plus tard',
  'common.pageGrouping.moveToSection': 'Déplacer vers la section',
  'common.pageGrouping.sectionOption': 'Section n° {id}',
  'common.pageGrouping.sectionOptionWithClass': 'Section n° {id} ({class})',
  'common.pageGrouping.sortSection': 'Remettre les pages de la section {id} dans l’ordre du document',
  'common.pageGrouping.deleteSection': 'Supprimer la section {id}',
  'common.pageGrouping.sectionHeader': 'Section n° {id}',
  'common.pageGrouping.chooseClass': 'Choisissez une classe de document',
  'common.pageGrouping.customOrder': 'Ordre des pages personnalisé',
  'common.pageGrouping.dropHere': 'Déposez une page ici.',
  'common.pageGrouping.invalidHeader': 'Ce regroupement n’est pas encore valide',
  'common.pageGrouping.summary':
    '{pages, plural, one {# page} other {# pages}} dans {sections, plural, one {# section} other {# sections}}',
  'common.pageGrouping.selected':
    '{count, plural, one {# page sélectionnée — la déplacer la déplace seule} other {# pages sélectionnées — en déplacer une les déplace toutes}}',
  'common.pageGrouping.clearSelection': 'Effacer la sélection',
  'common.pageGrouping.addSection': 'Ajouter une section',
  'common.pageGrouping.expand': 'Agrandir',
  'common.pageGrouping.draggingPages': '{count} pages déplacées',
  'common.pageGrouping.cancel': 'Annuler',
  'common.pageGrouping.defaultSave': 'Enregistrer le regroupement',
  'common.pageGrouping.defaultTitle': 'Regroupement des pages',
  'common.pageGrouping.close': 'Fermer',
  'common.sectionGrouping.noPages': 'Cette section n’a aucune page. Déplacez-y une page, ou supprimez la section.',
  'common.sectionGrouping.pagesNotInSection':
    '{count, plural, one {La page {pageIds} n’est dans aucune section. Chaque page doit appartenir à une section.} other {Les pages {pageIds} ne sont dans aucune section. Chaque page doit appartenir à une section.}}',
  'common.labels.processChanges': 'Traiter les modifications',
  'common.processChangesModal.cancel': 'Annuler',
  'common.processChangesModal.confirm': 'Confirmer et traiter',
  'common.processChangesModal.reprocessHeader': 'Confirmer le retraitement',
  'common.processChangesModal.versionHeader': 'Ouvrir une nouvelle version et enregistrer la vérité terrain',
  'common.processChangesModal.versionBody':
    '{processChanges} ouvre la prochaine version de ce jeu de test (la version actuelle reste telle quelle) et enregistre vos étiquettes comme vérité terrain révisée.',
  'common.processChangesModal.regroupIntro':
    'Vous allez enregistrer le nouveau regroupement des pages, puis extraire de nouveau ces sections :',
  'common.processChangesModal.sectionLine':
    '{count, plural, one {Section {id} : {class}, page {pages}} other {Section {id} : {class}, pages {pages}}}',
  'common.processChangesModal.willDo': 'Cela va :',
  'common.processChangesModal.effect.values': 'Remplacer leurs valeurs extraites, y compris celles corrigées à la main',
  'editScreen.pageGrouping.title': 'Modifier le regroupement des pages',
  'editScreen.pageGrouping.thumbnailsUnavailable': 'Miniatures des pages non disponibles',
  'editScreen.pageGrouping.save': 'Enregistrer le regroupement des pages',
  'editScreen.pageGrouping.saveAndReextract': 'Enregistrer et réextraire',
  'editScreen.pageGrouping.close': 'Fermer',
  'editScreen.pageGrouping.reextractAgain': 'Réextraire de nouveau',
  'editScreen.pageGrouping.partialFailed':
    'Le regroupement des pages est enregistré, mais les sections n’ont pas été extraites de nouveau : {message}',
  'editScreen.pageGrouping.sectionLine':
    '{count, plural, one {Section {id} : {class}, page {pages}} other {Section {id} : {class}, pages {pages}}}',
  'documents.pageThumbnails.failed': 'Impossible de charger les miniatures des pages.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;
type IntlOptions = Parameters<typeof renderWithIntl>[1];

const PAGES = [1, 2, 3, 4].map((id) => ({ id, imageUri: `blob:page-${id}` }));
const SECTIONS: GroupedSection[] = [
  { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
  { sectionId: '2', documentClass: 'Invoice', pageIds: [3, 4] },
];
const CLASSES = [
  { label: 'FieldTicket', value: 'FieldTicket' },
  { label: 'Invoice', value: 'Invoice' },
];

const renderEditor = (props: Partial<React.ComponentProps<typeof PageGroupingEditor>> = {}, options: IntlOptions = {}) =>
  renderWithIntl(
    <PageGroupingEditor
      pages={PAGES}
      sections={SECTIONS}
      classOptions={CLASSES}
      consequence="consequence"
      onSave={vi.fn()}
      onCancel={vi.fn()}
      {...props}
    />,
    options,
  );

/** The document page's board: thumbnails to sign, and a Save and Re-extract whose re-extract fails. */
const BOARD: EditBoard = {
  pages: { kind: 'thumbnails', pages: [1, 2, 3, 4].map((id) => ({ Id: String(id), ImageUri: `s3://bucket/page-${id}.png` })) },
  sections: () => SECTIONS,
  classOptions: CLASSES,
  canChangeClass: true,
  consequence: 'consequence',
  save: async () => null,
  reextract: {
    disabledReason: () => undefined,
    run: async () => {
      throw new Error('Throttled');
    },
  },
};

/** The board as EditPage and the document page's actions host it, opened as soon as it mounts. */
const BoardHost = (): React.JSX.Element => {
  const { open, dialog } = usePageClassBoard(BOARD, null, null);
  useEffect(() => {
    open();
  }, []);
  return <>{dialog}</>;
};

const body = () => createWrapper(document.body);
const modalTitled = (title: string) =>
  body()
    .findAllModals()
    .find((m) => m.findHeader().getElement().textContent === title && m.isVisible());
const footerLabels = (title: string) =>
  modalTitled(title)!
    .findFooter()!
    .findAllButtons()
    .map((b) => b.getElement().textContent);
const listItems = (title: string) => [...modalTitled(title)!.findContent().getElement().querySelectorAll('li')].map((li) => li.textContent);
/** Open a page's menu, by the menu button's label. */
const openMenu = (label: string) => userEvent.click(screen.getByRole('button', { name: label }));
/** Start a keyboard drag on a page's thumbnail, the one drag jsdom can start. */
const startDrag = (label: string) => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { value: vi.fn(), writable: true, configurable: true });
  const handle = screen.getByLabelText(label);
  handle.focus();
  fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
};
const overlayText = () => document.body.querySelector('[data-page-grouping-overlay]')?.textContent;

let presignCalls = 0;
beforeEach(() => {
  presignCalls = 0;
  state.presign = async () => {
    presignCalls += 1;
    return { get: () => undefined, size: 0, failed: true, accessDenied: false };
  };
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('Edit Page Grouping under fr-CA', () => {
  it('words the board’s title, columns, counts, buttons and screen-reader labels', async () => {
    renderEditor({ presentation: 'modal' }, FR_CA);
    expect(await screen.findByText('4 pages dans 2 sections')).toBeInTheDocument();
    expect(modalTitled('Regroupement des pages')).toBeDefined();
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['Section n° 1', 'Section n° 2']);
    expect(screen.getByAltText('Page n° 1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ajouter une section' })).toBeInTheDocument();
    expect(footerLabels('Regroupement des pages')).toEqual(['Annuler', 'Enregistrer le regroupement']);
    expect(screen.getByRole('checkbox', { name: 'Sélectionner la page 1' })).toBeInTheDocument();
    expect(screen.getByLabelText('Page 1, glissez-la pour la réordonner ou la déplacer vers une autre section')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Supprimer la section 2' })).toBeDisabled();
  });

  it('words a page’s menu, and the placeholder of a section with no class', async () => {
    renderEditor({ sections: [SECTIONS[0], { sectionId: '2', documentClass: null, pageIds: [3, 4] }] }, FR_CA);
    expect(await screen.findByText('Choisissez une classe de document')).toBeInTheDocument();
    await openMenu('Déplacer la page 1');
    expect(screen.getByRole('menuitem', { name: 'Voir la page entière' })).toBeInTheDocument();
    expect(screen.getByText('Ordre dans cette section')).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Déplacer plus tôt' })).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByRole('menuitem', { name: 'Déplacer plus tard' })).toBeInTheDocument();
    expect(screen.getByText('Déplacer vers la section')).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Section n° 2' })).toBeInTheDocument();
    await userEvent.keyboard('{Escape}');
    await openMenu('Déplacer la page 3');
    expect(screen.getByRole('menuitem', { name: 'Section n° 1 (FieldTicket)' })).toBeInTheDocument();
  });

  it('words the counts in the singular and the plural', async () => {
    renderEditor({ pages: PAGES.slice(0, 1), sections: [{ sectionId: '1', documentClass: 'FieldTicket', pageIds: [1] }] }, FR_CA);
    expect(await screen.findByText('1 page dans 1 section')).toBeInTheDocument();
  });

  it('words the selection, the labels of a selected page and the drag label', async () => {
    renderEditor({}, FR_CA);
    expect(await screen.findByText('4 pages dans 2 sections')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Sélectionner la page 1' }));
    expect(screen.getByText('1 page sélectionnée — la déplacer la déplace seule')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Sélectionner la page 2' }));
    expect(screen.getByText('2 pages sélectionnées — en déplacer une les déplace toutes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Déplacer la page 1 et 1 autre page sélectionnée' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Sélectionner la page 3' }));
    expect(screen.getByRole('button', { name: 'Déplacer la page 1 et 2 autres pages sélectionnées' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Effacer la sélection' })).toBeInTheDocument();
    startDrag('Page 1, glissez-la pour la déplacer avec 2 autres pages sélectionnées');
    await waitFor(() => expect(overlayText()).toBe('3 pages déplacées'));
  });

  it('words the grouping’s problems from their codes', async () => {
    // Page 4 is in no section; moving page 3 out of section 2 leaves that section empty.
    renderEditor(
      {
        sections: [
          { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
          { sectionId: '2', documentClass: 'Invoice', pageIds: [3] },
        ],
      },
      FR_CA,
    );
    const alert = await screen.findByText('Ce regroupement n’est pas encore valide');
    expect(alert).toBeInTheDocument();
    expect(screen.getByText('La page 4 n’est dans aucune section. Chaque page doit appartenir à une section.')).toBeInTheDocument();
    await openMenu('Déplacer la page 3');
    await userEvent.click(screen.getByRole('menuitem', { name: 'Section n° 1 (FieldTicket)' }));
    expect(screen.getByText('Cette section n’a aucune page. Déplacez-y une page, ou supprimez la section.')).toBeInTheDocument();
    expect(screen.getByText('Déposez une page ici.')).toBeInTheDocument();
  });

  it('words the confirmation before Save and Re-extract, with page 5 for a one-page section', async () => {
    renderWithIntl(
      <ProcessChangesConfirmModal
        visible
        reextractSections={[
          { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3] },
          { sectionId: '3', documentClass: 'Invoice', pageIds: [5] },
        ]}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
      FR_CA,
    );
    await waitFor(() => expect(modalTitled('Confirmer le retraitement')).toBeDefined());
    expect(listItems('Confirmer le retraitement').slice(0, 3)).toEqual([
      'Section 1 : FieldTicket, pages 1, 2, 3',
      'Section 3 : Invoice, page 5',
      'Remplacer leurs valeurs extraites, y compris celles corrigées à la main',
    ]);
    expect(modalTitled('Confirmer le retraitement')!.findContent().getElement().textContent).toContain(
      'Vous allez enregistrer le nouveau regroupement des pages, puis extraire de nouveau ces sections :',
    );
    expect(footerLabels('Confirmer le retraitement')).toEqual(['Annuler', 'Confirmer et traiter']);
  });

  it('words the confirmation’s version form with the shared Process Changes name', async () => {
    renderWithIntl(<ProcessChangesConfirmModal visible variant="version" onConfirm={vi.fn()} onCancel={vi.fn()} />, FR_CA);
    const title = 'Ouvrir une nouvelle version et enregistrer la vérité terrain';
    await waitFor(() => expect(modalTitled(title)).toBeDefined());
    expect(modalTitled(title)!.findContent().getElement().textContent).toBe(
      'Traiter les modifications ouvre la prochaine version de ce jeu de test (la version actuelle reste telle quelle) et enregistre vos étiquettes comme vérité terrain révisée.',
    );
    expect(footerLabels(title)).toEqual(['Annuler', 'Traiter les modifications']);
  });

  it('words the board’s dialog, the confirmation and the failed re-extract, and signs the thumbnails once', async () => {
    const view = renderWithIntl(<BoardHost />, FR_CA);
    const title = 'Modifier le regroupement des pages';
    await waitFor(() => expect(modalTitled(title)).toBeDefined());
    expect(await screen.findByText('Miniatures des pages non disponibles')).toBeInTheDocument();
    expect(screen.getByText('Impossible de charger les miniatures des pages.')).toBeInTheDocument();
    expect(footerLabels(title)).toEqual(['Annuler', 'Enregistrer et réextraire', 'Enregistrer le regroupement des pages']);
    expect(presignCalls).toBe(1);

    await openMenu('Déplacer la page 3');
    await userEvent.click(screen.getByRole('menuitem', { name: 'Section n° 1 (FieldTicket)' }));
    await userEvent.click(screen.getByRole('button', { name: 'Enregistrer et réextraire' }));
    await waitFor(() => expect(modalTitled('Confirmer le retraitement')).toBeDefined());
    expect(listItems('Confirmer le retraitement').slice(0, 2)).toEqual([
      'Section 1 : FieldTicket, pages 1, 2, 3',
      'Section 2 : Invoice, page 4',
    ]);

    await act(async () => {
      modalTitled('Confirmer le retraitement')!
        .findFooter()!
        .findAllButtons()
        .find((b) => b.getElement().textContent === 'Confirmer et traiter')!
        .click();
    });
    await waitFor(() => expect(modalTitled(title)?.findContent().getElement().textContent).toContain('Throttled'));
    const failure = modalTitled(title)!;
    expect(within(failure.findContent().getElement()).getByText(/^Le regroupement des pages est enregistré/).textContent).toBe(
      'Le regroupement des pages est enregistré, mais les sections n’ont pas été extraites de nouveau : Throttled',
    );
    expect(listItems(title)).toEqual(['Section 1 : FieldTicket, pages 1, 2, 3', 'Section 2 : Invoice, page 4']);
    expect(footerLabels(title)).toEqual(['Fermer', 'Réextraire de nouveau']);

    // The thumbnails were signed when the board opened, not again since: not when the catalog
    // arrived, nor now that the language changes.
    view.setLocale('en');
    expect(modalTitled('Edit Page Grouping')).toBeDefined();
    expect(listItems('Edit Page Grouping')).toEqual(['Section 1: FieldTicket, pages 1, 2, 3', 'Section 2: Invoice, page 4']);
    expect(presignCalls).toBe(1);
  });
});

describe('Edit Page Grouping under en', () => {
  it('keeps the board’s English', () => {
    renderEditor({ presentation: 'modal' });
    expect(screen.getByText('4 pages · 2 sections')).toBeInTheDocument();
    expect(modalTitled('Edit Page Grouping')).toBeDefined();
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['Section 1', 'Section 2']);
    expect(screen.getByAltText('Page 1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add section' })).toBeInTheDocument();
    expect(footerLabels('Edit Page Grouping')).toEqual(['Cancel', 'Save grouping']);
    expect(screen.getByRole('checkbox', { name: 'Select page 1' })).toBeInTheDocument();
    expect(screen.getByLabelText('Page 1, drag to reorder it or move it to another section')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete section 2' })).toBeDisabled();
  });

  it('keeps a page’s menu and the class placeholder in English', async () => {
    renderEditor({ sections: [SECTIONS[0], { sectionId: '2', documentClass: null, pageIds: [3, 4] }] });
    expect(screen.getByText('Choose a document class')).toBeInTheDocument();
    await openMenu('Move page 1');
    expect(screen.getByRole('menuitem', { name: 'View full page' })).toBeInTheDocument();
    expect(screen.getByText('Order within this section')).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Move earlier' })).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByRole('menuitem', { name: 'Move later' })).toBeInTheDocument();
    expect(screen.getByText('Move to section')).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Section 2' })).toBeInTheDocument();
    await userEvent.keyboard('{Escape}');
    await openMenu('Move page 3');
    expect(screen.getByRole('menuitem', { name: 'Section 1 (FieldTicket)' })).toBeInTheDocument();
  });

  it('keeps the counts in the singular and the plural', () => {
    renderEditor({ pages: PAGES.slice(0, 1), sections: [{ sectionId: '1', documentClass: 'FieldTicket', pageIds: [1] }] });
    expect(screen.getByText('1 page · 1 section')).toBeInTheDocument();
  });

  it('keeps the selection, the labels of a selected page and the drag label', async () => {
    renderEditor();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select page 1' }));
    expect(screen.getByText('1 page selected — moving any one moves all of them')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select page 2' }));
    expect(screen.getByText('2 pages selected — moving any one moves all of them')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Move page 1 and 1 other selected page' })).toBeInTheDocument();
    expect(screen.getByLabelText('Page 1, drag to move it and 1 other selected page')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select page 3' }));
    expect(screen.getByRole('button', { name: 'Move page 1 and 2 other selected pages' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clear selection' })).toBeInTheDocument();
    startDrag('Page 1, drag to move it and 2 other selected pages');
    await waitFor(() => expect(overlayText()).toBe('3 pages'));
  });

  it('keeps the grouping’s problems in English', async () => {
    renderEditor({
      sections: [
        { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
        { sectionId: '2', documentClass: 'Invoice', pageIds: [3] },
      ],
    });
    expect(screen.getByText('This grouping is not valid yet')).toBeInTheDocument();
    expect(screen.getByText('Page 4 is not in any section. Every page has to belong to one.')).toBeInTheDocument();
    await openMenu('Move page 3');
    await userEvent.click(screen.getByRole('menuitem', { name: 'Section 1 (FieldTicket)' }));
    expect(screen.getByText('This section has no pages. Move a page into it, or delete the section.')).toBeInTheDocument();
    expect(screen.getByText('Drop a page here.')).toBeInTheDocument();
  });

  it('keeps the confirmation’s English, with page 5 for a one-page section', () => {
    renderWithIntl(
      <ProcessChangesConfirmModal
        visible
        reextractSections={[
          { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3] },
          { sectionId: '3', documentClass: 'Invoice', pageIds: [5] },
        ]}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(listItems('Confirm Reprocessing').slice(0, 3)).toEqual([
      'Section 1: FieldTicket, pages 1, 2, 3',
      'Section 3: Invoice, page 5',
      'Replace their extracted values, including any corrected by hand',
    ]);
    expect(footerLabels('Confirm Reprocessing')).toEqual(['Cancel', 'Confirm & Process']);
  });

  it('keeps the confirmation’s version form in English', () => {
    renderWithIntl(<ProcessChangesConfirmModal visible variant="version" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    const title = 'Open a new version and save ground truth';
    expect(modalTitled(title)!.findContent().getElement().textContent).toBe(
      'Process Changes opens the next version of this test set (the current version stays as it is) and writes your labels as reviewed ground truth.',
    );
    expect(footerLabels(title)).toEqual(['Cancel', 'Process Changes']);
  });

  it('keeps the board’s dialog, the confirmation and the failed re-extract in English', async () => {
    renderWithIntl(<BoardHost />);
    const title = 'Edit Page Grouping';
    await waitFor(() => expect(screen.getByText('Page thumbnails unavailable')).toBeInTheDocument());
    expect(screen.getByText('Page thumbnails could not be loaded.')).toBeInTheDocument();
    expect(footerLabels(title)).toEqual(['Cancel', 'Save and Re-extract', 'Save page grouping']);

    await openMenu('Move page 3');
    await userEvent.click(screen.getByRole('menuitem', { name: 'Section 1 (FieldTicket)' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save and Re-extract' }));
    await waitFor(() => expect(modalTitled('Confirm Reprocessing')).toBeDefined());
    expect(listItems('Confirm Reprocessing').slice(0, 2)).toEqual(['Section 1: FieldTicket, pages 1, 2, 3', 'Section 2: Invoice, page 4']);

    await act(async () => {
      modalTitled('Confirm Reprocessing')!
        .findFooter()!
        .findAllButtons()
        .find((b) => b.getElement().textContent === 'Confirm & Process')!
        .click();
    });
    await waitFor(() => expect(modalTitled(title)?.findContent().getElement().textContent).toContain('Throttled'));
    expect(within(modalTitled(title)!.findContent().getElement()).getByText(/^The page grouping was saved/).textContent).toBe(
      'The page grouping was saved, but the sections were not re-extracted: Throttled',
    );
    expect(listItems(title)).toEqual(['Section 1: FieldTicket, pages 1, 2, 3', 'Section 2: Invoice, page 4']);
    expect(footerLabels(title)).toEqual(['Close', 'Re-extract again']);
  });
});
