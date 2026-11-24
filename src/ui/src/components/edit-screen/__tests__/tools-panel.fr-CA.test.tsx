// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The edit page's help panel under `fr-CA`: with a fixture catalog its title and headings read
 * French, each control it names in bold stays inside its French sentence (Process Changes and
 * Next alert with the shared labels' French), and the Learn more links read French with the
 * same documentation URLs. Under `en` the same render reads today's English, and its footer
 * renders the same HTML as a footer written out link by link.
 */
import React from 'react';
import { screen, within } from '@testing-library/react';
import { HelpPanel, Icon } from '@cloudscape-design/components';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { DOCS_BASE_URL } from '../../../constants/github';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));

import EditScreenToolsPanel from '../tools-panel';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'editScreen.toolsPanel.title': 'Modifier le document',
  'editScreen.toolsPanel.heading.buttons': 'Boutons',
  'editScreen.toolsPanel.heading.reports': 'Rapports',
  'editScreen.toolsPanel.heading.moreActions': 'Plus d’actions',
  'editScreen.toolsPanel.heading.documentEditor': 'Éditeur de document',
  'editScreen.toolsPanel.button.refresh':
    '<b>Actualiser</b> recharge le document; s’il reste des modifications non enregistrées, il demande d’abord.',
  'editScreen.toolsPanel.button.processChanges':
    '<b>{processChanges}</b> enregistre, puis traite les modifications : l’évaluation et le résumé sont relancés.',
  'editScreen.toolsPanel.editor.buttons':
    'Ses boutons, à droite de son titre : <b>Afficher l’évaluation</b> (si le document a une référence d’évaluation), <b>Section</b>, <b>Afficher les champs</b> et <b>Modifier le regroupement des pages</b>, qui déplace des pages entre les sections et change la classe d’une section.',
  'editScreen.toolsPanel.editor.reviewReason':
    'La raison de révision d’un élément de file dit pourquoi il est en révision, la plus grave d’abord. Choisissez une raison, ou <b>{nextAlert}</b>, pour aller à son champ.',
  'editScreen.toolsPanel.link.reviewQueues': 'Files de révision',
  'editScreen.toolsPanel.link.humanReview': 'Révision humaine',
  'editScreen.toolsPanel.link.testStudio': 'Studio de test',
  'common.learnMoreLinks.heading': 'En savoir plus',
  'common.labels.processChanges': 'Traiter les modifications',
  'common.labels.nextAlert': 'Alerte suivante',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

/** The list item whose whole text is `text`. */
const item = (text: string): HTMLElement => screen.getByText((_, el) => el?.tagName === 'LI' && el.textContent === text);
const boldIn = (el: HTMLElement): string[] => Array.from(el.querySelectorAll('b'), (b) => b.textContent ?? '');
const subHeadings = (): string[] => screen.getAllByRole('heading', { level: 3 }).map((h) => (h.textContent ?? '').trim());
const links = (scope: HTMLElement = document.body): string[][] =>
  within(scope)
    .getAllByRole('link')
    .map((a) => [a.textContent ?? '', a.getAttribute('href') ?? '', a.getAttribute('target') ?? '', a.getAttribute('rel') ?? '']);
const footerHtml = (container: HTMLElement): string => createWrapper(container).findHelpPanel()!.findFooter()!.getElement().innerHTML;

