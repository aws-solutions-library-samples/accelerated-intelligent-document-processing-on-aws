# Release Validation — the whole battery for a published release, in one request

Use this skill when the user says something like **"validate the 0.6.8 release"**,
"run the full release validation", "run everything against the release — functional,
upgrade, govcloud, headless, benchmarks, security", or "is the release good?". It is
the umbrella over every per-tier skill in this directory: it fixes the tier list, the
order, the stacks, the three documents that come out, and the PRs that carry them.

The user gives one sentence. You run every tier below, write the three records, open
the PRs, and report back in the shape under **Reporting**. Do not ask which tiers to
run — the tier list is the deliverable. Ask only for the two things you genuinely
cannot infer (see **Inputs**), and only if they are not already in the request.

> **What "validated" means here.** `docs/release-validation/README.md` defines the
> tier list and the record format; `docs/release-validation/v0.6.6.md` is the worked
> example to copy. The artifact under test is the **published** template
> `idp-main_<VERSION>.yaml`, not a local build — every stack is created from that
> object except the two transform tiers, which must publish from source to apply the
> transform. Check the template's `Description` says `(v<VERSION>)` before you start.

## Inputs

| Input | Where it comes from |
|---|---|
| `VERSION` | the repo `VERSION` file; must match the newest `## [x.y.z]` in `CHANGELOG.md` |
| `PREV` | the previous **published** release — the next `## [x.y.z]` heading down in `CHANGELOG.md`. Not a `.dev`/`.rc` |
| Template URLs | `grep -nE "idp-main_<VERSION>\.yaml" CHANGELOG.md` — never hand-type them |
| `REGION` | `us-west-2` unless told otherwise (the deploy-target memory / `test-upgrade.md`) |
| `ADMIN_EMAIL` | `git config user.email` unless the user gave one |
| Live stack for the security tier | an existing stack already at `v<VERSION>` if one exists (check `Description` via `describe-stacks`); else the benchmark stack once it is up |

Nothing else needs asking. VPC wiring, the seller region, and the benchmark stack
are all decided below.

## Preflight — ten minutes that save a day

1. **Credentials and account.** Stale env creds shadow the profile, so unset them
   in the same command:
   ```bash
   unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_CREDENTIAL_EXPIRATION
   AWS_PROFILE=default aws sts get-caller-identity      # must be the deployment account
   ```
   Every AWS call from here on is `AWS_PROFILE=default`. `unset` does not persist
   between Bash tool calls — prefix each call that fails with it.
2. **Python environment.** `source .venv/bin/activate` (or `lib/idp_common_pkg/.venv`)
   and `export PYTHONPATH=$PWD/lib/idp_common_pkg`. A sibling `idp2`/`idp3`
   checkout is commonly installed editable — a bare `python` tests the WRONG tree.
   Install the test extras before believing any failure (`full-test-battery.md`
   gotcha 1).
3. **Run SRT before you build — and run setup first.** `CI=1 python scripts/srt/setup.py
   && make srt-scan`. `make srt-scan` alone merges into whatever gitignored
   `.srt/issues.json` is already there; if that state predates suppressions committed
   to `scripts/srt/issues.json`, the scan **re-opens them** and the gate reads red
   (v0.6.8: 10 false open HIGH). `make srt-setup` copies the committed register in —
   but without `CI=1` it prompts for an AWS profile, and interrupting that prompt
   deletes SRT's config and scanner venv. Scanning a tree with `.aws-sam/` output is
   fine (those hits are labelled LOCAL-ONLY), but scan first, `publish.py` second.
4. **Confirm the published artifact.** `curl -sI <TO_URL>` returns 200, and its
   `Description` line contains `(v<VERSION>)`.
5. **Check IAM role headroom — it is the binding constraint on parallelism.**
   `aws iam get-account-summary --query 'SummaryMap.[Roles,RolesQuota]'`. Each IDP
   stack creates **~121 roles** (158 with every feature on); budget ~160 per in-flight
   stack. At v0.6.8 the account sat at 1,740/2,000 and five concurrent deploys failed
   two tiers on `RolesPerAccount`. The quota (`L-FE177D64`) is adjustable and a request
   to 3,000 was auto-approved in minutes — request it up front if headroom is under
   two stacks' worth. Note the standing stacks in *other regions* too: roles are global.
