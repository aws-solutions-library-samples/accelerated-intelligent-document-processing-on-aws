Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Run plans (`idp_common.run_plan`)

A run plan says where a pipeline run starts and which steps it skips. Every run the
product starts carries one. A run with no plan starts at the first step, as every
run did before run plans existed, and that stays supported: the document queue's
URL is a stack output, so code outside the product can send to it.

| Module | Holds |
|---|---|
| `steps.py` | The declared list of steps a run can start at or skip, the aliases, and `resolve_start`. The display steps (`DISPLAY_STEPS`), `display_order` and `step_label`: one label for every step |
| `models.py` | `RunPlan`, `RunKind`, and `encode` / `decode` for the `RunPlan` message attribute |
| `asl.py` | `entry_for` and `to_execution_input`: the plan as the `runPlan` block of the execution input |
| `mode.py` | `resolve_processing_mode`: Pipeline mode or BDA mode for a pinned configuration |
| `restart.py` | `decide_restart`: where the run that follows a completed review, a send-back or saved edits starts, derived from the document. `layout_of`, `SEND_BACK_START` and the causes a section is extracted again for |
| `planned.py` | `planned_steps` and `planned_run`: every step of a run before it starts, each with a state. `describe_start`: what a start step clears, keeps and leaves out |
| `clearing.py` | `clear_for_start`, `clear_policy_rule_validation`, `clear_after_review`: remove what a run will produce again. `recount`: the document's alert and missing-field counts from its sections |
| `eligibility.py` | `check`: whether a document can run again from a step, with a refusal code when it cannot; `resolve_resume` |
| `sender.py` | `send_run`: the one sender of the document queue, and the `EventType` values a message carries |
| `reprocess.py` | `ReprocessService`: runs documents again, for Reprocess, send-back and restarting a failed run |
| `resolver_requests.py` | `reprocess_in_requests`: asks the reprocess resolver for many documents in requests of bounded size and joins the answers. Shared by the SDK and the MCP `reprocess` tool; pure |

Importing the package loads nothing; import the module you need. `steps.py` uses
the standard library only, and `models.py`, `asl.py`, `restart.py` and `planned.py`
add pydantic. None of the five imports `idp_common.review_queues`, so the queue
processor reads a plan, and a resolver lists the steps of a run, without loading the
review code. `clearing.py`, `eligibility.py`, `sender.py` and
`reprocess.py` are for the code that queues a run: they read the document model,
`clearing.py` deletes from S3, and `reprocess.py` reads the configuration.

## How a plan travels

```
sender                         builds a RunPlan, clears what the run will produce again,
   │                           and queues the document
   ▼  SQS message attribute "RunPlan" (compact JSON, at most 4,096 bytes)
queue processor                decodes it, resolves the processing mode, and writes
   │                           runPlan into the execution input
   ▼  $$.Execution.Input.runPlan
state machine                  CheckRunPlan jumps to runPlan.startAt; the skip choices
                               read the four skip flags
```

- The plan is a pure function of the message. It holds no timestamp and no random
  value, so a message that is sent again carries the same bytes.
- A message with no `RunPlan` attribute, or with one that does not decode, starts
  an unplanned run: the execution input has no `runPlan` key and the run starts at
  the first step.

## Steps

`steps.RUN_STEPS` is the one declaration of the steps a run can start at or skip.
The SDK's step values, the CLI's choices, the MCP tool's schema and the web UI's
step picker derive from it.

| id | Label | Required | Pipeline mode | BDA mode |
|---|---|---|---|---|
| `beginning` (alias `ocr`) | Beginning | yes | yes | yes |
| `classification` | Classification | yes | yes | alias of `beginning` |
| `extraction` | Extraction | yes | yes | alias of `beginning` |
| `assessment` | Assessment | no | yes | no |
| `policy_rule_validation` | Policy Rule Validation | no | yes | yes |
| `summarization` | Summarization | no | yes | yes |
| `evaluation` | Evaluation | no | yes | yes |

A run also does steps it can neither start at nor skip. `steps.DISPLAY_STEPS`
declares them, each with the run step it comes right before:

| id | Label | Comes before | Listed for |
|---|---|---|---|
| `apply_review_changes` | Apply Review Changes | `policy_rule_validation` | The run after a completed review only |
| `review_routing` | Review Routing | `summarization` | Every run |

