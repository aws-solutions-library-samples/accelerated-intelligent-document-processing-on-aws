# Solution Profile, version 1

A solution profile is a versioned, machine-readable description of one deployable solution, such as the GenAI IDP Accelerator or the Live Meeting Assistant. It tells an AI assistant, or a person, what the solution does, where it can be extended, where it stops, what to ask a customer to judge fit, and how a delivery usually starts.

The solution's own repository publishes the profile. It is built from the source at a release tag, so it describes exactly that release. A consumer pins a profile version, as it pins any other dependency, and moves to a newer one through a reviewed change.

This page defines the contract. [`solution-profile.schema.json`](solution-profile.schema.json) holds the exact rules, as JSON Schema 2020-12.

## Design rules

1. **Evidence, not claims.** Every capability, extension point and limitation names the files or docs that show it (`evidence`). A validator checks that each repository path exists at the release commit.
2. **Built from a tag.** The profile describes one release. `version` equals the solution's own version, and `source.commit` is the release commit.
3. **Reviewed for each release.** People write the curated files (capabilities, limitations and the rest). `reviewed_for_version` records the release that a person last checked them against. A release build fails when it does not equal `version`, so a frequent release cadence cannot quietly leave the profile behind.
4. **Generic.** A profile names no consumer, no customer and no internal programme. It is published with the solution, under the solution's licence.
5. **Customer-safe.** Everything in a profile may be shown to a customer. Discovery questions are written to be asked as they stand.
6. **Stable ids.** An id, once published, keeps its meaning. A retired item stays in the file with `status: retired` for at least one release. Consumers store ids, for example a fit assessment that cites `capability:advanced-extraction`.

## Layout

A profile is a directory, `solution-profile/` at the repository root:

```
solution-profile/
  solution-profile.yaml     the manifest (required)
  capabilities.yaml         what the solution does (required)
  extension-points.yaml     where and how it can be changed (required)
  limitations.yaml          where it stops (required)
  discovery-questions.yaml  what to ask to judge fit (required)
  sizing.yaml               the inputs that size a deployment (optional)
  deployment.yaml           how it is deployed, where and what it needs (required)
  delivery.yaml             how a delivery usually starts: day 1, the first week, typical work (optional)
  changes.yaml              what each release changed, and what it requires users to act on (required)
  skills/<name>/SKILL.md    optional Agent Skills that use the profile
```

The release asset is `solution-profile-<id>-<version>.tar.gz`, with one top-level directory `solution-profile/`, plus `solution-profile-<id>-<version>.tar.gz.sha256`. The tarball also holds `build.json` (`{"id", "version", "tag", "commit", "schema_version"}`), with no timestamps, so the same commit always builds the same bytes.

## The manifest: `solution-profile.yaml`

```yaml
schema_version: 1
id: genai-idp-accelerator            # [a-z0-9-], stable forever
name: GenAI Intelligent Document Processing Accelerator
version: 0.6.15                      # equals the solution's VERSION
reviewed_for_version: 0.6.15         # the release a person last checked the curated files against
source:
  repository: https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws
  tag: v0.6.15                       # stamped by the build
  commit: 0123456789abcdef0123456789abcdef01234567   # stamped by the build
summary: >-
  One paragraph: what problem it solves, for whom, and how.
problem_domains: [document-processing, data-extraction]
signals:                             # how an assistant recognises a possible need
  keywords: [invoice, claim form, OCR, extraction, classification, bank statement]
  phrases: ["we key in data from PDFs by hand", "documents arrive by email and someone reads them"]
  anti_signals: ["real-time video", "document management system"]   # suggest it is not this solution
licence: MIT-0
support:
  model: community-sample            # community-sample | aws-supported | partner-supported | customer-owned
  statement: >-
    A sample from the AWS Solutions Library. The customer deploys, owns and operates it.
docs_url: https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/
files:
  capabilities: capabilities.yaml
  extension_points: extension-points.yaml
  limitations: limitations.yaml
  discovery_questions: discovery-questions.yaml
  sizing: sizing.yaml
  deployment: deployment.yaml
  delivery: delivery.yaml
  changes: changes.yaml
skills: [skills/idp-solution-fit]
```

