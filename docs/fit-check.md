---
title: "Fit Check"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Fit Check

`idp-cli fit-check` runs a folder of sample documents through a stack you have
already deployed and writes a structured report of what happened to each one: the
class it was given, whether each field was found and with what confidence, which
of the accelerator's known limitations the run observed, how long it took and what
it is estimated to have cost. It replaces the manual sequence of generating a
configuration, uploading, waiting and reading results in the web UI with one
command, so that "it should fit" can be checked against measured results per
document class.

The report states measurements. It does not judge whether the accelerator fits;
a person, or an AI assistant working with one, does that. Run it only on samples
the document owner has agreed may be processed in the stack's account.

## Run it

```bash
idp-cli fit-check --stack-name my-idp-stack --samples ./samples-to-check/ --cleanup
```

| Option | Meaning |
|---|---|
| `--stack-name` | The deployed stack to run on |
| `--samples DIR` | The sample documents. Read recursively; hidden files are skipped |
| `--description TEXT` | A plain-language description of the documents, used by Quick Start when Discovery cannot produce a class |
| `--config-profile NAME` | Use this existing configuration profile instead of generating one |
| `--ground-truth DIR` | Run the evaluation step against these baselines (see [Ground truth](#ground-truth)) |
| `--output DIR` | Where to write the report (default `./fit-check-<timestamp>/`) |
| `--cleanup` | Delete the run's inputs, results, baselines and generated profile when done |
| `--max-documents N` | Refuse to run when more than `N` files can be processed (default 50) |
| `--timeout SECONDS` | How long to wait for processing (default 3600); documents not finished by then are reported as failed |
| `--from-record FILE` | Rebuild the report offline from a `fit-run-record.json` (see [Rebuild a report offline](#rebuild-a-report-offline)) |
| `--json` | Print `fit-report.json` to stdout instead of the summary |
| `--region` | The stack's AWS Region |

Progress goes to stderr and a short summary to stdout. The same operation is
available to Python code as `IDPClient(stack_name=...).fit_check.run(...)` (see
[IDP SDK](./idp-sdk.md)).

## What it does

1. **Checks the inputs.** Every file is accepted or refused before anything is
   uploaded, and a refusal says why. A file is refused when its extension is not
   one the OCR step routes by name (the OCR step would guess its type from the
   content instead), when it is a legacy binary Word document (an OLE2 container,
   even when renamed `.docx`), or when another sample has the same file name.
   Pages are counted for PDFs and images; Office and text files are counted during
   OCR. The command prints what will be uploaded and where.
2. **Picks a configuration.**
   - Without `--config-profile` it creates a **new** profile named
     `fit-check-<timestamp>` and never changes the active one. The repository's
     own guidance prefers Discovery when real documents are available:
     [Quick Start](./quick-start.md) runs attached example documents through
     [Discovery](./discovery.md) because a schema inferred from real documents is
     higher-fidelity than a prompt-only draft, using single-document Discovery for
     one file and multi-document Discovery for several. The fit check follows the
     same routing over the samples Discovery reads (PDF and images). Multi-document
     Discovery drops a class with only one sample as noise, so when it finds no
     class the fit check runs single-document Discovery on each file instead. When
     Discovery produces nothing (for example, every sample is an Office file) and
     `--description` is given, Quick Start's prompt path (`idp-cli bootstrap`)
     authors the class, with synthetic test-set generation off. The new profile is
     a copy of `default` holding only the classes found.
   - With `--config-profile` it uses that profile as it is.
3. **Processes** the accepted samples under that profile with the CLI's batch
   upload, and waits for them to finish.
4. **Evaluates**, when `--ground-truth` is given, with the stack's evaluation step.
5. **Writes the report** and prints a summary.
6. **Cleans up**, with `--cleanup` (see [What is uploaded, kept and removed](#what-is-uploaded-kept-and-removed)).

Before anything else the command records the active profile's name, a SHA-256 of
its content and a SHA-256 of `Config#default`'s content, and it records them again
at the end, after cleanup and also when the run fails. Both are printed. If they
differ, the command exits with an error after writing the report.

## The report

The output folder holds three files:

| File | Contents |
|---|---|
| `fit-report.json` | The report, `schema_version: 1` |
| `fit-report.md` | The same content for people, with the before and after hashes and what cleanup removed |
| `fit-run-record.json` | Everything the report was built from, so it can be rebuilt offline |

`fit-report.json` is validated against its JSON Schema before it is written. The
schema is
[`lib/idp_sdk/idp_sdk/_core/fit_check/fit-report.schema.json`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/lib/idp_sdk/idp_sdk/_core/fit_check/fit-report.schema.json).

| Field | How it is filled |
|---|---|
| `solution.version` | The stack's solution version, from its CloudFormation description |
| `solution.profile_version` | The [Solution Profile](./solution-profile.md) version that release publishes (a PEP 440 suffix is written with a hyphen) |
| `stack.mode` | `bda` when the profile sets `use_bda`, otherwise `pipeline` |
| `configuration.generated_by` | `discovery`, `quick-start`, or `existing-profile` for `--config-profile` |
| `configuration.classes` | The class ids in the profile |
| `documents[].status` | `succeeded` (completed, or finished and waiting in a review queue), `failed`, or `refused` (never uploaded) |
| `documents[].class`, `fields` | From the section covering the most pages. A packet split into several sections says so in `warnings` |
| `fields[].present` | The extracted value is not empty |
| `fields[].confidence`, `below_threshold` | From the assessment step; for a list or object field, the lowest confidence among its values and whether any is below its threshold. `null` when the field has no confidence |
| `documents[].warnings` | Processing issues the pipeline recorded, plus what each detector observed |
| `documents[].latency_seconds` | From processing start to end, as the tracking table records it |
| `documents[].estimated_cost_usd` | See [Costs](#costs) |
| `evaluation` | See [Ground truth](#ground-truth) |
| `summary.failed` | Every document that did not succeed, **including** refused ones, so `documents == succeeded + failed` |
| `summary.refused` | How many of the failed documents were refused before upload |
| `summary.latency_seconds` | Percentiles over succeeded documents, interpolated between ranks |
| `summary.estimated_cost_usd_per_page` | Total estimated cost over the pages of the documents that have one |

### Limitations the run observed

`triggered_limitations` holds refs of the form `limitation:<id>`, each an id in
[`solution-profile/limitations.yaml`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/solution-profile/limitations.yaml)
of the release that ran. A detector reports only what it observed. Every
observation also appears as a warning, and a ref is added only when the
observation is the condition the limitation describes:

| Ref | Observed when |
|---|---|
| `limitation:unsupported-file-format` | A sample's extension is not one the OCR step routes |
| `limitation:legacy-office-formats` | A sample is a legacy binary Word document |
| `limitation:simple-extraction-row-limit` | In simple extraction mode, the OCR text of a section holds a table larger than the size the profile records as extracted completely with the default model (the threshold depends on the model), or extraction reported a list shorter than its schema's `minItems`. The table's row count and the length of the longest extracted list are always in `warnings` |
| `limitation:simple-extraction-page-limit` | Simple extraction refused a section as too large for one request. Page count alone triggers nothing, because no page cap was measured |
| `limitation:packet-classification-long-context` | Packet-granularity classification refused a request as too long |
| `limitation:language-script-support` | Text in a non-Latin script is seen in the PDF's text layer or the OCR text, and the profile processes it with OCR that reads Latin-script languages only: BDA mode, or the Textract backend with no features, `QUERIES` or `FORMS`. The warning names the script and the OCR configuration; no language is inferred |

### Costs

Each document's cost is an estimate: its metering, priced with the processing
profile's pricing table by the same code that writes the reporting database's
metering rows and the `DocumentEstimatedCost` metric
(`idp_common.reporting.pricing`). A row with no price is left out rather than
counted as zero, and a document whose rows are all unpriced has no cost. Two things
are not included: generating the configuration (Discovery and Quick Start run from
your workstation against Bedrock and are not metered by the stack) and any attempt
that Step Functions retried (see [Cost Calculator](./cost-calculator.md)).

### Ground truth

Without `--ground-truth`, `evaluation` is `null` and the report says so plainly:
confidence is the model's own estimate of each value and is not accuracy.

With `--ground-truth DIR`, `DIR` holds one baseline folder per sample, named after
the sample's file, in the layout the evaluation step reads
(`DIR/<file name>/sections/1/result.json`, the layout `idp-cli use-as-baseline`
and the [evaluation workflow](./idp-cli.md#complete-evaluation-workflow) produce).
The baselines are uploaded to the stack's evaluation-baseline bucket with the
documents, the evaluation step compares each result against its baseline, and
`evaluation` holds the mean of the per-document precision, recall, F1 and accuracy,
overall and per class, with any document that has no evaluation result listed.

## What is uploaded, kept and removed

| What | Where | Kept | Removed by `--cleanup` |
|---|---|---|---|
| The accepted samples | Input bucket, under `fit-check-<timestamp>/` | Under the stack's `DataRetentionInDays` (default 365 days) | Yes |
| Processing results, every object version | Output bucket, under the same prefix | As above | Yes |
| Tracking, list and run records, review-queue items | The stack's DynamoDB tables | As above | Yes |
| Baselines, with `--ground-truth` | Evaluation-baseline bucket | As above | Yes |
| The generated profile | Configuration table | Until deleted | Yes; a profile passed with `--config-profile` is never deleted |
| Intermediate files | Working bucket | Under `LogRetentionDays`, the bucket's lifecycle rule | No |
| Metering, section and evaluation rows | Reporting bucket and its Glue tables | Under `DataRetentionInDays` | No |
| Logs and Step Functions execution history | CloudWatch Logs, Step Functions | Under `LogRetentionDays` and the service's own retention | No |

Discovery reads the sample files on your workstation and sends their content to
Amazon Bedrock directly; it uploads nothing to the stack. After cleanup the
command reads back the input and output prefixes and the profile list and reports
what is left.

## Rebuild a report offline

Every run writes `fit-run-record.json`. `--from-record` rebuilds `fit-report.json`
and `fit-report.md` from it without any AWS access, which is how to regenerate a
report after a change to the report code, or to inspect a run recorded elsewhere.
It is off unless given, and then uploads nothing:

```bash
idp-cli fit-check --from-record ./fit-check-20261120T150000Z/fit-run-record.json \
    --output ./rebuilt/
```

## Contract notes

Two choices sit inside the fixed report contract and are recorded here so a
consumer can rely on them:

- **Refused files are counted in `failed`.** The contract's summary has
  `documents`, `succeeded` and `failed`, and a file refused before upload is neither
  processed nor successful, so it is counted in `failed` and `documents ==
  succeeded + failed` holds. The additive `summary.refused` says how many of them
  were refused.
- **One class per document.** The contract gives a document one `class`. A packet
  the pipeline splits into sections of different classes reports the class and
  fields of the section covering the most pages, and lists every section's class in
  `warnings`.

## Related

- [Solution Profile](./solution-profile.md), whose ids the report's refs name
- [IDP CLI](./idp-cli.md#fit-check)
- [Discovery](./discovery.md) and [Quick Start](./quick-start.md)
- [Evaluation](./evaluation.md)
