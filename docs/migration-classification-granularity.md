---
title: "Classification Method → Granularity + Modality (config format 0.8)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Classification Method → Granularity + Modality (config format 0.8)

`classification.classificationMethod` is gone. `classification.granularity` and
`classification.modality` replace it, and a stored configuration carrying the old
key is converted automatically on every read.

**You do not need to edit anything to keep processing documents.** This page is
here so you can see what the conversion does, and so the three cases where it is
not a pure rename are written down rather than discovered. One of those — a
holistic configuration becoming windowed — is a behaviour change you may want to
turn off; the other two are deliberate choices that change nothing.

## What changed

The old key had two values, and each one bundled two choices that are
independent of each other — **how many calls are made** and **what each call
sees**:

| | Text only | Text + images | Images only |
|---|---|---|---|
| **Per page** | prompt editing only | `multimodalPageLevelClassification` | prompt editing only |
| **Whole packet** | `textbasedHolisticClassification` | *not available* | *not available* |

Four of the six combinations had no setting at all, and the two that did could
only be reached by deleting a placeholder from the task prompt. Two independent
settings cover all six:

```yaml
classification:
  granularity: page      # or packet — how many calls
  modality:
    text: ""             # or true/false — what each call sees
    images: ""           # empty means "infer from the task prompt"
```

## What the conversion does

| Stored | Becomes |
|---|---|
| `classificationMethod: multimodalPageLevelClassification` | `granularity: page` |
| `classificationMethod: textbasedHolisticClassification` | `granularity: packet`, **and the flat prompts are copied to `classification.packet`** (see below) |
| An unrecognised value | `granularity: page`, with a warning — the same thing the service did with it |
| `granularity` already set to `page` or `packet` | Left alone. An explicit setting always wins over the migrated one |
| `granularity: ""` | Converted. An empty value means "derive it from `classificationMethod`", which is what the conversion does, so it is not an explicit setting |

⚠️ **Upgrading from a release, your configuration carries `classificationMethod`
and no `granularity` at all**, because `granularity` is new in this release — so
the first row or the second is the one that applies to you. The last two rows
matter only if you have already set `granularity` by hand, or are coming from a
`develop` build that had the setting.

The value is matched case- and whitespace-insensitively, since a hand-written
config is where this key most often came from.

### ⚠️ Modality is deliberately *not* written

The conversion does **not** set `modality.text` or `modality.images`, even though
`multimodalPageLevelClassification`'s name says "multimodal". That is on purpose.

An unset flag is **inferred from the task prompt's placeholders** —
`{DOCUMENT_TEXT}` present means text is sent, `{DOCUMENT_IMAGE}` present means
the image is — which is exactly what the service already did. So leaving them
unset is the only choice that changes nothing.

Writing `images: true` to match the method's name would be a real behaviour
change for anyone who used the documented workaround of **deleting
`{DOCUMENT_IMAGE}` to get text-only classification**: their prompt would start
receiving images again, and their cost would rise. If you used that workaround,
it keeps working untouched — and you can now say the same thing explicitly with
`modality.images: false` if you prefer the setting to the prompt edit.

### ⚠️ A migrated holistic config is windowed at 20 pages

`granularity: packet` brings `packet.window_pages: 20` with it, and
`textbasedHolisticClassification` had no windowing at all: a packet either fitted
in one call or failed. So a 30-page packet that previously went to the model in
one request now goes as two overlapping windows whose segments are merged, and
the boundaries it returns can differ.

That is the trade the default makes deliberately — above 20 image blocks in one
request Bedrock downscales every image to 2,000px, and a packet larger than the
context window used to fail outright rather than being split. But it *is* a
change, and a text-only configuration (which every holistic one was) gets the
split without the image-resolution reason for it. **To keep the old call shape
exactly, set `classification.packet.window_pages: 0`**, which means never window;
a packet too large for the model then fails, as it did before.

### ⚠️ A holistic config's prompts are copied into `classification.packet`

This part of the conversion is not a rename either, and it exists because of a
quirk of how the old setting worked.

`task_prompt` was a **single key shared by both methods**, and no packet-shaped
default prompt has ever shipped in this product — not in the system defaults and
not in any preset. So the only way `textbasedHolisticClassification` ever
produced a working result was a **hand-written, packet-shaped `task_prompt`**,
asking for a `segments` array.

