# HITL Review Queues and Inbox — Threat Analysis

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 1.3 |
| **Last Updated** | 2026-11-17 |
| **Applies to release** | v0.6.16 |
| **Feature** | HITL review queues (Production and Test Studio), Send to review, bulk Skip All Reviews and its jobs, the per-user Inbox, the review finalize execution |
| **Classification** | Internal |

> **Why this surface has its own prefix.** Review queues bring their own UI API
> operations, a table (`HITLTable`), a per-user Inbox (`NotificationsTable`), a
> second Step Functions execution per reviewed document, a background job queue
> for bulk Skip All Reviews, and an outbound interface (`review/status.json` plus
> Output-bucket EventBridge notifications). They also let the `Annotator` group
> work **production** documents in the queues it is eligible for, beyond the test
> sets named in `allowedTestSets`. Each of those is an entry point or a principal
> capability the rest of the corpus does not describe, so they are modelled here
> rather than as further `AUTH.*` entries.

## 1. Feature Overview

When HITL is enabled in a document's configuration and the **Default** review
queue is enabled, a production document that raised confidence alerts is held for
review instead of completing. Each test set is also a Test Studio queue.

- **Queues** are system-wide definitions in `HITLTable` with an **Eligible
  Annotators** list (an empty list means no Annotator). Admins and Reviewers work
  every queue (`QueueDirectory.is_member`), and work is assigned only to
  Annotators. Queue administration — create, update, delete, list, overview, list
  Annotators — is `Admin` only.
- **Items** are claimed by an eligible Annotator or a Reviewer (or assigned to an
  Annotator by an Admin),
  edited as per-section drafts, and completed, rejected, released or skipped with a
  reason. An Admin can also take over, assign, unassign, move, or remove an item
  from review, which completes the document with its machine output.
- **Finalize.** Completing, rejecting or removing an item moves it to
  `FINALIZING` with a single-use finalize token, and the review-queues resolver
  sends the document to `DocumentQueue` itself. The queue processor validates the
  token's shape, names the execution from it, and the second (finalize) execution
  re-runs the state machine; `ProcessResults` matches the execution name to the
  item and applies the reviewer's drafts (complete), the machine output (removed),
  or ends the document `REJECTED`. The first execution ended `HITL_IN_PROGRESS`.
- **Status object.** `<document>/review/status.json` in the Output bucket records
  `in_review`, then `complete` or `rejected`, written last. The Output bucket now
  sends every object event to the account's default EventBridge bus so external
  consumers can trigger on it; no rule in the stack consumes them.
- **Inbox.** `idp_common.notifications` posts messages — assignments, released
  claims, take-overs, time-limit reminders, items waiting — addressed to users or
  to Cognito groups, expanded to per-recipient rows at post time with
  `ListUsersInGroup`. No API operation posts a message; the eight Inbox
  operations read, mark, delete and set preferences, each keyed on the caller's
  verified identity.
- **Send to review.** `sendToReview` (`Admin`, `Reviewer`) puts finished
  production documents into the Default queue by hand with a reason from a fixed
  list and an optional note, which reviewers see on the item
  (`idp_common/review_queues/send_to_review.py`). Each document is checked against
  the caller's profile scope by its own profile; a document that is not
  `COMPLETED`, is a Test Studio document, or is already in a review is refused as
  an in-band per-document outcome.
- **Bulk Skip All Reviews.** `bulkSkipAllReviews` (`Admin`, `Reviewer`) finishes
  many pending production reviews with their machine output, for a selection or
  for a profile and submitted-date range, with a required reason and a dry run.
  Up to `hitl.bulk_skip_sync_limit` documents run inline in
  `BulkReviewResolverFunction`; a larger request becomes a job on
  `BulkReviewJobQueue`, advanced one page at a time by `BulkReviewWorkerFunction`,
  one job at a time per stack, paced against `DocumentQueue`. **Resubmit for
  downstream processing** (on by default) re-runs Summarization, Evaluation and
  the post-processing hook. The job's requester is told in their Inbox when it
  ends. `getBulkReviewJob`, `listBulkReviewJobs`, `cancelBulkReviewJob` and
  `deleteBulkReviewJob` serve **Home › Jobs**; a Reviewer sees and changes only
  the jobs they requested, an Admin every job.
