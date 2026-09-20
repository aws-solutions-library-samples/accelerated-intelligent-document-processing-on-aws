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
 * - Entries are stripped — by Python's `str.strip()` set of code points, not
 *   `String.prototype.trim`'s, which is a different set; see
 *   `PYTHON_WHITESPACE` — and blank entries dropped, so a stray empty string does
 *   not become a rule that matches nothing. The profile *name* is not stripped,
 *   because `scope_allows` does not strip it either.
 * - An entry matches when it **equals** the name, or when it contains `*`, `?`
 *   or `[` and matches the name as a glob with Python
 *   `fnmatch.fnmatchcase` semantics: case-**sensitive**, anchored at both ends.
 *
 * **What is identical and what is not.** The *value* returned agrees with the
 * Python except on one named shape of pattern, and within that shape it can
 * disagree in either direction. The shape is **a character class whose body holds
 * a range hyphen and at least one code point outside the Basic Multilingual
 * Plane**; the note in `translateClass` names the two pieces of arithmetic that
 * cause it and what each direction costs. The shape is a sufficient condition for
 * a disagreement to be possible, not for one to occur: plenty of patterns fit it
 * and agree.
 *
 * **State this as a shape, not as a count.** An absolute number of disagreeing
 * pairs measures how much of a harness's corpus carries the shape — a parameter of
 * the harness, not a property of this code — so enumerating more astral range
 * endpoints raises it with no code change. What holds for any corpus is that
 * **every** disagreement fits the shape above, **none** fits anything else, and
 * over patterns free of the shape there are **zero** disagreements. Checking that
 * means a differential run against the Python of the day; the one behind this
 * paragraph compared 43,398,144 pattern/name pairs — 18,836 patterns × 2,304
 * names — in three parts:
 *
 * - 15,921 patterns over the characters that are structural to a class or to a
 *   regex (`] ! - ^ \ [ ? * & | ~`): every character class up to three members,
 *   bare and embedded in a longer pattern, `*` runs and interleavings, against
 *   names holding backslashes, brackets and embedded newlines. This part contains
 *   **no character outside the BMP**, so on its own it says nothing about those.
 * - 2,439 patterns that add them: astral literals in both pattern and name, `?`
 *   and `*` against them, and them as class members and as class *range
 *   endpoints*, together with lone surrogates and BMP characters above the
 *   surrogate range.
 * - 476 patterns carrying every code point that either `str.strip()` or
 *   `String.prototype.trim` removes — and several that neither removes — in
 *   leading, trailing and sole position, which is the `normalizeScope` half of the
 *   mirror rather than the matcher half.
 *
 * 519 of the 18,836 patterns disagree on at least one name, and all 519 fit the
 * shape; the 17,936 that do not fit it contribute 41,324,544 pairs and **0**
 * disagreements. That the astral part diverges *only* in the shape is what the `u`
 * flag on the compiled RegExp buys: without it the pattern and the name are
 * matched by UTF-16 code unit, so `.` matches half a surrogate pair and `?` admits
 * a name the Python refuses.
 *
 * The harness is not committed — it is a throwaway run against the Python of the
 * day, and re-running it is how a change to either side is checked. What runs on
 * every change is `__tests__/config-scope.test.ts`, which carries a sample of each
 * part's cases, including the divergence above and both of its directions.
 *
 * The **worst-case running time** does not agree, and cannot: CPython 3.12 and
 * later wrap each interior `*` in an atomic group (`(?>.*?…)`), a construct
 * JavaScript has no equivalent of, so a pattern built to force backtracking (`a*`
 * repeated a dozen times before a letter the name lacks) answers here in seconds
 * where the server answers in a fraction of a millisecond. Runs of consecutive `*`
 * are collapsed to one, which removes the cheapest way to trigger that — a 24-`*`
 * run against a name it cannot match goes from about a minute to under a
 * millisecond — but `*` interleaved with literals remains exposed.
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
 * The code points Python's `str.strip()` removes, as a RegExp class body.
 *
 * `String.prototype.trim` is a different function over a different set, so it
 * cannot stand in for `strip()` here. Swept over the whole code-point range
 * against this deployment's interpreter, `strip()` removes 29 code points and
 * `trim()` removes 25; they share 24 and differ on six:
 *
 * | Code point | `strip()` | `trim()` |
 * |---|---|---|
 * | U+001C, U+001D, U+001E, U+001F (the file/group/record/unit separators) | removes | keeps |
 * | U+0085 (NEL) | removes | keeps |
 * | U+FEFF (zero-width no-break space, the byte-order mark) | keeps | removes |
 *
 * Both directions are reachable, and neither is benign, because `normalizeScope`
 * decides which entries *survive* as well as what each surviving one matches — so
 * a `trim()` here would break the mirror twice over:
 *
 * - U+FEFF makes the client **looser**. `trim()` reduces an entry written
 *   `U+FEFF` + `lending` to a bare `lending`, which then admits the profile
 *   `lending`; the server compares the mark-prefixed entry and refuses. Worse, an
 *   entry of nothing but the mark
 *   trims to the empty string and is dropped as blank, which empties the scope —
 *   and an empty scope is *unrestricted*, so a caller the server confines to one
 *   unmatchable entry would be offered every profile in the deployment.
 * - The five Python-only separators make it **stricter**, which hides a profile
 *   the server serves. The server strips `\x1clending` to `lending` and admits
 *   that profile, and drops an entry of separators alone as blank, leaving the
 *   caller unrestricted; a `trim()` client keeps both and matches neither.
 *
 * Python's set is Unicode-version dependent — U+180E was whitespace before
 * Unicode 6.3 and is in neither set now — so this is the measured set for the
 * interpreter the Lambdas run, not a set derived from a specification.
 */
