---
title: "Sample: Health Insurance Review"
---
# Sample: Health Insurance Review

> **This is a sample, not the Claims Processing product.** It exists to show how
> a *use-case* extension is built on the Feature Platform. A full,
> production-grade claims solution is planned as a separate AWS Marketplace
> offering — this demo is intentionally minimal and is not it.

**Sample: Health Insurance Review** is the bundled *use-case* sample feature for
the [Feature Platform](../feature-platform.md) — the counterpart to the simpler
*feature add-on* sample,
[Sample: Document Status](sample-document-status.md). Where Document Status is a minimal
contract reference, this sample demonstrates a complete industry vertical —
health insurance claims — exercising the parts of the platform Document Status
does not: a **config preset** applied at install, a **pipeline hook**,
**host-GraphQL calls from a feature UI**, and a multi-route feature API. It
builds directly on the
accelerator's [rule validation](../rule-validation.md) capability.

## What it does

Adds a **Claims Review** page with two tabs:

1. **Claims Dashboard** — lists every document that has run through rule
   validation, with a deterministic claim status, the Pass / Fail / Information
   Not Found counts, and a drill-down showing each rule's recommendation,
   supporting pages, and reasoning (plus the full markdown summary).
2. **Rules Discovery** — upload a payer policy document (e.g. an NCCI Medicare
   Policy Manual) and the host's Rules Discovery pipeline extracts the
   validation rules into a configuration version, which the tab then renders as
   a browsable rule set.

## Claim status

The claim status is derived **deterministically** (no LLM) by the feature's
`postRuleValidation` hook from the consolidated rule-validation summary:

| Condition                                            | Status                        |
| ---------------------------------------------------- | ----------------------------- |
| Every rule recommendation is **Pass**                | **Clean claim**               |
| Any rule recommendation is **Fail**                  | **Review required**           |
| No failures, but rules are **Information Not Found**  | **Insufficient documentation** |

## How it works

It exercises the full host contract:

- **Config preset (with the hook baked in)** — the feature bundles a
  prior-authorization rule-validation configuration
  (`config-preset/claims-config.yaml`). At install, the feature's ui-deployer
  injects its `postRuleValidation` hook into the preset's
  `rule_validation.postHook` (using the hook Lambda's resolved ARN), then calls
  the host's `applyFeatureConfigPreset` mutation, which writes the preset as a
  **new, non-active** configuration version
  (`sample-health-insurance-review` — one profile, a revision per release). An admin reviews and activates
  it from the **Configuration** page — installing the feature never silently
  changes the active configuration. Because the hook lives **inside** the preset
  version, activating that version brings the rules and the hook together
  atomically.
- **Pipeline hook** — after rule validation completes for a document, the host's
  pipeline-hooks dispatcher (which reads hooks from the *active* config version)
  invokes the feature's hook Lambda, which derives the claim status and records
  it in the feature's own DynamoDB table. The hook is shipped as part of the
  config preset above rather than via a separate `registerFeatureHooks` call —
  that keeps it attached to the very version the admin activates, instead of
  being orphaned in whatever version happened to be active at install time.
- **Feature API** — an HTTP API + Lambda (Cognito-authenticated) over that table
  with routes for the claim list (filterable by status/time window), a single
  claim's detail (merged with the consolidated summary from the output bucket),
  and the markdown summary.
- **Host-GraphQL from the feature UI** — the Rules Discovery tab calls the
  host's own `uploadDiscoveryDocument`, `listDiscoveryJobs`, and
  `getConfigVersion` operations directly, using the shared signed-in Amplify
  instance the host exposes to features. (Rules Discovery requires the **Admin**
  or **Author** role; the tab shows a friendly message to read-only users.)

## Installing

Claims Review is bundled with the accelerator and listed in the catalog, but —
as a reference sample — it sets `showInNav: false`, so it has no nav entry of
its own until installed. Open **Extensions → Browse catalog** in the nav,
select **Sample: Health Insurance Review**, and install it from its feature
page with one click. Once installed it gets its own entry in the
**Extensions** nav.

After installing:

1. Open **Configuration**, find the `sample-health-insurance-review` profile, and
   **activate** it.
2. Upload the sample prior-auth packet
   `samples/rule-validation/medicare_respiratory_pa_packet.pdf` to the input
   bucket. (Its content — a Medicare respiratory prior-auth request — matches
   the NCCI policy rules the bundled preset ships with, so rules evaluate to
   real Pass/Fail results.)
3. When processing finishes, the claim appears in the **Claims Dashboard** with
   its status and per-rule breakdown.
4. In the **Rules Discovery** tab, upload
   `samples/rule-validation/NCCI Medicare Policy Manual.pdf`, watch the job run,
   and browse the discovered rules.

## Source & authoring

The full source lives in
[`feature-platform/sample-health-insurance-review/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/main/feature-platform/sample-health-insurance-review).
For the host contracts it builds on — pipeline hooks and config presets — see
the [Feature Platform Developer Guide](../feature-platform-developer-guide.md).
