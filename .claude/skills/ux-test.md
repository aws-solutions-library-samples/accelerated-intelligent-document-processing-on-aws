# Skill: UX test the web UI in a browser — GenAI IDP Accelerator

Use this when the user wants to check that the **web UI actually works for a
person** against a live stack — "run the UX tests", "does the annotation flow
work", "review the UI experience", "suggest UI improvements".

This is the one gap in the project's test coverage. There is a lot of testing
already (`make test`, `make api-test`, `make stacktest-*`, SRT, ZAP, the
benchmark suite) — none of it opens a browser. Everything below the UI can be
green while a button does nothing, a mode is unexplained, or a critical flow is
broken. That happened: correcting a document's classification in a test set was
shipped broken for several versions and was found by a customer, not by us.

**Two things to produce, and they are different:**

1. **Functional** — does the flow work? Objective pass/fail per flow.
2. **Experience** — is it *good*? Would a subject-matter expert who has never
   seen this product get through it without being told? This half produces
   suggestions, not failures, and it is the half a deterministic test can never
   do.

Report both. A flow that technically works but confuses everyone who tries it is
a finding worth having.

## Scope, deliberately small

Agreed constraints — do not exceed them without being asked:

- **Local browser, existing stack.** No CI integration, no remote/headless
  browser infrastructure, no new Feature Platform extension. Those were all
  considered and deferred; the local version gets most of the value.
- **Read-mostly, and never against a customer stack.** Use a dev stack. Flows
  that write (saving a correction, re-extracting) are fine there.
- **A throwaway user, never a real operator's account.** `ux_test_session.py`
  makes one.

## What you need

**1. A deployed IDP stack with the web UI enabled**, and `AWS_PROFILE=default`
(the ambient sandbox credentials point at a *different* account — see CLAUDE.md).
The stack's **region** matters: a stack is invisible from any other region, and
passing the wrong `--region` is the most common way to mis-invoke this. Find it
with:

```bash
AWS_PROFILE=default aws cloudformation list-stacks --region <region> \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
    --query "StackSummaries[?ParentId==null].StackName" --output text
```

**2. Browser control — check this before promising a run.** There is no browser
automation in this repo and none is assumed; it comes from an MCP server the
developer configures:

| Option | Behaviour |
|---|---|
| Playwright MCP | Launches its own clean browser. Preferred: a fresh profile per run is what you want when testing a first-time experience, and it cannot disturb the developer's open tabs. |
| Chrome DevTools MCP | Can attach to an already-running Chrome. Use when the developer specifically wants their own browser driven. |

Added with `claude mcp add <name> -- <command>`; confirm the tools are actually
present before starting, because *the failure mode here is a confident report
about a UI nobody looked at.*

**If no browser tooling is available, the whole run is blocked.** Say so plainly
and offer to walk the flows with the developer manually. **Never report a flow as
passed without having exercised it** — that reintroduces, at the reporting step,
exactly the false assurance this layer exists to remove.

The session does **not** use the developer's logged-in browser: setup mints a
throwaway user and the browser signs in with those credentials. That is
deliberate — a UX run saves edits, claims review locks and can reset labels, none
of which should happen under a real operator's identity; and the personas need
separate users anyway.

## Running it

```bash
# 1. Create a throwaway session. Prints url / email / password / group and the
#    exact teardown command. Pass the stack's OWN region.
AWS_PROFILE=default ./scripts/ux_test_session.py setup <STACK_NAME> \
    --group Admin --region <region>

# 2. Sign in at the printed url with the printed credentials, then drive the
#    flows in scripts/ux_flows.yaml.

# 3. ALWAYS tear down — copy the command from the setup output
AWS_PROFILE=default ./scripts/ux_test_session.py teardown <STACK_NAME> \
    --email ux-test-xxxx@example.invalid --region <region>
```

The URL is resolved from the stack's `ApplicationWebURL` output, so you pass a
stack name and never a URL — that output is correct under both hosting variants
(CloudFront, or the REST API stage when the SPA is served from API Gateway).

