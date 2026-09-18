Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Roadmap

This page describes the direction of the GenAI Intelligent Document Processing
accelerator: the themes work is organised around, the priorities within them, and —
just as importantly — what the project will not take on.

**There are no dates here, and there will not be.** The project releases when a
change is ready and validated, roughly every one to two weeks, and a dated public
commitment would either be met by shipping something thin or missed. What follows
is a statement of what the maintainers care about and in what order, derived from
the open issue set, the recent `CHANGELOG.md` history and the design plans under
[`docs/planning/`](./docs/planning/). Individual issues are cited as evidence of a
theme, not as promises that each one will be closed.

See [GOVERNANCE.md](./GOVERNANCE.md) for how a change gets accepted and
[MAINTAINERS.md](./MAINTAINERS.md) for who owns which subsystem. If you want to
work on something here, open an issue first — the project is run lean and
deliberately, and a short conversation before the code is written saves both sides
a wasted week.

## Themes

### Correctness at document scale

The hardest problems in this codebase are not "does extraction work" but "does it
still work on a 1,600-row bank statement". Long lists and large tables are where
completeness quietly fails: a section too large for one inference has to be
sharded and rejoined, confidence assessment over hundreds of rows has to be batched
without losing cells, and a table split across an OCR page break has to be
recognised as one table. Work in this theme is about making the large case behave
like the small one, and about failing loudly rather than returning a plausible
short answer. Recent releases added truncation warnings, a deterministic table
parser, model-aware shard sizing and prompt-overhead-aware shard budgets, and
closed the gap where the sharded agentic path never received the pre-parsed table
guidance the single-pass path builds
([#900](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/900),
now fixed). Open work includes assessment that cannot converge on a long
multi-instance list
([#894](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/894),
[#901](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/901)),
and the redundant schema restatement that inflates every advanced-extraction
request
([#710](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/710)).

### Reliability under load

The accelerator is a queue-driven pipeline, and its failure modes at volume are
distinct from its failure modes on one document: admission control that drifts,
retry ladders that multiply a deterministic failure, and error paths that discard
a document that had already been processed successfully. The priority here is that
a saturated system degrades predictably and that no failure is invisible — an
alarm that reads OK while documents are being processed six times each is worse
than no alarm. Current items include a concurrency counter that can go negative and
permanently disable admission control
([#915](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/915),
[#916](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/916)),
deterministic Lambda timeouts retried eight times
([#917](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/917)),
an unreachable failure path that throws away a finished document
([#918](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/918)),
inert hook error handling
([#919](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/919)),
and alarms wired to a topic nobody is subscribed to
([#922](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/922)).

### Honest cost and honest confidence

Two numbers this solution reports are load-bearing for the people who deploy it:
what a document cost, and how much to trust each extracted field. Both have to be
right or they are worse than absent, because a wrong cost figure drives the wrong
model choice and a miscalibrated confidence score drives the wrong human-review
threshold. The direction is toward numbers that are measured and explainable
rather than estimated — per-phase prompt-cache accounting and per-class cost
breakdowns landed recently — and toward giving deployers control over spend rather
than only visibility into it. Open work covers pricing lookups that over- or
under-charge
([#899](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/899),
[#926](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/926)),
customer-facing budgets, cost alarms and a spend-driven circuit breaker
([#934](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/934)),
and measuring calibration properly against exact ground truth
([#935](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/935)).

### Security posture and least privilege

This is sample code that customers deploy into their own AWS accounts, so an
over-broad IAM policy or a default-on integration ships straight into production
somewhere. The standing priorities are least privilege in every execution role,
default-deny at the API boundary, defaults that are safe when nobody changes them,
and a threat model that stays current with the code. The project already gates
every pull request on a static security scan and a dependency audit, and publishes
curated per-release security results under [`security/`](./security/README.md).
Open work includes a deployment service role that can escalate to account
administrator
([#927](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/927)),
per-resolver opt-in authorization with no dispatcher-level default deny
([#928](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/928)),
caller identity taken from a request body
([#920](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/920)),
a log-redaction denylist copied into ten Lambdas and now stale
([#921](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/921)),
and a threat model that is not linked from the docs and several releases behind
([#932](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/932)).

### Measuring changes instead of asserting them

The project's own accuracy and cost claims are treated as testable. The
`benchmarks/` suite runs a configuration-by-document-size matrix against corpora
with exact ground truth, each release is A/B'd against its predecessor, and the
live-stack tiers that CI cannot run are recorded per release in
[`docs/release-validation/`](./docs/release-validation/README.md). Continuing to
invest here is itself a roadmap item: metrics with blind spots produce confident
wrong conclusions, and several past "no effect" results turned out to be artefacts
of what the metric counted. Expect continued work on the measurement layer, on
publishing configuration guidance derived from it, and on keeping the test
inventory in [`docs/testing.md`](./docs/testing.md) honest.

### Evaluation and Test Studio as the way you tune a deployment

Configuration for document processing is empirical: you cannot reason your way to
the right prompt, model and threshold set, you have to try them against labelled
documents. Test Studio, the evaluation framework and configuration profiles exist
so a deployer can do that inside the product instead of in a notebook. The
direction is toward making that loop fast and legible — editable test sets,
per-configuration accuracy curves, review-effort estimates that say which curve
they used, and comparison views that show everything the markdown report shows.
The empty Evaluation Method dropdown on `$ref`-declared object fields
([#906](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/906))
has since been fixed; test set versioning in the individual set view rather than
the multi-set view
([#903](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/903))
remains open.

### Extensibility instead of forking

The accelerator is meant to be adapted, and every adaptation that requires editing
the templates is a fork the customer then has to maintain. The extension points —
Lambda hooks at each pipeline stage, the feature platform, installable extensions,
custom MCP agents, configuration profiles — exist so that customisation survives
an upgrade. Expanding and hardening those seams is preferred over adding
configuration flags to the core pipeline, and a proposal that can be built as a
hook or an extension will be steered that way.

### Documentation that matches the code

Documentation drift is treated as a defect class with its own tests, not as
housekeeping. Guards in `scripts/tests/` fail the build when the published test
inventory, the CI gate parity or the docs sidebar goes stale, and the two-tier
rule (feature docs under `docs/`, module docs in `lib/idp_common_pkg/**/README.md`)
is enforced by review. There is a real backlog: documentation still asserting
AppSync exists after its removal
([#929](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/929)),
a `CONTRIBUTING.md` describing directories that no longer exist
([#930](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/930)),
a stale Well-Architected review
([#937](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/937)),
and figures in planning documents that can no longer be reproduced
([#938](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/938)).
Removing an unreproducible number is as welcome a contribution as adding a
feature.

### Fewer moving parts

The project actively retires things. The three separate processing patterns became
one unified pattern; AppSync was removed in favour of a REST API; the granular
assessment path is being retired once the standalone path can batch large lists on
its own ([`docs/planning/retire-granular-assessment-plan.md`](./docs/planning/retire-granular-assessment-plan.md)).
Removing a code path that two mechanisms cover is treated as progress, and
consolidation proposals are welcome — provided they come with evidence that no
customer configuration regresses, which is the hard part and the reason these are
sequenced rather than done in one change.

## Non-goals

These are as much a part of the roadmap as the themes above. They are what the
project has consistently declined, and knowing them saves you writing a pull
request that will not be merged.

- **This is not a document management system.** The accelerator processes
  documents and hands structured data onward. Long-term document storage,
  records management, retention policy, e-signature, workflow approvals and
  content collaboration are out of scope.
- **The web UI is an operator console, not the product.** It exists to configure
  the pipeline, inspect results, run evaluations and review low-confidence
  extractions. It is not a general end-user application, and it will not grow
  features that only make sense as one.
- **Issues with the underlying AWS services are not this repository's issues.**
  Bedrock model quality, Textract accuracy, Bedrock Data Automation behaviour,
  service quotas and throttling go to AWS Support, as `CONTRIBUTING.md` states.
- **No provider-abstraction layer.** Several model families are supported, and
  more are added when there is a measured reason, but the project will not build a
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
- **No governance apparatus.** No steering committee, no voting, no RFC process,
  no published response-time commitments. GOVERNANCE.md explains what exists
  instead and is explicit about what is undefined.
- **Refactors need a problem.** A large change that improves structure without
  fixing an observable defect or enabling named work is unlikely to be accepted,
  because the review cost and regression risk are real and the benefit is
  asserted.

## Where help is most welcome

The most useful contributions are the ones the maintainers cannot easily produce:
a reproducible bug report with the document class and configuration that triggers
it; a benchmark run showing that a configuration choice is better or worse than the
documented default on your corpus; a documentation correction with the evidence
that the current text is wrong; and a well-scoped fix for one of the open defects
cited above. Start with an issue, read
[GOVERNANCE.md](./GOVERNANCE.md#how-a-change-gets-accepted), and expect to be asked
for measurements if you are changing behaviour.
