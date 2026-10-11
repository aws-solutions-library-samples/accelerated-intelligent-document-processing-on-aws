// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Client-side mirror of the server's S3 key canonicalization.
 *
 * The authority is `canonical_key` in
 * `lib/idp_common_pkg/idp_common/config/prefix_mappings.py`. This copy exists so
 * the upload panel can ask "what would happen to this key?" about the key the
 * upload will actually land on, rather than about the one the user typed.
 *
 * ⚠️ **A partial mirror is worse than none.** It must collapse interior repeats as
 * well as leading and trailing slashes, and drop `.` segments. One that only
 * strips the ends previews `acme//invoices` as *unmapped* while the upload lands
 * on `acme/invoices/<file>`, which is governed — so under a `reject` mapping the
 * user gets no warning and a 400 at ingest.
 *
 * The two implementations are pinned in step by a shared fixture table,
 * `__tests__/config-prefix-key.fixtures.json`, read by BOTH this module's vitest
 * suite and `scripts/tests/test_config_prefix_key_mirror.py`. Drift in either
 * implementation fails on one side. Add a case to the table, not to one suite.
 */

/**
 * The form of `key` that prefix-mapping matching is defined against: leading and
 * repeated slashes collapsed, `.` segments dropped, trailing slash preserved
 * because it is the prefix/exact mode selector.
 *
 * `..` is deliberately left alone — S3 keys are opaque strings with no parent
 * directory, so `a/b/../c` is a real, distinct object and resolving it would claim
 * a key S3 serves from somewhere else.
 */
export const canonicalKey = (key: string): string => {
  if (!key) return '';
  const trailing = key.endsWith('/');
  const canonical = key
    .split('/')
    .filter((segment) => segment && segment !== '.')
    .join('/');
  return trailing && canonical ? `${canonical}/` : canonical;
};

/** Mirrors `upload_resolver`'s only filename rewrite, so the probe key is the real one. */
export const sanitizeFileName = (name: string): string => name.replace(/ /g, '_');

/** Which match mode a mapping uses: a folder prefix, or one exact object key. */
export type MatchMode = 'prefix' | 'exact';

/**
 * The prefix value actually SAVED for a typed path and a chosen match mode.
 *
 * ⚠️ **The trailing slash is semantic and must not be left to punctuation the
 * administrator happens to type.** The server stores the prefix verbatim and reads
 * the trailing slash as the mode selector (`match_kind` in
 * `lib/idp_common_pkg/idp_common/config/prefix_mappings.py`): `acme/` governs
 * everything beneath it, `acme` governs one object literally named `acme`.
 *
 * A folder name saved without the slash is therefore an exact-object-key mapping
 * that can never match an uploaded document, because the upload panel builds
 * `<prefix>/<filename>`. The mapping is stored and listed and governs nothing, and
 * every document falls through to its own upload tag with no mapping recorded --
 * which in the document list is indistinguishable from "the mapping deferred to the
 * upload", so the conflict mode reads as broken rather than as never reached.
 *
 * So the mode is an explicit control in the form and this derives the stored
 * string from it. Normalizing a typed value *without* such a control is what the
 * original design rightly refused, since it makes exact mode unexpressible;
 * deriving it from a deliberate choice keeps both modes reachable and removes the
 * trap.
 *
 * Exact mode strips a trailing slash rather than keeping it: a key ending in `/`
 * is an S3 folder pseudo-object, which the ingest Lambda skips outright, so such a
 * mapping could never match a document either.
 */
export const storedPrefix = (prefix: string, mode: MatchMode): string => {
  const trimmed = prefix.trim();
  if (!trimmed) return '';
  const withoutTrailing = trimmed.replace(/\/+$/, '');
  if (!withoutTrailing) return '';
  return mode === 'prefix' ? `${withoutTrailing}/` : withoutTrailing;
};

/**
 * Why the typed prefix can never work as a mapping, or null if it can.
 *
 * A client-side mirror of the server's `prefix_rejection_reason`
 * (`lib/idp_common_pkg/idp_common/config/prefix_mappings.py`) — the server stays
 * the authority, this only saves a round trip. It is returned as a **string**
 * because it is rendered as the form field's `errorText`, not its
 * `constraintText`: constraint text is advisory, so a screen reader never
 * announces it as a problem and Save stays enabled. A prefix that can never
 * match is not advice.
 *
 * The checks are in the server's order on purpose. They overlap — `'/'` fails
 * three of them — so the order decides which sentence the admin reads, and the
 * two sides disagreeing about that is a confusing way to be consistent.
 *
 * ⚠️ **Pinned to the server by `__tests__/config-prefix-key.fixtures.json`**, the
 * same shared table that holds `canonicalKey` to `canonical_key`: its
 * `rejections` array records, per prefix, WHICH rule fires rather than the exact
 * prose, so the two sides may word a refusal differently but may not disagree
 * about whether a prefix is refused or about which rule refused it. Two server
 * rules are deliberately absent here and named in that table's
 * `serverOnlyRules`; see `scripts/tests/test_config_prefix_key_mirror.py`.
 */
export const prefixError = (prefix: string): string | null => {
  if (!prefix) return null;
  const ROOT_REFUSAL =
    'A root mapping is not allowed: it would change the configuration of every unmapped upload in this deployment. The active Configuration Profile already serves that purpose.';
  if (prefix === '/' || prefix === '//') return ROOT_REFUSAL;
  if (prefix.startsWith('/')) {
    return "A mapping prefix must not start with '/'. S3 keys do not, so the mapping would never match.";
  }
  if (prefix.includes('//')) {
    return "A mapping prefix must not contain '//'. S3 treats 'acme//invoices/' as a different folder from 'acme/invoices/', so the mapping would never match.";
  }
  if (prefix.split('/').some((segment) => segment === '.' || segment === '..')) {
    return "A mapping prefix must not contain '.' or '..' path segments.";
  }
  // Last, as on the server: anything left that canonicalizes to nothing at all.
  if (prefix.split('/').every((segment) => !segment)) return ROOT_REFUSAL;
  return null;
};