Packet granularity reads its prompt from `classification.packet.task_prompt`. So
setting `granularity: packet` alone would have the service use *our* shipped
packet prompt and quietly ignore yours — on the one path where the output shape
is what the parser depends on. The conversion therefore **copies** your flat
`system_prompt` and `task_prompt` into `classification.packet`:

**Stored (pre-0.8):**

```yaml
classification:
  classificationMethod: textbasedHolisticClassification
  system_prompt: You split packets.
  task_prompt: |
    ... return segments with ordinal_start_page ...
```

**After conversion:**

```yaml
classification:
  granularity: packet
  packet:
    system_prompt: You split packets.          # copied
    task_prompt: |                             # copied
      ... return segments with ordinal_start_page ...
  system_prompt: You split packets.            # still here
  task_prompt: |                               # still here
    ... return segments with ordinal_start_page ...
```

Two details:

- **Copied, not moved.** The flat prompt stays where it is, because two-stage
  Stage B falls back to it and a config later switched back to `page` would
  otherwise have no page prompt at all.
- **Only a packet-shaped prompt is copied**, and the test is applied to the
  **task** prompt: it must name both keys the segments parser reads, `segments`
  **and** `ordinal_start_page`. The system prompt moves with it or not at all —
  the two were written to describe one output shape, and half a hand-written pair
  is worse than neither. ⚠️ So a holistic configuration that put its output
  schema in `system_prompt` and left `task_prompt` minimal gets the shipped
  packet prompt instead; check the result if that is your shape. A task prompt
  naming neither key, or only one, is the page-level default, which means that
  configuration was
  *already broken* under holistic — the page prompt asks for one class plus a
  boundary verdict, which the segments parser cannot read. Such a config gets
  the shipped packet prompt instead, which actually works. If you want the old
  (non-functioning) behaviour back, set `granularity: page`.

## When the conversion runs

At every configuration choke point, the same places the earlier migrations run:

- when a configuration is **read** and validated (`IDPConfig` validation),
- when a stored custom delta is **merged** onto the current defaults,
- when a configuration is **written** (normal update, Save As Version, Save As
  Default).

It short-circuits once a config is stamped `config_format_version: "0.8"` — but
the *trigger* is the presence of the legacy key, not the stamp. That matters
because the merge path can produce a config stamped with the current version
(inherited from the defaults) while still carrying the legacy key; keying on the
stamp alone would skip it.

⚠️ **For a sparse custom override, what preserves your setting is that the delta
is migrated *before* it is merged**, not the trigger. Once a legacy delta has
been deep-merged onto the defaults, the defaults' own `granularity: page` has
arrived as a value, and the conversion's "an explicit setting wins" rule keeps
it — so a holistic delta merged first and migrated second runs at page
granularity. `merge_config_with_defaults` migrates at the top for exactly this
reason, every live path goes through it, and the merge-first helper that did not
(`ConfigurationReader.simple_merge`) was removed rather than left for a future
caller to reach for.

The stamp is never rewritten **backwards**. If an older release reads a
configuration a newer one wrote, it leaves the newer stamp alone, so a later
roll-forward still applies the migration that config needs.

> **Tip:** Export your current configuration before upgrading (View/Edit Config
> → Export) as a safety backup.

## What you will see in the UI

The *Classification method* dropdown is gone. In its place, at the top of the
Classification panel:

- **Granularity** — `page` or `packet`.
- **Modality** — *Send page text* and *Send page images*, each with an empty
  option meaning "infer from the prompt".
- **Strategy** — `single_pass` or `two_stage`.

Settings that do not apply to your combination are hidden: the **Packet
settings** group appears only at packet granularity, the **Image** settings only
when images are being sent, and the two-stage **Stage A** / **Stage B** groups
only under `two_stage`.

One setting is deliberately **not** hidden even though it can be overridden:
*Pages to classify*. When it holds a number it overrides *Section splitting*, and
an option that is invisible while still in force is worse than one shown while
inert — so it stays visible and the interaction is stated in its help text.

## Related documentation

- [Classification](classification.md) — granularity, modality, two-stage, and
  how to choose between them.
- [Configuration](configuration.md) — the full configuration reference.
- [v0.5 → v0.6 Migration](migration-v05-to-v06.md) — the earlier
  assessment-into-extraction consolidation, which follows the same
  automatic-and-idempotent pattern.