- `display_order(mode, review_completed=False)` is the ids of the steps a run in a
  mode does, the display steps in their place. No start or skip choice offers a
  display step.
- `step_label(step_id)` is the one label of a run step or a display step. Every
  surface that names a step reads it, through the generated catalogues
  (`scripts/generate_run_step_catalog.py`) where the surface is not Python.

- A **required** step can be skipped only when its output exists, which is what
  starting after it means. An **optional** step can always be skipped: a plan's
  `skip` list holds optional step ids only.
- In BDA mode one call does OCR, classification and extraction, so `classification`
  and `extraction` mean `beginning` there. `assessment` does not exist in BDA mode.
- `resolve_start(start, mode)` applies the aliases and raises `UnknownStep` or
  `StepNotInMode`.
- `resume` means "the first step that has no output". It is resolved against a
  document before a plan is sent, so a sent plan never carries it.
- `apply_review_changes` and `rejected` are internal starts, used only by runs that
  follow a review. They are never offered to a user.
- Each step names, per mode, the state machine states that do its work
  (`RunStep.states`). A run that entered none of them skipped the step.

## The plan

```python
from idp_common.run_plan.models import RunKind, RunPlan, encode, decode

plan = RunPlan(kind=RunKind.REPROCESS, start="summarization", skip=["evaluation"])
attribute = encode(plan)     # '{"cycle_id":"","kind":"reprocess",...,"v":1}'
decode(attribute) == plan
```

| Field | Meaning |
|---|---|
| `v` | Format version, `1` |
| `kind` | Why the run was queued (`RunKind`) |
| `start` | A step id or alias, or an internal start |
| `skip` | Optional steps the run skips: sorted, unique |
| `sections` | The sections to extract again, when the run starts at `extraction` or `assessment` and not every section needs it. Empty means every section |
| `reviewed` | The document's last review was completed and this run does not extract or assess again |
| `cycle_id` | The review cycle the run follows; review kinds only |

The model refuses combinations that cannot be meant:

| Kind | Start |
|---|---|
| `new_upload`, `legacy_review` | `beginning` |
| `review_completed` | `apply_review_changes`, or `extraction` when sections are extracted again first |
| `review_rejected` | `rejected` |
| `review_skipped` | `policy_rule_validation` |
| `reprocess`, `saved_edits`, `sent_back`, `restart_failed` | Any step or alias |

`reviewed` can be true only for a start that does not extract or assess again
(`apply_review_changes`, `policy_rule_validation`, `summarization`, `evaluation`),
so a `review_completed` plan that starts at `extraction` carries `reviewed` false.

`encode` sorts the keys and raises `RunPlanError` when the result is larger than
`MAX_RUN_PLAN_BYTES`. `decode` raises `RunPlanError` for anything that is not a
plan this code reads, including an unknown key or version.

## The execution input

`to_execution_input(plan, mode=..., policy_rule_validation_enabled=...)` returns
the `runPlan` block. Every key is always present:

```json
{ "v": 1, "kind": "reprocess", "startStep": "summarization", "startAt": "policy_rule_validation",
  "skipAssessment": false, "skipPolicyRuleValidation": true, "skipSummarization": false, "skipEvaluation": false,
  "policyRuleValidationEnabled": true, "sections": [], "reviewed": false, "mode": "pipeline", "cycleId": "" }
```

- `startStep` is the logical step after aliases. `startAt` is the entry id the
  state machine jumps to.
- `sections` is filled only for the entry `pipeline.extraction_sections`.

| Start | Entry id (`startAt`) | Forced skips |
|---|---|---|
| `beginning`, `ocr` | `beginning` | |
| `classification` | `pipeline.classification` (Pipeline); `beginning` (BDA) | |
| `extraction` | `pipeline.extraction`, or `pipeline.extraction_sections` with `sections` (Pipeline); `beginning` (BDA) | |
| `assessment` | `pipeline.assessment` (Pipeline) | |
| `policy_rule_validation` | `policy_rule_validation` | |
| `summarization` | `policy_rule_validation` | policy rule validation |
| `evaluation` | `policy_rule_validation` | policy rule validation, summarization |
| `apply_review_changes` | `apply_review_changes` | |
| `rejected` | `rejected` | |

Summarization and evaluation enter at policy rule validation with that stage
skipped, so the routing step still runs. A reprocess from a late step withdraws an
open review, and without routing the document would never be reviewed again.

