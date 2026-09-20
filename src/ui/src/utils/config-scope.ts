// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Client-side mirror of `scope_allows` in
 * `lib/idp_common_pkg/idp_common/config_scope.py`.
 *
 * A non-admin user may be restricted to a set of Configuration Profiles through
 * their `allowedConfigVersions`. An entry may be an exact profile name
 * (`lending`) or a glob pattern (`lending-*`, `uc?-prod`, `usecaseA_v[12]`);
 * patterns exist because deployments that predate revision history encode
 * lineage in the profile *name*, so scoping a user to a use case otherwise means
 * re-granting on every iteration.
 *
 * ⚠️ **The server is the enforcement point, not this module.** Every resolver
 * re-checks the caller's scope with the Python `scope_allows`, so nothing here
 * can grant access. What this decides is only what the UI *offers* — which
 * profiles appear in a picklist, which ones a form will let you pick. That still
 * matters: a client that cannot read a pattern shows a caller scoped to
 * `tenant-a_*` an empty Configuration-Profile dropdown, and an empty dropdown
 * leaves them unable to reprocess a document at all even though the server would
 * allow every profile the pattern covers.
 *
 * The two implementations must therefore stay in step: they are not redundant
 * copies of one check, they are two halves of one feature, and a client that is
 * stricter than the server makes a server-side capability unreachable while a
 * client that is looser only produces a request the server refuses. When the
 * Python matcher changes, change this one in the same commit — and see
 * `__tests__/config-scope.test.ts`, whose cases are the Python suite's cases.
 *
 * Matching semantics:
 *
 * - An **empty or unset** scope is unrestricted — it returns `true`. Scoping is
 *   opt-in per user and most users have none.
 * - A **set** scope denies an empty or missing profile name. An unnamed object
 *   cannot be proven in scope, and on the server "cannot prove" must not mean
 *   "allow"; this side agrees so that the UI never offers something the server
 *   will then refuse.
 * - Entries are trimmed and blank entries dropped, so a stray empty string does
 *   not become a rule that matches nothing.
 * - An entry matches when it **equals** the name, or when it contains `*`, `?`
 *   or `[` and matches the name as a glob with Python
 *   `fnmatch.fnmatchcase` semantics: case-**sensitive**, anchored at both ends.
 *
 * **What is identical and what is not.** The *value* returned agrees with the
 * Python for every input the two have been compared on except one class of
 * pattern, named at the end of this paragraph. Two differential harnesses have
 * been run. The first is 351,540 pattern/name pairs over the characters that are
 * structural to a class or to a regex (`] ! - ^ \ [ ? * & | ~`): every character
 * class up to three members, bare and embedded in a longer pattern, `*` runs and
 * interleavings, and names holding backslashes, brackets and embedded newlines.
 * It contains **no character outside the Basic Multilingual Plane**, so it says
 * nothing about those. The second, 4,837,316 pairs, adds them — astral literals
 * in both pattern and name, `?` and `*` against them, and them as class members
 * and as class *range endpoints* — together with lone surrogates and BMP
 * characters above the surrogate range. 1,113 of those pairs disagree (474 where
 * this side admits a name the Python refuses, 639 the other way), and every one
 * of them is **a character class with a range endpoint outside the BMP**; see the
 * note in `translateClass` for why and for what each direction costs. Nothing
 * else in the second harness diverges, which is what the `u` flag on the compiled
 * RegExp buys: without it the pattern and the name are matched by UTF-16 code
 * unit, so `.` matches half a surrogate pair and `?` admits a name the Python
 * refuses. Neither harness is committed — each is a throwaway run against the
 * Python of the day. What runs on every change is
 * `__tests__/config-scope.test.ts`, which carries a sample of each harness's
 * cases, including the divergence above. The **worst-case running time** does not
 * agree, and cannot: CPython 3.12 and later wrap each interior `*` in an atomic
 * group (`(?>.*?…)`), a construct JavaScript has no equivalent of, so a pattern
 * built to force backtracking (`a*` repeated a dozen times before a letter the
 * name lacks) answers here in seconds where the server answers in a fraction of
 * a millisecond. Runs of consecutive `*` are collapsed to one, which removes the
 * cheapest way to trigger that — a 24-`*` run against a name it cannot match
 * goes from about a minute to under a millisecond — but `*` interleaved with
 * literals remains exposed.
 *
 * That residual is bounded by who can reach it. A scope entry is written only by
 * `createUser` / `updateUser`, both **Admin-only** in the API RBAC manifest, and
 * the Admin-facing UI that calls them offers a fixed list of existing profile
 * names rather than a free-text field — so a glob, let alone an adversarial one,
 * arrives only from a direct API or CLI call. It costs an Admin their own
 * browser tab, not anyone else's.
 */