- **Sweepers.** `ReviewQueueSweeperFunction` (schedule) resends stalled finalize
  runs, abandons a Send-to-review item left `HOLDING`, and sends reminders;
  `TestRunSweeperFunction` (schedule) reconciles Test Studio run status.

The legacy HITL operations (`claimReview`, `releaseReview`,
`completeSectionReview`, `skipAllSectionsReview`, `saveSectionReviewEdits`) remain
for documents that are not queue-managed, and refuse a document a queue manages
(`QUEUE_MANAGED`). `uploadDocument` and `processChanges` refuse to overwrite a
document's section results while it is in review.

## 2. Architecture

```mermaid
flowchart TD
    Ann[Annotator / Admin browser] -->|"POST /op/{field} (JWT)"| Disp[HTTP API dispatcher<br/>group floor, default deny]
    Disp --> RQ[ReviewQueuesResolverFunction]
    Disp --> TS[TestSetResolverFunction]
    Disp --> NR[NotificationsResolverFunction]
    Disp --> BR[BulkReviewResolverFunction<br/>Send to review, bulk skip]
    BR -->|"items, job rows"| HITL
    BR -->|"jobs above the inline limit"| BQ[BulkReviewJobQueue] --> BW[BulkReviewWorkerFunction]
    BW -->|"skip, resubmit"| DQ
    BR -->|"skip, resubmit"| DQ
    RQ -->|"items, drafts, history"| HITL[(HITLTable)]
    RQ -->|"draft bodies, status.json"| Out[(Output bucket<br/>EventBridge enabled)]
    RQ -->|"finalize message"| DQ[DocumentQueue]
    RQ -->|"owner mirror"| Track[(TrackingTable)]
    TS -->|"allowedTestSets on assignment"| Users[(UsersTable)]
    DQ --> QP[Queue processor] -->|"StartExecution, name from finalize token"| SFN[State machine<br/>finalize execution]
    SFN -->|"apply drafts / machine output / REJECTED"| Out
    WT[WorkflowTracker] -->|"status.json, Inbox posts"| Out
    WT --> Notif[(NotificationsTable)]
    NR -->|"caller's own partition only"| Notif
    Out -.->|"every object event"| Bus[Default EventBridge bus<br/>no in-stack consumer]
```

## 3. Threat Analysis

### HITL.T01: Queue Listings Show Items Outside the Caller's Profile Scope

