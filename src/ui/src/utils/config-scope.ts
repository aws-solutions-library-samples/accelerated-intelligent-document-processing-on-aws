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
 * Matching semantics, identical to the Python:
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
 * Escape a character class's body for a JavaScript RegExp.
 *
 * `-` is deliberately left alone so that `[a-z]` stays a range, as it is in
 * fnmatch. `\`, `]` and `^` are escaped because they are literal members in
 * fnmatch but structural in a JS class — `^` only when leading, but escaping it
 * anywhere is harmless and avoids a position-dependent rule.
 */
const escapeClassBody = (body: string): string => body.replace(/[\\\]^]/g, '\\$&');

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
 * Translate an fnmatch pattern to an anchored, case-sensitive RegExp.
 *
 * `*` becomes any run of characters, `?` exactly one, `[seq]` / `[!seq]` a
 * character class (fnmatch negates with `!` where a regex uses `^`), and every
 * other character matches itself. An **unterminated** `[` is a literal `[`, as
 * fnmatch treats it.
 *
 * The `s` flag and `^…$` (without `m`, so `$` is end-of-input) together give
 * Python's `(?s:…)\Z`. The degenerate classes agree with fnmatch by
 * coincidence rather than by special case: an empty `[]` matches nothing in
 * both, and a negated empty `[^]` matches any character in both.
 */
const globToRegExp = (pattern: string): RegExp => {
  let out = '';
  let i = 0;
  while (i < pattern.length) {
    const char = pattern[i];
    i += 1;
    if (char === '*') {
      out += '.*';
    } else if (char === '?') {
      out += '.';
    } else if (char === '[') {
      const end = findClassEnd(pattern, i);
      if (end >= pattern.length) {
        out += '\\[';
      } else {
        const body = pattern.slice(i, end);
        i = end + 1;
        const negated = body.startsWith('!');
        out += `[${negated ? '^' : ''}${escapeClassBody(negated ? body.slice(1) : body)}]`;
      }
    } else {
      out += escapeLiteral(char);
    }
  }
  return new RegExp(`^(?:${out})$`, 's');
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
