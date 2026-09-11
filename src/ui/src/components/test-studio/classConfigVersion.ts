// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Which config's classes the ground-truth editor offers, and why.
 *
 * Extracted as a function because getting it wrong is silent. The previous
 * inline version fell through to `'default'` whenever a baseline carried no
 * config stamp, and `'default'` resolves to the **built-in preset** — so a test
 * set of hand-uploaded ground truth was offered the lending-package class list
 * regardless of what the deployment actually runs, and the document's real class
 * rendered as "Not defined in this config version" ([#662]).
 *
 * Nothing failed, nothing logged, and the UI hid the fallback: the config badge
 * was suppressed precisely when the version was `'default'`. So the source is
 * returned alongside the version and the caller is expected to display it.
 */

export type ClassConfigSource = 'baseline' | 'testSet' | 'active' | 'default';

export interface ClassConfigChoice {
  version: string;
  source: ClassConfigSource;
}

/**
 * Resolve in order of authority.
 *
 * 1. **The baseline's own stamp.** These labels were produced by that config, so
 *    its class list is the one they can legitimately be changed within — even if
 *    the deployment has since moved on.
 * 2. **The test set's declared version.** No stamp means the labels were not
 *    produced by a pipeline run (uploaded or synthetic), so there is no
 *    producing config to defer to; the set's own declaration is the next best
 *    statement of intent.
 * 3. **The active config.** What this deployment actually runs, and what a
 *    re-extract would use.
 * 4. **`'default'`** — the built-in preset. Correct only when the deployment has
 *    no active version at all, which is why it must be visibly labelled rather
 *    than passed off as the config in force.
 *
 * Blank strings are treated as absent: an empty `config_version` on a baseline
 * is missing data, not a request for a config named "".
 */
export const resolveClassConfigVersion = (
  stampedOnBaseline?: string | null,
  declaredOnTestSet?: string | null,
  activeVersion?: string | null,
): ClassConfigChoice => {
  const stamped = stampedOnBaseline?.trim();
  if (stamped) return { version: stamped, source: 'baseline' };

  const declared = declaredOnTestSet?.trim();
  if (declared) return { version: declared, source: 'testSet' };

  const active = activeVersion?.trim();
  if (active) return { version: active, source: 'active' };

  return { version: 'default', source: 'default' };
};

/** Badge text naming the config and, when it is a fallback, where it came from. */
export const describeClassConfigSource = ({ version, source }: ClassConfigChoice): string => {
  switch (source) {
    case 'baseline':
      return version;
    case 'testSet':
      return `${version} (test set)`;
    case 'active':
      return `${version} (active config)`;
    default:
      return 'built-in default';
  }
};
