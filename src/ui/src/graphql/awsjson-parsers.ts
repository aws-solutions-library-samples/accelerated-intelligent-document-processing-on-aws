// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type {
  MeteringData,
  HITLReviewHistoryEntry,
  AccuracyBreakdown,
  CostBreakdown,
  TestRunConfig,
  WeightedOverallScores,
  SplitClassificationMetrics,
  GradedPacketMetrics,
  ClassificationErrors,
  FieldMetrics,
  ConfusionMatrix,
  ConfidenceMetrics,
  ComparisonMetrics,
  ConfigSettingValues,
  ConfigurationData,
  PricingData,
  ModelConfigLimitsData,
  StepFunctionStepPayload,
  BedrockModelsQuota,
} from './awsjson-types';

const MAX_PARSE_DEPTH = 3;

function safeParse<T>(json: unknown, fallback: T): T {
  if (json == null || json === '') return fallback;
  let result: unknown = json;
  let depth = 0;
  while (typeof result === 'string' && depth < MAX_PARSE_DEPTH) {
    depth++;
    try {
      result = JSON.parse(result);
    } catch {
      return fallback;
    }
  }
  if (typeof result !== 'object' || result === null) {
    if (result === null && fallback === null) return null as T;
    return fallback;
  }
  if (Array.isArray(fallback) && !Array.isArray(result)) return fallback;
  if (!Array.isArray(fallback) && fallback !== null && Array.isArray(result)) return fallback;
  return result as T;
}

export function parseMetering(json: unknown): MeteringData | null {
  return safeParse<MeteringData | null>(json, null);
}

export function parseHITLReviewHistory(json: unknown): HITLReviewHistoryEntry[] {
  return safeParse<HITLReviewHistoryEntry[]>(json, []);
}

export function parseAccuracyBreakdown(json: unknown): AccuracyBreakdown {
  return safeParse<AccuracyBreakdown>(json, {});
}

export function parseCostBreakdown(json: unknown): CostBreakdown {
  return safeParse<CostBreakdown>(json, {});
}

export function calculateTotalPages(costBreakdown: CostBreakdown): number {
  let totalPages = 0;
  Object.values(costBreakdown).forEach((services) => {
    Object.values(services).forEach((details) => {
      if (details.unit === 'pages') totalPages += Number(details.value) || 0;
    });
  });
  return totalPages;
}

export function calculateAvgCostPerPage(totalCost: number | null | undefined, costBreakdown: CostBreakdown | null): number | null {
  if (totalCost == null || !costBreakdown) return null;
  const totalPages = calculateTotalPages(costBreakdown);
  return totalPages > 0 ? totalCost / totalPages : null;
}

export function parseTestRunConfig(json: unknown): TestRunConfig | null {
  return safeParse<TestRunConfig | null>(json, null);
}

export function parseWeightedOverallScores(json: unknown): WeightedOverallScores {
  return safeParse<WeightedOverallScores>(json, {});
}

/**
 * Parse ``weightedOverallScores`` and drop entries whose value isn't a finite
 * number. The aggregation Lambda already omits documents excluded from scoring
 * (no extractable schema) from the map, so in practice this returns the same
 * shape — but it lets Test Studio call sites read the map as clean data
 * without repeating a defensive filter at every histogram / lowest-scores /
 * average call site (previously 6+ duplicates). Callers that need the raw
 * shape (e.g. exposing null explicitly) can still use ``parseWeightedOverallScores``.
 */
export function parseWeightedOverallScoresFinite(json: unknown): Record<string, number> {
  const raw = parseWeightedOverallScores(json);
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (typeof v === 'number' && Number.isFinite(v)) {
      out[k] = v;
    }
  }
  return out;
}

export function parseSplitClassificationMetrics(json: unknown): SplitClassificationMetrics {
  return safeParse<SplitClassificationMetrics>(json, {});
}

export function parseGradedPacketMetrics(json: unknown): GradedPacketMetrics {
  return safeParse<GradedPacketMetrics>(json, {});
}

export function parseClassificationErrors(json: unknown): ClassificationErrors {
  return safeParse<ClassificationErrors>(json, {});
}

export function parseFieldMetrics(json: unknown): FieldMetrics {
  return safeParse<FieldMetrics>(json, {});
}

export function parseConfusionMatrix(json: unknown): ConfusionMatrix {
  return safeParse<ConfusionMatrix>(json, {});
}

export function parseConfidenceMetrics(json: unknown): ConfidenceMetrics | null {
  return safeParse<ConfidenceMetrics | null>(json, null);
}

export function parseComparisonMetrics(json: unknown): ComparisonMetrics {
  return safeParse<ComparisonMetrics>(json, {});
}

export function parseConfigSettingValues(json: unknown): ConfigSettingValues {
  return safeParse<ConfigSettingValues>(json, {});
}

export function parseConfigurationData(json: unknown): ConfigurationData | null {
  return safeParse<ConfigurationData | null>(json, null);
}

export function parsePricingData(json: unknown): PricingData | null {
  return safeParse<PricingData | null>(json, null);
}

export function parseModelConfigLimitsData(json: unknown): ModelConfigLimitsData | null {
  return safeParse<ModelConfigLimitsData | null>(json, null);
}

export function parseStepFunctionPayload(json: unknown): StepFunctionStepPayload | null {
  return safeParse<StepFunctionStepPayload | null>(json, null);
}

export function parseBedrockModelsQuota(json: unknown): BedrockModelsQuota | null {
  return safeParse<BedrockModelsQuota | null>(json, null);
}
