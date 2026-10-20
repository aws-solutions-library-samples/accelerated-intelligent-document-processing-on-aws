# Work the Backlog — standing hazards and bounds

Reference for `work-the-backlog.md`. Split out of it because the coordinator reads that file in full at every invocation, including after each compaction, and its first ground rule is that coordinator context is the scarce resource. Everything here is load-bearing for an agent **writing or reviewing a fix** and not for a coordinator deciding what to dispatch, so it is pointed at from the briefs rather than carried in the loop's own context.

**Put this file in every fixer and reviewer brief.** The hazards are the measured ones, and most of them are shapes where a wrong result looks exactly like a right one.

## Standing hazards, all observed here

- **A test that cannot fail is the dominant defect class.** Five measured shapes:
  a fixture injecting a field the service never sends; an expected value derived
  from the fixture under test; an assertion behind an unmet guard; a probe that
  inherits the environment it is testing; a fixture that never reaches the call
  it asserts on. Only executing the mutation finds any of them.
- ⚠️ **A guard that enumerates spellings instead of implementing its stated rule.**
  This was the single most common defect in one run — **four** separate agents wrote
  one, and a reviewer walked the original defect straight back past every one. The
  worst case: of 11 spellings probed, **9 passed both of the author's rules**, including
  an annotated assignment that spelled out in full the very construct the author's
  stated residual claimed to cover; another was defeated by putting a verbatim copy of
  the offending code in a sibling directory, because the collector used a non-recursive
  `glob`. The remedy is to write the rule about **capability** — "this module cannot
  import the decoder", not "this line must not match these patterns" — and to put
  "attack your own guard before the reviewer does" in every brief. A guard protecting a
  gate is the highest-risk instance, because a vacuous one there reads as protection
  that is not present.
- **A vacuous test can hide inside a correct authority.** One test derived its fault
  codes from the botocore service model, which is the right authority — but an empty
  derived list collects as a pytest *skip*, not a failure, so replacing the derivation
  with `[]` left the suite green and took the whole guarantee with it. Deriving from the
  authority is necessary and not sufficient: assert the derived set is non-empty.
- **"Measured against X" is a claim to check.** Three documents in one run said
  conditional-write behaviour was measured against DynamoDB's expression engine when it
  was measured against `moto`, and that matters precisely because the property under
  test *is* expression-language behaviour — the one thing a `moto` test cannot establish
  about itself. Require every brief to name the engine, and treat a mismatch as a
  finding rather than a wording nit.
- **A conflict-marker probe can be silently broken by a missing trailing newline.**
  `CHANGELOG.md` here ends without one, so a planted `<<<<<<<` appended directly
  concatenates onto the final line and fails a `^` anchor — measured twice, matching
  0 of 1 and 2 of 3. Plant with a leading newline, and prove the probe fires before
  trusting a clean result. Use `^={7}( |$)` rather than `^={7}`, which excludes the
  decorative 20-to-78-character `=` rules in six files structurally, with no exemption
  list.
- **Counting a log is not reading it.** Two measured ways to get a wrong number: ANSI
  colour codes in summary lines break a naive tally (61 invocations and 7778 passes,
  against a true 62 and 7997), and a `Makefile` recipe's apparent invocation count can
  include `@#` prose comments that merely mention the variable. Strip ANSI, and
  reconcile against the log's own per-target lines.
- **A gate count that rises is as suspicious as one that falls.** When a merge result
  reported 20 and 7 more passes than the author measured, the right response was to
  attribute each delta exactly to the test files the merge brought in — which confirmed
  both figures. An unexplained delta in either direction is a finding.
- ⚠️ **A gate figure without its base commit is not a reference, and handing one to an
  agent manufactures false evidence.** A coordinator here gave an agent "62 invocations,
  8,800 passed, 0 failed" as the expected baseline. The figure had been measured seven
  minutes before a large merge landed; the agent matched it exactly, and **the match is
  what made a pre-merge measurement read as evidence about the post-merge tree**,
  concealing four failures. Quote the commit with every number, and treat a figure that
  arrives without one as unmeasured. This is the same self-consistent-and-false shape as
  the stale-green CI mark and the truncated issue count — three instances of it in one
  day, which is why it is stated as a rule rather than an anecdote.
- **Ancestry is not identity.** `is_relative_to(root)` accepts a sibling
  worktree nested under the root. This has bitten three separate controls here.
  Compare roots by equality.
