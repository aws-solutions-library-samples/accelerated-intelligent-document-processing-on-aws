Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Review queues (`idp_common.review_queues`)

HITL review queues hold documents that a routing rule sent to review, until a
person has reviewed them. Queues are system-wide definitions (`queue_defs.py`) that
Admins manage in Review Studio › Review Management, and they have no confidence
threshold of their own. When Review Routing is on in a document's configuration
(`hitl.enabled`) and the Default queue is enabled, the routing table of that
configuration decides whether a production document waits in a queue instead of
finishing; with the built-in routing rules, a document with a section whose
extraction failed, or whose sections raised confidence alerts (each field's own
threshold, otherwise `hitl.confidence_threshold`) or left a required field without
a value, waits in the Default queue. An Admin assigns them to Annotators, who correct and dispose of
them, and the run after a completed review starts where the library decides. This
README is the canonical description of the library, the queue item lifecycle and
the **review status contract** that downstream consumers rely on. The user guide is
[docs/review-queues.md](../../../../docs/review-queues.md).

Install with the `review_queues` extra:

```bash
pip install -e "lib/idp_common_pkg[review_queues]"
```

## Module map

| Module | What it owns |
|---|---|
| `statuses.py` | The one declaration of `ReviewStatus` (`in_review`, `complete`, `rejected`), `QueueItemState` and the legacy `HITLStatus` spellings, plus `normalize_hitl_status`, and the one definition of "review finished" (`is_review_finished`, `FINISHED_HITL_STATUSES`), "review raised" and "legacy review pending" (`is_review_raised`, `is_legacy_review_pending`, and their DynamoDB form `pending_filter_expression`), the Skip All and Send to review reasons, and `MACHINE_RESULT_DISPOSITIONS` (`removed`, `skipped`). Standard library only, so the layer-free list resolvers, `lookup_function` and `backfill_gsi_attributes` vendor it byte for byte as `review_statuses.py` |
| `models.py` | Pydantic records for the table (`ReviewItemRecord`, `DraftRecord`, `HistoryEntry`, and `RoutedBy`, the routing rule that placed an item, with the reason its reviewer reads and its `PolicyFailure`s), `StatusObject`, `ReviewDecision`, `CallerContext` |
| `keys.py` | Table key layout and the opaque list cursors |
| `store.py` | `ReviewStore`: every read and write of `HITLTable`, all conditional on the item `Version`. `delete_draft` removes one section's draft record |
| `annotators.py` | Who the Annotators are: the Cognito `Annotator` group's members (`annotator_emails`, behind `listAnnotators`) and one user's groups (`cognito_user_lookup`, `is_annotator`), used by both assignment paths. Only Annotators are assigned work; Reviewers claim it |
| `queue_defs.py` | `QueueDefinition` and `QueueDefStore`: the system-wide queues (`PK=QDEF`, `SK=Q#<id>`), each with an `enabled` switch, cached for 30 s; the Default queue is seeded, enabled, whenever it is missing, and a stored Default is never overwritten |
| `directory.py` | `QueueDirectory`: which queues exist and who may work them (Admins and Reviewers every queue; Annotators only those whose `eligible_annotators` names them, so an empty list means no Annotators), failing closed |
| `reasons.py` | `build_reasons`: why a document is held (its sections' `confidence_threshold_alerts` and `missing_required_fields`, and in BDA mode a blueprint confidence under `hitl.confidence_threshold`); reads no S3. `count_confidence_reasons`: the alert count routing reads |
| `regrouping.py` | `regroup_refusal`: who may change the page grouping of a document in review (only the holder of its `CLAIMED` item). `discard_drafts`: deletes the drafts of the sections a regroup removed. Both used by `updateDocumentSections` |
| `routing.py` | `review_mode` (the `hitl.enabled` and Default-queue gate), `default_queue` (the Default queue's definition; `strict=True` raises `TransientError` for a read that failed for a moment), `recognize_finalize` (is this the finalize run of a disposed item), `place_for_review` (writes the `HOLDING` item) and `route_for_review`, which composes the two for a run on a state machine definition with no routing step |
| `routing_inputs.py` | `build_inputs`: the values a routing table reads from a document, the section results and the policy rule validation summary. `InputReader` holds one evaluation's reads, so the table's inputs, a reason's placeholders (`value`) and the failed policy rules (`failed_policy_rules`) read no object twice |
| `routing_step.py` | `decide`: the routing decision after policy rule validation (`hold`, `continue` or `rejected`), with the reason the reviewer reads and the failed policy rules of a document it places. `evaluate_table` and `test_table`, which `testRoutingTable` uses, and `fallback_text`, the generated reason |
| `routing_dmn.py` | A routing table as a decision table and as DMN XML: `to_decision_table`, `export_xml`, `import_xml`. The table's outputs are `outcome`, `queue` and `reason` |
| `apply_review.py` | `apply_review_changes`: the step of the run after a completed review that applies it. Writes the corrections, records the review on every section result, clears the alerts. For a section the run extracted again it sets the corrections on the new result (`merge_corrections`) and records it as not confirmed (`extracted_review_record`) |
| `draft_diff.py` | `leaf_values` (every leaf of a section result by its field path) and `changed_fields` (the leaf paths under `inference_result` that differ between two results). Standard library only, so `apply_review` compares a draft with a result without loading the service |
| `tracker.py` | WorkflowTracker integration: activation after phase 1 and completion after the finalize run |
| `service.py` | `ReviewQueueService`: listing, claims, leases, drafts, dispositions, the supervisor actions, queue administration (`create_queue`, `update_queue`, `list_queues`, `delete_queue`) and who may send a document back (`authorize_send_back`). `complete` decides where the run after the review starts, and `completion_plan` answers what completing or sending back would run |
| `finalize.py` | The finalize token, `DocumentResubmitter` (puts a tracked document back on the DocumentQueue, optionally with a `ResubmitIdempotencyToken`, and raises on failure), `FinalizeSender` (the finalize message, a `DocumentResubmitter` that resets metering; `plan_for` reads the run plan off the item) and `apply_corrections` |
| `review_routing_setting.py` | `review_routing_enabled_provider`: `hitl.enabled` of the active profile, cached, for `MyReviewQueues.review_routing_enabled`. `None` when the profile could not be read |
| `status_object.py` | Writing and deleting `<document>/review/status.json` |
| `notify.py` | Inbox messages (`review.*` notification types); never raises |
| `testset_labels.py` | `TestSetLabels`: the one writer of a reviewed Test Studio label (`write_reviewed`: `labelSource = reviewed-human`, the `_editHistory` entry with field diffs, capped at `MAX_EDIT_HISTORY_ENTRIES`, the confidence-curve observations, the section's draft deleted), and section drafts (save, list, delete, move). Used by `test_set_resolver` and `complete_section_review` |
| `section_writes.py` | `assert_section_result_writable`: refuses (`QUEUE_MANAGED`) a direct write of `<document>/sections/<id>/result.json` to the Output bucket while the document is in review, reading its tracking row. Used by `uploadDocument` (`upload_resolver`) |
| `sweeper.py` | `ReviewSweeper`: assignment reminders and self-healing, run every 5 minutes |
| `api_models.py` | The camelCase API views and argument models |
| `legacy_skip.py` | Skip All Reviews on a review outside a queue: `skip_legacy_review` (the conditional `HITLStatus = Review Skipped` write and its `HITLReviewHistory` entry, with `source`, `jobId` and `resubmitted`) and its guards. Takes the tracking table as a Table-protocol object; the caller resubmits. Used by `complete_section_review` and `review_skip` |
| `review_skip.py` | One document of a bulk Skip All Reviews: `classify` (the outcome from the tracking row and the queue item, shared with the dry run) and `skip_document` (a queue item through `ReviewQueueService.skip_all_reviews`, a legacy review through `legacy_skip`, then the resubmit). Never raises |
| `send_to_review.py` | `SendToReviewService`: creates a manual (`origin="manual"`) Default-queue item for a finished production document, with the tracking row as the commit point, and `abandon`, which the sweeper also calls for a manual item left `HOLDING` |
| `bulk_models.py` | The bulk contracts: `BulkOutcome` (the per-document outcomes the UI mirrors), `BulkSkipRequest`, `SendToReviewRequest`, the result and job views, and the limits |
| `bulk_selection.py` | `PendingReviewQuery`: a profile's pending production reviews in a submitted-date range, one paginated `TypeDateIndex` Query filtered by `pending_filter_expression` |
| `bulk_jobs.py` | `BulkJobStore` (job, chunk, result, list and lock rows in this table) and `BulkSkipService` (dry run, the inline run on 8 threads within an 11 s admission budget, async jobs, and the worker step: one job at a time, paced against the DocumentQueue, idempotent per document) |
| `bulk_settings.py` | `effective_sync_limit`: `hitl.bulk_skip_sync_limit` from the active profile, clamped to 1-100 |
| `testset_summaries.py` | Computing a Test Studio document's summary from its labels in S3 (label source, alert and field counts, the weakest confidence and its threshold, classes, sections) for one document, one page, or a whole set (`summarize_test_set`, which lists `input/` and `baseline/` once each). Used by the test-set resolver and, for whole sets, by `TestSetFileCopierFunction` |
| `testset_docs.py` | Test Studio documents as queue items: one `TSDOC#<testSetId>` / `D#<objectKey>` item per document with its summary (label source, alert count, lowest confidence, classes, sections, reviewed) and assignment, so a set's queue is one Query while the labels stay in S3. Assign, unassign, skip, the assignee check, and the assigned / unassigned / reminder Inbox notices. Standard library and boto3 only |

## Queue item lifecycle

One item per document (`DOC#<docKey>` / `ITEM`), carrying the current review
**cycle**. A reprocess or a fresh run that needs review starts a new cycle and
supersedes the old one.

```text
HOLDING ──phase 1 succeeds──▶ OPEN ──assign──▶ CLAIMED ──complete / reject──▶ FINALIZING ──finalize run──▶ COMPLETE / REJECTED
   │                            ▲                │  │                              │
   └─phase 1 fails─▶ (deleted)  └──── unassign / move ────┘  └─3 failed attempts─▶ FINALIZE_FAILED
any live state ──abort, delete, reprocess, send back, a run that needs no review──▶ WITHDRAWN
FINALIZING (complete) ──a routing rule places the reviewed document again──▶ a new cycle, HOLDING
```

| State | Meaning | Document status |
|---|---|---|
| `HOLDING` | Written by ProcessResults; not claimable until phase 1 ends | in flight |
| `OPEN` | Waiting in a queue | `HITL_IN_PROGRESS` |
| `CLAIMED` | Assigned to one Annotator (or held by an Admin) | `HITL_IN_PROGRESS` |
| `FINALIZING` | Disposed; the finalize run is applying it | `HITL_IN_PROGRESS` |
| `COMPLETE` / `REJECTED` | Finished | `COMPLETED` / `REJECTED` |
| `FINALIZE_FAILED` | The finalize run failed 3 times; an Admin must reprocess | `HITL_IN_PROGRESS` |
| `WITHDRAWN` | The review no longer applies; the status object is deleted | whatever the new run sets |

## Routing (both processing modes)

After policy rule validation, the state machine's routing step calls
`routing_step.decide(document, config, mode=..., execution_arn=..., run_plan=..., store=...)`.
It answers `hold` (the document is placed in a review queue and the run ends
`HITL_IN_PROGRESS`), `continue` (the run goes on to summarization and evaluation)
or `rejected`.

### Review mode

A Test Studio document (`submission_source == "test-studio"` or a `test_set_id`)
always reads as legacy (`review_mode(config, store, document)`): test runs are
scored on the machine's output and their documents are reviewed as test-set
annotation work, which the UI lists beside production queues. Otherwise
`review_mode` is `"queues"` only when all three hold: `config.hitl.enabled` is true,
the stack has a `HITLTable`, and its Default queue is `enabled` (read through
`QueueDefStore`, cached for 30 s). A document whose item is `FINALIZING` stays
`"queues"` so its finalize run completes after HITL or the switch is turned off; a
fresh run in that state never holds. Anything else is `"legacy"`, logged at DEBUG as
`event=review_mode_legacy reason=<hitl_disabled|default_queue_disabled|no_store>`.
In legacy mode routing writes nothing, and the HITL marking is reproduced by
`legacy_section_triggers` and `bda_segment_triggers` in the assembly step. Both BDA
modes read the blueprint confidence through
`idp_common.utils.parse_blueprint_confidence`, which is `parse_confidence` except
that a string is scored only when `float()` accepts it, so a percentage string
(`"95%"`) is unscored rather than rescaled. An unscored blueprint confidence (null,
non-finite, a bool, a string `float()` rejects, or a value outside 0 to 1 after a
bare number in (1, 100] is rescaled, so `150` and `-0.2`) is reviewed at every
threshold: legacy HITL marks the segment and queue mode holds it (below).

The routing step does not call `review_mode`. `decide` checks the same things
itself, in the order given under [The decision](#the-decision), so that its log line
names the one that stopped it. Both read the switch through
`idp_common.config.feature_flags.review_routing_enabled`.

### Reasons

`build_reasons` gives a document's reasons, worst gap first. Up to 20 are stored
per item; `reason_count` keeps the total.

- Each of a section's `confidence_threshold_alerts` (computed by assessment or
  extraction in Pipeline mode, and by BDA processing in BDA mode, with the field's
  `x-aws-idp-confidence-threshold` and otherwise `hitl.confidence_threshold`, and
  never raised for a field with no extracted value, see
  `idp_common.assessment.presence`) is a `low_confidence` reason named by its path
  (`Address.City`, `Rows[0].Amount`), with the alert's confidence and threshold. A
  malformed alert is skipped.
- In BDA mode a matched blueprint under `hitl.confidence_threshold` is a
  `blueprint_confidence` reason, and so is a matched blueprint whose confidence is
  unscored: the BDA result processor sets `Section.blueprint_unscored`, and the
  reason carries a score of 0.0. The flag is persisted in the document's own
  serialization and in the tracking table (`BlueprintUnscored`, written only when
  true), so a reprocess that reloads the document is held again. A section whose
  segment matched no blueprint is unscored too but is not flagged, so it is not
  held, as legacy HITL does not mark it.
- With `hitl.route_missing_required_fields` on (the default; on when unset), each
  of a section's `missing_required_fields` is a `missing_required` reason named by
  its path, with a score of 0.0 and the entry's threshold, otherwise
  `hitl.confidence_threshold`, so it sorts ahead of the confidence alerts. Legacy
  HITL (`legacy_section_triggers`, `bda_segment_triggers`) does not read it.
- A section carrying an error-severity `extraction_failed` processing issue
  (`failed_extraction_issue`; the content-filter case of #2038, where extraction
  records the failure and returns) is one `extraction_failed` reason with no
  field, the issue's `message` as its label, a score of 0.0 and
  `hitl.confidence_threshold`. That one is not queue-only: `legacy_section_triggers`
  reads the same helper, so legacy HITL marks the section pending too, and
  `routing_inputs` counts such sections as `extraction_failed_count`, the input
  the built-in routing rule `builtin-extraction-failed` tests. Each is gated on
  `hitl.enabled`. The code is spelled `EXTRACTION_FAILED_ISSUE_CODE` here rather
  than imported, because importing `idp_common.extraction` loads the extraction
  service; a test pins it to `extraction.failure.EXTRACTION_FAILED_CODE`.
- A section in `confirmed_sections` gives no reason: a reviewer has confirmed its
  values.

`reasons.py` reads nothing from S3 and must not import `idp_common.assessment`:
the routing Lambda installs only the `review_queues`, `decision_tables` and
`run_plan` extras.

### The routing table and its inputs

A profile's `routing` configuration holds one decision table
(`idp_common.config.routing_models`): ordered routing rules, each with conditions
that must all hold, a list of `actions` and a `reason`, and a default outcome with
the same two. The one action type, `review_queue`, sends the document to a review
queue; an empty list means no review. The first routing rule that matches decides.
The built-in routing rules (`routing_models.builtin_rules()`) send an unreviewed
document to the Default queue when a section's extraction failed, when it has a
confidence alert, and when it has a missing required field and
`hitl.route_missing_required_fields` is on. That is exactly when `build_reasons`
gives a reason, so a profile that never changed its routing holds the documents
`build_reasons` has a reason for. The failed extraction is tried first: such a
section has no fields, so its required fields are missing too, and the first
routing rule that matches gives the reviewer the reason. A profile with a table of
its own holds a document with a failed section only when one of its routing rules
matches it.

`routing_dmn.to_decision_table(table)` is the table the engine evaluates. Its
outputs are `outcome` (`KIND_QUEUE` or `KIND_NO_REVIEW`), `queue` (the queue of the
outcome's `review_queue` action, read with `routing_models.review_queue_of`) and
`reason` (the reason as written, placeholders unfilled), with the default outcome as
their defaults. DMN export writes all of them; DMN import accepts a file with
`outcome` and `queue` alone, whose routing rules then have no reason.

`routing_inputs.build_inputs(document, config, table, mode=..., reader=...)` computes
the inputs the table's enabled routing rules use, and only those. The catalogue is
`ROUTING_INPUTS` in `idp_common.config.routing_validation`:

| Input | Path | Value |
|---|---|---|
| `document_class` | | The class of the first processed section |
| `section_classes` | | The classes of the processed sections |
| `class_present` | `<class>` | A processed section has that class, compared without regard to case. False when none has. `None` for a class the configuration does not have |
| `page_count`, `section_count` | | Numbers |
| `confidence_alert_count` | | The confidence reasons of the sections a reviewer has not confirmed. A section counts the larger of its alert list and its stored alert total |
| `missing_required_field_count` | | The required fields those sections' results leave absent |
| `extraction_failed_count` | | Those sections whose extraction failed without failing the document: an error-severity `extraction_failed` issue (`reasons.failed_extraction_issue`) |
| `route_missing_required_fields` | | The setting; true when unset |
| `field_value` | `[Class:]a.b[0].c` | The value at the path in every processed section's result, of the named class when one is named |
| `policy_rule_verdict` | `<policy_type>.<rule_key>` | `pass`, `fail`, `not_found` or `unknown`; `None` when the rule or the summary is not there |
| `failed_policy_rule_count` | `[policy_type]` | The rules whose verdict is `fail` |
| `reviewed` | | Every processed section's result carries a confirmed review record |
| `processing_mode` | | `pipeline` or `bda` |

- A processed section is one that is not excluded.
- The section results are read at most once each, at most 8 at a time, and the
  policy rule validation summary once. A table that uses only inputs on the
  document reads nothing.
- `InputReader(document, config, mode, s3, max_workers)` holds those reads.
  `decide` and `test_table` build one and pass it to `build_inputs(reader=...)`, so
  what the table read also serves the placeholders of the reason
  (`reader.value(condition)`) and the failed policy rules
  (`reader.failed_policy_rules(...)`), and nothing is read twice.
- A reference that cannot be resolved (a class the configuration does not have, a
  section result that cannot be read, a verdict asked of a document with no
  summary) reads as `None`, an empty list or 0. It is logged once per evaluation
  as `event=routing_stale_reference` and counted in `RoutingStaleReference`.

### The decision

`decide` works in this order. A step that lets the run continue names itself in the
log line (`reason=`):

1. A Test Studio document continues (`test_studio`).
2. A stack with no review table continues (`no_review_store`).
3. The finalize run of a review that was removed or skipped continues
   (`review_skipped`). One that was rejected ends as `rejected`
   (`review_rejected`). One that was completed continues when the run carries no
   run plan (`unplanned_finalize`): such a run applied the review in the assembly
   step and recorded nothing on the sections, so it is not evaluated.
4. With Review Routing off for the profile (`hitl.enabled`), the run continues
   (`hitl_off`).
5. With the Default queue missing or disabled, the run continues
   (`default_queue_off`). The definition is read with
   `default_queue(store, strict=True)`: a read that failed for a transient cause
   (throttling, a timeout) raises `TransientError`, so the state retries. A queue
   that could not be read for a moment is not a queue that is off. Any other read
   failure is logged and reads as no queue.
6. The table is evaluated. `no_review` continues (`rule` or `default_outcome`), and
   withdraws a live review that an earlier run of the document left
   (`superseded_no_review`).
7. A queue outcome places the document (`place_for_review`): a `HOLDING` item in
   the named queue, with the queue's priority. When that queue is missing or
   disabled the document goes to the Default queue and the item records why
   (`fallback_reason`: `queue_missing` or `queue_disabled`). Only now is anything
   read for what the reviewer sees: the reason and the failed policy rules (below).
   Neither is read for a document that is not placed.

| Item field | Meaning |
|---|---|
| `routed_by` | The routed-by record (below) |
| `reasons`, `sections` | The document's reasons, which can be none when a routing rule placed it. The sections in review are those of the reasons, or every processed section when there are none |
| `placement_count` | 1, and one more each time a routing rule places a document whose review was just completed |
| `processing_mode` | `pipeline` or `bda`: the mode of the run that placed the document, which is the mode the run after the review is planned in. The API returns it as `processingMode` on the item, so the edit page knows a BDA-mode item without reading the configuration (Edit Page Grouping is disabled there). `None` on an item placed by Send to review, whose mode is resolved from its profile when the review ends (see [Completing a review](#completing-a-review)) |
| `placed_layout` | The document's sections when it was placed: section id to a digest of the section's pages (`run_plan.restart.layout_of`). Completing the review compares the document with it. Written by `place_for_review` and by Send to review; empty on a document with more sections than `MAX_LAYOUT_SECTIONS` |
| `restart_step`, `restart_sections` | Where the run after a completed review starts, and the sections it extracts again. Written by `complete`; `None` and empty until then |
| `carried_metering_json` | For such a placement, the usage of the runs before it. The tracker adds it to the baseline when the item is activated |

- **A table that cannot be evaluated** does not fail the run. An unreviewed
  document goes to the Default queue with `fallback_reason = evaluation_error`. A
  document whose review was just completed continues, and its cycle's history gets
  `routing_evaluation_failed`. A storage failure does raise, and the state retries.
- **Placing a reviewed document again.** A routing rule can match a document whose
  review was just completed (a routing rule on `reviewed = true`, for a second
  opinion). The new cycle replaces the finalizing one, whose history gets
  `rerouted`. This can happen up to `routing.max_consecutive_placements` placements
  in a row (3 by default, 1 to 10); above it the run continues, the history gets
  `placement_limit_reached` and `RoutingPlacementLimit` is counted.
- **A retry** of the routing step finds the `HOLDING` item this execution wrote
  and keeps its cycle, its placement count and its carried usage.
- **History.** The new cycle gets `routed` (`queue`, `rule_id`, `rule_label`,
  `fallback_reason`, `routing_reason`, `reason_source`). The key is
  `routing_reason`, not `reason`, because the UI reads a history entry's `reason`
  as one a person chose. A live cycle of an earlier run that is replaced gets
  `superseded`.
- Every decision is logged as `event=routing_decision decision=… reason=… rule=…
  queue=… reason_source=…` and counted in `RoutingDecisions` with the dimension
  `Decision`. `reason` is why the decision was made (a `REASON_*` value) and
  `reason_source` where the reviewer's reason came from. `RoutingDecision`, which
  the handler logs, holds no text a reviewer reads.

### The routed-by record

`RoutedBy` (`models.py`) is what a placement records about the decision. It is
stored on the item as the map `RoutedBy`, with PascalCase keys.

| Field | Meaning |
|---|---|
| `table_id`, `rule_id`, `rule_label` | The table and the routing rule that matched. `rule_id` is empty when the default outcome applied |
| `profile`, `config_revision` | The configuration the document was routed with |
| `outcome_queue`, `placed_queue` | The queue the outcome named, and the queue the document is in. They differ when the named queue is missing or disabled |
| `fallback_reason` | Empty, or `queue_missing`, `queue_disabled`, `evaluation_error` |
| `inputs` | The input values the decision read, by expression text: at most 20, each cut to 200 bytes |
| `evaluated_at` | When the decision was made |
| `reason` | What the reviewer reads: the reason with the document's values in it, at most `MAX_RENDERED_REASON_BYTES` (1000) bytes |
| `reason_template` | The reason as written, placeholders unfilled; empty when the outcome has none. Kept also when it could not be rendered |
| `reason_source` | `admin` (the reason as written, rendered) or `fallback` (generated). Empty on an item stored before routing rules had a reason |
| `policy_failures` | The first `MAX_POLICY_FAILURES` (10) policy rules the document failed, each a `PolicyFailure` |
| `policy_failure_count` | How many policy rules the document failed in all |

`inputs`, `reason` and `policy_failures` are bounded when the record is built, in
`routing_step`, not by a validator, so an item that was stored always loads.

**The reason** comes from `_reason_for`. The outcome that applied carries its
reason as the table's `reason` output. With one, `routing_reason.render_reason`
fills its placeholders (`reason_source = admin`):

| Placeholder | Value |
|---|---|
| An input, with or without a path | The input read with the aggregate `any`: the value the table already read under that expression when it has one, otherwise `reader.value(...)`. A list input is therefore shown as its values |
| `{condition_N.value}` | `decision_tables.sfeel.display_values` of the N-th condition's test: the literal a comparison compares with, `low..high` for a range, `null` for `null` |
| `{confidence_threshold}` | `reasons.hitl_threshold(config)` |

With no reason, or when rendering raises, the reason is `fallback_text(rule,
values, evaluation_error=...)` (`reason_source = fallback`): the routing rule's label
(its id when it has none) and each condition with the value found, "applies to
every document" for a routing rule with no condition, "No routing rule matched."
for the default outcome, and a sentence saying the routing rules could not be
evaluated for `evaluation_error`. The metric `RoutingReasonFallback` counts it with
the dimension `Cause` (`no_reason` or `render_error`), and a rendering failure is
logged as `event=routing_reason_render_failed rule=<id> error=<type>`, with the
error's type only: its message can quote an extracted value. `_reason_for` never
raises, so a reason never fails or delays a placement.

**The rendered reason is never logged.** It can hold extracted values, so it is
stored on the item and its copies only. Reporting gets `reason_template`
(`tracker.reporting_info` → `ReviewReportingInfo.routing_reason` → the
`routing_reason` column of `document_reviews`): the reporting database is read
across profiles, so it gets no document values.

**The failed policy rules** come from `InputReader.failed_policy_rules`, for every
placed document that has any, whichever routing rule placed it. It reads the policy
rule validation summary (at most once for the whole evaluation) and keeps, in the
summary's order, each rule whose verdict is `fail`:

| `PolicyFailure` field | From the summary |
|---|---|
| `rule_id` | The entry's `rule_id` (`<policy type>.<rule key>`) |
| `policy` | The policy the entry is listed under |
| `name` | The id after its first `.` |
| `reason` | The entry's `reasoning` as one line (whitespace collapsed, control characters removed), cut to `MAX_POLICY_FAILURE_REASON_BYTES` (500) |
| `pages` | The entry's `supporting_pages` that are page numbers, the first `MAX_POLICY_FAILURE_PAGES` (10) |

The text is cleaned here and not by `rule_validation`, which this module must not
import. A document with no summary has none, an entry that cannot be read is
counted and left out, and a failure to read them is logged
(`event=routing_policy_failures_failed`) and leaves the placement as it is.

**Copies.** The record is copied, in part, to three places:

| Where | Fields |
|---|---|
| The status object (`status_object.build_status`) | `table_id`, `rule_id`, `rule_label`, `placed_queue`, `fallback_reason`, `reason`, `reason_source`, `policy_failures`, `policy_failure_count` (see [Review status contract](#review-status-contract)) |
| The tracking row (`tracker.after_completion`, attribute `ReviewRoutedBy`) | `tableId`, `ruleId`, `ruleLabel`, `reason`, `policyFailures` (`ruleId`, `policy`, `name`, `reason`, `pages`), `policyFailureCount`. The lookup function passes the map through, so the SDK (`ReviewInfo.reason`, `.policy_failures`, `.policy_failure_count`) and the CLI status have them without reading the item |
| The API views (`ReviewRoutedByView`, `ReviewPolicyFailureView`) | The status object's fields and `inputs`, as `routedBy` on a review item |

`test_table` resolves the queue and the reason as `decide` does, so
`RoutingTestResult` carries `reason` and `reason_source` for an outcome that holds
the document. It writes nothing, and whether a run reaches the table (the switch,
the Default queue, a Test Studio document) is not part of the test.

### Completing a review

`ReviewQueueService.complete` decides where the run after the review starts. The
reviewer does not choose a step, and neither does the caller: `completeReviewItem`
takes none. The decision is `run_plan.restart.decide_restart(document,
kind=REVIEW_COMPLETED, mode=..., baseline_layout=record.placed_layout)`, made
through `_restart_for`:

| The document, against the layout recorded at placement | Start | Sections |
|---|---|---|
| BDA mode, or no document loader | `apply_review_changes` | |
| The processing mode is not known | `apply_review_changes` | |
| No processed section was added, has other pages or lacks a result | `apply_review_changes` | |
| A processed section was added (`added`), has other pages (`pages_changed`) or has no result (`no_result`) | `extraction` | Those sections, with the cause of each |

- **The mode** is `_mode_of(record)`: the item's `processing_mode`, else the mode of
  the item's profile at its revision, read through the `configuration_of(profile,
  revision)` callable the service is built with (the review queues resolver passes
  `resolve_processing_mode`). It is unknown when no callable was given, when it
  raises, and when it answers with no configuration: `resolve_processing_mode` then
  answers Pipeline mode as a default, and a guessed Pipeline mode on a BDA document
  would start it over and lose the corrections. An unknown mode extracts nothing.
- **A processed section** is one that is not excluded. With an empty layout only a
  missing result sends a section to extraction.
- **Sections that do not fit a run plan** (more than a plan names, or a plan over
  its size limit) are not named: the plan then extracts every processed section.
- **Order.** The cycle, state and holder checks run first (`_check_disposable`), so
  a caller who cannot complete the item causes no read, no copy and no withdrawal.
  A document that no longer exists has its review withdrawn (`document_missing`)
  and the call is refused with `DOCUMENT_NOT_FOUND`.
- **Base copies.** For an extraction start, before the item is written, the result
  of every section the run extracts again that has a draft is copied to
  `<docKey>/review/base/<cycle>/<section>.json` (`BASE_KEY_FORMAT`), under the
  resolver's existing `*/review/*` grant. It is the result the reviewer started
  from, which extraction then writes over. The copy comes before the write because
  the run can start once the item is `FINALIZING`. A failed copy raises, and the
  review stays as it was: a second attempt makes the same copies.
- **What is stored.** `restart_step` and `restart_sections` on the item, and both in
  the `completed` history entry (the sections cut to the first 50). The finalize
  sender reads the plan off the item (see
  [Dispositions and durable finalize](#dispositions-and-durable-finalize)), so a
  resend by the sweeper carries the same plan.

`completion_plan(caller, object_key, cycle_id)` is the read behind
`getReviewCompletionPlan`. It returns a `ReviewCompletionPlanView` with two
`run_plan.planned.PlannedRun`s, `complete` and `send_back`: the step each run starts
at, every step with its state (`run`, `skipped`, `not_enabled`), and for a completion
that extracts again the sections with their causes.

- It is for the item's holder, and for an Admin on an item that is `OPEN` or
  `CLAIMED` (`_holder_or_admin`, the rule of `authorize_send_back`); anyone else is
  refused with `NOT_LEASE_HOLDER`. `getReviewItem` also admits the queue's other
  members; this read does not.
- The completion is decided through `_restart_for` with the mode `complete` uses, so
  the dialog shows what `complete` then stores. With an unknown mode both plans list
  the steps of Pipeline mode, and the completion starts at `apply_review_changes`.
- The states come from `feature_flags.step_enabled(config)` for the item's
  configuration; with no configuration read, every step is listed as enabled.
- It reads only, except that the review of a document that no longer exists is
  withdrawn, as opening the item does.

### After a completed review

The finalize run of a completed review carries the run plan kind `review_completed`.
It starts at `apply_review.apply_review_changes`, or at extraction for the item's
`restart_sections`, in which case the state machine reaches the same step after the
extraction and the assembly (`CheckReviewToApply`). The step works per processed
section:

| The section | Its result | Its review record | On the document |
|---|---|---|---|
| Not extracted again | The draft is written over it (`apply_corrections`) | `review_record`: `confirmed: true`, `changed_paths` from the draft | Alerts, alert total and missing required fields cleared |
| Extracted again, with a draft | Each correction is set on the new result (`merge_corrections`) | `extracted_review_record`: `confirmed: false`, `extracted_again: true`, `changed_paths` (applied), `unapplied_paths` | Alerts and missing required fields on an applied path removed, the others kept; alert total cleared |
| Extracted again, no draft | Unchanged | `extracted_review_record` with no paths | Kept |

The sections extracted again are the item's `restart_sections`, or every processed
section when the item's `restart_step` is `extraction` and the list is empty.

```json
"review": { "v": 1, "cycle_id": "…", "reviewed_by": "reviewer@example.com",
            "reviewed_at": "2026-11-16T10:00:00.000Z", "confirmed": true,
            "changed_paths": ["Total", "LineItems[2].Amount"], "changed_paths_truncated": false }
```

**The correction merge** (`merge_corrections(base, draft, new)`). `base` is the copy
kept when the review was completed (`{}` when there is none), `draft` the
reviewer's corrected copy of it and `new` the result of the new extraction. A
correction is a leaf path under `inference_result` whose value in the draft differs
from the base (`draft_diff.leaf_values`). Each one the draft holds is set on `new`
with `idp_common.field_paths.set_path`, which sets a path only where every container
before its last step exists. The rest are recorded as unapplied: a path that no
longer resolves (a list row the new extraction does not have), and a value the
reviewer removed, which is not removed from a new extraction. An alert or a missing
required field counts as answered when it is on an applied path, under it or over
it. A base copy that is missing reads as empty; any other read error fails the step.

`reviewed_at` is the time the review was completed, `changed_paths` come from the
section's draft or the merge (at most 200 paths), and the base copy and the draft do
not change, so running the step again writes the same bytes. The document's two
counts are set from its sections (`run_plan.clearing.recount`). The run then
validates the policy rules again and reaches the routing step. `routing_inputs`
reads `review.confirmed`, so a document whose every processed section is confirmed
reads as reviewed, and a section that was extracted again counts again: for the
alert count, the missing required fields and `reviewed`.

### A state machine definition with no routing step

A run that started before an upgrade finishes on the definition it started with,
which has no routing step, while calling the new Lambda code. For it the assembly
step still decides, through `route_for_review(document, config, mode=...)`:

- **Hold.** A run with reasons writes a `HOLDING` item in the Default queue and
  returns `review_hold`; the definition's Choice rule on `$.Result.review_hold`
  ends phase 1 with `HITL_IN_PROGRESS`.
- **Finalize run.** A run whose execution name matches the item's finalize
  execution applies the drafts (`apply_corrections`) on `complete`, or nothing on
  `rejected`, `removed` or `skipped`, and returns `review_outcome` (`removed` and
  `skipped` read as `complete`).
- **No reasons.** A fresh run that needs no review withdraws a live item and logs
  `event=review_no_reasons`.

On a definition that has the routing step, the assembly step recognises a finalize
run (`recognize_finalize(..., apply=True)`). For one that carries no run plan it
copies the drafts onto the section results, so the corrections land before policy
rule validation. For one that started at extraction it copies nothing, and the
apply-review step that follows merges the corrections.

The WorkflowTracker activates a `HOLDING` item when the run succeeds (→ `OPEN`,
writes the `in_review` status object, announces the queue; an item whose queue no
longer exists opens in the Default queue) and records the document's completion
when the finalize run ends.

### Send back

A reviewer who finds that a document needs processing again, not correcting, sends
it back. `sendBackReviewItem` takes the document, the cycle and an optional note,
and no start step: `SendBackArgs` forbids other arguments, so a request that names
one is a bad request. `ReviewQueueService.authorize_send_back(caller, object_key,
cycle_id)` says who may: the holder of the item, and an Admin for an item that is
`OPEN` or `CLAIMED`. The caller then asks the reprocess service
(`idp_common.run_plan.reprocess`) for a `sent_back` run, which starts at
`SEND_BACK_START` (classification; the beginning in BDA mode, and for a document
that lacks the page text classification reads) and withdraws the review with
`withdraw(..., actor=..., expected_cycle=..., history_event="sent_back",
details={"start_step": ..., "note": ...})`. `start_step` is the step the run starts
at after aliases. `expected_cycle` refuses (`CYCLE_CHANGED`) when the review was
replaced in the meantime. The run keeps the revision the document is pinned to, is
never marked reviewed, and its routing decides again: the next placement is counted
from one.

## Regrouping during review

Correcting a document's split is part of review. `updateDocumentSections` accepts a
document that is `HITL_IN_PROGRESS` and queue-managed `in_review` when the caller holds
its `CLAIMED` item (`regroup_refusal`); an `OPEN` item must be claimed first, a
`FINALIZING` one is refused, and anyone but the holder gets `NOT_LEASE_HOLDER`. An
Annotator is admitted to a production document only as the holder of its `CLAIMED`
item, within their `allowedConfigVersions` (`_assert_holds_review_item` in
`process_changes_resolver`); a test-set document is decided by their
`allowedTestSets`. The legacy HITL flow is unchanged.

- **BDA mode.** A regroup of a document in review that runs in BDA mode is refused
  by the resolver (`_assert_not_a_bda_structural_edit`, a 400 with `BDA_MODE:`): a BDA
  run keeps the sections a document has and cannot extract one again. The edit page
  disables Edit Page Grouping for an item whose `processing_mode` is `bda`.
- **Drafts.** Section ids are kept across a regroup, so a draft stays with its
  section. After the regroup is saved, `discard_drafts(store, doc_key, removed_ids)`
  deletes the current cycle's draft record of every section the regroup removed
  (`ReviewStore.delete_draft`): a later regroup may give that id to a new section,
  and a new section starts with no corrections. It never raises. A delete that
  fails is logged at WARNING (`event=review_draft_discard_failed`) and the regroup
  stands, which is why a test pins the resolver role's `dynamodb:DeleteItem` grant
  on the review table. The item's `has_draft` flag is left as it was.
- **What the run extracts.** Nothing is extracted when the regroup is saved.
  Completing the review compares the document with the item's `placed_layout` and
  extracts again each section that was added or has other pages (see
  [Completing a review](#completing-a-review)); the corrections saved for such a
  section are set on its new result (see
  [After a completed review](#after-a-completed-review)).

## Queues and assignment

- A queue is identified by its `id`, which items, table keys (`Q#<id>`) and the
  API's queue arguments use. Its `name` is display text. Admins manage queues with
  `listReviewQueues`, `createReviewQueue`, `updateReviewQueue` and
  `deleteReviewQueue`, which are `ReviewQueueService.list_queues`, `create_queue`,
  `update_queue` and `delete_queue`. Each checks that the caller is an Admin, so
  every caller of the service gets the same rule. `list_queues` returns one page
  of definitions ordered by id and the next page's token, read past the cache. A
  routing rule names a queue by its id. Only Default can't be deleted (`PROTECTED_QUEUE`); an
  Escalation queue an earlier release seeded is an ordinary queue.
  `ReviewQueueService.delete_queue` refuses with `QUEUE_FINALIZING` while one of
  the queue's items is `FINALIZING`, then deletes the definition, and only then
  moves the queue's `OPEN` and `CLAIMED` items to Default with `move` (unassigned;
  an assignee is told) and re-points its `HOLDING` items at Default. So a delete
  the store refuses (`QUEUE_CHANGED`, or a fault) has moved nothing. A run that
  stops part-way leaves items in a queue that is no longer defined, which the rest
  of the service handles (a `HOLDING` item activates into Default; the overview
  lists the queue); deleting it again moves the rest, and the web UI offers Delete
  on such a queue while it still holds items.
- Every definition carries a `version` (`Version` in the table), which
  `QueueDefStore.put` sets to 1 on create and bumps by one on every update; a
  definition stored before versions existed reads as 0, and so does one whose
  `Version` is not a non-negative whole number (logged as
  `queue_definition_version_invalid`): it stays listed. A write at version 0
  matches a missing `Version`, one that is not a number, and a number of 0 or
  below, and stores a well-formed version. A positive fraction is not matched,
  because a condition expression cannot test for one, so such a queue stays listed
  but is refused as `QUEUE_CHANGED`; nothing here writes one. `delete` keeps the last
  version under `QDEFGONE` / `Q#<id>`, and a create under that id continues from it,
  so a version never repeats for an id. Both are single transactions conditional on
  the versions they read (the definition's for a delete, the retired one for a
  create), retried a few times when a concurrent write moves one; a create or
  delete that cannot settle is refused with `QUEUE_CHANGED`. Given `expected_version`,
  an update is a conditional write and is refused with `QUEUE_CHANGED`
  (`ReviewConflict`) when the stored version has moved on, so two Admins' saves
  built from one read cannot silently overwrite each other; a missing queue is still
  `UNKNOWN_QUEUE`. Without it, an update replaces what is stored, retrying a few
  times if a write lands between its consistent read and its put.
  `updateReviewQueue` passes the input's optional `version` through, or, when an
  older client sends none, the version of its own consistent read
  (`read_current`): the client's fields replace what that read found, and it is
  refused only if another write lands in between, which protects the fields the
  resolver merges from the stored definition. So the store's own unversioned path
  is not reached through the API. `listReviewQueues` calls `list(fresh=True)`, a
  strongly consistent read past the 30 s cache, since it is what an Admin edits
  from.
- `create`/`updateReviewQueue` take `enabled`. A new queue is enabled; an update
  that leaves it out keeps the stored value, and a definition stored without it
  reads as enabled. Queues have no confidence threshold: a `confidenceThreshold`
  sent by an older client is ignored, and a stored `ConfidenceThreshold` attribute is
  ignored on read and dropped on the next save. Only the Default queue's switch
  routes documents.
- A queue's `eligible_annotators` is its membership (`QueueDefinition.allows`): an
  Annotator works only the queues that list them, and an empty list means none.
  Admins work every queue (`QueueDirectory.is_member`). Taking an Annotator off a
  queue's list stops them seeing the queue and claiming from it, but an item they
  already hold stays theirs (`_check_access` admits the holder of a `CLAIMED` item)
  until they finish or release it, or an Admin unassigns it with
  `forceReleaseReviewItem`. Queue definitions are cached for 30 s per Lambda
  instance, so a list change can take that long to reach every instance.
- For a caller who may not work an item, the access check (`_check_access`: scope,
  then membership) runs before any argument is checked against the item's queue
  definition, so `completeReviewItem`'s checklist and `rejectReviewItem`'s reason are
  only validated for someone allowed to work the item.
- Work reaches an Annotator two ways. They **claim** an item from one of their
  queues (`claimReviewItem`, `claimNextReviewItem`), or an Admin assigns it with
  `reassignReviewItem` (and unassigns it with `forceReleaseReviewItem`). Either way
  the Annotator must be in the `Annotator` group and on the queue's list.
- An Admin works an item by holding it too. They claim an `OPEN` item with
  `claimReviewItem` (the web UI does this on an Admin's first draft save or Complete
  of an unassigned item), and take an item another reviewer holds with
  `takeOverReviewItem`. Both go through the same counters and history as an
  assignment. Internally,
  both are a claim whose lease is `ASSIGNMENT_LEASE_MINUTES` (ten years), so it
  never expires, and the per-reviewer cap is lifted to `ASSIGNMENT_CAP`.
- The queue's `time_limit_minutes` schedules one `review.task_reminder` to the
  holder if the item is still open after that long. The item stays assigned.
- **Skip** is `releaseReviewItem` by the holder: the item goes back to the queue
  `OPEN`, unassigned, with its draft kept. It takes a `reason` from the queue's
  `skip_reasons` (`DEFAULT_SKIP_REASONS` unless the queue sets its own;
  `REASON_REQUIRED` or `BAD_ARGUMENT` otherwise) and an optional `note`, both
  written to the `released` history event. `getReviewItem` returns the list as
  `skipReasons`.
- An Annotator listed on a queue may open its items read-only (`getReviewItem`);
  saving, completing and rejecting need the claim. `getReviewItem` also returns
  `queueName`, the queue's display name, for the web UI's breadcrumbs. The holder keeps access to their
  item even after being taken off the list.
- **Profile scope is per item.** A queue is system-wide, so it can hold items of
  several profiles. A caller with `allowedConfigVersions` sees and works only the
  items whose own profile that scope admits: `ReviewQueueService._sees`, which uses
  `idp_common.config_scope.scope_allows` and so admits no item without a profile to a
  scoped caller. It applies to the per-item operations and to every listing
  (`list_queue_items` for one queue or all of the caller's, `get_my_queues`,
  `list_my_work`, `claim_next`). A listing that leaves items out still pages
  forward, so a page can be short; `claim_next` reads past the items it leaves out,
  up to `CLAIM_NEXT_MAX_PAGES` pages of a queue. `QueueDirectory.in_scope` is for a
  queue reference, which has no profile, never for an item. `get_my_queues` applies
  the same decision to every count: for a scoped caller, a queue's depth, unclaimed
  and claimed counts, its oldest enqueue time, and the caller's own `claimedByMe` and
  `myClaimCount` cover only the items the caller's scope admits (one projected
  QueueIndex pass per queue, `ReviewStore.queue_scope_rows`). Admins and unscoped
  callers see the whole queue through the cheap counts. The claim cap
  (`max_claims_per_reviewer`) still counts all of the caller's claims, in scope or
  not.
- Admins also have `renewReviewLease`, which the web UI doesn't use.
- **Test Studio** queues are the test sets. `testset_docs.TestSetDocStore` holds
  each document's summary and assignment (`assignTestSetDocuments`,
  `unassignTestSetDocuments`, `claimTestSetDocument`, `listMyTestSetTasks`). A
  set's queue is worked by the Annotators in its `QueueEligibleAnnotators`; listing
  one adds the set to their `allowedTestSets`. `claimTestSetDocument` is
  `assign(..., only_if_unassigned=True)`: it fails `ALREADY_CLAIMED` or
  `ALREADY_REVIEWED` instead of overriding. While a document is assigned,
  it carries `OwnerPK=RT#<annotator>`, so My Work › Tasks is one `OwnerIndex`
  Query, and `SweepPK=TS` at the set's time limit for the reminder. When every
  section is reviewed, `complete_section_review` ends the assignment.
  `skipTestSetDocument` (`unassign`) hands the document back: the assignment ends,
  the document is unassigned again and still counts as remaining, and its labels
  are left alone. It takes a `reason` from the set's `skipReasons` (its queue
  settings, or `DEFAULT_SKIP_REASONS` until one is saved; `getAnnotationQueue`
  returns the list) and an optional `note`; `unassign(skip=...)` appends who, when,
  the reason and the note to the document's `SkipHistory` in the same write. Documents skipped before this behaviour carry `Skipped`,
  `SkippedBy` and `SkippedAtMs` (`mark_skipped`); summary rebuilds keep those,
  `assign` clears them, and `assignment_counts` and `getAnnotationQueue`'s
  `completedDocs` still treat such a document as completed.
- **Summary builds** never run in a queue request. `request_summaries` marks the
  set's metadata `SummaryStatus=BUILDING` (conditionally, so concurrent readers send
  one request; a build older than 15 minutes is requested again) and sends
  `{"action": "summarize", "testSetId": ...}` on the TestSetFileCopyQueue. The copier
  summarizes the whole set and `mark_summaries_ready` records `SummaryVersion`,
  `SummaryStatus=READY`, `SummaryCount` and the reviewed count. Until then
  `getAnnotationQueue` returns the summaries written so far with
  `summaryStatus: BUILDING`.
- **The Test Studio queue list** is `listTestSetQueues` (every set for an Admin in
  Review Management, only the sets listing the caller for an Annotator in My Work ›
  Queues): one DynamoDB read
  of the sets' metadata items, with the counts the build, assignments and reviews
  keep there (`fileCount`, `QueueReviewedDocs`, `QueueAssignedDocs` via
  `store_assigned_count`). It reads no labels and no queues.

## Drafts

`saveReviewDraft` stores a section's whole edited result at
`<docKey>/review/drafts/<cycle>/<section>/<revision>.json` (at most 2,000,000
bytes) and records it as `DRAFT#<cycle>#<section>` with the changed field paths.
The paths are the difference between the draft and the section's stored result
(`draft_diff.changed_fields`), read on every save, so `changed_fields` covers every
save of the cycle and not only the last one. `baseRevision` makes concurrent saves
fail with `STALE_DRAFT`. Drafts survive unassignment, reassignment, take-over and
move. A regroup deletes the draft of a section it removes (see
[Regrouping during review](#regrouping-during-review)), and completing a review
that extracts a drafted section again keeps a copy of the result the draft was made
over, under `<docKey>/review/base/<cycle>/` (see
[Completing a review](#completing-a-review)). The `review/` segment is
not part of any run manifest and is deleted by Reprocess. A top-level `review`
key in the posted result is dropped: the review record of a section result is
written when a completed review is applied, never by a draft.

## Test Studio labels and drafts

`testset_labels.TestSetLabels` writes a Test Studio document's labels. Both
`saveTestSetDocumentSection` (authored ground truth) and `completeSectionReview` (a
document with a review copy) call `write_reviewed`, so a reviewed label always carries
the same provenance: `labelSource = reviewed-human`, an `_editHistory` entry naming the
editor, the source and the changed fields (`field_diffs`, the flattening the curve
uses; the trail keeps the newest `MAX_EDIT_HISTORY_ENTRIES`), and the curve
observations (`record_curve_observations`, only when the replaced label carries
confidences). The handlers keep the summary, count and HITL bookkeeping;
`saveTestSetDocumentSection` also ends the assignment once every section is reviewed.

A **section draft** is a section's saved edits that are not reviewed and start nothing
(`saveTestSetDocumentSectionDraft`):

- **Where:** `<testSetId>/drafts/<objectKey>/sections/<sectionId>/<revision:06d>-<suffix>.json`
  in the TestSetBucket (each write has its own object), indexed by a top-level
  `Draft#<sectionId>` map on the document's `TSDOC` item (`Revision`, `S3Key`,
  `UpdatedAt`, `UpdatedBy`; `keys.tsdoc_draft_attr`). `TestSetDocStore.view` lists them
  as `sectionDrafts`, and `getAnnotationQueue` returns them as `draftSections`.
- **Writes:** `save_draft` writes the object, then a conditional `UpdateItem` on the
  revision the editor last loaded (`baseRevision`); a stale write fails `STALE_DRAFT`
  and its object is deleted, and a successful one deletes the previous revision's.
  It refuses `BAD_ARGUMENT`, `DRAFT_TOO_LARGE` (over `MAX_SECTION_LABEL_BYTES`),
  `NOT_IN_SET` and `NO_LABEL`. The label, the summary fields (`Reviewed`,
  `LabelSource`, `ReviewedSections`, `UpdatedAt`), the queue counts and the curve are
  untouched.
- **Lifecycle:** `write_reviewed` deletes the section's draft; `move_drafts` makes
  drafts follow their sections through a regroup, rewriting each draft's
  `split_document.page_indices`, `document_class.type` and `extractedAsClass`
  (`EXTRACTED_AS_CLASS_KEY`: the class the section's fields were extracted under,
  which the regroup records on the label when it first changes the class and removes
  when the class is changed back; #1816), and drops a removed section's; `delete_document_drafts` runs when a document is removed from the set.
  Skip, Clear draft labels and Reset labels keep drafts. A re-extract's write-back
  deletes the written section's draft (`delete_draft`; the job carries
  `reextract: true`), since its edits were made over the fields just replaced; a
  Generate draft labels write-back only removes `extractedAsClass` from it
  (`clear_draft_extracted_as`, in place, revision unchanged). Publishing, the
  version snapshot, test runs, the content signature and the summaries read
  `baseline/` only.

## Dispositions and durable finalize

| Operation | Who | Effect |
|---|---|---|
| `completeReviewItem` | the assignee | → `FINALIZING`, disposition `complete`; the queue checklist answers are recorded, and where the run after it starts (`restart_step`, `restart_sections`; see [Completing a review](#completing-a-review)) |
| `rejectReviewItem` | the assignee | → `FINALIZING`, disposition `rejected`, a reason from the queue's `reject_reasons` and an optional note, both kept on the item and in the `rejected` history event. The edit screen's **Reject** asks for the reason; for an Admin on an unassigned item it assigns the item first, since only a held item can be rejected |
| `reassignReviewItem`, `forceReleaseReviewItem` | Admin | assign to an eligible Annotator, or unassign (→ `OPEN`, draft kept) |
| `takeOverReviewItem` | Admin | an item `CLAIMED` by someone else → `CLAIMED` by the calling Admin, draft kept; a `taken_over` history event, both claim counters moved, and the former holder told (`review.claim_released`). Refused with `NOT_ASSIGNED` for an `OPEN` item (claim it instead), `ALREADY_YOURS` for the Admin's own, and `INVALID_STATE` once the item has left review |
| `moveReviewItem` | Admin | move to another queue (ends any assignment) |
| `removeReviewItem` | Admin | an `OPEN` or `CLAIMED` item → `FINALIZING`, disposition `removed`, with an optional note in a `removed` history event; an assignee is released and told. No draft is applied: the document ends `COMPLETED` with its machine results, the item ends `COMPLETE`, the status object is **deleted** rather than written, and `document_reviews` records disposition `removed` |
| `bulkSkipAllReviews` (`ReviewQueueService.skip_all_reviews`) | Admin, or a Reviewer in scope | an `OPEN` item, the caller's own claim, or (Admin) any claim → `FINALIZING`, disposition `skipped`, finished like `removed` (machine results, status object deleted), with a `skipped` history event carrying the reason (`SKIP_ALL_REASONS`), note, source and job id, and `skip_job_id` on the item. A claimant other than the caller is told. Another reviewer's claim is refused for a Reviewer (`HELD_BY_OTHER`). A finalize message that could not be sent adds a `finalize_send_failed` history event; the sweeper resends it |

A disposition writes `FINALIZING` with a finalize token `rvw-<32 hex>` derived from
the document, cycle and attempt, then sends one message to the DocumentQueue. The
queue processor names the finalize execution after the token, so a re-sent message
collapses into `ExecutionAlreadyExists`. The message carries a run plan
(`FinalizeSender.plan_for`), which is a function of the item, so a re-sent message
carries the same plan:

| Disposition | Run kind | The run starts at |
|---|---|---|
| `complete`, the item's `restart_step` is `extraction` | `review_completed` | Extraction, for the item's `restart_sections` (every processed section when the list is empty); then applying the review and the steps below. `reviewed` is false |
| `complete`, otherwise | `review_completed` | Applying the review (`apply_review_changes`), then policy rule validation, routing, summarization and evaluation. `reviewed` is true. An item completed before the start was recorded has no `restart_step` and takes this row |
| `removed`, `skipped` | `review_skipped` | Policy rule validation; the routing step does not hold the document again |
| `rejected`, or anything else | `review_rejected` | The rejected end |

For `complete`, `removed` and `skipped` the queued document's policy rule
validation result, summary and evaluation are cleared, because the run produces
them again. A run that starts at extraction also has the fields of the sections it
extracts again cleared (`clear_for_start(..., keep_objects=True)`). Only fields are
cleared: nothing is deleted in S3, because the stages write over their own outputs
and the role that sends the message may delete only under the document's `review/`
folder. A run that starts at extraction passes the Pipeline assembly step, which
recognises the finalize run (`recognize_finalize(apply=True)`) and copies no draft
over a result when the item's `restart_step` is `extraction`: the results are new,
and the apply-review step merges the corrections into them. The finalize run starts from a fresh
metering record: when an item is activated, the tracker stores the document's
cumulative usage up to phase 1 (`cumulative_metering`) on the item as
`phase1_metering_json`, and the finalize run carries it as its `metering_baseline`.
The tracking row then holds phase 1 plus the finalize run, while reporting, the
`DocumentEstimatedCost` metric and the finalize run's version record get the
finalize run's own usage only. If the message is lost or the run fails, the sweeper resends the
same token, then retries with a new token up to 3 attempts before
`FINALIZE_FAILED`.

## Bulk Skip All Reviews jobs

Rows in this table, none with a GSI key:

| PK | SK | Holds |
|---|---|---|
| `BRJOB#<jobId>` | `META` | The job: requester, mode and selection, reason, note, resubmit, status (`QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`), `Total`, `Processed`, `Counts` per outcome, the page cursor and wake tokens, heartbeat, and `ExpiresAfter` (30 days, or the data retention if shorter) |
| `BRJOB#<jobId>` | `K#<seq>` | A selected-mode key chunk (25 object keys) |
| `BRJOB#<jobId>` | `R#<sha256(objectKey)[:32]>` | One document's outcome, put once (`attribute_not_exists(SK)`); the counters move only when the put succeeds |
| `BRJOBS#U#<reviewerKey>`, `BRJOBS#ALL` | `<createdAt>#<jobId>` | The requester's and the all-jobs (Admin) lists |
| `BRJOB#ACTIVE` | `LOCK` | The one-running-job lock, with a lease |

A request at or under the inline limit runs in the resolver and is recorded as a
`SYNC` job; a larger one is an `ASYNC` job advanced by `BulkReviewWorkerFunction` from
`BulkReviewJobQueue`. A step defers (re-sends itself 60 s later) while another job
holds the lock or while the DocumentQueue holds at least half of
`MaxConcurrentWorkflows`, and hands itself to its next wake-up when the invocation runs
low on time. A legacy resubmit carries `ResubmitIdempotencyToken = rsb-sha256(jobId|key)`,
which `queue_processor` uses only to name the execution, so a retried step starts no
second run. A dead-lettered job message, or a step that fails on its last receive,
marks the job `FAILED`; a job with no heartbeat for 30 minutes reads as `stalled`.
The requester's scope is snapshotted at submission and intersected with their current
scope once per step; a by-profile job whose profile leaves the scope fails
(`scope_revoked`).

`cancel_job` (the requester or an Admin; another user's job reads as not found) sets a
`QUEUED` or `RUNNING` job to `CANCELLED` with a conditional write, records
`CancelledBy` and notifies the requester; a finished job is refused (`JOB_FINISHED`).
The worker reads the status before each page and stops, releasing the lock, so at most
the page in progress is still skipped. Its start, finish and fail writes are
conditional on the job still being active, so a cancel is never overwritten.
`delete_job` deletes a finished job's `META` (conditional on a final status) and its two
list entries; an active job is refused (`JOB_ACTIVE`). The chunk and result rows are
unreachable without `META` and expire with it (`ExpiresAfter`), so a job of any size is
deleted in a bounded number of writes.

## Send to review

`SendToReviewService.send` creates a `HOLDING` item with `origin="manual"` and the
`manual_request` (reason from `SEND_TO_REVIEW_REASONS`, note, requester, and the
tracking row's prior `HITLStatus`/`HITLCompleted`), then conditionally marks the
tracking row `in_review` (the commit point: finished, production, not in a review),
publishes the `in_review` status object, and moves the item to `OPEN` with the
document's cumulative usage as `phase1_metering_json`. A failure after the commit point
is undone by `abandon`, which restores the row, deletes the status object and deletes
the item; the sweeper does the same for a manual item it finds still `HOLDING`. The
send reason is shown to reviewers as the item's `sentToReview`.

## Sweeper

`ReviewQueueSweeperFunction` (reserved concurrency 1, every 5 minutes) walks the 4
`SweepIndex` shards and, per due item: sends an assignment's time-limit reminder
(`review.task_reminder`); replays a lost
tracker event for a `HOLDING` item whose phase 1 succeeded, deletes one whose
phase 1 failed; resends, retries or fails a stuck finalize; withdraws an item whose
tracking row is gone. It sends the Test Studio assignment reminders (`SweepPK=TS`),
refreshes the queue registry (`QREG`) and emits `ReviewQueueDepth`, `ReviewLeaseExpiries` and related
metrics. Every write is conditional on the item `Version`, so a concurrent writer
always wins.

## Notifications

Inbox messages (see [notifications](../notifications/README.md)) that reach an
Annotator are `review.item_reassigned` (an Admin assigned an item or Test Studio
document to you), `review.claim_released` (an item you hold was unassigned,
reassigned, taken over or moved, or an Admin completed, rejected or removed it) and
`review.task_reminder` (still open after the queue's time limit, once per
assignment). The producers `review.queue_items_waiting` (debounced per queue, 10
minutes), `review.claim_expiring` and `review.claim_expired` exist but send nothing in
this release: arrivals in a queue are announced to nobody (`QueueDirectory.members`
returns no recipients) and assignments do not expire. Admins see waiting work in
Review Management, whose counts (pending, assigned, in progress, annotators) come from
`getReviewQueuesOverview` for production queues and from the test sets' metadata
(`store_assigned_count`) for Test Studio queues. Links point at the document's edit
page, `/documents/<encoded key>/edit` (`idp_common.notifications.links.document_edit_link`),
which opens the item while the document is in review. The UI redirects an older
`/review-queues/items/<encoded key>` link there, keeping its query.

## Review status contract

This is the interface for consumers that act on a reviewed document.

- **Key:** `s3://<OutputBucket>/<documentKey>/review/status.json`. Only documents
  routed to a review queue have one.
- **Schema:** [`status_object.schema.json`](status_object.schema.json)
  (`schema_version` `"1.1"`). The file ships with the package:
  `importlib.resources.files("idp_common.review_queues") / "status_object.schema.json"`.
  Version 1.1 adds to 1.0 and removes nothing: the reason kinds
  `missing_required` and `extraction_failed`, and the optional `routed_by`
  (`$defs.routedBy`, with
  `$defs.policyFailure` for the entries of its `policy_failures`).
- **Values:** `status` is `in_review`, then `complete` or `rejected`. `sequence`
  increases by one with every write; `cycle_id` names the review cycle;
  `machine_output` points at the phase-1 run manifest, and `final_output` at the
  finalize run's manifest once finished.
- **`routed_by`** names the routing rule that placed the document, and says what
  its reviewer is told. Every field is present when the object is:

  | Field | Value |
  |---|---|
  | `table_id`, `rule_id`, `rule_label` | The table and the routing rule; `rule_id` is empty when the table's default outcome applied |
  | `placed_queue` | The queue the document is in |
  | `fallback_reason` | Empty, or `queue_missing`, `queue_disabled`, `evaluation_error` |
  | `reason` | What the reviewer reads: the routing rule's reason with the document's values in it. It can hold extracted values |
  | `reason_source` | `admin` for the reason as written, `fallback` for the generated one. Empty, with an empty `reason`, for a placement stored before routing rules had a reason |
  | `policy_failures` | The first policy rules the document failed, whichever routing rule placed it. Each entry has `rule_id`, `policy`, `name`, `reason` (the explanation policy rule validation gave) and `pages` (integers) |
  | `policy_failure_count` | How many policy rules the document failed in all; `policy_failures` can list fewer |

  `routed_by` is `null` for an item no routing rule placed: one sent to review by
  hand, and one placed before routing rules existed.
- **Ordering:** `in_review` is written before the item is claimable. `complete` and
  `rejected` are written **last**, after the tracking status, the reporting rows and
  the final manifest, so a consumer that sees them can read everything else.
- **Deletion** means the review was withdrawn (reprocess, abort, delete, a new
  run that needs no review, or an Admin removing the document from review).
- **Events:** the OutputBucket sends S3 events to EventBridge. Events are
  at-least-once and unordered, so read the object and use `sequence` rather than
  trusting event order. Example rule pattern:

```json
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created"],
  "detail": {
    "bucket": { "name": ["<OutputBucket>"] },
    "object": { "key": [{ "suffix": "/review/status.json" }] }
  }
}
```

The tracking row mirrors the status in `HITLStatus` (the same three values), with
`ReviewQueue`, `ReviewQueueName`, `ReviewCycleId`, `ReviewStatusUri`,
`HITLReviewedBy`, `ReviewDisposedAt` and `ReviewRejectReason`. The SDK and CLI
report it as `review_status`. `ReviewRoutedBy` is the routed-by record's copy on the
row (see [The routed-by record](#the-routed-by-record)): the routing rule, the
reason and the failed policy rules, which is where the SDK and the CLI status read
them. It is removed when a placement has no routed-by record, so the row never
shows the routing rule of an earlier one.

It also carries who holds and who finished the review, so the document page's **Review
Owner** and **Review Completed By** are filled for a queue document as for the legacy
flow:

- `HITLReviewOwnerEmail` is the item's assignee. `ReviewQueueService._write` mirrors it
  after every write that changes who holds an item: a claim, an assignment, a
  reassignment or a take over writes the new holder; a skip or an unassignment (back to
  `OPEN`) clears it; a finish (`FINALIZING`, then `COMPLETE`, `REJECTED` or removed)
  keeps the last holder. A withdrawal clears it with the other review attributes. The
  mirror is best-effort: the queue item is the source of truth, and a tracking row that
  cannot be written is logged (`event=review_owner_mirror_failed`) and never undoes the
  write. Only the email is written; `HITLReviewOwner` is the legacy Reviewer flow's
  Cognito username, and is only cleared.
- `HITLReviewedBy` and `HITLReviewedByEmail` are both the email of whoever finished the
  review, written by the tracker when the finalize run ends.

## Producer IAM

The policies in `template.yaml`, `patterns/unified/template.yaml` and
`nested/api-resolvers/template.yaml` are authoritative; in outline:

| Producer | Grants beyond its pre-existing role |
|---|---|
| ProcessResults (Pipeline and BDA) | `HITLTable` and its indexes read/write (only when the table exists) |
| WorkflowTracker | `HITLTable` read/write, the NotificationsTable, Cognito group reads for announcements, the ConfigurationTable |
| ReviewQueuesResolverFunction | `HITLTable` read/write, the tracking, configuration, users and notifications tables, the OutputBucket (drafts and status object), DocumentQueue `SendMessage` (finalize) |
| ReviewQueueSweeperFunction | the resolver's table and bucket grants, `states:DescribeExecution`, `lambda:InvokeFunction` on the WorkflowTracker, DocumentQueue `SendMessage` |
| TestSetResolverFunction | `HITLTable` and its indexes read/write (`TSDOC#` summaries and assignments), UsersTable `UpdateItem` (an assignment adds the set to the Annotator's `allowedTestSets`) |
| TestSetFileCopierFunction | `HITLTable` `Query`/`UpdateItem`/`DeleteItem`/`BatchWriteItem`: builds a set's `TSDOC#` summaries after a copy and on a `{"action": "summarize"}` message |
| ProcessChangesResolverFunction | `HITLTable` `GetItem`/`DeleteItem` (the holder check before a regroup during review, and the drafts of the sections that regroup removes) |
| CompleteSectionReviewFunction | `HITLTable` `GetItem`/`UpdateItem` (the assignee check, ending the assignment) |

Routing doesn't read the extraction results; a finalize run's corrections are
applied through ProcessResults' existing OutputBucket grant. Headless deployments
have no `HITLTable`: the routing step continues with `no_review_store`, and the
assembly steps keep the legacy marking.

## Testing

Unit tests live in `lib/idp_common_pkg/tests/unit/review_queues/`; the `review_table`
fixture in its `conftest.py` creates the table with every index under moto.
`make stacktest-review-queues STACK_NAME=...` runs the end-to-end check against a
deployed stack.
