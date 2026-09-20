// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Parity tests for the client-side config-profile scope matcher.
 *
 * Every expectation below was produced by running the same input through the
 * server's `scope_allows` (`lib/idp_common_pkg/idp_common/config_scope.py`), so
 * this suite is the thing that keeps the two halves in step. A case that
 * disagrees is not a matter of taste: a client stricter than the server hides a
 * profile the server would serve (which is the bug this matcher fixed), and a
 * client looser than the server offers one the server will refuse.
 */

import { describe, expect, it } from 'vitest';

import { scopeAllows } from '../config-scope';

describe('scopeAllows — unrestricted scopes', () => {
  it('treats a null, undefined or empty scope as unrestricted', () => {
    expect(scopeAllows(null, 'anything')).toBe(true);
    expect(scopeAllows(undefined, 'anything')).toBe(true);
    expect(scopeAllows([], 'anything')).toBe(true);
  });

  it('allows even a nameless profile when no scope is set', () => {
    // Nothing to check the name against, so there is nothing to deny.
    expect(scopeAllows(null, '')).toBe(true);
    expect(scopeAllows([], undefined)).toBe(true);
  });

  it('treats a scope of only blank entries as unrestricted', () => {
    // A stray empty string must not become a rule that matches nothing.
    expect(scopeAllows(['  '], 'anything')).toBe(true);
    expect(scopeAllows([''], 'anything')).toBe(true);
  });
});

describe('scopeAllows — exact entries', () => {
  it('admits the name it names', () => {
    expect(scopeAllows(['lending'], 'lending')).toBe(true);
  });

  it('denies a name it does not name', () => {
    expect(scopeAllows(['lending'], 'lending-x')).toBe(false);
    expect(scopeAllows(['lending'], 'lendin')).toBe(false);
  });

  it('ignores surrounding whitespace on an entry', () => {
    expect(scopeAllows(['  lending  '], 'lending')).toBe(true);
  });

  it('does not treat a literal entry as a regular expression', () => {
    expect(scopeAllows(['v1.0'], 'v1.0')).toBe(true);
    expect(scopeAllows(['v1.0'], 'v1x0')).toBe(false);
  });

  it('admits a name matched by any one of several entries', () => {
    expect(scopeAllows(['lending', 'tenant-a_*'], 'tenant-a_1')).toBe(true);
    expect(scopeAllows(['lending', 'tenant-a_*'], 'tenant-b_1')).toBe(false);
  });
});

describe('scopeAllows — glob entries', () => {
  it('expands * to any run of characters, including none', () => {
    expect(scopeAllows(['tenant-a_*'], 'tenant-a_prod')).toBe(true);
    expect(scopeAllows(['tenant-a_*'], 'tenant-a_')).toBe(true);
    expect(scopeAllows(['tenant-a_*'], 'tenant-b_prod')).toBe(false);
    // fnmatch's * is not path-aware; a separator is just another character.
    expect(scopeAllows(['a*'], 'a/b')).toBe(true);
  });

  it('expands ? to exactly one character', () => {
    expect(scopeAllows(['uc?-prod'], 'uc1-prod')).toBe(true);
    expect(scopeAllows(['uc?-prod'], 'uc12-prod')).toBe(false);
    expect(scopeAllows(['uc?-prod'], 'uc-prod')).toBe(false);
  });

  it('expands [seq] to a character class', () => {
    expect(scopeAllows(['usecaseA_v[12]'], 'usecaseA_v1')).toBe(true);
    expect(scopeAllows(['usecaseA_v[12]'], 'usecaseA_v2')).toBe(true);
    expect(scopeAllows(['usecaseA_v[12]'], 'usecaseA_v3')).toBe(false);
  });

  it('negates a class with [!seq], where a regex would use ^', () => {
    expect(scopeAllows(['log-[!0-9]*'], 'log-alpha')).toBe(true);
    expect(scopeAllows(['log-[!0-9]*'], 'log-1a')).toBe(false);
    // The class still requires one character to be there.
    expect(scopeAllows(['log-[!0-9]*'], 'log-')).toBe(false);
  });

  it('reads a leading ^ inside a class as a literal member, not a negation', () => {
    expect(scopeAllows(['p[^a]'], 'p^')).toBe(true);
    expect(scopeAllows(['p[^a]'], 'pa')).toBe(true);
    expect(scopeAllows(['p[^a]'], 'pb')).toBe(false);
  });

  it('reads a ] in first position inside a class as a literal member', () => {
    expect(scopeAllows(['x[]a]'], 'x]')).toBe(true);
    expect(scopeAllows(['x[]a]'], 'xa')).toBe(true);
    expect(scopeAllows(['x[]a]'], 'x[')).toBe(false);
  });

  it('treats an unterminated [ as a literal [', () => {
    expect(scopeAllows(['weird[name'], 'weird[name')).toBe(true);
    expect(scopeAllows(['weird[name'], 'weirdname')).toBe(false);
    expect(scopeAllows(['weird[name'], 'weird[other')).toBe(false);
  });

  it('matches case-sensitively, like fnmatchcase', () => {
    expect(scopeAllows(['lending'], 'Lending')).toBe(false);
    expect(scopeAllows(['tenant-a_*'], 'TENANT-A_prod')).toBe(false);
    expect(scopeAllows(['Tenant-A_*'], 'Tenant-A_prod')).toBe(true);
  });

  it('anchors at both ends rather than matching a substring', () => {
    expect(scopeAllows(['tenant-a_*'], 'x-tenant-a_prod')).toBe(false);
    expect(scopeAllows(['*_prod'], 'tenant_prod')).toBe(true);
    expect(scopeAllows(['*_prod'], 'tenant_prod-2')).toBe(false);
  });
});

describe('scopeAllows — a set scope fails closed on a nameless profile', () => {
  it('denies an empty or missing name even against a match-everything pattern', () => {
    expect(scopeAllows(['tenant-a_*'], '')).toBe(false);
    expect(scopeAllows(['tenant-a_*'], null)).toBe(false);
    expect(scopeAllows(['tenant-a_*'], undefined)).toBe(false);
    expect(scopeAllows(['*'], '')).toBe(false);
  });

  it('still admits a named profile under a match-everything pattern', () => {
    expect(scopeAllows(['*'], 'anything')).toBe(true);
  });
});
