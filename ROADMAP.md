Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Roadmap

This page describes the direction of the GenAI Intelligent Document Processing
accelerator: the themes work is organised around, and — just as importantly — what
the project will not take on.

**There are no dates here, and there will not be.** The project releases when a
change is ready and validated, roughly every one to two weeks. What follows is a
statement of what the maintainers care about, so you can tell whether an idea fits
before you build it. For what is being worked on right now, read the
[open issues](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues),
the recent [CHANGELOG.md](./CHANGELOG.md) history and the design plans under
[`docs/planning/`](./docs/planning/).

See [GOVERNANCE.md](./GOVERNANCE.md) for how a change gets accepted and who
reviews what. If you want to work on something here, open an issue first — a short conversation before the code is
written saves both sides a wasted week.

## Themes

### Correctness at document scale

The hardest problems in this codebase are not "does extraction work" but "does it
still work on a 1,600-row bank statement". Long lists and large tables are where
completeness quietly fails: a section too large for one inference has to be sharded
and rejoined, confidence assessment over hundreds of rows has to be batched without
losing cells, and a table split across an OCR page break has to be recognised as
one table. Work in this theme is about making the large case behave like the small
one, and about failing loudly rather than returning a plausible short answer.
Recent releases added truncation warnings, a deterministic table parser,
model-aware shard sizing and prompt-overhead-aware shard budgets.

### Reliability under load

The accelerator is a queue-driven pipeline, and its failure modes at volume are
distinct from its failure modes on one document. The priority here is that a
saturated system degrades predictably and that no failure is invisible: admission
control that holds under sustained load, retry behaviour that distinguishes a
transient fault from a deterministic one, and error paths that never discard work
that had already succeeded. Observability is part of the same theme — an alarm is
only useful if it fires when something is actually wrong.

### Honest cost and honest confidence

Two numbers this solution reports are load-bearing for the people who deploy it:
what a document cost, and how much to trust each extracted field. Both have to be
right or they are worse than absent, because a wrong cost figure drives the wrong
model choice and a miscalibrated confidence score drives the wrong human-review
threshold. The direction is toward numbers that are measured and explainable rather
than estimated — per-phase prompt-cache accounting and per-class cost breakdowns
landed recently — and toward giving deployers control over spend rather than only
visibility into it.

### Security posture and least privilege

This is sample code that customers deploy into their own AWS accounts, so the
shipped defaults matter as much as the code. The standing priorities are least
privilege in every execution role, default-deny at the API boundary, defaults that
are safe when nobody changes them, and a threat model that stays current with the
code. Every pull request is checked by a static security scan and a dependency
audit, and curated per-release security test results are published under
[`security/`](./security/README.md). Hardening contributions are welcome; see
[SECURITY.md](./SECURITY.md) for what to report privately and what belongs in a
public issue.

### Measuring changes instead of asserting them

The project's own accuracy and cost claims are treated as testable. The
`benchmarks/` suite runs a configuration-by-document-size matrix against corpora
with exact ground truth, each release is A/B'd against its predecessor, and the
live-stack tiers that CI cannot run are recorded per release in
[`docs/release-validation/`](./docs/release-validation/README.md). Continuing to
invest here is itself a roadmap item: metrics with blind spots produce confident
wrong conclusions. Expect continued work on the measurement layer, on publishing
configuration guidance derived from it, and on keeping the test inventory in
[`docs/testing.md`](./docs/testing.md) honest.

### Evaluation and Test Studio as the way you tune a deployment

Configuration for document processing is empirical: you cannot reason your way to
the right prompt, model and threshold set, you have to try them against labelled
documents. Test Studio, the evaluation framework and configuration profiles exist
so a deployer can do that inside the product instead of in a notebook. The
direction is toward making that loop fast and legible — editable test sets,
per-configuration accuracy curves, review-effort estimates that say which curve
they used, and comparison views that show everything the markdown report shows.

