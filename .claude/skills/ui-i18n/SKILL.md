---
name: ui-i18n
description: "Use when adding, editing or reviewing web UI text and its French (Canada) translation, and for the release catch-up."
---

# Web UI translations — GenAI IDP Accelerator

Use this skill when a change adds, edits or removes user-facing text in `src/ui/`,
adds a product term, or is a review of such a change, and before a release is cut.
The web UI ships in English and French (Canada). A message's translations are
added, updated or removed in the change that adds or edits its English, and the
coding agent that edited the screen writes the French, since it knows where the
text shows. How a message is declared, the catalogs and the gates are in
[the i18n README](../../../src/ui/src/i18n/README.md); this skill is the procedure.

## In the change that edits the English

1. Declare the text with react-intl, as
   [Writing a message](../../../src/ui/src/i18n/README.md#writing-a-message) says: an
   explicit dotted `id`, the English as `defaultMessage`, and a `description` that
   says where the text shows and what each argument holds. Run
   `make ui-i18n-extract`.
2. List what to translate:
   `mkdir -p scratch && make ui-i18n-todo LOCALE=fr-CA > scratch/todo.json`
   (`scratch/` is gitignored). The list holds the entries the catalog lacks and the
   stale ones, whose English changed after they were translated, each with its
   English, its description, the glossary rows it uses and, when stale, its old
   translation.
3. Give each entry its French as a `"message"`, by the rules below. A long list can
   be written and ingested in parts: an entry needs only its `id` and `message`.
4. Write them: `make ui-i18n-ingest LOCALE=fr-CA FILE=scratch/todo.json`. A message
   the build's parity check rejects is refused with the reason and its entry keeps
   its text; correct it and run the same command again, which leaves what it already
   wrote as it is. An entry whose English changed after the list was made is
   refused too: list it again with `make ui-i18n-todo`.
5. When the French cannot be written in this change, delete the stale entries, and
   any dead ones, instead, so the English shows: `make ui-i18n-drop-stale LOCALE=fr-CA`.
   A missing translation never fails a job.
6. `make check-ui-i18n-catalog` passes; `make ui-i18n-coverage` shows what is left.

Never edit `fr-CA.json` by hand, its `sourceHash` least of all, and do not use
`make ui-i18n-translate` (Amazon Translate) in this flow: its drafts lose the context
the screen gives you. When `fr-CA.json` conflicts in a merge because two changes
added entries side by side, keep both sides' entries as they are, which is a merge
and not an edit, then run `make check-ui-i18n-catalog`. If both sides changed the
same entry, keep neither: translate it again from `make ui-i18n-todo`.

## Writing the French

- **The rules have one source:** `STYLE_RULES` in
  [`scripts/ui_i18n_review.py`](../../../scripts/ui_i18n_review.py) and `REVISE_RULES`
  in [`scripts/ui_i18n_revise.py`](../../../scripts/ui_i18n_revise.py), the rules the
  Bedrock review and revise tools give their model. Read them before you write:

  ```bash
  python3 -c 'import sys; sys.path.insert(0, "scripts"); import ui_i18n_review as r, ui_i18n_revise as v; print("STYLE_RULES:", r.STYLE_RULES["fr-CA"], "REVISE_RULES:", v.REVISE_RULES["fr-CA"], sep="\n")'
  ```

  Write ordinary spaces before punctuation: `ui-i18n-ingest` puts in the no-break
  spaces those rules ask for.
- **The glossary is binding.** [`glossary.fr-CA.csv`](../../../src/ui/src/i18n/glossary.fr-CA.csv)
  (`en,fr-CA,note`) gives the French for each English term; use it. A product term
  the change introduces gets its row in the same change. The file holds names and
  nouns only, never verbs, as
  [Drafting a translation](../../../src/ui/src/i18n/README.md#drafting-a-translation)
  explains.
- **Keep the ICU.** Every argument, plural, select and rich-text tag of the English
  stays, with its branch keys; only the text around and inside them is translated.
  `ui-i18n-ingest` refuses a missing or extra argument or tag, or an argument of
  another kind, in the build's wording. It does not compare plural or select branch
  keys, so keep those yourself.

## Reviewing a change's translations

Read the diff under `src/ui/src/i18n/` beside the English each entry translates, and
flag:

- English added or edited in `en.json` with no matching change in `fr-CA.json` and
  no `make ui-i18n-drop-stale`;
- a product term with no glossary row, or a glossary row that is a verb;
- English left in the French;
- a meaning the English does not have, or part of it lost;
- a term other than the glossary's;
- the wrong register (`tu`, a familiar tone) or another region's usage, against the
  rules above;
- an argument, plural, select or tag the French changed;
- text likely to be cut off where the English already fills its place, since French
  runs longer;
- an entry whose English changed but whose French did not (only its `sourceHash`
  moved): check that the French still says what the new English says.

The review is advisory. Only `make check-ui-i18n-catalog` blocks a merge: on a stale
entry, an entry the build rejects (for example a changed argument or tag), or a dead
one. A missing translation never does.

## Before a release

[prepare-changelog.md](../prepare-changelog/SKILL.md) holds the catch-up before the cut and
where the coverage figure goes, and [release-validation.md](../release-validation/SKILL.md)
and [ux-test.md](../ux-test/SKILL.md#reviewing-in-french-canada) the run of the release's
flows in French (Canada).