/** Characters that make a scope entry a glob rather than a literal name. */
const GLOB_CHARS = ['*', '?', '['];

/**
 * A caller's `allowedConfigVersions` as the UI may hold it.
 *
 * `useUserRole` supplies `string[] | null`, but the GraphQL types model the
 * field as a nullable list of nullable strings, and the Python `normalize_scope`
 * accepts a bare string as a one-entry scope. Accepting all of those here keeps
 * the coercion in one place instead of at each call site.
 */
export type ConfigVersionScope = string | readonly (string | null | undefined)[] | null | undefined;

/**
 * Coerce a raw scope into a list of usable entries, or `null` for unrestricted.
 *
 * Mirrors `normalize_scope`, so callers can use a single `if (!entries)` test for
 * "unrestricted".
 */
const normalizeScope = (scope: ConfigVersionScope): string[] | null => {
  if (!scope) return null;
  const raw: readonly (string | null | undefined)[] = typeof scope === 'string' ? [scope] : scope;
  const entries = raw.map((entry) => (entry ?? '').trim()).filter((entry) => entry.length > 0);
  return entries.length > 0 ? entries : null;
};

/** True when a scope entry is a glob rather than a literal profile name. */
const isPattern = (entry: string): boolean => GLOB_CHARS.some((char) => entry.includes(char));

