# UI Acceptance Testing (UAT)

The only test tier that renders a page. It drives the **deployed** Web UI in a real
browser and asserts that **tasks are completable** — not that pixels match.

## Why this tier exists

Every other tier is headless by construction, so none can observe whether a user can
actually get something done:

| Tier | What it drives | Blind to |
|---|---|---|
| `make ui-test` (Vitest/jsdom) | mounted components, mocked API | real auth, real data, navigation, multi-step flows |
| `make api-test` | REST `/op/<field>` per Cognito group | anything the UI never wires up, or wires wrong |
| Primary CI suite (steps 3–14) | `idp-cli` + boto3 against a live stack | the UI entirely |
| `stacktest-*` probes | deploy variants + one HTTP GET on `ApplicationWebURL` | "200 OK" ≠ "usable" |

A document stuck in `EVALUATING` forever, a HITL gate silently blocking an unattended
run, a nav entry that leads nowhere, a `Viewer` shown a Delete button the API will
refuse — all of these deploy clean, pass every unit test, return 200, and answer RBAC
correctly. They are what this tier catches.

## Running it

```bash
# Full cycle: deploy a throwaway stack, test it, tear everything down
make uat-testing ADMIN_EMAIL=you@example.com REGION=us-west-2

# Against a stack you already have. NOT deleted afterwards — you own its lifecycle.
make uat-testing STACK_NAME=my-idp-stack

# Against a URL only. Zero AWS calls; the users must already exist.
UAT_ADMIN_USER=... UAT_ADMIN_PASSWORD=... \
UAT_VIEWER_USER=... UAT_VIEWER_PASSWORD=... \
  make uat-testing BASE_URL=https://d123.cloudfront.net/

# Useful flags
make uat-testing ADMIN_EMAIL=... KEEP=1        # leave the stack up for debugging
make uat-testing STACK_NAME=... GREP="Viewer"  # one scenario group
make uat-testing STACK_NAME=... HEADED=1       # watch the browser
make uat-lint                                  # typecheck the harness; no stack needed
```

`stacktest-uat` is an alias, so it appears alongside the other live-stack tests.

### Teardown is unconditional

`scripts/uat/run_uat.py` runs the test phase inside `try/finally`. A crash, a failed
assertion, or Ctrl-C still deletes the Cognito test users and the stack **the script
created**. Two deliberate exceptions:

- `KEEP=1` preserves everything, and prints the `make delete-stack` command to clean up.
- A stack passed via `STACK_NAME` is **never** deleted. Only the test users are removed.

If a deploy fails half-way, the stack name is recorded *before* the deploy starts, so
the partial stack is still torn down rather than orphaned.

## What it produces

Under `scratch/uat-report/` (override with `REPORT_DIR=`):

| Artifact | Contents |
|---|---|
| `test-results/uat-report.md` | Human-readable summary — attachable to a ticket or PR |
| `test-results/uat-results.json` | Machine-readable, for tracking trends across runs |
| `test-results/junit.xml` | For CI test-result ingestion |
| `playwright-report/` | Interactive HTML report with traces |
| `test-results/<scenario>/` | Trace, video and screenshot for each failure |

### Interaction cost

Beyond pass/fail, the report records **clicks, field edits and navigations per
scenario**, plus the route path walked:

```
| Scenario                    | Result  | Duration | Clicks | Field edits | Navs |
|-----------------------------|---------|----------|--------|-------------|------|
| Admin can open the document…| ✅ pass | 3.2s     | 4      | 0           | 1    |
```

This is **not** an assertion — nothing fails because a number rose. It is a usability
signal that ordinary tests have no opinion about: if "upload a document and see its
fields" starts costing 11 clicks instead of 6, no test breaks but the product got
worse. Tracking it in `uat-results.json` makes that reviewable.

Counting happens **in the browser** (a capture-phase listener injected via
`addInitScript`), not by wrapping Playwright calls, so it sees every click a test
causes — including the extra ones a dropdown or modal forces. Note that field edits
count `input` events rather than keystrokes: Playwright's `fill()` sets a value and
dispatches `input` without ever firing `keydown`.