| Attribute | Value |
|-----------|-------|
| **Threat ID** | HITL.T01 |
| **Category** | STRIDE: Information Disclosure |
| **Description** | Queues are system-wide, so one queue holds items of several configuration profiles, and an Annotator's access to an item is bounded by two things: queue eligibility and profile scope (`allowedConfigVersions`). A queue reference carries no profile, so a check made at the queue level cannot apply profile scope; it has to be made against each item's own profile. A listing that applies only eligibility shows a scoped Annotator the items of every profile in the queues they work. A listed item carries the document's object key, its profile, the fields that raised confidence alerts with their scores, and the holder's email. |
| **Attack Vector** | An Annotator with `allowedConfigVersions` set, eligible for a queue that also holds other profiles' items, calls `listReviewQueueItems`, `getMyReviewQueues`, `listMyReviewWork` or `claimNextReviewItem`. |
| **Impact** | Disclosure of review-item metadata — document names, profile names, alert fields and scores, holder identity — across the profile boundary. Not document content, drafts or the work itself, which the per-item operations refuse. |
| **Likelihood** | Low |
| **Severity** | Medium |
| **Affected Components** | `review_queues_resolver`, `idp_common/review_queues/` (`service.py`, `directory.py`) |
| **Mitigations** | Every per-item operation and every listing decides profile scope by the **item's own profile**: `ReviewQueueService._sees`, which uses `idp_common.config_scope.scope_allows` — the same comparison the rest of the API uses (AUTH.T07) — and so admits an item with no profile to no scoped caller. It applies to `listReviewQueueItems` (one queue or all of the caller's), `getMyReviewQueues`, `listMyReviewWork` and `claimNextReviewItem`; a listing that leaves items out still pages forward, and Claim next reads past them up to `CLAIM_NEXT_MAX_PAGES` pages of a queue. `QueueDirectory.in_scope`, which admits an empty profile, is used only for a queue reference, never for an item. `lib/idp_common_pkg/tests/unit/review_queues/test_listing_profile_scope.py` covers each listing for a scoped caller, an unscoped caller and an Admin. Admins and unscoped Annotators see every profile by design. |
| **Residual risk / recommendation** | A queue's counts (depth, unclaimed, oldest item) are filtered for a **scoped** caller item by item, in one projected pass per queue (`ReviewQueueService` in `idp_common/review_queues/service.py`), so they count only the items the caller's scope admits; Admins and callers whose scope is unrestricted get whole-queue counts, which match what they are shown. The static RBAC rule (`make api-test-static`) checks that a scope lookup is reachable from a `scope_filtered` operation, not that its result is applied to each returned item, so a regression of this class is caught by the unit tests above rather than by that gate. The behaviour has not been exercised against a live deployment. |
| **Evidence** | `lib/idp_common_pkg/tests/unit/review_queues/test_listing_profile_scope.py`; `make api-test-static`; UNVERIFIED: the citations have not been matched to each listed mitigation |

### HITL.T02: Reviewer-Controlled Drafts Become the Document's Final Output