6. **Tell the user the budget** before the first deploy: the full battery stands up
   and tears down roughly eight stacks and took **most of a working day** for
   v0.6.6 and about seven hours for v0.6.8, with real Bedrock/Textract spend on the
   benchmark side (corefast is 19 cells × 3 repeats × 3 docs per side, ~$32 a side).

## The tiers

Every row is mandatory. A tier you cannot run is reported as **NOT RUN with the
reason**, never dropped from the table. Long-running commands go **detached**
(`setsid nohup … > scratch/<tier>.log 2>&1 &`) and are polled — the tool kills
background children when memory is tight, and a killed deploy leaves a stack up.

### A. Offline (no AWS) — run first, in parallel with the first deploy

| Tier | Command | Pass | Per-tier skill |
|---|---|---|---|
| SRT | `make srt-scan` (BEFORE publish) | 0 open/reopened HIGH in tracked source | `srt-security-scan.md` |
| Unit + integration suites | `make test` | all test roots green — there is **no** standing failure set | `full-test-battery.md` |
| Lint | `make lint-cicd` | exit 0 | — |
| Dependency audit | `make dep-audit` | nothing at or above HIGH | — |
| Type check | `make typecheck` | compare **totals** against develop's baseline (4 errors / 51 warnings on 2026-09-11); file placement shifts with the installed deps. Report the delta, not the raw count | `full-test-battery.md` |
| Build + package | `publish.py … --clean-build` in a **clean env** (`env -i HOME=$HOME PATH=/usr/local/bin:/usr/bin:/bin AWS_PROFILE=default bash -lc '…'`) | template validation + cfn-lint clean on all packaged templates | `full-test-battery.md` |

The build is needed anyway: the transform tiers publish from source, and the
self-deploy stack-tests take its `idp-main.yaml` URL as `TEMPLATE_URL`.

### B. Security (against a live stack at `v<VERSION>`)

```bash
make security-results STACK_NAME=<stack> REGION=<region>     # SRT + RBAC static + RBAC dynamic + ZAP, then curates
```

Writes `security/test-results/<VERSION>/` (MANIFEST + four reports). Pass `SKIP_SRT=1`
and let it curate the tier-A scan already in `.srt/issues.json` — the wrapper calls
`make srt-scan` without `make srt-setup`, so run on a stale `.srt/` it would stamp
the false-red from preflight step 3 into the snapshot. Review the redactions before
committing. Gate values to compare against `security/test-results/<PREV>/`:
SRT open/reopened HIGH, RBAC static fail/warn, RBAC dynamic check count and hard
fails, ZAP High/Medium. **Any movement from PREV is a finding**, up or down.
Skill: `curate-security-results.md`; per-test triage in `api-rbac-test.md` and
`run-stack-tests.md`.

### C. Deploy variants (each self-deploys from the published template, validates, tears down)

```bash
URL=<published idp-main_<VERSION>.yaml>
make stacktest-hosting-global  TEMPLATE_URL=$URL REGION=<region> ADMIN_EMAIL=<email>
make stacktest-waf             TEMPLATE_URL=$URL REGION=<region> ADMIN_EMAIL=<email>
make stacktest-hosting-private TEMPLATE_URL=$URL REGION=<region> ADMIN_EMAIL=<email> VPC_ID=… SUBNET_IDS=… LAMBDA_SG_ID=… APIGW_VPCE_ID=…
make stacktest-jobsapi         TEMPLATE_URL=$URL REGION=<region> ADMIN_EMAIL=<email> VPC_ID=… SUBNET_IDS=… LAMBDA_SG_ID=… APIGW_VPCE_ID=…
make stacktest-seller                                    # REGION deliberately UNSET — see below
```

- **VPC wiring.** Reuse the account's purpose-built stack-test VPC: the one whose
  security group is named `idp-stacktest-vpce-sg`, with two private subnets and an
  `execute-api` interface endpoint. Discover it with the `describe-*` calls in
  `run-stack-tests.md` and **state in the record which VPC was used** (redacted).
  Only if that VPC is missing do you stop and ask; never create one unasked.
- **Seller: leave `REGION` unset (us-east-1).** `tests/stacktest.sh` still does not
  forward `--region` to `dynamic_activation_test.py` (open since v0.6.6); in any
  other region three refusal assertions pass **vacuously** on API Gateway's SigV4
  403. A us-east-1 run is a real pass. If it has been fixed when you read this,
  say so in the record and delete this bullet.
