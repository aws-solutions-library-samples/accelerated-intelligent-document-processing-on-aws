// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Reads a document's `evaluation/results.json` into what the report UI renders.
 *
 * The evaluation report used to be a markdown file rendered in a panel — a
 * separate artifact that happened to be displayed, so the headline number was
 * buried in prose and nothing could be sorted, filtered or linked. The same data
 * has always been available as JSON (`DocumentEvaluationResult.to_dict`); this
 * module reads it so the UI can be a real component.
 *
 * Pure and side-effect free: no fetching, no formatting decisions that belong to
 * the component. The markdown report is still generated and downloadable — this
 * does not replace the artifact, only how it is presented.
 */

export interface AttributeResult {
  name?: string;
  expected?: unknown;
  actual?: unknown;
  matched?: boolean;
  score?: number | null;
  reason?: string | null;
  evaluation_method?: string | null;
  evaluation_threshold?: number | null;
  confidence?: number | null;
  confidence_threshold?: number | null;
  weight?: number | null;
  field_comparison_details?: Record<string, unknown>[] | null;
}

export interface SectionResult {
  section_id?: string | number;
  document_class?: string | null;
  metrics?: Record<string, unknown> | null;
  attributes?: AttributeResult[];
}

export interface EvaluationResults {
  document_id?: string;
  overall_metrics?: Record<string, unknown> | null;
  execution_time?: number | null;
  section_results?: SectionResult[];
  doc_split_metrics?: Record<string, unknown> | null;
  excluded_sections?: unknown[];
}

/** The three figures the report leads with, plus what they were measured on. */
export interface EvaluationSummary {
  /** Weighted overall score if the run produced one, else the raw match rate. */
  extractionScore: number | null;
  /** True when extractionScore is the weighted score rather than the match rate. */
  extractionIsWeighted: boolean;
  matchedAttributes: number;
  totalAttributes: number;
  /** Page-level classification accuracy, when the document was split-evaluated. */
  classificationScore: number | null;
  precision: number | null;
  recall: number | null;
  f1Score: number | null;
  sectionCount: number;
  excludedSectionCount: number;
  /** True when no section had an extractable schema, so scores mean nothing. */
  excluded: boolean;
  exclusionReason: string | null;
}

const asNumber = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);

/**
 * Summarise a results payload.
 *
 * Every field is optional in the source, and older payloads legitimately lack
 * some, so this never throws — a missing figure becomes null and the component
 * omits its tile rather than rendering a confident zero. A zero here would be
 * indistinguishable from "scored 0%", which is the one wrong reading available.
 */
export const summarizeEvaluation = (results: EvaluationResults | null | undefined): EvaluationSummary => {
  const sections = Array.isArray(results?.section_results) ? results.section_results : [];
  const overall = (results?.overall_metrics ?? {}) as Record<string, unknown>;
  const split = (results?.doc_split_metrics ?? {}) as Record<string, unknown>;

  let totalAttributes = 0;
  let matchedAttributes = 0;
  for (const section of sections) {
    for (const attribute of section.attributes ?? []) {
      totalAttributes += 1;
      if (attribute.matched) matchedAttributes += 1;
    }
  }

  const weighted = asNumber(overall.weighted_overall_score);
  const matchRate = totalAttributes > 0 ? matchedAttributes / totalAttributes : null;

  const excludedSections = Array.isArray(results?.excluded_sections) ? results.excluded_sections : [];

  return {
    extractionScore: weighted ?? matchRate,
    extractionIsWeighted: weighted !== null,
    matchedAttributes,
    totalAttributes,
    classificationScore: asNumber(split.page_level_accuracy),
    precision: asNumber(overall.precision),
    recall: asNumber(overall.recall),
    f1Score: asNumber(overall.f1_score),
    sectionCount: sections.length,
    excludedSectionCount: excludedSections.length,
    excluded: overall.evaluation_excluded === true,
    exclusionReason: typeof overall.exclusion_reason === 'string' ? overall.exclusion_reason : null,
  };
};

/** Attributes that did not match, for the "problems only" view. */
export const mismatchedAttributes = (section: SectionResult): AttributeResult[] =>
  (section.attributes ?? []).filter((attribute) => !attribute.matched);

/**
 * Which comparison methods this document's evaluation actually used.
 *
 * The markdown report listed these; worth keeping because a surprising score is
 * often a comparison-method question ("why is 'Acme Inc' not matching 'Acme,
 * Inc.'?") rather than an extraction one.
 */
export const evaluationMethodsUsed = (results: EvaluationResults | null | undefined): string[] => {
  const methods = new Set<string>();
  for (const section of results?.section_results ?? []) {
    for (const attribute of section.attributes ?? []) {
      if (attribute.evaluation_method) methods.add(String(attribute.evaluation_method));
    }
  }
  return [...methods].sort();
};

/**
 * A traffic-light band for a score, matching the thresholds the markdown report
 * used so the two do not disagree about what "good" means during the changeover.
 */
export type ScoreBand = 'good' | 'fair' | 'poor' | 'bad' | 'unknown';

export const scoreBand = (score: number | null): ScoreBand => {
  if (score === null) return 'unknown';
  if (score >= 0.9) return 'good';
  if (score >= 0.7) return 'fair';
  if (score >= 0.5) return 'poor';
  return 'bad';
};

/** "94.2%" — scores are proportions everywhere in this payload. */
export const formatScore = (score: number | null): string => (score === null ? '—' : `${(score * 100).toFixed(1)}%`);
