# Internationalization

Developer notes for `src/ui/src/i18n/`: how the web UI shows a language other than English,
how a message is declared, and what the gates around it check. The user-facing description is
[Interface language](../../../../docs/web-ui.md#interface-language) in the web UI guide. The series
of pull requests that builds this out is tracked in
[issue #1770](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1770).

## What this is

The UI uses [react-intl](https://formatjs.github.io/docs/react-intl/) with the English text kept
inline in the code. `en` and `fr-CA` are enabled, each by its catalog file (see "Catalogs and the
build"), and the pseudo-locale `en-XA` exists on flagged builds only (see below). The rules every
piece of this module follows:

- **English inline, stable ids.** Each message is declared next to the component that renders it,
  with an explicit dotted `id` and the English as `defaultMessage`. There is no English catalog to
  load: `en` renders the inline text, and the app's `LocaleProvider` adds nothing to the markup a
  plain `IntlProvider` renders (identical apart from per-render generated ids, which the tests
  normalise before comparing).
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
- **Text the UI sends to an agent.** A label shown for text the UI sends (a suggested prompt) is a
  message; the text sent stays English and is read from the descriptor's `defaultMessage`, so it
  is written once. Text the UI sends only for the agent to read (Quick Start's upload summary
  and attachment note) goes inside a `<panel-context>` block that the chat does not show
  (`components/agent-chat/panelContext.ts`): it stays English, counted by the string-debt gate,
  with a comment at its builder, and what the user sees instead is a message of its own, such as
  `agentChat.quickStart.attachedDocuments`, or nothing.

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
English-only deployment would get a French sign-in form while the app resolved `en`. Under
`fr-CA` the sign-in form uses Amplify's built-in `fr` dictionary, through `I18n.get`'s
language-subtag lookup. No `fr-CA` override dictionary (Québec terms such as "courriel" for
e-mail, the space before `?`) is registered; one would be added in `registerAmplifyVocabularies`.
The UI's own sign-in strings ([`UnauthRoutes.tsx`](../routes/UnauthRoutes.tsx),
[`SessionStates.tsx`](../routes/SessionStates.tsx)) are ordinary messages.

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
  `0.0123` to `0.01`; state today's precision at each site. Money never goes through
  `intl.formatNumber`, `<FormattedNumber>` or an ICU `::currency/USD` skeleton: those print the
  locale's own symbol (`$ US` under `fr-CA`). A message that contains a cost takes the
  `formatUsDollars` string as a plain argument.
- **`formatPercent` takes a ratio** (`0.125`, not `12.5`); `Intl` does the ×100.
- **`formatFixed(value, digits)`** is the locale-aware `toFixed`; `{ useGrouping: false }`
  drops the thousands separator.
- **Display only.** A value that is parsed back (a form field), exported (CSV, JSON, markdown),
  copied to the clipboard, or compared or sorted as text keeps locale-neutral formatting
  (`toFixed`, `String`, `JSON.stringify`): `1 234,50` round-trips through `parseFloat` as `1`,
  and `US$0.0123` through `replace('$', '')` as `NaN`.
- **`useFormatters()` works without the provider.** Outside `LocaleProvider` (a plain `render`
  in a test) it formats as `en`, the runtime default, so an English component test needs no
  provider; the app always mounts one. Its `locale` member is what a component hands to a pure
  function that takes `locale` (`formatCostUsd(value, locale)`), so component code never calls
  `useLocale()` for formatting; `useLocale()` still throws without the provider, because
  switching locale needs it.
- In `fr-CA`, ICU emits a no-break space as the group separator and before `%` and `US$`; which
  one (U+00A0, or the narrow U+202F) has changed between ICU releases, so tests match those
  positions with `\s` and never name a codepoint. jsdom's `Intl` follows the process locale, so every
  formatting test passes an explicit locale rather than relying on the machine's default.

Every displayed date, time and amount goes through the helpers, and so does nearly every
number. What remains on `toFixed` or `toLocaleString` is either technical (exports,
parse-backs, text comparisons, debug timings, chart data) or one of the exceptions named here:
the test-run comparison's colour-graded cells and its field-metrics string, which are parsed
back; and the capacity planner's quota strings, a follow-up. The comparison screen's CSV test
pins an export beside its localized cell.

## Catalogs and the build

| File | Role |
| --- | --- |
| [`en.json`](en.json) | The extracted English source catalog, `{ "<id>": { "defaultMessage", "description" } }`, committed. Written by `npm run i18n:extract` (`make ui-i18n-extract`), which runs `formatjs extract` over `src/**/*.{ts,tsx}` minus tests, `src/graphql/**`, `.d.ts` files and `src/test-utils/**`; a duplicate id with a different `defaultMessage` fails extraction |
| `<locale>.json` (`fr-CA.json`) | A translation: `{ "<id>": { "message": "<ICU>", "sourceHash": "<16 hex>" } }`. `sourceHash` is the first 16 hex characters of SHA-256 over the NFC-normalised UTF-8 English `defaultMessage` the entry was translated from ([`catalog.mjs`](../../scripts/i18n/catalog.mjs) `sourceHash()`) |
| [`vite-plugin-catalogs.mjs`](../../scripts/i18n/vite-plugin-catalogs.mjs) | Compiles every `<locale>.json` at build time into the virtual modules `virtual:i18n-catalogs` (`availableLocales`, `loaders`) and `virtual:i18n-catalog/<locale>` (the parsed table), typed in [`virtual-catalogs.d.ts`](virtual-catalogs.d.ts) |
| [`validate-message.mjs`](../../scripts/i18n/validate-message.mjs) | The per-message parity check: both texts parse as ICU (every plural/select needs `other`), the same argument names, each shared argument keeps its kind (plural, selectordinal, select, or plain), the same rich-text tag names. Plural branch keys are not compared, since languages have different categories |
| [`check-catalog.mjs`](../../scripts/i18n/check-catalog.mjs) | CLI over `compileCatalog` for one locale; what the gate runs |
| [`loadCatalog.ts`](loadCatalog.ts) | `loadCatalog(locale)`: `en` → a frozen empty table with no import; otherwise `loaders[locale]()`, the lazy chunk |
| [`glossary.fr-CA.csv`](glossary.fr-CA.csv) | The `fr-CA` terms, `en,fr-CA,note`: names and nouns the translation draft must use. Not a catalog (the build reads only `<locale>.json`); see [Drafting a translation](#drafting-a-translation) |
| [`translate-segments.mjs`](../../scripts/i18n/translate-segments.mjs) | What a translation engine is shown of a message and how its answer becomes ICU again; the Node half of `make ui-i18n-translate` |

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
- **A flagged tree makes one test red on purpose**: with the marker present (or the variable
  exported), `ENABLED_LOCALES` is `['en', 'fr-CA', 'en-XA']`, so the production allow-list test in
  [`resolveLocale.test.ts`](__tests__/resolveLocale.test.ts) fails. A flagged tree is not
  production; remove the marker (the command above does) before running the suite. The marker
  also changes the `make ui-lint` checksum, so lint re-runs once.
- `en-XA` has no Cloudscape or Amplify bundle: `importMessages` walks it down to `en` and loads
  `all.en`, so Cloudscape's and Amplify's own strings show their English built-ins.
- A hand-written `en-XA.json` in this directory while the build is flagged is a build error (the
  file and the generated catalog would be ambiguous). Unflagged, such a file compiles as an
  ordinary translation.

### Reviewing translated screens

`make ai-ux-translation-review STACK=<name>` looks at the signed-in UI in `fr-CA`, then in `en-XA`,
and reports what only a rendered screen shows: English left on a French screen, text that bypasses
the catalog, text cut off by its layout, a term wrong for its place, a placeholder shown raw, and
house-style typography. Whether each message means what its English means is the catalog review's
question, not this one's.

- **What the UI must hold.** `fr-CA` needs `fr-CA.json` in this directory when the UI is built;
  `en-XA` needs a flagged build (above: the `src/ui/.pseudo-locale` marker for a deployed stack,
  `IDP_UI_PSEUDO_LOCALE=1` for a local build). Point the review at a test stack deployed that way
  (its web URL is the default, or `WEB_URL=`), or at a local build made with the stack's
  `.env.production.local`, as `BUILD_DIR=`, which is served at `http://localhost:3000`. A locale
  the build lacks fails alone, saying so, and the next one still runs.
- **How it runs.** In the UX runner's Docker image, each locale in its own confined, signed-in
  Chrome, as the MR UX test runs. The harness switches the language as the UI records a user's
  choice (`idp-locale`, then a reload) and waits until the screen shows it. The model may only
  navigate, wait, hover, snapshot and screenshot: the commit guard recognises a control by its
  English name, so on these screens a click could not be checked. Dialogs and menus that need a
  click are not reviewed.
- **Cost.** Bedrock (`us.anthropic.claude-opus-5-5`, in `ca-central-1` unless `AWS_REGION` is
  set) on your own AWS credentials, within `MAX_USD` (default 40): each session may spend nine
  tenths of an even share of what is left. The fr-CA glossary is `GLOSSARY=` (default
  `glossary.fr-CA.csv` here); `LOCALE=en-XA` reviews the pseudo-locale alone.
- **Output.** `test-reports/ux-translation-review/<time>-review/`: `findings.md` (a table per
  locale, each finding linked to its screenshot), `findings.json`, `run.json` and `screens/`.
  There is no video: the recorder renders only the review stack `idp-ux-nightly`. The findings are
  the model's claims for a person to adjudicate.
- **What the stack holds is the operator's job.** Nothing checks it: the model sees, and the
  screenshots keep, whatever the stack's pages show, and the screenshots are not redacted. Point
  a run only at a stack that holds sample data, and keep its screenshots private: even then, the
  Users page screenshot (the default persona is an admin) shows the e-mail addresses of the
  stack's users, the admin's included.

## The string-debt gate

The tree carries thousands of hardcoded English strings. Rather than a lint rule that would be red
forever, the rule is run as a measurement and ratcheted: per file, the count may only fall.

- **The counting rule** is [`eslint.i18n.config.js`](../../eslint.i18n.config.js):
  `i18next/no-literal-string` in mode `all`, run alone by `npm run i18n:lint` (never part of
  `npm run lint`, which runs with `--max-warnings 0` inside `npm run build`). It counts JSX text,
  user-facing attributes (`label`, `header`, `placeholder`, `ariaLabel`, ...), object properties
  including Cloudscape `i18nStrings` members, and strings in plain `.ts` modules (labels,
  breadcrumbs, notifications). Its lists run in **exclude mode** only (an include list would hide
  every descendant of a container it did not name): leaf-valued attributes and keys (`className`,
  `iconName`, `id`, `href`, CSS properties), explicit callees (`logger.info`, `Object.keys`,
  `Error` constructors) and full-match patterns for technical tokens (punctuation, units,
  kebab/snake/dotted ids, URLs, MIME types, CSS values, emoji). The ESLint cache is
  `.ui-i18n-eslintcache` at the repository root, outside `src/ui` so it does not disturb the
  `ui-lint` checksum; about 15 s cold, 2.5 s warm.
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
  reviewable; the justified cases are upstream code merged before the gate existed, as the two
  `chore(ui): absorb upstream ...` commits of the foundation PR show, and an export module whose
  text is English by design (below). Export builders whose text is English by design (CSV column
  headers, generated report headings) stay counted: the gate does not tell text a user reads on
  screen from text a user reads in a download, and an exemption by file name would take whatever
  is written under that name out of the count unseen. Once the conversion PR that touches the
  screen has declared everything else, the file's entry shrinks to the export text and `--write`
  records it. To make the remaining entry self-explanatory, move the export text and the function
  that assembles it into a sibling `<name>.export.ts` module and record the new path once with
  `--write --allow-new-debt "<path>: <what it exports>, English by design"`: the pinned count is
  the record, an increase needs another reason, and renaming an existing counted module into the
  suffix meets the same refusal. Never write an `eslint-disable i18next/no-literal-string`
  comment: `npm run lint` does not register the plugin and rejects the directive as an unknown
  rule, and registering it without the rule only turns the error into an unused-directive warning
  that `--max-warnings 0` rejects.

```ts
// test-results.export.ts — the CSV a screen offers for download, English by design
export const csvHeader = ['Test Run ID', 'Test Set', 'Status'];

export function toCsv(rows: string[][]): string {
  return [csvHeader, ...rows].map((row) => row.join(',')).join('\n');
}
```

`src/ui/src/i18n/__tests__/eslint-i18n-config.test.ts` lints this example through both
configurations: counted by the counting configuration on its own `.export.ts` path, clean under
`npm run lint`, and the file-local `eslint-disable` comment rejected there as an unknown rule.

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
   `'link'`, `'primary'`) are treated as technical. Some of them are user-facing, such as the
   wizard's `optional: 'optional'` in [`CreateTestSetWizard.tsx`](../components/test-studio/CreateTestSetWizard.tsx),
   and the conversion PRs find the others by reading each screen.
   To re-measure: remove that pattern from `WORDS` in `eslint.i18n.config.js`, run
   `npm run i18n:lint`, and count the findings it adds (then restore the pattern: the gate pins
   the configuration's hash).
4. **Object literals keyed by enum-like names.** `LEAF_KEYS` excludes keys matching `[A-Z_-]+` and
   the plugin skips the whole value under an excluded key, so a map keyed `CLOSED`/`HALF_OPEN`/`OPEN`
   never counts: **16 findings in 4 files** when this entry was added, 6 of them user-facing (the
   circuit-breaker panel's state labels, since converted, and `TestSets.tsx`'s type badges). To
   re-measure: remove that pattern from `LEAF_KEYS` in `eslint.i18n.config.js`, run
   `npm run i18n:lint`, diff against the committed run, restore it.
5. **Words that contain a slash.** The `WORDS` pattern for MIME types and slash paths
   (`[\w.+-]+/[\w.+/-]+`) also skips user-facing words with a slash: **109 findings in 19 files**
   when this entry was added (`N/A` 95, `Service/Api` 6, `YYYY/MM/DD` 5, `Class/Type` 2,
   `Docs/Hour` 1). The conversion PRs convert them with their screens. To re-measure: remove that
   pattern from `WORDS` in `eslint.i18n.config.js`, run `npm run i18n:lint`, count the findings it
   adds, then restore it (the gate pins the configuration's hash).

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
candidate (it is never a file). This is the figure release notes quote.

## Drafting a translation

`make ui-i18n-translate LOCALE=fr-CA` ([`ui_i18n_translate.py`](../../../../scripts/ui_i18n_translate.py) over
[`translate-segments.mjs`](../../scripts/i18n/translate-segments.mjs)) drafts, with Amazon Translate, the entries of
`<locale>.json` that the coverage report calls missing or stale, and writes them with their `sourceHash`. It is a
developer tool, run with your own AWS credentials: no CI job runs it and no deployed stack calls it. Start with
`UI_I18N_TRANSLATE_ARGS=--dry-run`, which prints the entries, the characters and the estimated cost and calls
nothing; a real run needs `--region` (and `--profile` for a named profile), and `--limit N` takes a small batch first.
A `<locale>.json` in this directory turns that language on in the switcher (`availableLocales` is file presence), so
a drafted catalog is committed only in the change that enables its language.

- **What Translate sees.** Never ICU. Every plural and select is hoisted so each branch is a whole sentence, sent as
  HTML with arguments and `#` in `translate="no"` spans and rich-text tags as spans. The answer is rebuilt into ICU
  and must pass the parity check the build applies; otherwise the entry is left out (English shown) and named.
- **Register and typography.** `fr-CA` is asked for the formal register, and an answer without it is refused. The
  rebuilt text gets the typographic apostrophe, no space before `;`, `?` and `!`, a no-break space before a colon that
  ends a phrase, and « » quotation marks.
- **The glossary.** [`glossary.fr-CA.csv`](glossary.fr-CA.csv) is imported as the custom terminology `idp-ui-fr-CA`,
  its first two columns only, again whenever they change. Translate applies a term wherever its English occurs, so
  the file holds names and nouns that are safe in every context, never verbs. A multi-word term matched in Title Case
  English is put back in the glossary's own case, before the parity check, so the check judges the text written. A new
  screen's new term goes in the same change as the screen.
- **Cost.** Translate bills the characters sent. The run refuses to start above `--max-chars` (by default about
  US$5 at the standard rate); a full `fr-CA` pass is far below it.
- **Provenance.** Each run writes what it sent and received, the terms Translate applied, the terminology hash and,
  with `--back-translate`, each answer translated back into English, under `test-reports/ui-i18n-translate/`. The
  catalog records none of it. Review the French before committing it.
- **Permissions.** `translate:TranslateText`, `translate:GetTerminology` and `translate:ImportTerminology` in the
  Region you pass.
- **Exit status.** 0 when every entry was written (or a dry run), 1 when something needs a person (an answer
  refused, a message that cannot be split, a back-translation stopped), 2 when nothing was written.

## Reviewing a translation

`make ui-i18n-review LOCALE=fr-CA` ([`ui_i18n_review.py`](../../../../scripts/ui_i18n_review.py)) has a Bedrock model
review the entries of `<locale>.json` and writes `report.md` and `report.json` under `test-reports/ui-i18n-review/`.
It changes no catalog: a person reads the report and decides. Like the drafting tool, it is a developer tool run with
your own AWS credentials. Start with `UI_I18N_REVIEW_ARGS=--dry-run`, which prints the entries, the batches and the
estimated cost and calls nothing; a real run needs `--region` (and `--profile` for a named profile).

- **What is reviewed.** Every entry the build accepts and whose English has not changed since it was translated.
  The coverage report's invalid and stale entries are listed in the report and never sent. `--provenance <file>`
  narrows the review to the entries one `make ui-i18n-translate` or `make ui-i18n-revise` run wrote, with the
  translation run's back-translations as evidence.
- **What the model is asked.** The locale's rules and the whole glossary come first, then, per entry, the English, its
  `description`, the translation and the glossary terms its English uses. The model answers through one tool with the
  problems it sees (meaning, glossary, register, regional, grammar, typography), a short reason and its own
  translation. Arguments, plurals, tags and spacing before punctuation are checked by code and kept out of its brief.
- **What the report says.** Each entry is `same` (no problem, same wording), `same-meaning` (no problem, worded
  otherwise: the current translation stands) or `disagreement` (a person decides, with the model's suggestion beside
  it). An answer that is not well formed is listed as unreviewed and never counted. The agreement figure is the share
  of reviewed entries with no problem. A suggestion you take is copied into `<locale>.json` by hand, where
  `make check-ui-i18n-catalog` checks it.
- **Model and cost.** The default model is `us.anthropic.claude-opus-5-5`; `--model` takes another inference profile
  of it that has a price in `config_library/pricing.yaml`. The run refuses to start above `--max-usd` (US$20 by
  default) and stops before any batch that could take it past the cap. Each attempt botocore retried, and every
  attempt of a call that failed, counts at its most, since a timed-out attempt may be billed; the report shows that
  part on a line of its own.
- **Permissions.** `bedrock:InvokeModel` on the inference profile and on the foundation model in each Region the
  profile routes to.
- **Exit status.** 0 when the report covers every entry (or a dry run within the cap), 1 when it is partial (the
  cap, an AWS error or unusable answers, each listed), 2 when no report was written.

## Revising a translation

`make ui-i18n-revise LOCALE=fr-CA` ([`ui_i18n_revise.py`](../../../../scripts/ui_i18n_revise.py)) has the review's
model write again the entries of `<locale>.json` that a review flagged, or with `--refresh` every entry the catalog
lacks or holds for an older English, and writes each answer that passes the build's parity check. It is a developer
tool run with your own AWS credentials, like the other two. Start with
`UI_I18N_REVISE_ARGS="--review <report.json> --dry-run"`, which prints the entries, the batches and the estimated cost
and calls nothing; a real run needs `--region` (and `--profile` for a named profile).

- **What is revised.** `--review <report.json>` (repeatable) takes a review's disagreements, each with the problems,
  reason and suggestion the review gave, and the entries it could not review. An entry whose translation changed
  after that review is left out. `--notes <file>`, a JSON object of message id to a note, adds entries no review
  flagged, such as one a screen shows wrongly or one the catalog lacks. Invalid entries are never sent, and stale
  ones only with `--refresh`.
- **Refreshing a catalog.** `--refresh` is the way to bring a catalog up to date with Bedrock after the English
  changed: it adds every entry `make ui-i18n-coverage` calls missing or stale. A missing entry is drafted from the
  English, and so is a stale one: its request says that its English changed and carries neither its old translation
  nor a review's findings about it, and its answer is written with the new `sourceHash` even when the text is the
  same. It combines with `--review` and `--notes`. `UI_I18N_REVISE_ARGS="--refresh --dry-run"` prints how many
  entries are missing and stale and the estimated cost; after the run, `make ui-i18n-review` with
  `--provenance <file>` reviews what it wrote.
- **What the model is asked.** The review's rules for the locale come first, then the rules for a revision: for
  `fr-CA`, Canadian French wherever it fits (Quebec usage such as courriel, clavardage, téléverser and bogue), a
  no-break space before a colon that ends a phrase and none before `;`, `?` and `!`, then the whole glossary. Per
  entry it gets the English, its `description`, the current translation (none for a missing or stale entry), the
  glossary terms its English uses, and the review's findings and the note when there are any. It answers with the
  message only.
- **What is written.** Each answer first gets the locale's spacing before punctuation by code, as the drafting tool
  applies it (the model does not keep no-break spaces). The coverage worker then checks every answer with the
  build's parity check. An answer that fails, or is not well formed, is refused with the reason, and its entry keeps
  its text. An answer that passes and differs is written with the `sourceHash` of its English. Review the French
  before committing it.
- **Provenance.** Each run writes the model, the Region, the prompt's and the glossary's SHA-256, the cost, and per
  entry the text before and after and its outcome (revised, unchanged, refused or skipped) under
  `test-reports/ui-i18n-revise/`. `make ui-i18n-review` with `--provenance <file>` reviews the entries that run
  wrote. When the same model wrote them, that review is not independent of the revision.
- **Model and cost.** As the review: `us.anthropic.claude-opus-5-5` by default, a refusal to start above
  `--max-usd` (US$20 by default), a stop before any batch that could take the run past it, and retried attempts
  counted at their most, apart in the provenance.
- **Exit status.** 0 when every entry sent was answered and checked and none was refused (or a dry run within the
  cap), 1 when an answer was refused or the cap or an AWS error stopped the run (what passed is written, the rest is
  listed), 2 when nothing was written.

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
  react-intl calls can keep plain `render`. `LocaleProvider` under `en` adds nothing to the
  markup: the helper's own test renders the document-list header and a set of Cloudscape
  built-ins under it and under a plain `en` `IntlProvider`, and they render identically apart
  from the per-render generated ids (React `useId` values, Cloudscape's performance marks)
  that the assertions normalise first. A test moves to `renderWithIntl` when it renders
  another locale or its component reads `useLocale()`.
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

The list a user reads is [Interface language](../../../../docs/web-ui.md#interface-language) in
the web UI guide; change it together with this section. In the text that list keeps English, the
unit suffixes on durations and sizes (`s`, `m`, `KB`, `MB`) and the English `%` placement on
integer percents built in template strings (`${progressPct}%`) stay until that text converts.
The follow-ups (keeping the choice with the user account, the configuration editor, stable error
codes, agent-convention docs) are listed in
[issue #1770](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1770).
