# Skill: UX review the web UI in a browser — GenAI IDP Accelerator

Use this when the user wants the web UI **looked at by a person's standards** —
"let's test the UI", "review the UX", "does the annotation flow make sense",
"suggest UI improvements", "walk the annotation flow".

This is the one gap in the project's test coverage. There is a lot of testing
already (`make test`, `api-test`, `stacktest-*`, SRT, ZAP, benchmarks) and none of
it opens a browser, so everything below the UI can be green while a button does
nothing or a mode is unexplained. That is not hypothetical: correcting a
document's classification in a test set shipped broken for several versions and
was found by a customer.

**This is primarily a visual review, not an acceptance-test suite.** The
deliverable is *feedback a designer or engineer can act on*, with functional
breakage reported when you trip over it. Weight it that way: a flow that works
but confuses everyone who tries it is the finding this exists to produce.

**Drive the browser and look at screenshots.** Never report on a screen you did
not load.

---

## Setup — one time, then never again

Assumes a **disposable dev stack**. Do not point this at anything a customer
uses: the review saves edits, re-extracts documents and can reset labels.

### 1. Install the browser MCP server (once)

```bash
claude mcp add chrome-devtools -- npx -y chrome-devtools-mcp@latest \
    --browserUrl http://127.0.0.1:9222 --redactNetworkHeaders
```

`--redactNetworkHeaders` matters: the session carries Cognito bearer tokens and
they would otherwise land in the transcript.

**Then restart Claude Code** — MCP servers load at session start, so the tools
are absent until it restarts.

### 2. Relaunch Chrome with remote debugging (once)

Fully **quit** Chrome (⌘Q on macOS — closing the window is not enough), then:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 >/dev/null 2>&1 &
```

No `--user-data-dir`: this is the user's **normal profile**, so their tabs,
cookies and existing IDP sign-in all come back. The signed-in session lives in the
profile on disk, not in the running process — quitting loses nothing. Suggest they
add the flag to how they normally launch Chrome so this never comes up again.

Verify before going further:

```bash
curl -s http://127.0.0.1:9222/json/version    # must return JSON, not 404
```

### 3. Get the stack URL and make sure they are signed in

```bash
AWS_PROFILE=default ./scripts/ux_test_session.py url <STACK_NAME> --region <region>
```

`AWS_PROFILE=default` because the ambient sandbox credentials point at a
*different* account (see CLAUDE.md). A stack is invisible from the wrong region;
if the name is not found the script lists the IDP stacks it can see.

Then have the user open that URL in the debug-enabled Chrome and sign in as
themselves. **That is the whole setup** — no separate profile, no second window,
no throwaway credentials.

### Gotchas, all of them measured rather than assumed

- **`--remote-debugging-port` has no effect on an already-running Chrome.** A
  second launch hands off to the existing process and CDP stays off. Hence the
  quit in step 2.
- **`chrome://inspect/#remote-debugging` does not work with this tool.** It opens
  the port but serves no HTTP discovery endpoints (`/json/version` → 404), and
  `--autoConnect` and `--wsEndpoint` both time out against it. The toggle is also
  transient — it switches itself off. Do not recommend it.
- **Never launch a second Chrome on a port another Chrome already holds.** They
  split across IPv4 and IPv6 on the same port number and the browser can hang.
- If `list_pages` reports it cannot connect, re-check step 2 rather than trying
  other flags.

### The exception: reviewing as an Annotator

An annotator sees different navigation and only their assigned test sets, and you
cannot get there by reusing an Admin session. Only for that:

```bash
AWS_PROFILE=default ./scripts/ux_test_session.py setup <STACK> \
    --group Annotator --region <region>
# ... review, then run the teardown command it prints
```

This temporarily widens the app client's auth flows to set a known password, so
**always run the teardown it prints**. For every other persona, skip this
entirely — the reviewer's own account is fine on a disposable stack.

---

## Where the use cases come from

**Whatever the user asks for, first.** "Look at the annotation flow", "review the
config editor", "here are three things my customer struggled with" — take it and
go. No file needs editing to review something new, and a use case someone brings
today is worth more than one written down months ago.

