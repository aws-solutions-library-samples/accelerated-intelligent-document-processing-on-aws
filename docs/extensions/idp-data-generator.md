---
title: "Test Set Generator"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Test Set Generator

The **Test Set Generator** is an installable [Feature Platform](../feature-platform.md)
extension that generates labeled synthetic document test sets — a PDF plus a
paired ground-truth JSON label per document — using the open-source
[SEED](https://awslabs.github.io/synthetically_engineered_evaluation_data)
generator (`seed-data`) on an Amazon Bedrock AgentCore Runtime.

Generate test sets from a plain-language description of a document type, or from
an existing configuration version's class schema. The output lands in the host's
Test Set bucket in the layout [Test Studio](../test-studio.md) auto-discovers, so
generated documents are immediately usable for evaluation runs.

> The extension keeps the stable feature id `idp-data-generator` (used by the
> host UI, IAM tags, and registration); only the display name is "Test Set
> Generator".

## What it does

Produces realistic, schema-conformant synthetic documents with ground-truth
labels for evaluating OCR, classification, and extraction:

- **Two generation modes** — from a document-type description (a schema is
  authored on the fly), or from an existing configuration version + document
  class (the class schema seeds generation).
- **Scenario theming** — an optional high-level theme (e.g. "small-business
  owners in retail") that the generator diversifies into distinct documents. A
  **Suggest** button proposes themes with Bedrock.
- **Quality control** — a quality setting (faster vs. higher quality) tunes how
  many generation/critique passes SEED runs per document.
- **Labeled output** — each document is emitted as `input/<doc>.pdf` plus
  `baseline/<doc>.pdf/sections/<n>/result.json` (document class + inference
  result), the standard IDP test-set layout Test Studio reads.
- **Optional image augmentation** — scan/fax-style aging effects for
  robustness testing (requires `seed-data>=0.0.6`).
- **Cost estimate** — the modal shows an estimated cost/time band before you
  start a run.

## Where to use it

Once installed, the generator is reachable three ways:

- **Test Studio → Test Sets → Generate Test Set** — a modal (shown only
  when this extension is installed) to generate from a description or a
  configuration version/class, with scenario, quality, and document-count
  controls. Choose a **destination**: create a new test set (a unique name is
  required — a name that already exists is rejected) or add the generated
  documents to an existing test set. The resulting test set appears in the list
  when the background job completes; click a test set's name to preview its
  documents without running a test execution.
- **View/Edit Configuration → Document Schema** — a **Generate test set** button
  deep-links into Test Studio with the modal pre-filled for the current
  configuration version.
- **Quick Start** — the onboarding agent discovers the extension and can
  generate synthetic documents directly (including passing a scenario).

## Installation

Install like any Feature Platform extension: open **Extensions (Preview)** in the
web UI, select **Test Set Generator**, and launch the CloudFormation stack (it
attaches to the host by `MainStackName`). Installing builds the generator's
AgentCore Runtime image (CodeBuild → ECR → AgentCore) — allow a few minutes on
first install.

The generator requires Amazon Bedrock model access for the models SEED uses
(Claude, Nova, GPT-OSS, etc.); requests run in your account.

## How it works

A self-contained extension stack that plugs into the host contract:

- **FeatureApi** (HTTP API + Cognito JWT) — `POST /generate` and
  `/generate-from-config` enqueue a job (each takes a destination: `testSetName`
  to create a new set, or `testSetId` to append to an existing one; both require
  the caller to be in the `Admin` or `Author` group); `POST /estimate-cost`
  returns a cost/time band; `POST /suggest-scenario` proposes scenario themes via
  Bedrock; `GET /jobs` lists in-flight jobs and `GET /jobs/{id}` returns one
  job's status.
- **BootstrapProcessor** (SQS-driven Lambda) — authors/resolves the document
  class schema, writes it into a configuration version, and invokes the
  AgentCore Runtime asynchronously.
- **AgentCore Runtime** (arm64 container) — runs the SEED pipeline
  (`seed-data` + the accelerator's `idp_common.synthesis` adapter), writes the
  test set to the host Test Set bucket, and records terminal job status in the
  extension's tracking table.
- **Job status** is feature-owned (a DynamoDB tracking table), surfaced through
  the FeatureApi.

## Tuning and troubleshooting

Generation is long and expensive — a high-quality batch with scan/fax effects can run for
an hour or more and cost tens of dollars — so the failure modes are worth knowing. All
three below were hit on a real stack while building a 100-document set.

### The stack parameters that matter

| Parameter | Default | When to change it |
|---|---|---|
| `SeedNodeTimeoutSeconds` | 10800 (3h) | A run fails with `Node 'doc_loop' execution timed out`. The cap is per *stage*, and one stage renders the whole batch, so its cost scales with document count x quality x augmentation — not per document. Because the stage is all-or-nothing, exceeding it discards every document generated so far. |
| `BedrockMaxAttempts` | 10 | Rarely. Retries transient Bedrock faults in botocore, beneath SEED and strands. |
| `GeneratorModelId` | Sonnet | Cost or quality trade-offs. |

### "Node 'doc_loop' execution timed out"

The render stage exceeded `SeedNodeTimeoutSeconds` and the whole batch was lost. Raise the
parameter rather than shrinking the batch — but check the log first, because a run that
*thrashes* rather than works looks the same from outside. Count critic rejections:

```
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT --since 3h   | grep -c "data_critic rejected"
```

A handful is normal. Over a hundred for a dozen documents means the generator is retrying,
not rendering — see below.

### Documents rejected in a loop

If the log repeats a rejection like *"fields … were set to `null` instead of being omitted
entirely"*, the config's field descriptions and its JSON Schema disagree. IDP config
classes are written for extraction, where the convention for an absent field is an
explicit null (many descriptions say "Output null if not shown"), but a JSON Schema
`string` cannot hold null — so the instruction is unsatisfiable and every document retries
until the stage budget is gone. The generator now widens optional scalar fields to accept
null in its own copy of the schema, so this resolves itself; if you see it, the extension
predates that fix and should be updated.

### A run that goes silent

Scan/fax augmentation is CPU-bound image work and, unlike generation, produces little log
output while it runs — so a slow augmentation and a dead runtime look identical. The job
record carries `heartbeatAt` and `elapsedMinutes`, updated once a minute while the run
lives, which distinguishes them: a heartbeat that keeps advancing means the runtime is
working, a frozen one means it is gone. A job whose heartbeat goes stale is failed
automatically rather than showing as generating forever.

### A transient Bedrock error ending a long run

`internalServerException` from `ConverseStream` is retryable — its own message says to try
again — but the generator reaches Bedrock through SEED and strands, so the accelerator's
own retry decorators are not on that path. `BedrockMaxAttempts` makes botocore retry
underneath every library in the container. One caveat: an error that arrives *mid-stream*
cannot be retried by the SDK, so a failure after a long run is still possible and simply
re-running is the remedy.

## Command line

The [`idp-cli bootstrap`](../idp-cli.md) command runs the same synthesis pipeline
in-process (prompt → schema → configuration version → labeled test set), with
`--count`, `--threshold`, and `--augment` options. It uses the
`idp_common[synthesis]` adapter directly rather than the deployed extension.

## Related

- [Test Studio](../test-studio.md) — where generated test sets are used.
- [Feature Platform](../feature-platform.md) — the extension framework.
- [Quick Start](../quick-start.md) — the onboarding agent that can delegate to
  this extension.
- [SEED documentation](https://awslabs.github.io/synthetically_engineered_evaluation_data) — the upstream generator.
