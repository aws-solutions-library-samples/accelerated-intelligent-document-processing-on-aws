// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Derives a ground-truth-vs-predicted classification view from a document's
 * `evaluation/results.json`.
 *
 * The data has always been there — `doc_split_metrics.page_details` records
 * every page's expected and predicted class — but nothing rendered it: the
 * markdown report shows only section-level tables, so a page misclassification
 * was visible as a low score with no way to see which page went where.
 */

/** Per-page comparison as emitted by stickler's doc-split metrics. */
export interface PageComparison {
  /** 1-based page number for display (see `pageNumberFor`). */
  pageNumber: number;
  /** Raw 0-based index as stored in results.json. */
  pageIndex: number;
  groundTruthClass: string;
  predictedClass: string;
  correct: boolean;
}

/** Per-section comparison, including predicted sections with no GT match. */
export interface SectionComparison {
  /** Stable row key — section ids can repeat across the GT and predicted sides. */
  rowKey: string;
  /** Ground-truth section id, or null for an extra predicted section. */
  sectionId: string | null;
  /** Predicted section id, or null for a ground-truth section never matched. */
  predictedSectionId: string | null;
  groundTruthClass: string | null;
  groundTruthPages: number[];
  predictedClass: string | null;
  predictedPages: number[];
  /** Same pages AND same class. */
  matched: boolean;
  /** Matched and the page order agrees too. */
  orderMatched: boolean;
}

export interface ClassificationComparison {
  pages: PageComparison[];
  sections: SectionComparison[];
  pageLevelAccuracy: number | null;
  totalPages: number;
  correctPages: number;
  splitAccuracyWithoutOrder: number | null;
  splitAccuracyWithOrder: number | null;
  /** Errors stickler recorded while loading sections (surfaced, not hidden). */
  errors: string[];
}

interface RawPageDetail {
  page_index?: number;
  ground_truth_class?: string;
  predicted_class?: string;
  correct?: boolean;
}

interface RawSectionDetail {
  section_id?: string;
  ground_truth_class?: string;
  ground_truth_pages?: number[];
  predicted_class?: string;
  predicted_pages?: number[];
  matched?: boolean;
  matched_section_id?: string | null;
  order_matched?: boolean;
}

interface RawPredictedSection {
  section_id?: string;
  document_class?: string;
  page_indices?: number[];
}

interface RawDocSplitMetrics {
  page_level_accuracy?: number;
  split_accuracy_without_order?: number;
  split_accuracy_with_order?: number;
  total_pages?: number;
  correctly_classified_pages?: number;
  page_details?: RawPageDetail[];
  section_details_without_order?: RawSectionDetail[];
  section_details_with_order?: RawSectionDetail[];
  predicted_sections?: RawPredictedSection[];
  errors?: string[];
}

/**
 * Page indices in results.json are 0-based (a section's `page_indices` are
 * computed as `page_id - min(page_id)`), while page ids everywhere in the UI
 * are 1-based. Convert once, here, so no caller has to remember.
 */
export const pageNumberFor = (pageIndex: number): number => pageIndex + 1;

const asNumberOrNull = (value: unknown): number | null => (typeof value === 'number' && Number.isFinite(value) ? value : null);

const asPageNumbers = (indices: number[] | undefined): number[] =>
  (indices ?? []).filter((index) => typeof index === 'number').map(pageNumberFor);

/**
 * Read the classification comparison out of a parsed `results.json`.
 *
 * Returns `null` when the document has no doc-split metrics at all — which is
 * the normal case for a document evaluated without baseline sections, and
 * means "nothing to show" rather than an error.
 */