### Extensibility instead of forking

The accelerator is meant to be adapted, and every adaptation that requires editing
the templates is a fork the customer then has to maintain. The extension points —
Lambda hooks at each pipeline stage, the feature platform, installable extensions,
custom MCP agents, configuration profiles — exist so that customisation survives an
upgrade. Expanding and hardening those seams is preferred over adding configuration
flags to the core pipeline, and a proposal that can be built as a hook or an
extension will be steered that way.

### Documentation that matches the code

Documentation drift is treated as a defect class with its own tests, not as
housekeeping. Guards in `scripts/tests/` fail the build when the published test
inventory, the CI check parity or the docs sidebar goes stale, and the two-tier rule
(feature docs under `docs/`, module docs in `lib/idp_common_pkg/**/README.md`) is
enforced by review. A documentation correction with the evidence that the current
text is wrong is as welcome a contribution as a feature.

### Fewer moving parts

The project actively retires things. The three separate processing patterns became
one unified pattern, and AppSync was removed in favour of a REST API. Removing a
code path that two mechanisms cover is treated as progress, and consolidation
proposals are welcome — provided they come with evidence that no customer
configuration regresses, which is the hard part and the reason these are sequenced
rather than done in one change.

## Non-goals

These are as much a part of the roadmap as the themes above. They are what the
project has consistently declined, and knowing them saves you writing a pull
request that will not be merged.

- **This is not a document management system.** The accelerator processes documents
  and hands structured data onward. Long-term document storage, records management,
  retention policy, e-signature, workflow approvals and content collaboration are
  out of scope.
- **The web UI is an operator console, not the product.** It exists to configure
  the pipeline, inspect results, run evaluations and review low-confidence
  extractions. It is not a general end-user application, and it will not grow
  features that only make sense as one.
- **Issues with the underlying AWS services are not this repository's issues.**
  Bedrock model quality, Textract accuracy, Bedrock Data Automation behaviour,
  service quotas and throttling go to AWS Support, as `CONTRIBUTING.md` states.
- **No provider-abstraction layer.** Several model families are supported, and more
  are added when there is a measured reason, but the project will not build a
  generic gateway that pretends every model behaves the same. The differences —
  caching minimums, reasoning parameters, tool-use reliability, per-region
  availability, output limits — are exactly what the configuration has to express.
- **New models are added on evidence, not on release day.** Adding a selectable
  model touches pricing, output limits, region lists, client routing, IAM, the UI
  and both documentation tiers, and the point of doing it is that someone measured
  it to be better or cheaper for a real workload.
- **No competition with the CDK and Terraform siblings.** This repository stays
  CloudFormation and SAM. If you want CDK or Terraform, use
  [cdklabs/genai-idp](https://github.com/cdklabs/genai-idp) or
  [awslabs/genai-idp-terraform](https://github.com/awslabs/genai-idp-terraform).
- **No long-term-support line and no backports.** Fixes land on `develop` and ship
  in the next release. Older tags stay deployable but are not maintained.
- **No stability guarantee for internal APIs before 1.0.** `idp_common` is the
  library that powers the accelerator, not a versioned public SDK contract. Its
  signatures change between minor releases, and `CHANGELOG.md` is where you find
  out.
- **Refactors need a problem.** A large change that improves structure without
  fixing an observable defect or enabling named work is unlikely to be accepted,
  because the review cost and regression risk are real and the benefit is asserted.

## Where help is most welcome

The most useful contributions are the ones the maintainers cannot easily produce: a
reproducible bug report with the document class and configuration that triggers it;
a benchmark run showing that a configuration choice is better or worse than the
documented default on your corpus; a documentation correction with the evidence
that the current text is wrong; and a well-scoped fix for an open issue. Start with
an issue, read
[GOVERNANCE.md](./GOVERNANCE.md#how-a-change-gets-accepted), and expect to be asked
for measurements if you are changing behaviour.
