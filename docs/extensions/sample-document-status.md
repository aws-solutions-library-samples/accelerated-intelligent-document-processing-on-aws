---
title: "Sample: Document Status (feature add-on)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Sample: Document Status (feature add-on)

**Sample: Document Status** is the minimal *feature add-on* sample for the
[Feature Platform](../feature-platform.md) — the simplest of the two bundled
samples (its companion, the richer *use-case* sample, is
[Sample: Health Insurance Review](sample-health-insurance-review.md)). It demonstrates the host
contract end-to-end and is the recommended starting point for authoring your
own extension.

> Its `featureId` remains `docs-by-status` (the stable install key); only the
> display name and docs slug were updated to read as a sample
> (`Sample: Document Status` / `extensions/sample-document-status`).

## What it does

Adds a **Document Status** page to the IDP web UI: a pie chart showing how many
documents are currently in each processing status — NEW, QUEUED, RUNNING,
COMPLETED, FAILED. Counts are fetched live from the main stack's document
tracking table through a small feature-owned HTTP API.

## How it works

It exercises every part of the host contract, making it a complete, working
template:

- **UI bundle** — a React UMD module rendered inside the host UI, sharing the
  host's React/Cloudscape instances via `window.IdpFeatures.register(...)`.
- **Backend API** — an HTTP API + Lambda that reads the main stack's
  `TrackingTable` (granted a scoped cross-stack read role) and returns
  per-status counts. The UI calls it with a Cognito-issued bearer token.
- **Registration** — a `RegisterFeature` custom resource registers the feature
  with the host on stack create and unregisters on delete, so it appears in the
  **Extensions** nav automatically.

## Installing

This sample is bundled with the accelerator and listed in the catalog, but —
as a reference sample — it sets `showInNav: false`, so it has no nav entry of
its own until installed. Open **Extensions → Browse catalog** in the nav and
select **Sample: Document Status (feature add-on)**; an admin installs it from
its feature page with one click — there's no subscription step for open-source
extensions. Once installed it gets its own entry in the **Extensions** nav.

## Source & authoring

The full source lives in
[`feature-platform/sample-feature/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/main/feature-platform/sample-feature).
To build your own extension starting from this pattern, see the
[Feature Platform Developer Guide](../feature-platform-developer-guide.md).
