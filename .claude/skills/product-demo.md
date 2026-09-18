# Skill: Record a product demo video — GenAI IDP Accelerator

Use this when the user wants a **video that shows a feature to other people** —
"make a demo of the unreleased changes", "record a product demo of PR #912",
"demo the test-set editing feature for the team", "turn the 0.6.9 changelog into
a walkthrough".

This is the sibling of `.claude/skills/ux-test.md`. That skill *judges* the UI and
its video ends on a Findings card; this one *shows* the UI and its video ends on a
Key takeaways card. Same browser, same recorder (`scripts/ux_recorder.py`), same
Polly narration and captions — different purpose, different audience, and a
different rule about mistakes: a review keeps a mis-click and reports it, a demo
re-takes it.

**The run is collaborative by design.** You propose three demos, the user picks
one, you write the storyboard, the user confirms it, and only then does a browser
get driven. Do not skip the proposal step because the request looked specific:
"demo PR #912" still has three defensible cuts (the headline change, the whole PR,
the user story it fixes), and the user is the one who knows who will watch it.

**Drive the browser and record what it shows.** Never narrate a screen you did not
load, and never describe a capability the stack does not have deployed.

---

## The shape of a run

1. **Gather the material** — changelog entries, a PR/MR, or a named feature.
2. **Check the stack has it** — version and build date at the bottom of the side
   navigation; a change on an unmerged branch is not demoable on a stack that does
   not run it.
3. **Propose three storyboards** and let the user pick one (`AskUserQuestion`).
4. **Write the chosen storyboard in full** into `scripts/demo_storyboards.yaml`
   and confirm it once ("record as written" / "change something").
5. **Prepare fixtures and rehearse unrecorded.** Processing takes minutes; the demo
   should open on a finished state, and every click should already have landed
   once before the recorder is running.
6. **Record**, one `mark --say` per chapter.
7. **Render, watch the pacing and click tables, re-take if needed.**
8. **Hand over**: the mp4, the docs entry draft, and the saved storyboard.

Only steps 3 and 4 block on the user. Everything else runs to completion.

---

## Setup

Identical to the UX review — the debug Chrome with its own profile, the
chrome-devtools MCP server, a signed-in tab, `make ux-record-deps`. Follow the
Setup section of `.claude/skills/ux-test.md` and its gotchas (Chrome ≥136 ignores
the debug port on the default profile; never start a second Chrome on a held port;
the recorded tab must stay visible and unresized). Nothing there is different for a
demo. Also as there: **a disposable dev stack, never one someone works in** — a demo
creates test sets, processes documents and deletes fixtures.

```bash
AWS_PROFILE=default ./scripts/ux_test_session.py url <STACK> --region <region>
```

Sign in as yourself. Demos are recorded as the Admin persona unless the story is
about another role; when it is, create that user with `ux_test_session.py setup
--group <Group>` and **run the teardown it prints**.

---

## Where the material comes from

Look in `scripts/demo_storyboards.yaml` first. If a storyboard already covers the
request, offer to re-record it (updated for the current version) as one of the
three proposals — a demo that exists for the previous release is the one most worth
refreshing.

### A changelog section

Read `CHANGELOG.md` — `## [Unreleased]` by default, or the `## [X.Y.Z]` section the
user names. Every entry links the doc that explains it; read those links, that is
where the user-visible behaviour is described. Then sort the entries:

- **Shows in the browser** — a new page, control, column, state or message.
  Demoable as-is.
- **Shows only in artifacts** — a `result.json` field, a Processing Report line, an
  Athena column, a CLI flag. Demoable through the page that displays the artifact
  (View Data, the Processing Report, the cost table), or not at all.
- **Invisible** — IAM scoping, a Lambda's memory, a retry window. Not demoable.
  Say so in the proposal rather than silently dropping it; the user may want a
  slide instead of a video.

### A PR or MR

Fetch it the way `.claude/skills/pr-review.md` Step 1 does — `gh pr view`,
`gh pr diff`, or `glab mr view` / `glab mr diff` — but stop there. You are learning
*what changed and why*, not producing the six-question review. Read the PR
description, the CHANGELOG hunk if there is one, and the changed files under
`src/ui/` and `docs/`. A PR with no `src/ui/` or `docs/` change is usually in the
"artifacts" or "invisible" bucket above.

A PR's change is on the stack only if the stack was deployed from that branch or a
release that includes it. Compare the branch and version shown at the bottom of the
side navigation with the PR's head; if they do not match, report **blocked — not
deployed** and stop. Deploying is a separate task the user has to ask for.

### A named feature

"Demo the annotation queue", "show how configuration versions work". Read the
feature's page under `docs/` and, if one exists, the entry in
`docs/demo-videos.md` — an existing public video means the new one should show
what changed since, not repeat it.

---

## Proposing three storyboards

