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

// ---------------------------------------------------------------------------
// Parity with the markdown report.
//
// `DocumentEvaluationResult.to_markdown()` (idp_common/evaluation/models.py) is the
// reference for what a report contains. Everything below reads the same
// results.json it does and reshapes one of its sections for rendering; the
// component adds nothing the markdown does not have, and a parity test holds the
// two section lists together. Kept as pure functions so each shape is testable
// without mounting Cloudscape.
// ---------------------------------------------------------------------------

/** Metrics the markdown rates higher-is-better, on its 0.9/0.7/0.5 thresholds. */
const HIGHER_IS_BETTER = new Set([
  'precision',
  'recall',
  'f1_score',
  'accuracy',
  'weighted_overall_score',
  'page_level_accuracy',
  'split_accuracy_without_order',
  'split_accuracy_with_order',
]);

/** Error rates: rated lower-is-better on 0.1/0.3/0.5. */
const LOWER_IS_BETTER = new Set(['false_alarm_rate', 'false_discovery_rate']);

/**
 * Keys that live in a metrics dict but are not metrics: internal state and flags
 * the markdown skips or renders elsewhere. Anything underscore-prefixed is
 * internal by convention (`_stickler_counts`).
 */
const NON_METRIC_KEYS = new Set([
  'evaluation_failed',
  'failure_type',
  'skipped_field_count',
  'evaluation_excluded',
  'exclusion_reason',
  'skipped_section_count',
]);

export interface MetricRow {
  metric: string;
  /** Null when the run recorded no value (an excluded document's weighted score). */
  value: number | null;
  /** Null for metrics the markdown gives no rating. */
  band: ScoreBand | null;
}

/** The markdown's rating for one metric, or null where it prints none. */
export const rateMetric = (metric: string, value: number | null): ScoreBand | null => {
  if (value === null) return null;
  if (HIGHER_IS_BETTER.has(metric)) return scoreBand(value);
  if (LOWER_IS_BETTER.has(metric)) {
    if (value <= 0.1) return 'good';
    if (value <= 0.3) return 'fair';
    if (value <= 0.5) return 'poor';
    return 'bad';
  }
  return null;
};

/**
 * A metrics dict as table rows, in the dict's own order. Numbers only, as the
 * markdown does: booleans, strings and nested dicts are internal state, and a
 * new one must not break the table. An explicit null survives as "not scored".
 */
export const metricRows = (metrics: Record<string, unknown> | null | undefined): MetricRow[] => {
  const rows: MetricRow[] = [];
  for (const [metric, raw] of Object.entries(metrics ?? {})) {
    if (NON_METRIC_KEYS.has(metric) || metric.startsWith('_')) continue;
    if (raw === null) {
      rows.push({ metric, value: null, band: null });
      continue;
    }
    if (typeof raw !== 'number' || !Number.isFinite(raw)) continue;
    rows.push({ metric, value: raw, band: rateMetric(metric, raw) });
  }
  return rows;
};

export interface SplitCount {
  score: number | null;
  correct: number;
  total: number;
}

export interface SplitRow {
  /** Ground-truth section id, or null for a predicted section nothing expected. */
  sectionId: string | null;
  sectionMatched: boolean;
  orderMatched: boolean;
  expectedClass: string | null;
  /** 1-based page numbers, as the rest of the UI shows them. */
  expectedPages: number[];
  predictedClass: string | null;
  predictedPages: number[];
  matchedSectionId: string | null;
}

export interface SplitAnalysis {
  pageLevel: SplitCount;
  withoutOrder: SplitCount;
  withOrder: SplitCount;
  /** Graded packet score, when the run produced one (newer results only). */
  graded: {
    finalScore: number | null;
    clusteringScore: number | null;
    vMeasure: number | null;
    randIndex: number | null;
    orderingScore: number | null;
  } | null;
  rows: SplitRow[];
  errors: string[];
}

const asInt = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : 0);

const toPageNumbers = (indices: unknown): number[] =>
  Array.isArray(indices) ? indices.filter((i): i is number => typeof i === 'number').map((i) => i + 1) : [];

const asString = (value: unknown): string | null => (value === null || value === undefined ? null : String(value));

/**
 * The document-split half of the report, or null for a document evaluated
 * without section-level ground truth.
 *
 * Rows follow the markdown's "Section Split Analysis" table exactly: one per
 * ground-truth section, with the membership verdict taken from the
 * without-order details and the ordering verdict from the with-order details
 * at the same index, followed by every predicted section no ground-truth
 * section matched. Page indices are 0-based in the payload and 1-based
 * everywhere the UI shows a page, so they are converted here, once.
 */
