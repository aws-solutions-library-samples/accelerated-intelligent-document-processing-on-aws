// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Reports › Evaluation Report under `fr-CA`: with a fixture catalog the headline scores' labels,
 * notes and ratings, the count of sections not evaluated, the misclassified-pages warning, a
 * section's How to fix steps, a list value's size and the help on how scores are computed read
 * French. The comparison method names, the metric keys, the classes and the evaluation's own
 * reasons stay as the evaluation writes them. The results are requested once: neither the
 * catalog's arrival nor a language switch requests them again, and a field row opened before the
 * switch stays open. Under `en` the same renders read today's English, with the singular for a
 * total of one.
 */
import React from 'react';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render. The report calls generateClient() at import time,
// so the client's graphql function is hoisted.
const state = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));

import EvaluationReport from '../EvaluationReport';

// The render test's packet: two expected sections, one of them split wrongly, a page classified
// `form` where ground truth says `W2`, a Payslip section with no evaluation configuration, and one
// section the evaluation skipped.
const PACKET = {
  document_id: 'packet.pdf',
  execution_time: 12.3,
  overall_metrics: {
    precision: 0.9,
    recall: 0.8,
    f1_score: 0.85,
    accuracy: 0.8,
    false_alarm_rate: 0.1,
    false_discovery_rate: 0.1,
    weighted_overall_score: 0.82,
  },
  doc_split_metrics: {
    page_level_accuracy: 0.5,
    split_accuracy_without_order: 0.5,
    split_accuracy_with_order: 0.0,
    total_pages: 2,
    total_splits: 2,
    correctly_classified_pages: 1,
    correctly_split_without_order: 1,
    correctly_split_with_order: 0,
    page_details: [
      { page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 1, ground_truth_class: 'W2', predicted_class: 'form', correct: false },
    ],
    section_details_without_order: [
      {
        section_id: '1',
        ground_truth_class: 'W2',
        ground_truth_pages: [0],
        matched: true,
        matched_section_id: '1',
        predicted_class: 'W2',
        predicted_pages: [0],
      },
      {
        section_id: '2',
        ground_truth_class: 'W2',
        ground_truth_pages: [1],
        matched: false,
        matched_section_id: null,
        predicted_class: 'No Match',
        predicted_pages: [],
      },
    ],
    section_details_with_order: [
      {
        section_id: '1',
        ground_truth_class: 'W2',
        ground_truth_pages: [0],
        matched: true,
        order_matched: false,
        matched_section_id: '1',
        predicted_class: 'W2',
        predicted_pages: [0],
      },
      {
        section_id: '2',
        ground_truth_class: 'W2',
        ground_truth_pages: [1],
        matched: false,
        order_matched: false,
        matched_section_id: null,
        predicted_class: 'No Match',
        predicted_pages: [],
      },
    ],
    predicted_sections: [
      { section_id: '1', document_class: 'W2', page_indices: [0] },
      { section_id: '2', document_class: 'form', page_indices: [1] },
    ],
    errors: ['section 2 had no prediction'],
    final_score: 0.75,
  },
  excluded_sections: [{ section_id: '3', classification: 'Cover', exclusion_reason: 'no extractable attributes', page_ids: ['3'] }],
  section_results: [
    {
      section_id: '1',
      document_class: 'W2',
      metrics: {
        precision: 0.9,
        recall: 0.8,
        f1_score: 0.85,
        weighted_overall_score: 0.82,
        _stickler_counts: { tp: 1 },
        skipped_field_count: 1,
      },
      attributes: [
        {
          name: 'Employer',
          expected: 'Acme',
          actual: 'Acme',
          matched: true,
          score: 1,
          evaluation_method: 'Exact',
          confidence: 0.97,
          confidence_threshold: 0.8,
          weight: 2,
        },
        {
          name: 'Boxes',
          expected: [
            { code: 'A', amount: 1 },
            { code: 'B', amount: 2 },
          ],
          actual: [
            { code: 'B', amount: 2 },
            { code: 'A', amount: 9 },
          ],
          matched: false,
          score: 0.5,
          evaluation_method: 'Hungarian',
          reason: 'one of two items matched',
          field_comparison_details: [
            {
              expected_key: 'Boxes[0].amount',
              actual_key: 'Boxes[1].amount',
              expected_value: 1,
              actual_value: 9,
              match: false,
              score: 0,
              evaluation_method: 'NumericExact',
              reason: 'differs',
            },
            {
              expected_key: 'Boxes[1].amount',
              actual_key: 'Boxes[0].amount',
              expected_value: 2,
              actual_value: 2,
              match: true,
              score: 1,
              evaluation_method: 'NumericExact',
              reason: 'exact',
            },
          ],
        },
      ],
    },
    {
      section_id: '2',
      document_class: 'Payslip',
      metrics: { precision: 0, recall: 0, evaluation_failed: true, failure_type: 'missing_schema_configuration' },
      attributes: [{ name: 'n/a', reason: 'No evaluation configuration found for Payslip' }],
    },
  ],
};

