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
 * ⚠️ **A partial mirror is worse than none**, and that is not hypothetical: an
 * earlier version stripped leading and trailing slashes but not interior repeats,
 * so a typed `acme//invoices` previewed as *unmapped* while the upload landed on
 * `acme/invoices/<file>` — governed by a mapping. Under a `reject` mapping the
 * user got no warning and a 400 at ingest.
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
