// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Types of ./coverage.mjs for the TypeScript callers (the Vitest suite).

/** One translation catalog measured against `en.json`. */
export interface LocaleCoverage {
  /** Entries that are present, valid and current. */
  translated: number;
  /** Ids `en.json` has and the catalog does not, sorted. */
  missing: string[];
  /** Ids whose recorded `sourceHash` is not the current English's, sorted. */
  stale: string[];
  /** Ids the build would reject, sorted. */
  invalid: string[];
  /** Why, one line per problem, in the build's wording. */
  problems: string[];
  /** Ids `en.json` does not have, sorted. */
  dead: string[];
  /** `translated / sourceKeys`, to one decimal; 0 when there are no source keys. */
  coveragePercent: number;
}

export interface CoverageReport {
  /** The number of ids in `en.json`. */
  sourceKeys: number;
  /** One entry per translation catalog, keyed by locale. */
  locales: Record<string, LocaleCoverage>;
}

/** Coverage of one parsed translation catalog against the parsed `en.json`; the verdicts are `compileCatalog`'s. */
export declare const localeCoverage: (
  enJson: Record<string, unknown>,
  localeJson: Record<string, unknown>,
  locale: string,
) => LocaleCoverage;

/**
 * The report over a catalog directory, or over the one locale named. Throws on a missing `en.json`, an
 * unreadable or malformed catalog, or a locale with no catalog file.
 */
export declare const coverageReport: (catalogDir: string, options?: { locale?: string | null }) => CoverageReport;

/** The report as text: one block per locale, totals first, then the ids in each list. */
export declare const formatReport: (report: CoverageReport, catalogDir: string) => string;