/** The packet with three sections skipped, the last one with no recorded reason. */
const threeSkipped = () => {
  const results = JSON.parse(JSON.stringify(PACKET));
  results.excluded_sections = [
    { section_id: '3', classification: 'Cover', exclusion_reason: 'no extractable attributes', page_ids: ['3'] },
    { section_id: '4', classification: 'Cover', exclusion_reason: 'no extractable attributes', page_ids: ['4'] },
    { section_id: '5', classification: 'Notes', page_ids: ['5'] },
  ];
  return results;
};

/** A one-page document whose one field matched: every total is one. */
const ONE_OF_ONE = {
  document_id: 'invoice.pdf',
  execution_time: 2,
  overall_metrics: { precision: 1, recall: 1, f1_score: 1 },
  doc_split_metrics: {
    page_level_accuracy: 1,
    split_accuracy_without_order: 1,
    split_accuracy_with_order: 1,
    total_pages: 1,
    total_splits: 1,
    correctly_classified_pages: 1,
    correctly_split_without_order: 1,
    correctly_split_with_order: 1,
    page_details: [{ page_index: 0, ground_truth_class: 'Invoice', predicted_class: 'Invoice', correct: true }],
    section_details_without_order: [
      {
        section_id: '1',
        ground_truth_class: 'Invoice',
        ground_truth_pages: [0],
        matched: true,
        matched_section_id: '1',
        predicted_class: 'Invoice',
        predicted_pages: [0],
      },
    ],
    section_details_with_order: [
      {
        section_id: '1',
        ground_truth_class: 'Invoice',
        ground_truth_pages: [0],
        matched: true,
        order_matched: true,
        matched_section_id: '1',
        predicted_class: 'Invoice',
        predicted_pages: [0],
      },
    ],
    predicted_sections: [{ section_id: '1', document_class: 'Invoice', page_indices: [0] }],
    errors: [],
  },
  section_results: [
    {
      section_id: '1',
      document_class: 'Invoice',
      metrics: { precision: 1, recall: 1, f1_score: 1 },
      attributes: [{ name: 'Total', expected: '10.00', actual: '10.00', matched: true, score: 1, evaluation_method: 'Exact' }],
    },
  ],
};

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.evaluationReport.title': 'Évaluation',
  'documents.evaluationReport.tile.extraction': 'Exactitude de l’extraction',
  'documents.evaluationReport.tile.extractionHintWeighted':
    'Pondérée selon l’importance des champs · concordance : {matched, number} sur {total, plural, one {# champ} other {# champs}}',
  'documents.evaluationReport.tile.extractionHint': 'Concordance : {matched, number} sur {total, plural, one {# champ} other {# champs}}',
  'documents.evaluationReport.tile.classification': 'Exactitude de la classification',
  'documents.evaluationReport.tile.classificationHint': '{correct, number} sur {total, plural, one {# page} other {# pages}}',
  'documents.evaluationReport.tile.split': 'Exactitude de la séparation',
  'documents.evaluationReport.tile.splitHint':
    '{correct, number} sur {total, plural, one {# section} other {# sections}} · dans l’ordre {score}',
  'documents.evaluationReport.tile.f1': 'Score F1',
  'documents.evaluationReport.tile.f1Hint': 'Précision {precision} · Rappel {recall}',
  'documents.evaluationReport.band.good': 'Excellente',
  'documents.evaluationReport.band.fair': 'Bonne',
  'documents.evaluationReport.band.poor': 'Passable',
  'documents.evaluationReport.band.bad': 'Faible',
  'documents.evaluationReport.excluded.header': '{count, plural, one {# section non évaluée} other {# sections non évaluées}}',
  'documents.evaluationReport.excludedReason': 'exclue',
  'documents.evaluationReport.misclassified.header': 'Certaines pages n’ont pas la classe attendue par la vérité terrain',
  'documents.evaluationReport.misclassified.pages':
    '{count, plural, one {Page} other {Pages}} <b>{pages}</b> : classe <b>{predicted}</b> attribuée, alors que la vérité terrain indique <b>{expected}</b>.',
  'documents.evaluationReport.section.notEvaluated': 'Cette section n’a pas été évaluée',
  'documents.evaluationReport.section.howToFix': 'Comment corriger',
  'documents.evaluationReport.fix.addConfiguration': "Ajoutez une configuration pour ''{className}'' dans la configuration d’évaluation",
  'documents.evaluationReport.fields.methodColumn': 'Méthode',
  'documents.evaluationReport.fields.showNested': 'Afficher les comparaisons imbriquées de {name}',
  'documents.evaluationReport.fields.hideNested': 'Masquer les comparaisons imbriquées de {name}',
  'documents.evaluationReport.listSize': '{count, plural, one {# élément} other {# éléments}}',
  'documents.evaluationReport.metrics.metricColumn': 'Métrique',
  'documents.evaluationReport.methods.body':
    'Un score surprenant relève souvent de la méthode de comparaison plutôt que de l’extraction. Ce document a utilisé : {methods}.',
  'documents.evaluationReport.help.exact': '<b>{method}</b> — correspondance caractère par caractère. Identifiants, codes.',
  'documents.evaluationReport.help.fuzzy':
    '<b>{method}</b> / <b>{other}</b> — similarité de chaînes par rapport à un seuil. Noms, adresses, petites variations.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const contents = (results: unknown) => ({ data: { getFileContents: { content: JSON.stringify(results), isBinary: false } } });

