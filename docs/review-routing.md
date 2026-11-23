---
title: "Review Routing"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Review Routing

## Table of Contents

- [Overview](#overview)
- [When a document is held](#when-a-document-is-held)
- [The Review Routing switch](#the-review-routing-switch)
- [The built-in routing rules](#the-built-in-routing-rules)
- [Routing rules](#routing-rules)
- [Inputs](#inputs)
- [Tests](#tests)
- [Inputs that are lists](#inputs-that-are-lists)
- [Outcomes](#outcomes)
- [The reason](#the-reason)
- [Failed policy rules](#failed-policy-rules)
- [When the queue cannot take the document](#when-the-queue-cannot-take-the-document)
- [Reviewed documents and the placement limit](#reviewed-documents-and-the-placement-limit)
- [Editing the table](#editing-the-table)
- [Warnings](#warnings)
- [Testing a table](#testing-a-table)
- [DMN XML export and import](#dmn-xml-export-and-import)
- [Limits](#limits)
- [Policy rule verdicts in routing rules](#policy-rule-verdicts-in-routing-rules)
- [The showcase preset](#the-showcase-preset)
- [Configuration reference](#configuration-reference)
- [Logs and metrics](#logs-and-metrics)

## Overview

Review Routing decides, for each processed document, whether a person reviews it
and in which [review queue](./review-queues.md). The decision is made by a
**routing table**: an ordered list of **routing rules**, each with conditions, an
outcome and a reason for the reviewer. The first enabled routing rule whose
conditions all hold decides. A document that matches no routing rule gets the
table's **default outcome**.

Each configuration profile has one routing table, in its `routing` section. The
table is versioned with the profile, so a document is routed by the table of the
configuration revision it is processed with. A revision that holds no `routing`
section, such as one saved before routing tables existed, routes with the table the
`default` profile holds, as it takes every setting it lacks from `default` (see
[Selecting a revision for processing or testing](./configuration-profiles.md#selecting-a-revision-for-processing-or-testing)):
the built-in routing rules unless an Admin changed `default`'s.

The table is evaluated once per run, by the **Review Routing** step, which comes
after policy rule validation and before summarization, in both Pipeline mode and
BDA mode:

```
… → Extraction → Assessment → Policy Rule Validation → Review Routing → Summarization → Evaluation
```

A routing rule can therefore read everything the earlier steps produced:
extracted values, confidence alerts, missing required fields, sections whose
extraction failed, the classes of the document's sections, and the verdicts of
[policy rules](./rule-validation.md). A document the table sends to a queue stops
there (status `HITL_IN_PROGRESS`) until its review ends; a document it does not
send to a queue goes on to summarization.

A profile that has never been edited routes as the
[built-in routing rules](#the-built-in-routing-rules) say: a document with a
section whose extraction failed, with confidence alerts or with missing required
fields goes to the Default queue.

## When a document is held

A routing rule can hold a document only when all of these are true. When one of
them is false the Review Routing step lets the document continue, and its log line
says why (see [Logs and metrics](#logs-and-metrics)):

| What must be true | Logged when it is not |
|---|---|
| The document was not submitted by a Test Studio run. Those documents are reviewed as test-set work and are never placed in a production queue | `test_studio` |
| The stack has review queues. A [headless deployment](./headless-deployment.md) has none | `no_review_store` |
| **Review Routing** is on in the document's configuration profile: `hitl.enabled: true` (see [The Review Routing switch](#the-review-routing-switch)) | `hitl_off` |
| The **Default** review queue is enabled (**Review Studio › Review Management**). While it is disabled, no document is held, whichever queue a routing rule names | `default_queue_off` |

A Default queue whose settings cannot be read for a moment (a throttled or timed-out
read) is not taken for a queue that is off: the step is retried.

## The Review Routing switch

**Review Routing** is a setting of each configuration profile (`hitl.enabled`). In
the web UI it is the **Review Routing** switch in **Configuration → Processing
Settings → Review Routing (Optional)**, the section after **Policy Rule Validation
(Optional)**. While it is off no document of that profile is held, whatever the
routing rules say.

The same section holds the two settings the built-in routing rules read,
**Confidence threshold** (`hitl.confidence_threshold`) and **Review missing required
fields** (`hitl.route_missing_required_fields`), and the Inbox and bulk-skip
settings, which apply whether or not Review Routing is on.

**Its value on a new stack.** A newly created stack turns Review Routing on in the
profiles it creates (`default` and the managed profiles), so documents with a
failed section, confidence alerts or missing required fields wait in the Default
queue for a reviewer. These cases differ:

- A preset or a custom configuration that sets `hitl.enabled` itself decides: the
  value it sets is applied, on a new stack and on each update.
- Otherwise a stack update does not change the switch. Each profile keeps the value
  it has, and a managed profile that an update adds takes the value of `default`.
- A [headless deployment](./headless-deployment.md) has no review queues, and its
  profiles are left with Review Routing off.

A profile you create later takes the value of the profile it is created from
(`default`, for a new profile name uploaded with the CLI or the SDK), unless the
configuration you save sets `hitl.enabled`.

**Turning it off for unattended processing.** A stack that processes documents with
nobody reviewing them (a batch job, a load test, a pipeline that waits for every
document to complete) needs the switch off, or documents with a low-confidence field
wait in the Default queue. Turn it off in the profile the documents are processed
with:

- In the web UI: **Configuration → Processing Settings → Review Routing
  (Optional)**, switch **Review Routing** off, and save.
- With the CLI or the SDK, upload a file that holds only the switch. An upload is
  merged into the profile, so every other setting keeps its value:

  ```yaml
  hitl:
    enabled: false
  ```

  ```bash
  idp-cli config-upload --stack-name my-stack \
      --config-file review-routing-off.yaml --config-profile default
  ```

- For a stack that should never start with it on, set `hitl.enabled: false` in the
  custom configuration the stack is deployed with (`CustomConfigPath`, or
  `idp-cli deploy --custom-config`).

## The built-in routing rules

Every profile starts with these routing rules, and a `routing` section with no
table is read as this one:

| Order | Id | Conditions | Outcome | Reason |
|---|---|---|---|---|
| 1 | `builtin-extraction-failed` | The document has not been reviewed, and the extraction of at least one section failed without failing the document | Default queue | `Sections whose extraction failed: {extraction_failed_count}. They have no extracted values: enter them from the document.` |
| 2 | `builtin-confidence-alerts` | The document has not been reviewed, and at least one field is under its confidence threshold | Default queue | `Fields below the confidence threshold: {confidence_alert_count}. Check the highlighted fields and correct them.` |
| 3 | `builtin-missing-required` | The document has not been reviewed, `hitl.route_missing_required_fields` is on, and at least one required field has no extracted value | Default queue | `Required fields with no value: {missing_required_field_count}. Fill them in from the document.` |

Default outcome: no review.

The failed extraction comes first because such a section has no fields, so its
required fields are missing too, and the first routing rule that matches gives the
reviewer the reason. See
[Where a failed section shows up](./extraction-and-confidence.md#where-a-failed-section-shows-up).

They are ordinary routing rules. You can disable them, reorder them, change their
queue or their reason, or delete them, and **More actions › Restore built-in
routing rules** on the Routing Rules tab puts back the ones the table no longer
has. A table holds only what its routing rules say: one with no routing rule that
tests `extraction_failed_count` does not hold a document for a failed section.

## Routing rules

A routing rule has:

| Part | Meaning |
|---|---|
| **Id** | Identifies the routing rule in the document's routing record, in logs and in DMN files. Lower-case letters, digits, hyphens, dots and underscores; unique in the table. On the Routing Rules tab it cannot be changed after the routing rule is added |
| **Label** | The name reviewers see on a document the routing rule placed in a queue ("Routed by rule: …"). Optional |
| **Enabled** | A disabled routing rule is kept in the table and never evaluated |
| **Conditions** | Each condition is an [input](#inputs) and a [test](#tests) on its value. The routing rule matches a document when **every** condition holds. A routing rule with no conditions matches every document |
| **Outcome** | **Send to a review queue** (and which one), or **No review**. See [Outcomes](#outcomes) |
| **Reason** | What the reviewer is told about a document the routing rule placed in a queue. Optional. See [The reason](#the-reason) |

Routing rules are tried in the order they are listed, and the first match decides.
Put the specific routing rules first: one placed after a routing rule that matches
every document it would match can never decide, and the Routing Rules tab lists
such routing rules under **Routing rules that can never match**.

A "No review" routing rule is how you exempt documents from a later one. For
example, a first routing rule "document class is `Receipt` → No review" keeps
receipts out of review whatever the routing rules after it say.

## Inputs

An input is one fact about the processed document. Some inputs take a **path**
that says which field, policy rule, policy type or class they read.

| Input | Path | Value | Example condition |
|---|---|---|---|
| `document_class` | — | The class of the document's first section | `document_class` test `"Invoice"` |
| `section_classes` | — | The classes of the document's sections (a list) | `section_classes`, any, test `"Bank-checks"` |
| `class_present` | class | True when at least one section of the document has that class, false when none has | `class_present` path `Bank-checks` test `false` |
| `page_count` | — | Number of pages | `page_count` test `> 50` |
| `section_count` | — | Number of sections | `section_count` test `[2..5]` |
| `confidence_alert_count` | — | Fields under their confidence threshold, over the sections that have not been confirmed by a review | `confidence_alert_count` test `> 0` |
| `missing_required_field_count` | — | Required fields with no extracted value, over the same sections | `missing_required_field_count` test `>= 2` |
| `extraction_failed_count` | — | Sections whose extraction failed without failing the document, over the same sections. Such a section has no extracted values and raises no confidence alert | `extraction_failed_count` test `> 0` |
| `route_missing_required_fields` | — | The setting `hitl.route_missing_required_fields` (true when unset) | `route_missing_required_fields` test `true` |
| `field_value` | `[Class:]field.path` | The value of an extracted field in every section (of that class, when the path names one); a list | `field_value` path `Invoice:GrossTotal`, any, test `> 1000` |
| `policy_rule_verdict` | `policy_type.rule_key` | The verdict of one policy rule: `pass`, `fail`, `not_found` or `unknown`; no value when the policy rule was not evaluated | `policy_rule_verdict` path `invoice_controls.total_matches_lines` test `"fail"` |
| `failed_policy_rule_count` | policy type, or empty for all | Policy rules whose verdict is `fail` | `failed_policy_rule_count` test `> 0` |
| `reviewed` | — | True when every section of the document has been confirmed by a completed review | `reviewed` test `false` |
| `processing_mode` | — | `pipeline` or `bda` | `processing_mode` test `"bda"` |

Notes on the values:

- Sections whose class is excluded from processing are left out of every input.
- A **field path** is the field's name in the class schema, with dots for nested
  fields and an index for a list item: `Vendor.Name`, `LineItems[0].Amount`.
  Prefix it with a class and a colon to read sections of that class only. When a
  profile is saved, the path must exist in the class it names, or in at least one
  class when it names none.
- A **policy rule** is named `policy_type.rule_key`, the id shown beside each policy
  rule in the Policy Rule Validation Summary.
- A **class** for `class_present` is one of the profile's document classes, and is
  compared without regard to case. `class_present` is how a routing rule catches a
  package that lacks a document it should contain: `class_present` path
  `Bank-checks` test `false` holds for a lending package with no bank check. When a
  profile is saved, the class must be a class of the profile.
- `confidence_alert_count` counts what the document page shows as confidence
  alerts: each field against its own threshold, otherwise against
  `hitl.confidence_threshold` (see
  [Extraction and Confidence](./extraction-and-confidence.md)).
- An input the document has no value for is empty. This is the case for
  `document_class` on a document with no sections, and for `policy_rule_verdict`
  when policy rule validation did not run. [Tests](#tests) says how an empty
  value compares.

## Tests

A test is written in the cell syntax of DMN decision tables (S-FEEL unary tests).

| Test | Holds when the value … |
|---|---|
| `-` | Always (the condition is not used) |
| `"Invoice"` | equals the text `Invoice` (case-sensitive) |
| `"Invoice", "Credit-Note"` | equals one of the listed values |
| `not("Receipt")` | is not `Receipt` |
| `!= "Receipt"` | is not `Receipt` |
| `1000` or `= 1000` | equals 1000 |
| `> 1000`, `>= 1000`, `< 5`, `<= 5` | compares that way with the number |
| `[1..10]` | is from 1 to 10, both included |
| `(1..10)` | is between 1 and 10, both excluded |
| `[1000..5000)` | is at least 1000 and less than 5000 |
| `true`, `false` | is that boolean |
| `> date("2026-01-01")` | is a date after 1 January 2026 |
| `null` | is empty |

How values are compared:

- **Numbers** are compared exactly. An extracted value that is text holding a
  number (`"1250.00"`) is read as that number. A value that is not a number does
  not satisfy a numeric test, and is not an error.
- **Text** is compared as it is: `"invoice"` does not match `Invoice`.
- **Dates** are written `date("YYYY-MM-DD")` and compare with a value in ISO form
  (`2026-03-31`, or the date part of `2026-03-31T10:15:00Z`).
- **Booleans** are `true` and `false`, without quotes. A boolean is never read as
  a number.
- An **empty value** equals nothing, so `"Invoice"` does not hold for it, and
  `!= "Invoice"` and `not("Invoice")` do. A comparison (`<`, `>`, a range) with an
  empty value, or with a value of another type, holds neither way: `> 1000` does
  not hold, and neither does `not(> 1000)`. Use `null` to test for emptiness.

A test that cannot be read is refused when the profile is saved, with the routing
rule, the input and the position of the error.

## Inputs that are lists

`section_classes` and `field_value` have one value per section. For these the
condition also says how the test is applied (**Match**):

| Match | The condition holds when … |
|---|---|
| `any` | the test holds for at least one value |
| `all` | there is at least one value and the test holds for every one |
| `count` | the test holds for the **number** of values |

Examples: `section_classes`, any, `"Bank-checks"` (the package contains a bank
check); `field_value` `LineItems[0].Amount`, all, `<= 500`; `section_classes`,
count, `> 10` (more than ten sections of any class). For `field_value`, sections
in which the field has no value contribute no value.

## Outcomes

A routing rule's outcome is a list of **actions**: what happens to a document the
routing rule matches. There is one action type, `review_queue`, which sends the
document to a review queue, and an outcome holds at most one action. An empty list
means the document is not reviewed. The Routing Rules tab shows the list as one
choice:

- **Send to a review queue** (`actions: [{type: review_queue, queue: <id>}]`). The
  document is placed in the queue the routing rule names and stops with status
  `HITL_IN_PROGRESS`. The queue is named by its id; create it first in **Review
  Studio › Review Management** (see
  [Review Queues](./review-queues.md#creating-a-queue)). A save is refused when a
  routing rule names a queue that does not exist.
- **No review** (`actions: []`). Processing continues with summarization.

The **default outcome** is what a document that matches no routing rule gets. It
has the same two parts as a routing rule's, actions and a reason, and it is "No
review" unless you change it.

The document's review record keeps the routing rule that placed it, the queue, the
values the routing rule read, the [reason](#the-reason) and the
[failed policy rules](#failed-policy-rules). Reviewers see "Routed by rule:
*label*" with the reason in the item's **Review Reason** panel, the document page
shows **Review queue** (with the routing rule under the queue's name) and **Routing
reason**, and `review/status.json` carries `routed_by` (see
[Review status for downstream consumers](./review-queues.md#review-status-for-downstream-consumers)).

## The reason

A routing rule that sends documents to a review queue can carry a **reason**: a
sentence or two, written for the reviewer, that say why the document is held and
what to check. The default outcome can carry one too, when it names a queue. A
reason is optional, and an outcome with no review has none.

A reason is text with **placeholders** in braces. When a document is placed in a
queue, each placeholder is replaced with that document's value:

| Placeholder | Replaced with |
|---|---|
| `{input}` | The value of an [input](#inputs) that takes no path: `{page_count}`, `{confidence_alert_count}`, `{document_class}` |
| `{input:path}` | The value of an input for a path, written as a condition writes it: `{field_value:Invoice:GrossTotal}`, `{policy_rule_verdict:invoice_controls.total_matches_lines}`, `{class_present:Bank-checks}`. Everything after the first colon is the path |
| `{condition_N.value}` | What the routing rule's N-th condition compares with: `1000` for the test `> 1000`, `1..10` for `[1..10]`. Not available in the default outcome's reason |
| `{confidence_threshold}` | The profile's confidence threshold (`hitl.confidence_threshold`) |
| `{{` and `}}` | A literal `{` and `}` |

For example, on a routing rule whose second condition is `field_value` path
`Invoice:GrossTotal` test `> 1000`:

```yaml
reason: "Invoice total {field_value:Invoice:GrossTotal} is over {condition_2.value}. Check the total against the line items."
```

A document with a gross total of 1250.00 is placed with the reason *Invoice total
1250.00 is over 1000. Check the total against the line items.*

How a value is written:

- A value the document does not have is written `(no value)`.
- A boolean is written `yes` or `no`, and a whole number without a fraction.
- A list (`section_classes`, `field_value`) is written as its values separated by
  commas. A long list shows its first five values, then `and N more`.
- A single value is cut to 200 bytes and the whole reason to 1000 bytes; a text that
  was cut ends with `…`.
- Values are filled in once. Braces inside an extracted value are text, and are
  never read as a placeholder.

A reason is checked when the profile is saved: its length, that its outcome names a
review queue, and that every placeholder names an input, with the path that input
needs (a field of the class schema, a policy rule or policy type of the Policy
Schema, a class of the profile) or a condition the routing rule has. See
[Limits](#limits).

**A routing rule with no reason gets a generated one.** It is built, in English,
from the routing rule's label and its conditions, with the value the document had:

| What placed the document | Generated reason |
|---|---|
| A routing rule with conditions | `Invoices over 1000: Reviewed false (found no); Field value Invoice:GrossTotal > 1000 (found 1250.00)` |
| A routing rule with no conditions | `<label>: this routing rule applies to every document` |
| The default outcome | `No routing rule matched.` |
| The table could not be evaluated | `The routing rules could not be evaluated, so the document is in the Default queue.` |

The generated reason is also used when a reason cannot be filled in for a document.
The record says which one a document got: `reason_source` is `admin` for the
reason as written, and `fallback` for the generated one.

**Where the reason is shown and recorded.**

| Where | What it holds |
|---|---|
| The **Review Reason** panel of the queue item | The routing rule (**Routed by rule: …**) with the reason under it. A long reason shows its beginning, with **Show more** (see the [Annotator guide](./review-queues.md#annotator-guide)) |
| **Home › Tasks** and a queue's **Documents** table | The **Reason** column, shortened, with the whole text on request |
| The document page | **Review queue**, with the routing rule under the queue's name, and **Routing reason** |
| The review history (**Reports › History › Review Activity**) | **Routing rule: …** and **Routing reason: …**, on the entry written when the document is placed |
| `review/status.json` | `routed_by.reason` and `routed_by.reason_source` (see [Review status for downstream consumers](./review-queues.md#review-status-for-downstream-consumers)) |
| The document's status | `idp-cli status` prints `Reason:`, and its JSON form has `routing_reason`; the SDK has `status.review.reason` (see [IDP CLI](./idp-cli.md) and [IDP SDK](./idp-sdk.md)) |
| The [reporting database](./reporting-database.md#document-reviews-table) | `document_reviews.routing_reason`: the reason **as written**, with its placeholders unfilled |

A reason filled in for a document can hold extracted values, so it is stored with
the review and never written to the logs. The reporting database gets the reason as
written for the same cause: it is read across profiles.

## Failed policy rules

When a document is placed in a review queue and policy rule validation gave it
failed policy rules, the placement records them, whichever routing rule placed the
document and whether or not that routing rule reads a policy rule result. Nothing
has to be written for it in the reason.

Each failed policy rule is recorded with its id (`policy_type.rule_key`), its
policy, its name, the explanation policy rule validation gave (as one line) and the
page numbers that explanation cites. The record holds the first ten in the order of
the Policy Rule Validation Summary, and the number of failed policy rules in all.

| Where | What it holds |
|---|---|
| The **Review Reason** panel of the queue item | **Failed policy rules (n)**: the first three, each with its name, its policy and the explanation, and **Show all n failed policy rules** for the rest of those recorded. **Reports › Policy Rule Validation Summary** on the edit page has the whole report |
| **Home › Tasks** and a queue's **Documents** table | The number of failed policy rules, after the reason |
| `review/status.json` | `routed_by.policy_failures` and `routed_by.policy_failure_count` |
| The document's status | `idp-cli status` prints `Failed policy rules:` with the number, and its JSON form has `policy_failures` and `policy_failure_count`; the SDK has `status.review.policy_failures` and `status.review.policy_failure_count` |

A document has failed policy rules to record only when policy rule validation runs
for its profile (see
[Policy rule verdicts in routing rules](#policy-rule-verdicts-in-routing-rules)).

## When the queue cannot take the document

The Review Routing step never drops a document that a routing rule sent to review:

| Situation | What happens | Recorded as |
|---|---|---|
| The routing rule's queue has been deleted | The document goes to the Default queue | `queue_missing` |
| The routing rule's queue is disabled | The document goes to the Default queue | `queue_disabled` |
| The table or the document's results could not be evaluated | A document that has not been reviewed goes to the Default queue | `evaluation_error` |

The reviewer sees why the document is in the Default queue. **Routing rules that
use this queue** on a queue's page, and the confirmation dialog when you delete a
queue, show which profiles' routing rules send documents to it.

## Reviewed documents and the placement limit

When a review is completed, the document runs again from the point where the
review's changes are applied: policy rule validation runs on the corrected
values, and the routing table is evaluated again. This lets a routing rule send a
corrected document to a second queue, for example a "Compliance" routing rule that
reads a policy rule verdict which only fails after the correction.

Two things keep this from looping:

- The input `reviewed` is true for a document whose review was completed. The
  built-in routing rules test `reviewed = false`, so they do not hold a reviewed
  document again. Add the same condition to your own routing rules unless you
  intend a second review.
- **Maximum placements in a row** (`routing.max_consecutive_placements`, from 1
  to 10, default 3) limits how many times in a row a document can be placed: the
  first time, and each time a routing rule sends the reviewed document to a queue
  again. Above the limit the document continues without another review, and the
  event is recorded in its review history.

**A review completed after the page grouping was changed.** When the reviewer added
a section or changed a section's pages, the run after the review extracts those
sections again before it applies the review (see
[Regrouping pages in a review](./review-queues.md#regrouping-pages-in-a-review)).
Their new values are not confirmed by a reviewer, so `reviewed` is false for that
document, and the alerts, missing required fields and failed extractions of those
sections count again.
The built-in routing rules can therefore hold the document a second time, and that
placement counts toward the limit.

A review that ends in **Reject** ends the document as `REJECTED` and evaluates
nothing. A review that an Admin removes, or that is skipped, lets the document
continue without being held again. A document that is **sent back** is processed
again from classification (from the beginning in BDA mode) and is routed as an
unreviewed document when its new run reaches the Review Routing step: `reviewed` is
false, and its placements are counted from one again.

## Editing the table

Open **Configuration**, choose the profile, and open the **Routing Rules** tab,
after **Policy Schema**. It lists the routing rules in the order they are tried,
with each one's id, label, conditions, outcome and **Enabled** switch. A routing
rule's label opens it: to edit it for an Admin, to read it for anyone else.
**Default outcome**, its reason and **Maximum placements in a row** are under the
table.

**Add routing rule** opens the routing rule dialog, which has these groups:

- **Routing rule**: the **Routing rule id**, the **Label** and **Enabled**.
- **Conditions**: the conditions, up to the [limit](#limits), each in a box of its
  own titled with its number (**Condition 1**, **Condition 2**, …), which is the
  number a [placeholder](#the-reason) such as `{condition_1.value}` names. The first
  line of a condition chooses the **Input**; for an input that takes a path, the
  **Field**, **Policy rule**, **Policy type** or **Class** it reads, from the
  profile's own; and for an input that is a list, the **Match**. The second line
  is the test, entered with guided controls:
  - a yes or no input: **Yes** or **No** (**Present** or **Missing** for *Class is
    present*);
  - a text input: **Is one of** or **Is none of**, and the values. Classes, policy
    rule verdicts and processing modes are chosen from a list; other values are
    typed;
  - a number: a comparison (**Equals**, **Does not equal**, **Greater than**, **At
    least**, **Less than**, **At most**, **Between**) and the number.

  **Edit as expression** shows the value as a [test](#tests) to type, for a test the
  controls do not offer, and **Use guided controls** goes back when the controls can
  show what was typed. A date field, and a field whose type the schema does not
  state, are entered as an expression. The menu at the top right of a condition has
  **Duplicate**, **Move up**, **Move down** and **Remove**.
- **Outcome and reason**: **Send to a review queue**, with the **Review queue**, or
  **No review**. With a queue, **Reason for the reviewer (optional)** takes the
  [reason](#the-reason). **Insert value** puts a placeholder where the cursor is:
  what each of this routing rule's conditions reads and compares with, then every
  input that takes no path and the confidence threshold. The field shows how many
  of its bytes are used.

On the table:

- A routing rule's **Actions** menu has **Edit**, **Duplicate** (a copy under a new
  id, right after it), **Move up**, **Move down** and **Delete**, which asks before
  it removes the routing rule. Dragging a routing rule by its handle reorders too.
- **More actions** has **Import DMN**, **Export DMN 1.5**, **Export DMN 1.3** (see
  [DMN XML export and import](#dmn-xml-export-and-import)) and **Restore built-in
  routing rules**.
- The tab warns when the routing rules are not evaluated, or cannot do what they
  say (see [Warnings](#warnings)).
- Nothing takes effect until you **save the configuration**. New documents use
  the table once the saved revision is the one the profile processes with (see
  [Configuration Profiles](./configuration-profiles.md)).

**Only Admins can change routing rules.** Authors and Viewers see the table
read-only, and can open a routing rule to read it. An Author's save that would
change the `routing` section is refused, and the same Author can still save every
other section. A configuration uploaded with the CLI or the SDK is not
role-checked; it is authorised by the caller's AWS credentials, as every upload is.

The checks that run on save are the same on every path: the table's
[limits](#limits), that every input exists and has the path it needs, that every
test can be read and fits the input's type, that a field path exists in the class
schema, a policy rule in the Policy Schema and a `class_present` class among the
profile's classes, that every outcome's actions and reason are valid, and, on a
stack that has review queues, that every queue a routing rule names exists.

## Warnings

**On the Routing Rules tab.**

| Warning | Shown when | Shown to |
|---|---|---|
| *Review Routing is off for this profile, so no document is held for review and these routing rules are not evaluated.*, with **Open Processing Settings** | The profile's [Review Routing switch](#the-review-routing-switch) is off | Everyone |
| *The Default review queue is off, so routing rules are not evaluated and documents use the document-page review flow.*, with a link to **Review Management** | The Default queue is disabled (see [When the Default queue is off](./human-review.md#when-the-default-queue-is-off)) | Admins |
| *Tests a policy rule result; policy rule validation is off*, on a routing rule | A condition or the reason of that routing rule reads a policy rule result, and policy rule validation is off for the profile or the profile has no policies | Everyone |
| **Routing rules that can never match** | A routing rule comes after one that matches every document it would match | Everyone |
| **Queues that do not exist** | A routing rule or the default outcome names a queue that is not a review queue | Admins |

**When a profile is saved.** A save is accepted, with a warning, when it leaves a
routing rule that reads a policy rule result in a profile where policy rule
validation does not run (it is off, or the profile has no policies). There is one
warning for each enabled routing rule whose conditions test a policy rule result,
one for each whose reason only shows one, and one for a default outcome's reason
that does:

```
Routing table 'review-routing', rule 'compliance-check': this routing rule tests a policy rule result (failed_policy_rule_count), and policy rule validation is off for this profile or it has no policies, so the routing rule never matches.
```

Every save is checked, not only a save that changes the routing table, because
turning policy rule validation off is what makes such a routing rule stop
matching. The web UI shows the warnings with the confirmation that the
configuration was saved, `idp-cli config-upload` prints them in its `Warnings:`
block on standard output, after any validation warnings (see
[config-upload](./idp-cli.md#config-upload)), and the SDK returns them in
`ConfigUploadResult.warnings`.

## Testing a table

**Test on a document** on the Routing Rules tab runs the table, as it is shown on
the tab including unsaved changes, on a document that has already been processed.
Enter the document's key and choose **Test**. The result shows:

- the decision (**Held for review** or **No review: processing continues**);
- the matched routing rule, or that the default outcome applied;
- the review queue, and why the Default queue would be used instead when the
  routing rule's queue is missing or disabled;
- the values of the inputs the table read;
- anything the table refers to that the document does not have (a class, a
  policy rule result).

A test reads the document's stored results and changes nothing: no review is
created and the document's status does not change. The answer is about the table
only. Whether a run reaches the table (Review Routing on, the Default queue
enabled, not a Test Studio document) is not part of the test. Testing is available
to Admins.

## DMN XML export and import

A routing table can be exported as a DMN decision table and imported from one, so
it can be reviewed or edited in a DMN modeler and kept in version control.

- **Web UI (Admins):** **More actions** on the Routing Rules tab has **Export DMN
  1.5**, **Export DMN 1.3** (the version Camunda Modeler opens) and **Import DMN**.
  An import asks before it replaces every routing rule in the table, and is saved
  only when you save the configuration.
- **CLI:** `idp-cli config-routing-export --config-file config.yaml --output
  routing.dmn [--dmn-version 1.3]` and `idp-cli config-routing-import --dmn-file
  routing.dmn --output routing.yaml`. Both convert local files and call no AWS
  service; upload the result with `idp-cli config-upload`.
- **SDK:** `client.config.routing_to_dmn(config)` and
  `client.config.routing_from_dmn(xml)`.

The file holds one decision with one decision table, hit policy `FIRST`. Each
input column is one input of this page written as a call (`reviewed`,
`field_value("Invoice:GrossTotal","any")`, `class_present("Bank-checks")`), and
each row is a routing rule. The outputs are `outcome` (`queue` or `no_review`),
`queue` (the queue id) and `reason` (the reason as written, with its
placeholders), and an export always writes all of them. A disabled routing rule is kept
in the file and marked. Exporting a table and importing the file gives the same
routing rules, in the same order. `max_consecutive_placements` is not part of the
table and is not in the file.

An import accepts DMN 1.3, 1.4 and 1.5 files. A file whose outputs are `outcome`
and `queue` alone is accepted too, and its routing rules get no reason. An import
refuses, naming the construct:

- more than one decision, or a decision that depends on another decision or on a
  business knowledge model;
- decision logic that is not a decision table (a literal expression, a context,
  an invocation, a relation, a list, a function);
- a hit policy other than `FIRST`, or an aggregation;
- an input expression that is not one of the [inputs](#inputs);
- outputs other than `outcome`, `queue` and `reason` in that order, or `outcome`
  and `queue` alone, and an `outcome` that is neither `"queue"` nor `"no_review"`;
- a cell that is not one of the [tests](#tests) of this page;
- a file with a DTD or an entity declaration, a file larger than 512 KB, or more
  routing rules than a table can hold.

Item definitions, input data, allowed-value lists, diagram information and other
vendors' attributes are ignored. The DMN reader is checked against the decision
tables of the DMN Technology Compatibility Kit that use the supported features.

## Limits

Checked when a profile is saved:

| Item | Limit |
|---|---|
| Routing tables per profile | 1 |
| Routing rules per table | 100 |
| Conditions per routing rule | 8 |
| Routing rule id | 1 to 64 characters: lower-case letters, digits, `-`, `.`, `_`, starting with a letter or a digit |
| Label | 200 bytes |
| Path | 128 bytes |
| Test | 256 bytes |
| Actions per outcome | 1, of type `review_queue` |
| Reason, as written | 500 bytes |
| Placeholders in a reason | 10 |
| The `routing` section | 64 KB |

The table's `stage` is `after_policy_rule_validation`, its hit policy `FIRST`
and its engine `builtin`; other values are refused. A reason on an outcome that
names no review queue is refused, since nobody would read it.

Applied when a document is placed, so that the review record stays small:

| Item | Limit |
|---|---|
| A reason, with its values filled in | 1000 bytes |
| One value in a reason | 200 bytes |
| A list value in a reason | Its first 5 values, then how many more there are |
| Failed policy rules recorded | The first 10, and the number of them in all |
| The explanation of a failed policy rule | 500 bytes |
| Page numbers of a failed policy rule | 10 |
| Input values recorded | 20, each cut to 200 bytes |

## Policy rule verdicts in routing rules

Policy rule validation gives each policy rule a verdict that routing can test:

| Verdict | The policy rule's recommendation |
|---|---|
| `pass` | Pass |
| `fail` | Fail |
| `not_found` | Information Not Found |
| `unknown` | anything else |

Two things to know before routing on them:

- **Verdicts come from a model's reading of the document and can differ between
  runs** of the same document. A routing rule such as
  "any failed policy rule → Compliance" can therefore hold a document in one run
  and not in the next. Test the table on several processed documents before
  relying on a single policy rule.
- **Custom recommendation labels read as `unknown`.** A configuration whose
  policy rule prompt asks for labels other than Pass, Fail and Information Not
  Found still works for the Policy Rule Validation Summary, and a routing rule
  sees `unknown` for those. `failed_policy_rule_count` counts `fail` only.

Policy rule validation must run for the profile for these inputs to have values:
it is on (`rule_validation.enabled: true`, in **Configuration → Processing Settings
→ Policy Rule Validation (Optional)**) and the profile has at least one policy.
When it does not run, `policy_rule_verdict` is empty and
`failed_policy_rule_count` is 0, and saving the profile gives a
[warning](#warnings) for each routing rule that reads one of them.

## The showcase preset

`config_library/unified/review-routing-showcase` is a ready-made configuration
that routes four sample documents of this repository to four queues: a lending
package with a bank check to **Specialist**, a prior-authorization packet with
failed policy rules to **Compliance**, an invoice over 1000 to **Approvals**,
and a W-2 to **Default**. Each sample reaches its queue through a routing rule that
does not read a confidence score, which comes from a model and differs between
runs. Every routing rule of the preset carries a reason with placeholders, and one
tests for a class the document lacks (`class_present`). Its
[README](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/config_library/unified/review-routing-showcase/README.md)
lists the queues to create first and the commands to upload it.

**The scenario documents.** `samples/review-routing/` holds synthetic invoices with
one, two and three known problems: a vendor that is not on the approved list, a net
amount that does not add up, missing payment terms, and an invoice that names a
purchase order the document does not contain. Uploaded under the showcase profile,
each returns to review once for each problem that remains: the first routing rule
that matches places it and gives its reason, the reviewer corrects that problem and
completes the review, and Review Routing runs again on the corrected data. The
routing rules for problems a reviewer cannot correct (a missing class, a policy rule
that reads the page text) apply only before the first review. The preset's README
lists the routing rule, the queue and the correction of every round.

## Configuration reference

```yaml
hitl:
  enabled: true                      # Review Routing is on for this profile
  confidence_threshold: 0.8          # For a field with no threshold of its own
  route_missing_required_fields: true

routing:
  max_consecutive_placements: 3      # 1 to 10
  tables:
    - id: review-routing
      stage: after_policy_rule_validation
      hit_policy: FIRST
      engine: builtin
      rules:
        - id: approvals-high-value
          label: Invoices over 1000
          enabled: true
          conditions:                # All must hold
            - input: reviewed
              test: "false"
            - input: field_value
              path: "Invoice:GrossTotal"
              aggregate: any         # any | all | count, for inputs that are lists
              test: "> 1000"
          actions:                   # What happens to a matching document; [] = not reviewed
            - type: review_queue     # The only action type
              queue: approvals       # A review queue id
          reason: "Invoice total {field_value:Invoice:GrossTotal} is over {condition_2.value}. Check the total against the line items."
        - id: packages-without-a-check
          label: Lending package with no bank check
          enabled: true
          conditions:
            - input: reviewed
              test: "false"
            - input: class_present
              path: "Bank-checks"    # A document class of the profile
              test: "false"
          actions:
            - type: review_queue
              queue: specialist
          reason: "The package has no bank check. Ask the applicant for one."
        - id: builtin-extraction-failed
          label: Failed extractions
          enabled: true
          conditions:
            - input: reviewed
              test: "false"
            - input: extraction_failed_count
              test: "> 0"
          actions:
            - type: review_queue
              queue: default
          reason: "Sections whose extraction failed: {extraction_failed_count}. They have no extracted values: enter them from the document."
        - id: builtin-confidence-alerts
          label: Confidence alerts
          enabled: true
          conditions:
            - input: reviewed
              test: "false"
            - input: confidence_alert_count
              test: "> 0"
          actions:
            - type: review_queue
              queue: default
          reason: "Fields below the confidence threshold: {confidence_alert_count}. Check the highlighted fields and correct them."
      default_outcome:
        actions: []                  # No review
        reason: ""                   # Used only when the default outcome names a review queue
```

A test is YAML text, so quote it: `"false"`, `"> 0"`, `'"Bank-checks"'` (a text
value keeps its own double quotes inside the YAML quotes). Quote a reason too: a
brace or a colon in it can be read as YAML syntax otherwise.

An outcome is always written as `actions`. A routing rule that holds an `outcome`
key, and a `default_outcome` that holds `kind` or `queue`, are refused when the
profile is saved, with the form to write instead.

## Logs and metrics

The Review Routing step is the `RoutingFunction` Lambda. Its log group is
`/<StackName>-PATTERNSTACK-<id>/lambda/RoutingFunction`: the name comes from the
nested pattern stack, so list on the `/<StackName>-PATTERNSTACK` prefix to find
it. It writes one line per decision:

```
event=routing_decision decision=hold reason=rule rule=approvals-high-value queue=approvals reason_source=admin
```

`reason` says why the step decided as it did:

| `reason` | Decision | Meaning |
|---|---|---|
| `rule` | `hold` or `continue` | A routing rule matched; `rule` names it |
| `default_outcome` | `hold` or `continue` | No routing rule matched |
| `test_studio` | `continue` | The document belongs to a Test Studio run |
| `no_review_store` | `continue` | The stack has no review queues (a headless deployment) |
| `hitl_off` | `continue` | Review Routing is off for the profile |
| `default_queue_off` | `continue` | The Default queue is missing or disabled |
| `review_skipped` | `continue` | The run follows a review that was skipped or removed |
| `review_rejected` | `rejected` | The run follows a rejected review |
| `unplanned_finalize` | `continue` | The run follows a completed review and carries no run plan |
| `placement_limit` | `continue` | The document reached **Maximum placements in a row** |
| `evaluation_error` | `hold` or `continue` | The table or the document's results could not be evaluated |

`reason_source` says where a held document's reason for the reviewer came from:
`admin` or `fallback`. The reason itself is never logged. A reason that could not
be filled in is logged as `event=routing_reason_render_failed` with the routing
rule's id and the kind of error.

| Metric | Meaning |
|---|---|
| `RoutingDecisions` (dimension `Decision`) | Decisions made, by decision: `hold`, `continue` or `rejected` |
| `RoutingStaleReference` | A routing rule referred to something that could not be read for the document (a class the profile no longer has, a policy rule result when policy rule validation left no summary, a section result that could not be read) |
| `RoutingPlacementLimit` | A document reached the placement limit and continued without another review |
| `RoutingReasonFallback` (dimension `Cause`) | A generated reason was used: `no_reason` when the routing rule has none, `render_error` when its reason could not be filled in |

The metrics are in the stack's own namespace; see [Monitoring](./monitoring.md#review-routing).
