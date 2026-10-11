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

import { canonicalKey, sanitizeFileName, storedPrefix, type MatchMode } from '../config-prefix-key';
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
