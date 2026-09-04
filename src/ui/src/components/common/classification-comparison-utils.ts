// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Ground-truth-vs-predicted classification lookups, derived from a document's
 * `evaluation/results.json`.
 *
 * The data has always been there — `doc_split_metrics.page_details` records
 * every page's expected and predicted class — but nothing surfaced it: the
 * markdown report renders section-level tables only, so a page misclassification
 * was visible as a low score with no way to see which page went where.
 *
 * This module answers the two questions the existing UI annotates with:
 *
 *   - "Is this page's class the one ground truth expects?" (Document Pages)
 *   - "Is this section's class the one ground truth expects, and if not, what
 *     does ground truth say these pages are?" (Document Sections, and the
 *     Visual Editor's Show Evaluation mode)
 *
 * Both are derived from **pages**, deliberately, rather than by pairing section
 * ids: when the split itself is wrong there is no single counterpart section to
 * compare against, but every page still has a ground-truth class.
 */

/** One page's expected and predicted class, as stickler's doc-split emits it. */
export interface PageComparison {
  /** 1-based page number, matching page ids in the UI (see `pageNumberFor`). */
  pageNumber: number;
  /** Raw 0-based index as stored in results.json. */
  pageIndex: number;
  groundTruthClass: string;
  predictedClass: string;
  correct: boolean;
}

interface RawPageDetail {
  page_index?: number;
  ground_truth_class?: string;
  predicted_class?: string;
  correct?: boolean;
}

interface RawDocSplitMetrics {
  page_details?: RawPageDetail[];
}

/**
 * Page indices in results.json are 0-based (a section's `page_indices` are
 * computed as `page_id - min(page_id)`), while page ids everywhere in the UI are
 * 1-based. Convert once, here, so no caller has to remember.
 */
const pageNumberFor = (pageIndex: number): number => pageIndex + 1;

/**
 * stickler's placeholder for a page present on only one side of the comparison.
 * It is an absence of ground truth, not a class name.
 */
const MISSING_CLASS = 'Missing';

/**
 * A page-number-keyed index over a document's classification comparison.
 *
 * Built once per document and shared by the panels, so annotating a table row
 * costs a map lookup rather than a scan of `page_details`.
 */
export interface ClassificationIndex {
  byPageNumber: Map<number, PageComparison>;
  /** False when the document carries no comparison — callers annotate nothing. */
  hasGroundTruth: boolean;
}

export const EMPTY_CLASSIFICATION_INDEX: ClassificationIndex = {
  byPageNumber: new Map(),
  hasGroundTruth: false,
};

/**
 * Read the per-page comparison out of a parsed `results.json`.
 *
 * Returns an empty index when the document has no doc-split metrics — the normal
 * case for a document evaluated without section-level ground truth, which means
 * "nothing to annotate" rather than an error.
 */
export const extractClassificationIndex = (evaluationResults: Record<string, unknown> | null | undefined): ClassificationIndex => {
  const metrics = evaluationResults?.doc_split_metrics as RawDocSplitMetrics | undefined;
  if (!metrics || typeof metrics !== 'object') return EMPTY_CLASSIFICATION_INDEX;

  const byPageNumber = new Map<number, PageComparison>();
  (metrics.page_details ?? []).forEach((detail) => {
    if (typeof detail?.page_index !== 'number') return;
    byPageNumber.set(pageNumberFor(detail.page_index), {
      pageIndex: detail.page_index,
      pageNumber: pageNumberFor(detail.page_index),
      groundTruthClass: detail.ground_truth_class ?? MISSING_CLASS,
      predictedClass: detail.predicted_class ?? MISSING_CLASS,
      // stickler's own verdict rather than a re-derived string equality: it owns
      // the comparison and it may not be a raw match.
      correct: detail.correct === true,
    });
  });

  return { byPageNumber, hasGroundTruth: byPageNumber.size > 0 };
};

/** Ground truth's class for one page, or null when it has none. */
export const groundTruthClassForPage = (index: ClassificationIndex, pageNumber: number): string | null => {
  const page = index.byPageNumber.get(pageNumber);
  if (!page || page.groundTruthClass === MISSING_CLASS) return null;
  return page.groundTruthClass;
};

export type ClassVerdict = 'match' | 'mismatch' | 'unknown';

/**
 * Compare one page's class against ground truth.
 *
 * Returns `unknown` — not `mismatch` — when there is no ground truth for the
 * page, so an incomplete baseline cannot be reported as a misclassification.
 */
export const comparePageClass = (
  index: ClassificationIndex,
  pageNumber: number,
  predictedClass: string | null | undefined,
): ClassVerdict => {
  const expected = groundTruthClassForPage(index, pageNumber);
  if (!expected) return 'unknown';
  if (!predictedClass) return 'mismatch';
  return expected === predictedClass ? 'match' : 'mismatch';
};

/** What ground truth expects across a set of pages, grouped by class. */
export interface ExpectedClass {
  className: string;
  /** 1-based page numbers ground truth assigns to this class. */
  pageNumbers: number[];
}

export interface SectionClassComparison {
  verdict: ClassVerdict;
  /** Ground truth's classes for the section's pages, most pages first. */
  expected: ExpectedClass[];
  /** Section pages ground truth has no class for. */
  unknownPageNumbers: number[];
}

/**
 * Compare a section's class against what ground truth says its pages are.
 *
 * A section spanning two ground-truth classes is a mismatch even when one of
 * them equals the predicted class: there the section *boundary* is what is
 * wrong, and naming both classes is what makes that visible. A section whose
 * class is right for every page ground truth knows about is a match, even if
 * ground truth is silent about some of its pages.
 */
export const compareSectionClass = (
  index: ClassificationIndex,
  pageNumbers: number[],
  predictedClass: string | null | undefined,
): SectionClassComparison => {
  const byClass = new Map<string, number[]>();
  const unknownPageNumbers: number[] = [];

  pageNumbers.forEach((pageNumber) => {
    const expected = groundTruthClassForPage(index, pageNumber);
    if (!expected) {
      unknownPageNumbers.push(pageNumber);
      return;
    }
    const pages = byClass.get(expected);
    if (pages) pages.push(pageNumber);
    else byClass.set(expected, [pageNumber]);
  });

  const expected: ExpectedClass[] = [...byClass.entries()]
    .map(([className, pages]) => ({ className, pageNumbers: pages.sort((a, b) => a - b) }))
    .sort((a, b) => b.pageNumbers.length - a.pageNumbers.length || a.className.localeCompare(b.className));

  if (expected.length === 0) return { verdict: 'unknown', expected, unknownPageNumbers };

  const verdict: ClassVerdict = expected.length === 1 && expected[0].className === predictedClass ? 'match' : 'mismatch';

  return { verdict, expected, unknownPageNumbers };
};

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
  // Matched against the whole `evaluation/report.md` tail rather than the
  // filename alone, so this stays pinned to the one documented key template
  // (EVALUATION_RESULTS_KEY_TEMPLATE, `{input_key}/evaluation/results.json`).
  // A document also carries a summary report; deriving a results URI from that
  // would produce a path that does not exist and read as a missing evaluation
  // rather than as the wrong input. A version snapshot's longer prefix still
  // ends this way, so nothing legitimate is excluded.
  if (!evaluationReportUri.endsWith('/evaluation/report.md')) return null;
  return evaluationReportUri.replace(/\/report\.md$/, '/results.json');
};

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
