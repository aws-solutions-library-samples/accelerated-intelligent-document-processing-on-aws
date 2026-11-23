# PII Anonymization — IDP Accelerator Extension

> **EXPERIMENTAL / unproven.** Try it and send feedback (GitHub issues), but
> validate the redacted output yourself; not a sole PII control.

Detects and redacts PII from source documents **before** the accelerator's
classification and extraction models see them, so the processing pipeline —
its prompts, logs, and stored results — operates only on de-identified
content. (The redaction step itself uses a Bedrock model to *detect* the PII,
so PII does transit that one detection call; what this feature prevents is PII
spreading through the rest of the pipeline.) Built on a new standalone
**`preprocessing`** pipeline hook that runs first — before the BDA/pipeline
routing — in both processing modes.

Reuses the document detection/redaction library from the AWS Labs
[pii-anonymizer](https://github.com/awslabs/pii-anonymizer) sample (Apache-2.0),
**vendored** under `hook/vendor/` so the accelerator owns and security-scans it.
See `hook/vendor/PROVENANCE.md` and the `sync-pii-anonymizer` Claude skill for
the re-sync procedure.

## How it works

1. A document is uploaded under a configuration profile that carries a `preprocessing`
   block + hook (created by the Config Pairing wizard).
2. The `preprocessing` hook (`hook/handler.py`) runs first. It detects + redacts
   PII and writes a **de-identified copy** into the Input bucket beside the
   original with a `(REDACTED)` marker in its name (e.g. `report(REDACTED).pdf`),
   stamped with S3 metadata `config-version=<companion>`.
3. That upload re-triggers processing — the redacted copy is processed under the
   **companion** configuration profile (which has **no** preprocessing hook, so it is
   not redacted again; a `(REDACTED)`-marker guard is the belt-and-suspenders).
4. Depending on **mode**, the hook either deletes the original or lets it run too.

## Modes

| Mode | Original | Redacted copy | Use case |
|------|----------|---------------|----------|
| `redactcopy_and_stop` | **deleted** (S3 + tracking) | processed | PII must never reach the model / be retained |
| `redactcopy_and_continue` | processed | processed | Two result sets; scope each to different users via `allowedConfigVersions` RBAC |

## Generic preprocessing hook

The `preprocessing` step is **generic** and **flat**: the `preprocessing`
section holds a single hook — `arn`, `onError`, and a list of key/value **args**
directly on the section (no nested list). The hook reads its own config from
`args`. This feature's PII settings all live in those args (`mode`,
`companion_config_version`, `model_id`, `model_provider`, `redaction_mode`,
`store_mapping`) — so the preprocessing step is reusable for any job, not just PII.

## Config Pairing wizard (primary UX)

The feature UI's **Config Pairing** tab clones an admin's **existing** working
configuration profile into a matched pair (base truncated to keep names ≤ 50 chars):

- `<base>__pii_stop` / `<base>__pii_go` — *initiating* profile: base + the
  generic `preprocessing` hook (ARN + PII args, flat on the section).
- `<base>__pii_target` — *companion* profile: base with **no** preprocessing
  hook; the redacted copy is processed under it.

Both are created **non-active**; the admin activates the initiating profile (one
click). A minimal `config-preset/pii-preprocessing.yaml` is also installed as a
non-active `pii-anonymizer` profile, a quick-start reference.

## Redaction Report + PII mapping (RBAC-gated)

The **Redaction Report** tab shows a **metadata-only** audit: per-document PII
count, mode, companion profile, redacted-copy key, timestamp.

Optionally (wizard toggle, off by default) the original→synthetic **mapping** is
stored KMS-encrypted in a feature-owned DynamoDB table. It contains **real PII**; the report's
**View mapping** action reveals it only to callers whose `allowedConfigVersions`
include the **original** document's configuration profile (Admins always pass).

## Cost note

Redaction adds a detection pass per page **before** processing. PDFs use the
image path (Textract + vision per page), so `redactcopy_and_continue` on scanned
docs runs ~2× the whole pipeline. Claude Haiku is the default detection model
(large output budget for dense forms); Nova Lite is cheaper but can truncate.
A hook configuration that names no `model_id` uses Claude Haiku 4.5 through the
stack's own region family: `eu.anthropic.claude-haiku-4-5-20251001-v1:0` in an EU
region, the `us.` profile elsewhere, as the deploy-time model mapping does.

## Formats (v1)

PDF (redacted PDF out — always via the image path so the copy is a real,
flattened PDF with no leaked text layer), images (JPG/PNG/TIFF/BMP/WEBP), TXT,
CSV. Office formats (DOCX/XLSX) work via the vendored processors but are lower-fidelity;
audio is out of scope.

## Not a sole compliance control

LLM-based PII detection is probabilistic. Pair with human verification for
compliance-critical use — position as strong risk-reduction, not a guarantee.

## Layout

```
feature.yaml              # manifest (preprocessing hook + writes-documents)
template.yaml             # hook Lambda + deps layer + audit table + API + ui-deployer
config-preset/pii-preprocessing.yaml
hook/handler.py           # the preprocessing hook (+ vendor/ closure)
feature-api/handler.py    # Redaction Report API
feature-ui/               # Config Pairing wizard + Redaction Report (UMD bundle)
ui-deployer/handler.py    # install-time registration + preset apply
```

## Tests

```bash
(cd hook && python -m pytest tests -q)
(cd feature-api && python -m pytest tests -q)
(cd ui-deployer && python -m pytest tests -q)
(cd feature-ui && npm ci && npm run build)   # tsc --noEmit + vite
```
