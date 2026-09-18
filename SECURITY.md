Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Security Policy

## Reporting a vulnerability

**If you have found something exploitable, do not open a GitHub issue.** Report it
privately to AWS Security through the AWS Vulnerability Disclosure Program, by
either route:

- **HackerOne:** <https://hackerone.com/aws_vdp>
- **Email:** [aws-security@amazon.com](mailto:aws-security@amazon.com)

Full details, including what to include in a report and how AWS handles it, are on
the [AWS Vulnerability Reporting page](https://aws.amazon.com/security/vulnerability-reporting/).
These are the same channels stated by the
[organization-wide security policy](https://github.com/aws-solutions-library-samples/.github/blob/HEAD/SECURITY.md)
for `aws-solutions-library-samples` and by this repository's
[`CONTRIBUTING.md`](./CONTRIBUTING.md); they are repeated here so that a reporter
who lands on this repository does not have to go looking.

Please report privately even if you are not sure the finding is exploitable. It is
straightforward for AWS Security to tell you it is not, and irreversible to
disclose a working exploit in a public issue.

Reports sent to AWS Security are handled under the AWS Vulnerability Disclosure
Program's own process, described on the pages linked above; that process, not this
repository, governs the timeline for an exploitable finding.

## What belongs in a public GitHub issue instead

Hardening suggestions and findings that are not exploitable as shipped are normal
engineering work and are welcome in the public
[issue tracker](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues),
using the bug report template. That includes:

- An IAM policy that is broader than the handler needs, where the excess is not
  reachable by an untrusted caller.
- A default that is safe but not the safest available choice.
- A missing defence in depth — an absent header, an unencrypted-at-rest resource
  where the data is already scoped, a log line that could be more redacted.
- A dependency advisory that the code does not reach, or one already triaged in
  `scripts/security/dep_audit_allowlist.json`.
- Anything you would be comfortable describing in full in public before a fix
  exists.

The dividing line is that last point. If a public description of the finding would
hand someone the means to compromise a deployed stack, or to read data they should
not be able to read, use the private channel. If in doubt, use the private channel.

## Supported versions

| Version | Supported |
|---|---|
| The most recently published release (see [CHANGELOG.md](./CHANGELOG.md)) | Yes |
| Any earlier tagged release | No |
| `develop` | Fixes land here first; not a supported deployment target |

The project maintains one line of development. Security fixes land on `develop` and
ship in the next release, which is published roughly every one to two weeks; they
are not backported to earlier tags and there is no long-term-support branch (see
[GOVERNANCE.md](./GOVERNANCE.md#branch-and-release-model)). Older releases remain
deployable — each `CHANGELOG.md` entry keeps its version-pinned template URLs — but
they do not receive fixes. If you are running an older release, the remediation for
any security issue is to update to the current one; the
[Deployment Guide](./docs/deployment.md#updating-an-existing-stack) covers in-place
stack updates, and each release's
[validation record](./docs/release-validation/README.md) states what was exercised
before it was published.

## What this project does about security, and where to look

The [`security/`](./security/README.md) directory is the auditable home for this
solution's security artifacts, and is the right starting point if you are assessing
the accelerator rather than reporting a specific bug:

- [`security/threat-modeling/`](./security/threat-modeling/) — the STRIDE threat
  model, the mitigation reports, and per-feature threat analyses with stable threat
  IDs that the test suites reference.
- [`security/test-results/`](./security/test-results/) — curated, public-safe
  snapshots of four security tests, one folder per release: the Sample Security
  Review Tool static and dependency scan, an OWASP ZAP dynamic scan of the deployed
  API, and the static and live role-based access control suites.
- [`security/README.md`](./security/README.md) — what each test covers, its pass
  criterion, and how to run it yourself.

Two of those checks run on every pull request: the static security scan (which fails
the build on any open high-severity finding) and a dependency audit against the OSV
database (which fails on high or above, with triaged exceptions justified in an
allowlist file). The role-based access control and dynamic scans need a deployed
stack and are run per release.

## Shared responsibility for your deployment

This is sample code, licensed MIT-0, that you deploy into your own AWS account. The
security of a running deployment depends on choices made at and after deploy time —
which models and regions you enable, who is in which Cognito group, whether the web
UI is public or private, your log retention and log level, your data retention, and
your account-level controls. A finding that is a consequence of a deployment choice
is a documentation or defaults question rather than a vulnerability in the code, and
is best raised as a public issue.

The guidance for hardening a deployment lives in
[Well-Architected Framework Review](./docs/well-architected.md), with related
material in [private-network deployment](./docs/deployment-private-network.md),
[GovCloud deployment](./docs/govcloud-deployment.md),
[role-based access control](./docs/rbac.md) and
[Monitoring](./docs/monitoring.md). One default worth knowing without reading
further: raising `LogLevel` above the shipped default of `WARN` can put document
contents, presigned URLs and personally identifiable information into CloudWatch
Logs.
