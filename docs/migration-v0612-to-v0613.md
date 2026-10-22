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
| `system_prompt`, `task_prompt` | `one_stage.page.system_prompt`, `one_stage.page.task_prompt` |
| `contextPagesCount` | `one_stage.page.contextPagesCount` and `two_stage.segmentation.llm.page.contextPagesCount` |
| `model`, `model_lambda_hook_arn`, `temperature`, `top_p`, `top_k`, `max_tokens`, `reasoning_effort`, `image` | `one_stage.llm.…`, and copied into `two_stage.segmentation.llm.…` and `two_stage.section_classification.llm.…` — see [Model settings are copied into every call](#model-settings-are-copied-into-every-call) |
| `enforceValidClasses`, `maxValidationRetries` | `one_stage.llm.validation.…` and `two_stage.section_classification.llm.validation.…` |
| `invalidClassFallback` | `fallback_class` |
| `confidence` | `one_stage.llm.confidence` and `two_stage.section_classification.llm.confidence` |
| `sectionSplitting` | `file_shape.sectionSplitting` |
| `maxPagesForClassification` | `file_shape.maxPagesForClassification` — see [A page limit](#a-page-limit-with-boundary-detection) |

The resulting layout is described in
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

### Modality is not written

The conversion does not set `modality.text` or `modality.images`. An unset flag
is inferred from the call's task prompt — `{DOCUMENT_TEXT}` present means text is
sent, `{DOCUMENT_IMAGE}` present means the image is — which is what v0.6.12 did.
The documented way of getting text-only classification, deleting
`{DOCUMENT_IMAGE}` from the task prompt, keeps working unchanged.

### Model settings are copied into every call

The model, its parameters and the image size are copied into all three calls —
one stage, segmentation and section classification — not only into the one stage
call your configuration uses today. Switching a converted profile to two stage
therefore starts every call on the model and parameters it already had, and you
change each one from there. A custom or fine-tuned model ARN is the one exception
at deploy time: `CustomClassificationModelARN` is written only to the call that
assigns the class, because a classification model is not trained to find
boundaries.

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

### One fallback class

`invalidClassFallback` becomes `fallback_class`, the one class assigned whenever
a class cannot be decided. Extraction reads it from there too.

## When the conversion runs

- **When a profile is read.** The runtime and the configuration editor both read
  through the conversion. The first time the runtime resolves a profile after the
  upgrade, it saves the converted, resolved configuration back to the profile.
- **When a pinned revision is read.** A revision is immutable, so it is converted
  in memory every time it is loaded and its stored body is never rewritten. A test
  run or reprocess pinned to a revision saved under v0.6.12 runs with that
  revision's settings.
- **When a configuration is written**: a save from the editor, Save As Version,
  Save As Default, `idp-cli config-upload`, and the stack's own presets and
  `CustomConfigPath` file on deploy.

The trigger is the presence of an old key, not the `config_format_version`
stamp. A file that mixes old keys with new ones — for example a v0.6.12 file
applied on top of a v0.6.13 one — has its old keys win, because they are the more
recent edit, and the conversion removes them, so running it again changes
nothing.

## What you will see in the configuration editor

The Classification panel starts with **Strategy** (*One stage* or *Two stage*)
and **Fallback class**, then shows only the calls the selected strategy makes:

- **One stage** — granularity, the page or packet settings for that granularity,
  and the model block.
- **Two stage** — **Segmentation** (method, granularity, boundary hints, page or
  packet settings, model block) and **Section classification** (method, pages per
  section, prompts, model block).
- **Advanced — known file shape**, collapsed: *Section splitting*, and *Pages to
  classify*, which appears only for *One document*.

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
