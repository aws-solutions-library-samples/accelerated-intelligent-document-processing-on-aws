---
title: "v0.6.15 → v0.6.16 Migration (Review Routing and Run Plans)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# v0.6.15 → v0.6.16 Migration (Review Routing and Run Plans)

v0.6.16 adds [review routing](./review-routing.md) (routing rules that decide
whether a document is reviewed, in which review queue and with which reason for the
reviewer) and gives every processing run a **run plan** that says at which step the
run starts and which steps it leaves out. Both change the processing workflow: a
Review Routing step is added after policy rule validation, a run after a completed
review starts at a new step that applies the review, and several states of the
workflow have new names.

**You do not need to edit a configuration to keep processing documents on a stack
you update.** An update leaves the Review Routing switch (`hitl.enabled`) of every
profile as it is, and a profile that has no routing rules of its own holds the
documents it held before: a document with confidence alerts or missing required
fields goes to the Default review queue when Review Routing is on. It holds one
more: a document with a section whose extraction failed without failing the
document, which v0.6.15 let complete unreviewed (see
[Where a failed section shows up](./extraction-and-confidence.md#where-a-failed-section-shows-up)).
Every other document is not reviewed. **A stack you create with v0.6.16 turns Review Routing on**; see
[New stacks turn Review Routing on](#new-stacks-turn-review-routing-on). This page
lists what behaves differently, what to check if you integrate with the workflow,
the API, the SDK or the review status object, and what a rollback does.

## Before you upgrade

Check each of these that applies to you:

| If you … | Read |
|---|---|
| create a new stack and process documents with nobody reviewing them | [New stacks turn Review Routing on](#new-stacks-turn-review-routing-on) |
| deploy pipeline hooks (Feature Platform features or your own hook Lambdas) | [Hooks](#hooks) |
| read `review/status.json` | [Review status object](#review-status-object-schema-version-11) |
| call the UI API directly (`reprocessDocument`, `listReviewQueues`, `processChanges`) | [API changes](#api-changes) |
| reprocess documents with the SDK, the CLI or the MCP tool | [SDK, CLI and MCP](#sdk-cli-and-mcp) |
| have dashboards, alarms or tooling that name Step Functions states | [State names](#state-names) |
| use policy rule validation together with review | [Policy rule validation and review](#policy-rule-validation-and-review) |
| query the reporting database | [Reporting](#reporting) |
| document or script the configuration page | [Names in the web UI](#names-in-the-web-ui) |

## What stays the same

- Statuses, the tracking table's keys, the S3 layout of results and the review
  queue item states are unchanged.
- Documents in review when the stack is updated can be completed, rejected or
  removed afterwards (see [During the upgrade](#during-the-upgrade)).
- Step Functions execution names are built as before, so a redelivered message
  still cannot start a second execution.
- A document reviewed from the document page while the Default review queue is
  disabled (see [Human Review](./human-review.md)) is processed as before: it is
  not held, and it runs again from the beginning when its review is completed.
- Configuration profiles need no conversion. A profile, or a revision a document
  is pinned to, that has no `routing` section takes the table the `default` profile
  holds: the built-in routing rules, until an Admin changes `default`'s.
- **The Review Routing switch of an existing profile.** A stack update writes no
  value for `hitl.enabled`: a profile that has it off stays off, and one that has it
  on stays on. Only a preset or a custom configuration that sets `hitl.enabled`
  itself is applied on an update, as for every other setting.

## Processing changes

### A run starts at the step it needs

Every message that starts a run carries a run plan: the kind of run (a new
upload, a reprocess, saved edits, a completed review, a rejected review, a review
that was removed or skipped, a send-back, a restart of a failed run), the step it
starts at, and the optional steps it skips. The first state of the workflow,
`CheckRunPlan`, sends the run to that step.

| Run | v0.6.15 | v0.6.16 |
|---|---|---|
| New upload | every step | every step |
| After a completed review | from the beginning, with each step detecting that its work exists | starts at **Apply Review Changes**, then policy rule validation, Review Routing, summarization, evaluation. When the reviewer changed the page grouping (Pipeline mode), the sections that were added or whose pages changed are extracted first, and the review is applied to the new values |
| After a rejected review | from the beginning, ending at the review check | ends the document as `REJECTED` without invoking a Lambda function |
| After a review is removed or skipped | from the beginning | starts at policy rule validation |
| After a review is sent back (new) | — | starts at classification, or at the beginning in BDA mode; the review's corrections are discarded |
| Reprocess | from the beginning, or from classification or extraction with the SDK | from any step, on every surface (web UI, SDK, CLI, MCP) |
| Process Changes after edits | from the beginning, with each step detecting that its work exists | starts at the first step the edits affect: policy rule validation when only field values were edited |

A run that starts later does not repeat the steps before it, so it is faster and
costs less. The Processing Flow viewer shows the steps a run left out as
**Skipped**.

**A reviewer chooses no step.** Where the run after a review starts is decided by
the system, from the document as it is when the review ends. The dialog that
finishes a review (**Process Changes** on a queue item) and the confirmation of
**Process Changes** on a document outside a review both list the steps the run
will do, each marked as running, skipped or not enabled, before you confirm. See
[How a review ends](./review-queues.md#how-a-review-ends).

The run plan is in the execution input, as `runPlan`. A message with no plan
(one queued before the update) runs every step, as before.

### Review Routing step

The new `RoutingStep` (Review Routing) runs after policy rule validation in both
modes and decides whether the document is held. In v0.6.15 the decision was made by
the step that assembles the document's results, before policy rule validation. A
document it holds carries the routing rule that placed it, a reason for the
reviewer and the policy rules it failed. See [Review Routing](./review-routing.md).

### New stacks turn Review Routing on

Whether documents are held for review is the **Review Routing** switch of each
configuration profile (`hitl.enabled`).

| Stack | Review Routing after the deployment |
|---|---|
| A stack **updated** to v0.6.16 | Unchanged in every profile. A managed profile that the update adds takes the value of `default` |
| A stack **created** with v0.6.16 | **On** in `default` and in the managed profiles the stack creates, unless the preset or the custom configuration sets `hitl.enabled` itself |
| A [headless deployment](./headless-deployment.md), created or updated | Off: it has no review queues |

On a new stack, a document with a failed section, a confidence alert or a missing
required field therefore waits in the Default review queue, with status
`HITL_IN_PROGRESS`, until
a person finishes its review. In v0.6.15 a new stack came up with the switch off,
unless the configuration it was deployed with turned it on.

**If nobody reviews the documents** (a batch job, a load test, an integration that
waits for every document to reach `COMPLETED`), turn Review Routing off in the
profile the documents are processed with:

- In the web UI: **Configuration › Processing Settings › Review Routing
  (Optional)**, switch **Review Routing** off, and save.
- With the CLI, upload a file that holds only the switch; every other setting
  keeps its value:

  ```yaml
  hitl:
    enabled: false
  ```

  ```bash
  idp-cli config-upload --stack-name my-stack \
      --config-file review-routing-off.yaml --config-profile default
  ```

- For a stack that should come up with it off, set `hitl.enabled: false` in the
  configuration the stack is deployed with (`CustomConfigPath`, or
  `idp-cli deploy --custom-config`).

The cases are listed in
[The Review Routing switch](./review-routing.md#the-review-routing-switch).

### Policy rule validation and review

Because routing can read policy rule verdicts, policy rule validation runs
**before** a document is held. When the review is completed, it runs **again**
on the corrected values. A document that is reviewed therefore runs policy rule
validation twice, where v0.6.15 ran it once, after the review. If you use both
features, expect the Bedrock cost of policy rule validation to double for
reviewed documents; documents that are not reviewed are unaffected.

Two further changes to policy rule validation:

- Every policy rule has a stable id, `policy_type.rule_key`, and every result a
  normalised `verdict` (`pass`, `fail`, `not_found`, `unknown`) beside its
  recommendation text. Both are in `consolidated_summary.json` and in the
  Markdown report.
- The verdict pass runs for every document, including a document with one
  section, which previously got facts and no verdict. It makes one model call
  per policy rule the symbolic engine did not decide.

See [Rule Validation](./rule-validation.md).

### State names

States of the workflow were renamed so that names say what a state does. If a
dashboard, an alarm, a Logs Insights query or a script names one of them, update
it.

| v0.6.15 | v0.6.16 |
|---|---|
| `RouteByProcessingMode` | `CheckProcessingMode` |
| `BDA_CheckExistingData` | `CheckExistingBdaData` |
| `BDA_InvokeDataAutomation` | `InvokeDataAutomationStep` |
| `BDA_ProcessResultsStep` | `BDA_AssembleDocumentStep` |
| `BDA_ProcessResultsSkip` | `AssembleExistingDocumentStep` |
| `Pipeline_ProcessResultsStep` | `Pipeline_AssembleDocumentStep` |
| `ShardModeChoice` | `CheckShardMode` |
| `CheckRuleValidationEnabled` | `CheckPolicyRuleValidationEnabled` |
| `SetEmptyRuleValidationResult` | `SetEmptyPolicyRuleValidationResult` |
| `SetSkippedRuleValidationResult` | `SetSkippedPolicyRuleValidationResult` |
| `ProcessRuleValidationSections` | `ValidatePolicyRulesBySection` |
| `RuleValidationStep` | `PolicyRuleValidationStep` |
| `RuleValidationSectionComplete` | `PolicyRuleValidationSectionComplete` |
| `RuleValidationOrchestration` | `PolicyRuleConsolidationStep` |
| `CheckHITLRequired` | `CheckRoutingDecision` |
| `MarkHITLPending` | removed |
| `RecordEvaluationFailure` | `RecordEvaluationFailureStep` |
| `FailState` | `WorkflowFailed` |

New states: `CheckRunPlan` (the first state), `StartAtClassification`,
`StartAtExtraction`, `StartAtExtractionForSections`, `StartAtAssessment`,
`StartAtPolicyRuleValidation`, `SelectAllSections`, `CheckSectionStart`,
`CheckAssessmentSkipped`, `CheckReviewToApply`, `SelectAssembledDocument`,
`ApplyReviewChangesStep`, `SkipPolicyRuleValidation`, `RoutingStep`,
`CheckSummarizationSkipped`, `SkipSummarization`, `CheckEvaluationSkipped` and
`SkipEvaluation`. `CheckReviewToApply` and `SelectAssembledDocument` come after the
Pipeline assembly: they send a run that follows a completed review and extracted
sections again to `ApplyReviewChangesStep`, and every other run on to policy rule
validation.

Two Lambda functions are added, `RoutingFunction` and
`ApplyReviewChangesFunction`, each with its own log group. Like every function
of the workflow, they are named after the nested pattern stack:
`/<StackName>-PATTERNSTACK-<id>/lambda/<name>`.

## Hooks

Pipeline hooks fire for the steps a run **does**. A run that starts at a later
step does not fire the hooks of the steps before it, and a step the run plan
skips does not fire its hook. A step that the configuration turns off still
fires its hook, as before.

What this changes for a hook:

- `preprocessing`, `postOcr`, `postClassification` and `postExtraction` fire on
  the run that does those steps, and **not** on the run after a completed review,
  which starts after them. In v0.6.15 they fired on both. The one exception is
  `postExtraction`, which fires on that run for each section it extracts again
  because the reviewer changed the page grouping.
- `postRuleValidation` fires **before** a document is held for review, and again
  on the run after the review is completed. In v0.6.15 it fired after the review
  only.
- `postSummarization` and `postprocessing` fire on a run that reaches those
  steps. A run that ends by holding the document in a review queue does not reach
  them, so they do not fire while a document waits in a queue.

To let a hook tell the runs apart, the payload has three new top-level fields:

| Field | Value |
|---|---|
| `runKind` | `new_upload`, `reprocess`, `saved_edits`, `review_completed`, `review_rejected`, `review_skipped`, `sent_back`, `restart_failed` or `legacy_review`; `unplanned` for a run with no run plan |
| `startStep` | The step the run started at, for example `beginning`, `extraction` or `apply_review_changes` |
| `reviewed` | `true` when the document's review was completed and this run does not extract again |

A hook that needs the values no review will change should use
`postSummarization` or `postprocessing`: a run reaches them only when the
document is not held. A `postRuleValidation` hook sees the values before a
possible review, and sees them again, with `reviewed: true`, on the run after a
completed review. When that run extracts sections again first, it carries
`runKind` `review_completed` with `startStep` `extraction` and `reviewed: false`,
because the newly extracted values are not confirmed by a reviewer; test
`reviewed`, not `runKind`, to act on confirmed values. The existing payload fields
are unchanged. The Health Insurance Review sample feature (0.2.3) stores `reviewed`
and `runKind` with each claim status. See
[Feature Platform developer guide](./feature-platform-developer-guide.md).

## Review status object: schema version 1.1

`review/status.json` moves from schema version `1.0` to `1.1`. The change is
additive:

- `schema_version` is `"1.1"`.
- A new optional property `routed_by` holds the routing rule that placed the
  document and what its reviewer is told: `table_id`, `rule_id`, `rule_label`,
  `placed_queue`, `fallback_reason`, `reason`, `reason_source`, `policy_failures`
  (each with `rule_id`, `policy`, `name`, `reason` and `pages`) and
  `policy_failure_count`. It is `null` for an item no routing rule placed (for
  example one created by **Send to review**). `reason` can hold extracted values,
  so treat the object as document content.
- The reason `kind` values now include `missing_required` and `extraction_failed`
  in the schema.

A consumer that checks for `schema_version == "1.0"` exactly has to accept
`"1.1"`. A consumer that ignores unknown properties needs no change. See
[Review status for downstream consumers](./review-queues.md#review-status-for-downstream-consumers).

## API changes

Two operations of the UI API return a different shape:

| Operation | v0.6.15 | v0.6.16 |
|---|---|---|
| `reprocessDocument` | `Boolean` | `ReprocessResult`: `queued`, and for each document `status` (`QUEUED`, `REFUSED` or `FAILED`), `startStep`, `reason` and `message`. It also takes `startStep` and `skipSteps` |
| `listReviewQueues` | a list of queue definitions | a page: `items` and `nextToken`. It takes `limit` and `nextToken` |

`processChanges` keeps its arguments, and its answer gains `startStep` and `steps`:
the step the run it queued starts at, and every step with its state.

New operations: `getReprocessOptions`, `sendBackReviewItem`,
`getReviewCompletionPlan`, `getProcessChangesPlan`, `testRoutingTable`,
`getReviewQueueReferences` and `convertRoutingDmn`. `sendBackReviewItem` takes the
document, the review cycle and an optional note, and no start step: the run starts
at classification, or at the beginning in BDA mode. The two plan operations read
what finishing a review, and what Process Changes, would run, and start nothing.
Review items gain `queueName`, `routedBy` (with `reason`, `policyFailures` and
`policyFailureCount`) and `processingMode`, and documents gain `ReviewQueue`,
`ReviewQueueName` and `ReviewRoutedBy`. `updateConfiguration` answers with
`warnings` when a save leaves a routing rule that reads a policy rule result while
policy rule validation does not run. The groups each operation admits are in
[RBAC](./rbac.md).

**Reload the web UI after the update.** A browser tab loaded before the update
runs the previous UI, which expects the previous shapes of these two
operations.

## SDK, CLI and MCP

Reprocessing is done by the stack, not by the client. The SDK and the CLI invoke
the stack's reprocess function, so every surface applies the same rules: which
steps a document can start at, what is cleared first, and what is refused.

- **Permissions.** The caller's credentials need `lambda:InvokeFunction` on the
  stack's reprocess resolver function (and on the review queues resolver
  function for `client.review_queues` and `idp-cli review-queues`), and
  `cloudformation:DescribeStackResources` and `cloudformation:DescribeStacks` to
  find them through the nested API resolver stack. In v0.6.15 a reprocess needed
  access to the tracking table, the buckets and the document queue instead.
- **SDK.** `client.document.reprocess(document_id, step=…, skip=…)` and
  `client.batch.reprocess(…)` take any step, and return each document's status,
  reason and message. `rerun()` remains as a deprecated alias.
  `client.document.reprocess_options(…)` lists the steps a document can start at.
- **CLI.** `idp-cli reprocess --step <step> [--skip <step> …]`; `rerun-inference`
  remains as an alias. The step names are `beginning` (alias `ocr`),
  `classification`, `extraction`, `assessment`, `policy_rule_validation`,
  `summarization`, `evaluation`, and `resume` for a failed document.
- **MCP.** The `reprocess` tool keeps its name; `step` accepts the same steps and
  `skip` is new.
- **A stack that has not been updated** does not have the functions. The v0.6.16
  SDK and CLI say so and name the release; update the stack, or use the v0.6.15
  client with it.

A document a reprocess refuses is a result, not an error: the call succeeds and
that document's status is `REFUSED` with a reason code such as
`RUN_IN_PROGRESS`, `OUTPUT_MISSING` or `STEP_NOT_IN_MODE`. See
[IDP SDK](./idp-sdk.md) and [IDP CLI](./idp-cli.md).

## Names in the web UI

The configuration keys are unchanged. On **Configuration › Processing Settings**
the sections for the two steps have these names, and are in the order the pipeline
runs the steps:

| Section | Holds | Key |
|---|---|---|
| **Policy Rule Validation (Optional)** | Whether policy rule validation runs, and its settings. The policy rules are on the **Policy Schema** tab | `rule_validation` |
| **Review Routing (Optional)** | The **Review Routing** switch, the confidence threshold, whether a missing required field is reviewed, and the Inbox and bulk-skip settings | `hitl` |

The routing rules are on the new **Routing Rules** tab, after **Policy Schema**
(key `routing`). The report of a document's policy rule results is the **Policy
Rule Validation Summary**, on the document page's and the edit page's **Reports**
menu, and the pipeline steps are named **Policy Rule Validation**, **Review
Routing** and **Apply Review Changes** wherever a step is listed.

## Reporting

- The `rule_validation_details` table gains the columns `rule_id` and `verdict`.
- `document_reviews.top_reason_kind` is `routing_rule` for a document that a
  routing rule placed with no confidence alert, no missing required field and no
  failed section, and `extraction_failed` for a document whose first review reason
  is a section whose extraction failed.
- `document_reviews` gains the column `routing_reason`: the reason of the routing
  rule that placed the document, as written, with its placeholders unfilled. See
  [Reporting Database](./reporting-database.md#document-reviews-table).

## During the upgrade

**Runs in progress.** An execution that is running when the state machine is
updated finishes on the definition it started with. If it ends by holding the
document for review, the document is in the Default queue and can be reviewed
after the update like any other. Executions started after the update use the new
definition.

**Documents in review.** A document held before the update is completed,
rejected or removed after it with the new behaviour: completing it starts a run
at Apply Review Changes (or at extraction, for a section that has no extracted
values), rejecting it ends it as `REJECTED` without a Lambda invocation, and
removing it starts a run at policy rule validation. Its review record gets no
`routed_by`, since no routing rule placed it, so its Review Reason panel shows the
alerts and no routing notice.

**The Review Routing switch.** The update changes it in no profile (see
[New stacks turn Review Routing on](#new-stacks-turn-review-routing-on)).

**Messages already queued.** A message queued before the update has no run plan
and starts a run of every step, in which each step detects work that exists, as
in v0.6.15.

## Rollback

Rolling the stack back to v0.6.15 is possible, with these effects:

- **Runs started on the v0.6.16 definition** that are in progress during the
  rollback finish on that definition and call functions the rollback removes, so
  they fail at the routing step. Reprocess those documents afterwards.
- **Profiles saved by v0.6.15 lose their `routing` section**, because that
  release does not know the section and writes the profile without it. Export
  the routing tables first (see
  [DMN XML export and import](./review-routing.md#dmn-xml-export-and-import)) if
  you intend to update again.
- **Review queues other than Default** stay in the review table. v0.6.15 routes
  every held document to the Default queue and its web UI cannot create a queue,
  but it lists the existing queues and their documents can be worked.
- **Review items** keep the fields v0.6.16 wrote (`routed_by` with its reason and
  failed policy rules, the placement count, the processing mode, the page layout
  recorded at placement and the start of the run after the review). v0.6.15 ignores
  them.
- **`review/status.json`** objects written by v0.6.16 keep schema version `1.1`
  until the document is reviewed again.