const report = () => <EvaluationReport reportUri="s3://b/packet.pdf/evaluation/report.md" documentId="packet.pdf" />;

/** The element of the given tag whose whole text is `text`. */
const byWholeText = (tag: string, text: string) =>
  screen.getByText((_content, element) => element?.tagName === tag && element.textContent === text);

const boldParts = (element: Element) => [...element.querySelectorAll('b')].map((b) => b.textContent);

beforeEach(() => {
  state.graphql.mockReset();
  state.graphql.mockResolvedValue(contents(PACKET));
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('Reports › Evaluation Report under fr-CA', () => {
  it('labels the headline scores, their notes and their ratings in French', async () => {
    renderWithIntl(report(), FR_CA);
    expect(await screen.findByText('Exactitude de l’extraction')).toBeInTheDocument();
    expect(screen.getByText('Exactitude de la classification')).toBeInTheDocument();
    expect(screen.getByText('Exactitude de la séparation')).toBeInTheDocument();
    expect(screen.getByText('Score F1')).toBeInTheDocument();
    expect(screen.getByText('Pondérée selon l’importance des champs · concordance : 1 sur 3 champs')).toBeInTheDocument();
    expect(screen.getByText('1 sur 2 pages')).toBeInTheDocument();
    expect(screen.getByText(/^1 sur 2 sections · dans l’ordre 0,0\s%$/)).toBeInTheDocument();
    expect(screen.getByText(/^Précision 90,0\s% · Rappel 80,0\s%$/)).toBeInTheDocument();
    // 82% rates Good, 50% Fair: the tiles and the metrics tables read the French ratings.
    expect(screen.getAllByText('Bonne').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Passable').length).toBeGreaterThan(0);
    expect(screen.queryByText('Good')).toBeNull();
    expect(screen.queryByText('Fair')).toBeNull();
  });

  it('reads the singular in French for a total of one', async () => {
    state.graphql.mockResolvedValue(contents(ONE_OF_ONE));
    renderWithIntl(report(), FR_CA);
    expect(await screen.findByText('Concordance : 1 sur 1 champ')).toBeInTheDocument();
    expect(screen.getByText('1 sur 1 page')).toBeInTheDocument();
    expect(screen.getByText(/^1 sur 1 section · dans l’ordre 100,0\s%$/)).toBeInTheDocument();
  });

  it('counts the sections not evaluated in French, for one and for three', async () => {
    const one = renderWithIntl(report(), FR_CA);
    expect(await screen.findByText('1 section non évaluée')).toBeInTheDocument();
    one.unmount();

    state.graphql.mockResolvedValue(contents(threeSkipped()));
    renderWithIntl(report(), FR_CA);
    expect(await screen.findByText('3 sections non évaluées')).toBeInTheDocument();
    // A section skipped without a recorded reason; the recorded reasons stay as written.
    expect(screen.getByText('exclue')).toBeInTheDocument();
    expect(screen.getAllByText('no extractable attributes')).toHaveLength(2);
  });

  it('words the misclassified pages in French, with the pages and the classes in bold', async () => {
    renderWithIntl(report(), FR_CA);
    expect(await screen.findByText('Certaines pages n’ont pas la classe attendue par la vérité terrain')).toBeInTheDocument();
    const line = byWholeText('P', 'Page 2 : classe form attribuée, alors que la vérité terrain indique W2.');
    expect(boldParts(line)).toEqual(['2', 'form', 'W2']);
  });

  it("gives a section's How to fix steps in French, with the class in its quotes, and leaves the evaluation's reason as written", async () => {
    renderWithIntl(report(), FR_CA);
    expect(await screen.findByText('Cette section n’a pas été évaluée')).toBeInTheDocument();
    expect(screen.getByText('Comment corriger')).toBeInTheDocument();
    expect(screen.getByText("Ajoutez une configuration pour 'Payslip' dans la configuration d’évaluation")).toBeInTheDocument();
    expect(screen.getByText('No evaluation configuration found for Payslip')).toBeInTheDocument();
  });

  it('leaves the comparison method names and the metric keys as the evaluation writes them', async () => {
    renderWithIntl(report(), FR_CA);
    expect(await screen.findAllByText('Méthode')).not.toHaveLength(0);
    expect(screen.getAllByText('Exact').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Hungarian').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Métrique').length).toBeGreaterThan(0);
    for (const key of ['precision', 'f1_score', 'false_alarm_rate', 'weighted_overall_score']) {
      expect(screen.getAllByText(key).length).toBeGreaterThan(0);
    }
    expect(boldParts(byWholeText('LI', 'Exact — correspondance caractère par caractère. Identifiants, codes.'))).toEqual(['Exact']);
    const fuzzy = byWholeText(
      'LI',
      'Fuzzy / Levenshtein — similarité de chaînes par rapport à un seuil. Noms, adresses, petites variations.',
    );
    expect(boldParts(fuzzy)).toEqual(['Fuzzy', 'Levenshtein']);
    expect(
      screen.getByText(
        'Un score surprenant relève souvent de la méthode de comparaison plutôt que de l’extraction. Ce document a utilisé : Exact, Hungarian.',
      ),
    ).toBeInTheDocument();
  });

  it("gives a list value's size in French", async () => {
    renderWithIntl(report(), FR_CA);
    expect(await screen.findAllByText('2 éléments')).toHaveLength(2);
  });

  // The catalog arrives after the first render, as a new `intl`; neither that nor a language
  // switch may request the results again.
  it('requests the results once, and a language switch rewords the report without a new request and keeps an opened row open', async () => {
    const view = renderWithIntl(report(), FR_CA);
    expect(await screen.findByText('Évaluation')).toBeInTheDocument();
    expect(state.graphql).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button', { name: 'Afficher les comparaisons imbriquées de Boxes' }));
    expect(await screen.findByText('[0].amount')).toBeInTheDocument();

    view.setLocale('en');
    await waitFor(() => expect(screen.getByText('Extraction accuracy')).toBeInTheDocument());
    expect(screen.getByText('Evaluation')).toBeInTheDocument();
    expect(state.graphql).toHaveBeenCalledTimes(1);
    expect(screen.getByText('[0].amount')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Hide nested comparisons for Boxes' })).toBeInTheDocument();
  });
});

describe('Reports › Evaluation Report under en', () => {
  it("labels the headline scores, their notes and their ratings with today's English", async () => {
    renderWithIntl(report());
    expect(await screen.findByText('Extraction accuracy')).toBeInTheDocument();
    expect(screen.getByText('Classification accuracy')).toBeInTheDocument();
    expect(screen.getByText('Split accuracy')).toBeInTheDocument();
    expect(screen.getByText('F1 score')).toBeInTheDocument();
    expect(screen.getByText('Weighted by field importance · 1 of 3 fields matched')).toBeInTheDocument();
    expect(screen.getByText('1 of 2 pages')).toBeInTheDocument();
    expect(screen.getByText('1 of 2 sections · in order 0.0%')).toBeInTheDocument();
    expect(screen.getByText('Precision 90.0% · Recall 80.0%')).toBeInTheDocument();
    expect(screen.getAllByText('Good').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Fair').length).toBeGreaterThan(0);
  });

  it('reads the singular for a total of one', async () => {
    state.graphql.mockResolvedValue(contents(ONE_OF_ONE));
    renderWithIntl(report());
    expect(await screen.findByText('1 of 1 field matched')).toBeInTheDocument();
    expect(screen.getByText('1 of 1 page')).toBeInTheDocument();
    expect(screen.getByText('1 of 1 section · in order 100.0%')).toBeInTheDocument();
  });

  it("counts the sections not evaluated with today's English, for one and for three", async () => {
    const one = renderWithIntl(report());
    expect(await screen.findByText('1 section not evaluated')).toBeInTheDocument();
    one.unmount();

    state.graphql.mockResolvedValue(contents(threeSkipped()));
    renderWithIntl(report());
    expect(await screen.findByText('3 sections not evaluated')).toBeInTheDocument();
    expect(screen.getByText('excluded')).toBeInTheDocument();
    expect(screen.getAllByText('no extractable attributes')).toHaveLength(2);
  });

  it("words the misclassified pages with today's English, with the pages and the classes in bold", async () => {
    renderWithIntl(report());
    expect(await screen.findByText('Some pages are not the class ground truth expects')).toBeInTheDocument();
    const line = byWholeText('P', 'Page 2 classified as form, but ground truth says W2.');
    expect(boldParts(line)).toEqual(['2', 'form', 'W2']);
  });

  it("gives a section's How to fix steps with today's English, with the class in its quotes", async () => {
    renderWithIntl(report());
    expect(await screen.findByText('This section was not evaluated')).toBeInTheDocument();
    expect(screen.getByText('How to fix')).toBeInTheDocument();
    expect(screen.getByText("Add a configuration for 'Payslip' in the evaluation config")).toBeInTheDocument();
    expect(screen.getByText('No evaluation configuration found for Payslip')).toBeInTheDocument();
  });

  it('shows the comparison method names and the metric keys as the evaluation writes them', async () => {
    renderWithIntl(report());
    expect(await screen.findAllByText('Method')).not.toHaveLength(0);
    expect(screen.getAllByText('Exact').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Hungarian').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Metric').length).toBeGreaterThan(0);
    for (const key of ['precision', 'f1_score', 'false_alarm_rate', 'weighted_overall_score']) {
      expect(screen.getAllByText(key).length).toBeGreaterThan(0);
    }
    expect(boldParts(byWholeText('LI', 'Exact — character-for-character match. IDs, codes.'))).toEqual(['Exact']);
    const fuzzy = byWholeText('LI', 'Fuzzy / Levenshtein — string similarity against a threshold. Names, addresses, minor variations.');
    expect(boldParts(fuzzy)).toEqual(['Fuzzy', 'Levenshtein']);
    expect(
      screen.getByText(
        'A surprising score is often a comparison-method question rather than an extraction one. This document used: Exact, Hungarian.',
      ),
    ).toBeInTheDocument();
  });

  it("gives a list value's size with today's English", async () => {
    renderWithIntl(report());
    expect(await screen.findAllByText('2 items')).toHaveLength(2);
  });
});
