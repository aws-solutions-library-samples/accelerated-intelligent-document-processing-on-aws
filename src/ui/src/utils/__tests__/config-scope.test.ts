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

describe('scopeAllows — character-class ranges fnmatch discards', () => {
  // fnmatch drops a range whose endpoints are out of order instead of raising,
  // and what is left of the class decides the match. A JavaScript RegExp rejects
  // the same range outright, so the discarding has to happen before the RegExp is
  // built. Each expectation below is what the server returns for that pair.

  it('never matches when the only range is out of order', () => {
    // `[z-a]` and `[9-0]` lose their one range and leave an empty class.
    expect(scopeAllows(['[z-a]'], 'a')).toBe(false);
    expect(scopeAllows(['[z-a]'], 'z')).toBe(false);
    expect(scopeAllows(['[z-a]'], 'b')).toBe(false);
    expect(scopeAllows(['[9-0]'], '0')).toBe(false);
    expect(scopeAllows(['[9-0]'], '9')).toBe(false);
    expect(scopeAllows(['[9-0]'], '5')).toBe(false);
  });

  it('still admits a profile whose name is the pattern verbatim', () => {
    // The equality branch runs first, so a profile really called `[z-a]` is in
    // scope even though the pattern it would compile to matches nothing.
    expect(scopeAllows(['[z-a]'], '[z-a]')).toBe(true);
    expect(scopeAllows(['[9-0]'], '[9-0]')).toBe(true);
    expect(scopeAllows(['[a-\\]'], '[a-\\]')).toBe(true);
  });

  it('keeps the members a discarded range leaves behind', () => {
    // `[a--z]` is the range `a`–`-`, which is out of order, followed by `z`.
    // Dropping the range leaves a one-member class, so only `z` matches.
    expect(scopeAllows(['[a--z]'], 'z')).toBe(true);
    expect(scopeAllows(['[a--z]'], 'a')).toBe(false);
    expect(scopeAllows(['[a--z]'], 'b')).toBe(false);
    expect(scopeAllows(['[a--z]'], '-')).toBe(false);
    // `[z-a-?]` loses `z`–`a` and keeps `-` and `?` as literal members.
    expect(scopeAllows(['[z-a-?]'], '-')).toBe(true);
    expect(scopeAllows(['[z-a-?]'], '?')).toBe(true);
    expect(scopeAllows(['[z-a-?]'], 'a')).toBe(false);
  });

  it('never matches when a trailing backslash is the range end', () => {
    // `[a-\]` is the range `a`–`\`, out of order, with nothing left over.
    expect(scopeAllows(['[a-\\]'], 'a')).toBe(false);
    expect(scopeAllows(['[a-\\]'], '\\')).toBe(false);
    expect(scopeAllows(['[a-\\]'], '-')).toBe(false);
    expect(scopeAllows(['[a-\\]'], 'z')).toBe(false);
  });

  it('fails only the entry holding the bad class, not the whole check', () => {
    // A discarded range inside a longer pattern leaves that pattern unable to
    // match, and a sibling entry is still evaluated.
    expect(scopeAllows(['tenant-[b-a]_*'], 'tenant-a_x')).toBe(false);
    expect(scopeAllows(['tenant-[b-a]_*'], 'tenant-b_x')).toBe(false);
    expect(scopeAllows(['tenant-[b-a]_*'], 'tenant-_x')).toBe(false);
    expect(scopeAllows(['*[z-a]*'], 'anything')).toBe(false);
    expect(scopeAllows(['*[z-a]*'], 'x')).toBe(false);
    expect(scopeAllows(['tenant-[b-a]_*', 'lending'], 'lending')).toBe(true);
  });

  it('matches any single character when a negated class loses its only range', () => {
    // An emptied *negated* class is "none of nothing", which is every character.
    expect(scopeAllows(['[!z-a]'], 'a')).toBe(true);
    expect(scopeAllows(['[!z-a]'], 'z')).toBe(true);
    expect(scopeAllows(['[!z-a]'], '-')).toBe(true);
    // It is still exactly one character.
    expect(scopeAllows(['[!z-a]'], 'ab')).toBe(false);
    expect(scopeAllows(['[!z-a]'], '')).toBe(false);
    expect(scopeAllows(['[!a-Z]'], 'q')).toBe(true);
    expect(scopeAllows(['[!a-Z]'], 'qq')).toBe(false);
  });

  it('keeps a well-ordered range whose endpoints need escaping', () => {
    // `[\-a]` is the range `\`–`a`, which is in order and covers `]`, `^` and
    // `_`. Escaping the endpoints must not turn it into three literal members.
    expect(scopeAllows(['[\\-a]'], '\\')).toBe(true);
    expect(scopeAllows(['[\\-a]'], 'a')).toBe(true);
    expect(scopeAllows(['[\\-a]'], '^')).toBe(true);
    expect(scopeAllows(['[\\-a]'], '-')).toBe(false);
    // A hyphen at either edge of a class is a literal member, not a range.
    expect(scopeAllows(['[a-]'], 'a')).toBe(true);
    expect(scopeAllows(['[a-]'], '-')).toBe(true);
    expect(scopeAllows(['[a-]'], 'b')).toBe(false);
    expect(scopeAllows(['[a-b-c]'], 'b')).toBe(true);
    expect(scopeAllows(['[a-b-c]'], '-')).toBe(true);
    expect(scopeAllows(['[a-b-c]'], 'c')).toBe(true);
    expect(scopeAllows(['[a-b-c]'], 'd')).toBe(false);
  });
});

describe('scopeAllows — runs of *', () => {
  it('treats a run of * as a single *', () => {
    expect(scopeAllows(['a**b'], 'ab')).toBe(true);
    expect(scopeAllows(['a**b'], 'axxb')).toBe(true);
    expect(scopeAllows(['a**b'], 'ax')).toBe(false);
    expect(scopeAllows(['a**b'], 'b')).toBe(false);
    expect(scopeAllows(['**'], 'anything')).toBe(true);
    expect(scopeAllows(['a***'], 'a')).toBe(true);
    expect(scopeAllows(['a***'], 'abc')).toBe(true);
  });

  it('answers a long run of * against a non-matching name promptly', () => {
    // A run of n stars compiled to n separate `.*` costs time exponential in n
    // before it can report a non-match, which on this path is a render. The
    // budget is loose on purpose: it is here to catch a return to one `.*` per
    // star, not to measure the machine.
    const name = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaac';
    const started = performance.now();
    expect(scopeAllows([`a${'*'.repeat(24)}b`], name)).toBe(false);
    expect(performance.now() - started).toBeLessThan(1000);
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
