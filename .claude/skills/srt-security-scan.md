# SRT Security Scan — Running, Triaging, Suppressing & Mitigating

Use this skill whenever you run the [Sample Security Review Tool
(SRT)](https://github.com/aws-samples/sample-security-review-tool) — via
`make srt-scan` / `make srt` — or when CI's `security_review` stage fails on
an MR to `develop`. It covers reproducing the scan, telling real issues from
false positives, and the two ways to make a HIGH finding go away: **mitigate**
(fix the code) or **suppress** (record accepted-risk / scanner-limitation).

## ⚠️ SRT does NOT check dependencies for CVEs — `make dep-audit` does

SRT's five scanners include **syft**, which builds an SBOM: an *inventory* of
packages, with **no vulnerability matching**. So SRT can never fail a build for a
known-vulnerable dependency, and for a long time nothing else in CI did either
(found by diffing our SRT output against an independent ASH scan, whose grype
stage flags exactly this class). If you are asked "are we exposed to CVE-X",
**SRT is the wrong tool** — reach for:

```bash
make dep-audit        # regenerate manifests + audit ~1800 pinned deps vs OSV
make dep-audit-fast   # reuse existing dist/manifests (skip regeneration)
```

- Implemented by `scripts/security/dep_audit.py`; gated in CI by the `dep_audit`
  job (`fast_checks`, every push and MR, no AWS).
- Reads the manifests `scripts/generate-dep-manifest.sh` produces, covering
  `uv.lock`, every `package-lock.json` and every `requirements.txt`.
- **Fails on HIGH/CRITICAL.** Triage the same way as SRT — but in
  `scripts/security/dep_audit_allowlist.json`, with a real justification
  (`{"id": "GHSA-…", "package": "…", "reason": "…"}`). Prefer bumping the pin;
  allowlist only when the advisory is genuinely unreachable, and say why.
- **Exit 2 means the audit could not run** (OSV unreachable) — that is not a
  pass. Exit 1 is findings, 0 is clean.
- Two gotchas worth knowing before you chase a finding:
  - An advisory with **no `fixed` event** and an open-ended range matches *every*
    version. Check the reported `last known affected range` — the fix may have
    shipped outside the registry (SheetJS/`xlsx` publishes patches to
    `cdn.sheetjs.com`, which OSV cannot model).
  - Bumping a pin can be blocked downstream: `bedrock-agentcore`'s fix needs
    `boto3>=1.43.31` against a deliberate `boto3<1.43.0` pin, and a Pillow bump
    can break SAM's arm64 build (see `feature-platform/pii-anonymizer/hook/Makefile`
    for the multi-`--platform` workaround).

## Suppressing a finding so it stays suppressed (SRT *and* ASH)

