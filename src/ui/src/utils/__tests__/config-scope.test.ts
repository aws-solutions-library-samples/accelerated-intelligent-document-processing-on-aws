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
 *
 * The one exception is the last `describe` block, which pins the single shape of
 * input where the two do **not** agree — a character class whose body holds a
 * range hyphen and a code point outside the Basic Multilingual Plane — and states
 * the server's answer for each case in a comment rather than in the assertion.
 * Its purpose is the opposite of the rest: to make a change in that behaviour
 * visible instead of silent. That shape disagrees in **both** directions, so the
 * block carries a case of each.
 *
 * Code points that are invisible or ambiguous on the page are written as `\u{...}`
 * escapes rather than as literal characters, because which one a case is about is
 * the whole content of the case.
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

describe('scopeAllows — entries are stripped as Python strip() strips', () => {
  // `String.prototype.trim` and Python's `str.strip()` are different functions
  // over different sets: swept over the whole code-point range, strip() removes 29
  // code points and trim() removes 25, and they share 24. Stripping decides which
  // entries survive as well as what each survivor matches, so each expectation
  // below is the server's answer for that pair.
  const BOM = '\u{FEFF}'; // trim() removes this one; strip() keeps it
  const FS = '\u{001C}'; // strip() removes these five; trim() keeps them
  const GS = '\u{001D}';
  const RS = '\u{001E}';
  const US = '\u{001F}';
  const NEL = '\u{0085}';

  it('keeps a byte-order mark, which trim() would remove', () => {
    // The server compares the mark-prefixed entry, so `lending` is out of scope.
    expect(scopeAllows([`${BOM}lending`], 'lending')).toBe(false);
    expect(scopeAllows([`lending${BOM}`], 'lending')).toBe(false);
    expect(scopeAllows([`${BOM}lending${BOM}`], 'lending')).toBe(false);
    expect(scopeAllows([`${BOM}lending`], `${BOM}lending`)).toBe(true);
    expect(scopeAllows([`${BOM}tenant-a_*`], 'tenant-a_prod')).toBe(false);
  });

  it('counts an entry of nothing but a byte-order mark as a real entry', () => {
    // Dropping it as blank would leave the scope empty, and an empty scope is
    // unrestricted — so the caller would be offered every profile, not none.
    expect(scopeAllows([BOM], 'lending')).toBe(false);
    expect(scopeAllows([BOM], 'anything')).toBe(false);
    expect(scopeAllows([BOM], BOM)).toBe(true);
    expect(scopeAllows([BOM, 'lending'], 'lending')).toBe(true);
  });

  it('strips the separators and NEL that trim() would keep', () => {
    expect(scopeAllows([`${FS}lending`], 'lending')).toBe(true);
    expect(scopeAllows([`${GS}lending`], 'lending')).toBe(true);
    expect(scopeAllows([`${RS}lending`], 'lending')).toBe(true);
    expect(scopeAllows([`${US}lending`], 'lending')).toBe(true);
    expect(scopeAllows([`${NEL}lending`], 'lending')).toBe(true);
    expect(scopeAllows([`lending${NEL}`], 'lending')).toBe(true);
    expect(scopeAllows([`${NEL}lending${NEL}`], 'lending')).toBe(true);
    expect(scopeAllows([`${FS}tenant-a_*`], 'tenant-a_prod')).toBe(true);
  });

  it('treats an entry of only those separators as blank, so unrestricted', () => {
    expect(scopeAllows([FS], 'anything')).toBe(true);
    expect(scopeAllows([NEL], 'anything')).toBe(true);
    expect(scopeAllows([`${FS}${GS}${RS}${US}${NEL}`], 'anything')).toBe(true);
  });

  it('strips the whitespace the two sets share', () => {
    expect(scopeAllows(['\u{00A0}lending'], 'lending')).toBe(true); // no-break space
    expect(scopeAllows(['\u{2028}lending'], 'lending')).toBe(true); // line separator
    expect(scopeAllows(['\u{3000}lending'], 'lending')).toBe(true); // ideographic space
  });

  it('keeps the code points neither set removes', () => {
    // U+200B is a zero-width space and U+180E a Mongolian vowel separator; neither
    // is whitespace to either implementation, so both stay part of the entry.
    expect(scopeAllows(['\u{200B}lending'], 'lending')).toBe(false);
    expect(scopeAllows(['\u{200B}lending'], '\u{200B}lending')).toBe(true);
    expect(scopeAllows(['\u{180E}lending'], 'lending')).toBe(false);
    expect(scopeAllows(['\u{180E}lending'], '\u{180E}lending')).toBe(true);
  });

  it('never strips the profile name, because scope_allows does not', () => {
    expect(scopeAllows(['lending'], ' lending')).toBe(false);
    expect(scopeAllows(['lending'], 'lending ')).toBe(false);
    expect(scopeAllows(['lending'], `${BOM}lending`)).toBe(false);
    expect(scopeAllows(['lending'], `${FS}lending`)).toBe(false);
    // A name of nothing but whitespace is still a name, so `*` admits it.
    expect(scopeAllows(['*'], ' ')).toBe(true);
    expect(scopeAllows(['*'], BOM)).toBe(true);
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

describe('scopeAllows — characters outside the Basic Multilingual Plane', () => {
  // A profile name may hold an astral character, and an astral character is two
  // UTF-16 code units in a JavaScript string but one character to Python's
  // fnmatch. The `u` flag on the compiled RegExp is what reconciles the two; each
  // expectation below is what the server returns for that pair.
  const GRIN = '\u{1F600}'; // U+1F600, one code point, two code units
  const SMILE = '\u{1F603}'; // U+1F603
  const LONE_HIGH = '\uD83D'; // the high half of U+1F600, on its own

  it('counts an astral character as one ? and not two', () => {
    expect(scopeAllows(['tenant-?_x'], `tenant-${GRIN}_x`)).toBe(true);
    expect(scopeAllows(['tenant-??_x'], `tenant-${GRIN}_x`)).toBe(false);
    expect(scopeAllows(['tenant-??_x'], `tenant-${GRIN}${GRIN}_x`)).toBe(true);
    expect(scopeAllows(['tenant-?_x'], `tenant-${GRIN}${GRIN}_x`)).toBe(false);
    expect(scopeAllows(['?'], GRIN)).toBe(true);
    expect(scopeAllows(['??'], GRIN)).toBe(false);
    // A lone surrogate is a code point of its own, so it is also one `?`.
    expect(scopeAllows(['?'], LONE_HIGH)).toBe(true);
  });

  it('matches an astral character as a literal in a glob entry', () => {
    expect(scopeAllows([`tenant-${GRIN}_*`], `tenant-${GRIN}_prod`)).toBe(true);
    expect(scopeAllows([`tenant-${GRIN}_*`], `tenant-${GRIN}_`)).toBe(true);
    expect(scopeAllows([`tenant-${GRIN}_*`], `tenant-${SMILE}_prod`)).toBe(false);
    // Half of the pair must not match on its own.
    expect(scopeAllows([`tenant-${GRIN}_*`], `tenant-${LONE_HIGH}_prod`)).toBe(false);
  });

  it('spans astral characters with *', () => {
    expect(scopeAllows(['a*b'], `a${GRIN}b`)).toBe(true);
    expect(scopeAllows(['a*b'], `a${GRIN}${SMILE}b`)).toBe(true);
    expect(scopeAllows([`*${GRIN}*`], `x${GRIN}y`)).toBe(true);
    expect(scopeAllows([`*${GRIN}*`], `x${SMILE}y`)).toBe(false);
  });

  it('treats an astral character in a class as one member', () => {
    expect(scopeAllows([`v[${GRIN}${SMILE}]`], `v${GRIN}`)).toBe(true);
    expect(scopeAllows([`v[${GRIN}${SMILE}]`], `v${SMILE}`)).toBe(true);
    expect(scopeAllows([`v[${GRIN}${SMILE}]`], `v\u{1F602}`)).toBe(false);
    // Neither half of the pair is a member of the class on its own.
    expect(scopeAllows([`v[${GRIN}${SMILE}]`], `v${LONE_HIGH}`)).toBe(false);
    expect(scopeAllows([`[a${GRIN}]`], GRIN)).toBe(true);
    expect(scopeAllows([`[a${GRIN}]`], LONE_HIGH)).toBe(false);
  });

  it('excludes exactly the astral member a negated class names', () => {
    expect(scopeAllows([`v[!${GRIN}]`], `v${GRIN}`)).toBe(false);
    expect(scopeAllows([`v[!${GRIN}]`], `v${SMILE}`)).toBe(true);
    expect(scopeAllows([`v[!${GRIN}]`], 'va')).toBe(true);
  });
});

describe('scopeAllows — where an astral code point in a class range diverges from the server', () => {
  // The only shape of input the two implementations answer differently: a class
  // body holding a range hyphen and a code point outside the BMP. Two pieces of
  // `translateClass` index by UTF-16 code unit where CPython's `fnmatch._translate`
  // indexes by code point — the endpoint ordering test, and the arithmetic that
  // splits the class body into runs around range hyphens — and each can be reached
  // on its own. The divergence runs **stricter** than the server in most cases and
  // **looser** in some; both directions appear below, and each comment says what
  // the server returns.

  it('keeps only the last code point of a well-ordered astral range', () => {
    // The server reads `[U+1F600-U+1F603]` as those four code points and returns
    // true for each. Here the range collapses to its last member.
    expect(scopeAllows(['[\u{1F600}-\u{1F603}]'], '\u{1F603}')).toBe(true);
    expect(scopeAllows(['[\u{1F600}-\u{1F603}]'], '\u{1F600}')).toBe(false);
    expect(scopeAllows(['[\u{1F600}-\u{1F603}]'], '\u{1F601}')).toBe(false);
  });

  it('matches nothing when a u-flag RegExp rejects an astral range', () => {
    // `[U+1F600-U+FFFF]` looks well-ordered compared by code unit, so it survives
    // to `RegExp`, which rejects it; the entry is then unable to match. The server
    // discards the range instead, which for a positive class is the same answer…
    expect(scopeAllows(['[\u{1F600}-\u{FFFF}]'], 'a')).toBe(false);
    expect(scopeAllows(['[\u{1F600}-\u{FFFF}]'], '\u{FFFF}')).toBe(false);
    // …and for a negated one is not: an emptied negated class matches any single
    // character on the server, so it returns true for both of these.
    expect(scopeAllows(['[!\u{1F600}-\u{FFFF}]'], 'a')).toBe(false);
    expect(scopeAllows(['[!\u{1F600}-\u{FFFF}]'], '\u{1F600}')).toBe(false);
  });

  it('splits the class body at a hyphen the server keeps as a literal member', () => {
    // The run-split arithmetic, not the ordering test. The server partitions
    // `[a-U+1F600-U+1F603]` into `a` | `U+1F600-U+1F603` and emits the range
    // `a`–`U+1F600` plus the literal members `-` and `U+1F603`; here the body
    // partitions three ways and the class becomes the single range
    // `a`–`U+1F603`. That is stricter in one place and looser in another.
    const both = '[a-\u{1F600}-\u{1F603}]';
    // Agrees: inside `a`–`U+1F600` on the server, inside `a`–`U+1F603` here.
    expect(scopeAllows([both], 'b')).toBe(true);
    expect(scopeAllows([both], '\u{1F600}')).toBe(true);
    // Agrees: the last member either way.
    expect(scopeAllows([both], '\u{1F603}')).toBe(true);
    // Agrees: below `a`, so outside the range on both sides.
    expect(scopeAllows([both], 'Z')).toBe(false);
    // Stricter: the server keeps the second hyphen as a literal member and
    // returns true.
    expect(scopeAllows([both], '-')).toBe(false);
    // Looser: U+1F601 and U+1F602 are inside `a`–`U+1F603` here but above the
    // server's `a`–`U+1F600`, so the server returns false for both.
    expect(scopeAllows([both], '\u{1F601}')).toBe(true);
    expect(scopeAllows([both], '\u{1F602}')).toBe(true);
  });

  it('collapses a split-off astral member against a BMP neighbour', () => {
    // Same arithmetic, this time leaving a lone high surrogate as the range end.
    // The server emits `a`–`U+1F600`, `-`, `c` and returns true for all four of
    // these; here the class is `a`–`\uD83D`.
    expect(scopeAllows(['[a-\u{1F600}-c]'], 'b')).toBe(true);
    expect(scopeAllows(['[a-\u{1F600}-c]'], 'c')).toBe(true);
    expect(scopeAllows(['[a-\u{1F600}-c]'], '\u{1F600}')).toBe(false);
    expect(scopeAllows(['[a-\u{1F600}-c]'], '-')).toBe(false);
    // `[0-U+1F600-c]` is the same shape with a digit as the left endpoint: the
    // server returns true for `-`, this side does not.
    expect(scopeAllows(['[0-\u{1F600}-c]'], '5')).toBe(true);
    expect(scopeAllows(['[0-\u{1F600}-c]'], '-')).toBe(false);
  });

  it('still admits a profile whose name is such an entry verbatim', () => {
    // The equality branch runs before any of this, so a profile really called
    // `[U+1F600-U+1F603]` is in scope — the same guarantee the discarded-range
    // cases above rely on.
    expect(scopeAllows(['[\u{1F600}-\u{1F603}]'], '[\u{1F600}-\u{1F603}]')).toBe(true);
    expect(scopeAllows(['[!\u{1F600}-\u{FFFF}]'], '[!\u{1F600}-\u{FFFF}]')).toBe(true);
    expect(scopeAllows(['[a-\u{1F600}-\u{1F603}]'], '[a-\u{1F600}-\u{1F603}]')).toBe(true);
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
