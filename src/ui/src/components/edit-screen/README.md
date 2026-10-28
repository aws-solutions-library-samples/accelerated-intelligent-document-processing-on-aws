# The edit page

Developer notes for `src/ui/src/components/edit-screen/`. What users see is described in
[docs/web-ui.md](../../../../../docs/web-ui.md).

A document is edited on one page, in one of three modes: a **direct edit** of any
processed document, a **Production** review-queue item, and a **Test Studio** item. The
page itself holds no mode-specific code; each mode supplies a model that the page
renders.

## The pieces

| Piece               | File                                                   | What it does                                                                                                                                                                                                                                                                                                                      |
| ------------------- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The page            | [`EditPage.tsx`](EditPage.tsx)                         | Renders an `EditModel`: the layout and breadcrumbs, the load and error states, the bar, the shared editor, the Edit \| View switch, History, Skip's reason, Cancel, the Process Changes confirmation, Edit Page Class and the exit. It has no mode or role branching, which `__tests__/edit-page.no-mode-branching.test.ts` pins. |
| The model           | [`edit-model.ts`](edit-model.ts)                       | The types: the document header, the notices, the bar, the editor, the board, Skip, Process Changes, Claim, the baseline, the failure surface and the navigation.                                                                                                                                                                  |
| The editor session  | [`use-editor-session.ts`](use-editor-session.ts)       | What the page and a model both need to know about the open editor: its controller, unsaved and saving state, the open section and its working copy, the view, and the running action (`run`).                                                                                                                                     |
| The bar and reports | [`EditScreen.tsx`](EditScreen.tsx)                     | The bar's layout, the title, the Processing Report and Summary dialogs.                                                                                                                                                                                                                                                           |
| The editor          | [`SectionEditor.tsx`](SectionEditor.tsx)               | The field editing and change tracking, the same in every mode.                                                                                                                                                                                                                                                                    |
| The board           | [`use-page-class-board.tsx`](use-page-class-board.tsx) | Edit Page Class as a dialog over the model's `EditBoard`, with the save error shown inside it.                                                                                                                                                                                                                                    |

## One adapter and one model per mode

Each mode is a three-line shell that creates the session and renders the page with its
model. The model hook holds every mode-specific decision; the adapter holds the server
calls.

| Mode             | Shell                    | Model                            | Adapter                                                                               |
| ---------------- | ------------------------ | -------------------------------- | ------------------------------------------------------------------------------------- |
| Direct edit      | `DocumentEdit.tsx`       | `direct-edit-model.tsx`          | `document-edit-adapter.ts`, `document-edit-save.ts`                                   |
| Production item  | `ProductionItemEdit.tsx` | `production-item-edit-model.tsx` | `production-item-edit-adapter.ts`                                                     |
| Test Studio item | `TestSetItemEdit.tsx`    | `test-set-item-edit-model.tsx`   | `test-set-item-edit-adapter.ts`, with the editor slots in `test-set-editor-slots.tsx` |

A section result is read through `section-result.ts` (`fetchSectionResult`) in every mode.

## Adding an action

1. Add the action to `EditAction` in `EditScreen.tsx`, and its button or menu item to the
   bar there.
2. Add its show and disabled-reason fields to `EditBar` in `edit-model.ts`, and the handler
   to `EditReady` (a function that does the work and resolves the toast, or the exit).
3. Add the flow to `handleAction` in `EditPage.tsx`: run it through `session.run`, so the
   bar is busy meanwhile and a failure is reported where the model's `failures.surface`
   says.
4. Fill the fields in each model where the action applies.
5. Add the control to `MUTATION_CONTROLS` in `scripts/tests/test_ux_story_relay.py` for
   every mutation it sends.

## The board and the slots

`EditBoard` describes Edit Page Class for one mode: its pages (thumbnails signed only while
the board is open, or pages rendered from the source document), every section in board
terms, the class options, whether the class may change, the consequence text, an optional
check before opening, and `save`, which throws with the message shown inside the board.

The Test Studio model takes its editor content from `useTestSetEditorSlots`: the
baselines as sections, the page images, the class config the labels were written under,
the board over the set's 0-based page indices, and the slots the editor renders (the
pages-moved warning, the Re-extract notice and the pages field, the no-fields text, the
page-image notices).
