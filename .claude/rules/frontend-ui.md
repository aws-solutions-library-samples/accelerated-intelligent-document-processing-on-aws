---
paths:
  - "src/ui/**"
---

# Frontend UI rules

Covers the React and TypeScript web UI in `src/ui/`. It is built on the
Cloudscape Design System and reaches the backend through a GraphQL-shaped client
that sends REST requests.

## Must
- **UI consistency: use Cloudscape.** Build from `@cloudscape-design/components`,
  `collection-hooks` and `chat-components`, and follow the same Cloudscape layout
  and table patterns that neighbouring pages use (`SpaceBetween`, `Container`,
  `Header`, and `Table` with `useCollection`, `Pagination` and `TextFilter`).
- **UI consistency: reuse before you create.** Look in `src/ui/src/components/common/`
  (tables, modals, `SafeMarkdown`, notification flashbar, info links, labels,
  download helpers) and in `src/ui/src/hooks/` (`use-graphql-api`, `use-polling`,
  `use-user-role`, `use-notifications`, `use-unsaved-changes-guard` and others)
  before you write a component or hook. If an existing one is close, extend it
  instead of forking a near-copy.
- **UI consistency: handle loading, empty and error states the same way
  everywhere.** Every data view needs:
  - a Cloudscape loading state (`loading`/`loadingText`, `Spinner` or
    `StatusIndicator`)
  - an `empty` state that explains what is missing and what to do next
  - a visible error (an `Alert` or the shared notifications), never a blank area
    or an error that only reaches the console
- **UI consistency: keep wording and display consistent.** For a concept that
  already appears elsewhere, reuse its label, status rendering and constants.
  Don't invent new wording.
- Write components as arrow functions (ESLint enforces this) in this order:
  hooks, effects, memos, handlers, render.
- Give new hooks kebab-case names (`use-my-hook.ts`). Don't rename existing
  camelCase hooks.
- Import through the `@/` alias, keep state in React Context with `immer`, and
  log with Amplify's `ConsoleLogger`.
- Call the API with `generateClient` from `@/api/client-shim` or with the
  `useGraphqlApi` hook. For a view that updates live, poll with `usePolling`.
- Render HTML or markdown only through `SafeMarkdown` or DOMPurify.
- Make every view accessible: labelled inputs (`FormField`), working keyboard
  navigation, and sensible focus handling.
- When you add a Cognito group, add it to `APP_GROUPS` in
  `src/ui/src/hooks/use-user-role.ts`.
- **i18n: declare text through react-intl.** User-facing text is a message with
  an explicit dotted `id`, the English as `defaultMessage` and a `description`,
  re-extracted with `make ui-i18n-extract`.
- **i18n: translate in the same change.** A change that adds or edits English UI
  text writes its French in the same change (`make ui-i18n-todo`, then
  `make ui-i18n-ingest`), or runs `make ui-i18n-drop-stale` when it cannot.
- **i18n: a new product term gets its glossary row** in
  `src/ui/src/i18n/glossary.fr-CA.csv` in the same change.

## Must not
- Use another component library (MUI, Ant Design, Bootstrap) or hand-build a
  control that Cloudscape already provides.
- Import `generateClient` from `aws-amplify/api`. No GraphQL endpoint is
  configured, so it throws. The UI uses Amplify only to get Cognito tokens.
- Edit `src/ui/src/graphql/generated/` by hand. Run `make codegen` after the
  schema changes.
- Look for subscriptions. There are none, so status comes from polling.
- Leave `console.log` in production code, or use `dangerouslySetInnerHTML`
  without sanitizing the content.
- Treat hiding a control by role as security. The server enforces access (see
  [api-authorization.md](api-authorization.md)).
- Hardcode user-facing text, or widen the exclude lists of
  `src/ui/eslint.i18n.config.js` to get past the string-debt ratchet.
- Edit `src/ui/src/i18n/fr-CA.json` by hand, its `sourceHash` included, or use
  `make ui-i18n-translate` (Amazon Translate) in the per-change flow.

## Verify with
- `make ui-lint`. Use `FORCE=1` if the checksum cache reports `SKIPPED`.
- `make ui-test`, `make ui-build`, `make codegen-check`
- `cd src/ui && npm run typecheck`
- `make check-ui-i18n-catalog`, `make check-ui-string-debt`, and
  `make ui-i18n-coverage` (advisory)

## Sources
- [.claude/skills/frontend-ui.md](../skills/frontend-ui.md): the stack, directory
  layout, context, auth and transport patterns, and lint config
- [.claude/skills/pr-review.md](../skills/pr-review.md): the UX review criteria
- [.claude/skills/ux-test.md](../skills/ux-test.md): browser-driven UX testing
  against a live stack
- [.claude/skills/ui-i18n.md](../skills/ui-i18n.md): writing the French in the
  change that edits the English, where the fr-CA rules live, and the review
  criteria
- [src/ui/src/i18n/README.md](../../src/ui/src/i18n/README.md): how a message is
  declared, the catalogs and the i18n gates
- [docs/web-ui.md](../../docs/web-ui.md): UI features as users see them