| Attribute | Value |
|-----------|-------|
| **Threat ID** | HITL.T02 |
| **Category** | STRIDE: Tampering, Repudiation |
| **Description** | Review exists to let a person change what the pipeline produced, so the threat is a holder who changes it wrongly — deliberately, or because their account is compromised. A draft is any JSON object up to 2,000,000 bytes and nesting depth 64; it is not validated against the class schema or restricted to the extracted values, and on completion the finalize execution copies each section's latest draft verbatim over that section's extraction result. The holder can also regroup pages, and the finalize run reprocesses with the new grouping. An Admin can complete or reject an item someone else holds, take one over, or **remove** a document from review, which completes it with its unreviewed machine output. **Send to review** widens which documents can reach that path: an Admin or Reviewer can put any finished production document in their profile scope into the Default queue, not only one that raised confidence alerts, then claim and complete it like a routed item. Downstream consumers — the post-processing hooks, reporting, evaluation against a baseline, and any external reader of `status.json` — receive the result as reviewed. |
| **Attack Vector** | An eligible Annotator or a Reviewer claims an item and saves a draft that alters values or the section structure, then completes it; a Reviewer sends an already-finished document to review and then works it the same way; or an Admin removes a document from review to release machine output that should have been checked. |
| **Impact** | Falsified final extraction results presented as human-reviewed, reaching every downstream consumer of the document. |
| **Likelihood** | Low |
| **Severity** | High |
| **Affected Components** | `review_queues_resolver`, `bulk_review_resolver` (`sendToReview`), `idp_common/review_queues/` (`service.py`, `finalize.py`, `routing.py`, `regrouping.py`, `send_to_review.py`), `process_changes_resolver`, the state machine's `ProcessResults` steps |
| **Mitigations** | Only the holder can save, complete or reject: each write is a transactional `Put` conditioned on the item's version **and** on `Status = CLAIMED AND OwnerKey = <caller>` (with the lease unexpired), so a stale or concurrent writer is refused by DynamoDB rather than by a read-then-check. Supervisor operations (take over, assign, unassign, move, remove, force release) are `Admin` only at the dispatcher and again in the service, and a take-over tells the former holder in their Inbox. The finalize message body is built server-side from the tracking row, so no request argument reaches the workflow input; the finalize token is validated by shape and becomes the execution name, so a resent finalize does not start a second run. The item records its review activity, field edits and disposition; `status.json` and the `document_reviews` reporting table record the reviewer and disposition. Legacy review operations and `uploadDocument` refuse a document a queue manages, so there is one write path while it is in review. `sendToReview` is `Admin` and `Reviewer` only at the dispatcher and in `bulk_review_resolver`; it checks each document against the caller's scope by the document's own profile (`review_skip.in_scope`, `scope_allows`), refuses a Test Studio document, one not `COMPLETED` and one already in a review, and makes the tracking row the commit point with a conditional update (`attribute_not_exists(TestSetId)`, finished, not in review), so a concurrent run or a second send cannot overwrite it. The item records `origin=manual` and a `sent_to_review` history entry naming the sender, the reason (from a fixed list) and the note. `processChanges` requires a caller who is not an Admin to hold the document's open review and to have the document's profile in scope, and refuses section and page changes to a document whose pinned revision routes it to BDA. |
| **Residual risk / recommendation** | The review history lives in `HITLTable`, which several Lambda roles in the stack hold write grants on, so it is an operational record rather than a tamper-evident one. A draft is not checked against the section's schema; validating it there would bound a draft to the fields the class declares and keep a malformed body out of the consumers that render it (UI.T01). Remove-from-review is a supervisor decision with no second approver, recorded only as a disposition. A Reviewer can both send a document to review and complete it, so one person can replace any finished in-scope document's output with a reviewed-looking result; the send and the completion are recorded on the item, but nothing requires a second person. |
| **Evidence** | `lib/idp_common_pkg/tests/unit/review_queues/test_send_to_review.py`; `nested/api-resolvers/src/lambda/bulk_review_resolver/test_index.py`; UNVERIFIED: the citations cover Send to review and its resolver; the conditional item writes, the server-built finalize message and the single write path are not matched to a test |

### HITL.T03: Review Metadata Reaches Readers Beyond the Queue's Audience

