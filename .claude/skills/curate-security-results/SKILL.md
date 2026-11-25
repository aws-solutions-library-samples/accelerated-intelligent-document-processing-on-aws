---
name: curate-security-results
description: "Use when publishing the public-safe snapshot of the security tests (SRT, ZAP DAST, RBAC static and dynamic) into security/test-results/<version>/ with scripts/security/curate_results.py."
---

# Run Security Tests & Curate Results — Public-Safe Snapshots

Use this skill when the user wants to **run the security tests and update the
published results** — e.g. "run security tests and update results", "capture
the security results for 0.6.1", "update the security test-results", "curate
the SRT/ZAP/RBAC output". It runs the tests and produces redacted, public-safe
markdown under `security/test-results/<version>/`.

## Fast path — one command

For "run security tests and update results", just run:

```bash
# Full (offline tests + live ZAP + RBAC-dynamic against a stack):
make security-results STACK_NAME=<stack> REGION=<region>

# Offline-only (SRT + RBAC static; ZAP + RBAC-dynamic stubbed "not run"):
make security-results
```

This wraps `scripts/security/run_security_tests.sh`: it runs each test, tees the
outputs the curator needs (incl. the ZAP scan stdout), and writes the snapshot.
Then **review the redactions** (step 3) and, if the user asked, commit
`security/test-results/<version>/`.

- Use `AWS_PROFILE=default` for the live tests (see CLAUDE.md). If the user
  didn't name a stack, ask which one, or run offline-only.
- Env knobs: `VERSION=` (default repo `VERSION`), `DATE=` (default today),
  `SKIP_SRT=1` (skip the slow SRT scan, curate from existing `.srt/issues.json`).
- The live RBAC-dynamic setup can hit a Cognito eventual-consistency race on
  first run (`UserNotFoundException`); it tears its users down cleanly, so just
  re-run `make security-results` if that happens.

The rest of this doc is the **manual breakdown** — reach for it when you need to
run only part of the flow, re-curate existing reports, or debug the curator. The
per-test triage skills remain [`srt-security-scan.md`](../srt-security-scan/SKILL.md),
[`api-rbac-test.md`](../api-rbac-test/SKILL.md), and
[`run-stack-tests.md`](../run-stack-tests/SKILL.md).

## The layout it maintains

```
security/
├── README.md            # coverage & goals of each test (durable)
├── threat-modeling/     # threat model (moved here from repo root)
└── test-results/
    ├── README.md        # the process (this skill is its runbook)
    └── <version>/        # one snapshot per release
        ├── MANIFEST.md   # version, git SHA, date, per-test gate
        ├── srt.md
        ├── zap-dast.md
        ├── rbac-static.md
        └── rbac-dynamic.md
```

## Why curated, never raw

Raw reports carry environment-specific identifiers (AWS account IDs, Cognito
pool IDs, API Gateway hostnames, request IDs, absolute local paths) and change
every run — unfit for a public repo. `scripts/security/curate_results.py`
parses each raw report and re-emits only publish-safe fields, running every
string through a redaction filter (`_REDACTIONS`). **Public-safe by
construction** — but still eyeball the output before committing.

## Steps

1. **Run the tests you want in the snapshot** (each writes to gitignored dirs):

   | Test | Command | Raw output the curator reads |
   |------|---------|------------------------------|
   | SRT | `make srt-scan` | `.srt/issues.json` (live results) if present, else committed `scripts/srt/issues.json` |
   | RBAC static | `make api-test-static 2>&1 \| tee /tmp/rbac-static.txt` | the captured stdout file (S1–S5 check enumeration) |
   | RBAC dynamic | `make api-test STACK_NAME=<stack>` | newest `scratch/api-test-results/<stack>-<ts>/` (`report.json` → op × role matrix) |
   | ZAP DAST | `make stacktest-zap STACK_NAME=<stack> 2>&1 \| tee scratch/zap-reports/zap-scan-stdout.txt` | newest `scratch/zap-reports/` |

   Use `AWS_PROFILE=default` for the live ones (see CLAUDE.md). A test you skip
   gets a visible **"not run" stub**, not a silent omission — so a partial
   snapshot is fine and honest.

   **ZAP stdout matters:** the ZAP JSON report carries *findings only*. The
   per-rule PASS/WARN/IGNORE enumeration (the auditable "which rules ran"
   record) lives **only in the scan stdout**, so tee it into the report dir as
   `zap-scan-stdout.txt`. Without it the curated ZAP doc still reports alert
   counts, but falls back to a note instead of the full rule list.

2. **Curate.** The tool auto-discovers the newest `scratch/` report dirs; it
   does **not** read the wall clock, so `--date` is required:

   ```bash
   python3 scripts/security/curate_results.py \
       --date <YYYY-MM-DD> \
       --version <label> \          # defaults to repo VERSION file
       --rbac-static /tmp/rbac-static.txt
   ```

   Overrides: `--srt-issues`, `--rbac-dynamic-dir`, `--rbac-static`,
   `--zap-dir` point at specific source files/dirs.

3. **Review the diff.** Confirm the redaction placeholders are present where
   identifiers would be (`<ACCOUNT_ID>`, `<API_HOST>`, `<COGNITO_POOL>`,
   `<ARN>`, `<REQUEST_ID>`, `<LOCAL_PATH>`), the gate outcomes match what the
   tools printed, and no raw account/host/pool string leaked. Skim the full
   generated markdown, not just the manifest.

4. **Commit** `security/test-results/<version>/`.

## Gotchas

- **Redaction is a choke point, not a guarantee about unknown fields.** The
  curator only publishes fields it explicitly maps. If you extend it to surface
  a new raw field, either confirm that field is already env-agnostic or add a
  pattern to `_REDACTIONS` first. Never `cp` a raw report into `test-results/`.
- **The `<REQUEST_ID>` pattern requires a digit** (so it won't eat CamelCase
  English like `OriginAccessControl`). If a real token is all-letters it won't
  be caught — but ZAP/API request IDs always contain digits.
- **Re-running overwrites the version folder** (idempotent). Bump `--version`
  for a genuinely new release snapshot; don't overwrite a shipped release's
  record with a later run.
- **The RBAC dynamic snapshot deliberately drops the per-request-id full
  matrix** — that's environment-specific. It publishes the gate, failures, and
  coverage counts; the full matrix stays in the gitignored raw `report.md`.
