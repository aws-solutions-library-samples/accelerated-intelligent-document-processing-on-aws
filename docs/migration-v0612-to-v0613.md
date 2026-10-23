---
title: "v0.6.12 → v0.6.13 Migration (Classification Settings)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# v0.6.12 → v0.6.13 Migration (Classification Settings)

v0.6.13 reorganises the `classification:` block of the configuration. In v0.6.12
almost every classification setting sat directly under `classification:`, and one
key, `classificationMethod`, chose between page-level and holistic
classification. In v0.6.13 **every setting sits inside the model call that uses
it**, and a new two-stage strategy adds calls of its own. The configuration format
moves from `0.7` to `0.9`.

**You do not need to edit anything to keep processing documents.** A stored
profile, a preset, a configuration file you upload, and a pinned revision are all
converted on read, and each keeps its behaviour. This page lists what each old
setting becomes, the cases where the conversion is more than a move, and what to
check if you maintain tooling that reads the configuration directly.

## Where each setting goes

| v0.6.12 (`classification.…`) | v0.6.13 (`classification.…`) |
|---|---|
| `classificationMethod: multimodalPageLevelClassification` | `one_stage.granularity: page` |
| `classificationMethod: textbasedHolisticClassification` | `one_stage.granularity: packet` — see [A holistic configuration](#a-holistic-configuration) |
| `granularity` (if written directly) | **both** `one_stage.granularity` and `two_stage.segmentation.llm.granularity` |
| `system_prompt`, `task_prompt` | `one_stage.page.system_prompt`, `one_stage.page.task_prompt` |
| `packet.system_prompt`, `packet.task_prompt` | `one_stage.packet.system_prompt`, `one_stage.packet.task_prompt` |
| `packet.window_pages`, `packet.overlap_pages` | **both** `one_stage.packet.…` and `two_stage.segmentation.llm.packet.…` |
| `contextPagesCount` | `one_stage.page.contextPagesCount` and `two_stage.segmentation.llm.page.contextPagesCount` |
| `model`, `model_lambda_hook_arn`, `temperature`, `top_p`, `top_k`, `max_tokens`, `reasoning_effort`, `image`, `modality` | `one_stage.llm.…`, and copied into `two_stage.segmentation.llm.…` and `two_stage.section_classification.llm.…` — see [Model settings are copied into every call](#model-settings-are-copied-into-every-call) |
| `segmentation.system_prompt`, `segmentation.task_prompt` | `two_stage.segmentation.llm.page.…` (and `segmentation.packet.…` to `…llm.packet.…`) |
| `segmentation.boundary_hints` | `two_stage.segmentation.llm.boundary_hints` |
| `segmentation.model`, `labeling.model` | the matching stage's `…llm.model`, and a non-empty value here wins over a flat `model` |
| `labeling.method`, `labeling.pages`, `labeling.k` | `two_stage.section_classification.…` — the block is named `section_classification` in v0.6.13 |
| `labeling.system_prompt`, `labeling.task_prompt` | `two_stage.section_classification.llm.…` |
| `enforceValidClasses`, `maxValidationRetries` | `one_stage.llm.validation.…` and `two_stage.section_classification.llm.validation.…` |
| `invalidClassFallback` | `fallback_class` |
| `confidence` | `one_stage.llm.confidence` and `two_stage.section_classification.llm.confidence` |
| `sectionSplitting` | `file_shape.sectionSplitting` |
| `maxPagesForClassification` | `file_shape.maxPagesForClassification` — see [A page limit](#a-page-limit-with-boundary-detection) |

Rows naming **both** destinations are what makes the conversion exact in either
strategy: those settings describe the call that reads pages, and under
`strategy: two_stage` that call is segmentation rather than one stage, so a
converted profile behaves the same whichever strategy it selects. Any other key
inside a v0.6.12 `segmentation` or `labeling` block is not carried over.

`classification.strategy` is new in v0.6.13 and has no v0.6.12 equivalent; a
converted profile gets the default, `single_pass`. The resulting layout is
described in
[How the classification settings are organised](classification.md#how-the-classification-settings-are-organised).

## Cases where the conversion is more than a move

### A holistic configuration

`textbasedHolisticClassification` becomes `one_stage.granularity: packet`. Three
things follow from it.

**The prompts move to the packet call.** Packet granularity reads
`one_stage.packet.task_prompt`, which asks for a `segments` array. In v0.6.12 no
packet-shaped default prompt shipped, so a working holistic configuration always
had a hand-written one in the flat `task_prompt`. The conversion copies that
prompt, and its `system_prompt`, into `one_stage.packet` when the task prompt is
packet-shaped — it must name both `segments` and `ordinal_start_page`, the keys
the segments parser reads. The same prompts also stay at `one_stage.page`, so the
configuration still has a page prompt if you later switch it to page granularity.

⚠️ A holistic task prompt naming neither key, or only one, was already unusable:
it asked for one class and a boundary verdict, which the segments parser cannot
read. Such a configuration is given the shipped packet prompt, which works. A
configuration that put its output schema in `system_prompt` and left
`task_prompt` minimal gets the shipped packet prompt too; check the result if that
is your shape.

**Packets over 20 pages are windowed.** Packet granularity brings
`one_stage.packet.window_pages: 20`. A holistic call had no windowing — a packet
either fitted in one call or failed — so a 30-page packet that went to the model
in one request now goes as two overlapping windows whose segments are merged, and
the boundaries returned can differ. The default is 20 because above 20 images in
one request Bedrock downscales every image to 2,000px. **To keep the single-call
shape, set `classification.one_stage.packet.window_pages: 0`**, which never
windows; a packet too large for the model then fails, as it did before.

**Page images and few-shot examples are sent.** The packet call attaches every
page's image (subject to `one_stage.llm.modality.images`) and fills
`{FEW_SHOT_EXAMPLES}`. A holistic call sent text only. If your prompt carries
`{DOCUMENT_IMAGE}`, images are now sent; set `modality.images: false` to keep a
text-only call.

### Modality follows the prompt it was inferred from

An unset `modality.text` or `modality.images` is inferred from the task prompt of
the call that reads it — `{DOCUMENT_TEXT}` present means text is sent,
`{DOCUMENT_IMAGE}` present means the image is. The documented way of getting
text-only classification, deleting `{DOCUMENT_IMAGE}` from the task prompt, keeps
working unchanged.

Two things the conversion does write:

- **A flat `modality` block is copied into all three calls**, merged flag by
  flag, alongside the model parameters below. Setting `modality.images: false`
  in the flat layout therefore turns images off for one stage, segmentation and
  section classification alike.
- **A two-stage configuration gets its stage flags written explicitly.** In
  v0.6.12 every call inferred an unset flag from the single-pass prompt; in
  v0.6.13 each call infers from its own, and the two-stage prompts are not that
  prompt. So when the configuration being converted already selects `two_stage`
  and carries a single-pass task prompt to infer from, the flags inferred from
  that prompt are written onto `two_stage.segmentation.llm.modality` and
  `two_stage.section_classification.llm.modality`, and those calls keep seeing
  exactly what they saw. Each flag is written only where it is neither set in the
  flat `modality` block nor already set on the stage, so a flag you chose wins.

### Model settings are copied into every call

The model parameters (`temperature`, `top_p`, `top_k`, `max_tokens`,
`reasoning_effort`), the image settings, the modality flags and
`model_lambda_hook_arn` are copied into all three calls — one stage, segmentation
and section classification — not only into the one stage call your configuration
uses today. Switching a converted profile to two stage therefore starts every
call on the parameters it already had, and you change each one from there.

**The `model` is the one setting treated differently, so that a stage model you
chose is kept.** A flat `model` always sets the one stage call. It reaches a
two-stage call only when that call names no model, or names the same model as the
one stage call — so a stage model you picked yourself survives the conversion. A
v0.6.12 `segmentation.model` or `labeling.model` sub-block wins outright for its
own stage. [If you read the configuration from your own
code](#if-you-read-the-configuration-from-your-own-code) states the same rule for
a flat key written onto an already-converted configuration.

A custom or fine-tuned model ARN is a separate case at deploy time:
`CustomClassificationModelARN` is written only to the call that assigns the
class, because a classification model is not trained to find boundaries.

### A page limit with boundary detection

`maxPagesForClassification` gives a whole file one class from its first N pages.
In v0.6.12, combined with boundary detection (`sectionSplitting: llm_determined`
or `page`), it collapsed a file **longer** than N pages to one section while a
file of N pages or fewer was classified and sectioned normally — one setting gave
a different answer for different files in the same run.

In v0.6.13 the page limit applies only to a one-document file. A stored
configuration combining a limit with boundary detection is converted to
`file_shape.sectionSplitting: disabled`, with a warning in the log naming both
settings, so a long file keeps the behaviour it had. ⚠️ **A short multi-document
file in such a configuration becomes one section** where v0.6.12 sectioned it.
Set `maxPagesForClassification: ALL` and `sectionSplitting: llm_determined` if
you want boundary detection on every file instead. None of the presets shipped
with the solution uses the combination.

The same rule applies to a v0.6.13 configuration you write by hand, at a
different point: the **runtime** resets `file_shape.maxPagesForClassification`
to `ALL` and logs a warning whenever `file_shape.sectionSplitting` is anything
other than `disabled`. So a page limit outside `disabled` is not an error and
does not change the conversion — it is simply ignored, and the log line is the
only notice. Pair the two settings yourself rather than relying on the
conversion to pair them.

### An empty model is the default model

Each call has its own `model`. A call whose model is empty — cleared in the
editor, or left empty in a hand-written file — uses the default classification
model, `us.amazon.nova-2-lite-v1:0`, with a warning in the log, rather than
sending no model ID. In v0.6.12 an empty classification model failed the
document.

That is the same model the shipped defaults put on all three calls, so clearing
one call's model leaves it running what the rest of the profile runs. ⚠️ It is
**not** the same as leaving the call out: there is no "off" for a model, so if
you want a cheaper or more capable model on one call, name it rather than
clearing the field.

### One fallback class

`invalidClassFallback` becomes `fallback_class`, the one class assigned whenever
a class cannot be decided. Extraction reads it from there too.

## When the conversion runs

- **When a profile is read.** The runtime and the configuration editor both read
  through the conversion, so a profile written under v0.6.12 behaves correctly
  from the first read. The runtime also tries to **store** the converted
  configuration back onto the profile, which is what takes a profile off the old
  layout permanently. That write-back is skipped in two cases, and a profile it
  skips keeps the v0.6.12 layout in storage and is converted again on every
  read: when the resolved configuration already matches what is stored, and when
  the profile's published revision was cut before revisions recorded an
  integrity hash, because rewriting the profile would destroy that revision's
  only proof. Saving the profile — a save from the editor, *Save As Version*, or
  `idp-cli config-upload` — stores it converted and ends the condition. Nothing
  is wrong with a profile in that state; it resolves correctly every time.
- **Not as part of the upgrade.** Deploying v0.6.13 rewrites the stack-managed
  `default` profile and the presets the stack ships, and does not visit profiles
  you created. So a deployment that has been through several releases normally
  stores profiles at several configuration formats at once. To see the spread on
  your own stack, read each `Config#` record and decompress it:
  `config_format_version` is stored inside the compressed configuration blob
  rather than as a top-level attribute, so it is not listable.
- **When a pinned revision is read.** A revision is immutable, so it is converted
  in memory every time it is loaded and its stored body is never rewritten. A test
  run or reprocess pinned to a revision saved under v0.6.12 runs with that
  revision's settings.
- **When a configuration is written**: a save from the editor, Save As Version,
  Save As Default, `idp-cli config-upload`, and the stack's own presets and
  `CustomConfigPath` file on deploy.

⚠️ On the deploy path the conversion also **validates**, so a hand-written
`CustomConfigPath` file is refused there rather than corrected — and on a stack
update a refusal is a rollback, so correct the file before deploying rather than
after.

What gets refused is an unrecognised value for any of the classification
settings that take a fixed set of values: `strategy`, either `granularity`,
`boundary_hints`, either `method`, `section_classification.pages`, the
class-confidence `mode`, and `file_shape.sectionSplitting`. What gets normalised
is a value that has a safe reading — a blank takes the setting's default, and an
unusable page limit becomes `ALL` — which is why
`maxPagesForClassification: banana` is reported and tolerated where
`sectionSplitting: banana` is not.

No shipped preset or configuration can carry one: the editor renders each of
these as a picklist, and every value in `config_library/` is on-list. This is
about a hand-edited file. The full list of values each setting accepts is in
[Configuration placement](classification.md#configuration-placement).

The trigger is the presence of an old key, not the `config_format_version`
stamp. A file that mixes old keys with new ones — for example a v0.6.12 file
applied on top of a v0.6.13 one — has its old keys win, because they are the more
recent edit, and the conversion removes them, so running it again changes
nothing.

## What you will see in the configuration editor

The Classification panel starts with **Strategy** (*One stage*, stored as
`strategy: single_pass`, or *Two stage*, stored as `strategy: two_stage`) and
**Fallback class**, then shows only the calls the selected strategy makes. The
form view shows the labels; the editor's JSON and YAML views, and any file you
upload, use the stored values — note the configuration *block* is named
`one_stage` while the strategy *value* is `single_pass`.

- **One stage** (`strategy: single_pass`) — granularity, the page or packet
  settings for that granularity, and the model block (`one_stage.…`).
- **Two stage** (`strategy: two_stage`) — **Segmentation**
  (`two_stage.segmentation`: method, granularity, boundary hints, page or packet
  settings, model block) and **Section classification**
  (`two_stage.section_classification`: method, *pages per section* with its `k`,
  prompts, model block).
- **Advanced — known file shape** (`file_shape`), collapsed: *Section splitting*
  (`sectionSplitting`: `llm_determined`, `disabled` or `page`) and *Pages to
  classify* (`maxPagesForClassification`), which appears only for *One document*
  — the label for `sectionSplitting: disabled`.

Each model block has its own model picker, modality, image settings and model
parameters, with reasoning effort shown only for a model that accepts it. The two
calls that assign a class add class validation and class confidence.

## If you read the configuration from your own code

Code that reads `config["classification"]["model"]`, `["task_prompt"]`,
`["granularity"]` or any other flat key finds nothing in a v0.6.13
configuration. Read the call you mean instead, for example
`config["classification"]["one_stage"]["llm"]["model"]` or
`config["classification"]["one_stage"]["page"]["task_prompt"]`. With
`idp_common`, `IDPConfig(**config).classification.class_assigning_llm()` returns
the model block that decides the class under the configured strategy, and
`idp_common.config.models.class_assigning_llm_path()` returns its path.

Code that writes flat keys keeps working, because they are converted on read —
but a flat key written onto a v0.6.13 configuration overrides the nested setting
it maps to, so prefer the nested path.

A flat setting reaches every call that uses it, **except a two-stage call that
chose its own value for it**. A flat `model` sets the one-stage call, and a
two-stage call only if that call names no model or the same model as the
one-stage call. The per-call parameters — `temperature`, `top_p`, `top_k`,
`reasoning_effort`, `max_tokens`, `image`, `modality` and
`model_lambda_hook_arn` — follow the same rule, and `image` and `modality`
follow it key by key, so a flat `image.target_width` leaves a stage's own
`target_height` alone. A pure v0.6.12 configuration has no stage parameters at
all, so every one of them takes the flat value and the conversion is exact; the
rule only has anything to decide when a flat key is written **on top of** a
v0.6.13 configuration, and there the value on the stage is one somebody chose.
`idp-cli config-validate` warns when a file still uses the flat layout.

Two names change in outputs:

- The MLflow parameters keep their names (`classification.model`,
  `classification.temperature`, `classification.modality.*`) and describe the
  call that assigns the class; a two-stage run also logs
  `classification.segmentation.model`.
- Save-time validation messages name the nested path, for example
  `classification.one_stage.llm.max_tokens (999999) exceeds model limit`.

## Rolling back to v0.6.12

⚠️ **Rolling back is safe for the stack and degrades classification on profiles
saved in v0.6.13.** v0.6.12 does not know the nested layout. A profile it reads
that was written by v0.6.13 has no classification keys v0.6.12 recognises, so
v0.6.12 runs that profile with its own built-in classification defaults —
including empty prompts, on which classification fails with `No system_prompt
found in classification configuration`. Nothing is overwritten: v0.6.12 does not
write a full profile back on read, and rolling forward to v0.6.13 restores every
setting. The stack-managed `default` profile is rebuilt from the v0.6.12 preset by
the rollback itself, so it keeps working.

**To get a profile working on v0.6.12 again, restore its last revision from
before the upgrade.** v0.6.13 converts an old profile without recording a
revision and never rewrites a revision's stored body, so for a profile carried
over from v0.6.12 the newest revision saved before the upgrade holds exactly the
settings it had, in the v0.6.12 format. After rolling back, open the profile in
the v0.6.12 editor, choose that revision in *Revision history*, and restore it.
Do this after the rollback, not before: restoring in the v0.6.13 editor writes the
profile in the new format again. Two limits:

- Edits made to the profile after the upgrade are not in that revision. Make them
  again on v0.6.12, or take them from a v0.6.13 export.
- A profile created after the upgrade has no v0.6.12 revision. Recreate it, or
  upload a v0.6.12-format file for it.

Revisions expire under the Configuration bucket's retention period
(`DataRetentionInDays`); for a revision that has expired, use the backup below.

> **Tip:** Export each custom profile before upgrading (*View/Edit Config* →
> *Export*) as a backup in the v0.6.12 format.

## Related documentation

- [Classification](classification.md) — strategies, granularity, modality and
  how to choose between them.
- [Configuration](configuration.md) — the full configuration reference.
- [v0.5 → v0.6 Migration](migration-v05-to-v06.md) — the earlier
  assessment-into-extraction consolidation, which follows the same
  automatic-and-idempotent pattern.