Three *different* demos, not three lengths of the same one. Good axes to vary:

- **One feature, deep** — the single most visible change, start to finish.
- **What's new in this version** — a 3–4 minute tour touching several entries,
  one chapter each.
- **A person's job** — a persona getting something done (an annotator working a
  queue, an author shipping a config change) where the new features appear as
  they are needed, not as a list.

For each proposal give: a working title, who it is for, the one-sentence hook, 4–7
chapters as one line each, the fixtures it needs and whether they exist in
`samples/` or a pre-deployed test set, an estimated running time (2–4 minutes is
the target; the published demos run 2–5), and what it deliberately leaves out.

Rules for a proposal to be honest:

- Everything in it is **deployed on the stack** (step 2) and **reachable in the
  browser** by a persona you can sign in as.
- Its fixtures come from `samples/`, the pre-deployed test sets, or synthetic
  generation, unless the user has already agreed otherwise (see Privacy).
- A wait longer than about a minute is either done before recording or covered by
  `pause`. A proposal built around watching a document process is a weak one.

Present them with one `AskUserQuestion`, three options, the outline as each
option's preview. Put your recommendation first and say why in a sentence. The
user may pick, combine, or bring their own; take whatever they say and move on.

---

## Writing the storyboard

Expand the pick into an entry in `scripts/demo_storyboards.yaml` — the header of
that file documents the fields. The parts that matter most:

- **`chapters[].say`** is the narration, one or two sentences each. It is spoken
  before the action lands, so it says what the person is about to do and why it
  matters, in first person plural and present tense: "We remove the two
  documents that no longer belong in this set, without rebuilding it." Name the
  feature the way the docs name it. Never say click, uid, stack, id, or an email.
- **`chapters[].do`** is what you will actually do in the browser, precise enough
  to rehearse from.
- **`takeaways`** are the 3–5 lines that become the end card. Write them as
  claims a viewer takes away, not as a table of contents.
- **`fixtures`** lists what must exist before recording and how you will make it.
- **`not_shown`** is the honest boundary — the entries you left out and why.

Show the storyboard to the user and ask once whether to record it as written. If
they have already said "just go", do not ask again.

A storyboard is the only file under version control a demo run touches. It holds no
document content, account ids or names, so it is safe to commit; commit it with the
change it demonstrates, or on its own if the demo is of a past release.

---

## Fixtures and rehearsal

Do this before `start`, every time:

1. **Create the fixtures.** Upload and fully process the sample documents, create
   the test set, run the test run. Processing is minutes per document; the demo
   opens on the finished result, it does not watch the spinner.
2. **Walk every chapter once, unrecorded.** Take a `take_snapshot` at each step so
   you know the element you will click and what the page does next. This is where
   you find the flash message that shifts the table, the modal that needs a
   scroll, the button that is disabled until something loads.
3. **Clear the state you disturbed.** Do a real page reload, not a hash
   navigation: the app's flash messages ("Successfully deleted 1 test set")
   survive route changes and will otherwise sit in your first chapter. Then
   take a screenshot, not a DOM query, to confirm what is on screen before
   `start` — the side navigation's open state, in particular, is easy to
   misread from the DOM.
4. **Keep the context small.** `take_snapshot` of a long table costs thousands
   of tokens each time. Save it to a file and grep for the uid you need:
   `take_snapshot --filePath scratch/…/snap.txt` then
   `grep 'button "Create"' snap.txt`.

If a step **does not work** during rehearsal, the demo stops here. Report it as
functional breakage in the format `.claude/skills/ux-test.md` uses, offer a UX
review of that flow, and do not record around it. A demo that hides a broken step
is worse than no demo.

---

## Privacy — say it once, before recording

A demo is made to be shared, and a recording of a live stack shows real documents,
real file names, the signed-in user's email in the navigation, and whatever else is
on screen. The page URL is not captured (the screencast is the page, not the
browser chrome), but nothing on the page is redacted.

Default to shipped samples (`samples/`), the pre-deployed test sets and synthetic
generation. If the storyboard needs anything else — a document already on the
stack, a real test set — tell the user exactly what will be in frame and ask once.
If they confirm, record it and repeat the caveat when you hand over the path.
Everything lands under `scratch/ux-recordings/` (gitignored); **never commit the
video or attach it to a PR.**

---

## Recording

```bash
./scripts/ux_recorder.py targets                     # find the tab; MCP page ids are not these ids
./scripts/ux_recorder.py start --kind demo --stack <STACK> \
    --title "Editing test sets in place" --subtitle "Version 0.6.9" \
    --url-contains cloudfront \
    --say "<the hook — one or two sentences>"

# before EACH chapter:
./scripts/ux_recorder.py mark "<chapter label>" --say "<chapters[].say>"
#   ... then the MCP clicks / fills for that chapter

./scripts/ux_recorder.py pause      # around any wait you could not pre-empt
./scripts/ux_recorder.py resume

./scripts/ux_recorder.py stop --say "<the closing line: the takeaway and where to read more>"
```

