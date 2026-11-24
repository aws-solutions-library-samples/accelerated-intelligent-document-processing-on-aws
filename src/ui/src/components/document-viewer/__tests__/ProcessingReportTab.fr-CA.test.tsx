// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The edit page's Processing Report under `fr-CA`: with a fixture catalog the extraction outcome,
 * its counts in the singular and the plural, the items to review, the confidence auto-recovery
 * note, the confidence assessment sentence, the escalation sentence and the percentages read
 * French, and a processing issue's code shows its French label beside the backend's English
 * message. The stage boxes, the issue messages, the model ids and the escalation decision stay as
 * the backend writes them. A language switch rewords the report. Under `en` the same renders read
 * today's English, with singular and plural forms instead of "(s)" and today's whole percentages.
 */
import React from 'react';
import { screen, waitFor } from '@testing-library/react';
import { I18n } from 'aws-amplify/utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));

import ProcessingReportContent from '../../edit-screen/ProcessingReportContent';

const OPUS = 'us.anthropic.claude-opus-4-1';

// A section with three items to review (schema validation, field population, completeness), an
// escalation that resolved it, a confidence recovery by retry and by escalation, three sequential
// confidence batches, one list row and one processing issue.
const SECTION = {
  inference_result: { holdings: [{ symbol: 'AMZN' }] },
  processing_report: 'Extraction: agentic\nValidation: 1 error',
  metadata: {
    extraction_method: 'agentic',
    extraction_time_seconds: 12.34,
    parsing_succeeded: true,
    extraction_model: 'us.anthropic.claude-sonnet-4-6',
    validation: {
      valid: false,
      error_count: 1,
      failed_fields: ['Total'],
      errors: [{ path: 'Total', validator: 'type', message: 'is not a number' }],
      check_formats: true,
      fail_action: 'escalate',
      escalated: true,
      initial_error_count: 3,
      escalation_model: OPUS,
      escalation_scope: 'field-subset',
      escalation_fields: ['Total'],
      resolved_by_escalation: true,
      escalation_decision: 'kept the escalated result',
    },
    population_check: { fields_defined: 50, fields_populated: 7, population_ratio: 0.14, threshold: 0.4, below_threshold: true },
    completeness_check: {
      schema_constraints_met: false,
      violations: [{ field: 'holdings', message: 'has 1 item, needs 2', possible_cause: 'truncation' }],
      summary: 'holdings has fewer items than minItems',
    },
    assessment_batch_split_stats: { batch_count: 3, concurrent_batches: 1 },
    processing_flow: {
      stages: [
        { key: 'ocr', label: 'OCR', status: 'ok' },
        { key: 'extract', label: 'Extract', status: 'ok', detail: 'single LLM pass' },
        { key: 'confidence', label: 'Confidence', status: 'warning' },
      ],
      recovery: {
        truncated_calls: 1,
        splits: 2,
        rows_recovered_by_retry: 6,
        rows_recovered_by_escalation: 4,
        escalation_model: OPUS,
        unrecoverable_rows: 0,
      },
    },
    processing_issues: [
      { stage: 'extraction', severity: 'warning', code: 'extraction_sparse', message: 'Only 7 of 50 schema fields were populated.' },
    ],
  },
};

