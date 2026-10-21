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

## Verify with
- `make ui-lint`. Use `FORCE=1` if the checksum cache reports `SKIPPED`.
- `make ui-test`, `make ui-build`, `make codegen-check`
- `cd src/ui && npm run typecheck`

## Sources
- [.claude/skills/frontend-ui.md](../skills/frontend-ui.md): the stack, directory
  layout, context, auth and transport patterns, and lint config
- [.claude/skills/pr-review.md](../skills/pr-review.md): the UX review criteria
- [.claude/skills/ux-test.md](../skills/ux-test.md): browser-driven UX testing
  against a live stack
- [docs/web-ui.md](../../docs/web-ui.md): UI features as users see them