## How authentication works

`run_uat.py` creates one Cognito user per role via `scripts/rbac_common.py`
(`create_cognito_user`), the same helpers `make api-test` uses, then exports
`UAT_<ROLE>_USER` / `UAT_<ROLE>_PASSWORD`. Playwright's `setup` project signs in
through the real Amplify form once per role and saves `storageState`; every scenario
reuses it.

Two design notes:

- **Form login, not token seeding.** It needs no AWS credentials at all — only a
  username and password — which is what lets the tier run from a stock CI runner
  with no IAM role.
- **Permanent passwords.** `create_cognito_user` sets `--permanent`, which avoids the
  `NEW_PASSWORD_REQUIRED` challenge. A user created any other way will hang the
  sign-in form on a forced password change.

If setup fails, dependent scenarios are reported **skipped** rather than passing, so a
broken login is never silently green.

## Adding a scenario

1. Create `src/ui/e2e/specs/NN-thing.spec.ts`.
2. Import from `../fixtures/test-base` (**not** `@playwright/test`) — that is what
   installs the metrics instrumentation.
3. Pick a role: `test.use({ storageState: statePath('Admin') })`.
4. Assert **task completion**, not appearance.

```ts
import { test, expect } from '../fixtures/test-base';
import { statePath } from '../fixtures/roles';
import { gotoDocumentList } from '../helpers/documents';

test.use({ storageState: statePath('Admin') });

test('Admin can do the thing', async ({ page }) => {
  await gotoDocumentList(page);
  await expect(page.getByRole('columnheader', { name: 'Document ID' })).toBeVisible();
});
```

### Selector policy

Prefer accessible roles and the nav's own link text. The UI has **no `data-testid`
attributes**, and the harness deliberately does not add any: Cloudscape renders real
semantic HTML, and role/label selectors are both stable and self-documenting.

Add a `data-testid` only when a role selector has demonstrably failed, and say in the
PR which flake motivated it.

Handles that are stable because they are user-visible and config-driven:

| Selector | Defined in |
|---|---|
| `getByRole('link', { name: 'Upload Document(s)' })` | `components/genaiidp-layout/navigation.tsx` |
| `getByRole('columnheader', { name: 'Document ID' })` | `components/document-list/documents-table-config.tsx` |
| `getByRole('heading', { name: 'Upload Documents' })` | `components/upload-document/UploadDocumentPanel.tsx` |

### Waiting

Never `sleep`. Use `waitForDocumentTerminal()` from `helpers/documents.ts`, which polls
the app's own Status text until it leaves the in-flight set and treats "never reached a
terminal state" as the failure. `IN_FLIGHT_STATUSES` there mirrors `ABORTABLE_STATUSES`
in `documents-table-config.tsx` — **keep the two in step**, or a document in a newly
added status will be wrongly treated as finished.

## Scope

- Chromium only. Not visual regression, not a11y auditing, not cross-browser.
- Not a replacement for the CLI suites — UAT is slower and coarser.
- Scenarios that mutate shared state (configuration, test sets) stay read-only against
  a shared stack; a scenario that rewrites configuration would corrupt the environment
  for every later scenario.
- `workers: 1` by default for the same reason.

## Flake policy

`retries: 1`. A scenario that only passes on retry is reported as **flaky** rather than
green, so it stays visible. Quarantine (don't delete) any scenario that flakes twice in
a release, and fix the wait rather than raising the timeout.

## CI

`.github/workflows/ui-uat.yml` runs the suite nightly and on `workflow_dispatch`
against a **persistent** UAT stack. It needs no AWS credentials — only the Cognito
username/password pairs, held in the reviewer-gated `uat` Environment.

There is deliberately **no `pull_request` trigger**: this repo is public, and a fork PR
must never be able to reach those secrets. CI does not manage the stack lifecycle;
use `make uat-testing` locally for deploy→test→teardown.
