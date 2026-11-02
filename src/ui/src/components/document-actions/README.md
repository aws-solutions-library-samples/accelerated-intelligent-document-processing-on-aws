# The document page's actions

Developer notes for `src/ui/src/components/document-actions/`. What users see is
described in [docs/web-ui.md](../../../../../docs/web-ui.md).

The document page (`#/documents/<key>`) has one action bar, in its page header. It is
rendered through the shared `ActionBar` (`../common/ActionBar.tsx`), the same component
the edit page's bar uses, so the two pages read the same. Panels and table rows hold
only actions about their own row, in a ⋮ menu.

## The pieces

| Piece               | File                                                                 | What it does                                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------- | -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The gates           | [`document-action-gates.ts`](document-action-gates.ts)               | Pure: for each action, whether the user is offered it and, if so, why it is disabled now.                                                                                                                                                                                                                                                                                                                                  |
| The reasons         | [`action-reasons.ts`](action-reasons.ts)                             | The disabled reasons the bar, menus, dialogs and rows show. Reasons shared with the edit page are in `../edit-screen/document-edit-status.ts` and `../common/review-status.ts`.                                                                                                                                                                                                                                            |
| The bar             | [`use-document-actions.tsx`](use-document-actions.tsx)               | The bar's controls, in order, and the dialogs they open: the reports, Processing Flow, Troubleshoot, Skip All Reviews' reason, Start Review and Use As Evaluation Baseline. It reads no configuration and no route; the page passes them in.                                                                                                                                                                               |
| Download            | [`use-document-download.tsx`](use-document-download.tsx)             | The downloads, a group in More Actions, and their options and progress dialogs.                                                                                                                                                                                                                                                                                                                                            |
| Edit Page Grouping     | [`use-detail-page-class-board.tsx`](use-detail-page-class-board.tsx) | The edit page's board (`../edit-screen/use-page-class-board.tsx`) over this document: **Save page grouping** (`updateDocumentSections`, which keeps the extracted values), or **Save and Re-extract**, which confirms the sections whose class or pages changed (`changedSections`), saves, then sends `processChanges` with those sections. A re-extract that fails after the save is reported with **Re-extract again**. |
| Re-extract Sections | [`ReextractSectionsDialog.tsx`](ReextractSectionsDialog.tsx)         | `processChanges` with the chosen sections, each with its current class and pages.                                                                                                                                                                                                                                                                                                                                          |
| Reset Page Class    | [`ResetPageClassDialog.tsx`](ResetPageClassDialog.tsx)               | `processChanges` with `classReset` for the chosen pages. A reset classifies the whole document again and rebuilds every section; the dialog says so.                                                                                                                                                                                                                                                                       |
| Edit Page Text      | [`EditPageTextDialog.tsx`](EditPageTextDialog.tsx)                   | `../pages-panel/PageTextEditorModal.tsx`, whose **Save and Reprocess** uploads the text (`uploadDocument`), then sends `processChanges` with `textModified`. A failed reprocess keeps it open with **Retry reprocess** and **Restore original text**; closed unfinished, the page shows a notice with **Retry reprocess**.                                                                                                 |
| Reports             | [`DocumentReportDialog.tsx`](DocumentReportDialog.tsx)               | Summary, Evaluation Report and Rule Validation Summary, each in a dialog, at the version the page shows.                                                                                                                                                                                                                                                                                                                   |

`../document-panel/DocumentPanel.tsx` lays out the page: the header and bar first (the
bar's test helpers read the first Header), the notices, the source document when Source
is chosen, then the widgets, which stay mounted (hidden) behind Source so Chat and the
tables keep their state.

## Offered, or disabled with a reason

An action the user's role can **never** perform is not offered: it is left out of the
bar and its menus, and a menu left with no items is left out too. An action the role
may perform but that does not apply **now** is shown disabled, with its reason.

- **Who is offered what.** `ACTION_OPERATIONS` names each action's API operations and
  `ACTION_GROUPS` the groups offered it. `__tests__/document-action-gates.rbac.test.ts`
  derives the groups from `scripts/api_rbac_expectations.yaml`: the groups every one of
  the action's operations admits, less the groups the server admits only for some
  objects (`object_scoped_roles`). So the page cannot offer an action the server refuses,
  or hide one it accepts, without that test failing. Hiding is not security: the server
  enforces every policy.
- **Why it is disabled.** The reasons are checked in one order and the first that
  applies wins: a past version is being viewed, the document is in a review queue, it is
  still processing, its configuration is loading, it is in BDA mode (the document's own
  configuration profile's `use_bda`, read with `isBdaModeConfig`), then the action's own
  availability (a report that does not exist, no pending review). BDA mode disables Edit
  Page Class as well as the actions that reprocess. The actions that call
  `processChanges` (Re-extract Sections, Edit Page Text, Reset Page Class, and the
  board's Save and Re-extract) also follow the server's rule for a caller who is not an
  Admin: refused on a finished review, and unless the caller holds the document's open
  review. `__tests__/document-action-gates.test.ts` pins each reason and the order.

## Adding an action

1. Add its id to `DocumentActionId`, its operations to `ACTION_OPERATIONS` and its
   groups to `ACTION_GROUPS` in `document-action-gates.ts`, and its gate (the reasons, in
   order) to `documentActionGates`. Put a new reason in `action-reasons.ts`.
2. Add its menu item or button to the controls in `use-document-actions.tsx`, through
   `offered(...)` for a menu item, and its handler or dialog there.
3. Add the control to `MUTATION_CONTROLS` in `scripts/tests/test_ux_story_relay.py` for
   every mutation it sends, naming the file the label is in.
4. Test it in `__tests__/document-bar.test.tsx` (where it sits, by role) and in the gates
   tests; the RBAC test picks up the new operations by itself.

## The tests

| Test                                                                                                        | What it pins                                                                                                                                        |
| ----------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `document-action-gates.rbac.test.ts`                                                                        | Each action is offered to exactly the groups the server admits                                                                                      |
| `document-action-gates.test.ts`                                                                             | Each disabled reason, and the order they are checked in                                                                                             |
| `document-bar.test.tsx`                                                                                     | The header, the bar's order, Reports, More Actions' groups, Source, Refresh, roles, the baseline copy                                               |
| `edit-page-class.test.tsx`, `edit-page-class.reviewer-lock.test.tsx`, `edit-page-class.thumbnails.test.tsx` | Save page grouping and Save and Re-extract send the right calls in order; the Reviewer-only class lock; thumbnails signed only once the board opens |
| `reextract-sections.test.tsx`, `reset-page-class.test.tsx`, `edit-page-text.test.tsx`                       | Each dialog's exact payload, consequence text, failure handling and when it is disabled                                                             |
| `skip-all-reviews.test.tsx`                                                                                 | Skip All Reviews sends the reason and note; when it is available                                                                                    |
| `detail-bar.ts`                                                                                             | The bar's test helpers, over `../common/__tests__/action-bar-helpers.ts`                                                                            |
