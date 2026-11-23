---
title: "Customizing Extraction"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Customizing Extraction

> **This guide has moved.** Extraction, confidence, and geometry documentation
> is now consolidated in **[Extraction & Confidence](extraction-and-confidence.md)**.

As of config format 0.6, confidence and geometry are **outputs of extraction**, so
extraction and confidence are documented together. What moved:

- **Extraction configuration** — Simple (non-agentic) vs Advanced (agentic) modes, deterministic table parsing, schema validation/model escalation, sharding for large documents.
- **Document classes, attributes, and prompts** — per-class model and prompt overrides, few-shot examples, custom prompt generator Lambda.
- **Image placement (`{DOCUMENT_IMAGE}`), CachePoint, JSON/YAML output**, and image processing configuration.

See **[Extraction & Confidence](extraction-and-confidence.md)**.
