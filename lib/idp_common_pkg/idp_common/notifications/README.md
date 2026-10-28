Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Notifications Module (host Inbox)

`idp_common.notifications` is the platform's one in-app notification service. A
platform Lambda posts an **inbox message** to a user, several users or a Cognito
group; each recipient sees it in the web UI's bell, the **Inbox** navigation entry
and the Inbox page, and can read, mark, delete and mute it.

## Overview

| Piece | Module |
|---|---|
| `notify()`, `NotificationService`, `create_notification_service()` | `service.py` |
| All DynamoDB access (through `idp_common.dynamodb.DynamoDBClient`) | `store.py` |
| Pydantic contracts (`NotificationRequest`, `InboxMessage`, `PostResult`, ...) | `models.py` |
| Recipient keys, ids, cursors | `keys.py` |
| Internal deep links | `links.py` |
| Type catalogue | `catalogue.py` |
| Retention and cap from the active Configuration Profile | `settings.py` |
| Group fan-out (Cognito `ListUsersInGroup`) | `recipients.py` |
| Domain exceptions | `exceptions.py` |

Importing the package needs only Pydantic; the service (and boto3) loads on first
use of `notify`, `NotificationService` or `create_notification_service`.

## Data model

One DynamoDB table, `NotificationsTable`, with one partition per recipient
(`PK = USER#<recipient key>`):

| Item | `SK` | Notes |
|---|---|---|
| Message | `N#<id>` | `id` is 13-digit epoch milliseconds + 12 hex chars, so it sorts by time. `UnreadPK` (= `PK`) exists only while the message is unread. |
| Dedupe pointer | `D#<sha256(dedupe_key)[:32]>` | `CurrentSK` names the message holding the key |
| Idempotency marker | `I#<sha256(idempotency_key)[:32]>` | One per (event, recipient) |
| Preferences | `PREFS` | `MutedTypes`, `UpdatedAt` |

The sparse GSI `UnreadIndex` (HASH `UnreadPK`, RANGE `SK`, INCLUDE
`ExpiresAfter`, `Type`, `Severity`) gives a single-partition unread count, which
`getInboxUnreadCount` returns. `listInboxMessages` and the mutations return the
exact count from the base table instead: the UI lists again straight after the
caller's own mark or delete, and the GSI lags that write. Items
expire through DynamoDB TTL on `ExpiresAfter`; reads also filter
`ExpiresAfter > now`, so an expired item that TTL has not yet deleted is never
listed or counted.

## Recipient keys

Use one helper on both sides:

- `recipient_key_from_claims(claims)`: from **verified ID-token claims** only —
  `email`, then `cognito:username`, then `sub`, the same precedence as
  `identity.username` in `idp_common.api_adapter.normalize_event`.
- `recipient_key_from_cognito_user(user)`: from a Cognito user entry (`email`
  attribute, then `Username`, then `sub`).
- `normalize_recipient_key(value)`: `strip().lower()`; empty is invalid. The user
  pool is case-insensitive, so an Admin-typed `Alice@Example.com` reaches the user
  who signs in as `alice@example.com`.

Because the key is the email when one exists, a user whose email changes stops
seeing messages posted under the old address; those expire through retention.

## Notification types

Every message has a registered type (`NotificationType`: `id`, `display_name`,
`description`, `default_severity`). Declare production types as data in
`catalogue.PLATFORM_TYPES`, so the API resolver sees the same catalogue as every
producer. **Never call `register_type()` from production code**: it exists for
tests and the live-check tool, and a test fails if production code calls it.
Posting an unregistered type raises `UnknownNotificationTypeError`.

## Posting

```python
from idp_common.notifications import document_detail_link, notify

# One user
notify(
    type_id="hitl.review.assigned",
    title="Review assigned",
    body="**invoice.pdf** is waiting for your review.",
    source="hitl.review",
    users=["reviewer@example.com"],
    link=document_detail_link("invoice.pdf"),
)

# Several users
notify(type_id="...", title="...", source="...", users=["a@example.com", "b@example.com"])

# A Cognito group, expanded to its current members at write time
notify(type_id="...", title="...", source="...", groups=["Admin"], severity="warning")
```

`notify()` returns a `PostResult` (`deliveries`, `recipients_considered`,
`truncated`, and the counts `delivered`, `muted`, `duplicates`, `failed`), or
`None` when nothing was posted.

## Severity and links