| Attribute | Value |
|-----------|-------|
| **Threat ID** | HITL.T03 |
| **Category** | STRIDE: Information Disclosure |
| **Description** | Three channels carry review metadata further than a queue's eligible Annotators. **(1) Inbox announcements.** The "items waiting" message for a queue is addressed to the queue's member groups, which every queue definition returns as `Annotator`, `Reviewer` and `Admin` (`queue_defs.member_groups`), so it reaches every Annotator in the pool whether or not they are eligible for that queue; messages carry the queue and test-set ids, the profile name, the assigning user's email and the document's file name. **(2) `review/status.json`.** It records the reviewer's email and, for a rejection, the free-text reject note, and it sits under the document's prefix in the Output bucket, where the generic object reads serve it to any caller whose scope covers the document. **(3) Output-bucket EventBridge.** Every object event in the Output bucket — not only `status.json` — now goes to the account's default event bus, carrying object keys (document names) to any rule another workload in the account defines. The `document_reviews` reporting table also records reviewer identity, so it is queryable wherever Athena reporting is (AGT.T05). |
| **Attack Vector** | An Annotator reads announcements for queues they cannot work; a caller with document read scope fetches a document's `status.json`; a principal in the same account subscribes a rule to the default bus. |
| **Impact** | Disclosure of queue activity, profile names, document names and reviewer identities beyond the queue's audience; no document content. |
| **Likelihood** | Medium |
| **Severity** | Low |
| **Affected Components** | `idp_common/notifications/`, `idp_common/review_queues/` (`queue_defs.py`, `notify.py`, `status_object.py`), `NotificationsTable`, Output bucket notification configuration |
| **Mitigations** | No API operation posts a message, so Inbox content comes only from platform code; a bulk Skip All Reviews job's completion notice goes to its requester only and carries counts, not document names. Reading, marking and deleting are keyed on the caller's verified identity and take no user argument, and the Inbox operations require an assigned group. A message's link must be an in-app route: the server refuses a scheme, a host or a protocol-relative form, and the UI repeats the check before rendering it as a hash route. Message bodies are rendered through `SafeMarkdown` (`rehype-sanitize`), not raw HTML, and skip notes are never put into a message. `NotificationsTable` is encrypted with the stack CMK, expires rows by TTL and has point-in-time recovery. EventBridge delivery from S3 carries object metadata, not object content. |
| **Residual risk / recommendation** | Address the waiting-work announcement to the queue's eligible Annotators rather than to the whole `Annotator` group. Message bodies interpolate the uploader's file name and are rendered as sanitized markdown. The sanitizer removes script, event handlers and `<style>`, loads an image only from the app's own origins or the deployment's buckets (any other image is shown as a link), and keeps a `style` attribute only for layout and typography properties with no URL-capable value (`safe-markdown-policy.ts`); it still admits external `http(s)` links, so a file name is rendered as markup — emphasis, a link — in another user's Inbox rather than as text. Rendering the interpolated name as plain text would remove that class. Whether every Output-bucket event needs to reach the default bus, rather than only the `status.json` key, is a deployment decision; a rule filtering on the key suffix bounds what a consumer receives but not what the bus carries. |
| **Evidence** | `src/ui/src/components/common/__tests__/SafeMarkdown.test.tsx::SafeMarkdown loads nothing from another origin`; UNVERIFIED: the citation covers the renderer's resource loading; the Inbox's identity keying, the link check and the announcement recipients are not matched to a test |

### HITL.T04: Resolvers Serving Annotator Operations Hold Write Grants on Authorization and Workflow State

| Attribute | Value |
|-----------|-------|
| **Threat ID** | HITL.T04 |
| **Category** | STRIDE: Elevation of Privilege |
| **Description** | The review resolvers are reachable by the `Annotator` and `Reviewer` groups and carry grants wider than any one of their operations needs. `TestSetResolverFunction`, which serves `claimTestSetDocument` and the test-set draft and save operations to Annotators, can now `PutItem` and `UpdateItem` on the **UsersTable** — the table `allowedTestSets` and `allowedConfigVersions` live in — because the Admin-only `assignTestSetDocuments` writes the assignee's `allowedTestSets`. `ReviewQueuesResolverFunction` can `UpdateItem` anywhere in the TrackingTable, read every object in the Output bucket, write the Working bucket, and send to `DocumentQueue`, which starts a workflow execution. `BulkReviewResolverFunction`, which serves `sendToReview` and `bulkSkipAllReviews` to Reviewers, and `BulkReviewWorkerFunction`, which runs a Reviewer's job with its own role, can each read and write any `HITLTable` item, `UpdateItem` any tracking row, read every Output-bucket object and write its `*/review/*` keys, write the Working bucket, send to `DocumentQueue`, and write `NotificationsTable`. `ReviewQueueSweeperFunction` and `TestRunSweeperFunction` can update any tracking row. |
| **Attack Vector** | A defect in one of those resolvers' Annotator- or Reviewer-callable paths — an argument used as a key without the per-object check, a missing branch guard — is steered by the caller into a write the operation was not meant to make, such as widening a user's scope, re-queueing a document, or finishing another profile's review. |
| **Impact** | A scope change on any user row, or a document re-run or status change, made with the resolver's role rather than the caller's authority. |
| **Likelihood** | Low |
| **Severity** | High |
| **Affected Components** | `TestSetResolverFunction`, `ReviewQueuesResolverFunction`, `BulkReviewResolverFunction`, `BulkReviewWorkerFunction`, `ReviewQueueSweeperFunction`, `TestRunSweeperFunction` (IAM policies in `template.yaml` and `nested/api-resolvers/template.yaml`) |
| **Mitigations** | The dispatcher's group floor admits `assignTestSetDocuments` to `Admin` only, and the resolver checks again. Every grant is scoped to a named table, bucket, prefix or queue of this stack; the only `*` resources are `cloudwatch:PutMetricData` statements conditioned on the namespace. `states:StopExecution` on the queue processor and `states:DescribeExecution` on the sweeper are scoped to this stack's processing state machine. The finalize message is built server-side from the tracking row (HITL.T02). The bulk resolver takes the caller only from verified claims (`_caller_context`), its scope lookup fails closed, and it never reads a key, profile or job id it was given without the per-document or requester check of HITL.T05. `BulkReviewJobQueue` and its dead-letter queue are encrypted with the stack key, and their queue policies deny non-TLS access. |
| **Residual risk / recommendation** | IAM cannot express "this item" or "this user's own row", so a resolver's per-object correctness is the only bound on these writes (AUTH.T16). Moving the `allowedTestSets` write behind the user-management function, which already owns UsersTable writes, would take UsersTable write off a role Annotators reach. |
| **Evidence** | `nested/api-resolvers/src/lambda/bulk_review_resolver/test_index.py`; UNVERIFIED: the citation covers the bulk resolver's group and caller checks; no test asserts the IAM grants themselves |

