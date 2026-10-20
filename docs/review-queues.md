---
title: "Review Queues"
---

# Review Queues

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

## Table of Contents

- [Overview](#overview)
- [How a document reaches a queue](#how-a-document-reaches-a-queue)
- [Annotator guide](#annotator-guide)
- [Admin guide: Queue Management](#admin-guide-queue-management)
- [Turning review queues on](#turning-review-queues-on)
- [Review status for downstream consumers](#review-status-for-downstream-consumers)
- [Limits and behaviour to know](#limits-and-behaviour-to-know)

## Overview

Review queues send documents to people before the documents finish. This is
HITL (human-in-the-loop) review, and it works in both BDA mode and Pipeline mode.
There are two kinds of queue:

- **Production** queues hold processed documents with low-confidence fields. A
  document waits in its queue along with the reasons it is there.
- **Test Studio** queues come one per test set, automatically. They hold the
  set's documents whose labels need a human check before they become ground truth.

An **Admin** assigns queue items to **Annotators**. An Annotator corrects the
item and completes it, or skips it to give it back to its queue. An Admin can work
a Production item too. For a Production
document, the corrections are applied by a second (finalize) run, which produces
the document's final outputs. For a Test Studio document, they become the set's
reviewed labels.

Queues are **system-wide**, and they are created and managed only by Admins in
**HITL › Queue Management**; they aren't part of any Configuration Profile.
Low-confidence production documents go to the **Default** queue when it is enabled,
judged against the Default queue's confidence threshold. The Default queue is off on
a new stack, and while it is off documents keep the existing
[Human-in-the-Loop review](./human-review.md) flow.

## How a document reaches a queue

1. The document is processed as usual (OCR, classification, extraction,
   assessment).
2. If the Default queue is enabled, the document is held in it when either of these
   is true:
   - any extracted field's confidence is under the Default queue's **Confidence
     threshold**
   - in BDA mode, the matched blueprint's confidence is under that threshold

   A section whose extraction result can't be read is treated as low confidence
   (the reason reads **Confidence unavailable**), so a read failure never skips
   review.

   Its status becomes `HITL_IN_PROGRESS` ("In review"), and the machine output is
   kept as the run's manifest. Admins see it waiting in HITL › Queue Management.
3. An Annotator listed on the queue claims it, or an Admin assigns it to one (or
   works it themselves, see [Working an item as an Admin](#working-an-item-as-an-admin)),
   and the assignee completes it.
   - **Complete** applies the corrections, and the document ends `COMPLETED`.
   - **Skip** gives it back to its queue, unassigned and not finished.
   - An Admin can instead **Remove** it from the queue: it ends `COMPLETED` with the
     machine output unchanged.
   - The API's `rejectReviewItem` ends it `REJECTED` with the machine output
     unchanged; the web UI doesn't offer it.
4. Summarization, evaluation, rule validation and the post-processing hooks run in
   the finalize run, after review. They don't run at all for rejected documents.

## Annotator guide

An Annotator sees only **My Work** (**Inbox**, **Tasks** and **Queues**) and the
notifications bell. Only Annotators have My Work and an
Inbox; other users have neither.

- **Queues** lists the Production and Test Studio queues you're listed on (a queue's
  **Eligible Annotators**). Open one to see its items. You can open any item
  read-only, and **Claim** one that nobody holds: it's assigned to you and appears
  in your Tasks. On a Test Studio queue you can also select several documents
  nobody holds and choose **Claim (n)** to take them all at once; any that another
  Annotator took first are listed and left with them. Items someone else holds stay
  view-only.
- **Tasks** lists everything assigned to you, whether you claimed it or an Admin
  assigned it: Production review items and Test Studio documents, each with its
  document, source, queue and age. Its badge counts both. You get an Inbox message
  when an Admin assigns you work.
- Select an item to open it in the **annotation screen**. Production and Test Studio
  items use the same screen, whether you open them from Tasks, an Inbox message or
  Queue Management. The document's page images are shown beside its fields.
  - **Production item**: the reasons it is in review are at the top; hover a reason
    chip to see the field, the score and the threshold. Your edits are saved as a
    draft as you type. The queue's checklist is advisory. **Complete** applies your
    corrections.
  - **Test Studio item**: correct the labels and choose **Complete**. You can also
    choose **Complete** without changing anything, which confirms the labels as
    correct; it needs no **Start annotating** first. Saving and versioning work
    exactly as in Test Studio.
  - **Skip**, on either kind of item assigned to you, gives it back: it leaves your
    Tasks and goes back to its queue unassigned, where another Annotator can claim
    it. It isn't finished, and a Production item's draft is kept.
  - The breadcrumbs read **My Work › Queues**, then the item's queue (linking to its
    page), then the document.
- When you complete an item, it leaves your Tasks and the queue's progress counts it
  as completed. The server takes a draft save or a skip only from the item's
  assignee, and a complete from the assignee or an Admin.
- An assignment doesn't expire. If the queue's time limit passes while the item is
  still open, you get one **still to complete** reminder in the Inbox.

Regrouping pages and changing the document class aren't available for documents
in review.

## Admin guide: Queue Management

**HITL › Queue Management** is visible to Admins only. It has two tables,
**Production Queues** and **Test Studio Queues**, with the same columns: **Queue**,
**Items**, **Pending**, **Assigned**, **Annotators** and an actions menu. A queue's
other settings, such as its priority, Enabled switch and threshold, are in its
**Settings**.

**Who the Annotators are.** Work can be assigned only to members of the Cognito
**Annotator** group, the same group that authorizes the Annotator's API calls. A user
joins it when User Management gives them the Annotator role, or when the external
identity provider maps them to it (`ExternalIdPAnnotatorGroupName`, see
[external-idp.md](./external-idp.md)). A user may hold Annotator alongside another
role, Admin for example. A queue's **Eligible Annotators** setting is who works it:
only the Annotators listed there see the queue in My Work › Queues, can claim its
items, and can be assigned them. A queue with nobody listed has no Annotators until
you add some. Listing an Annotator on a Test Studio queue also gives them access to
that test set. A user added to the group in Cognito directly can be listed at once:
their User Management record is created when they're first given a test set.

Each row's actions menu offers **View** and **Settings**; a production queue also
offers **Delete**. Each queue's page has a **Settings** button too.

**Production queues** are created with **Create queue** (the **Create Queue**
dialog), edited in **Settings**, and deleted here. Each queue has:

| Setting | Meaning | Default |
|---|---|---|
| Id | Lowercase letters, digits, `_`, `.` and `-` (up to 64 characters). Links and the API refer to the queue by its id | — |
| Name, description | What Admins and Annotators see; the name defaults from the id | — |
| Priority | Higher-priority queues are listed first (0–100) | `50` |
| Time limit | Minutes after which an Annotator with an open assigned item gets a reminder (1–10080) | `60` |
| Eligible Annotators | The Annotators who work this queue: they see it, claim its items and can be assigned them. Empty means none (only Admins) | empty |
| Enabled | When off, no documents are added to the queue | on for a new queue; off for the seeded Default queue |
| Confidence threshold | Documents with a field below this (0–1) are queued | `0.8` |

The **Default** queue is created automatically, disabled and with nobody listed, and
it is created again if it is ever missing. Turn it on and add Annotators in
**Settings**; see [Turning review queues on](#turning-review-queues-on). It is the
only queue that can't be deleted. Today all routing goes to Default, so the Enabled
switch and threshold of any other queue are stored but not used for routing; items
reach other queues when an Admin moves them there.

Deleting a queue that still holds items moves them to the Default queue first, and
the confirmation says how many will move:
open and assigned items become unassigned there (an assignee is told), and items
still waiting for their first run to finish are activated in Default. Deleting is
refused while one of the queue's items is being finalized; try again a minute later.
An **Escalation** queue that an earlier release created is an ordinary queue and can
be deleted.

Both tables, **Production Queues** and **Test Studio Queues**, show for each queue:

| Column | Meaning |
|---|---|
| Items | Everything in the queue (for a test set, its documents) |
| Pending | Not yet assigned |
| Assigned | Assigned to an Annotator, whether or not they've started on it |
| Annotators | How many Annotators are listed in the queue's Eligible Annotators (the names on hover). A change in Settings shows as soon as the dialog closes |

The top of each queue's page splits the assigned items into **Assigned** (not
started yet) and **In progress** (the Annotator has saved work), beside **Pending**,
**Annotators** and the oldest waiting item.

To work a queue, open its page, select items, and choose one of:

- **Assign to…** picks one of the queue's Annotators. The item appears in their Tasks and
  they get an Inbox message. Assigning an item that already has an assignee
  reassigns it.
- **Unassign** returns the item to the queue. Its draft is kept, and the former
  assignee is told.
- **Move…** sends the item to another queue.
- **Remove from queue…** (for items waiting or assigned) asks for confirmation and
  an optional note, then takes the item out of review: the document ends `COMPLETED`
  with its machine output, no draft is applied, and the review status object is
  deleted. An assignee is told. The note is optional and is kept in the item's
  history.

**Test Studio queues** come from test sets: each test set is a queue, created and
deleted with the set. Its page shows **Queue Progress**, a progress bar on its own,
and **Key metrics**: **Status** (percent of documents completed), **Pending**,
**Assigned** and **Annotators**, then the label-accuracy estimate (**Estimated label
accuracy** and **Evidence**) once there is one. Below that are the
draft-labeling status, the ground-truth version being annotated toward, and every
document, worst first, with columns **Document**, **Labels**, **Confidence** and
**Assigned** (Yes or No; the assignee shows on hover). The document list filters to
**All documents**, **Assigned** or **Not assigned**, and opens on **Not assigned**,
which is exactly the documents that can still be claimed.
Each row's menu offers **View** (the source document), **Annotate**, and for Admins
**Assign** and **Delete** (removes the document from the test set, after a
confirmation). An Admin can also select several documents and choose **Assign to…** to
give them all to one Annotator, or **Unassign** to take them back. Every document can be
assigned, including authored ground truth: the Annotator checks or corrects its
labels and completes it. A document with no labels yet needs draft labels first.
Assigning a document also gives the Annotator access to that test set.

A Test Studio queue's **Settings** are the production settings less the id, name,
Enabled switch and confidence threshold (its documents come from the
set, not from the pipeline): description, priority, time limit (the reminder) and Eligible
Annotators. They are kept with the test set.

A Test Studio queue loads quickly because each document's summary (label source,
alert count, lowest confidence, classes, sections, assignee, reviewed) is kept in
DynamoDB, in the stack's `HITLTable`, while the labels and documents stay in S3. The
queue page reads only the summaries; it never reads a set's labels. Queue Management
lists every test set in one call from the counts stored with each set (documents,
reviewed, assigned), refreshed every 20 seconds; a set still being summarized shows
**Preparing…** in its counts.

The summaries are built in the background, by the same Lambda that copies documents
into a test set:

- when documents are copied into a set, at the end of the copy;
- when a set has none yet, or its labels were cleared, reset or removed as a whole.
  Opening Queue Management or the queue page requests the build.

While a build runs, the queue page says **Preparing this queue** and refreshes
itself until it finishes. A large set takes a few minutes once. A single
document's summary is updated when its labels are saved, when it is re-extracted,
and when a draft-labeling run writes its draft.

### Working an item as an Admin

Open an item from its queue's page. Its breadcrumbs read **Queue Management**, then
the queue's name, then the document, and the queue links back to its page.

- **An unassigned item** says **Not assigned yet. Editing or completing assigns it to
  you.** You can edit it straight away: your first saved edit, or **Complete**,
  assigns the item to you first. If an Annotator claimed it a moment earlier, the
  screen says who and turns view-only.
- **An item assigned to someone else** is view-only, with **Take over**. Taking over
  assigns the item to you, keeps its draft so you carry on from the Annotator's
  edits, and tells the former assignee in their Inbox. Then you can edit and
  complete it.
- **An item assigned to you** has **Complete** and **Skip**, as for an Annotator.

## Turning review queues on

1. **Give people the Annotator role** in **User Management**, which adds them to the
   Cognito Annotator group. With an external identity provider, you can map a group
   instead with `ExternalIdPAnnotatorGroupName` (see [external-idp.md](./external-idp.md)).
   An Annotator's `allowedConfigVersions` limits which profiles' Production items they
   can work.
2. **Turn on the Default queue.** In **HITL › Queue Management**, open the Default
   queue's **Settings**, switch **Enabled** on, set the **Confidence threshold**, and
   list the **Eligible Annotators**. From then on, every production document from
   every profile is routed by that switch and threshold; a change applies to
   documents processed after it (definitions are cached for about 30 seconds).
3. **What the threshold is compared with.** Each field confidence that extraction
   wrote is compared with the Default queue's threshold alone. The profile's
   `hitl.confidence_threshold` and any per-field thresholds in the class schema still
   drive the confidence alerts and field highlights (see
   [extraction-and-confidence.md](./extraction-and-confidence.md)), but not routing.
   Without confidence scoring, only BDA's blueprint confidence can queue a document.

⚠️ Configuration Profiles no longer carry review-queue settings. A saved profile
that still has a `hitl.review_queues` block loads as before, and the block is
ignored and dropped when the configuration is read.

Turning the Default queue off stops new documents from being queued; items already
in review stay there and can be finished. Headless deployments have no review queue
resources and always use the legacy HITL flow.

## Review status for downstream consumers

Each routed document has one status object,
`s3://<OutputBucket>/<documentKey>/review/status.json`. Its `status` is
`in_review` at first, then `complete` or `rejected`. When an Admin removes the
document from review, or its review is withdrawn, the object is deleted. It is written **last**, after
the tracking status, the reporting rows and the final manifest. The OutputBucket
sends S3 events to EventBridge, so a consumer can trigger on that key instead of
polling for `COMPLETED`. The SDK and CLI report the same value as `review_status`.
The full contract, including the schema, the ordering, `sequence` and an
EventBridge rule pattern, is in the
[review queues module README](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/lib/idp_common_pkg/idp_common/review_queues/README.md#review-status-contract).

## Limits and behaviour to know

- `REJECTED` is a terminal status, but it isn't success: the SDK counts it as
  failed and the CLI exits 1.
- While review queues are on, the hooks before the review point (preprocessing,
  post-OCR, post-classification, post-extraction) run in both the first run and the
  finalize run, so they must be idempotent.
- Reprocessing, aborting or deleting a document in review withdraws its review and
  deletes the status object.
- Documents processed by a Test Studio run are never held in a production review
  queue, whatever the Default queue's settings. Test runs are scored on the machine's output,
  and their documents are reviewed in the test set's own queue instead.
- The legacy Reviewer role and the Document List **Start Review** flow are separate
  from review queues and work as before.
- The Integration API and IDP Monitor don't yet distinguish review states.