/** Escape a character that must match itself in the generated RegExp. */
const escapeLiteral = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * Escape one member of a character class for a JavaScript RegExp.
 *
 * `\`, `]`, `^` and `[` are literal members in fnmatch but structural in a JS
 * class. `-` is escaped too, which is why this is applied to each member *run*
 * rather than to the whole class body: the hyphens that join the runs back
 * together are the ones that form ranges, and they must stay bare. `^` and `[`
 * only need escaping in particular positions, but escaping them anywhere is
 * harmless and avoids a position-dependent rule.
 *
 * `&`, `~` and `|` are deliberately not escaped. Python's `re` escapes them
 * against a future set-operation syntax; in a JavaScript class without the `v`
 * flag they are ordinary literals, and this module builds no `v`-flag patterns.
 */
const escapeClassMember = (text: string): string => text.replace(/[-\\\]^[]/g, '\\$&');

/**
 * A RegExp that matches nothing, for a class fnmatch reduces to the empty set.
 */
const NEVER_MATCHES = /(?!)/;

/**
 * The index of the `]` that closes a character class, or `pattern.length` when
 * the class is unterminated.
 *
 * `start` is the index just after the opening `[`. Two fnmatch quirks are
 * reproduced here: a leading `!` negates rather than being a member, and a `]`
 * in first position (`[]abc]`, `[!]abc]`) is a literal member rather than the
 * terminator.
 */
const findClassEnd = (pattern: string, start: number): number => {
  let j = start;
  if (pattern[j] === '!') j += 1;
  if (pattern[j] === ']') j += 1;
  while (j < pattern.length && pattern[j] !== ']') j += 1;
  return j;
};

/**
 * Translate one fnmatch character class into a RegExp fragment.
 *
 * `start` is the index just after the `[`, `end` the index of the closing `]`.
 *
 * The work here is the **ranges**, and it is the reason this is not a matter of
 * escaping the body and handing it to `RegExp`. fnmatch accepts a range whose
 * endpoints are out of order (`[z-a]`, `[a-\]`, and `[a--z]`, whose first range
 * is `a` to `-`) by *discarding* the range and keeping whatever members remain
 * around it; a JavaScript RegExp rejects the same source with a `SyntaxError`.
 * So the class body is split into the runs between range hyphens, an out-of-order
 * pair is collapsed exactly as fnmatch collapses it, and only then is a class
 * emitted. Three outcomes follow:
 *
 * - Members remain — emit them, e.g. `[a--z]` keeps only `z`.
 * - Nothing remains and the class was not negated — it matches no character, so
 *   emit a never-matching fragment. `[z-a]` and `[a-\]` land here, and so the
 *   *entry* matches nothing while its siblings are still evaluated.
 * - Nothing remains and the class **was** negated (`[!z-a]`) — "none of nothing"
 *   is every character, so emit `.`.
 *
 * This ordering test is the one thing that still differs from the Python. It
 * compares UTF-16 code units here and code points there, so for a range with an
 * endpoint outside the Basic Multilingual Plane the two sides can disagree about
 * whether the range is out of order — and the collapse then takes a code *unit*
 * off each side where the Python takes a code point. It breaks two ways:
 *
 * - `[😀-😃]` is a well-ordered range of four code points to the Python. Here its
 *   endpoints compare as the low surrogate `\uDE00` against the high surrogate
 *   `\uD83D`, so it is read as out of order and collapsed to the single member
 *   `😃` — **stricter** than the server, which is the direction that hides a
 *   profile the server would serve.
 * - `[😀-\uFFFF]` is out of order to the Python (U+1F600 above U+FFFF), which
 *   discards it. Here the endpoints compare as `\uDE00` against `\uFFFF` and the
 *   range is kept, and `RegExp` then rejects it under the `u` flag. The
 *   `try`/`catch` in `globToRegExp` turns that into a never-matching entry, which
 *   is the same answer the Python reaches for a positive class — both match
 *   nothing — but stricter for a negated one (`[!😀-\uFFFF]`), where the Python's
 *   emptied negated class matches any single character.
 *
 * Reaching either needs an admin to write a glob whose range endpoint is an
 * astral character, which the Admin UI's fixed profile list cannot produce.
 */
const translateClass = (pattern: string, start: number, end: number): string => {
  const body = pattern.slice(start, end);
  let members: string[];
  if (!body.includes('-')) {
    members = [body];
  } else {
    // Split on the hyphens that separate ranges. The `+ 3` skip is what keeps a
    // hyphen that follows a completed range (`[a-b-c]`) a literal member rather
    // than reading it as the start of another range.
    members = [];
    let from = start;
    let cursor = pattern[start] === '!' ? start + 2 : start + 1;
    for (;;) {
      const hyphen = pattern.indexOf('-', cursor);
      if (hyphen < 0 || hyphen >= end) break;
      members.push(pattern.slice(from, hyphen));
      from = hyphen + 1;
      cursor = hyphen + 3;
    }
    const tail = pattern.slice(from, end);
    if (tail) {
      members.push(tail);
    } else {
      // A hyphen at the very end of the body is a literal member.
      members[members.length - 1] += '-';
    }
    // Discard out-of-order ranges, from the right so a collapse can cascade.
    for (let m = members.length - 1; m > 0; m -= 1) {
      const left = members[m - 1];
      const right = members[m];
      if (left.slice(-1) > right.slice(0, 1)) {
        members[m - 1] = left.slice(0, -1) + right.slice(1);
        members.splice(m, 1);
      }
    }
  }
  // fnmatch negates with `!`, so the marker rides along in the first run.
  const negated = members.length > 0 && members[0].startsWith('!');
  if (negated) members[0] = members[0].slice(1);
  const emitted = members.map(escapeClassMember).join('-');
  if (!emitted) return negated ? '.' : '(?!)';
  return `[${negated ? '^' : ''}${emitted}]`;
};

/** Sentinel standing for a run of one or more `*`, mirroring fnmatch's own. */
const STAR = Symbol('star');

/**
 * Translate an fnmatch pattern to an anchored, case-sensitive RegExp.
 *
 * `*` becomes any run of characters, `?` exactly one, `[seq]` / `[!seq]` a
 * character class, and every other character matches itself. An
 * **unterminated** `[` is a literal `[`, as fnmatch treats it — which is what
 * `[]` is: the `]` is taken as a first-position member, the scan then runs off
 * the end with no terminator, and both sides emit a literal two-character `[]`.
 * `[^]` is not a negated empty class either but a one-member class holding `^`,
 * because fnmatch negates with `!` and so reads a leading `^` as an ordinary
 * member. A class that genuinely matches nothing, or genuinely matches any
 * character, comes from a discarded range instead — see `translateClass`.
 *
 * Consecutive `*` collapse into one, as they do in fnmatch. Each surviving `*`
 * becomes a plain `.*`; the atomic group CPython 3.12 and later use has no
 * JavaScript equivalent, so the guarantee this loses is worst-case time, not the
 * set of names matched. See the note in the module header.
 *
 * The `s` flag and `^…$` (without `m`, so `$` is end-of-input) together give
 * Python's `(?s:…)\Z`.
 *
 * The `u` flag is what makes `.`, a class member and a literal each one **code
 * point** rather than one UTF-16 code unit, which is the unit Python matches in.
 * Without it `?` matches half a surrogate pair, so `tenant-??_x` admits
 * `tenant-😀_x` where the server refuses it, and `[😀]` becomes a class of two
 * surrogates that matches either half alone. `u` is also **stricter about
 * escapes** than the default: outside a class it accepts an escape only of a
 * syntax character (`^ $ \ . * + ? ( ) [ ] { } |`) or `/`, and inside a class
 * those plus `\-` and `\b`. Everything `escapeLiteral` and `escapeClassMember`
 * emit is within that set, so neither needed loosening — measured over the
 * 4,837,316-pair harness, no pattern's escaping fails to compile.
 */
const globToRegExp = (pattern: string): RegExp => {
  const fragments: (string | typeof STAR)[] = [];
  let i = 0;
  while (i < pattern.length) {
    const char = pattern[i];
    i += 1;
    if (char === '*') {
      if (fragments[fragments.length - 1] !== STAR) fragments.push(STAR);
    } else if (char === '?') {
      fragments.push('.');
    } else if (char === '[') {
      const end = findClassEnd(pattern, i);
      if (end >= pattern.length) {
        fragments.push('\\[');
      } else {
        fragments.push(translateClass(pattern, i, end));
        i = end + 1;
      }
    } else {
      fragments.push(escapeLiteral(char));
    }
  }
  const source = fragments.map((fragment) => (fragment === STAR ? '.*' : fragment)).join('');
  try {
    return new RegExp(`^(?:${source})$`, 'su');
  } catch {
    // A safety net, not the mechanism: the range handling above covers the
    // classes fnmatch accepts and `RegExp` rejects, bar one shape it cannot see —
    // a class range whose left endpoint is an astral code point and whose right
    // endpoint is a BMP one at or above U+DC00, which looks well-ordered to a
    // code-unit comparison and is rejected by a `u`-flag `RegExp` (see
    // `translateClass`). Over the 4,837,316-pair harness that shape is the only
    // thing that reaches this arm: 20 of 9,812 patterns, and never an escaping
    // fault. Whatever lands here must leave its own entry unable to match rather
    // than throw out of the render that called this.
    return NEVER_MATCHES;
  }
};

/**
 * Whether a scope permits a Configuration Profile.
 *
 * @param scope The caller's `allowedConfigVersions`, or null/empty for
 *   unrestricted.
 * @param profileName The profile name to test. An empty or missing name is
 *   **denied** whenever a scope is set.
 */
export const scopeAllows = (scope: ConfigVersionScope, profileName: string | null | undefined): boolean => {
  const entries = normalizeScope(scope);
  if (!entries) return true;
  if (!profileName) return false;
  return entries.some((entry) => entry === profileName || (isPattern(entry) && globToRegExp(entry).test(profileName)));
};