## Common item fields

Every item in the list files has:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | `[a-z0-9][a-z0-9-]{1,63}`, unique within its file. Cite as `<kind>:<id>`, for example `limitation:simple-extraction-row-limit` |
| `title` | yes | A few words |
| `description` | yes | Plain prose, two to five sentences. What it is and why it matters, not how it is coded |
| `evidence` | yes, at least one | Repository paths relative to the root at `source.commit` (`docs/extraction-scaling-guide.md`, optionally `#anchor`), or `https://` URLs |
| `status` | no | `ga` (default), `preview`, `experimental`, `deprecated` or `retired` |
| `since` | no | The first version that has it |
| `tags` | no | Free keywords for search |

## `capabilities.yaml`

```yaml
capabilities:
  - id: advanced-extraction
    title: Agentic extraction for long tables
    description: ...
    evidence: [docs/extraction-scaling-guide.md]
    status: ga
    modes: [pipeline]               # optional: the solution modes it works in
    configuration: [extraction.mode] # optional: the config keys that turn it on
    related: [limitation:simple-extraction-row-limit, extension:custom-extraction-prompt-lambda]
```

## `extension-points.yaml`

Each item is one way to change the solution's behaviour without changing anything else. `category` uses the **gap taxonomy**, in order of preference:

| `category` | Meaning | Who owns the change |
|---|---|---|
| `configuration` | Settings, profiles, prompts and schemas the solution reads. No code | The deployment's operator |
| `hook` | Customer-owned code at a documented hook point, for example a Lambda hook. The solution calls it | The customer |
| `extension` | An installable add-on on the solution's extension framework, deployed beside it | The customer or a partner |
| `core-enhancement` | A change to the solution itself, contributed upstream. It ships when the solution's maintainers release it | The solution's maintainers |
| `fork` | A private copy of the solution with code changes. Least preferred: every upgrade then has to be merged by hand | The customer, permanently |
| `other` | Work outside the solution: `other_kind` says which | Varies |

`other_kind` is one of `aws-service` (quotas, model availability per Region), `integration` (systems upstream or downstream of the solution), `data` (samples, ground truth, labelling), `process` (people, review and operating procedures), `security-review` or `infrastructure` (accounts, networks, identity).

A consumer that finds a customer need the solution cannot meet even with a fork gives the verdict `not-a-fit`. That verdict is not an extension-point category, so it never appears in this file.

```yaml
extension_points:
  - id: pipeline-hook-post-extraction
    title: Post-extraction pipeline hook
    category: hook
    description: ...
    evidence: [docs/feature-platform.md#pipeline-hooks]
    when_to_use: Normalise or enrich extracted values, or stop a document that fails a business rule.
    constraints: [Not available in BDA mode, Synchronous, counts against the step's timeout]
    effort: M                        # S (up to 2 days), M (up to 2 weeks), L (more than 2 weeks), for a first working version
    upgrade_risk: low                # low | medium | high: how likely a new release breaks it
    modes: [pipeline]
```

## `limitations.yaml`

```yaml
limitations:
  - id: simple-extraction-row-limit
    title: Simple extraction truncates long tables
    description: Simple mode drops rows somewhere between about 400 and 800 rows and still reports success.
    evidence: [docs/extraction-scaling-guide.md]
    impact: high                     # low | medium | high: the effect on a deployment that meets it
    detect_with: [question:max-table-rows]
    mitigations: [capability:advanced-extraction]   # capability:, extension: or none
```

## `discovery-questions.yaml`

Questions that test fit, each written to be asked to a customer as it stands.

