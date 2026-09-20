---
title: "PII Anonymization"
---
# PII Anonymization

:::caution[Experimental]
PII Anonymization is an **experimental, unproven** feature. Try it and tell us
what works and what's missing — but **validate the redacted output yourself** and
do not rely on it as your sole PII control. LLM-based detection is probabilistic;
missed PII is possible. See [Feedback](#feedback).
:::

**PII Anonymization** is a bundled [Extension Feature](../feature-platform.md)
that detects and redacts personally identifiable information (PII) from documents
**before** the accelerator's classification and extraction models see them — so
the processing pipeline (its prompts, logs, and stored extraction results)
operates only on de-identified content. Note the boundary precisely: the
redaction step itself sends each page to a Bedrock model to *detect* the PII,
so PII does transit that single detection call; what the feature prevents is
PII flowing through the rest of the pipeline and into its stored outputs. It
is the reference example of the
standalone **`preprocessing`**
[pipeline-hook point](../feature-platform.md#pipeline-hooks), which runs first
in the workflow (before the BDA/pipeline routing).

It integrates the detection/redaction library from the AWS Labs
**[pii-anonymizer](https://github.com/awslabs/pii-anonymizer)** project
(Apache-2.0). That code is *vendored* into this feature (copied, security-scanned,
and shipped as part of the accelerator) rather than deployed as a separate stack —
see the feature's `hook/vendor/PROVENANCE.md` and the `sync-pii-anonymizer` skill
for how it is kept in sync with upstream.

## Why use it

- **Unblocks GenAI adoption in regulated settings.** Healthcare, finance,
  insurance, and government teams that can't let raw PII reach a model can redact
  first, then extract from the de-identified copy.
- **De-identified datasets as a deliverable** — safe for analytics, ML training,
  or third-party sharing.
- **Dual-track access** — process both an original and a redacted copy and scope
  each to different users via the existing config-version RBAC.
- **Structure-preserving synthetic redaction** keeps downstream extraction
  accuracy intact (unlike a plain blackout).

## How it works

1. A document is uploaded under a config version that carries a `preprocessing`
   hook (created by the **Config Pairing** wizard).
2. The preprocessing hook runs first. It detects and redacts PII, then writes a
   **de-identified copy** into the Input bucket **beside the original with a
   `(REDACTED)` marker** in its name (e.g. `report(REDACTED).pdf`), tagged with S3
   metadata pointing at a **companion** config version.
3. That upload re-triggers processing: the redacted copy is processed under the
   companion version (which has no preprocessing hook, so it is not redacted
   again — a `(REDACTED)`-marker guard prevents any loop).
4. Depending on the **mode**, the original is either deleted or also processed.

### Modes

| Mode | Original document | Redacted copy | When to use |
|------|-------------------|---------------|-------------|
| **Redact copy and stop** | **Deleted** (S3 + tracking) so it no longer appears | Processed | PII must never reach the model, and the original should not be retained |
| **Redact copy and continue** | Processed normally | Processed | You need two result sets; scope each to different users |

### Formats

PDFs are **redacted PDF-in / PDF-out**: every PDF goes through the image path
(pages rasterized, PII boxed/replaced, flattened) so the redacted copy is a real
PDF with **no leaked text layer**. Images (JPG/PNG/TIFF/BMP/WEBP), TXT, and CSV
are also supported. Office formats (DOCX/XLSX) are processed but lower-fidelity;
audio is out of scope in v1.

## Use cases

- **De-identified extraction** — redact-and-stop so extraction runs only on
  synthetic data; the real document is not retained by the pipeline.
- **Two-tier review** — redact-and-continue, granting a small privileged group
  the original's config version and everyone else only the redacted companion.
- **Safe data products** — export the redacted copies and (optionally) the
  extraction results as a shareable, de-identified dataset.

## Enabling it — the Config Pairing wizard

Install the feature from **Extensions → Browse catalog**, then open its page. The
**Config Pairing** tab is the primary way to turn redaction on:

1. Pick one of your **existing** config versions as the base (your real
   extraction settings are preserved).
2. Choose a mode, a PII-detection model (Claude Haiku is the default — dense
   forms like W2s need a large output budget; Nova Lite is cheaper but can
   truncate and fail closed), a redaction style, and whether to store the PII
   mapping (see below).
3. Click **Create config pair**. The wizard creates two **non-active** versions:
   - `<base>__pii_stop` or `<base>__pii_go` — the *initiating* version, carrying
     the generic preprocessing hook with its args.
   - `<base>__pii_target` — the *companion*, which processes the redacted copy.
4. Click **Activate** to make the initiating version active.

The preprocessing step is **generic** — a Lambda ARN plus key/value args — so the
`preprocessing` section in **View/Edit Configuration** works for any preprocessing
job, not just PII. This feature's settings (mode, model, redaction, companion,
store_mapping) live in that hook's `args`.

## RBAC — how access works

Access follows the accelerator's existing **config-version scoping**
(`allowedConfigVersions` per user, on the Configuration page):

- **Two-tier documents** — in *redact copy and continue* mode, the original is
  processed under the initiating version and the redacted copy under
  `<base>__pii_target`. Grant privileged reviewers the initiating version and
  everyone else `<base>__pii_target`. Users only see documents whose config
  version is in their allowed set.
- **PII mapping (re-identification key)** — when synthetic redaction is used you
  can optionally store the original→synthetic value map. **It contains real PII.**
  It lives in a feature-owned, KMS-encrypted DynamoDB table — deliberately *not*
  in any host bucket, so the host's generic file-contents API can never serve it —
  and the only read path is the feature API's `GET /report/{docId}/mapping`
  route. That route applies the same config-version scoping as every other
  consumer: a caller **restricted** to a set of Configuration Profiles gets the
  mapping only if the **original** document's profile is in that set, an Admin always
  passes, and — deliberately — so does a caller carrying **no** restriction at all,
  which is the default for a user with no User Management record. Scoping is opt-in
  per user across the whole product and this route is not an exception to it; if the
  mapping should be reachable by fewer people than that, scope them. It **fails
  closed** where the scope cannot be *evaluated*: a lookup that errors for any reason
  denies (403) rather than being read as unrestricted. A document the caller's scope
  does not cover answers exactly as one that does not exist, so the route does not
  report which documents have a redaction record. The Redaction Report *list* is
  filtered by the same scoping, and audit rows carry only a stored-yes/no flag —
  never the mapping itself or its location. Off by default; enable per-pair with
  the wizard's "Store PII mapping" toggle.

## Redaction Report

The **Redaction Report** tab shows a metadata-only audit (no PII by default):
per-document PII count, mode, companion version, redacted-copy location, and
timestamp. When a mapping was stored, a **View mapping** action opens the
original→synthetic table — subject to the RBAC gate above.

## Cost and latency

Redaction adds a detection pass per page **before** processing. Because PDFs use
the image path, expect a Textract + vision pass per page; *redact copy and
continue* on scanned PDFs runs roughly the whole pipeline twice. Choose the
detection model deliberately (Haiku for dense forms, Nova Lite for lighter docs).

## Feedback

This feature is experimental and we want your input — accuracy gaps, missing
formats, and the use cases you need. Please open an issue on the accelerator's
GitHub repository:
**https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues**.
Upstream detection/redaction issues can also be raised at
[awslabs/pii-anonymizer](https://github.com/awslabs/pii-anonymizer).

## See also

- [Feature Platform](../feature-platform.md) — how extensions work.
- [Feature Platform → Pipeline hooks](../feature-platform.md#pipeline-hooks) —
  the generic `preprocessing` hook contract this feature builds on.
- [Feature Platform developer guide](../feature-platform-developer-guide.md).
- Upstream: [awslabs/pii-anonymizer](https://github.com/awslabs/pii-anonymizer).