`--kind demo` is what makes this a demo: no persona on the title card, the
`--title` and `--subtitle` lines instead of the stack name, a **Key takeaways** end
card read from `demo.md`, and `demo.mp4` / `demo.srt` as the outputs. The session
directory is `scratch/ux-recordings/demo-<title>-<timestamp>/`.

`mark` goes **before** the action: the renderer holds the frame until the narrator
has started, then the click lands. Use the MCP `click` for anything the viewer
should see happen — a programmatic `element.click()` from `evaluate_script` fires
no mousedown, so the cursor overlay draws nothing and Cloudscape popovers ignore
it. Scrolling a wide table with `scrollTo({behavior: 'smooth'})` from a script is
fine and reads well on camera. Keep the pace of a person watching, not of the
model driving — one idea per chapter, five to eight chapters, and `take_snapshot`
(not screenshots) between actions so nothing flashes on screen that the viewer
should not see.

**Clicks are drawn where they landed.** `status` counts clicks that hit nothing
interactive; `render` prints the click table. In a demo, a click on the wrong
element is a re-take, not a footnote — re-record the whole demo (it is three
minutes) rather than ship a ring drawn on the page header.

### Narration style

The opening line is the hook: what the viewer will be able to do after watching.
Each chapter says the *why* before the *what*. The closing line states the
takeaway and names the doc to read. No stack names, ids, emails, selectors or the
word "click". No findings and no hedging — a demo shows what the product does; if
something needs a caveat, it goes in `not_shown` and the docs entry, not the voice
track.

---

## After stop

1. Fill in `<session>/demo.md` — the skeleton is there: **Storyboard** (chapter
   labels), **Key takeaways** (3–5 indented lines; `render` puts the first five on
   the end card), **Fixtures**, **Not shown**.
2. Optionally edit `<session>/narration.md`; only changed lines are re-synthesised.
3. `AWS_PROFILE=default ./scripts/ux_recorder.py render --voice Ruth --dry-run`
   first. A chapter silent for more than a few seconds, or sitting at the 3× speed
   ceiling, wants a longer narration line or a `pause` next time. The narration
   can still be lengthened now: edit that chapter's line in `narration.md` to
   describe what the footage shows (the video is paced to the voice, so a longer
   line means less speed-up), then re-run the dry run.
4. `AWS_PROFILE=default ./scripts/ux_recorder.py render --voice Ruth`, then watch
   the mp4 length against the target and check the click table.
5. Write `<session>/docs-entry.md` — a ready-to-paste section for
   `docs/demo-videos.md` in the shape that page already uses:

   ```markdown
   ### <Title>
   <One paragraph: what it shows and why it matters, in the voice of the existing entries.>

   **Duration**: ~<m> minutes

   <ASSET_URL — upload demo.mp4 by dragging it into a comment on a GitHub PR or issue in this repo, then paste the https://github.com/user-attachments/assets/... URL here>

   **Related Documentation**: [<Doc title>](./<doc>.md#<anchor>)
   ```

   Say which category heading of `docs/demo-videos.md` it belongs under. The
   upload itself is manual: GitHub only mints asset URLs from a comment box, so the
   skill drafts the entry and the user attaches the file.
6. Update the storyboard's `last_recorded` with the date and the version shown in
   the navigation.

---

## Reporting

```
🎬  Demo — <title>, <date>
Source     CHANGELOG [Unreleased]: <entries> | PR #<NN> | <feature>
Stack      version <X.Y.Z> (build <date>), <persona>
Video      scratch/ux-recordings/demo-<...>/demo.mp4   <m:ss>, <N> chapters — not for commit
Captions   demo.srt   Chapters in demo.md
Docs entry scratch/ux-recordings/demo-<...>/docs-entry.md → docs/demo-videos.md, under "<category>"
Storyboard scripts/demo_storyboards.yaml → <id>

Fixtures   <what was created, and that it was cleaned up>
Not shown  <entries left out, and why>
Seen on the way  <anything broken or odd during rehearsal — a UX-review finding, not a demo chapter>
```

Repeat the privacy caveat if the recording shows anything beyond shipped samples.

## Don't

- **Don't record before rehearsing.** The first time a click lands must not be on
  camera.
- **Don't demo around a broken step.** Stop, report, offer a UX review.
- **Don't narrate findings.** The video shows; the docs entry and `not_shown`
  qualify.
- **Don't show what is not deployed.** Blocked is a valid result; invented is not.
- **Don't commit anything under `scratch/`**, and don't paste the mp4 into a PR.
- **Don't hide, minimize or resize the recorded tab** while recording, and don't
  record with DevTools or a bearer token in frame.