export const extractClassificationComparison = (
  evaluationResults: Record<string, unknown> | null | undefined,
): ClassificationComparison | null => {
  const metrics = evaluationResults?.doc_split_metrics as RawDocSplitMetrics | undefined;
  if (!metrics || typeof metrics !== 'object') return null;

  const pages: PageComparison[] = (metrics.page_details ?? [])
    .filter((detail) => typeof detail?.page_index === 'number')
    .map((detail) => ({
      pageIndex: detail.page_index as number,
      pageNumber: pageNumberFor(detail.page_index as number),
      groundTruthClass: detail.ground_truth_class ?? 'Missing',
      predictedClass: detail.predicted_class ?? 'Missing',
      // Trust stickler's own verdict rather than re-deriving it from the two
      // class strings: it owns the comparison and may not be a raw equality.
      correct: detail.correct === true,
    }))
    .sort((a, b) => a.pageIndex - b.pageIndex);

  // The with-order and without-order lists describe the SAME ground-truth
  // sections in the same order, differing only in their verdict, so they are
  // zipped by index (which is what the markdown report does too).
  const withOrder = metrics.section_details_with_order ?? [];
  const withoutOrder = metrics.section_details_without_order ?? [];
  const base = withOrder.length >= withoutOrder.length ? withOrder : withoutOrder;

  const matchedPredictedIds = new Set<string>();
  const sections: SectionComparison[] = base.map((detail, index) => {
    const counterpart = (base === withOrder ? withoutOrder : withOrder)[index];
    const matched = (detail.matched ?? counterpart?.matched) === true;
    if (detail.matched_section_id) matchedPredictedIds.add(detail.matched_section_id);

    return {
      rowKey: `gt:${detail.section_id ?? index}`,
      sectionId: detail.section_id ?? null,
      predictedSectionId: detail.matched_section_id ?? null,
      groundTruthClass: detail.ground_truth_class ?? null,
      groundTruthPages: asPageNumbers(detail.ground_truth_pages),
      // "No Match" is stickler's placeholder for "nothing matched"; render it
      // as absent rather than as a class literally called "No Match".
      predictedClass: detail.predicted_class && detail.predicted_class !== 'No Match' ? detail.predicted_class : null,
      predictedPages: asPageNumbers(detail.predicted_pages),
      matched,
      orderMatched: matched && (base === withOrder ? detail.order_matched === true : counterpart?.order_matched === true),
    };
  });

  // Predicted sections the run produced that no ground-truth section claimed —
  // an over-split, or a section given a class the packet does not contain.
  // Without these the table would silently omit half of a split error.
  (metrics.predicted_sections ?? []).forEach((predicted) => {
    if (!predicted.section_id || matchedPredictedIds.has(predicted.section_id)) return;
    sections.push({
      rowKey: `pred:${predicted.section_id}`,
      sectionId: null,
      predictedSectionId: predicted.section_id,
      groundTruthClass: null,
      groundTruthPages: [],
      predictedClass: predicted.document_class ?? null,
      predictedPages: asPageNumbers(predicted.page_indices),
      matched: false,
      orderMatched: false,
    });
  });

  if (pages.length === 0 && sections.length === 0) return null;

  return {
    pages,
    sections,
    pageLevelAccuracy: asNumberOrNull(metrics.page_level_accuracy),
    totalPages: metrics.total_pages ?? pages.length,
    correctPages: metrics.correctly_classified_pages ?? pages.filter((page) => page.correct).length,
    splitAccuracyWithoutOrder: asNumberOrNull(metrics.split_accuracy_without_order),
    splitAccuracyWithOrder: asNumberOrNull(metrics.split_accuracy_with_order),
    errors: (metrics.errors ?? []).filter((error): error is string => typeof error === 'string'),
  };
};

/** Count of pages whose predicted class differs from ground truth. */
export const countPageMismatches = (comparison: ClassificationComparison): number =>
  comparison.pages.filter((page) => !page.correct).length;

/** Count of ground-truth/predicted sections that did not match cleanly. */
export const countSectionMismatches = (comparison: ClassificationComparison): number =>
  comparison.sections.filter((section) => !section.matched).length;

/**
 * Derive the `results.json` URI from the evaluation report URI.
 *
 * They are written to the same prefix by `EvaluationService`, so deriving one
 * from the other follows whatever prefix that document actually used —
 * including a historical version snapshot — instead of rebuilding the key from
 * bucket + input key and hoping they agree.
 */
export const evaluationResultsUriFrom = (evaluationReportUri: string | undefined | null): string | null => {
  if (!evaluationReportUri) return null;
  if (!evaluationReportUri.endsWith('/report.md')) return null;
  return evaluationReportUri.replace(/\/report\.md$/, '/results.json');
};

/** Format a 0-1 ratio as a percentage, or an em dash when unavailable. */
export const formatAccuracy = (value: number | null): string => (value === null ? '—' : `${(value * 100).toFixed(1)}%`);

/** Render a page-number list compactly: [1,2,3,5] -> "1-3, 5". */
export const formatPageRanges = (pageNumbers: number[]): string => {
  if (pageNumbers.length === 0) return '—';
  const sorted = [...pageNumbers].sort((a, b) => a - b);
  const parts: string[] = [];
  let start = sorted[0];
  let previous = sorted[0];

  sorted.slice(1).forEach((current) => {
    if (current === previous + 1) {
      previous = current;
      return;
    }
    parts.push(start === previous ? `${start}` : `${start}-${previous}`);
    start = current;
    previous = current;
  });
  parts.push(start === previous ? `${start}` : `${start}-${previous}`);

  return parts.join(', ');
};