// A section with one item to review: 29 of 200 fields populated, a share of 0.145 that today's
// whole percentage reads as 14%; three list rows and one confidence batch.
const ONE_ITEM = {
  inference_result: { holdings: [{ symbol: 'A' }, { symbol: 'B' }, { symbol: 'C' }] },
  metadata: {
    extraction_method: 'agentic',
    population_check: { fields_defined: 200, fields_populated: 29, population_ratio: 0.145, threshold: 0.4, below_threshold: true },
    assessment_batch_split_stats: { batch_count: 1 },
  },
};

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.processingReport.noSection': 'Ouvrez une section pour voir son rapport de traitement.',
  'documents.processingReport.notAvailable': 'Rapport de traitement non disponible',
  'documents.processingReport.outcome.title': 'Résultat de l’extraction',
  'documents.processingReport.outcome.agenticTimed': 'Extraction agentique · {seconds} s',
  'documents.processingReport.outcome.completedToReview': 'Terminé avec {count, plural, one {# élément} other {# éléments}} à vérifier',
  'documents.processingReport.outcome.dataExtracted': 'Données extraites',
  'documents.processingReport.outcome.rows': '{rows, plural, one {# ligne} other {# lignes}}',
  'documents.processingReport.review.header': '{count, plural, one {# élément} other {# éléments}} à vérifier',
  'documents.processingReport.review.item': '<strong>{label} :</strong> {detail}',
  'documents.processingReport.review.possibleMissing': 'Données possiblement manquantes',
  'documents.processingReport.review.possibleMissingDetail':
    'Seulement {populated} des {defined} champs du schéma ont été remplis ({ratio}, sous le seuil de {threshold}) — vérifiez s’il y a une perte silencieuse à l’extraction.',
  'documents.processingReport.issues.header': '{count, plural, one {# problème de traitement} other {# problèmes de traitement}}',
  'documents.processingIssue.code.extractionSparse': 'Extraction clairsemée',
  'documents.processingReport.path.recoveryLabel': '⚠ Récupération automatique de la confiance :',
  'documents.processingReport.path.recoveryTruncated':
    '{calls, plural, one {# appel de confiance tronqué} other {# appels de confiance tronqués}} à la limite de sortie du modèle (lots divisés {splits} fois).',
  'documents.processingReport.path.recoveredByBoth':
    '<strong>{rows}</strong> {rows, plural, one {ligne récupérée} other {lignes récupérées}} — {retried} par nouvel essai avec le même modèle, {escalated} par escalade vers {model}.',
  'documents.processingReport.path.recoveryAllScored': 'Toutes les lignes ont une note',
  'documents.processingReport.path.batchesSequential':
    'L’évaluation de la confiance s’est faite en <strong>{batches}</strong> {batches, plural, one {lot} other {lots}}, l’un après l’autre{batchSize}{escalation}.',
  'documents.processingReport.details.escalated': 'Escaladé vers <strong>{model}</strong> ({scope}) — {outcome}{errors}.',
  'documents.processingReport.details.scopeFields': 'champs : {fields}',
  'documents.processingReport.details.outcomeResolved': 'résolu',
  'documents.processingReport.details.errorCounts': ' ; erreurs {before} → {after}',
  'documents.processingReport.details.populationSummary': '{populated}/{defined} champs remplis ({ratio}) ·',
  'documents.processingReport.details.populationBelow': 'sous le seuil de {threshold}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** The whole text of the element that holds the bold `text`: a sentence with a bold part. */
const sentenceAround = (text: string): string | null | undefined =>
  screen.getByText(text, { selector: 'strong' }).parentElement?.textContent;

/** The whole text of a processing issue's row, found by its label. */
const issueRow = (label: string): string | null | undefined => screen.getByText(label).closest('div')?.textContent;

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe("the edit page's Processing Report under fr-CA", () => {
  it('words the outcome in French, with its counts in the singular and the plural, and rewords it on a switch to English', async () => {
    const view = renderWithIntl(<ProcessingReportContent sectionData={SECTION} />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Résultat de l’extraction')).toBeInTheDocument();
    expect(screen.getByText(/^Extraction agentique · 12,3\ss$/)).toBeInTheDocument();
    expect(screen.getByText('Terminé avec 3 éléments à vérifier')).toBeInTheDocument();
    expect(screen.getByText('3 éléments à vérifier')).toBeInTheDocument();
    expect(screen.getByText('Données extraites')).toBeInTheDocument();
    expect(screen.getByText('1 ligne')).toBeInTheDocument();
    view.setLocale('en');
    await waitFor(() => expect(screen.getByText('Completed with 3 items to review')).toBeInTheDocument());
    expect(screen.getByText('Extraction Outcome')).toBeInTheDocument();
    expect(screen.getByText('1 row')).toBeInTheDocument();
  });

  it('counts one item to review in the singular', async () => {
    renderWithIntl(<ProcessingReportContent sectionData={ONE_ITEM} />, FR_CA);
    expect(await screen.findByText('Terminé avec 1 élément à vérifier')).toBeInTheDocument();
    expect(screen.getByText('1 élément à vérifier')).toBeInTheDocument();
    expect(screen.getByText('3 lignes')).toBeInTheDocument();
  });

  it("shows a processing issue's code by its French label beside the backend's English message", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />, FR_CA);
    expect(await screen.findByText('1 problème de traitement')).toBeInTheDocument();
    expect(issueRow('Extraction clairsemée')).toBe('Extraction clairsemée Only 7 of 50 schema fields were populated.');
    expect(screen.queryByText('extraction_sparse')).toBeNull();
  });

  it('words the confidence auto-recovery note in French, with a retry and an escalation, around the model id', async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />, FR_CA);
    const label = await screen.findByText('⚠ Récupération automatique de la confiance :');
    expect(label.closest('p')?.textContent).toBe(
      '⚠ Récupération automatique de la confiance : 1 appel de confiance tronqué à la limite de sortie du modèle (lots divisés 2 fois). ' +
        `10 lignes récupérées — 6 par nouvel essai avec le même modèle, 4 par escalade vers ${OPUS}. Toutes les lignes ont une note`,
    );
    expect(sentenceAround('3')).toBe('L’évaluation de la confiance s’est faite en 3 lots, l’un après l’autre.');
  });

  it("words the escalation sentence in French, and keeps the backend's decision as it is written", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />, FR_CA);
    expect(await screen.findByText('Résultat de l’extraction')).toBeInTheDocument();
    expect(sentenceAround(OPUS)).toBe(`Escaladé vers ${OPUS} (champs : Total) — résolu ; erreurs 3 → 1. kept the escalated result.`);
  });

  it('formats a share as a French percentage', async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />, FR_CA);
    expect(await screen.findByText(/^7\/50 champs remplis \(14\s%\) ·$/)).toBeInTheDocument();
    expect(screen.getByText(/^sous le seuil de 40\s%$/)).toBeInTheDocument();
    expect(sentenceAround('Données possiblement manquantes :')).toMatch(
      /^Données possiblement manquantes : Seulement 7 des 50 champs du schéma ont été remplis \(14\s%, sous le seuil de 40\s%\) — /,
    );
  });

  it('says in French that no section is open, or that the section has no report', async () => {
    renderWithIntl(<ProcessingReportContent sectionData={null} />, FR_CA);
    expect(await screen.findByText('Ouvrez une section pour voir son rapport de traitement.')).toBeInTheDocument();
    renderWithIntl(<ProcessingReportContent sectionData={{}} />, FR_CA);
    expect(await screen.findByText('Rapport de traitement non disponible')).toBeInTheDocument();
  });
});

