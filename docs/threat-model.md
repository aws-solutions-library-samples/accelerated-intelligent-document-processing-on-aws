---
title: "Threat Model"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Threat Model

This solution ships with a STRIDE threat model: **98 threats** across the
architecture, the processing pipeline, the web UI and API, the agent and chat
features, the extensibility points, and the analytics stack, each with a risk
score, the controls that address it, and — where a control does not fully cover
the threat — the residual risk stated plainly.

It lives in the repository under
[`security/threat-modeling/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/develop/security/threat-modeling)
rather than on this documentation site, because it is a 24-document corpus that is
read alongside the templates and code. This page is the entry point to it: what it
covers, how to read a threat entry, and how the model is kept from going stale.

**Start here:**
[`security/threat-modeling/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/security/threat-modeling/README.md).

## Why read it

If you are deploying this solution into an account you are accountable for, the
threat model answers three questions that the feature documentation does not:

**Where are the trust boundaries?** The
[system overview](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/security/threat-modeling/architecture/system-overview.md)
and
[data flows](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/security/threat-modeling/architecture/data-flows.md)
name each boundary and what crosses it. Two of them are easy to miss. The API
edge is a single API Gateway REST route, `POST /op/{field}`, behind a Cognito
authorizer that **authenticates only** — every group and scope decision is made in
the code behind a dispatcher Lambda, not by a gateway rule, which is why this
repository gates authorization with an automated harness rather than a template
review. And the extension points — Lambda pipeline hooks, and Feature Platform
extensions that contribute both backend resources and UI code — are a trust
boundary you own: code you install there runs with real privilege inside the
deployment.

**Which controls are actually in place?** The
[implementation guide](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/security/threat-modeling/deliverables/implementation-guide.md)
lists controls verified against the shipped templates: the stack's KMS
customer-managed key and which resources are bound to it, the bucket policies that
deny non-TLS requests, the Cognito group model, the concurrency admission control
that bounds runaway processing and spend, and the authorization test harness.

**What is not covered, and what is still open?** This is the part worth reading
twice. The
[risk register](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/security/threat-modeling/risk-assessment/risk-matrix.md)
records every threat's status, including the ones with no effective control today
and the surfaces the automated tests do not reach. `security/README.md`'s
["Known coverage gaps"](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/security/README.md)
table exists so that a green CI run is not mistaken for full coverage.

## How to read a threat entry

Each threat has a stable identifier (`AUTH.T14`, `CHAT.T03`, `SELL.T02`), a STRIDE
category, a likelihood × severity risk score, and a status. The status is the field
to read carefully:

| Status | What it means |
|---|---|
| **Mitigated** | Controls are implemented in the shipped code or templates and were verified |
| **Partially Mitigated** | Some controls are present; the entry names what they do not cover |
| **Open** | A real gap with no effective control today |
| **Accepted** | The risk is knowingly accepted, with the rationale recorded |

Two conventions keep the model honest and are worth knowing before you rely on it.

A mitigation that depends on a change which has not merged is written as
**pending**, with its issue number, and the threat's status is **not** upgraded on
the strength of it. A threat model that counts intentions as controls is more
dangerous than one that is merely out of date, because it reads as assurance.

And the corpus does not claim uniform freshness. Each document records the release
it was last verified against, and the model's README names which documents were
re-derived from source in the most recent review and which were carried forward.
A blanket version bump across every document would assert a review that did not
happen.

## Staying current

A threat model describes a system that keeps moving, so this one has an expiry
mechanism rather than a good intention. The model records a machine-readable
`Last reviewed against version` field, and a gate fails the build when that value
falls more than one release behind the repository's `VERSION`:

```bash
make check-threat-model-currency
```

It runs from `make lint-cicd`, so both CI systems execute it on every pull
request, and it also verifies that the generated Threat Composer export still
rebuilds byte-identical from the Markdown corpus.

One release is the threshold for a specific reason. Zero would fail the build the
moment `VERSION` is bumped to the next development version — which happens at the
*start* of a cycle, before there is anything to review. Two or more is how this
model reached roughly six releases behind the architecture it described, including
a period when it still documented an AWS AppSync GraphQL API that had been replaced
by API Gateway. One release means the gate fires once per release cycle, at a point
where there is real change to look at.

When it fires, the fix is a review: re-derive the architecture documents from the
templates and state machine, read the `CHANGELOG.md` entries since the recorded
version for new entry points and for controls that were *removed* — a removed
control that a threat still credits is the most damaging kind of staleness — update
the affected entries, regenerate the export, and only then bump the field. The
gate's own failure message says this, because editing the field alone would clear
the gate while converting a stale-documentation signal into a silent false
assurance.

## Related documentation

- [Security tests and results](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/security/README.md)
  — the four security tests (SAST/SCA, DAST, static and dynamic authorization),
  what each covers, and the curated per-release result snapshots
- [Role-Based Access Control (RBAC)](./rbac.md) — the group model and per-operation
  authorization as a user-facing feature
- [External Identity Provider](./external-idp.md) — federated sign-in and how
  groups are assigned from provider claims
- [AWS Services & IAM Roles](./aws-services-and-roles.md) — the services in use and
  the roles the solution creates
- [Well-Architected Framework Assessment](./well-architected.md) — the security
  pillar review alongside the other five
- [Testing](./testing.md) — every test method in the repository, including which
  security gates run automatically and which are manual
