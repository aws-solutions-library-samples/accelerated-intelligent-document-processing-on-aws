---
title: "Processing with a Supplied Configuration"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Processing with a Supplied Configuration

A document can name the configuration it should be processed under, instead of
using a stored [Configuration Profile](./configuration-profiles.md). Upload the
configuration as a JSON or YAML file under the input bucket's `_configs/` prefix,
then upload the document with S3 object metadata `config-uri` pointing at it. The document is
processed under that configuration, and the stack's configuration table is not
consulted for it.

This is useful when an external system owns its own configurations, when running a
one-off document under an ad-hoc configuration, or when comparing several
configurations on the same document without creating profiles for each.

Uploads without `config-uri` metadata are unaffected.

## Usage

```bash
INPUT_BUCKET=<your stack's InputBucket>

# 1. Put the configuration under the input bucket's _configs/ prefix.
aws s3 cp ./my-config.json "s3://$INPUT_BUCKET/_configs/my-config.json"

# 2. Upload the document with config-uri metadata naming it.
aws s3 cp ./invoice.pdf "s3://$INPUT_BUCKET/invoice.pdf" \
  --metadata "config-uri=s3://$INPUT_BUCKET/_configs/my-config.json"
```

With boto3:

```python
s3.upload_file(
    "invoice.pdf",
    input_bucket,
    "invoice.pdf",
    ExtraArgs={"Metadata": {"config-uri": f"s3://{input_bucket}/_configs/my-config.json"}},
)
```

Results land in the output bucket exactly as they do for any other document.

⚠️ **Keep configurations under `_configs/`.** Every other object written to the
input bucket is treated as a document, so a configuration uploaded anywhere else is
itself queued for processing and fails. Objects under `_configs/` never start
processing — and, for the same reason, a document uploaded under `_configs/` is
never processed either.

## The configuration file

- **Format:** JSON, or YAML when the key ends in `.yaml` or `.yml`. YAML is parsed
  with a safe loader.
- **Content:** the same shape as a profile's configuration (see
  [Configuration](./configuration.md)). A partial configuration is accepted: it is
  merged onto the system defaults, exactly as `idp-cli config-validate` does.
- **Location:** it must be in the stack's input bucket, and belongs under
  `_configs/` (see above). A `config-uri` naming any other bucket is rejected.

Validate a file before using it:

```bash
idp-cli config-validate --config-file ./my-config.json
```

## What happens to the document

1. The queue processor reads the configuration and validates it. The document is
   **rejected** — recorded as `FAILED` with the reason in its errors, and no
   workflow is started — when:
   - the file is missing or unreadable, or not in the input bucket;
   - it is not valid JSON/YAML, or not an object;
   - validation reports errors;
   - it sets `use_bda: true`. BDA mode runs against a BDA project linked to a
     stored profile, which a supplied configuration does not have.

   Validation warnings are logged and do not reject the document.
2. The validated, merged configuration is written once to the working bucket under
   `config_snapshots/<sha256>.json`, and the document is processed under that
   snapshot. Editing or deleting your file after upload does not affect a document
   already in flight.
3. Every step — OCR, classification, extraction, assessment, summarization, rule
   validation, evaluation, reporting — and the [pipeline hooks](./lambda-hook-inference.md)
   read the snapshot. Hooks come from the supplied configuration, never from the
   active profile.

The tracking table records the snapshot URI as `ConfigUri`, and each run's output
version manifest records it as `config_uri`, so a result can be traced to the exact
configuration that produced it. Snapshots expire with the working bucket's
lifecycle (the stack's log retention period).

## Interactions and limits

- **`config-version` metadata is ignored** when `config-uri` is present, and the
  document carries no profile name. A document processed under a supplied
  configuration belongs to no profile.
- **Scoped Web UI users do not see these documents.** Access scoped by
  `allowedConfigVersions` is decided on the document's profile name, and a document
  with none is denied to a scoped user. Unscoped users and Admins see them as usual.
- **Only principals that can write to the input bucket directly can use it.** The
  Web UI's upload allows a fixed set of metadata fields, and `config-uri` is not one
  of them.
- **Reprocessing from the Web UI uses a stored profile.** Reprocess builds a fresh
  document from the tracking record and does not carry `config-uri`. To reprocess
  under the supplied configuration, upload the document again with the metadata.
- **BDA mode is not supported**, as described above.