A `review_completed` plan that starts at `extraction` enters like any extraction
start. After the Pipeline assembly the state machine's `CheckReviewToApply` reads
`runPlan.kind` and sends that run through `SelectAssembledDocument` to
`ApplyReviewChangesStep`, so the review is applied to the new results before policy
rule validation. Every other run goes from the assembly to policy rule validation.

A start that does not exist in the mode the run takes enters at `beginning`, and
`entry_for(...).fell_back` is true. The mode is resolved when the run starts,
after the plan was built, and it can differ: a BDA profile with no BDA project
linked runs in Pipeline mode.

**Who reads the block.** The state machine's `CheckRunPlan` and skip Choices read
`startAt` and the `skip…` flags. The routing and apply-review handlers and the
assessment handler receive the whole execution input. The pipeline-hooks dispatcher
receives it too, and passes three of its values on to every hook Lambda as
top-level event fields: `runKind` (`kind`, or `unplanned` with no plan), `startStep`
(or `beginning`) and `reviewed` (or `false`). See the
[hooks README](../hooks/README.md#when-a-hook-fires-and-which-run-it-is-in). The web
UI's Processing Flow viewer reads `mode`, `startStep` and the `skip…` flags to show
the steps a run left out as Skipped.

## Where a run starts

A person chooses the start of a reprocess. Every other start is derived, in one
place per kind:

| Run | The start comes from |
|---|---|
| New upload, legacy review | Fixed by the kind (`models._FIXED_START`) |
| Rejected review, removed or skipped review | Fixed by the kind |
| Reprocess, restart of a failed run | The caller's `start`, checked by `eligibility.check`; `resume` is resolved against the document by `eligibility.resolve_resume` |
| Completed review | `restart.decide_restart(kind=REVIEW_COMPLETED)`, called by `ReviewQueueService.complete`, stored on the review item and read back by `FinalizeSender.plan_for` |
| Send-back | `restart.SEND_BACK_START`, forced in `ReprocessService.reprocess` |
| Process Changes (saved edits) | `restart.decide_restart(kind=SAVED_EDITS)`, called by the process changes resolver's `_run_plan_for` |

`decide_restart(document, *, kind, mode, baseline_layout=None, class_reset=False)`
returns a `RestartDecision`: `start`, the `sections` to extract again and the
`causes` by section id.

| Kind | The document | Start |
|---|---|---|
| `sent_back` | Any | `classification`; `beginning` in BDA mode |
| `review_completed` | None, or BDA mode | `apply_review_changes` |
| `review_completed` | A processed section was added, has other pages or has no result | `extraction`, for those sections |
| `review_completed` | Otherwise | `apply_review_changes` |
| `saved_edits` | A page's class was reset (`class_reset`) | `classification` |
| `saved_edits` | A processed section has no result | `extraction`, for those sections |
| `saved_edits` | Otherwise | `policy_rule_validation` |

- A **processed** section is one that is not excluded.
- `baseline_layout` is what `layout_of(document)` returned when the document was
  placed for review: section id to a 16-character digest of the section's page ids,
  the same in whatever order the pages come. A section is `added` when its id is not
  in the baseline and has `pages_changed` when its digest differs. With no baseline,
  or an empty one, only a missing result (`no_result`) sends a section to extraction.
- `layout_of` includes excluded sections and returns an empty layout for a document
  with more than `MAX_LAYOUT_SECTIONS` sections. That cap bounds what is stored on
  the review item; the comparison itself reads every processed section.
- After saved edits, sections are named only when some section keeps its result.
  When none has one, the start is a plain `extraction`.
- Any other kind raises `ValueError`, and so does `saved_edits` with no document.

The callers add what the decision cannot know. `ReviewQueueService` resolves the
mode from the review item and starts at `apply_review_changes` when it is not known
(see the [review queues README](../review_queues/README.md#completing-a-review)).
The process changes resolver checks the start against the edited document and
starts at `beginning` when the document is not ready for it. `ReprocessService`
sends a document back to `beginning` when it lacks what classification reads.

## Planned steps

`planned.py` says what a run will do before it starts. The dialog that finishes a
review, the Process Changes confirmation and the CLI's reprocess description all
read it, so they agree with each other and with the run.

```python
from idp_common.config.feature_flags import step_enabled
from idp_common.run_plan.models import RunKind
from idp_common.run_plan.planned import planned_run

run = planned_run(
    "extraction", "pipeline",
    kind=RunKind.REVIEW_COMPLETED,
    enabled=step_enabled(config),
    sections=["2", "4"], causes={"2": "pages_changed", "4": "added"},
)
run.start_step                              # "extraction"
[(s.id, s.state) for s in run.steps]        # every step of the mode, in display order
run.model_dump(by_alias=True)               # the API shape: startStep, steps, sections
```

`planned_steps(start, mode, *, kind, skip=(), enabled={}, has_sections=False)`
returns one `PlannedStep` (`id`, `label`, `state`) for every step of
`display_order(mode, review_completed=kind == REVIEW_COMPLETED)`:

| The step | State |
|---|---|
| Comes before where the run enters | `skipped` (and `before_start`: its output is kept and read) |
| Named in `skip`, or skipped by the entry of the start | `skipped` |
| `enabled` holds `False` for its id | `not_enabled`: the run reaches the step, and the configuration has it switched off |
| Otherwise | `run` |

- The list is read off `asl.entry_for(start, mode, has_sections)`, the entry the
  state machine takes, not off the logical start. A `summarization` or `evaluation`
  start enters at policy rule validation with that stage skipped, so Review Routing
  is listed as `run` for it. A test compares the list with each entry's forced skips.
- `enabled` is the answer of `feature_flags.step_enabled(config)`: the rule each
  optional step itself reads (`assessment`, `policy_rule_validation`,
  `review_routing`, `summarization`, `evaluation`). A step it does not name is
  enabled.
- A rejected review's run does no step, so every step is `skipped`.
- A start of `apply_review_changes` for a run that does not follow a completed
  review raises `RunPlanError`.

`planned_run(start, mode, *, kind, skip=(), enabled={}, sections=(), causes={})`
returns a `PlannedRun`: `mode`, `start_step` (after aliases), `steps`, and
`sections`, each a `PlannedSection` with `section_id` and `cause`. Sections are
listed only for the entry that extracts named sections; any other run extracts
every section or none. A section with no cause given is listed as `no_result`. The
models serialise with camelCase aliases, which is the shape `getReviewCompletionPlan`
and `getProcessChangesPlan` return.

`describe_start(start, mode, *, skip=())` returns a `StartDescription` of output
names: `cleared` (the start step and every later step the run does), `kept` (the
steps before the start) and `left_out` (a later step named in `skip`). It is
`planned_steps` for a `reprocess`, limited to the steps a run can start at. The
first step's output is named `OCR` in Pipeline mode and `Bedrock Data Automation
results` in BDA mode. `idp-cli reprocess` prints it for both modes before it asks
for confirmation.

## Clearing

A planned run must not find the output of an earlier run and skip a step on it. So
the code that queues a run first clears the output of the start step and of every
later step. The skip checks in the Lambdas stay, for a run with no plan.

`clear_for_start(document, start, *, sections=(), s3=None, output_bucket=None,
keep_objects=False)` clears the document object and deletes the matching objects
from S3:

| Start | Cleared |
|---|---|
| `beginning` | Pages and sections, on the document only. The caller purges the output prefix |
| `classification` | Page classes with their confidence, reasons, candidates and boundaries; the sections, and their results in S3 |
| `extraction` | Per target section: the extraction result and its object in S3, the attributes, the alerts, the alert total, the missing fields |
| `assessment` | Per target section: the alerts, the alert total, the missing fields |
| `policy_rule_validation` | The result, and every object under `<input key>/rule_validation/` |
| `summarization` | The summary |
| `evaluation` | The evaluation |

- Each row also clears every row below it.
- `sections` names the target sections of an `extraction` or `assessment` start.
  Empty means every section. The document's alert and missing-field counts are
  recomputed from the sections that keep theirs.
- It returns the names of the tracking-row attributes that hold what was cleared
  (`ROW_SECTIONS`, `ROW_POLICY_RULE_VALIDATION`, `ROW_SUMMARY`, `ROW_EVALUATION`).
  `update_document` sets attributes and never removes one, so the caller removes
  these from the row; a row that kept them would make `resume` choose a later step
  than the document needs.
- A failed delete is logged and not raised. A missing object is not an error.
- With `keep_objects=True` nothing is deleted in S3: the section results and the
  policy rule validation files stay, and the fields are cleared exactly as without
  it. The finalize sender uses it for a completed review that starts at extraction,
  because the stages write over their own outputs and the role that sends that
  message may delete only under the document's `review/` folder.
- `recount(document)` sets the document's alert and missing-field counts from its
  sections. The apply-review step calls it too.

`clear_policy_rule_validation(document, *, s3=None, output_bucket=None)` clears
only that stage: the field, and the files. Process Changes calls it.

`clear_after_review(document)` clears fields only: the policy rule validation
result, the summary and the evaluation. The run that follows a review starts after
extraction and writes over its own files.

## Eligibility

`check(request, context)` answers whether one document can be queued, and never
writes. `ReprocessRequest` holds the document key, the start, the skip list, the
profile and revision to run under, and the kind. `EligibilityContext` holds the
readers: `load_document`, `mode_for(profile, revision)`, `published_revision` and
`has_preprocessing_hook`.

The answer is an `Eligibility`: `ok`, a refusal `code` and `message`, the `start`
after aliases and `resume`, the `sections` to extract again, the `mode`, and the
document as loaded. Check a whole selection before queueing any of it.

| Code | Refused when |
|---|---|
| `MALWARE_GATE` | The status is one the malware gate refuses (`MALWARE_REFUSED_STATUSES`), the scan is still running, or the status cannot be read |
| `RUN_IN_PROGRESS` | The kind is `reprocess`, `sent_back`, `restart_failed` or `saved_edits`, and the status is in `IN_PROGRESS_STATUSES` |
| `NOT_FAILED` | The start is `resume` and the status is not `FAILED` |
| `UNKNOWN_STEP` | The start or a skip value is not in the step list, or the start is an internal one |
| `STEP_NOT_IN_MODE` | The step does not exist in the document's mode and has no alias there |
| `OUTPUT_MISSING` | The start needs the output of an earlier required step that the document lacks |
| `OTHER_PROFILE_BEGINNING_ONLY` | Another profile is asked for and the start is not `beginning` |
| `REVISION_HAS_PREPROCESSING_HOOK` | Another revision is asked for, the start is not `beginning`, and that revision has an enabled preprocessing hook |
| `REVISION_CHANGES_MODE` | As above, and that revision processes in the other mode |

- The checks run in the table's order, and the first that refuses is the answer.
- `MALWARE_GATE` applies to every kind and fails closed: a run does not go back
  through the ingest gate, so a status that cannot be read refuses.
- What a start needs: `classification` needs every page's text and image;
  `extraction` and `assessment` need a class on every page (Pipeline mode) and at
  least one section; `assessment` and every later step need the extraction result
  of every section that is not excluded.
- The profile and revision rules apply to the `reprocess` kind only. The other
  kinds keep the revision the document is pinned to. No revision named means the
  profile's published revision.

`resolve_resume(document, config, mode)` returns the first step that has no
output, and the sections to extract again:

1. `beginning`: there are no pages, or a page lacks its text or image.
2. `classification`: a page has no class, or there are no sections (Pipeline
   mode; `beginning` in BDA mode).
3. `extraction`, with the sections that have no result (Pipeline mode;
   `beginning` in BDA mode). When no section has a result, every section.
4. `assessment`: a section records a processing issue of the assessment stage
   (Pipeline mode).
5. `policy_rule_validation`: it is enabled for the configuration and the document
   has no result.
6. `summarization`: the document has no summary.
7. `evaluation`.

`reviewed_for(document, start)` is the plan's `reviewed` flag for a run a person
starts: the last review was completed and the run starts at policy rule validation
or later.

## Sending

`send_run` is the only code that puts a message on the document queue:

```python
from idp_common.run_plan.models import RunKind, RunPlan
from idp_common.run_plan.sender import EVENT_DOCUMENT_REPROCESSED, send_run

message_id = send_run(
    sqs_client, queue_url,
    document=document,
    plan=RunPlan(kind=RunKind.REPROCESS, start="summarization"),
    event_type=EVENT_DOCUMENT_REPROCESSED,
    working_bucket=working_bucket,   # optional
    prefix="reprocess",
)
```

- The message attributes are `EventType`, `ObjectKey` (the document's input key)
  and `RunPlan`. `extra_attributes` adds String attributes, such as a
  de-duplication token; it cannot replace one of the three.
- With a `working_bucket` the document is stored there and the body is a small
  reference to it (`Document.serialize_document`). Without one the body is the
  document's JSON. Give a working bucket whenever the document has pages and
  sections: a large document does not fit in a message.
- `event_type` is one of `EVENT_TYPES`: `DocumentQueued`, `DocumentReprocessed`,
  `ReviewFinalize`, `ReviewSkipResubmit`, `DocumentRunRequested`.
- The plan is encoded and the arguments are checked before anything is stored or
  sent. `send_run` raises `RunPlanError` for a plan over the size limit and for a
  plan whose start is still `resume`, and `ValueError` for an unknown event type
  or a reserved extra attribute.
- It does not clear anything. The caller clears first (see Clearing).
- `build_run_message(...)` takes the same arguments without the client and the
  queue, and returns the body and the attributes `send_run` sends. With a working
  bucket it stores the document, as `send_run` does.

**Who sends what:**

| Sender | Kind | Start | Event type | Clears |
|---|---|---|---|---|
| `queue_sender` (an upload, or one the malware gate admitted) | `new_upload` | `beginning` | `DocumentQueued` | The stale `pages/` output of a previous upload of the key |
| `ReprocessService` (Reprocess, send-back, restart of a failed run) | `reprocess`, `sent_back`, `restart_failed` | The chosen step; for a send-back `classification` (`SEND_BACK_START`), or `beginning` | `DocumentReprocessed` | See Reprocess |
| Process Changes (`process_changes_resolver`) | `saved_edits` | What `decide_restart` answers: `classification` when a page's class was reset; `extraction` for the sections with no result; otherwise `policy_rule_validation`. `beginning` when the document is not ready for that step | `DocumentRunRequested` | The results of the sections it changed and the policy rule validation result; from a `policy_rule_validation` start also the summary and the evaluation |
| Legacy review completion (`complete_section_review`) and the legacy bulk skip resubmit (`DocumentResubmitter`) | `legacy_review` | `beginning` | `DocumentRunRequested`, `ReviewSkipResubmit` | Nothing: each step's own check skips what exists |
| `FinalizeSender`, a completed review | `review_completed` | `apply_review_changes`, or `extraction` for the review item's `restart_sections` | `ReviewFinalize` | The policy rule validation result, the summary and the evaluation, on the queued document; for an extraction start also the fields of the sections it extracts again. Fields only (`keep_objects`) |
| `FinalizeSender`, a rejected review | `review_rejected` | `rejected` | `ReviewFinalize` | Nothing |
| `FinalizeSender`, a removed or skipped review | `review_skipped` | `policy_rule_validation` | `ReviewFinalize` | As a completed review |

## Reprocess

`ReprocessService` runs documents again. The reprocess resolver builds one per
request; a send-back and a restart of a failed run go through the same service
with a `ReprocessOrigin`.

```python
from idp_common.run_plan.reprocess import ReprocessOrigin, ReprocessService

service = ReprocessService(
    document_service=document_service, sqs_client=sqs, queue_url=queue_url,
    s3=s3, input_bucket=input_bucket, output_bucket=output_bucket,
    working_bucket=working_bucket,
    config_manager_factory=lambda: manager,       # or one that returns None
    review_service_factory=lambda: review_queues,  # or None
    metrics=publish,                               # publish(name, dimensions)
)
outcomes = service.reprocess(["a.pdf", "b.pdf"], start="extraction", skip=["evaluation"])
```

`reprocess(object_keys, *, start="beginning", skip=(), version=None,
revision=None, allowed_versions=None, origin=None)` returns one
`ReprocessOutcome` per document. For a send-back (`origin.kind` is `sent_back`) the
`start` argument is not read: the run starts at `SEND_BACK_START`, and a document
that `eligibility.check` refuses there with `OUTPUT_MISSING` is checked again, and
queued, from `beginning`.

| `status` | Meaning | `reason` |
|---|---|---|
| `QUEUED` | The document was queued. `start_step` is the step the run starts at | |
| `REFUSED` | The document cannot run again as asked. Nothing was changed | An eligibility code |
| `FAILED` | Storing or sending failed, or the document's file is not in the input bucket | The error's class name |

A refused or failed document does not stop the others. The service works in this
order:

1. **Scope.** With `allowed_versions`, the named profile and the profile of every
   document must be in scope, or the call raises `PermissionError`. A document
   whose profile cannot be read is out of scope.
2. **Check.** `eligibility.check` for every document.
3. **Configuration.** For every accepted document, the profile and revision the
   run is pinned to (below). A prefix mapping set to refuse a conflicting profile
   raises `ValueError`, and a mapped profile outside the caller's scope raises
   `PermissionError`. Steps 1 to 3 finish for the whole request before anything
   is queued, so a call that raises has changed nothing.
4. **Queue**, per document: build the plan; check that the file is in the input
   bucket; withdraw a live review; clear and write the tracking row; `send_run`.
   A send-back withdraws the review with a `sent_back` history entry (who sent it
   back, the start step and the note) and only for the review cycle the reviewer
   acted on: when that review has been replaced, the document is refused with
   `CYCLE_CHANGED` and nothing is changed. Any other failure to withdraw is logged
   and the run goes ahead, because the run's own routing replaces a leftover item.
   A withdrawal removes the review status from the tracking row. For a start at a
   later step the row and the run are written from the document read before the
   withdrawal, so the service drops that document's copy of the status: a run that
   is not held again ends without one.

| Start | Tracking row | Output in S3 | Message body |
|---|---|---|---|
| `beginning` | Recreated: no pages, no sections, status `QUEUED`. It keeps `submission_source` and `test_set_id`, so a Test Studio document stays one, and the caller-supplied attributes of the original intake (`Document.attributes`, #1851) with the warning that intake dropped some. When no row is left, the attributes are read from the input object by the intake rules (`document_attributes.read_object_attributes`) | Everything under `<key>/` is deleted, except `<key>/runs/` | The document's JSON |
| A later step | Kept and updated: status `QUEUED`, no start time, completion time or execution ARN. The attributes `clear_for_start` names are removed | What `clear_for_start` deletes | A reference to the document in the working bucket, when the service has one |

**The configuration a run is pinned to:**

| Run | Profile | Revision |
|---|---|---|
| Reprocess from the beginning | In this order: the named profile, as the prefix mapping for the key allows it; the document's own profile, for a caller with a configuration scope; the profile a prefix mapping assigns; otherwise none, and the queue processor pins the active profile | The named revision, when the profile is the named one or the document's own; the mapping's revision with a mapped profile |
| Reprocess from a later step | The document's own. Naming another is refused (`OTHER_PROFILE_BEGINNING_ONLY`), and prefix mappings are not consulted | The named revision, else the profile's published revision, else the pinned one |
| Send-back, restart of a failed run | The document's own | The pinned one. `version` and `revision` are ignored |

The plan the service sends has the `kind` of the origin (`reprocess`,
`sent_back`, `restart_failed`), the resolved start, the skip list, and the
sections to extract again when `resume` chose them. `reviewed` is true for a
reprocess or a restart that starts at policy rule validation or later on a
document whose review was completed. A send-back never sets it: it asks for the
review again.

`options(object_keys, *, version=None, revision=None, allowed_versions=None)`
reads only. For each document it returns `objectKey`, `mode`, `defaultStep`
(`beginning`) and `steps`: every step of the mode with `id`, `available`,
`skippable` and `reason`, the eligibility code that refuses the step.

Metrics the service publishes through `metrics`: `StaleOutputPurgeFailed` (the
output of a run from the beginning could not be deleted; the run is still
queued), and `PrefixMappingApplied` (with `Prefix`), `PrefixMappingConflict`
(with `Winner`), `PrefixMappingRejected`, `PrefixMappingUnresolvable` and
`PrefixMappingLookupFailed`, with the meanings they have at upload.

## The processing mode

`resolve_processing_mode(manager, version, revision)` returns a `ProcessingMode`:
`mode` (`pipeline` or `bda`), the BDA project ARN, the configuration at the pinned
revision, and `fell_back` (the profile asks for BDA mode and no project is
linked, so the run takes Pipeline mode). It never raises: a configuration that
cannot be read gives Pipeline mode with no configuration, and a WARNING.

## Exceptions

`RunPlanError` is a `ValueError`. `UnknownStep` and `StepNotInMode` derive from it.
`reprocess.InputObjectMissing` is a `ValueError`: the document's file is not in
the input bucket, or cannot be read.
