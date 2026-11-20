---
title: "Document Attributes"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Document Attributes

Callers often know identifiers for a document before they submit it: an account
id, a claim number, a policy number or a case reference. **Document attributes**
let you attach them at upload as key-value pairs. The accelerator stores them with
the document and returns them unchanged with its results, so you no longer have to
encode the identifier in the S3 key and parse it back out of every output path.

Attributes are separate from the document's internal processing metadata, and
nothing in the pipeline reads them: they are not included in any classification or
extraction prompt, they do not choose the configuration profile, and output paths
are still built from the input key. A document uploaded without attributes is
processed and stored exactly as it was before this feature existed.

## Attaching attributes

### S3 upload: inline metadata

Add S3 user metadata named `idp-attr-<name>` to the object. On the wire that is the
header `x-amz-meta-idp-attr-<name>`. Because S3 lower-cases metadata names and HTTP
header names use hyphens, every `-` in `<name>` becomes `_` in the stored key:

```bash
aws s3 cp statement.pdf s3://<input-bucket>/accounts/statement.pdf \
    --metadata idp-attr-account-id=ACME-0042,idp-attr-claim-number=C-77
```

This document carries `account_id: ACME-0042` and `claim_number: C-77`. Values on
this route are always strings, and S3 limits all of an object's user metadata to
2 KB.

### S3 upload: a JSON sidecar

For a larger set, or for numbers, booleans, lists, or keys that contain `-`, put a
JSON object in the input bucket under the document's key plus
`.idp-attributes.json`, upload it **before** the document, and name it in the
document's metadata as `idp-attributes-uri`:

```bash
echo '{"account_id": "ACME-0042", "amount_due": 1250.5, "tags": ["q3", "priority"]}' \
    > attrs.json
aws s3 cp attrs.json s3://<input-bucket>/accounts/statement.pdf.idp-attributes.json
aws s3 cp statement.pdf s3://<input-bucket>/accounts/statement.pdf \
    --metadata idp-attributes-uri=s3://<input-bucket>/accounts/statement.pdf.idp-attributes.json
```

The sidecar must be in the same bucket as the document and its key must end in
`.idp-attributes.json`; intake never reads any other location. It is read once,
when the document arrives, and the result is stored with the document, so editing
or deleting the sidecar afterwards changes nothing. An object whose key ends in
`.idp-attributes.json` is not processed as a document. If the same key appears
both inline and in the sidecar, the inline value is kept.

### SDK

```python
from idp_sdk import IDPClient

client = IDPClient(stack_name="my-idp-stack")
client.document.process("statement.pdf", attributes={"account_id": "ACME-0042"})
client.batch.process(directory="./statements/", attributes={"source": "portal"})
```

The SDK chooses the transport itself: inline metadata when the set can be
reproduced exactly that way, otherwise the sidecar. Invalid attributes raise
`IDPConfigurationError` before anything is uploaded.

### CLI

```bash
idp-cli process --stack-name my-idp-stack --dir ./statements/ \
    --attribute account_id=ACME-0042 --attribute source=portal

idp-cli process --stack-name my-idp-stack --dir ./statements/ \
    --attributes-file attrs.json
```

`--attribute` is repeatable and its values are strings; `--attributes-file` takes
a JSON object. When both are given, each `--attribute` overrides the file's value
for that key. Neither can be combined with `--test-set`.

### Batch manifest

A manifest may carry an `attributes` column: a JSON object in a JSON manifest, or a
cell holding a JSON object in a CSV manifest. A row's attributes override the
batch-level `--attribute`/`--attributes-file` set key by key.

```csv
document_path,attributes
/data/statement-1.pdf,"{""account_id"": ""ACME-0042""}"
/data/statement-2.pdf,"{""account_id"": ""ACME-0043"", ""priority"": true}"
```

The upload API (web UI) and the MCP `process` tool do not accept attributes yet.

## Rules

| Rule | Limit |
|---|---|
| Key | Matches `^[a-z0-9][a-z0-9_-]{0,62}$`: lower-case letters, digits, `_` and `-`, starting with a letter or digit, at most 63 characters |
| Value | A string, a finite number, `true`/`false`, or a flat list of those. Nested objects and lists of lists are not accepted |
| Integer | Between -(2^53 - 1) and 2^53 - 1, so a browser reads it back unchanged |
| String | At most 1024 bytes (UTF-8), per value and per list item |
| List | At most 50 items |
| Count | At most 100 attributes per document |
| Total | At most 32 KB for the whole set as compact JSON, which is also the largest sidecar intake reads |

The SDK and CLI **refuse** an invalid set with a message naming every attribute
that broke a rule. At S3 intake there is no caller to answer, so an invalid
attribute is **dropped**: the document still processes with the valid ones, and a
warning (`ingest_attributes_dropped`) is recorded on the document, naming each
dropped key and the rule it broke but never the value. When the count or total
size is exceeded, attributes are kept in sorted key order until the limit is
reached. A sidecar that cannot be used at all (wrong bucket, wrong key suffix,
unreadable, too large, not a JSON object) is dropped as a whole.

A whole-valued number read back from the tracking table is returned as an integer,
so `2.0` is returned as `2`.

## Where attributes appear

- **The document detail page** lists them under **Attributes**, one `key: value`
  line each, rendered as plain text. The field is absent for a document without
  attributes.
- **`get_results` and `get_document_results`** (SDK) and the CLI commands built on
  them return an `attributes` object for a document that has any. The key is absent
  otherwise.
- **The document status lookup** returns `attributes` for a document that has any.
- **The tracking table** stores them as `DocumentAttributes` on the document's
  item, written only when the document has any.
- **The document object** carried between workflow steps has an `attributes` key,
  again only when the document has any, so post-processing hooks receive them.

Attributes are carried unchanged through a reprocess from the web UI or API, an SDK
or CLI rerun, and the malware-scanning gate: each uses the set stored at the
original intake rather than reading the object again. Only when a document being
reprocessed has no tracking record left are they read from the object by the same
rules.

## Security

Attributes carry the producer's authority: whoever can write to the input bucket
can set them, exactly as they can set `config-version` or supply the document
itself (see [Role-Based Access Control](./rbac.md)). Anyone who can see a document
can see its attributes. Because a trusted producer often copies values from
sources it does not control, the values are still treated as untrusted:

- they are validated against the rules above on every route;
- the web UI renders them as text, never as HTML;
- they are not included in any prompt and build no storage path;
- their values are never written to logs, since an account or claim number can be
  personal data. The intake log line lists attribute names with the values
  replaced by `<redacted>`.

## Developer reference

The rules and the S3 intake route are implemented once, in
`idp_common.document_attributes`; the SDK and the queue sender both use that
module. See the
[`idp_common` README](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/lib/idp_common_pkg/README.md#document-attributes)
for the library-level description.
