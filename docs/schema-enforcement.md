---
title: "Schema Enforcement"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Schema Enforcement

By default, Simple extraction describes your document schema in the prompt and then
parses whatever the model returns. Schema enforcement makes the reply itself match
the schema. It is experimental and off by default.

## Choose a mode

Set **Configuration → Extraction → Schema Enforcement → Mode**, or
`extraction.schema_enforcement.mode`:

| Mode | What it does |
|---|---|
| **Off** (default) | The schema is described in the prompt only. |
| **Structured outputs** | Bedrock [structured outputs](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html): the reply is guaranteed to be valid JSON that matches the schema, and is read without JSON repair. Claude models only. |
| **Forced tool call** | The schema is sent as a tool the model must call. Best effort: the model can still answer in prose, which is then parsed as usual. |
| **Auto** | Structured outputs if the model supports it, otherwise a forced tool call, otherwise the prompt. |

Either mechanism constrains the *shape* of the reply, not the accuracy of the
values. Measure accuracy and completeness on your own documents before turning it
on.

## Supported models

| Extraction model | Structured outputs | Forced tool call |
|---|---|---|
| Claude Haiku 4.5, Haiku 5.5, Sonnet 4.5, Sonnet 4.6, Sonnet 5, Opus 4.5, Opus 4.6, Opus 4.7, Opus 4.8, Opus 5 | ✅ | ✅ |
| Claude Sonnet 5.5, Opus 5.5 | ✅ | ❌ |
| Amazon Nova, xAI Grok, OpenAI GPT-6 Astra | ❌ | ✅ |
| OpenAI GPT-5.x, a custom Lambda hook | ❌ | ❌ |

A per-class model override is checked the same way. With a model the selected mode
does not support, the section is extracted with the prompt as usual.

## What to expect

- **It never fails a document.** Anything that cannot apply falls back to the
  prompt, and the reason is recorded in the section's metadata.
- **Large schemas fall back.** Structured outputs allows up to 28 optional fields:
  the first 16 are sent as required-but-nullable, so a missing field comes back as
  `null`, and up to 12 more stay optional. Constraints it does not support, such as
  `minimum` or `maxLength`, are left out of the request; schema validation still
  checks them afterwards.
- **The first document of a class is slower.** Bedrock compiles a schema the first
  time it sees it, which can add up to about 90 seconds; it reuses the result for 24
  hours.
- **Simple extraction only.** Advanced (agentic) extraction already uses a tool
  schema.

## Check what a section used

Each section's result records the mode and the mechanism actually sent, which is
the field to read under **Auto**:

```json
"metadata": {
  "schema_enforcement": { "mode": "auto", "used": "structured_output" },
  "structured_output": { "applied": true, "parsed": true, "stop_reason": "end_turn" }
}
```

`used` is `structured_output`, `forced_tool` or `prompt`. A `skipped` or `rejected`
entry in `structured_output` or `forced_tool` gives the reason a mechanism did not
apply.

## Upgrading

This setting replaces `extraction.forced_tool`. Saved configurations are converted
automatically: `forced_tool.enabled: true` becomes **Forced tool call**. Files and
scripts that set `extraction.forced_tool` keep working, but should switch to
`extraction.schema_enforcement.mode`.

## Learn more

- [Amazon Bedrock: Get validated JSON results from models](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html)
- [Anthropic: Structured outputs](https://docs.claude.com/en/docs/build-with-claude/structured-outputs)
- [Extraction module README](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/lib/idp_common_pkg/idp_common/extraction/README.md#schema-enforcement-simple-mode-extractionschema_enforcement) — implementation details