Our security reviewers also run [ASH](https://github.com/awslabs/automated-security-helper)
out of band. ASH runs the **real scanner binaries**, so it inherits whatever each
scanner natively honours — which means an **inline pragma is strictly better than
a tool-specific config entry**: it lives next to the code it excuses, survives
file moves and line drift, and satisfies every tool that runs that scanner.

Verified locally against the actual scanner binaries (not assumed):

| Scanner | Inline pragma | Notes |
|---|---|---|
| bandit | `# nosec B310 - reason` | **`# noqa: S310` is ruff, NOT bandit.** A line needs both. |
| detect-secrets | `# pragma: allowlist secret` | Works in `#`, `//` and `<!-- -->` comments. |
| checkov | `# checkov:skip=CKV_AWS_123:"reason"` | 521 already in use. |
| cfn-nag | `Metadata: cfn_nag: rules_to_suppress` | 157 already in use. |
| semgrep | `# nosemgrep: rule-id` | 43 already in use. |
| grype / npm-audit | none exists | Must go in a config file. |

Three traps that cost real time:

1. **bandit's `# nosec` is line-scoped, and on a multi-line string the pragma
   must sit on a line INSIDE the string node.** The closing `"""` works; the
   opening line does **not** — putting it there makes it part of the string, so it
   suppresses nothing *and* ships the pragma text as data. Two analytics prompts
   had exactly this bug and were sending `# nosec B608 - ...` to the model.
2. **detect-secrets dedupes by secret hash**, reporting one line per unique
   secret per file. Suppress the first occurrence and the next one surfaces — so
   annotate *every* occurrence of the pattern, then re-scan to confirm zero.
3. **A semgrep rule can match your own justification comment.** Writing
   `min-release-age=0` inside an explanatory comment in `.npmrc` re-triggered the
   very rule the comment was explaining.

### ASH-only suppressions: `.ash/.ash.yaml`

For what cannot be inline — generated/data files with no comment syntax, and
dependency advisories — use `global_settings.suppressions`. `path` and `reason`
are required; `rule_id`, `line_start`, `line_end`, `expiration` are optional
(schema: `automated_security_helper/schemas/AshConfig.json` in the ASH repo).

> ⚠️ **A `path` entry with no `rule_id` suppresses ALL findings on that path.**
> Only ever do that for pure data files. Never for a source file or a CFN
> template — it converts a triage note into a blind spot.
> `scripts/security/tests/test_dep_audit.py::TestAshConfig` enforces this.

Keep the dependency entries in `.ash/.ash.yaml` in sync with
`scripts/security/dep_audit_allowlist.json` (a test checks for stale twins).

⚠️ **`pip install automated-security-helper` does NOT install ASH.** That PyPI
name is an unrelated placeholder ("test-package-placeholder", single release
`11.0.2`, no console script). Real ASH is installed from GitHub. See
`docs/dependency-confusion.md` — this is the same class of hazard that doc covers
for our own first-party names.

## What SRT is and how the gate works

- `make srt-setup` downloads the `srt` binary into `.srt/`, writes
  `.srt/srtconfig.json`, installs prerequisites, and **copies the committed
  baseline `scripts/srt/issues.json` → `.srt/issues.json`** (this is how
  suppressions are restored).
- **Prerequisites are five scanners** — `checkov`, `semgrep`, `bandit`, `syft`
  (`anchore_syft`), `jupyter` — that SRT pip-installs into its OWN managed venv
  at `.srt/.venv/bin/` (NOT the system PATH). `srt assess` calls
  `checkAllInstalled()` and aborts with **`✗ Prerequisites not installed. Run
  'srt config' first`** (exit 1) if **any one** is missing. **Contrary to older
  guidance, semgrep is NOT optional** for SRT ≥ v1.0.2 — a missing semgrep
  blocks the scan exactly like any other scanner. `scripts/srt/setup.py` now
  verifies all five landed in the venv, retries `./srt config
  --reinstall-prerequisites` once, and hard-fails the setup step in CI if they
  didn't (instead of printing a misleading "✅ setup complete"). A cold install
  (no pip cache) of all five is slow (~10-15 min); the CI job allows 50 min.
- `make srt-scan` runs `scripts/srt/run.py`, which shells out to
  `./srt assess -y -p <repo> --no-diagrams --no-threat-models --no-license-update`.
  SRT merges new findings into `.srt/issues.json`, then `run.py` parses that
  file and prints the `OPEN HIGH PRIORITY SECURITY ISSUES` table.
- **The gate = HIGH priority AND `status` in (`Open`, `reopened`).** Medium/Low
  never block. Only `suppressed` and `resolved` are accepted dispositions.
- **`resolved` is NOT sticky — `suppressed` is.** SRT sets `reopened` when a
  finding it had recorded as resolved/suppressed is detected again, and counts it
  in its own "N issues need attention" line (`Open: 39 / Reopened: 10`). Until
  0.6.5 both `scripts/srt/run.py` and the results curator gated on `Open` alone,
  so a re-detected HIGH passed CI silently — two were sitting in the tree
  (`LAMBDA-012` in `nested/bedrockkb/template.yaml`, and the semgrep npm
  `minimum-release-age` finding on `src/ui/.npmrc`), both carrying
  `status: resolved`. If you intend a finding to stay quiet, write
  `status: "suppressed"` **with** a `suppressionReason`; never leave it
  `resolved`, because the next scan will re-detect it and now correctly block.
- **Exit code differs by environment** (`run.py`):
  - **CI** (`CI`/`GITLAB_CI`/`GITHUB_ACTIONS` set): exits **1** on any HIGH-open
    → pipeline fails.
  - **Local**: exits **0** even with findings and prints a "run `make srt-fix`"
    hint. So **read the printed table / `.srt/issues.json`, never trust the
    local exit code.**
- The scan itself is Bedrock-backed and **slow (~5–15 min)**; `run.py` buffers
  through `tail`, so you see nothing until it finishes. Run it in the background
  and wait — don't assume it hung.

## The gate can break with NO repo change (SRT upgrade / semgrep `--config=auto`)

Two moving parts outside the repo can fail the gate on an unchanged tree:

1. **The registry rule set.** SRT invokes `semgrep scan --config=auto ... --json`,
   and `auto` fetches rules from the semgrep registry at scan time, so new rules
   ship continuously.
2. **The SRT version.** `srt-setup` installs the *latest* release. **v1.0.2
   passed `--exclude ".github"` to semgrep; v1.1.0 dropped it**, so all
   `.github/workflows/**` findings appeared at once with zero code change.

Symptom of both: HIGH-open findings in files whose `git log` shows no recent
change. Don't hunt for the commit that "caused" it — diff the SRT version and
check whether the rule is new.

**Semgrep priority = `extra.metadata.impact`, NOT severity.** SRT's semgrep
`mapResult` reads `priority: J.extra?.metadata?.impact || "Unknown"` (only
Bandit/Checkov go through `mapSeverity`). So an `ERROR`-severity semgrep finding
with `impact: LOW/MEDIUM` never blocks — which is why this repo can carry ~11
ERROR findings (`subprocess-shell-true`, `tainted-sql-string`, …) with a green
gate. When triaging, filter on impact:

```bash
python3 -c "import json;[print((r['extra'].get('metadata') or {}).get('impact'), \
  r['check_id'].split('.')[-1], r['path'], r['start']['line']) \
  for r in json.load(open('/tmp/sg.json'))['results']]"
```

Semgrep findings key on `(path, line, issue)` (Bandit keys on
`(path, line, check_id)`), so a **new rule cannot be pre-suppressed** and a
line-number shift re-opens a suppressed one.

**Watch for a silently-skipped scanner.** `srt assess` logs a scanner crash to
`.srt/logs/srt-tool.log.*` and carries on with an empty result — the printed
table then looks clean for that source. On this dev box the venv `semgrep`
re-execs `pysemgrep` **from `PATH`** and finds a stray
`~/.local/bin/pysemgrep` (system python, no `semgrep` module), so every full
scan died with `ModuleNotFoundError: No module named 'semgrep'` and produced no
`.srt/semgrep-summary.json`.

**Since 0.6.7 `run.py` checks this automatically** (`scripts/srt/scanner_health.py`)
and prints a `⚠️ SCANNERS THAT DID NOT COMPLETE` block, failing CI when the loss
is on a tracked file. To check by hand:

```bash
ls .srt/semgrep-summary.json .srt/bandit-summary.json   # missing ⇒ scanner crashed
ls .srt/*/checkov-summary.json | wc -l                  # one per scanned template
grep -i error .srt/logs/srt-tool.log.*                  # why
```

### checkov's `stdout maxBuffer length exceeded` — caused by `.aws-sam/`, not by size

The other silent-skip cause, and it looks alarming: three
`Error during Checkov scan {stdout maxBuffer length exceeded}` per scan. Facts,
all verified against the binary and by reproducing the command:

- SRT scans **per file** (`checkov -f <file> -o json --output-file-path <dir>
  --soft-fail --quiet`) and its command wrapper calls node's `child_process.exec`
  with **no `maxBuffer`**, taking node's **1 MiB default**. It is not
  configurable from outside the binary — no flag, no env var.
- checkov duplicates its **entire JSON report to stdout** even with `--quiet`
  *and* `--output-file-path`. So the report size, not the file size, is what
  overflows.
- **It is not driven by template size.** The largest file in the repo,
  `template.yaml` (547 KB), yields 2.7 KB of stdout — 459 passed, **0 failed**,
  211 skipped — because it carries **218 `# checkov:skip=` comments**.
  `.aws-sam/idp-main.yaml` is *smaller* (435 KB) but yields **2.6 MB**, because
  **`sam package` re-serializes YAML and drops comments**, so all 218 skips
  become failures.
- Therefore **only `.aws-sam/` artifacts can trigger it.** Measured across a full
  scan, the largest successfully-scanned report is 127 KB — **12% of the buffer**,
  8× headroom. CI, which never has `.aws-sam/`, cannot hit this.

**So the fix is `make srt-clean`, not a checkov flag.** `run.py` now warns up
front when `.aws-sam` dirs are present, and classifies a checkov loss on a
gitignored path as non-blocking (`ℹ️`) while failing CI on a tracked one.

⚠️ Corollary worth remembering: **`# checkov:skip=` / `cfn_nag` suppressions do
not survive `sam package`.** Never treat a scan of a packaged template as
equivalent to a scan of its source — it will report every suppressed check as a
failure. This is also why suppressing an `.aws-sam` finding in `issues.json` is
always wrong.

Upstream bugs worth reporting to
[aws-samples/sample-security-review-tool](https://github.com/aws-samples/sample-security-review-tool)
if this ever bites a tracked file: `exec` should pass a generous `maxBuffer` (or
use `spawn` with a stream), and the checkov call already writes
`--output-file-path`, so stdout should be discarded rather than buffered.

To reproduce just the semgrep half in seconds instead of re-running the whole
Bedrock-backed assess, invoke the binary directly with the venv **first** on
`PATH` (this is what fixes the `pysemgrep` shadowing above):

```bash
PATH="$PWD/.srt/.venv/bin:$PATH" .srt/.venv/bin/semgrep scan \
  --config=auto --json --output=/tmp/sg.json .github/workflows/
python3 -c "import json;[print(r['extra']['severity'], r['check_id'].split('.')[-1], \
  r['path'], r['start']['line']) for r in json.load(open('/tmp/sg.json'))['results']]"
```

Rules seen from this path (all were **real** issues, fixed not suppressed):
`github-actions-mutable-action-tag` (pin `uses:` to a 40-char commit SHA),
`run-shell-injection` (never interpolate `${{ github.* }}` into `run:` — pass
via `env:` and quote `"${VAR}"`), `gha-curl-pipe-shell` (replace `curl … | sh`
with a SHA-pinned first-party action).

## CRITICAL: local scans see more than CI

CI runs on a **clean checkout of tracked files only**. Your working tree
usually has gitignored dirs that SRT will happily scan and flag, producing
findings that **do not exist in CI**:

- `.aws-sam/`, `**/.aws-sam/packaged.yaml` — SAM build artifacts (the `srt-clean`
  target strips these; CI checkouts never have them)
- `scratch/`, `subscription-features/`, vendored Lambda `layer/python/` trees —
  gitignored; full of third-party code (botocore, aiohttp, sympy, …) that trips
  bandit B105/B106/B324/B602 by the hundred

**Since 0.6.7 `run.py` does this reconciliation for you** — gitignored findings
are split into a separate non-blocking `ℹ️ LOCAL-ONLY FINDINGS` table and
excluded from the exit code (see "Never suppress an `.aws-sam/` finding" below).
To reconcile by hand, drop anything gitignored: a finding only matters if its
file is tracked:

```bash
# For each open-HIGH path, is it actually in the CI checkout?
git check-ignore -q "<path>" && echo "IGNORED (not in CI)" || echo "in CI"
git ls-files --error-unmatch "<path>"   # exit 0 = tracked
```

If the user reports N findings and your local scan shows many more, the extra
ones are almost always gitignored — confirm, then focus only on the tracked
subset. (To match CI exactly, run `make srt-clean` first, and/or scan a fresh
`git archive` / clean clone.)

## Reproducing a scan locally

```bash
make srt-setup     # one-time (or after upgrading SRT); restores suppressions
make srt-scan      # ~5–15 min; run in background
```

If `srt assess` errors with **"Configuration not found. Run: srt config"**, the
`.srt/srtconfig.json` is missing. Recreate it (non-interactive) and install
prereqs:

```bash
cat > .srt/srtconfig.json <<'EOF'
{ "AWS_PROFILE": "default", "AWS_REGION": "us-east-1",
  "TELEMETRY_ENABLED": false, "INSTALLATION_ID": "local-dev" }
EOF
( cd .srt && yes '' | ./srt config )   # installs all 5 scanners into .srt/.venv/bin/
# Verify every scanner landed — assess needs ALL of them (a missing one → "Prerequisites not installed"):
ls .srt/.venv/bin/{checkov,semgrep,bandit,syft,jupyter}
cp scripts/srt/issues.json .srt/issues.json   # restore suppressions before re-scan
```

Uses the `default` AWS profile (needs Bedrock access) — see the AWS-access note
in CLAUDE.md. To list current HIGH-open findings without re-scanning:

```bash
python3 -c "import json;[print(i['source'],i.get('check_id'),i['path'],i.get('line'),i.get('resourceName')) \
  for i in json.load(open('.srt/issues.json')) \
  if (i.get('priority') or '').upper()=='HIGH' and i.get('status')=='Open']"
```

## Triage: real issue → mitigate; false positive / accepted risk → suppress

For every HIGH-open finding, decide **mitigate vs suppress**. Default to
mitigating; suppress only with a specific, defensible justification.

**Mitigate (fix the code)** when the finding is a genuine weakness: missing
encryption, over-broad IAM, a real hardcoded secret, public resource that
shouldn't be, missing logging you actually want. Change the template/code so
the check passes, then re-scan to confirm it flips to resolved.

**Suppress** only when one of these holds — and say which in the reason:
- **Scanner false positive** — the property IS set but the checker can't see it.
  The most common cause in this repo: **the check can't resolve `Fn::If`**, so
  conditionally-set `AccessLogSetting`, `MethodSettings`, etc. read as absent.
  (SRT's security-matrix checks call a `resolveValue` that handles `Ref` but not
  `Fn::If` branches.)
- **Tool heuristic false positive** — e.g. bandit **B105** "hardcoded password"
  fires on any literal assigned near an identifier containing `token`/`secret`/
  `password`/`pwd`/`pass`/`key`/`auth`. A variable/dict-key like
  `shard_token_budget = 40000` trips it though `40000` is an LLM token budget.
- **Accepted architectural risk** — the flagged config is intentional and
  compensated. Example: `AuthorizationType: NONE` on the Web UI SPA static-asset
  routes (`WebUIRootMethod`, `WebUIProxyMethod`) — the browser must fetch
  index.html + hashed assets before the user logs in; a JWT can't ride the
  document fetch. Compensating controls: WAFv2 IP allowlist + PRIVATE-endpoint
  policy still gate the stage, and only non-sensitive static files are exposed.
- **Third-party / vendored code** not in the CI checkout — usually just
  gitignored (see above); prefer excluding it over suppressing each line.

## The two suppression mechanisms

### 1. `# nosec` for Bandit (Python) findings — MITIGATE-in-place

Bandit honors an inline comment. Scope it to the exact test id and add a why:

```python
# nosec B105 - the 40000 literal is an LLM token-budget default, not a
# credential. Bandit's heuristic fires only because the dict key
# "shard_token_budget" contains the substring "token".
DEFAULTS = {"max_tokens": 10000, "shard_token_budget": 40000}  # nosec B105
```

The `# nosec BXXX` must be on the flagged source line. Verify with
`.srt/.venv/bin/python -m bandit <file>` — the count under "specifically being
disabled" should increment and the issue disappears (Low-severity `B110`
try/except/pass etc. can remain; only High/Medium reach the gate). This is
preferred over a JSON suppression for Python because the justification lives
next to the code.

**Don't run `ruff format` on a file under `scripts/`** to tidy a `# nosec` edit:
`ruff.toml` `extend-exclude`s `scripts/` (and `src/`, `patterns/`, `notebooks/`),
so `make lint-cicd` never formats it — reformatting drags in unrelated cosmetic
hunks that CI doesn't want. Repo-wide `ruff format --check .` passing while a
single-file check fails is exactly this exclusion, not real drift. Also pin the
local ruff to CI's version before believing a formatting diff (CI: `ruff==0.15.13`
in `.gitlab-ci.yml` / `developer-tests.yml`).
(Checkov findings similarly honor `# checkov:skip=CKV_AWS_NNN: "reason"`, and
semgrep honors `# nosemgrep: <rule-id> - reason` — both already used in this
repo, e.g. `scripts/srt/run.py`, the WAF WebACL in `nested/api-resolvers`.)

