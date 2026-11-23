# Bulk review actions

Bulk **Skip All Reviews**, its jobs, and **Send to review** in the web UI. The server
side is `idp_common.review_queues` (`bulk_jobs`, `review_skip`, `send_to_review`; see
its [README](../../../../../lib/idp_common_pkg/idp_common/review_queues/README.md)).

| Module                                                                                                                                   | What it does                                                                                                                                                                   |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [`bulk-review-api.ts`](bulk-review-api.ts)                                                                                               | Typed calls: dry run, run (with a `clientRequestId`), job and job list, Send to review                                                                                         |
| [`bulk-outcomes.ts`](bulk-outcomes.ts)                                                                                                   | Labels and StatusIndicator types of the per-document outcomes and job statuses; the outcome list mirrors `BulkOutcome` in `bulk_models.py` (`__tests__/bulk-outcomes.test.ts`) |
| [`review-menu-items.ts`](review-menu-items.ts)                                                                                           | The Document List's **Review** menu and which selected rows each action counts; `isSkippableQueueItem` for the queue page                                                      |
| [`BulkSkipModal.tsx`](BulkSkipModal.tsx)                                                                                                 | Selected documents or a profile and date range: dry run, reason, resubmit, then an inline run or a job                                                                         |
| [`SendToReviewModal.tsx`](SendToReviewModal.tsx)                                                                                         | Send to review's reason dialog (Document Detail and the Document List)                                                                                                         |
| [`BulkResultsTable.tsx`](BulkResultsTable.tsx), [`BulkResultsModal.tsx`](BulkResultsModal.tsx), [`OutcomeCounts.tsx`](OutcomeCounts.tsx) | Per-document results (filter, Excel export) and outcome counts                                                                                                                 |
| [`JobsPage.tsx`](JobsPage.tsx), [`JobDetailPage.tsx`](JobDetailPage.tsx)                                                                 | Home › Jobs and a job's page, polled while a job is queued or running                                                                                                          |
| [`use-review-action-settings.ts`](use-review-action-settings.ts)                                                                         | Whether the Default queue is on and the inline limit (from `getMyReviewQueues`), cached for 60 s                                                                               |

New controls that send a mutation go in `MUTATION_CONTROLS`
(`scripts/tests/test_ux_story_relay.py`).