/** A footer written out by hand, link by link: LearnMoreLinks renders the same HTML. */
const writtenFooter = (
  <div>
    <h3>
      Learn more <Icon name="external" />
    </h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE_URL}/review-queues/`} target="_blank" rel="noopener noreferrer">
          Review queues
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE_URL}/human-review/`} target="_blank" rel="noopener noreferrer">
          Human review
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE_URL}/test-studio/`} target="_blank" rel="noopener noreferrer">
          Test Studio
        </a>
      </li>
    </ul>
  </div>
);

const NEW_TAB = ['_blank', 'noopener noreferrer'];

describe('the edit page’s help panel under fr-CA', () => {
  it('reads its title and headings in French, then in English after a switch', async () => {
    const { setLocale } = renderWithIntl(<EditScreenToolsPanel />, FR_CA);
    expect(await screen.findByRole('heading', { level: 2, name: 'Modifier le document' })).toBeInTheDocument();
    expect(subHeadings()).toEqual(['Boutons', 'Rapports', 'Plus d’actions', 'Éditeur de document', 'En savoir plus']);
    expect(screen.queryByText('Edit Document')).toBeNull();

    setLocale('en');
    expect(screen.getByRole('heading', { level: 2, name: 'Edit Document' })).toBeInTheDocument();
    expect(subHeadings()).toEqual(['Buttons', 'Reports', 'More Actions', 'Document Editor', 'Learn more']);
  });

  it('keeps each bold control name inside its French sentence', async () => {
    renderWithIntl(<EditScreenToolsPanel />, FR_CA);
    await screen.findByRole('heading', { level: 2, name: 'Modifier le document' });
    const refresh = item('Actualiser recharge le document; s’il reste des modifications non enregistrées, il demande d’abord.');
    expect(boldIn(refresh)).toEqual(['Actualiser']);
    const editorButtons = item(
      'Ses boutons, à droite de son titre : Afficher l’évaluation (si le document a une référence d’évaluation), Section, Afficher les champs et Modifier le regroupement des pages, qui déplace des pages entre les sections et change la classe d’une section.',
    );
    expect(boldIn(editorButtons)).toEqual([
      'Afficher l’évaluation',
      'Section',
      'Afficher les champs',
      'Modifier le regroupement des pages',
    ]);
  });

  it('names Process Changes and Next alert in bold with the shared labels’ French', async () => {
    renderWithIntl(<EditScreenToolsPanel />, FR_CA);
    await screen.findByRole('heading', { level: 2, name: 'Modifier le document' });
    const processChanges = item(
      'Traiter les modifications enregistre, puis traite les modifications : l’évaluation et le résumé sont relancés.',
    );
    expect(boldIn(processChanges)).toEqual(['Traiter les modifications']);
    const reviewReason = item(
      'La raison de révision d’un élément de file dit pourquoi il est en révision, la plus grave d’abord. Choisissez une raison, ou Alerte suivante, pour aller à son champ.',
    );
    expect(boldIn(reviewReason)).toEqual(['Alerte suivante']);
  });

  it('words the Learn more links in French, with the same documentation URLs', async () => {
    renderWithIntl(<EditScreenToolsPanel />, FR_CA);
    expect(await screen.findByRole('link', { name: 'Files de révision' })).toBeInTheDocument();
    expect(links()).toEqual([
      ['Files de révision', `${DOCS_BASE_URL}/review-queues/`, ...NEW_TAB],
      ['Révision humaine', `${DOCS_BASE_URL}/human-review/`, ...NEW_TAB],
      ['Studio de test', `${DOCS_BASE_URL}/test-studio/`, ...NEW_TAB],
    ]);
  });
});

describe('the edit page’s help panel under en', () => {
  it('reads today’s English title and headings', () => {
    renderWithIntl(<EditScreenToolsPanel />);
    expect(screen.getByRole('heading', { level: 2, name: 'Edit Document' })).toBeInTheDocument();
    expect(subHeadings()).toEqual(['Buttons', 'Reports', 'More Actions', 'Document Editor', 'Learn more']);
  });

  it('keeps each bold control name inside its English sentence, Process Changes and Next alert included', () => {
    renderWithIntl(<EditScreenToolsPanel />);
    expect(boldIn(item('Refresh reloads the document; with unsaved edits it asks first.'))).toEqual(['Refresh']);
    const editorButtons = item(
      "Its buttons, right of its title: Show Evaluation (where the document has an evaluation baseline), Section, Show Fields and Edit Page Grouping, which moves pages between sections and changes a section's class.",
    );
    expect(boldIn(editorButtons)).toEqual(['Show Evaluation', 'Section', 'Show Fields', 'Edit Page Grouping']);
    const processChanges = item(
      'Process Changes saves, then processes the changes: evaluation and summarization re-run, and OCR, classification and extraction are not repeated. It finishes a queue item. On a Test Studio document it also writes the labels as reviewed ground truth; when no draft version of the set is open, it asks first and opens the next version, leaving the current one as it is.',
    );
    expect(boldIn(processChanges)).toEqual(['Process Changes']);
    const reviewReason = item(
      "A queue item's Review Reason lists why it is in review, worst first. Choose a reason, or Next alert, to move to its field.",
    );
    expect(boldIn(reviewReason)).toEqual(['Next alert']);
  });

  it('links the same documentation pages, with the HTML of a footer written out link by link', () => {
    const { container } = renderWithIntl(<EditScreenToolsPanel />);
    expect(links(container)).toEqual([
      ['Review queues', `${DOCS_BASE_URL}/review-queues/`, ...NEW_TAB],
      ['Human review', `${DOCS_BASE_URL}/human-review/`, ...NEW_TAB],
      ['Test Studio', `${DOCS_BASE_URL}/test-studio/`, ...NEW_TAB],
    ]);

    const written = renderWithIntl(<HelpPanel footer={writtenFooter} />).container;
    expect(footerHtml(container)).toBe(footerHtml(written));
  });
});