const PYTHON_WHITESPACE = '\\t\\n\\v\\f\\r\\u001c-\\u001f \\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';

const LEADING_PYTHON_WHITESPACE = new RegExp(`^[${PYTHON_WHITESPACE}]+`);

const TRAILING_PYTHON_WHITESPACE = new RegExp(`[${PYTHON_WHITESPACE}]+$`);

/**
 * Strip a scope entry the way Python's `str.strip()` does.
 *
 * Applied to scope **entries** only. `scope_allows` does not strip the profile
 * name — it compares `str(profile_name)` as given — so neither does this module,
 * and adding it here would deny a profile whose name really does carry a space.
 */
const pythonStrip = (text: string): string => text.replace(LEADING_PYTHON_WHITESPACE, '').replace(TRAILING_PYTHON_WHITESPACE, '');

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
  const entries = raw.map((entry) => pythonStrip(entry ?? '')).filter((entry) => entry.length > 0);
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
 * **Two pieces of the code below index by UTF-16 code unit where CPython's
 * `fnmatch._translate` indexes by code point, and together they are the whole of
 * the residual disagreement with the Python.** Neither shows up unless a class
 * body holds both a range hyphen and a code point outside the Basic Multilingual
 * Plane, and both are needed to describe the residual: fixing either alone leaves
 * the other. Over the differential run described in the module header (18,836
 * patterns, 43,398,144 pairs), 519 patterns disagree on at least one name — 387
 * would be settled by making the ordering test code-point-correct, 81 by making
 * the run-split arithmetic code-point-correct, and 51 need both. Counted instead
 * by which dependency they involve at all, 438 involve the ordering test and 132
 * the run-split arithmetic.
 *
 * **The ordering test and collapse.** `left.slice(-1)` and `right.slice(0, 1)`
 * each take one code *unit*, so the two sides can disagree about whether a range
 * is out of order — and the collapse then takes a unit off each side where the
 * Python takes a code point. It breaks two ways:
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
 * **The run-split scan.** The `start + 1` / `start + 2` skip past a leading `!`,
 * the `indexOf('-', cursor)` search position and the `cursor = hyphen + 3` advance
 * past a completed range are all code-unit offsets. An astral member therefore
 * shifts where the `+ 3` lands relative to CPython's own `k = k+3`, and a hyphen
 * CPython keeps as a literal member after a completed range is read here as the
 * start of another range. The member partition itself then differs, before any
 * ordering question arises — and one pattern shows both directions at once:
 *
 * - `[a-😀-😃]` partitions to `a` | `😀-😃` in CPython, which emits the range
 *   `a`–`😀` plus the literal members `-` and `😃`. Here the `+ 3` lands one unit
 *   early, the body partitions to `a` | `😀` | `😃`, the last pair collapses as out
 *   of order, and the class becomes the single range `a`–`😃`. So `-` matches on
 *   the server and not here (**stricter**), while `😁` — inside `a`–`😃` but above
 *   `😀` — matches here and not on the server (**looser**: the client offers a
 *   profile the server refuses).
 * - `[a-😀-c]` partitions the same way in CPython and emits `a`–`😀`, `-`, `c`.
 *   Here it becomes `a` | `😀` | `c`, `😀`–`c` collapses to a lone high surrogate,
 *   and the class is `a`–`\uD83D` — so both `😀` and `-` match on the server and
 *   not here.
 *
 * Reaching any of this needs an admin to write a glob whose character class holds
 * an astral code point around a range hyphen, which the Admin UI's fixed profile
 * list cannot produce.
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
 * emit is within that set, so neither needs loosening: over the differential run
 * described in the module header, no pattern fails to compile for an escaping
 * reason.
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
    // `translateClass`). Over the differential run described in the module header
    // that shape is the only thing that reaches this arm — 108 of the run's 18,416
    // glob patterns reach it, every one of them fitting it, and no pattern reaches
    // it for an escaping fault. Whatever lands here must leave its own entry unable
    // to match rather than throw out of the render that called this.
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