### 2. `scripts/srt/issues.json` for security-matrix / Checkov CFN findings

SRT matches a scan finding to an existing issue by the **4-tuple**
`(path, resourceType, resourceName, check_id)` (see `mergeIssues` in the
binary). To suppress, add an entry with that exact key and
`"status": "suppressed"` plus a `"suppressionReason"`. The scanner emits
`resourceName` = the CloudFormation **logical id** and `path` = the template it
found the resource in (note: it scans **both** the source `template.yaml` **and**
any built `.aws-sam/packaged.yaml`; the `.aws-sam` copy is gitignored/absent in
CI, so suppressing the `template.yaml` path is what matters for the gate).

**The file that counts is the committed `scripts/srt/issues.json`** —
`srt-setup` copies it into `.srt/` at the start of every run, so suppressions
must live there to survive. Editing only `.srt/issues.json` is lost on next
setup.

Entry shape (copy the `issue`/`fix`/`resourceType`/`resourceName`/`check_id`
verbatim from the scan's finding object so the key matches):

```json
{
  "source": "security-matrix",
  "path": "nested/api-resolvers/template.yaml",
  "resourceType": "AWS::ApiGateway::Stage",
  "resourceName": "HttpApiStage",
  "issue": "API Gateway does not have access logging enabled with proper retention",
  "fix": "Ensure both DestinationArn and Format properties are specified in AccessLogSetting",
  "priority": "HIGH",
  "check_id": "API-GW-001",
  "status": "suppressed",
  "isCustomResource": false,
  "firstDetectedAt": "<ISO8601>",
  "assessmentCount": 1,
  "suppressionReason": "Accepted (scanner limitation): AccessLogSetting IS configured with DestinationArn + Format but wrapped in Fn::If [EnableApiAccessLogs], which SRT cannot resolve. Logging is correctly implemented."
}
```

Two ways to add entries:
- **Interactive:** `make srt-fix` walks each open finding and lets you
  suppress with a typed reason — then **commit the updated
  `scripts/srt/issues.json`**.
- **Scripted (when srt-fix isn't practical):** append entries to
  `scripts/srt/issues.json` with a small Python script. Dedupe on the 4-tuple
  key so you don't double-add. Grab the exact finding objects from
  `.srt/issues.json` (the `Open` records) to copy their fields.

After either, restore + re-scan to confirm:
```bash
cp scripts/srt/issues.json .srt/issues.json && make srt-scan
```
The finding should now show `Suppressed` and drop out of the HIGH-open table.

## Verifying and finishing

1. Re-run `make srt-scan` (after `cp scripts/srt/issues.json .srt/issues.json`).
2. Confirm the HIGH-open table no longer lists tracked-file findings (gitignored
   noise may remain locally — that's fine, CI won't see it).
3. **Commit `scripts/srt/issues.json`** and any `# nosec`/`# checkov:skip`
   source edits. Do **not** commit `.srt/` (gitignored binary/cache).
4. In the PR/MR description, list each finding and whether it was mitigated or
   suppressed-with-reason so reviewers can audit the security posture.

## Known-good suppressions already in the baseline (context)

These are accepted and living in `scripts/srt/issues.json` — don't "re-fix":
- **API-GW-001 / API-GW-006** (`HttpApiStage`) — access/execution logging IS set
  behind `Fn::If [EnableApiAccessLogs]`; scanner can't resolve the conditional.
- **API-GW-004** (`WebUIRootMethod`, `WebUIProxyMethod`) — intentional auth-less
  public SPA static-asset routes; WAF + PRIVATE-endpoint policy compensate.
- **API-GW-002** (`HttpApi`) — REST API in Lambda-proxy mode; the dispatcher
  validates each body per-field, request validators add nothing for opaque
  proxy payloads.
- **LAMBDA-012** (`nested/bedrockkb/template.yaml`, `StartIngestionJobFunction`) —
  scanner false positive: `StartIngestionJobFunctionRole` has exactly one consumer
  and the check's own fix text names no second function (`…sharing role
  GetAtt:StartIngestionJobFunctionRole with .`).
- **semgrep `npm-missing-minimum-release-age`** (`src/ui/.npmrc`) — accepted:
  `npm ci` + committed lockfile resolves nothing from the registry, and the
  mitigation (`min-release-age = 7`) needs npm ≥ 11.10, newer than the build
  toolchain. Revisit when the build npm reaches 11.10.
- Various **DDB-002 / S3-008 / KMS-007 / EC2-002 / LAMBDA-*** on the
  bastion/KB/feature-platform **source** templates — reviewed accepted risks.

> ⚠️ The baseline used to also carry 17 entries on `.aws-sam/*.yaml` paths.
> Those were **deleted** (along with 2 on the removed `nested/alb-hosting` and
> `nested/appsync` templates) — they were local-scan pollution, not accepted
> risks. Never add one back; see the next section for why.

## Never suppress an `.aws-sam/` finding — clean it instead

If a scan reports a pile of HIGH findings in `.aws-sam/packaged.yaml` /
`.aws-sam/idp-main.yaml`, **you ran a scan over a built tree**. It is not a
regression, and the fix is `make srt-clean`, never a suppression:

- SRT keys suppressions on `(path, resourceType, resourceName, check_id)`, so an
  artifact-path entry **cannot** cover the same resource in the source
  `template.yaml`. It suppresses only the copy CI never sees.
- Worse, `srt fix` writes those entries back as `resolved`, which is **not
  sticky** — the next local scan re-detects them as `reopened`, which *does*
  gate. That is a self-inflicted, recurring CI break.

Since 0.6.7 the tooling enforces this so you don't have to remember:

- `scripts/srt/ci_paths.py` classifies each finding by whether its path is
  git-tracked (= present in CI's checkout; the `srt_security_review` job has
  `needs: []`, so no build precedes it). It **fails closed** — if `git ls-files`
  can't be run, everything gates.
- `scripts/srt/run.py` prints two tables: the gating `🔴 OPEN HIGH PRIORITY
  SECURITY ISSUES` (tracked files only) and a non-blocking `ℹ️ LOCAL-ONLY
  FINDINGS (gitignored files - NOT in CI)`. Only the first affects the exit code.
- `scripts/srt/fix.py` drops gitignored-path findings before writing the
  committed baseline, so `make srt-fix` can no longer re-pollute it.
- `scripts/srt/tests/test_ci_paths.py` fails if any entry on an untracked path
  lands in `scripts/srt/issues.json`, or if a `suppressed` entry lacks a
  `suppressionReason`. Runs in CI via `make test-packages-cicd`.

So a local table showing only LOCAL-ONLY findings is a **pass**. `make srt-clean`
still makes the tree match CI exactly if you want a clean run.
