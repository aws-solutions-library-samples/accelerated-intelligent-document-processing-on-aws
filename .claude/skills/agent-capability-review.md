# Agent Capability Review — re-verify what every model can read, receive and send

Use this skill when the user asks to **audit the agents**, "check what the models can
reach", "re-verify the capability manifest", or at each release from
`release-validation.md` (tier A). Also run it whenever a new agent, a new tool set, a
new MCP integration (client or server) or a new place that renders model output
lands, without waiting for the release.

It is the audit behind `scripts/tests/agent_capabilities.yaml`. That manifest
declares, per surface, what the code around a model can **read**, what text reaches
the model as **input** and where that text comes from, and where the model's
**output** can go. `scripts/tests/test_agent_capabilities.py` keeps the manifest
complete (every derived surface has an entry, every named control and test exists),
but its docstring is explicit that it checks declarations, not truth: an entry that
understates what a model can read passes it. This skill is what checks the truth,
one surface at a time, against the code.

The method is the one that found the properties the per-file scanners could not
(`repo-quality-review.md`, Class 3): start from the outcome, walk every component on
the path, and write down which component, if any, decides.

## Hard constraints

- **Read-only.** No edits to the tree, no commits, no pushes, no comments on issues
  or PRs, no AWS calls. Subagents get the same constraint verbatim.
- **Output goes under `scratch/` only.** `scratch/` is gitignored. The audit's
  findings describe how a model could be steered, and that text is not published:
  not in a commit, a PR body, an issue, a CHANGELOG line or a release record. What
  leaves `scratch/` is the manifest edit that records the behaviour (a `reads` term,
  a `control`, a `finding` saying nothing limits a channel), written as behaviour.
- **Every claim is checked against the code.** A docstring, a README, a row under
  `security/threat-modeling/` or the manifest itself is a claim to check, not evidence.

## Inputs

| Input | Where it comes from |
|---|---|
| The surface list | `python3 -c "import sys; sys.path.insert(0,'scripts/tests'); import test_agent_capabilities as c; [print(n) for n in sorted(c.manifest()['entries'])]"` |
| Each entry's declaration | `scripts/tests/agent_capabilities.yaml`, and its `vocabulary` for what each term means |
| The previous audit | the newest `scratch/agent-capability-review/<date>/summary.md`, if one exists |

Run the gate first. A manifest that fails `test_agent_capabilities.py` has an
unregistered or stale surface, and auditing it audits something other than the tree:

```bash
python3 -m pytest -q scripts/tests/test_agent_capabilities.py \
  scripts/tests/test_analytics_role_grants.py
```

## How to run it: one subagent per surface

1. Create `scratch/agent-capability-review/<YYYY-MM-DD>/`.
2. **Fan out one subagent per manifest entry**, in parallel, read-only. Give each:
   - the entry's YAML block and the vocabulary;
   - the read-only and `scratch/`-only constraints above, verbatim;
   - the three questions below, and the instruction to answer each with file and
     line, from the code;
   - the instruction to label every claim **verified** (read in the code on the path
     that runs, or measured), **inferred** (follows from code read, but the path
     that runs was not traced end to end) or **unverified** (believed, not checked);
   - the instruction to write its result to
     `scratch/agent-capability-review/<date>/<entry>.md` and return only a summary.
3. **The three questions**, per surface:
   - **Reads.** Which tools does the model hold, which AWS or HTTP calls do they
     make, with which role's credentials (the entry's `roles`), and what can those
     credentials reach beyond what the tool uses? Which of that is private?
   - **Inputs.** What text reaches the model: the user's message, conversation
     history, document text or images, extracted values, reporting rows, logs,
     configuration a discovery run wrote, remote tool results? Trace each back to
     where it was first written.
   - **Outputs.** Where can the output go: rendered in a page (and what that page
     may load), into the Code Interpreter, to an MCP client or server, into a saved
     configuration a later model reads, into a PR or MR comment? For each external
     channel, which code limits it, and does the cited `test` actually assert that
     limit (revert the control in a scratch copy and see the test go red, if the
     test is cheap to run)?
4. **Each subagent also checks the entry's `control` and `test` lines:** that the
   control is on the path that runs (not only defined), and that the test exercises
   it. A control that exists but is never consulted is a `repo-quality-review.md`
   Class 1 finding.
5. **Consolidate yourself.** Read the per-entry files, then look across them for
   paths that cross entries (the orchestrator writing a question for an agent with
   an external channel; a discovery run writing configuration a later agent reads;
   an MCP tool returning what another model renders). Those are the findings no
   single subagent sees.

Use subagents only when the user or `release-validation.md` has authorized the Agent
tool for the run; otherwise do the entries one at a time yourself and say so.

## The diff against the manifest

The deliverable is a diff, not a narrative. For each entry, write one of:

| Result | Meaning | What to do |
|---|---|---|
| `match` | every term, control and test in the entry was verified | nothing |
| `understated` | the code reads, receives or sends something the entry does not declare | propose the manifest edit (add the term; add a `finding` if nothing limits a new external channel) |
| `overstated` | the entry declares something the code no longer does | propose removing it |
| `control-not-on-path` | a named control exists but the path that runs does not call it | propose a `finding`, and report it as Class 1 |
| `test-does-not-assert` | the cited test passes with the control reverted | propose a test that goes red, or drop the citation |
| `unverified` | the subagent could not establish it | list what would establish it |

Also diff the role declarations: for each grant under the manifest's `roles`, is the
`uses` code path real, and is an `unused` grant really unused? Record which of those
answers was measured and which inferred, as the manifest itself does.

## Output contract

Write `scratch/agent-capability-review/<date>/summary.md`:

```markdown
# Agent capability review — <commit> — <date>

## Gate
test_agent_capabilities.py / test_analytics_role_grants.py: <passed/failed counts>
Derived surfaces: <n>   Entries: <n>   Open findings in the manifest: <n>

## Diff against the manifest
| Entry | Result | Proposed manifest edit | Evidence label |

## Findings that cross entries
<each with the path through the components, and which component decides>

## Since the previous review
<findings closed, findings new, findings unchanged across two reviews>

## Not assessed
| Entry | Why |
```

Then report to the user: how it was run (how many subagents, or single-agent), the
counts per result, the proposed manifest edits, and any finding that crosses entries,
each labelled verified, inferred or unverified. Proposed manifest edits are made in a
normal change afterwards, with `PENDING_CONTROL_FINDINGS` in
`test_agent_capabilities.py` updated to match; this skill does not make them.

## Limits

- It reads the code; it does not drive a model. Whether a model actually follows an
  instruction carried in a document is a live question for a stack test, and a
  `match` here does not answer it.
- Third-party code (the SEED generator, the DeepWiki server, a customer's External
  MCP server) is read only as far as this repository describes it. Say so in the
  entry's result rather than marking it verified.
- The derivation's reach is stated in `test_agent_capabilities.py`'s docstring. A
  model call with no tools whose output is rendered is a `declared` surface, and a
  new one of those is found only by reading, so ask each subagent to name any it
  passes on the way.