### HITL.T05: Bulk Skip All Reviews Releases Unreviewed Output at Scale

| Attribute | Value |
|-----------|-------|
| **Threat ID** | HITL.T05 |
| **Category** | STRIDE: Tampering, Repudiation, Denial of Service |
| **Description** | `bulkSkipAllReviews` finishes up to `MAX_JOB_DOCS` pending production reviews in one request, each completing with its unreviewed machine output, and by default resubmits each for Summarization, Evaluation and the post-processing hook. A by-profile request reaches every pending review of a profile in a submitted-date range without naming any document. So one Admin or Reviewer can release, in one action, the output review was meant to check across a whole profile, and downstream consumers receive it as finished. A large job runs in the background under the requester's recorded identity for as long as it takes, and only one job runs at a time per stack. |
| **Attack Vector** | A Reviewer, or a compromised Reviewer account, submits a by-profile bulk skip over a profile in their scope with resubmission on; or submits a series of large jobs that hold the per-stack job lock and keep `DocumentQueue` busy with resubmissions. |
| **Impact** | Many documents completed without the human check their configuration asked for, reaching every downstream consumer; delayed review work and processing for other users while a large job runs. |
| **Likelihood** | Low |
| **Severity** | High |
| **Affected Components** | `bulk_review_resolver`, `bulk_review_worker`, `idp_common/review_queues/` (`bulk_jobs.py`, `bulk_selection.py`, `review_skip.py`, `legacy_skip.py`, `notify.py`), `BulkReviewJobQueue`, `HITLTable` job rows |
| **Mitigations** | **Who.** `bulkSkipAllReviews`, the four job operations and `sendToReview` are `Admin` and `Reviewer` only, at the dispatcher and again in `bulk_review_resolver` (`_FIELD_GROUPS`) before any work, the same roles that may skip one review (`skipAllSectionsReview`). **What.** A by-profile request is refused unless the profile is in the caller's `allowedConfigVersions`, before any document is read (`_enforce_scope`, `assert_profile_in_scope`); a selection is classified per document (`review_skip.classify`): a document outside the caller's scope or unknown is `NOT_PERMITTED`, a Test Studio document is excluded, a queue item claimed by someone else or a legacy review owned by someone else is `HELD_BY_OTHER` for a non-Admin, and one still processing or already finalizing is left alone. The dry run uses the same classification, so its counts match the run. **While it runs.** A background step rebuilds the requester from the job row and intersects the scope recorded at submission with the requester's current scope, looked up by the recorded Cognito `sub` (`BulkSkipService._caller_for`, the worker's `_scope_resolver`); a by-profile job whose profile has left that scope fails as `scope_revoked`, and a failed lookup fails the step rather than widening it. Retried pages neither skip nor count a document twice (conditional per-document result puts). **Accountability.** A reason from a fixed list is required, each document's skip goes through the queue's finalize path with the `skipped` disposition or the legacy skip, both recorded in review history, `BulkReviewOutcomes` counts every outcome, and the requester is told in their Inbox when the job ends. A Reviewer reads, cancels and deletes only the jobs they requested (`RequesterKey` from verified claims); another user's job reads as not found, and an active job cannot be deleted. **Volume.** One job runs at a time per stack (a lock row), the worker's event source mapping has a maximum concurrency of two, and a job advances one page per step, paced against `DocumentQueue` depth. |
| **Residual risk / recommendation** | A Reviewer can skip every pending review of any profile in their scope in one action with no second approver; the reason and the requester are recorded, but nothing prevents it. A background job keeps the requester's groups as recorded at submission, so a Reviewer removed from the group mid-job does not stop it, while a scope narrowed mid-job does; an Admin can cancel any job. Because one job runs at a time per stack, a large job delays every other requester's job. |
| **Evidence** | `nested/api-resolvers/src/lambda/bulk_review_resolver/test_index.py`; `nested/api-resolvers/src/lambda/bulk_review_worker/test_index.py`; `lib/idp_common_pkg/tests/unit/review_queues/test_review_skip_eligibility.py`; `lib/idp_common_pkg/tests/unit/review_queues/test_bulk_jobs.py`; UNVERIFIED: the citations have not been matched to each listed mitigation, and the behaviour has not been exercised against a live deployment |