export const splitAnalysis = (results: EvaluationResults | null | undefined): SplitAnalysis | null => {
  const split = results?.doc_split_metrics as Record<string, unknown> | null | undefined;
  if (!split) return null;

  const withOrder = Array.isArray(split.section_details_with_order) ? (split.section_details_with_order as Record<string, unknown>[]) : [];
  const withoutOrder = Array.isArray(split.section_details_without_order)
    ? (split.section_details_without_order as Record<string, unknown>[])
    : [];
  const predicted = Array.isArray(split.predicted_sections) ? (split.predicted_sections as Record<string, unknown>[]) : [];

  const matchedPredicted = new Set<string>();
  const rows: SplitRow[] = withOrder.map((section, index) => {
    const membership = withoutOrder[index];
    const matchedSectionId = asString(section.matched_section_id);
    if (matchedSectionId) matchedPredicted.add(matchedSectionId);
    return {
      sectionId: asString(section.section_id),
      sectionMatched: membership?.matched === true,
      orderMatched: section.order_matched === true,
      expectedClass: asString(section.ground_truth_class),
      expectedPages: toPageNumbers(section.ground_truth_pages),
      predictedClass: asString(section.predicted_class),
      predictedPages: toPageNumbers(section.predicted_pages),
      matchedSectionId,
    };
  });
  // Page order, not payload order: the payload lists sections in whatever order
  // the matcher visited them, which read as 2, 5, 6, 3, 4, 1 on a six-page packet.
  const firstPage = (pages: number[]) => (pages.length ? Math.min(...pages) : Number.MAX_SAFE_INTEGER);
  rows.sort((a, b) => firstPage(a.expectedPages) - firstPage(b.expectedPages));
  const unmatched: SplitRow[] = [];
  for (const section of predicted) {
    const id = asString(section.section_id);
    if (id && matchedPredicted.has(id)) continue;
    unmatched.push({
      sectionId: null,
      sectionMatched: false,
      orderMatched: false,
      expectedClass: null,
      expectedPages: [],
      predictedClass: asString(section.document_class),
      predictedPages: toPageNumbers(section.page_indices),
      matchedSectionId: id,
    });
  }
  unmatched.sort((a, b) => firstPage(a.predictedPages) - firstPage(b.predictedPages));
  rows.push(...unmatched);

  const graded =
    split.final_score === undefined
      ? null
      : {
          finalScore: asNumber(split.final_score),
          clusteringScore: asNumber(split.clustering_score),
          vMeasure: asNumber(split.v_measure),
          randIndex: asNumber(split.rand_index),
          orderingScore: asNumber(split.avg_ordering_score),
        };

  return {
    pageLevel: {
      score: asNumber(split.page_level_accuracy),
      correct: asInt(split.correctly_classified_pages),
      total: asInt(split.total_pages),
    },
    withoutOrder: {
      score: asNumber(split.split_accuracy_without_order),
      correct: asInt(split.correctly_split_without_order),
      total: asInt(split.total_splits),
    },
    withOrder: {
      score: asNumber(split.split_accuracy_with_order),
      correct: asInt(split.correctly_split_with_order),
      total: asInt(split.total_splits),
    },
    graded,
    rows,
    errors: Array.isArray(split.errors) ? split.errors.map(String) : [],
  };
};

export interface ExcludedSectionRow {
  sectionId: string;
  classification: string;
  reason: string;
  /** 1-based page numbers. */
  pages: number[];
}

/** Sections skipped by evaluation, with why — the markdown's "Excluded Sections" table. */
export const excludedSectionRows = (results: EvaluationResults | null | undefined): ExcludedSectionRow[] =>
  (Array.isArray(results?.excluded_sections) ? results.excluded_sections : [])
    .filter((entry): entry is Record<string, unknown> => typeof entry === 'object' && entry !== null)
    .map((entry) => ({
      sectionId: asString(entry.section_id) ?? '',
      classification: asString(entry.classification) ?? '',
      reason: asString(entry.exclusion_reason) ?? 'excluded',
      pages: toPageNumbers(entry.page_ids),
    }));

export interface SectionFailure {
  reason: string | null;
  failureType: string | null;
  /** The markdown's "How to fix" steps for this failure type; empty when the type is unknown. */
  steps: string[];
}

/**
 * Why a section was not scored, when it was not. Mirrors `_failure_remediation`
 * in models.py, including its refusal to guess: a result with no failure type
 * gets the reason and no steps, because advice for the wrong cause is worse than
 * none.
 */
export const sectionFailure = (section: SectionResult): SectionFailure | null => {
  const metrics = (section.metrics ?? {}) as Record<string, unknown>;
  if (metrics.evaluation_failed !== true) return null;
  const failureType = asString(metrics.failure_type);
  const cls = section.document_class ?? 'this class';
  const stepsByType: Record<string, string[]> = {
    missing_schema_configuration: [
      `Add a configuration for '${cls}' in the evaluation config`,
      'Ensure the document class name matches exactly (case-insensitive)',
      'Or provide baseline data when evaluating, so a configuration can be generated',
    ],
    empty_nested_object: [
      'Add at least one property to the nested object named above, or remove it from the schema',
      'Re-run the evaluation',
    ],
    extraction_parsing_failed: [
      "Open the section's extraction output — the model's response was not parseable JSON",
      'Check whether the response was truncated (raise max_tokens) or wrapped in commentary',
      'Re-run extraction for this document, then re-evaluate',
    ],
    baseline_data_validation_error: [
      'Compare the baseline (expected) values against the class schema — the types disagree',
      'Fix the baseline data, or widen the schema field type, and re-run the evaluation',
    ],
    schema_configuration_error: [
      `Review the evaluation schema for '${cls}' against the error above`,
      'Re-run the evaluation once the schema is valid',
    ],
  };
  return {
    reason: section.attributes?.[0]?.reason ?? null,
    failureType,
    steps: failureType ? (stepsByType[failureType] ?? []) : [],
  };
};

