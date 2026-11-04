// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Short display names for a list of object keys: each key shows the fewest trailing
// path segments that no other key in the list ends with, so rows with the same file
// name in different folders stay distinguishable.

const suffix = (segments: readonly string[], count: number): string => segments.slice(-count).join('/');

/**
 * The shortest unique trailing `/`-suffix of each key. A key that appears more than once,
 * or that is itself a trailing suffix of another key, maps to the full key.
 */
export const distinguishingNames = (keys: readonly string[]): Map<string, string> => {
  const occurrences = new Map<string, number>();
  keys.forEach((key) => occurrences.set(key, (occurrences.get(key) ?? 0) + 1));
  const distinct = [...occurrences.keys()];
  const split = new Map(distinct.map((key) => [key, key.split('/')]));

  // How many distinct keys end with each candidate suffix.
  const suffixCounts = new Map<string, number>();
  split.forEach((segments) => {
    for (let n = 1; n <= segments.length; n += 1) {
      const s = suffix(segments, n);
      suffixCounts.set(s, (suffixCounts.get(s) ?? 0) + 1);
    }
  });

  const names = new Map<string, string>();
  split.forEach((segments, key) => {
    if ((occurrences.get(key) ?? 0) > 1) {
      names.set(key, key);
      return;
    }
    for (let n = 1; n <= segments.length; n += 1) {
      const s = suffix(segments, n);
      if (suffixCounts.get(s) === 1) {
        names.set(key, s);
        return;
      }
    }
    names.set(key, key);
  });
  return names;
};