```yaml
questions:
  - id: max-table-rows
    topic: Documents                 # groups questions in a session
    text: What is the largest number of rows a single table in one of your documents can have?
    intent: Long tables need a different extraction mode, which changes cost and latency.
    answer_type: number              # text | number | boolean | enum | list | range
    unit: rows
    options: []                      # for enum
    priority: must                   # must | should | could
    phase: pre-contract              # pre-contract | discovery | build
    tests: [limitation:simple-extraction-row-limit, capability:advanced-extraction]
    implications:                    # what an answer means; read by people and assistants, not executed
      - when: more than about 400 rows
        then: Use advanced extraction; plan an accuracy check on the longest tables.
        refs: [capability:advanced-extraction]
    follow_ups: [tables-span-pages]
```

## `sizing.yaml`

The inputs a deployment is sized from, and where the solution's own estimator is. A profile does not hold formulas: estimates come from the solution's tools.

```yaml
inputs:
  - id: pages-per-month
    title: Pages per month
    unit: pages/month
    question: question:monthly-volume
    used_for: [cost, quotas]
estimators:
  - id: capacity-planner
    title: Capacity Planning page in the web UI
    evidence: [docs/capacity-planning.md]
```

## `deployment.yaml`

```yaml
options:
  - id: cloudformation
    title: CloudFormation (SAM) stack
    description: ...
    evidence: [README.md#deployment]
    status: ga
    regions: [us-east-1, us-west-2, eu-central-1, ca-central-1]
    time_to_deploy: about 30 minutes
prerequisites:
  - id: bedrock-model-access
    title: Bedrock model access in the deployment Region
    description: ...
    evidence: [docs/deployment.md]
```

## `delivery.yaml`

What a typical delivery does first, for a consumer that builds a project plan. Durations are typical values for planning, not commitments.

```yaml
day_one:
  - id: deploy-nearest-preset
    title: Deploy the stack with the configuration preset nearest the customer's documents
    evidence: [config_library/README.md]
walking_skeleton:
  description: One document class from upload to result in the customer's own account.
  steps: [deploy-nearest-preset, run-samples]
typical_work:
  - id: accuracy-baseline
    title: Measure accuracy against ground truth for the hardest document class
    category: configuration          # the gap taxonomy above
    effort: M
    depends_on: [deploy-nearest-preset]
    prerequisites: [Ground-truth labels for at least 20 documents of the class]
    evidence: [docs/evaluation.md]
```

## `changes.yaml`

One entry for each release, newest first. A release build fails when there is no entry for `version`. A consumer compares two pinned versions with it, and tells a person what changed for the capabilities an engagement relies on.

```yaml
releases:
  - version: 0.6.15
    date: 2026-11-13
    summary: Terraform and OpenTofu deployment (experimental), ...
    action_required:                 # anything a user must act on when upgrading
      - text: EnableMCP now defaults to false; set it to true to keep the MCP server.
        affects: [capability:mcp-server]
    affects: [deployment:terraform]
    evidence: [CHANGELOG.md]
```

`action_required` mirrors the solution's own "action required" CHANGELOG items. Keep at least the last ten releases.

## Skills

`skills/<name>/SKILL.md` follows the Agent Skills format (front matter with `name` and `description`). A profile's skill is generic: it tells any assistant how to read this profile, recognise a need, assess fit and classify gaps. A consumer can use its own skill instead.

## Validation

A conforming validator checks:

1. Every file against `solution-profile.schema.json`.
2. Ids unique within each file, and every `<kind>:<id>` reference resolving. The kinds are `capability`, `extension`, `limitation`, `question`, `deployment`, `prerequisite` and `delivery`.
3. Every repository path in `evidence` existing at the commit being built.
4. `version` equal to the solution's own version file.
5. At release (a tag build): `reviewed_for_version == version`, and `changes.yaml` holding an entry for `version`.

## Versioning the contract

`schema_version` is 1. An added optional field is not a version change. A removed or renamed field, or a changed meaning, is version 2, and the old version is supported side by side for at least one consumer release.
