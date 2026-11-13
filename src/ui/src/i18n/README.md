# Internationalization

Developer notes for `src/ui/src/i18n/`: how the web UI shows a language other than English,
how a message is declared, and what the gates around it check. Nothing here is visible to a user
yet (only `en` is enabled); the user-facing description goes into
[docs/web-ui.md](../../../../docs/web-ui.md) with the pull request that enables `fr-CA`. The series
of pull requests that builds this out is tracked in
[issue #1770](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1770).

## What this is

The UI uses [react-intl](https://formatjs.github.io/docs/react-intl/) with the English text kept
inline in the code. Today only `en` is enabled; `fr-CA` arrives with its reviewed catalog, and the
pseudo-locale `en-XA` exists on flagged builds only (see below). The rules every piece of this
module follows:

- **English inline, stable ids.** Each message is declared next to the component that renders it,
  with an explicit dotted `id` and the English as `defaultMessage`. There is no English catalog to
  load: `en` renders the inline text, and the markup is the one from before i18n existed (identical
  apart from per-render generated ids, which the tests normalise before comparing).
- **English fallback, never a broken screen.** A key the active catalog lacks renders its
  `defaultMessage`. [`LocaleProvider.tsx`](LocaleProvider.tsx) swallows react-intl's
  `MISSING_TRANSLATION` (the coverage report, not the console, tracks the gaps) and logs every
  other react-intl error once per distinct error through `ConsoleLogger`; nothing throws.
- **Allow-listed locale, never from the URL.** [`resolveLocale.ts`](resolveLocale.ts) takes a
  stored choice (`localStorage` key `idp-locale`, [`locales.ts`](locales.ts)) only when it exactly
  equals an enabled locale; otherwise it walks `navigator.languages` in order, each tried as an
  exact match and then by language subtag (`fr-FR` and `fr` → `fr-CA` once enabled, `en-GB` →
  `en`); otherwise `en`. The subtag step is for the browser default only: a stored `fr-FR` is
  ignored. `location.search` and `location.hash` are never read. `<html lang>` is set from the
  resolved locale and nothing else.
- **Catalogs are build-time, same-origin assets.** Every locale's messages are compiled by the
  Vite build into one lazy chunk; nothing is fetched from S3 or any URL at run time, and there is
  no runtime translation service.
- **Document data is never translated.** Nor is user-authored configuration content or any server
  response. Only UI text declared in this repository goes through the catalog.

`ENABLED_LOCALES` is derived from the catalog files present in this directory at build time, in
`KNOWN_LOCALES` order, so a locale enables itself the moment its `<locale>.json` lands. The
language menu ([`LanguageSwitcher.tsx`](LanguageSwitcher.tsx), mounted by
[`GenAIIDPTopNavigation.tsx`](../components/genai-idp-top-navigation/GenAIIDPTopNavigation.tsx))
renders nothing while fewer than two locales are enabled.

## Writing a message

Declare messages with `defineMessages` in the file that renders them, never in a shared messages
module (tests that read source files, and the Python tests that pin English labels, depend on the
text staying in the file):

```tsx
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

const messages = defineMessages<{ title: NoMessageValues; count: { readonly count: number } }>({
  title: { id: 'documents.list.title', defaultMessage: 'Documents', description: 'Heading of the document list page.' },
  count: {
    id: 'documents.list.count',
    defaultMessage: '{count, plural, one {# document} other {# documents}}',
    description: 'Row count under the heading. {count} is the number of documents listed.',
  },
});

const intl = useIntl();
intl.formatMessage(messages.count, { count: rows.length });
```

- **Id scheme**: `<area>.<component>.<name>`, lower camel case segments, at least one dot
  (`i18n.switcher.ariaLabel`). Ids are stable: an edit to the English keeps the id, and the
  `sourceHash` below tells translators the source moved. `defaultMessage` and `description` are
  both required literals; the description is for the translator (what the text is, where it shows,
  what each argument holds).
- **The generic on `defineMessages`** names each message's ICU arguments, as
  [`LanguageSwitcher.tsx`](LanguageSwitcher.tsx) does. react-intl 12 types `formatMessage`'s
  values from it, so a call site that forgets `{language}` fails `tsc`; a message with no
  arguments is `NoMessageValues`.
- **`<FormattedMessage>` vs `intl.formatMessage`**: the component for text rendered in JSX
  (`textComponent` stays `React.Fragment`, so it adds no element), `formatMessage` for a string
  prop (`ariaLabel`, `placeholder`, a column header, a notification).
- **Plurals** are ICU, with `#` for the number. `one` and `other` are required
  (`formatjs/enforce-plural-rules`); French applies `one` to 0 and 1, so never derive a plural
  from `=== 1` in code.
- **Rich text** goes through element values only: `defaultMessage: 'Open the <link>settings</link>.'`
  with `values={{ link: (chunks) => <Link href="...">{chunks}</Link> }}`. `dangerouslySetInnerHTML`
  stays banned.
- **Never concatenate fragments** (`'Delete ' + n + ' items'`) and never put user-facing text in
  a `const ALL_CAPS = ...` constant (see the known gaps under the string-debt gate): a
  module-level label becomes a `defineMessages` descriptor formatted at render time.

### Cloudscape and Amplify strings

Cloudscape's built-in strings come from its own `fr` bundle, mounted by
[`cloudscape.ts`](cloudscape.ts) through `I18nProvider`. An explicit `i18nStrings` prop always
beats the provider, so the UI's hand-written `i18nStrings` blocks (file upload, wizard, guided
tour, chat) are converted to messages like any other text; the counting rule below counts their
members. Under `en` the provider gets `messages={[]}` and never loads Cloudscape's `all.en`: that
bundle would fill `i18nStrings` props the app leaves undefined and change English DOM and aria
text. `@cloudscape-design/chat-components` has no i18n of its own; every string it shows is
app-supplied and goes through the catalog.

The sign-in form is Amplify UI. [`amplify.ts`](amplify.ts) registers Amplify's `fr` dictionary
once and calls `I18n.setLanguage(locale)` in the provider's state initialiser, before the
Authenticator first renders, and again on every switch. That order matters: once any vocabulary
exists, Amplify defaults to `navigator.language`, so without it a French browser on an
English-only deployment would get a French sign-in form while the app resolved `en`. The `fr-CA`
override dictionary (Québec terms such as "courriel" for e-mail, the space before `?`) lands with
PR 8 of the series, in the spot `registerAmplifyVocabularies` reserves. The UI's own sign-in
strings ([`UnauthRoutes.tsx`](../routes/UnauthRoutes.tsx), [`SessionStates.tsx`](../routes/SessionStates.tsx))
are ordinary messages.

## Dates, numbers, money

[`format.ts`](format.ts) holds the display-only helpers and `useFormatters()`, which returns
them bound to the current locale (one memoised object per locale, safe in dependency lists).
The one rule about which `Intl` locale is used is `formattingLocale`: under `en` and `en-XA` the
helpers pass no locale, so `Intl` uses the runtime default, exactly what today's bare
`toLocaleString()` calls do; under `fr-CA` they pass `'fr-CA'`. The time zone is never set. The
context also exposes `intlLocale` (`undefined` under `en`, the tag otherwise) for a component
that takes a `locale` prop directly.

| Helper | en (observed as en-US) | fr-CA |
| --- | --- | --- |
| `formatDateTime(d)` | `10/30/2026, 5:40:05 PM` | `2026-10-30 17 h 40 min 05 s` |
| `formatDate(d, { dateStyle: 'long' })` | `October 30, 2026` | `30 octobre 2026` |
| `formatTime(d, { timeStyle: 'short' })` | `5:40 PM` | `17 h 40` |
| `formatNumber(1234.56)` | `1,234.56` | `1 234,56` |
| `formatPercent(0.125, { fractionDigits: 1 })` | `12.5%` | `12,5 %` |
| `formatUsDollars(0.0123, { fractionDigits: 4 })` | `US$0.0123` | `0,0123 US$` |
| `formatUsDollars(1234.5, { fractionDigits: 2 })` | `US$1,234.50` | `1 234,50 US$` |

- **`US$` in every locale.** `formatUsDollars` formats with `Intl`'s currency style and replaces
  only the `currency` part of `formatToParts` with the literal `US$`; digits, separators, the
  space before a trailing symbol and the minus sign (`-US$3.50`, `-3,50 US$`) are the locale's
  own. `fractionDigits` is required because `Intl`'s two-digit default would round a cost of
  `0.0123` to `0.01`; state today's precision at each site.
- **`formatPercent` takes a ratio** (`0.125`, not `12.5`); `Intl` does the ×100.
- **`formatFixed(value, digits)`** is the locale-aware `toFixed`; `{ useGrouping: false }`
  drops the thousands separator.
- **Display only.** A value that is parsed back (a form field), exported (CSV, JSON, markdown),
  copied to the clipboard, or compared or sorted as text keeps locale-neutral formatting
  (`toFixed`, `String`, `JSON.stringify`): `1 234,50` round-trips through `parseFloat` as `1`,
  and `US$0.0123` through `replace('$', '')` as `NaN`.
- In `fr-CA`, ICU emits a no-break space as the group separator and before `%` and `US$`; which
  one (U+00A0, or the narrow U+202F) has changed between ICU releases, so tests match those
  positions with `\s` and never name a codepoint. jsdom's `Intl` follows the process locale, so every
  formatting test passes an explicit locale rather than relying on the machine's default.

Nothing calls the helpers yet; PR 2 of the series moves the existing `toLocaleString`, `toFixed`
and `$` sites over and records the one visible change (`US$`) in the CHANGELOG.

## Catalogs and the build

| File | Role |
| --- | --- |
| [`en.json`](en.json) | The extracted English source catalog, `{ "<id>": { "defaultMessage", "description" } }`, committed. Written by `npm run i18n:extract` (`make ui-i18n-extract`), which runs `formatjs extract` over `src/**/*.{ts,tsx}` minus tests, `src/graphql/**`, `.d.ts` files and `src/test-utils/**`; a duplicate id with a different `defaultMessage` fails extraction |
| `<locale>.json` (`fr-CA.json`) | A translation: `{ "<id>": { "message": "<ICU>", "sourceHash": "<16 hex>" } }`. `sourceHash` is the first 16 hex characters of SHA-256 over the NFC-normalised UTF-8 English `defaultMessage` the entry was translated from ([`catalog.mjs`](../../scripts/i18n/catalog.mjs) `sourceHash()`) |
| [`vite-plugin-catalogs.mjs`](../../scripts/i18n/vite-plugin-catalogs.mjs) | Compiles every `<locale>.json` at build time into the virtual modules `virtual:i18n-catalogs` (`availableLocales`, `loaders`) and `virtual:i18n-catalog/<locale>` (the parsed table), typed in [`virtual-catalogs.d.ts`](virtual-catalogs.d.ts) |
| [`validate-message.mjs`](../../scripts/i18n/validate-message.mjs) | The per-message parity check: both texts parse as ICU (every plural/select needs `other`), the same argument names, each shared argument keeps its kind (plural, selectordinal, select, or plain), the same rich-text tag names. Plural branch keys are not compared, since languages have different categories |
| [`check-catalog.mjs`](../../scripts/i18n/check-catalog.mjs) | CLI over `compileCatalog` for one locale; what the gate runs |
| [`loadCatalog.ts`](loadCatalog.ts) | `loadCatalog(locale)`: `en` → a frozen empty table with no import; otherwise `loaders[locale]()`, the lazy chunk |

`make check-ui-i18n-catalog` ([`check_ui_i18n_catalog.py`](../../../../scripts/check_ui_i18n_catalog.py))
replays the `i18n:extract` script from `package.json` into a temporary file, by path out of
`src/ui/node_modules` (never `npx`), and fails with a diff when `en.json` is stale ("Run `make
ui-i18n-extract` and commit"); it then compiles every other catalog and fails on any problem a
build would hit. Check-only; part of `lint`, `fastlint` and `lint-cicd`. Off CI without
`src/ui/node_modules`, or on a machine the installed `@formatjs/cli` ships no native binary for
(the wrapper is installed everywhere, the binary per platform), it prints `⏭️ SKIPPED` (a skip,
never a pass, naming the cause); in CI either is an error.

What compiling checks: every entry must be present in `en.json` (an id the English no longer has
is a dead translation and fails the build), be the `{ message, sourceHash }` shape with a
well-formed hash, and pass the parity check against its English. The hash's *value* is not
compared at build time: a stale translation compiles and ships, and the coverage report lists it.
An id the translation lacks is fine (English fallback). A problem throws inside the plugin, so
`vite build` fails naming the catalog, the id and the reason; nothing degraded ships. `formatjs
extract` itself does not validate ICU syntax, so a malformed English `defaultMessage` is caught
by `formatjs/no-invalid-icu` in `npm run lint`, by the parity check once any translation of it
exists, and at run time by `onIntlError` (one logged `FORMAT_ERROR`, the text rendered verbatim).

Build output: one async chunk per translation, named after the virtual id (`fr-CA-<hash>.js`),
referenced only by a dynamic `import()` in the entry chunk and never preloaded; `en` has no
chunk. Cloudscape's `importMessages` holds a dynamic `import()` per bundled locale, so Rollup
also emits all fifteen `all.<locale>-*.js` chunks (about 647 KB raw, 110 KB gzip in total); a
browser fetches only the one for its locale, `all.fr` under `fr-CA`. Amplify's `fr` dictionary is
in the eager `aws-amplify` chunk (+6.8 KB raw, +2 KB gzip) because `I18n.setLanguage` must run
before the first render and `@aws-amplify/ui` exposes no deep import for one dictionary.

**Extension point.** Any `<locale>.json` a build step writes into `src/i18n` before `vite build`
takes exactly this path: listed in `availableLocales`, compiled and checked message by message,
failing the build on any problem. The pseudo-locale is the first build-produced catalog. Only
such release-built UI catalogs are translation candidates; user-authored content, document data
and server responses never pass through here.

**Restart rule.** Vite's `addWatchFile` cannot watch a directory, and the pseudo-locale flag is
read once when the plugin is constructed, so a dev server or Vitest watch started before a
catalog file was added or removed, or before the flag changed, needs a restart to see it.
`vite build` and `npx vitest run` are unaffected.

## The pseudo-locale

`en-XA` is `en.json` run through `formatjs compile --ast --pseudo-locale en-XA` at build time, in
memory: `Language` becomes `[Ŀȧȧƞɠŭŭȧȧɠḗḗ]`, with every argument, plural, select and tag intact
(checked by `validateMessageAst` against the English; a CLI or parity failure fails the build).
Running a stack in it shows two things at once: text that bypasses the catalog stays plain
English, and layouts that clip under longer text, which French (15–30% longer) will also clip.
Numbers and dates still format as English under `en-XA` (runtime default), so growth from French
dates (`2026-10-30 17 h 40 min 05 s` against `10/30/2026, 5:40:05 PM`) shows only in a real
`fr-CA` pass.

- **Local build**: `IDP_UI_PSEUDO_LOCALE=1 npm run build:only` (from `src/ui`). The output gains
  `en-XA-<hash>.js`, the switcher offers `English` and `Pseudo-locale (test)`.
- **A deployed test stack**:

  ```bash
  touch src/ui/.pseudo-locale && idp-cli deploy --stack-name <name> --region <region> --from-code . --wait; rm src/ui/.pseudo-locale
  ```

  Why a marker file and not an environment variable: the deployed UI is built by CodeBuild
  ([`template.yaml`](../../../../template.yaml), `UICodeBuildProject`) from a source zip that
  [`publish.py`](../../../../lib/idp_sdk/idp_sdk/_core/publish.py) makes from `src/ui`, excluding
  `node_modules`, `build` and every `.env*` file. That build's environment is template-authored
  and `vite.config.js` reads none of it, so a variable cannot reach it without a template change.
  The empty, gitignored `src/ui/.pseudo-locale` is not an `.env*` name, so it travels in the zip,
  and it changes the zip's content hash, so the stack rebuilds its UI.
  [`test_ui_pseudo_locale_marker.py`](../../../../scripts/tests/test_ui_pseudo_locale_marker.py)
  asserts the marker is never tracked and does travel.
- **Production builds contain nothing of it**: an unflagged build emits no `en-XA` chunk and its
  entry chunk is identical to a build before the pseudo-locale existed.
- **A flagged tree makes two tests red on purpose**: with the marker present (or the variable
  exported), `ENABLED_LOCALES` is `['en', 'en-XA']`, so the production allow-list test in
  [`resolveLocale.test.ts`](__tests__/resolveLocale.test.ts) and the top-navigation
  [slot test](../components/genai-idp-top-navigation/__tests__/language-switcher-slot.test.tsx)
  fail. A flagged tree is not production; remove the marker (the command above does) before
  running the suite. The marker also changes the `make ui-lint` checksum, so lint re-runs once.
- `en-XA` has no Cloudscape or Amplify bundle: `importMessages` walks it down to `en` and loads
  `all.en`, so Cloudscape's and Amplify's own strings show their English built-ins.
- A hand-written `en-XA.json` in this directory while the build is flagged is a build error (the
  file and the generated catalog would be ambiguous). Unflagged, such a file compiles as an
  ordinary translation.

## The string-debt gate

The tree carries thousands of hardcoded English strings. Rather than a lint rule that would be red
forever, the rule is run as a measurement and ratcheted: per file, the count may only fall.

- **The counting rule** is [`eslint.i18n.config.js`](../../eslint.i18n.config.js):
  `i18next/no-literal-string` in mode `all`, run alone by `npm run i18n:lint` (never part of
  `npm run lint`, which runs with `--max-warnings 0` inside `npm run build`). It counts JSX text,
  user-facing attributes (`label`, `header`, `placeholder`, `ariaLabel`, ...), object properties
  including Cloudscape `i18nStrings` members, and strings in plain `.ts` modules (labels,
  breadcrumbs, notifications; `*.export.ts` modules are the exception, below). Its lists run in
  **exclude mode** only (an include list would hide every descendant of a container it did not
  name): leaf-valued attributes and keys (`className`, `iconName`, `id`, `href`, CSS properties),
  explicit callees (`logger.info`, `Object.keys`, `Error` constructors) and full-match patterns
  for technical tokens (punctuation, units, kebab/snake/dotted ids, URLs, MIME types, CSS values,
  emoji). The ESLint cache is `.ui-i18n-eslintcache` at the repository root, outside `src/ui` so
  it does not disturb the `ui-lint` checksum; about 15 s cold, 2.5 s warm.
- **The baseline** is [`scripts/ui_string_debt.json`](../../../../scripts/ui_string_debt.json):
  the pinned count of every file with findings, the sha256 of the counting configuration they
  were measured with, a `highWaterMark` and `newDebtReasons`. It is registered as a count-pinned
  exemption in [`gate_exemptions.json`](../../../../scripts/tests/gate_exemptions.json).
- **`make check-ui-string-debt`** ([`check_ui_string_debt.py`](../../../../scripts/check_ui_string_debt.py))
  re-measures and fails when a listed file grew, an unlisted file carries any finding (a new file
  and a converted file both have an implicit baseline of zero), a listed file is now at zero (a
  vacuous entry: delete it so the file is held there), a listed path no longer exists, the
  configuration's sha256 changed (a widened exclude list must be a visible `--write` in the same
  change), or the high-water mark does not match the list. The failure names file, line and
  string and points here. Part of `lint`, `fastlint` and `lint-cicd`; same SKIP-off-CI rule as the
  catalog gate.
- **Paying a file down**: `python3 scripts/check_ui_string_debt.py --explain <file>` lists every
  finding with its position; convert them; `python3 scripts/check_ui_string_debt.py --write`
  re-records the smaller count (or removes the entry at zero). `--summary` prints totals and the
  top offenders. `--write` refuses to add a path or raise a count unless given
  `--allow-new-debt "<reason>"`, which it records in `newDebtReasons` so the growth stays
  reviewable; the justified case is upstream code merged before the gate existed, as the two
  `chore(ui): absorb upstream ...` commits of the foundation PR show. Export builders whose text
  is English by design (CSV column headers, generated report headings) stay in the baseline until
  the conversion PR that touches the screen moves the builder into a sibling `<name>.export.ts`
  module holding only the export text and the function that assembles it. The counting rule is
  off for `src/**/*.export.ts` (last object in `eslint.i18n.config.js`), so the module never
  enters the baseline and the component's count drops; re-record it with `--write`. Never write
  an `eslint-disable i18next/no-literal-string` comment: `npm run lint` does not register the
  plugin and rejects the directive as an unknown rule, and registering it without the rule only
  turns the error into an unused-directive warning that `--max-warnings 0` rejects. A component
  file is never renamed into the pattern: it would excuse the whole screen.

```ts
// test-results.export.ts — the CSV a screen offers for download, English by design
export const csvHeader = ['Test Run ID', 'Test Set', 'Status'];

export function toCsv(rows: string[][]): string {
  return [csvHeader, ...rows].map((row) => row.join(',')).join('\n');
}
```

**Known gaps, stated with numbers** (measured 2026-11-05 on the tree the baseline was written
against; nothing re-measures them until the follow-up gate below, so each comes with the way to
re-measure it, and the figures will shrink as the conversion PRs land):

1. **ALL-CAPS constants.** `eslint-plugin-i18next` skips every string inside a
   `const ALL_CAPS = ...` declaration unconditionally (not an option): **679 strings in 184
   constants across 78 files** are invisible to the gate, 552 of them inside object or array
   literals under user-facing keys (`COLUMN_DEFINITIONS_MAIN`, `FORMAT_OPTIONS`, `METRIC_CONFIGS`,
   `LANGUAGE_CODES`), 77 bare string constants (`const DISMISS_LABEL = 'Dismiss tour'`), the rest
   in arrow functions and templates. Each conversion PR converts the ALL-CAPS constants of its
   folders along with the visible strings, the everyday-screen folders must be at zero before
   `fr-CA` is enabled, and a supplementary count for strings under ALL-CAPS names is a committed
   follow-up to this gate right after that. The coverage report cannot see these strings either:
   a string never declared through `defineMessages` has no id in `en.json`. To re-measure: run
   `npm run i18n:lint` once as committed and once with the plugin's `isUpperCase` test (in its
   `VariableDeclarator` visitor, `node_modules/eslint-plugin-i18next/lib/rules/no-literal-string.js`)
   made to return `false`; the findings only the second run reports are this gap.
2. **`new Error('...')` texts** are not counted, by design (`Error` and every `*Error`
   constructor are excluded callees): those messages reach users through `.message` paths that
   the stable-error-code work addresses separately.
3. **Single lowercase tokens** (`[a-z][a-zA-Z0-9]*`, about 1,300 strings such as `'error'`,
   `'link'`, `'primary'`) are treated as technical. The one known user-facing miss is the wizard's
   `optional: 'optional'` in [`CreateTestSetWizard.tsx`](../components/test-studio/CreateTestSetWizard.tsx).
   To re-measure: remove that pattern from `WORDS` in `eslint.i18n.config.js`, run
   `npm run i18n:lint`, and count the findings it adds (then restore the pattern: the gate pins
   the configuration's hash).

## Coverage

`make ui-i18n-coverage` ([`ui_i18n_coverage.py`](../../../../scripts/ui_i18n_coverage.py) over
[`coverage.mjs`](../../scripts/i18n/coverage.mjs); also `npm run i18n:coverage`) reports, per
`<locale>.json`: `translated`, `missing` (no entry, English shown), `stale` (the recorded
`sourceHash` is not the hash of the current English), `invalid` (what the build rejects, with its
`problems` in the build's own wording) and `dead` (ids `en.json` no longer has), plus a
percentage. Every English id lands in exactly one of translated, missing, stale or invalid.
`UI_I18N_COVERAGE_ARGS=--json` gives the machine-readable form; `--locale fr-CA` narrows it. It is
advisory: exit 0 for any report, 2 only for a tool error (no `en.json`, malformed JSON, a
`--locale` with no file); it is in none of `lint`, `fastlint` or `lint-cicd`. `en-XA` is never a
candidate (it is never a file). Once `fr-CA` ships, this is the figure quoted in release notes.

## Testing

- **Rendering another locale**: [`renderWithIntl`](../test-utils/renderWithIntl.tsx) (and
  `renderHookWithIntl`) mounts the app's `LocaleProvider` around a component with `locale`,
  `enabledLocales`, a fixture catalog `messages` and an optional inner `wrapper`, and returns the
  render result plus `locale` and `setLocale` accessors. A fixture catalog needs one line at the
  top of the test file, `vi.mock('virtual:i18n-catalogs', () => import('../../test-utils/i18nCatalogsMock'))`,
  which swaps the build's virtual module for [`i18nCatalogsMock.ts`](../test-utils/i18nCatalogsMock.ts);
  `loadCatalog` and `LocaleProvider` run unchanged. The catalog arrives asynchronously, as the
  chunk does in the browser, so the first assertion on translated text uses `findBy*` or
  `waitFor`. Misuse throws rather than quietly rendering English: `messages` without the mock
  line, `messages` for `en`, or a `locale` outside `enabledLocales`.
- **English tests of a react-intl component need an `IntlProvider`.** A component that calls
  `useIntl()` or renders `<FormattedMessage>` throws under plain `render` (`[React Intl] Could
  not find required intl object`), even in English, so every component a conversion PR
  touches needs one in its tests. The minimal form is an `en` `IntlProvider` passed as
  `render`'s `wrapper`, as
  [`test-set-item-edit-author.test.tsx`](../components/edit-screen/__tests__/test-set-item-edit-author.test.tsx)
  does:

  ```tsx
  const English = ({ children }: { children: React.ReactNode }) => (
    <IntlProvider locale="en" defaultLocale="en">
      {children}
    </IntlProvider>
  );

  render(<Component />, { wrapper: English });
  ```

  English is inline (`defaultMessage`), so no catalog is involved. A component with no
  react-intl calls can keep plain `render`: `LocaleProvider` under `en` adds nothing to the
  markup of the document-list header and a set of Cloudscape built-ins, which render
  identically with and without it apart from the per-render generated ids (React `useId`
  values, Cloudscape's performance marks) that the assertions normalise first. A test moves to
  `renderWithIntl` when it renders another locale or its component reads `useLocale()`.
- **Hygiene**: a file that switches locales resets `localStorage`, `document.documentElement.lang`
  and Amplify's global `I18n` language in `afterEach`, as the files under `__tests__/` do. Tests
  that pass fixtures through `vi.mock` see `ENABLED_LOCALES` derived from the mocked
  `availableLocales`, so they pass `enabledLocales` explicitly where it matters.
- **Explicit locales in every formatting assertion**: jsdom's `Intl` follows the process locale
  (CI's `C.UTF-8` is en-US; a Québec developer's machine is fr-CA).
- `src/test-utils/**` is excluded from extraction, from the formatjs rules and from the counting
  rule, and [`module-reachability.test.ts`](../__tests__/module-reachability.test.ts) excludes it
  from the entry-reachability check (tests import it, the app never does).

## Housekeeping

- The `.d.mts` files next to the `.mjs` helpers in `src/ui/scripts/i18n/` are hand-written
  declaration sidecars (`tsconfig.json` has no `allowJs`/`checkJs`); an export added to a `.mjs`
  is added to its `.d.mts` by hand. Those scripts are outside `npm run lint`'s `src/**` scope, so
  Prettier is not enforced on them.
- Cloudscape's `DatePicker` ([`DateRangeModal.tsx`](../components/common/DateRangeModal.tsx))
  reads the provider's locale, lower-cases it, rejects `fr-ca` with one dev-only console warning
  (`[AwsUi] [DatePicker] Invalid locale provided`) and falls back to `<html lang>`, which is
  `fr-CA` and works. Cloudscape behaviour; harmless.
- Message hygiene rules from `eslint-plugin-formatjs` run in [`eslint.config.js`](../../eslint.config.js)
  over `src/**/*.{ts,tsx}` minus tests, `setupTests.ts` and `src/test-utils/**` (test fixtures
  are not messages): `enforce-id` (an explicit dotted id, `idWhitelist`), `enforce-default-message`
  and `enforce-description` (literals), `enforce-placeholders` (every argument supplied),
  `enforce-plural-rules` (`one` and `other`), `no-invalid-icu`, `no-multiple-whitespaces`,
  `no-offset`, `prefer-pound-in-plural`, `no-complex-selectors`. `enforce-message-types` is left
  out: it accepts only its own generated form of the `defineMessages` generic and would rewrite
  the hand-written one; the hand-written generic already gives call sites argument type-checking.
  `import-x/no-unresolved` ignores `^virtual:` for the catalog modules.
- The five i18n dependencies are exact pins (`react-intl` 12.1.6, `@formatjs/cli` 6.16.36,
  `@formatjs/icu-messageformat-parser` 3.5.22, `eslint-plugin-formatjs` 8.1.2,
  `eslint-plugin-i18next` 6.1.5). Every pin is the newest version that satisfied
  [`.npmrc`](../../.npmrc)'s `min-release-age=7` when it was added, and pins move the way every
  other UI dependency's does; never override `min-release-age` to take a newer version. The CLI
  is a JS wrapper over a native binary installed per platform, and a version may ship no binary
  for yours (6.16.36 has none for Intel Macs or Windows on ARM): `formatjs` then cannot run
  locally, `make check-ui-i18n-catalog` reports that as a visible skip rather than a failure, and
  CI measures the tree. A newer CLI that ships the binary makes the gate run locally again. Two
  copies of `intl-messageformat` are bundled (Cloudscape's v10 and react-intl's v12, about 11 KB
  each); unavoidable.

## What stays English for now

Admin, configuration and Test Studio screens (the configuration editor, including its help text,
is a committed follow-up right after `fr-CA` ships; the rest is optional later); Cognito
invitation and verification emails; Cognito service errors on the sign-in form that Amplify's
dictionary does not cover; links to the documentation site; agent and chat responses; CSV column
headers and generated report headings; extension UIs built before the Feature Platform passes
the locale, and feature `displayName`s; backend error texts until they carry stable codes; and
document data, always. The remaining PRs of the series (formatting call sites, the screens area
by area, the Feature Platform locale, review tooling, enabling `fr-CA`, Cognito persistence, the
configuration editor, error codes, agent-convention docs) are listed in
[issue #1770](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1770).
