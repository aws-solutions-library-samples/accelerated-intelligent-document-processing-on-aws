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
3. **Run SRT before you build.** `make srt-scan` on a tree with `.aws-sam/` output
   reports every finding twice (source + packaged artifact). The gate now labels
   gitignored hits "LOCAL-ONLY, non-blocking", but the clean way is still: scan
   first, `publish.py` second.
4. **Confirm the published artifact.** `curl -sI <TO_URL>` returns 200, and its
   `Description` line contains `(v<VERSION>)`.
5. **Tell the user the budget** before the first deploy: the full battery stands up
   and tears down roughly eight stacks and took **most of a working day** for
   v0.6.6, with real Bedrock/Textract spend on the benchmark side (corefast is
   19 cells × 3 repeats × 3 docs per side).

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
if the SRT scan from tier A is already in `.srt/issues.json`. Review the redactions
before committing. Gate values to compare against `security/test-results/<PREV>/`:
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
- **Concurrency.** At most **two** stack deploys in flight at once. Six at once is
  what burst the account's control planes (Logs create-consistency, CodeBuild
  role-trust propagation, IAM CreatePolicy rate) and produced failures unrelated to
  the code — the reason these left CI.
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

### F. Release benchmark A/B `PREV → VERSION`

Same stack, byte-identical configs, only the code version differs. `corefast`,
`repeats: 3`, `--native-upload`, both sides; `aggregate.py --compare` against the
PREV summary; `--figures`; promote `baseline.json`. Reuse the upgrade stack from
tier E for the PREV side if it is still up and healthy (it is exactly "PREV
published, upgraded to VERSION published"), otherwise deploy PREV fresh.

- **If `docs/benchmarking/releases/v<VERSION>.md` already exists** as a *prerelease*
  audit (develop `.devN` vs PREV), do **not** add a sibling file. Re-run against the
  published template and update that file in place so its header, stack and commit
  reflect the release; keep the prerelease measurements as a clearly labelled
  section if they add information. One file per release, never overwritten by a
  later release, is the audit-trail rule.
- Results go to `benchmarks/results/v<VERSION>/corefast/` (one set per release,
  `RETENTION.md`). Cited figures to `images/benchmark-v<VERSION>-*.png`.
- Refresh `docs/benchmarking/config-guidance.md` only where the release changed a
  recommendation.
- Read `benchmark-metric-blindspots`: recall counts rows not cells, a null column
  scores 1.000 — show `cells_compared` next to any "no effect" claim.

Skill: `run-benchmarks.md` ("Release-cycle audit trail").

## Ordering that fits in a day

1. Preflight → `make srt-scan` → start `publish.py --clean-build` detached.
2. While it builds: `make test`, `make lint-cicd`, `make dep-audit`, `make typecheck`.
3. Deploy the **upgrade** PREV stack (long) and the **transform headless** run, two in flight.
4. When a `v<VERSION>` stack exists: `make security-results` against it.
5. Stack-tests two at a time; seller last (different region, no VPC).
6. Upgrade stack: baseline doc → update-stack → post doc → keep it as the benchmark stack.
7. Benchmark both sides (the PREV side must run **before** the upgrade — schedule
   it between the baseline document and `update-stack`, or use a second stack).
8. Aggregate, compare, figures; write the three records; sweep ENIs; tear down all
   but one reference stack at `v<VERSION>` (say which one you left up and why).

## Outputs — three records, two PRs

| Record | Path | Notes |
|---|---|---|
| Release validation record | `docs/release-validation/v<VERSION>.md` + a new row at the top of `docs/release-validation/README.md` | copy the previous record's structure: **Verdict → Results per tier group → Findings (ordered by how much they matter) → What was NOT validated → Reproduce** |
| Security snapshot | `security/test-results/<VERSION>/` | produced by `make security-results`; eyeball redactions |
| Benchmark audit | `docs/benchmarking/releases/v<VERSION>.md`, README row, `benchmarks/results/v<VERSION>/…`, `baseline.json`, cited images | see tier F |

Redaction applies to the validation record too: account ids, VPC/subnet/SG/VPCE
ids, API hostnames, pool ids, stack physical ids and local paths become
`<ACCOUNT_ID>`, `<VPC_ID>`, `<API_HOST>`, …. Raw logs stay in gitignored `scratch/`.

**PRs.** Branch from `github/develop` (not `main`, not `origin` — `origin` is
GitLab; push with `git push github <branch>` and verify the PR's `headRefOid`
matches your commit, see the `idp1-remotes-origin-is-gitlab` memory). Two PRs,
both targeting `develop`:

1. `docs/release-validation-v<VERSION>` — the validation record, its README row,
   and `security/test-results/<VERSION>/`.
2. `docs/benchmark-v<VERSION>` — the benchmark doc, README row, results directory,
   `baseline.json`, images.

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
   - surprising **improvements** too — v0.6.6 found a defect open since v0.6.0 had
     quietly closed, which was the most useful line in that record.
4. Findings in test/tooling code, marked as such, separately from product findings.
5. Links to the two PRs.

Never report a failure count without saying whether the environment was correctly
installed, never call a failure pre-existing without reproducing it on `develop`
with the test extras, and never write "GovCloud works" from a commercial run.

## Standing issues to recognise, not rediscover (delete when fixed)

- **Seller stack-test region** (2026-08-28, still open 2026-09-11): run with
  `REGION` unset; see tier C.
- **`make typecheck` whole-repo baseline** is non-zero by design (CI checks changed
  files only): compare totals to develop, do not report the raw number as a failure.
- **`make security-results` first-run Cognito race** (`UserNotFoundException`): the
  test tears its users down; just re-run.
- **`idp-cli config-upload` migrates configs** — the benchmark harness bypasses it
  with `--native-upload` for this reason; never round-trip configs through the CLI
  during the A/B.
