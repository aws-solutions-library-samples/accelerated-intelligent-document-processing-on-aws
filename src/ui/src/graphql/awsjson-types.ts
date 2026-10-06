// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Parsed metering data from Document.Metering AWSJSON field */
export interface MeteringData {
  [key: string]: unknown;
}

/** Parsed HITL review history from Document.HITLReviewHistory AWSJSON field */
export interface HITLReviewHistoryEntry {
  sectionId?: string;
  action?: string;
  timestamp?: string;
  user?: string;
  [key: string]: unknown;
}

/** Parsed accuracy breakdown from TestRun.accuracyBreakdown AWSJSON field */
export interface AccuracyBreakdown {
  [documentClass: string]: {
    accuracy?: number;
    total?: number;
    correct?: number;
    [key: string]: unknown;
  };
}

/** Parsed cost breakdown service detail from TestRun.costBreakdown AWSJSON field */
export interface CostBreakdownServiceDetail {
  estimated_cost?: number;
  value?: number;
  unit_cost?: number;
  unit?: string;
  [key: string]: unknown;
}

/** Parsed cost breakdown from TestRun.costBreakdown AWSJSON field */
export type CostBreakdown = Record<string, Record<string, CostBreakdownServiceDetail>>;

/** Parsed test run config from TestRun.config AWSJSON field */
export interface TestRunConfig {
  [key: string]: unknown;
}

/** Parsed weighted overall scores from TestRun.weightedOverallScores AWSJSON field */
export interface WeightedOverallScores {
  [documentId: string]: number;
}

/** Parsed split classification metrics from TestRun.splitClassificationMetrics AWSJSON field */
export interface SplitClassificationMetrics {
  [className: string]: {
    accuracy?: number;
    total?: number;
    [key: string]: unknown;
  };
}

/** A predicted section over the pages of an unmatched ground-truth section. */
export interface PredictedSectionSummary {
  class?: string | null;
  /** Its pages as sorted, inclusive 0-based `[first, last]` runs. */
  page_ranges?: number[][];
}

/** One ground-truth section the prediction did not reproduce. */
export interface ClassificationError {
  doc_key?: string;
  section_id?: string | number | null;
  /**
   * `class` — wrong document class, so extraction ran the wrong schema.
   * `unmatched` — a ground-truth section no predicted section matched (a
   * splitting difference). `order` — right class and pages, wrong page order.
   */
  kind?: 'class' | 'unmatched' | 'order';
  expected_class?: string | null;
  predicted_class?: string | null;
  expected_pages?: number[];
  predicted_pages?: number[];
  /**
   * `unmatched` only: the predicted sections sharing a page with the expected
   * pages, in page order, capped by the aggregation Lambda
   * (MAX_PREDICTED_SECTIONS_PER_ERROR). `predicted_section_count` is the
   * uncapped number. Absent on runs aggregated by an earlier release, which
   * report an unmatched section as kind `class` with predicted class
   * "No Match" instead.
   */
  predicted_sections?: PredictedSectionSummary[];
  predicted_section_count?: number;
  /**
   * `unmatched` only, and present only when true: some of the expected pages
   * are on no predicted section the evaluation recorded, in a document whose
   * record is known to be missing one. A section of a class excluded from
   * processing is recorded without its class or pages, and one whose result
   * failed to load is not recorded. What the prediction put on those pages is
   * unknown rather than absent. Also true when the ground-truth section was
   * itself recorded without pages.
   */
  predicted_sections_incomplete?: boolean;
}

/**
 * Parsed TestRun.classificationErrors.
 *
 * `errors` is capped by the aggregation Lambda because the whole run result is
 * one DynamoDB attribute; `total` is the uncapped count, so a truncated list can
 * still say how much it is not showing. `{}` on runs aggregated via the Athena
 * fallback, which has the percentages but not the per-section detail.
 */
export interface ClassificationErrors {
  errors?: ClassificationError[];
  total?: number;
  documents_affected?: number;
  truncated?: boolean;
}

/**
 * Parsed graded packet metrics from TestRun.gradedPacketMetrics AWSJSON field.
 *
 * Aggregated by the test-execution-aggregation Lambda as a simple unweighted
 * mean across documents that reported each key (see
 * ``_aggregate_graded_packet_metrics``). All values are in [0.0, 1.0] where
 * 1.0 is perfect. Empty ``{}`` when no document reported graded metrics —
 * older results.json payloads or classification runs with no page overlap.
 */
export interface GradedPacketMetrics {
  mean?: {
    final_score?: number;
    clustering_score?: number;
    v_measure?: number;
    rand_index?: number;
    avg_ordering_score?: number;
    [key: string]: number | undefined;
  };
  per_document?: {
    [documentId: string]: {
      final_score?: number;
      clustering_score?: number;
      v_measure?: number;
      rand_index?: number;
      avg_ordering_score?: number;
      [key: string]: number | undefined;
    };
  };
  document_count?: number;
}

/** Parsed field metrics from TestRun.fieldMetrics AWSJSON field */
export interface FieldMetrics {
  [fieldName: string]: {
    tp?: number;
    fp?: number;
    tn?: number;
    fn?: number;
    [key: string]: unknown;
  };
}

/** Parsed confusion matrix from TestRun.confusionMatrix AWSJSON field */
export interface ConfusionMatrix {
  tp?: number;
  fp?: number;
  tn?: number;
  fn?: number;
  fa?: number;
  fd?: number;
  [key: string]: unknown;
}

/** Parsed confidence metrics from TestRun.confidenceMetrics AWSJSON field (Stickler v0.4.0+) */
export interface ConfidenceMetrics {
  overall?: {
    auroc?: { value: number | null };
    ece?: { value: number | null; bins?: Array<unknown> };
    brier?: { value: number | null };
    [key: string]: unknown;
  };
  fields?: {
    [fieldName: string]: {
      auroc?: { value: number | null };
      ece?: { value: number | null; bins?: Array<unknown> };
      brier?: { value: number | null };
      [key: string]: unknown;
    };
  };
  coverage?: {
    fields_with_confidence: number;
    fields_total: number;
    ratio: number;
  };
  field_count?: number;
  total_pairs?: number;
  [key: string]: unknown;
}

/** Parsed comparison metrics from TestRunComparison.metrics AWSJSON field */
export interface ComparisonMetrics {
  [key: string]: unknown;
}

/** Parsed config setting values from ConfigSetting.values AWSJSON field */
export interface ConfigSettingValues {
  [testRunId: string]: unknown;
}

/** Parsed configuration schema/default/custom from ConfigurationResponse AWSJSON fields */
export interface ConfigurationData {
  [key: string]: unknown;
}

/** Parsed pricing data from PricingResponse AWSJSON fields */
export interface PricingData {
  pricing?: Array<{
    name: string;
    units?: Array<{
      name: string;
      price: number;
      [key: string]: unknown;
    }>;
    [key: string]: unknown;
  }>;
  [key: string]: unknown;
}

/** Parsed model config limits from ModelConfigLimitsResponse AWSJSON fields */
export interface ModelConfigLimitsData {
  model_limits?: Array<{
    pattern: string;
    max_output_tokens: number;
    max_input_tokens?: number;
    description?: string;
    reference?: string;
    [key: string]: unknown;
  }>;
  [key: string]: unknown;
}

/** Parsed step function step input/output from AWSJSON fields */
export interface StepFunctionStepPayload {
  [key: string]: unknown;
}

/** Parsed bedrock models quota from QuotasUsed.bedrock_models AWSJSON field */
export interface BedrockModelsQuota {
  [modelId: string]: unknown;
}