/** Fields excluded from scoring because they failed schema validation. */
export const skippedFieldCount = (section: SectionResult): number => {
  const raw = (section.metrics as Record<string, unknown> | null | undefined)?.skipped_field_count;
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : 0;
};

/**
 * One row of the attribute table, or one of its nested comparison rows.
 *
 * Both shapes share one type so a Cloudscape table can expand an aggregate
 * attribute into the field-by-field comparisons beneath it — the markdown's
 * "View N Nested Field Comparisons" block, which is where a Hungarian-matched
 * list says which item was paired with which.
 */
export interface ComparisonRow {
  key: string;
  name: string;
  expected: unknown;
  actual: unknown;
  matched: boolean;
  score: number | null;
  weight: number | null;
  method: string | null;
  reason: string | null;
  confidence: number | null;
  confidenceThreshold: number | null;
  /** Present on an aggregate attribute; the nested comparisons. */
  children?: ComparisonRow[];
  /** For a nested row whose actual path differs from its expected path (a list index moved). */
  actualPath?: string | null;
}

/** Stickler records `match` variously; only an unambiguous true counts. */
const isMatchTrue = (value: unknown): boolean => value === true || value === 'true' || value === 1;

/**
 * Attribute rows with their nested comparisons attached, on the markdown's own
 * threshold: more than one comparison detail means an aggregate (a nested
 * object or a list), a single detail is the attribute itself restated.
 */
export const attributeRows = (attributes: AttributeResult[] | undefined, keyPrefix = ''): ComparisonRow[] =>
  (attributes ?? []).map((attribute, index) => {
    const name = attribute.name ?? `field ${index + 1}`;
    const key = `${keyPrefix}${name}#${index}`;
    const details = Array.isArray(attribute.field_comparison_details) ? attribute.field_comparison_details : [];
    const children =
      details.length > 1
        ? details.map((detail, childIndex) => {
            const expectedKey = asString(detail.expected_key) ?? asString(detail.field_path) ?? `item ${childIndex + 1}`;
            const actualKey = asString(detail.actual_key);
            // Relative to the parent: every nested path repeats the attribute name,
            // which pushed the part that differs off the end of the cell.
            const relative = (path: string | null) => (path && path.startsWith(name) ? path.slice(name.length) || path : path);
            return {
              key: `${key}/${childIndex}`,
              name: relative(expectedKey) ?? expectedKey,
              expected: detail.expected_value,
              actual: detail.actual_value,
              matched: isMatchTrue(detail.match),
              score: asNumber(detail.score),
              weight: asNumber(detail.weight),
              method: asString(detail.evaluation_method),
              reason: asString(detail.reason),
              confidence: null,
              confidenceThreshold: null,
              actualPath: actualKey && actualKey !== expectedKey ? relative(actualKey) : null,
            };
          })
        : undefined;
    return {
      key,
      name,
      expected: attribute.expected,
      actual: attribute.actual,
      matched: attribute.matched === true,
      score: asNumber(attribute.score),
      weight: asNumber(attribute.weight),
      method: attribute.evaluation_method ?? null,
      reason: attribute.reason ?? null,
      confidence: asNumber(attribute.confidence),
      confidenceThreshold: asNumber(attribute.confidence_threshold),
      children,
    };
  });

export type ValueKind = 'empty' | 'scalar' | 'list' | 'object';

export interface ValueShape {
  kind: ValueKind;
  /** What the cell shows: the value itself for a scalar, a count for a structure. */
  summary: string;
}

/**
 * How to show a value in a table cell. A list or object used to be
 * `JSON.stringify`'d into one truncated string, which is the "flattens all the
 * lists" complaint; the cell now says what it is and the structure is shown
 * expanded on demand.
 */
export const describeValue = (value: unknown): ValueShape => {
  if (value === null || value === undefined || value === '') return { kind: 'empty', summary: '—' };
  if (Array.isArray(value)) return { kind: 'list', summary: `${value.length} item${value.length === 1 ? '' : 's'}` };
  if (typeof value === 'object') {
    const n = Object.keys(value as Record<string, unknown>).length;
    return { kind: 'object', summary: `${n} field${n === 1 ? '' : 's'}` };
  }
  return { kind: 'scalar', summary: String(value) };
};

/** Seconds → "12.3 s" / "2 min 5 s"; null when the run recorded none. */
export const formatDuration = (seconds: unknown): string | null => {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return null;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds - minutes * 60)} s`;
};