- **A gate that measured nothing must not report success.** Two instances, both
  fixed and worth reading as the template: the coverage ratchet exiting 0 with no
  report, and on a report from a run that never finished (#1190), and `lint-cicd`
  skipping the UI lint on an unchanged checksum (#1152).
- **`[skip ci]` runs no gate on either platform.** It let three defects reach
  `develop` in one session. Now refused by a hook — do not override it.
- **Pydantic's default `extra="ignore"` silently drops an undeclared key.** Three
  separate defects here trace to it, including one where a probe got a confident
  wrong answer because it wrote a key at the wrong nesting level (#1134).
- **A green CI mark can be stale.** GitHub never re-runs a PR's workflows when
  the base moves, and the merge guard cannot see a run that never happened.
- ⚠️ **A mocked paginator never terminates, and the cost lands on the host rather
  than on the test.** `MagicMock` auto-creates any attribute, so a
  `LastEvaluatedKey` read on one is truthy forever. A reviewer's probe built this
  way reached **79 GB resident** and froze the whole host for 145 minutes. See the
  memory note in section 5 and the `ulimit` rule in section 2.
- ⚠️ **A hung agent and a frozen host are indistinguishable from inside the
  session.** Under swap thrash a `sleep 240` returns `waited` with exit 0 after 145
  minutes, five agents go quiet at once, and nothing in any transcript reports an
  error. `free -g` and `dmesg | grep oom-kill` are the only place the cause exists,
  which is why the memory watchdog is a standing child and not a diagnostic you
  reach for afterwards.
- ⚠️ **One agent can void every other agent's measurements at once**, and the
  coordinator is the only actor positioned to notice. A single `pkill -f` on a suite
  pattern reached five concurrent agents here. When it happens, the response is not to
  re-read the logs — it is to name the window and tell every agent in it to void and
  re-run, with a specific warning to any doing mutation work that a kill from outside
  and an uncaught mutation produce the same evidence.
- ⚠️ **A turn that ends with nothing tracked ends the run**, and it looks exactly
  like a turn that ended because the work was done. Five and a half hours were lost
  to one of these. Name the live child before ending a turn.
- ⚠️ **Evidence files go in a sibling directory, not inside the worktree — and this
  repository's gates make that a correctness matter rather than tidiness.** Several
  gates discover their inputs **by content**, not by filename, and an untracked file
  is still a file: `scripts/discover_templates.sh` finds anything declaring
  `AWSTemplateFormatVersion`, so a byte-identical copy of `template.yaml` kept under
  the worktree as a restore-point added **one** to the count of templates discovery
  finds — content discovery cannot tell a restore-point copy from a template — and
  produced **seven red tests in suites the PR never touched** — three
  well-architected counting tests, two log-group tests, the markdown-link gate and
  the exemption-registry discovery test. Being untracked does not help, because
  discovery reads the filesystem; only being **gitignored** or **outside the tree**
  does, and `.logs/` is neither. ⚠️ **Note this cuts against the `/tmp` rule** — the
  reason not to use `/tmp` is that it is a tmpfs competing with the test processes for
  RAM, so the answer is a sibling path on real disk (`$HOME/wt/<runId>/m<pr>-logs`),
  not a path inside the checkout. Say this in the brief, because the failure
  signature is indistinguishable from a PR defect and the agent that hit it was
  following its instructions literally.

---

## What this skill does not make safe

Bounded on purpose, so nobody reads the sections above as a guarantee of
unattended operation.

**It does not make the coordinator's judgement reliable.** Every control here
verifies *code*. None verifies the premises the coordinator is operating on, and a
wrong premise is silent and self-consistent — see the check-in section for the
worked example. The mandatory check-in is a mitigation, not a fix.

Four further coordinator errors are recorded here because each survived every
code-level control and was caught only by the user asking a plain question:

- **Idling the loop during a promotion CI wait**, having read "wait for its checks" as
  "stop". Nothing was wrong with any gate; the run simply stopped producing. The
  mechanism is worth separating from the misreading, because the misreading is what a
  rule can fix and the mechanism is not: the wait was expressed by **ending the turn**,
  which in this harness is indistinguishable from finishing. The gate had gone green 28
  minutes in and the session sat for 5 h 32 m.
- **Reporting a policy as working from a quiet interval** rather than from an audit.
  Filing had gone to zero for two hours and then produced eight issues in ninety
  seconds; the audit that would have caught it costs one API call.
- **Repeating a subordinate's figure without a measurement behind it** — "8
  pre-existing failures" when the measured number at the base was 4, the difference
  being that the agent had locally installed a tool the host lacked. Numbers arriving
  in a report are claims until someone re-measures them, and the coordinator is the
  actor most likely to launder one into the record.
- **Optimising inside an item instead of asking whether the item was worth it.** A
  gate-over-a-gate fix ran to 1.4M tokens and three review rounds; every individual
  decision in it was defensible, every gate stayed green, and the coordinator's
  response to the dead end was to write the user a careful two-option question about
  the design. The user's reply was "is this worth the cycles?" — and it was not,
  measurably, from figures the agent had reported hours earlier. **A well-framed
  question about how to finish something implies it is worth finishing**, which is the
  premise that had quietly stopped being true. The controls are in section 1 ("What
  does this shield today?") and section 2c (three spellings is a stop signal), but the
  habit is the thing: when an item stops converging, re-ask its value before its
  design.

The pattern in all four is the same and it is worth naming: **the coordinator's
errors are about process and reporting, not about code, so a green tree proves nothing
about them.** Ask at every check-in what you are treating as established without having
measured it.

**What actually catches them is another reader, and the self-audit is the instrument
least able to.** The self-audit shares, by construction, whatever error it is looking
for. Measured across one day of this loop, four coordinator-level errors surfaced — the
idled promotion wait; an open-issue count truncated by `gh`'s default limit and
presented as a total; an issue comment whose recommendation would have deleted the only
pointer to a working capability; and a gate figure quoted to an agent without its base
commit — and **all four were caught by a second reader going over the first one's
output.** None by a gate, none by the self-audit. One of them had already cost 5 h 32 m
of run time before anyone looked.

So if a second session is available during a long run, the highest-value thing it can do
is read the coordinator's reports adversarially: recompute every count from a call with
an explicit `--limit`, re-run one measurement per report rather than accepting the
figure, and check any "X was dropped" or "X is broken" claim against the commit graph
before a decision rests on it.

⚠️ **Bound that second reader to reading and recomputing.** It must not merge, close,
re-triage or dispatch. A second session that starts doing the loop's work is a second
coordinator, and every sequencing guarantee here — merges serialise, one actor holds the
order — assumes exactly one. Observed: a reviewing session drifted from checking reports
into verifying merge decisions and relaying check-ins, which duplicated the coordinator's
job and put two actors on one queue. The value is in the reading; the risk is in
everything past it.

**It does not survive compaction losslessly.** The state file preserves the
mechanical state; it does not preserve why a ranking was chosen, what an agent
was told, or a decision's reasoning. A resumed loop is correct but shallower than
one that never compacted. Prefer finishing a cycle and handing over to a fresh
session over letting one session run through several compactions.

**The liveness invariant keeps the loop running; it does not keep the session
alive.** A tracked child re-invokes you when it finishes, which covers every wait the
loop creates itself. It does not cover the session being interrupted, the process
being restarted, or an agent dying without a notification.

An external keep-alive is now a usable remedy for the first of those, which it was not
when this paragraph was first written. The objection then was that a timer pings the
session and the only two unsolicited prompts this session had ever received killed every
background agent it had. **Both halves of that have since been measured and neither
holds on CLI 2.1.296.1183**: a firing `ScheduleWakeup` left a live subagent running
across two trials, and a message typed mid-turn displaced the running command to the
background without touching the subagent. So a booked wakeup is the preferred child, and
the heartbeat `sleep` is the fallback — see the `ScheduleWakeup` subsection in section 0
of the main skill for the method and for what the measurement does not cover.

What still has no cover is the **process** dying, since every mechanism here lives inside
the session. Recovery from that is a human noticing, and what makes it cheap is the state
file plus the journals.

**It does not bound review quality.** A nested reviewer that reads the diff and
agrees costs the same as one that re-runs every mutation, and only the second is
worth having. The brief demands the second; nothing enforces it. Read each report
for whether measurements are actually quoted, and send it back when they are not.

**It does not know when a fix is wrong in a way tests cannot see.** Every fix here
is verified against the repository's own gates. A fix that satisfies the gates and
is still wrong about the product — the right code for the wrong requirement — is
invisible to this loop and is exactly what the halt-and-ask list exists for.

**It does not make issue text safe, only handled.** Section 0b bounds what an
attacker-controlled report can reach, but the residual is real: an issue that argues
convincingly for a change *outside* the protected surfaces, in prose indistinguishable
from a good bug report, will be worked like any other. Provenance is the only signal
that scales, and `CONTRIBUTOR` is available to anyone who has landed one trivial PR.
The mitigations are a narrower blast radius, a gated promotion that keeps it out of
`develop`, and a human at the high-risk boundary — not immunity.

**The capability boundary is prose, and a restricted agent type would not fix that.**
Section 0b tells fixer agents they have no AWS credentials and make no outbound fetch,
and an injected agent is precisely the one that ignores being told. Because `Bash` is
required, and reaches the network and the filesystem, no tool list closes it and a
`PreToolUse` denylist only raises the bar. **The control that would actually work is not having readable
long-lived credentials on the host the loop runs on**, which is outside this
repository's reach and so is a residual rather than a task.

**It cannot enforce anything server-side.** Nothing in this repository can stop a
merge; see "Visible is not blocking" in `CLAUDE.md`. The loop merges on its own
measurement, so its discipline *is* the gate.
