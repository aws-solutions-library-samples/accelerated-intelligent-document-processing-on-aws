Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Review queues (`idp_common.review_queues`)

HITL review queues hold documents that raised confidence alerts until a person has
reviewed them. Queues are system-wide definitions (`queue_defs.py`) that Admins
manage in HITL › Queue Management, and they have no confidence threshold of their
own. When HITL is enabled in a document's configuration (`hitl.enabled`) and the
Default queue is enabled, a production document whose sections raised confidence
alerts (each field's own threshold, otherwise `hitl.confidence_threshold`) waits in
the Default queue instead of finishing. An Admin assigns them to Annotators, who
correct and dispose of them. This
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
| `statuses.py` | The one declaration of `ReviewStatus` (`in_review`, `complete`, `rejected`), `QueueItemState` and the legacy `HITLStatus` spellings, plus `normalize_hitl_status`. Standard library only, so the layer-free list resolvers can vendor it |
| `models.py` | Pydantic records for the table (`ReviewItemRecord`, `DraftRecord`, `HistoryEntry`), `StatusObject`, `ReviewDecision`, `CallerContext` |
| `keys.py` | Table key layout and the opaque list cursors |
| `store.py` | `ReviewStore`: every read and write of `HITLTable`, all conditional on the item `Version` |
| `annotators.py` | Who the Annotators are: the Cognito `Annotator` group's members (`annotator_emails`, behind `listAnnotators`) and one user's groups (`cognito_user_lookup`, `is_annotator`), used by both assignment paths |
| `queue_defs.py` | `QueueDefinition` and `QueueDefStore`: the system-wide queues (`PK=QDEF`, `SK=Q#<id>`), each with an `enabled` switch, cached for 30 s; the Default queue is seeded, enabled, whenever it is missing, and a stored Default is never overwritten |
| `directory.py` | `QueueDirectory`: which queues exist and who may work them (Admins every queue; Annotators those whose `eligible_annotators` is empty or names them), failing closed |
| `reasons.py` | `build_reasons`: why a document is held (its sections' `confidence_threshold_alerts`, and in BDA mode a blueprint confidence under `hitl.confidence_threshold`); reads no S3 |
| `regrouping.py` | `regroup_refusal`: who may change the page grouping of a document in review (only the holder of its `CLAIMED` item). Used by `updateDocumentSections` |
| `routing.py` | `review_mode` (the `hitl.enabled` and Default-queue gate) and `route_for_review`: the one routing function both ProcessResults steps call |
| `tracker.py` | WorkflowTracker integration: activation after phase 1 and completion after the finalize run |
| `service.py` | `ReviewQueueService`: listing, claims, leases, drafts, dispositions and the supervisor actions behind the review API |
| `finalize.py` | The finalize token, `FinalizeSender` and `apply_corrections` |
| `status_object.py` | Writing and deleting `<document>/review/status.json` |
| `notify.py` | Inbox messages (`review.*` notification types); never raises |
| `testset_labels.py` | `TestSetLabels`: the one writer of a reviewed Test Studio label (`write_reviewed`: `labelSource = reviewed-human`, the `_editHistory` entry with field diffs, capped at `MAX_EDIT_HISTORY_ENTRIES`, the confidence-curve observations, the section's draft deleted), and section drafts (save, list, delete, move). Used by `test_set_resolver` and `complete_section_review` |
| `section_writes.py` | `assert_section_result_writable`: refuses (`QUEUE_MANAGED`) a direct write of `<document>/sections/<id>/result.json` to the Output bucket while the document is in review, reading its tracking row. Used by `uploadDocument` (`upload_resolver`) |
| `sweeper.py` | `ReviewSweeper`: assignment reminders and self-healing, run every 5 minutes |
| `api_models.py` | The camelCase API views and argument models |
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
any live state ──abort, delete, reprocess, a run that needs no review──▶ WITHDRAWN
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

Pipeline and BDA ProcessResults call `route_for_review(document, config, mode=...)`
after their own work. A Test Studio document (`submission_source == "test-studio"` or
a `test_set_id`) always reads as legacy (`review_mode(config, store, document)`): test
runs are scored on the machine's output and their documents are reviewed as test-set
annotation work, which the UI lists beside production queues. Otherwise
`review_mode` is `"queues"` only when all three hold: `config.hitl.enabled` is true,
the stack has a `HITLTable`, and its Default queue is `enabled` (read through
`QueueDefStore`, cached for 30 s). A document whose item is `FINALIZING` stays
`"queues"` so its finalize run completes after HITL or the switch is turned off; a
fresh run in that state never holds. Anything else is `"legacy"`, logged at DEBUG as
`event=review_mode_legacy reason=<hitl_disabled|default_queue_disabled|no_store>`.
In legacy mode routing writes nothing, and the HITL marking is reproduced by
`legacy_section_triggers` and `bda_segment_triggers`. Both BDA modes read the
blueprint confidence through `idp_common.utils.parse_blueprint_confidence`, which
is `parse_confidence` except that a string is scored only when `float()` accepts
it, so a percentage string (`"95%"`) is unscored rather than rescaled. An unscored
blueprint confidence (null, non-finite, a bool, a string `float()` rejects, or a
value outside 0 to 1 after a bare number in (1, 100] is rescaled, so `150` and
`-0.2`) is reviewed at every threshold: legacy HITL marks the segment and queue
mode holds it (below).
In queue mode:

- **Reasons.** Each of a section's `confidence_threshold_alerts` (computed by
  assessment or extraction in Pipeline mode, and by BDA processing in BDA mode, with
  the field's `x-aws-idp-confidence-threshold` and otherwise
  `hitl.confidence_threshold`) is a `low_confidence` reason named by its path
  (`Address.City`, `Rows[0].Amount`), with the alert's confidence and threshold. In
  BDA mode a matched blueprint under `hitl.confidence_threshold` is a
  `blueprint_confidence` reason, and so is a matched blueprint whose confidence is
  unscored: the BDA result processor sets `Section.blueprint_unscored`, and the
  reason carries a score of 0.0. The flag is persisted in the document's own
  serialization and in the tracking table (`BlueprintUnscored`, written only when
  true), so a reprocess that reloads the document is held again. A section whose segment matched no blueprint is
  unscored too but is not flagged, so it is not held, as legacy HITL does not mark
  it. So routing agrees exactly with the alerts users
  see, and it reads nothing from S3. A malformed alert is skipped. The module must
  not import `idp_common.assessment`: the Pipeline ProcessResults Lambda installs
  only the `review_queues` extra. Up to 20 reasons are stored per item, worst gap
  first; `reason_count` keeps the total.
- **Hold.** A run with reasons writes a `HOLDING` item in the Default queue and
  returns `review_hold`. The state machine's guarded Choice
  rule on `$.Result.review_hold` then ends phase 1 with `HITL_IN_PROGRESS`,
  skipping the post-review tail (summarization, evaluation, rule validation, the
  `postprocessing` hook).
- **Finalize run.** A run whose execution name matches the item's finalize
  execution applies the drafts (`apply_corrections`) on `complete`, or nothing on
  `rejected` or `removed`, and returns `review_outcome` (`removed` reads as
  `complete`); the state machine then runs the tail (complete) or ends `REJECTED`.
- **No reasons.** A fresh run that needs no review withdraws a live item and logs
  `event=review_no_reasons`.

The WorkflowTracker activates a `HOLDING` item when phase 1 succeeds (→ `OPEN`,
writes the `in_review` status object, announces the queue; an item whose queue no
longer exists opens in the Default queue) and records the document's completion
when the finalize run ends.

## Regrouping during review

Correcting a document's split is part of review. `updateDocumentSections` accepts a
document that is `HITL_IN_PROGRESS` and queue-managed `in_review` when the caller holds
its `CLAIMED` item (`regroup_refusal`); an `OPEN` item must be claimed first, a
`FINALIZING` one is refused, and anyone but the holder gets `NOT_LEASE_HOLDER`. The
legacy HITL flow is unchanged. Section ids are kept across a regroup, so drafts stay
with their sections; `get_item_detail` no longer lists a section the regroup removed,
and `apply_corrections` skips its draft. The finalize run reprocesses the tracked
document, so it finishes with the new grouping.

## Queues and assignment

- A queue is identified by its `id`, which items, table keys (`Q#<id>`) and the
  API's queue arguments use. Its `name` is display text. Admins manage queues with
  `listReviewQueues`, `createReviewQueue`, `updateReviewQueue` and
  `deleteReviewQueue`; the web UI doesn't offer `createReviewQueue`. Only Default can't be deleted (`PROTECTED_QUEUE`); an
  Escalation queue an earlier release seeded is an ordinary queue.
  `ReviewQueueService.delete_queue` moves a queue's `OPEN` and `CLAIMED` items to
  Default with `move` (unassigned; an assignee is told), re-points its `HOLDING`
  items at Default, then deletes the definition. It refuses with
  `QUEUE_FINALIZING` while one of the queue's items is `FINALIZING`.
- `create`/`updateReviewQueue` take `enabled`. A new queue is enabled; an update
  that leaves it out keeps the stored value, and a definition stored without it
  reads as enabled. Queues have no confidence threshold: a `confidenceThreshold`
  sent by an older client is ignored, and a stored `ConfidenceThreshold` attribute is
  ignored on read and dropped on the next save. Only the Default queue's switch
  routes documents.
- A queue's `eligible_annotators` is its membership (`QueueDefinition.allows`): an
  Annotator works only the queues that list them, and an empty list means none.
  Admins work every queue (`QueueDirectory.is_member`).
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
  item even after being taken off the list. `allowedConfigVersions` still filters
  items by profile.
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
  Queue Management, only the sets listing the caller for an Annotator in My Work ›
  Queues): one DynamoDB read
  of the sets' metadata items, with the counts the build, assignments and reviews
  keep there (`fileCount`, `QueueReviewedDocs`, `QueueAssignedDocs` via
  `store_assigned_count`). It reads no labels and no queues.

## Drafts

`saveReviewDraft` stores a section's whole edited result at
`<docKey>/review/drafts/<cycle>/<section>/<revision>.json` (at most 2,000,000
bytes) and records it as `DRAFT#<cycle>#<section>` with the changed field paths.
`baseRevision` makes concurrent saves fail with `STALE_DRAFT`. Drafts survive
unassignment, reassignment, take-over and move. The `review/` segment is
not part of any run manifest and is deleted by Reprocess.

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
  `split_document.page_indices` and `document_class.type`, and drops a removed
  section's; `delete_document_drafts` runs when a document is removed from the set.
  Skip, Re-extract, Clear draft labels and Reset labels keep drafts. Publishing, the
  version snapshot, test runs, the content signature and the summaries read
  `baseline/` only.

## Dispositions and durable finalize

| Operation | Who | Effect |
|---|---|---|
| `completeReviewItem` | the assignee | → `FINALIZING`, disposition `complete`; the queue checklist answers are recorded |
| `rejectReviewItem` | the assignee | → `FINALIZING`, disposition `rejected`, a reason from the queue's `reject_reasons` and an optional note, both kept on the item and in the `rejected` history event. The web UI doesn't offer it |
| `reassignReviewItem`, `forceReleaseReviewItem` | Admin | assign to an eligible Annotator, or unassign (→ `OPEN`, draft kept) |
| `takeOverReviewItem` | Admin | an item `CLAIMED` by someone else → `CLAIMED` by the calling Admin, draft kept; a `taken_over` history event, both claim counters moved, and the former holder told (`review.claim_released`). Refused with `NOT_ASSIGNED` for an `OPEN` item (claim it instead), `ALREADY_YOURS` for the Admin's own, and `INVALID_STATE` once the item has left review |
| `moveReviewItem` | Admin | move to another queue (ends any assignment) |
| `removeReviewItem` | Admin | an `OPEN` or `CLAIMED` item → `FINALIZING`, disposition `removed`, with an optional note in a `removed` history event; an assignee is released and told. No draft is applied: the document ends `COMPLETED` with its machine results, the item ends `COMPLETE`, the status object is **deleted** rather than written, and `document_reviews` records disposition `removed` |

A disposition writes `FINALIZING` with a finalize token `rvw-<32 hex>` derived from
the document, cycle and attempt, then sends one message to the DocumentQueue. The
queue processor names the finalize execution after the token, so a re-sent message
collapses into `ExecutionAlreadyExists`. The finalize run starts from a fresh
metering record. If the message is lost or the run fails, the sweeper resends the
same token, then retries with a new token up to 3 attempts before
`FINALIZE_FAILED`.

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
Queue Management, whose counts (pending, assigned, in progress, annotators) come from
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
  (`schema_version` `"1.0"`).
- **Values:** `status` is `in_review`, then `complete` or `rejected`. `sequence`
  increases by one with every write; `cycle_id` names the review cycle;
  `machine_output` points at the phase-1 run manifest, and `final_output` at the
  finalize run's manifest once finished.
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
`ReviewQueue`, `ReviewCycleId`, `ReviewStatusUri`, `HITLReviewedBy`,
`ReviewDisposedAt` and `ReviewRejectReason`. The SDK and CLI report it as
`review_status`.

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
| ProcessChangesResolverFunction | `HITLTable` `GetItem` (the holder check before a regroup during review) |
| CompleteSectionReviewFunction | `HITLTable` `GetItem`/`UpdateItem` (the assignee check, ending the assignment) |

Routing doesn't read the extraction results; a finalize run's corrections are
applied through ProcessResults' existing OutputBucket grant. Headless deployments have no `HITLTable`; routing
stays legacy.

## Testing

Unit tests live in `lib/idp_common_pkg/tests/unit/review_queues/`; the `review_table`
fixture in its `conftest.py` creates the table with every index under moto.
`make stacktest-review-queues STACK_NAME=...` runs the end-to-end check against a
deployed stack.
