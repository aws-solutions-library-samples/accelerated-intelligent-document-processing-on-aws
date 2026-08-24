// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Ground-truth comparison for a section's document class.
 *
 * "Show evaluation" compares extracted *fields* against the baseline but says
 * nothing about the classification — yet the class is also model output, and
 * getting it wrong invalidates every field beneath it, because extraction then
 * ran the wrong schema. The comparison already exists in the document's
 * evaluation results (`doc_split_metrics.section_details_with_order`); nothing
 * surfaced it.
 *
 * This reads that structure. It does not compute anything.
 */

/** One entry of `doc_split_metrics.section_details_with_order`. */
interface SectionDetail {
  section_id?: string | number;
  ground_truth_class?: string | null;
  predicted_class?: string | null;
  ground_truth_pages?: number[];
  predicted_pages?: number[];
  matched?: boolean;
  order_matched?: boolean;
}

export type ClassificationVerdict = 'match' | 'class' | 'unmatched' | 'order';

export interface ClassificationComparison {
  expected: string | null;
  predicted: string | null;
  expectedPages: number[];
  predictedPages: number[];
  verdict: ClassificationVerdict;
}

/**
 * Reduce a section id to its digits so the two conventions in play compare equal.
 *
 * The repo uses both `"1"` (IDP section results) and `"section_1"` (the doc-split
 * details, which come from the upstream evaluator), and either can appear here
 * depending on which produced the payload. Comparing raw strings would silently
 * never match for one of them — and a silent never-match is indistinguishable
 * from "this document is classified correctly".
 */
const normalizeSectionId = (value: string | number | null | undefined): string | null => {
  if (value === null || value === undefined) return null;
  const digits = String(value).match(/\d+/);
  return digits ? digits[0] : String(value).trim().toLowerCase() || null;
};

const verdictFor = (detail: SectionDetail): ClassificationVerdict => {
  const expected = detail.ground_truth_class ?? null;
  const predicted = detail.predicted_class ?? null;
  if (predicted === null || predicted === '') return 'unmatched';
  if (expected !== predicted) return 'class';
  if (detail.order_matched === false) return 'order';
  return 'match';
};

/**
 * Find this section's classification comparison, or null when there isn't one.
 *
 * Returns null rather than guessing. Deliberate: showing the *wrong* expected
 * class next to a document is worse than showing none — a reviewer would
 * "correct" a document that was already right, or trust one that was wrong.
 *
 * Two ways to match, both exact:
 *
 * 1. By section id, normalized as above.
 * 2. When the document has a single section and the evaluation reports a single
 *    entry, they are the same section whatever the ids are called.
 *
 * There is deliberately no page-overlap fallback. `ground_truth_pages` are
 * 0-based indices while the UI's `PageIds` are 1-based page numbers, so overlap
 * matching would be right for some documents and off-by-one for others, with no
 * way to tell which from the payload alone.
 */
export const classificationComparisonForSection = (
  evaluationResults: Record<string, unknown> | null | undefined,
  sectionId: string | number | null | undefined,
  totalSectionsInDocument = 1,
): ClassificationComparison | null => {
  const docSplit = evaluationResults?.doc_split_metrics as Record<string, unknown> | undefined;
  const details = docSplit?.section_details_with_order;
  if (!Array.isArray(details) || details.length === 0) return null;

  const usable = details.filter((d): d is SectionDetail => typeof d === 'object' && d !== null);
  if (usable.length === 0) return null;

  const wanted = normalizeSectionId(sectionId);
  let detail = wanted === null ? undefined : usable.find((d) => normalizeSectionId(d.section_id) === wanted);

  if (!detail && usable.length === 1 && totalSectionsInDocument <= 1) {
    [detail] = usable;
  }
  if (!detail) return null;

  return {
    expected: detail.ground_truth_class ?? null,
    predicted: detail.predicted_class ?? null,
    expectedPages: detail.ground_truth_pages ?? [],
    predictedPages: detail.predicted_pages ?? [],
    verdict: verdictFor(detail),
  };
};

/** Human-readable reason, for the mismatch callout. */
export const describeVerdict = (comparison: ClassificationComparison): string => {
  switch (comparison.verdict) {
    case 'class':
      return `Classified as ${comparison.predicted}, but the ground truth for these pages is ${comparison.expected}. The fields below were extracted with the ${comparison.predicted} schema, so they may be wrong even where they look plausible.`;
    case 'unmatched':
      return `The ground truth expects a ${comparison.expected} section covering these pages, but no predicted section matched it. This is a document-splitting difference rather than a labelling one.`;
    case 'order':
      return 'The class and pages are correct, but the page order differs from the ground truth. Extraction is unaffected; this is what split accuracy "with order" measures.';
    default:
      return 'The document class matches the ground truth.';
  }
};