`severity` is `info`, `success`, `warning` or `error`; it defaults to the type's
`default_severity`. `link` must be an **internal route** (starting with a single
`/`); anything with a scheme or host, `://`, a backslash, whitespace or a control
character is refused, matching the UI's `isInternalRoute`. Build document links with
`document_detail_link(object_key)` (the document page) or `document_edit_link(object_key)`
(the document's edit page, which opens its review item while it is in review), which
encode the key exactly as the UI's `encodeURIComponent` does. Both are pinned against
the UI's builders by one fixture, `tests/resources/link_parity.json`.

## Dedupe

Pass `dedupe_key` to make a new message **replace** the recipient's previous one
with the same key: the replacement is unread and newest, and the unread count
changes by at most one. Use the convention `<source>:<subject>`, for example
`hitl.review:lease:<itemId>`. The replacement is one transaction that moves the
pointer, deletes the old message and writes the new one; a concurrent writer is
retried.

## Idempotency

Pass `idempotency_key` to make a re-post of the same event a no-op for each
recipient (`outcome="duplicate"`). The marker is per recipient and lives as long
as the message's retention, so it still suppresses a re-post after the user deleted
the message.

## Muting

Users mute types in the Inbox preferences. A muted type is **not delivered** — not
stored and not counted — including during group fan-out. Unmuting affects only
messages posted afterwards.

## Retention and cap

`hitl.notifications.retention_days` (1–365, default 30) and
`hitl.notifications.max_per_user` (1–200, default 50) come from the **active**
Configuration Profile, read through `idp_common.config` and cached for about 60
seconds per process. A change applies to messages written after it. If the
configuration cannot be read (or `CONFIGURATION_TABLE_NAME` is unset), the defaults
apply and a WARNING `event=inbox_settings_fallback` is logged once per cache window.

After every write the recipient's partition is trimmed to the cap: expired items
first, then the oldest **read** messages, and only when every message is unread, the
oldest unread one (logged at WARNING as `event=inbox_cap_evicted_unread`). Every
delete is conditional, so concurrent writers converge on the cap.

## Fan-out bound

A post reaches at most `recipients.MAX_RECIPIENTS` (1000) recipients. Beyond that
the list is truncated, a WARNING `event=inbox_fanout_truncated` is logged and
`PostResult.truncated` is `True`. Disabled Cognito users are skipped, and duplicates
across users and groups are delivered once.

## Failure semantics

**`notify()` never raises.** A failure to notify must not fail the producer's own
work:

- invalid input or an unexpected error: `None`, an ERROR `event=inbox_post_failed`
  with the traceback, and the metric `NotificationPostFailures`;
- a per-recipient write failure: that delivery is `failed`, the others continue, an
  ERROR `event=inbox_delivery_failed` per recipient, then one ERROR
  `event=inbox_post_partial_failure` for the post and `NotificationPostFailures`
  counting the failures;
- a failed cap trim: the message is still `delivered`; ERROR
  `event=inbox_cap_trim_failed` and the metric `NotificationCapTrimFailures`. The
  next write heals the cap.

`NotificationService.post()` itself raises for an unknown type or for group
recipients without `USER_POOL_ID`; `notify()` turns those into the logged `None`.

## Factory and headless no-op

`create_notification_service()` returns `None` when `NOTIFICATIONS_TABLE_NAME` is
unset or empty (for example in a headless deployment, where the table does not
exist), and `notify()` then logs at DEBUG and returns `None`.

## Producer IAM

A Lambda that posts needs (none in this release):

- `dynamodb:PutItem`, `UpdateItem`, `DeleteItem`, `GetItem`, `Query`,
  `BatchGetItem`, `BatchWriteItem` and `ConditionCheckItem` on the table and on
  its `/index/UnreadIndex`
- read access to the `ConfigurationTable`
- `cognito-idp:ListUsersInGroup` on the user pool, only if it posts to groups
- `cloudwatch:PutMetricData`
- KMS use of the customer-managed key that encrypts the table
- environment `NOTIFICATIONS_TABLE_NAME` (the parent stack Output
  `NotificationsTableName`), `USER_POOL_ID` (for group fan-out) and
  `CONFIGURATION_TABLE_NAME`

## API operations

The web UI reaches a user's own Inbox through self-scoped operations:
`listInboxMessages`, `getInboxUnreadCount`, `getInboxPreferences`,
`markInboxMessages`, `markAllInboxMessagesRead`, `deleteInboxMessages`,
`clearReadInboxMessages` and `updateInboxPreferences`. The recipient comes only from
verified claims, and an id that belongs to someone else behaves as not found. See
[RBAC](../../../../docs/rbac.md) for their policies.

They are served by `NotificationsResolverFunction`
(`nested/api-resolvers/src/lambda/notifications_resolver/index.py`), a thin handler
that validates `event["arguments"]` with the models in `api_models.py`:

| Model | Operation | Rules |
|---|---|---|
| `ListArgs` | `listInboxMessages` | `read`, `types` (type-id pattern), `severities`, `limit` and `nextToken`. `limit` defaults to 20 and is clamped, not rejected, to 1–100 by `models.clamp_list_limit`, the same function `NotificationService.list_messages` uses; a non-integer is an error |
| `IdsArgs` | `deleteInboxMessages` | `ids`: 1–100 message ids, de-duplicated with order kept |
| `MarkArgs` | `markInboxMessages` | `IdsArgs` plus `read` (a boolean) |
| `PrefsArgs` | `updateInboxPreferences` | `mutedTypes`: up to 100 type ids; unknown ids are rejected by the service |

Every model forbids unknown arguments, so no operation accepts a user id.
`summarize_validation_error` turns a Pydantic error into a 400 message that names each
bad argument and never echoes its value. The resolver re-raises exactly `ValueError`
(400), `idp_common.api_adapter.ResourceNotFound` (404) or `PermissionError` (403, a
caller with no group or no identity claim), because the dispatcher maps errors by
class name. Responses are `model_dump(by_alias=True, mode="json")` of the service
models, so keys are camelCase.

The table name reaches producers through the parent stack Output
`NotificationsTableName`; the resolver gets it from the `NotificationsTableName`
parameter of the API resolvers stack. A headless deployment has no table:
`create_notification_service()` returns `None` and `notify()` does nothing.

## Testing

`lib/idp_common_pkg/tests/unit/notifications/conftest.py` provides
`notifications_table` (a moto table identical to the template's), `test_type` (a
registered test-only type), `clock` (a deterministic UTC clock) and
`make_service(...)` (a service on the moto table with fixed settings and an
optional group lister). The table definition itself is in `_table.py` next to it
(`create_notifications_table()`), so suites in other folders, such as the resolver
tests in `tests/unit/lambdas/test_notifications_resolver.py`, build the same table;
`test_table_parity.py` pins it to `NotificationsTable` in the parent `template.yaml`.