`scripts/ux_flows.yaml` is the **fallback and the memory**, not the definition of
scope. It earns its place by holding the two things a runtime prompt cannot:

- **Preconditions.** Flow 6.1 needs a *misclassified* document and no shipped test
  set has one. A user asking for a classification review will not think to say
  that, and without it the review silently looks at the happy path only.
- **Regression memory.** The flows that broke before — class correction most of
  all — keep getting looked at even when nobody remembers to ask.

So: use the user's list when there is one, fall back to the file's `p0` flows when
they just say "test the UI", and read the file's `setup` notes either way in case
the thing they asked about needs a fixture that does not exist yet.

Worth writing a recurring use case into the file **after** reviewing it, once you
know what it actually needs. Adding it beforehand tends to encode a guess.

## Running the review

`scripts/ux_flows.yaml` holds the fallback flows: id, persona, priority, steps,
and `ux_watch` prompts. Treat it as **a list of things to go and look at**, not a
checklist to tick. Start with `p0`.

Work one flow at a time: load the screen, take a screenshot, look at it, say what
you notice. Prefer `take_snapshot` for structure and `take_screenshot` when the
finding is visual (spacing, hierarchy, whether something reads as a button).

Some flows need a document that is *wrong* in a specific way — 6.1 and 11.1 need a
misclassified document, and no shipped test set has one. The YAML says how to make
one. If a precondition cannot be met, report the flow **blocked** with the reason,
not passed and not failed.

**Check what is actually deployed.** These flows cover recent work; if a feature
is only on an unmerged branch, the stack will not have it and the flow is
**blocked**, not broken. Say which, so nobody chases a phantom bug.

## What to look for

Beyond each flow's `ux_watch` notes. Each of these has already bitten this
product:

1. **Is the current mode obvious?** A read-only field that looks like a greyed-out
   editable one is a real complaint about this UI.
2. **Is model output distinguishable from human-authored truth?** They look alike
   here and mean opposite things. A machine draft styled as verified ground truth
   is the worst confusion available.
3. **Does a number explain itself?** An accuracy figure with no sample size, or a
   metric named after the evaluator's internals, cannot be acted on.
4. **Is the next action discoverable without documentation?** If you needed to
   already know where something was, that is a finding.
5. **Does an error say what to do?** Stack traces, opaque codes and permanent
   spinners are all findings. Spinners especially — several bugs here presented as
   a UI that would not move.
6. **Is anything colour-only?** Status by colour alone fails a colour-blind
   reviewer.
7. **Does the work feel finite?** For a queue of hundreds of documents, can the
   reviewer see progress and stop cleanly?

Give a few specific, actionable observations rather than an exhaustive list. "The
re-extract button doesn't say it discards confirmed labels until after you click
it" is useful; "improve the information architecture" is not.

## Reporting

Report every flow you opened, including the ones that were fine — a list of only
problems is indistinguishable from a review that stopped after two screens.

```
🖱️  UX review — <stack>, <persona>, <date>

Looked at
  ✅ 5.1  Correct a field in the annotation queue     works; 2 UX findings
  ❌ 6.2  Change a class and re-extract               broken — <what happened>
  ⏭️  11.1 Run-level classification errors            blocked — not deployed

Findings                                    (ranked; suggestion, not a demand)
  5.1  <what you saw> → <what to change>

Functional breakage
  6.2  <steps> → <observed> → <expected>

Not covered
  <flows skipped, and why>
```

State stack, persona and date: a UX review is a snapshot, and a stale one read as
current is worse than none.

## Don't

- **Don't report a screen you didn't load.** If the browser is unreachable the
  review is blocked — say so.
- **Don't fix things mid-review.** The report is the deliverable; fixing as you go
  means it describes code that no longer exists. Offer fixes afterwards.
- **Don't restyle on a hunch.** Cloudscape conventions and
  `.claude/skills/frontend-ui.md` govern; a suggestion that fights the design
  system is not an improvement.
