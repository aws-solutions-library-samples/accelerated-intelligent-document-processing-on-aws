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

import { canonicalKey, sanitizeFileName } from '../config-prefix-key';
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