## 4. Security Controls Summary

| Control | Implementation | Threats Mitigated |
|---------|---------------|-------------------|
| **Group floor and default deny** | Dispatcher `authz.py`; queue administration and supervisor operations `Admin` only; item operations `Admin`, `Reviewer` and `Annotator`; Send to review and bulk Skip All Reviews `Admin` and `Reviewer` | HITL.T02, HITL.T04, HITL.T05 |
| **Per-item profile scope** | `ReviewQueueService._sees` (`scope_allows` on the item's own profile) in every per-item operation, every listing and, for a scoped caller, every queue count; `review_skip.in_scope` on each document of a Send to review or bulk skip, and `assert_profile_in_scope` on a by-profile bulk request | HITL.T01, HITL.T05 |
| **Bulk job bounds** | Requester-only job reads and changes; scope re-checked at every background step; one job per stack; paced against `DocumentQueue` | HITL.T05 |
| **Conditional writes** | Version plus `Status`/`OwnerKey`/lease conditions on every item write | HITL.T02 |
| **Server-built finalize** | Message body from the tracking row; token validated by shape and used as the execution name | HITL.T02, HITL.T04 |
| **Single write path in review** | Legacy review operations, `uploadDocument` and `processChanges` refuse a document a queue manages | HITL.T02 |
| **Self-scoped Inbox** | Partition key from verified claims; no posting operation; in-app links only, checked server- and client-side; sanitized rendering | HITL.T03 |
| **Encryption and retention** | `HITLTable` and `NotificationsTable` on the stack CMK; Inbox TTL and per-user cap | HITL.T03 |
| **Stack-scoped grants** | Named resources only; state-machine actions scoped to this stack's processing state machine | HITL.T04 |

## 5. Open Items

| Item | Threat | Status |
|------|--------|--------|
| Waiting-work announcements reach every Annotator, not the queue's eligible ones | HITL.T03 | **Open** |
| Drafts are not validated against the section schema | HITL.T02 | **Open** — recommendation |
| UsersTable write on a role Annotators reach | HITL.T04 | **Open** — recommendation |
| A Reviewer can bulk-skip a whole in-scope profile with no second approver | HITL.T05 | **Open** — recorded and attributable, not prevented |