- **Seller: delete the leftover log group first, and again after.** Every run leaves
  `/aws/apigateway/idp-seller-entitlement-citest-activation` behind — the template
  does **not** retain it, so it is most likely re-created by API Gateway's buffered
  access-log delivery after the stage is gone, and the teardown script does not
  sweep it — and CloudFormation's
  `AWS::EarlyValidation::ResourceExistenceCheck` then fails the *next* run at
  changeset creation. `aws logs delete-log-group --region us-east-1 --log-group-name
  /aws/apigateway/idp-seller-entitlement-citest-activation` before you run, and in
  cleanup. Open since v0.6.8.
- **Concurrency.** At most **two** stack deploys in flight at once, and never more
  than the IAM role headroom allows (preflight step 5). Six at once is what burst
  the account's control planes (Logs create-consistency, CodeBuild role-trust
  propagation, IAM CreatePolicy rate) and produced failures unrelated to the code —
  the reason these left CI. With the quota raised, v0.6.8 ran three at once without
  incident; the role count, not the rate limits, was what actually bit.
- **After the VPC tiers, sweep orphaned ENIs** (`describe-network-interfaces
  --filters Name=status,Values=available` in that VPC; delete the ones whose
  description names a deleted `idp-*` stack). They accumulate silently and will
  eventually block a VPC/SG delete.

Skill: `run-stack-tests.md`.

### D. Template transforms — the only tier that deploys a *transformed* template

```bash
make transform-deploy-test-all REGION=<region> ADMIN_EMAIL=<email>     # --headless then --govcloud, real document each
```

Both run `idp-cli deploy --headless|--govcloud --from-code . --wait`, process
`samples/lending_package.pdf`, and tear down. Report every `✓` check and whether the
document actually processed (never `SKIP_DOC_TEST=1` for a release record).