`make ux-test STACK_NAME=<stack>` prints the same instructions, for
discoverability via `make help`.

Setup temporarily enables `ALLOW_ADMIN_USER_PASSWORD_AUTH` on the UI app client
so a known password can be set non-interactively; teardown restores whatever was
there before. **Run teardown even if the test fails** — otherwise the stack keeps
a user with a known password and a modified auth-flow list.

For a persona other than Admin, re-run setup with `--group Annotator` (etc.).
The scoped-annotator flows are worth doing as an annotator specifically: they see
different navigation and a subset of test sets, and reviewing that as an Admin
misses exactly the confusion an annotator hits.

## Flows

`scripts/ux_flows.yaml` — each entry has an id, a persona, setup preconditions,
steps, objective `expect` criteria and subjective `ux_watch` prompts. Run the
`p0` flows first.

Ids match the user stories in the ground-truth correction QA plan on purpose, so
a finding can be referenced the same way in both. Add flows to the YAML, not
here.

Some flows need a document that is *wrong* in a specific way (e.g. 6.1 needs a
misclassified document). The YAML says how to create one. If you cannot establish
a precondition, report the flow as **blocked** with the reason — not as passed,
and not as failed.

## Judging the experience

Beyond each flow's `ux_watch` notes, apply these. They are chosen because each
one has already bitten this product:

1. **Is the current mode obvious?** A read-only field that looks like an
   editable one that happens to be greyed out is a real complaint about this UI.
   If you cannot tell whether you are viewing or editing, say so.
2. **Is model output distinguishable from human-authored truth?** These look
   alike here and mean opposite things. A machine draft presented as verified
   ground truth is the worst possible confusion in this product.
3. **Does a number explain itself?** An accuracy figure with no sample size, or a
   metric whose name only makes sense if you know the evaluator, is a number the
   reader cannot act on.
4. **Is the next action discoverable without documentation?** If completing a
   flow required you to already know where something was, that is a finding.
5. **Does an error say what to do?** A raw stack trace, an opaque code, or a
   permanent spinner are all findings. Spinners especially: several bugs here
   presented as a UI that would not move.
6. **Is anything colour-only?** Status conveyed by colour alone fails for a
   colour-blind reviewer.
7. **Does the work feel finite?** For a queue of hundreds of documents, can the
   reviewer see progress and stop cleanly?

Prefer a small number of specific, actionable observations over an exhaustive
list. "The re-extract button doesn't say it will discard confirmed labels until
after you click it" is useful; "improve the information architecture" is not.

## Reporting

Report every flow attempted, including the ones that passed — a report listing
only problems cannot be told apart from a report that only ran two flows.

```
🖱️  UX test report — <stack>, <persona>, <date>

Flows
  ✅ 5.1  Correct a wrong field value          pass
  ❌ 6.2  Correct a wrong class and re-extract  FAIL — <what happened>
  ⚠️  4.1  Work the queue worst-first           pass, 2 UX findings
  ⏭️  12.1 Scoped annotator cannot reach more   blocked — <precondition>

Functional failures
  6.2  <what you did> → <what happened> → <what should have happened>

UX findings                                    (suggestions, not failures)
  4.1  <observation> → <suggested change>

Not covered
  <flows not run, and why>
```

State the stack, the persona and the date, because a UX report is a snapshot and
a stale one read as current is worse than none.

## What not to do

- **Do not report a flow as passed without exercising it.** If the browser was
  unavailable, the whole run is blocked. Say that.
- **Do not fix what you find in the same pass.** The report is the deliverable;
  fixing mid-run means the report describes code that no longer exists. Offer
  the fixes afterwards.
- **Do not restyle the UI on a hunch.** Cloudscape conventions and
  `.claude/skills/frontend-ui.md` govern; a suggestion that fights the design
  system is not an improvement.
- **Do not leave the throwaway user behind.** See teardown above.
