# Flow files — how to say "go test flow XYZ"

Three ways to specify a target, in increasing order of setup cost. The cost buys you
**verification**: there is no way to confirm a task actually happened without someone
saying, once, what "done" looks like in machine-checkable terms.

| How | Setup | Verified? | Trendable? | Use for |
|---|---|---|---|---|
| `GOAL="…"` on the CLI | one sentence | ✗ `unverified` | ✗ | exploration, "go poke at this" |
| a `.yaml` file here | ~10 lines | ✓ | ✓ | release gating, regression trend |
| derived from CHANGELOG | none | ✓ | ✓ | *(not built yet)* every shipped claim |

## 1. Ad-hoc, zero config

```bash
python3 scripts/uat/agentic/run_prototype.py --stack-name <stack> \
  --goal "create a test set from the sample documents and run it"
```

You get the full report card — filmstrip, clicks, confusions, dead ends. You do **not**
get a verdict: `verification.confirmed` is `null` and the flow is marked `unverified`,
because nothing checked whether it really happened. Do not trend these; the agent's own
"I did it" is not evidence.

## 2. A flow file (recommended for anything you care about)

Drop a `.yaml` in this directory. No Python. It is reviewable in a PR and versioned with
the code.

```yaml
flow_id: create-test-set          # stable slug — the trend key. Never rename it.
claim: |
  You can create a test set in Test Studio to benchmark a configuration
  against known-good ground truth.
docs_ref: docs/test-studio.md#creating-test-sets    # lifted VERBATIM at run time
documented_steps: 4               # denominator for the complexity gap
verify:
  method: ddb_item_exists
  table_contains: testset         # matched against the stack's table names
  claim_names_key: true           # the agent must name the id it created
preconditions:                    # deterministic seeding, never an agent
  - "idp-cli upload --dir samples/"
```

If the file or anchor cannot be resolved, the WARM run **degrades to cold** and logs it
rather than testing invented prose — so a renamed heading shows up as lost coverage, not
as a false pass.

`claim` is the only thing a COLD run ever sees. `docs_ref` is read **only** for a WARM
run — and it points at a real doc anchor rather than inlining prose, so a finding filed
against the documentation is filed against what the docs actually say. Paraphrasing the
docs into the flow file produces findings about your paraphrase, which is worthless.

### verify methods

| method | confirms |
|---|---|
| `ddb_config_classes` | agent named the configured document classes |
| `ddb_document_status` | agent named the real processing statuses |
| `ddb_item_exists` | an item the agent claims to have created is in the table |
| `none` | nothing — verdict is `unverified` |

Adding a method means adding a function to `verify.py`. That is deliberate: verification
is deterministic Python, never a model call, so the vocabulary stays small and auditable.

## 3. Derived from the CHANGELOG (the end state, not built)

One flow per claim in the release section, `flow_id` from the heading slug. Zero
authoring, and coverage grows every time someone writes release notes. Blocked on the
`verify` vocabulary above being rich enough to infer — until then, the generator would
emit `method: none` for everything, which is just option 1 at scale.

## Promotion: turn an ad-hoc goal into a real flow

The intended loop, and why option 1 is not a dead end:

1. Explore with `GOAL="…"`.
2. It finds something. The report card records the route it walked and what it claimed.
3. Write that up as a `.yaml` here, with a real `verify` block.
4. It is now verified, trended, and safe to gate on.

Same shape as promoting an agentic finding into a deterministic Playwright spec — the
agent discovers, the durable artifact is written once by a human.
