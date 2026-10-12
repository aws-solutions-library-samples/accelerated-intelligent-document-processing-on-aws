// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The client mirror of the server's key canonicalization, measured against the
 * shared fixture table.
 *
 * `scripts/tests/test_config_prefix_key_mirror.py` asserts the Python authority
 * over the same table, so a change to either implementation that is not matched
 * in the other fails on one side. The failure this guards is silent: a mirror
 * that diverges makes the upload panel preview a *different key* from the one the
 * upload lands on, so a `reject` mapping gives no warning and the user gets a 400.
 */

import { describe, expect, it } from 'vitest';

import { canonicalKey, prefixError, sanitizeFileName, storedPrefix, type MatchMode } from '../config-prefix-key';
import fixtures from './config-prefix-key.fixtures.json';

describe('canonicalKey', () => {
  it.each(fixtures.cases as [string, string][])('canonicalizes %j to %j', (key, expected) => {
    expect(canonicalKey(key)).toBe(expected);
  });

  it('covers enough of the key space to be worth trusting', () => {
    // A fixture table that shrinks is a gate that quietly stops checking.
    expect(fixtures.cases.length).toBeGreaterThanOrEqual(16);
  });
});

describe('sanitizeFileName', () => {
  it('mirrors the resolver: spaces become underscores and nothing else changes', () => {
    expect(sanitizeFileName('my file name.pdf')).toBe('my_file_name.pdf');
    expect(sanitizeFileName("odd'chars&ok.pdf")).toBe("odd'chars&ok.pdf");
  });
});

describe('storedPrefix', () => {
  // The derivation that decides whether a mapping matches anything at all. A
  // folder name saved without its trailing slash becomes an exact-object-key
  // mapping, which never matches an uploaded document because the upload panel
  // builds `<prefix>/<filename>` -- so the mapping is stored, governs nothing, and
  // every document shows its own upload tag. That is what the explicit match-mode
  // control and this function exist to prevent.
  it.each([
    // [typed, mode, saved]
    ['bob1', 'prefix', 'bob1/'],
    ['bob1/', 'prefix', 'bob1/'],
    ['bob1//', 'prefix', 'bob1/'],
    ['  bob1  ', 'prefix', 'bob1/'],
    ['acme/invoices', 'prefix', 'acme/invoices/'],
    ['acme/invoices/', 'prefix', 'acme/invoices/'],
    ['bob1', 'exact', 'bob1'],
    ['bob1/', 'exact', 'bob1'],
    ['acme/invoices/jan.pdf', 'exact', 'acme/invoices/jan.pdf'],
    ['acme/invoices/jan.pdf/', 'exact', 'acme/invoices/jan.pdf'],
    // Nothing typed, and a value that is only slashes, both derive to empty so the
    // form refuses rather than saving a root mapping.
    ['', 'prefix', ''],
    ['   ', 'prefix', ''],
    ['/', 'prefix', ''],
    ['//', 'exact', ''],
  ] as [string, MatchMode, string][])('%s in %s mode saves as %s', (typed, mode, saved) => {
    expect(storedPrefix(typed, mode)).toBe(saved);
  });

  it('is idempotent, so re-saving an unedited mapping does not change its mode', () => {
    for (const mode of ['prefix', 'exact'] as MatchMode[]) {
      const once = storedPrefix('acme/invoices', mode);
      expect(storedPrefix(once, mode)).toBe(once);
    }
  });

  it('round-trips the mode a stored value encodes', () => {
    // How the edit form derives which radio to check. Getting this backwards would
    // rewrite a folder mapping into an exact one on an unrelated save.
    const modeOf = (stored: string): MatchMode => (stored.endsWith('/') ? 'prefix' : 'exact');
    for (const mode of ['prefix', 'exact'] as MatchMode[]) {
      const stored = storedPrefix('acme/invoices', mode);
      expect(modeOf(stored)).toBe(mode);
      expect(storedPrefix(stored, modeOf(stored))).toBe(stored);
    }
  });
});

describe('prefixError', () => {
  // The other half of the shared table. `prefix_rejection_reason` in
  // `lib/idp_common_pkg/idp_common/config/prefix_mappings.py` is the authority and
  // `scripts/tests/test_config_prefix_key_mirror.py` asserts it over these same
  // rows, so a change to either implementation that is not matched in the other
  // fails on one side.
  //
  // The fixture records WHICH rule fires, not the whole sentence: this side expands
  // two of the server's messages for the admin reading them, so the two may word a
  // refusal differently. What they may not do is disagree about whether a prefix is
  // refused, or about which rule refused it -- the rules overlap, so the order
  // decides, and two sides disagreeing about the order is a confusing way to be
  // consistent.
  const cases = fixtures.rejections as [string, string | null][];

  it.each(cases)('classifies %j the way the shared table says', (prefix, fragment) => {
    const actual = prefixError(prefix);
    if (fragment === null) {
      expect(actual).toBeNull();
    } else {
      expect(actual).not.toBeNull();
      expect(actual).toContain(fragment);
    }
  });

  it('covers both verdicts and at least one overlapping prefix', () => {
    // A table of only-refusals would pin half the rule, and without a prefix that
    // matches several rules nothing here can tell a reordered implementation from a
    // correct one.
    expect(cases.some(([, fragment]) => fragment === null)).toBe(true);
    expect(cases.some(([, fragment]) => fragment !== null)).toBe(true);
    expect(cases).toEqual(expect.arrayContaining([['/acme//x/', "must not start with '/'"]]));
  });

  it('returns null for an empty prefix, which the server refuses', () => {
    // A deliberate asymmetry, recorded in the fixture's `serverOnlyRules`: an
    // untouched field must not show an error. Save is disabled on empty anyway, so
    // nothing can be submitted through the gap.
    expect(prefixError('')).toBeNull();
    expect(Object.keys(fixtures.serverOnlyRules)).toContain('empty-prefix');
  });
});