> ⚠️ **The `--govcloud` result is a commercial-partition result.** It proves the
> CloudFront-free, API-Gateway-hosted template deploys and processes documents. It
> proves **nothing** about partition ARNs, GovCloud model availability or the BDA
> project rejection (#676/#677 were invisible to a commercial run). The record must
> carry this caveat verbatim and list "real GovCloud" under *What was NOT
> validated*, unless the run was `REGION=us-gov-west-1` with GovCloud credentials.
> Also state that the Knowledge Base was disabled in the `--govcloud` run (runner
> default) unless `--with-knowledge-base` was passed.

Skill: `transform-deploy-test.md`.

### E. In-place upgrade `PREV → VERSION`

The customer path, on a **fresh throwaway stack** (`IDPUpg<PREV><VERSION>`):
deploy the published PREV template → process `lending_package.pdf` → `update-stack`
to the published VERSION template with `UsePreviousValue=true` on every parameter →
process the same document → diff the outputs. Record: parameter-set diff (added /
removed / newly required), `UPDATE_COMPLETE` with no rollback, `Description` now
`(v<VERSION>)`, and the per-section field / null / **value-difference** counts (the
v0.6.6 record's table). Tail the `UpdateDefaultConfig` Lambda during the update; it
is where upgrades deadlock. **Pull the S3 outputs before teardown** — an
`UPDATE_COMPLETE` that silently broke extraction is still a regression, and you
cannot prove otherwise once the bucket is gone.

Skill: `test-upgrade.md` (including rollback-deadlock recovery).

### F. Benchmarks — the release A/B **and** the full documentation refresh

Tier F has two halves and both are mandatory. F1 answers "did this release regress?"
and F2 answers "what should a customer configure on this release?" — the second is the
one that was skipped at v0.6.8, leaving `config-guidance.md` headed *Release: v0.6.7*
and the newly selectable premium models with no published measurement at all. The
deliverable is **every document under `docs/benchmarking/` regenerated from data
measured on the published build**, not just the A/B entry.

Everything in F runs on the reference stack at `v<VERSION>` (the upgraded stack from
tier E). Run `run_matrix.py --estimate` for **every** suite first, add them up, and
tell the user the run count and rough cost before launching — F2 is the expensive
part of the whole battery (v0.6.7's refresh was 20+ suites). Do not trim the list to
save money without saying so in the record; a trimmed refresh is reported as a
trimmed refresh.

**F1 — release A/B `PREV → VERSION`.** Same published templates, byte-identical
configs, only the code version differs. `corefast`, `repeats: 3`, `--native-upload`,
both sides; `aggregate.py --compare` against the PREV summary; `--figures-compare`;
promote `baseline.json`. Reuse the upgrade stack from tier E for the NEW side; run
PREV on that stack *before* `update-stack`, or on a sibling stack created from the
same PREV template (say which, and read wall-time deltas accordingly). Because the
configs are built from the *current* repo's defaults, a fix that ships as default
**config** (prompt text, a default knob) is on both sides and cancels out — state
that explicitly and point at the prerelease comparison for the combined effect.

- **If `docs/benchmarking/releases/v<VERSION>.md` already exists** as a *prerelease*
  audit, do **not** add a sibling file. Re-run against the published template and
  rewrite it as the release entry, keeping the prerelease measurements as a clearly
  labelled section. One file per release, never overwritten by a later release.
- Results → `benchmarks/results/v<VERSION>/corefast/`; figures →
  `images/benchmark-v<VERSION>-*.png`; a row at the top of `releases/README.md`.

**F2 — guidance-paper refresh on the published build.** These are the suites the
paper's sections are computed from; each result set goes under
`benchmarks/results/v<VERSION>/<suite>[__<override>]/` per `RETENTION.md`:

| Paper section | Suite(s) | What it establishes |
|---|---|---|
| §2 configuration matrix | `coresynth` (19 cells × 7 synthetic docs) | the cross-config grid: OCR × mode × assessment, the v0.7 feature arms |
| §2 real-world accuracy | `core` (= `coresynth` + the RealKIE and OCR-benchmark reference corpora) — run `core` *instead of* `coresynth` when budget allows, it is a superset; also `detection-real-corpora` and `forcing-real-corpus` if those studies are cited | labelled-corpus accuracy, not just synthetic exact-GT |
| §2.1 / §6 standing hazards | `intconf`, `advverify` (`--set extraction_model=sonnet5`) | the two silent list-loss hazards, with repeats — the paper must say whether each is still open |
| §3 scaling | `scaling` (simple vs advanced across 100→3,200 rows) — expect simple-mode cells above the single-response limit to fail; that *is* the finding | where each mode's completeness cliff sits on this release |
| §4 cost level and variance | `cost` (n≥5, same 400-row doc), at the control model **and** `--set extraction_model=sonnet5` (the shipped default) | cost mean ± CV per cell, so the guide can say "budget as a range" |
| §7 knob sections | the feature A/B suites whose knob changed in this release (`enforcement`, `forcing`, `splitcost`, `boundary*`, `multiinstance`, `sizerab`, …) — read the CHANGELOG and re-measure every knob it touched | one-knob deltas on identical code |

**F3 — model coverage: premium *and* lightweight, with a when-to-use verdict.**
The models a customer can select must each have a measured row in the guide, and
the guide must say, from the data, when the cheap end is enough and when the
premium end earns its price. Three pieces:

| Piece | Runs | Output in the guide |
|---|---|---|
| **extraction-model sweep** — `coresynth --set extraction_model=<m>` for every value in the matrix's `sweeps.extraction_model` axis (today `nova_lite`, `nova_pro`, `sonnet5`, `sonnet5_1m`, `opus5`, `astra`; Sonnet 4.6 is the control) | 6 × 133 | a model table: recall, cell accuracy, typed accuracy, cost/doc, wall, mean confidence and % below 0.9 — same grid, same docs, so rows are comparable |
| **premium head-to-head** — `astravalue` (Sonnet 5 vs GPT-6 Astra vs `global.` Astra, simple + advanced, 4 docs 3–26 pages, 5 repeats) and `astracap` (the 66-page ceiling, 25-page-shard arms) | 100 + 12 | "Is a ~4× model worth it?" — answered as a ratio (accuracy gained per dollar) per document size, including the capacity band where only the 1M-context model completes in one request |
| **classification / confidence model sweeps** — `coresynth --set classification_model=<m>` and `--set confidence_model=<m>` over their axes (`nova_2_lite`, `sonnet5`, `haiku45`; `nova_lite`, `nova_2_lite`, `sonnet5`) | 3–4 × 133 each | which lightweight model is enough for classification and the confidence pass, and what upgrading it buys |

Write the verdict as a **model-selection section** in `config-guidance.md` §5: a
table with one row per selectable model (cost tier, accuracy, completeness at each
document size, cost/doc, latency, context ceiling in pages) and a short
recommendation per document profile — small forms, ≤100-row statements, 400–800-row
statements, 1,000+-row / 25+-page packets — naming the cheapest model that is at
ceiling accuracy for that profile and the point at which a premium model is the only
one that completes. If a premium model does **not** beat the default at ceiling, the
guide says so; the suite is built to be able to say "no". Every claim carries `n`,
the spread, and `cells_compared` (a null column scores 1.000 — see
`benchmark-metric-blindspots`).

**F4 — regenerate the docs.** After F1–F3, every file in `docs/benchmarking/` is
brought to `v<VERSION>`:

- `config-guidance.md` — header (`Release`, `Stack`, `Pricing` sha), §1–§7 re-cited
  from `benchmarks/results/v<VERSION>/…`, the new §5 model-selection section, and
  Appendix A listing the exact directory behind every section. A section whose data
  was **not** re-measured keeps its old numbers and says so in its first sentence.
- `index.md` — the suite table and the **"Which models are actually measured"** table
  (flip ❌/"added" to ✅ with the run date; a model still unmeasured stays ❌ with why).
- `releases/v<VERSION>.md` + `releases/README.md` (F1).
- `classification-confidence.md`, `prompt-caching.md`, `feature-multi-instance.md`,
  and any other paper in the folder whose numbers came from a knob F2 re-measured.
- Figures: `aggregate.py --figures` / `--figures-compare`; copy what you cite to
  `images/benchmark-v<VERSION>-<name>.png`.

Skill: `run-benchmarks.md` (procedure, cross-version config compatibility, retention,
honesty rules). Read `benchmarks/matrices/METHODOLOGY.md` before writing a number.

## Ordering that fits in a day

1. Preflight → `make srt-scan` → start `publish.py --clean-build` detached.
2. While it builds: `make test`, `make lint-cicd`, `make dep-audit`, `make typecheck`.
3. Deploy the **upgrade** PREV stack (long) and the **transform headless** run, two in flight.
4. When a `v<VERSION>` stack exists: `make security-results` against it.
5. Stack-tests two at a time; seller last (different region, no VPC).
6. Upgrade stack: baseline doc → update-stack → post doc → keep it as the benchmark stack.
7. Benchmark F1 both sides (the PREV side must run **before** the upgrade — schedule
   it between the baseline document and `update-stack`, or use a second stack).
8. Benchmark F2 + F3 on the `v<VERSION>` reference stack, suites back to back
   (`--max-inflight 6`; the harness serialises within a suite, so run two suites at
   once at most — Bedrock quotas are shared with every other stack in the account).
   This is the long pole: budget **a second day** for it and say so up front.
9. Aggregate, compare, figures; regenerate every `docs/benchmarking/` file (F4);
   write the three records; sweep ENIs; tear down all but one reference stack at
   `v<VERSION>` (say which one you left up and why).

## Outputs — three records, two PRs

| Record | Path | Notes |
|---|---|---|
| Release validation record | `docs/release-validation/v<VERSION>.md` + a new row at the top of `docs/release-validation/README.md` | copy the previous record's structure: **Verdict → Results per tier group → Findings (ordered by how much they matter) → What was NOT validated → Reproduce** |
| Security snapshot | `security/test-results/<VERSION>/` | produced by `make security-results`; eyeball redactions |
| Benchmark audit **and** guidance refresh | `docs/benchmarking/releases/v<VERSION>.md` + README row (F1); `docs/benchmarking/config-guidance.md` re-headed to `v<VERSION>` with the model-selection section, `index.md` model table, and every other paper in the folder whose data was re-measured (F2–F4); `benchmarks/results/v<VERSION>/<suite>/…` for every suite run; `baseline.json`; cited images | see tier F. A PR that carries only the A/B entry is an incomplete tier F and the record must say so |

Redaction applies to the validation record too: account ids, VPC/subnet/SG/VPCE
ids, API hostnames, pool ids, stack physical ids and local paths become
`<ACCOUNT_ID>`, `<VPC_ID>`, `<API_HOST>`, …. Raw logs stay in gitignored `scratch/`.

**PRs.** Branch from `github/develop` (not `main`, not `origin` — `origin` is
GitLab; push with `git push github <branch>` and verify the PR's `headRefOid`
matches your commit, see the `idp1-remotes-origin-is-gitlab` memory). Two PRs,
both targeting `develop`:

1. `docs/release-validation-v<VERSION>` — the validation record, its README row,
   and `security/test-results/<VERSION>/`.
2. `docs/benchmark-v<VERSION>` — the release A/B entry and README row, the refreshed
   `config-guidance.md` and `index.md` (and any other regenerated paper), every
   `benchmarks/results/v<VERSION>/<suite>/` set, `baseline.json`, images.

Split so the benchmark can be discussed without holding the security snapshot. Do
**not** merge; do not touch `CHANGELOG.md` (the release is already cut). Commit
messages and PR bodies end with the attribution lines the session provides.

## Reporting (the chat summary)

The user is context-switching and may read only the final message. Give the
context first, then the verdict, in prose, per the writing-style memory:

1. One paragraph: what was validated (version, template URL, account/region,
   stacks), how long it took, what was left up.
2. The per-tier table: tier · gate · one-line result · vs PREV.
3. **Concerning or surprising results, each written out in full** — what was
   measured, what is inferred, what is unverified. Always cover, even if the answer
   is "none":
   - any tier that failed, was NOT RUN, or passed **vacuously** (the seller-403
     class — a pass that never reached the code under test);
   - any security gate count that moved from PREV, either direction;
   - any benchmark cell where recall, `cell_accuracy` or calibration separation
     fell past the `--compare` thresholds, with `cells_compared` shown, and any cost
     movement beyond the cell's own spread on **unchanged** documents;
   - anything the upgrade changed in extracted values;
   - the open behaviour decisions the prerelease audit left (for 0.6.8: simple-mode
     truncation above ~400 rows now that over-splitting is fixed);
   - the model-selection verdict from F3 in two sentences: which lightweight model
     is at ceiling for the common profiles, and whether the premium models earned
     their price on any profile — with the ratio, not just the sign;
   - which `docs/benchmarking/` files were regenerated and which still carry data
     from an earlier release, by name;
   - surprising **improvements** too — v0.6.6 found a defect open since v0.6.0 had
     quietly closed, which was the most useful line in that record.
4. Findings in test/tooling code, marked as such, separately from product findings.
5. Links to the two PRs.

Never report a failure count without saying whether the environment was correctly
installed, never call a failure pre-existing without reproducing it on `develop`
with the test extras, and never write "GovCloud works" from a commercial run.

## Standing issues to recognise, not rediscover (delete when fixed)

- **Seller stack-test region** (2026-08-28, still open 2026-09-12): run with
  `REGION` unset; see tier C.
- **Seller stack-test leftover log group** (2026-09-12): delete
  `/aws/apigateway/idp-seller-entitlement-citest-activation` before and after; see tier C.
- **`make srt-scan` without setup re-opens suppressed findings** (2026-09-12): always
  `CI=1 python scripts/srt/setup.py` first; see preflight step 3. Same gap inside
  `scripts/security/run_security_tests.sh`.
- **`scripts/tests/test_iam_privilege_escalation.py` scans `scratch/`** (2026-09-12):
  a stale `git worktree` under `scratch/` fails `make test` on templates already exempt
  under their real paths. `git worktree list` and remove them before believing it.
- **Detached processes print late.** `run_stacktest.py` and `run_matrix.py` buffer
  stdout when redirected to a file — the verdict appears only at exit. Launch them
  with `PYTHONUNBUFFERED=1`, and track progress from CloudFormation (`list-stacks`)
  and `results/run-*/runmap.json` rather than the log.
- **Benchmark docs are the deliverable, not a by-product.** At v0.6.8 tier F ran the
  A/B only; `config-guidance.md` stayed headed *v0.6.7* and the `astravalue` /
  `astracap` suites that #850 added had never been run. F2–F4 above exist so that
  cannot recur; if budget forces a trim, the record names each un-refreshed paper.
- **`make typecheck` whole-repo baseline** is non-zero by design (CI checks changed
  files only): compare totals to develop, do not report the raw number as a failure.
- **`make security-results` first-run Cognito race** (`UserNotFoundException`): the
  test tears its users down; just re-run.
- **`idp-cli config-upload` migrates configs** — the benchmark harness bypasses it
  with `--native-upload` for this reason; never round-trip configs through the CLI
  during the A/B.
