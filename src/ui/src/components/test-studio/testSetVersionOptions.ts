// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The **Test set version** picker's options, as a function of the versions a set has.
 *
 * Extracted from `TestRunner.tsx` so the rule below can be tested against real inputs. It
 * is a rule about data, not about rendering: the previous source-level assertion on the
 * component would have passed with the branches inverted.
 */

import type { SelectProps } from '@cloudscape-design/components';

/** The fields of a published version this picker reads. */
export interface TestSetVersionOption {
  version: number;
  label?: string | null;
  /**
   * Whether the version has labels stored under `versions/{n}/baseline/`. Supplied by the
   * resolver, which probes the prefix; `null`/`undefined` means the answer is not known
   * (an older backend, or a failed read), in which case nothing is claimed.
   */
  hasStoredLabels?: boolean | null;
  /**
   * How many objects publishing copied. Provenance only — **not** a substitute for
   * `hasStoredLabels`, which is why it is declared here and read nowhere below: a version
   * published before publishing copied anything has no count and yet does have labels once
   * annotation backfilled them, and one published from a set with no labels yet has a count
   * of `0` and nothing stored at all.
   */
  snapshotObjectCount?: number | null;
}

export const CURRENT_LABELS = '__current__';

/**
 * What to warn about a version with no stored labels: a run pinned to it is staged from the
 * set's **current** labels instead, because the file copier falls back when the prefix is
 * empty. So pinning does not do what pinning is for, and this is the moment the choice is
 * made — otherwise the fallback appears only in the copier's log, after the run.
 *
 * Deliberately keyed on `hasStoredLabels === false` and not on the object count. The two
 * disagree in both directions: a version published before publishing copied anything has no
 * count and yet does have labels once annotation backfilled them, and a version published
 * from a set with no labels yet has a count of `0` and no stored labels at all.
 */
export const NO_STORED_LABELS = 'No stored labels — a run pinned here scores the current labels';

export const testSetVersionOptions = (versions: readonly TestSetVersionOption[]): SelectProps.Option[] => [
  { value: CURRENT_LABELS, label: 'Current labels', description: 'Including any annotation in progress' },
  ...versions.map((version) => ({
    value: String(version.version),
    label: `v${version.version}`,
    description:
      version.hasStoredLabels === false ? [version.label, NO_STORED_LABELS].filter(Boolean).join(' · ') : (version.label ?? undefined),
  })),
];