describe("the edit page's Processing Report under en", () => {
  it("words the outcome in today's English, with its counts in the singular and the plural", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />);
    expect(await screen.findByText('Extraction Outcome')).toBeInTheDocument();
    expect(screen.getByText('Agentic extraction · 12.3s')).toBeInTheDocument();
    expect(screen.getByText('Completed with 3 items to review')).toBeInTheDocument();
    expect(screen.getByText('3 items to review')).toBeInTheDocument();
    expect(screen.getByText('1 row')).toBeInTheDocument();
  });

  it('counts one item to review in the singular', async () => {
    renderWithIntl(<ProcessingReportContent sectionData={ONE_ITEM} />);
    expect(await screen.findByText('Completed with 1 item to review')).toBeInTheDocument();
    expect(screen.getByText('1 item to review')).toBeInTheDocument();
    expect(screen.getByText('3 rows')).toBeInTheDocument();
    expect(sentenceAround('1')).toBe('Confidence assessment ran in 1 batch — sequential.');
  });

  it("shows a processing issue's code as stored beside the backend's message", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />);
    expect(await screen.findByText('1 processing issue')).toBeInTheDocument();
    expect(issueRow('extraction_sparse')).toBe('extraction_sparse Only 7 of 50 schema fields were populated.');
  });

  it("words the confidence auto-recovery note and the confidence assessment sentence in today's English", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />);
    const label = await screen.findByText('⚠ Confidence auto-recovery:');
    expect(label.closest('p')?.textContent).toBe(
      "⚠ Confidence auto-recovery: 1 confidence call truncated at the model's output limit (batches split 2×). " +
        `Recovered 10 rows — 6 by same-model retry, 4 by escalation to ${OPUS}. All rows scored`,
    );
    expect(sentenceAround('3')).toBe('Confidence assessment ran in 3 batches — sequential.');
  });

  it("words the escalation sentence in today's English", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />);
    expect(await screen.findByText('Extraction Outcome')).toBeInTheDocument();
    expect(sentenceAround(OPUS)).toBe(`Escalated to ${OPUS} (fields: Total) — resolved; errors 3 → 1. kept the escalated result.`);
  });

  it("reads a share as today's whole percentage", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={SECTION} />);
    expect(await screen.findByText('7/50 fields populated (14%) ·')).toBeInTheDocument();
    expect(screen.getByText('below 40% threshold')).toBeInTheDocument();
    expect(sentenceAround('Possible missing data:')).toBe(
      'Possible missing data: Only 7/50 schema fields were populated (14%, below the 40% threshold) — review for silent extraction loss.',
    );
  });

  // 0.145 × 100 is 14.499… in floating point, so today's Math.round reads 14%; a percent format of
  // the raw ratio rounds its decimal form, 14.5, up to 15%.
  it("reads a share of 0.145 as 14%, as today's rounding does", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={ONE_ITEM} />);
    expect(await screen.findByText('29/200 fields populated (14%) ·')).toBeInTheDocument();
    expect(sentenceAround('Possible missing data:')).toBe(
      'Possible missing data: Only 29/200 schema fields were populated (14%, below the 40% threshold) — review for silent extraction loss.',
    );
  });

  it("says in today's English that no section is open, or that the section has no report", async () => {
    renderWithIntl(<ProcessingReportContent sectionData={null} />);
    expect(await screen.findByText('Open a section to see its processing report.')).toBeInTheDocument();
    renderWithIntl(<ProcessingReportContent sectionData={{}} />);
    expect(await screen.findByText('Processing report not available')).toBeInTheDocument();
  });
});
