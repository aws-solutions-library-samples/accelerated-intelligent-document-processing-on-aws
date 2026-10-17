Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Review queues (`idp_common.review_queues`)

HITL review queues hold documents with low-confidence fields until a person has
reviewed them. Queues are system-wide definitions (`queue_defs.py`). A profile turns
routing on with `hitl.enabled` and `hitl.review_queues.enabled`, and picks the
queue with `hitl.review_queues.queue`. Documents then wait in that queue instead of
finishing. An Admin assigns them to Annotators, who correct and dispose of them. This
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
| `queue_defs.py` | `QueueDefinition` and `QueueDefStore`: the system-wide queues (`PK=QDEF`, `SK=Q#<id>`), cached for 30 s, with Default and Escalation seeded on a stack that has none |
| `directory.py` | `QueueDirectory`: which queues exist and who may work them (Admins every queue; Annotators those whose `eligible_annotators` is empty or names them), failing closed |
| `reasons.py` | `build_reasons`: why a document is held (low-confidence fields, BDA blueprint confidence) |
| `routing.py` | `route_for_review`: the one routing function both ProcessResults steps call |
| `tracker.py` | WorkflowTracker integration: activation after phase 1 and completion after the finalize run |
| `service.py` | `ReviewQueueService`: listing, claims, leases, drafts, dispositions and the supervisor actions behind the review API |
| `finalize.py` | The finalize token, `FinalizeSender` and `apply_corrections` |
| `status_object.py` | Writing and deleting `<document>/review/status.json` |
| `notify.py` | Inbox messages (`review.*` notification types); never raises |
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
annotation work, which the UI lists beside production queues. With the feature off (`review_mode` is `"legacy"`) it does no
I/O, and the legacy HITL marking is reproduced by `legacy_section_triggers` and
`bda_segment_triggers`. In queue mode:

- **Reasons.** A section field whose confidence is under its threshold
  (`Section.confidence_threshold_alerts`, per-field thresholds included) is a
  `low_confidence` reason; in BDA mode a matched blueprint under
  `hitl.confidence_threshold` is a `blueprint_confidence` reason. Up to 20 reasons
  are stored per item; `reason_count` keeps the total.
- **Hold.** A run with reasons writes a `HOLDING` item in the profile's
  `hitl.review_queues.queue` (the Default queue when that id isn't defined) and
  returns `review_hold`. The state machine's guarded Choice
  rule on `$.Result.review_hold` then ends phase 1 with `HITL_IN_PROGRESS`,
  skipping the post-review tail (summarization, evaluation, rule validation, the
  `postprocessing` hook).
- **Finalize run.** A run whose execution name matches the item's finalize
  execution applies the drafts (`apply_corrections`) on `complete`, or nothing on
  `rejected`, and returns `review_outcome`; the state machine then runs the tail
  (complete) or ends `REJECTED`.
- **No reasons.** A fresh run that needs no review withdraws a live item.

The WorkflowTracker activates a `HOLDING` item when phase 1 succeeds (→ `OPEN`,
writes the `in_review` status object, announces the queue) and records the
document's completion when the finalize run ends.

## Queues and assignment

- A queue is identified by its `id`, which items, table keys (`Q#<id>`) and the
  API's queue arguments use. Its `name` is display text. Admins manage queues with
  `listReviewQueues`, `createReviewQueue`, `updateReviewQueue` and
  `deleteReviewQueue`. Default and Escalation can't be deleted, and nor can a queue
  that still holds items.
- A queue's `eligible_annotators` is its membership (`QueueDefinition.allows`): an
  Annotator works only the queues that list them, and an empty list means none.
  Admins work every queue (`QueueDirectory.is_member`).
- Work reaches an Annotator two ways. They **claim** an item from one of their
  queues (`claimReviewItem`, `claimNextReviewItem`), or an Admin assigns it with
  `reassignReviewItem` (and unassigns it with `forceReleaseReviewItem`). Either way
  the holder must be in the `Annotator` group and on the queue's list. Internally,
  both are a claim whose lease is `ASSIGNMENT_LEASE_MINUTES` (ten years), so it
  never expires, and the per-reviewer cap is lifted to `ASSIGNMENT_CAP`.
- The queue's `time_limit_minutes` schedules one `review.task_reminder` to the
  holder if the item is still open after that long. The item stays assigned.
- **Skip** is `releaseReviewItem` by the holder: the item goes back to the queue
  `OPEN`, unassigned, with its draft kept.
- An Annotator listed on a queue may open its items read-only (`getReviewItem`);
  saving, completing and rejecting need the claim. The holder keeps access to their
  item even after being taken off the list. `allowedConfigVersions` still filters
  items by profile.
- Admins also have `renewReviewLease` and `escalateReviewItem`. The web UI doesn't
  use them.
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
  are left alone. Documents skipped before this behaviour carry `Skipped`,
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
unassignment, reassignment and move. The `review/` segment is
not part of any run manifest and is deleted by Reprocess.

## Dispositions and durable finalize

| Operation | Who | Effect |
|---|---|---|
| `completeReviewItem` | the assignee | → `FINALIZING`, disposition `complete`; the queue checklist answers are recorded |
| `rejectReviewItem` | the assignee | → `FINALIZING`, disposition `rejected`, a reason from the queue's `reject_reasons` and an optional note |
| `reassignReviewItem`, `forceReleaseReviewItem` | Admin | assign to an eligible Annotator, or unassign (→ `OPEN`, draft kept) |
| `moveReviewItem` | Admin | move to another queue, e.g. Escalation (ends any assignment) |

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

Inbox messages (see [notifications](../notifications/README.md)) use the types
`review.queue_items_waiting` (debounced per queue, 10 minutes),
`review.item_reassigned` (assigned to you), `review.claim_released` (unassigned),
`review.task_reminder` (still to complete) and `review.item_escalated`. Only
Annotators have an Inbox, so arrivals in a queue are announced to nobody
(`QueueDirectory.members` returns no recipients); Admins see waiting work in
Queue Management, whose counts (pending, assigned, in progress, annotators) come from
`getReviewQueuesOverview` for production queues and from the test sets' metadata
(`store_assigned_count`) for Test Studio queues. Links point at
`#/review-queues/items/<encoded key>` (`review_item_link`).

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
- **Deletion** means the review was withdrawn (reprocess, abort, delete, or a new
  run that needs no review).
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
| CompleteSectionReviewFunction | `HITLTable` `GetItem`/`UpdateItem` (the assignee check, ending the assignment) |
| ConfigurationResolverFunction | `HITLTable` `Query`/`PutItem` on the queue definitions (the unknown-queue warning) |

Headless deployments have no `HITLTable`; routing stays legacy and the SDK
warns when a profile turns queues on there.

## Testing

Unit tests live in `lib/idp_common_pkg/tests/unit/review_queues/`; the `review_table`
fixture in its `conftest.py` creates the table with every index under moto.
`make stacktest-review-queues STACK_NAME=...` runs the end-to-end check against a
deployed stack.
