# Skill: UX review the web UI in a browser — GenAI IDP Accelerator

Use this when the user wants the web UI **looked at by a person's standards** —
"let's test the UI", "review the UX", "does the annotation flow make sense",
"suggest UI improvements", "walk the annotation flow".

This is the one gap in the project's test coverage. There is a lot of testing
already (`make test`, `api-test`, `stacktest-*`, SRT, ZAP, benchmarks) and none of
it opens a browser, so everything below the UI can be green while a button does
nothing or a mode is unexplained. That is not hypothetical: correcting a
document's classification in a test set shipped broken for several versions and
was found in the field.

**This is primarily a visual review, not an acceptance-test suite.** The
deliverable is *feedback a designer or engineer can act on*, with functional
breakage reported when you trip over it. Weight it that way: a flow that works
but confuses everyone who tries it is the finding this exists to produce.

**Drive the browser and look at screenshots.** Never report on a screen you did
not load.

---

## Setup — one time, then never again

Assumes a **disposable dev stack**. Do not point this at anything in production
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

### 2. Launch a debug Chrome with its own profile (once)

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.cache/chrome-idp-ux" \
    --no-first-run --no-default-browser-check >/dev/null 2>&1 &
```

Verify before going further — this must return JSON, not 404 and not nothing:

```bash
curl -s http://127.0.0.1:9222/json/version
```

**The dedicated `--user-data-dir` is mandatory, not a preference.** Chrome ≥136
**silently ignores `--remote-debugging-port` on the default profile**: Chrome
starts normally, but the port never answers and nothing explains why. This is a
deliberate Chrome hardening — remote debugging on the everyday profile would
expose every signed-in cookie to any local process — so it is not going to come
back. Do not offer "reuse your normal profile" as an option; it does not work on
any current Chrome.

Consequence, worth stating to the user up front so it is not a surprise: **the
reviewer signs into the stack once in this profile.** The profile persists at
`~/.cache/chrome-idp-ux`, so it is a one-time cost, not per-run.

If the port does not answer, the usual cause is a Chrome already holding it.
Check with `lsof -nP -iTCP:9222 -sTCP:LISTEN`, and never start a second Chrome on
a port another Chrome holds — they split across IPv4 and IPv6 on the same number
and the browser can hang.

### 3. Get the stack URL and make sure they are signed in

```bash
AWS_PROFILE=default ./scripts/ux_test_session.py url <STACK_NAME> --region <region>
```

`AWS_PROFILE=default` because the ambient sandbox credentials point at a
*different* account (see CLAUDE.md). A stack is invisible from the wrong region;
if the name is not found the script lists the IDP stacks it can see.

Then have the user open that URL in the debug Chrome and sign in as themselves.
First run in a fresh profile means an actual sign-in; after that the profile
remembers it.

**Known bug on a cold sign-in:** a fresh profile can land on a blank page after
valid credentials (a credential-fetch race the app does not recover from). A
reload fixes it. If it happens, that is not your review finding — it is already
written up; just reload and carry on.

### Gotchas, all of them measured rather than assumed

- **Chrome ≥136 ignores `--remote-debugging-port` on the default profile** —
  silently, so it looks like the flag worked. A dedicated `--user-data-dir` is
  required. See step 2.
- **`--remote-debugging-port` has no effect on an already-running Chrome** either;
  a second launch hands off to the existing process and CDP stays off.
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

This creates a real Cognito user with a known password in a live pool, so
**always run the teardown it prints** — nothing else expires that account. The
stack's app client is *not* modified: `admin-set-user-password --permanent` is a
user-pool admin API and ignores the client's `ExplicitAuthFlows`, and the browser
signs in over SRP, which the UI client already allows. For every other persona,
skip this entirely — the reviewer's own account is fine on a disposable stack.

---

## Where the use cases come from

**Whatever the user asks for, first.** "Look at the annotation flow", "review the
config editor", "here are three things users struggled with" — take it and
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
