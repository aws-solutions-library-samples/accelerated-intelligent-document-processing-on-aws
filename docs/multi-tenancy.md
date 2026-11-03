---
title: "Multi-Tenant Isolation"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Multi-Tenant Isolation

One deployment can process documents for several tenants (business units, customers,
or the clients of a service team) and keep each tenant's documents visible only to
that tenant's users. This page explains how isolation works, how to turn on the
strict mode that makes it fail closed, which controls belong in your own AWS
environment rather than in the solution, and when separate stacks are the better
answer.

## How isolation works

Isolation is built from three things the solution already has, joined together:

1. **A prefix mapping decides a document's profile.** A [prefix
   mapping](configuration-profiles.md#prefix-mappings-assigning-a-profile-by-where-a-document-lands)
   assigns a Configuration Profile to every document that lands under an Input-bucket
   prefix, so the folder a document arrives in decides how it is processed.
2. **A user's scope decides what they can see.** A user's `allowedConfigVersions`
   (see [RBAC](rbac.md)) limits them to documents processed under the matching
   profiles. It is enforced server-side on document lists and counts, every
   operation that reads or acts on one document (including its versions, its review,
   deletion, abort and copy-to-baseline), file downloads, chat, reprocessing, review
   queues and test runs — see [Scope Enforcement
   Points](rbac.md#scope-enforcement-points).
3. **A tenant declaration ties the two together.** A tenant names its Input-bucket
   prefixes and a profile name pattern, for example:

   | Tenant | Prefixes | Profile pattern |
   |---|---|---|
   | `acme` | `clients/acme/` | `acme-*` |
   | `globex` | `clients/globex/`, `feeds/globex/` | `globex-*` |

So a document uploaded to `clients/acme/invoices/jan.pdf` is mapped to an `acme-*`
profile, and only users scoped to `acme-*` profiles can see it.

Before tenants are declared, nothing records that `clients/acme/` and `acme-*`
belong together. An admin could map `clients/acme/eu/` to a Globex profile, or scope
one user to both tenants' profiles, and nothing would object. Declaring tenants
turns those mistakes into refusals.

### Rules for declarations

- **Prefixes are folders and never overlap.** Each prefix ends in `/`, and no
  tenant's prefix may equal, contain or sit inside another's. Every object key
  therefore belongs to at most one tenant.
- **A profile pattern is a stem, a separator, and `*`.** Use `acme-*`, `acme_*` or
  `acme.*`. A bare `acme*` is refused because it would also match
  `acmecorp-invoices`. No tenant's stem may begin another's (`acme-*` and `acme-eu-*`
  cannot both be declared), so every profile name belongs to at most one tenant.

### What declaring tenants enforces, in every mode

| When an admin… | It is refused if… |
|---|---|
| Saves a prefix mapping | the prefix is inside tenant T and the profile is not one of T's, or the prefix is outside every tenant and the profile belongs to a tenant |
| Saves a tenant | it overlaps another tenant, or an existing prefix mapping would cross the new boundary |
| Sets a user's scope | the scope reaches more than one tenant, mixes a tenant with untenanted profiles, or contains a pattern such as `a*` that could match a tenant's profiles without naming its stem |
| Saves a tenant, again | any non-admin user's stored scope would cross a boundary under the new tenant set. The refusal names one such user and counts the rest |

Each refusal names the tenant, prefix or profile involved, so it can be fixed from the
message alone. A scope is never rewritten automatically: fix the user's scope, then save
the tenant. `idp-cli tenancy audit` lists every user whose scope needs attention (see
[Tooling](#tooling)).

## Strict mode

Deploy or update the stack with **`TenantIsolationMode=Strict`** to make isolation
fail closed. The default, `Off`, keeps the solution's established single-tenant
behaviour.

| Behaviour | `Off` | `Strict` |
|---|---|---|
| Non-admin user with no scope | Sees every document | Sees **nothing** until an admin sets a scope |
| Upload under tenant T processed under a profile of another tenant (via upload metadata, `submission-source`, or an ancestor mapping) | Processed | **Refused** at ingest, recorded as Failed with the reason |
| Upload outside every tenant's prefixes | Processed under the active profile | **Refused** at ingest once at least one tenant is declared (internal producers excepted, below) |
| Document chat | Streams through the chat Function URL | Uses the REST path, which carries the caller's verified identity and so can be scoped; the Function URL refuses |
| Agent Chat, agent queries and Knowledge Base queries, which read across documents | Available to their usual groups | **Admin only** |
| MCP tools | Available | **Refused**: the gateway passes no user identity to scope by |

The ingest rule is a single comparison: **the tenant that owns the object key must
be the tenant that owns the profile the document resolves to.** It does not depend
on knowing who wrote the object, which S3 events do not report, so it holds even when
an uploader sets its own metadata. Two of the solution's own producers are handled
explicitly:

- **Test Studio** copies test sets to the bucket root under the test-run ID. A run
  against a tenant's profile is accepted only while that run is in progress and its
  stored record names that same profile. Starting a run is itself scope-checked, so a
  user can only start one under a profile their scope allows, and an uploader cannot
  create the record at all.
- **The PII anonymizer** writes its redacted copy next to the original, so the copy
  stays under the tenant's prefix. Its companion profile must therefore be one of
  the tenant's profiles (for example `acme-redacted`). If it is not, the copy is
  refused at ingest and stays in the Input bucket, where an admin can reprocess it.

When strict mode cannot read the tenant declarations at ingest, the document is not
processed. The event is retried, and a persistent failure lands in the dead-letter
queue rather than processing the document without the check; see
[Monitoring](monitoring.md#tenant-isolation) for the metric and alarm. The same check
runs again when a malware-scan verdict releases a held document, and the upload panel
reports a refusal before the upload is made.

⚠️ **Admins are unrestricted in both modes.** An Admin writes the prefix mappings and
tenant declarations, so they could route any tenant's documents into their own view.
Scoping their reads would therefore look like a boundary without being one. Treat the
Admin group as a cross-tenant role, keep its membership small, and audit it. The one
place an Admin's own scope applies is uploading through the web UI: an Admin who has a
scope row is held to it there, in both modes, and an Admin with none can upload
anywhere, in both modes.

### Turning strict mode on

**Prerequisite:** only the people who deploy the generated access point template may
hold `s3:CreateAccessPoint` and `s3:PutAccessPointPolicy` in the stack's account. In
strict mode the bucket policies delegate to *any* access point the account owns, so
the generated access points' denies are the whole confinement, and a principal able to
create its own access point is outside it (see [Confine each tenant's writers and
readers to its prefixes](#confine-each-tenants-writers-and-readers-to-its-prefixes)).
Enforce this before switching, for example with a service control policy that
denies both actions to everyone except the deploying role:

```json
{
  "Effect": "Deny",
  "Action": ["s3:CreateAccessPoint", "s3:PutAccessPointPolicy"],
  "Resource": "*",
  "Condition": {
    "ArnNotLike": {
      "aws:PrincipalArn": "arn:aws:iam::<account-id>:role/<access-point-deployer>"
    }
  }
}
```

A permissions boundary on the account's other roles that excludes the same two actions
achieves the same where SCPs are not available.

1. Declare every tenant (Configuration → Prefix Mappings → **Tenants**, or `idp-cli
   tenancy apply`).
2. Run `idp-cli tenancy audit` and fix every finding: mappings that cross a boundary,
   users without a scope or with a scope spanning tenants, and documents whose key
   and profile disagree.
3. Update the stack with `TenantIsolationMode=Strict`. It is an in-place parameter
   change and replaces no data. The stack publishes the mode in the SSM parameter
   `/<StackName>/tenant-isolation-mode`, which Feature Platform extensions read at run
   time, so installed extensions follow the change within a minute.
4. Run `idp-cli tenancy verify`, which signs in as throwaway users of two tenants and
   confirms that each sees only its own documents, that an upload outside every
   tenant is refused, and that an upload naming another tenant's profile does not
   end up in that tenant's view.

## What this does not isolate

Isolation here is enforced by the application. Every processing Lambda, table and
bucket is shared, and IAM does not separate one tenant's documents from another's
inside the stack. That has these consequences:

- **The boundary is only as good as the code paths that check it.** Each API
  operation that touches documents declares in `scripts/api_rbac_expectations.yaml`
  how it applies the scope, and `make api-test-static` fails when the code does not
  match the declaration. That proves the declared check exists, not that every
  operation that should declare one does: a new operation that reads documents
  without a check is a cross-tenant read until it is added. `idp-cli tenancy verify`
  and `make verify-tenant-isolation` exercise the boundary on a live stack.
- **Throughput is shared.** One tenant's bulk upload uses the same workflow
  concurrency and Bedrock quotas as everyone else's.
- **Logs and metrics are shared.** Do not log document content. CloudWatch Logs
  authorises reads per log group, so a per-tenant log stream would not separate
  tenants.
- **An S3 writer chooses its own prefix.** Strict mode keeps a document inside the
  tenant of the prefix it lands under, but it cannot stop a principal with
  `s3:PutObject` on the whole bucket from writing under another tenant's prefix.
  That is an IAM control in your account, covered next.

## Hardening in your own environment

These controls live in your AWS account rather than in the solution. Most of them
can be generated from the tenant declarations.

### Confine each tenant's writers and readers to its prefixes

Give each tenant's upstream uploader and downstream consumer its own IAM role, and
grant those roles access through an **S3 Access Point per tenant** rather than
through the bucket. `idp-cli tenancy generate-policies` writes a CloudFormation
template that creates, for each tenant:

- an Input-bucket access point allowing the tenant's uploader roles `s3:PutObject`
  only under the tenant's prefixes;
- an Output-bucket access point allowing the tenant's consumer roles to read only
  the tenant's results.

In strict mode the stack's Input and Output bucket policies delegate access control
to access points owned by the stack's own account, so the generated access points
take effect once deployed. Review the generated template before deploying it. Each
access point policy confines with explicit denies, so the confinement holds whatever
the roles' own policies allow through an access point. What it cannot stop is access
granted on the **bucket** itself, which never passes through an access point, so grant
those roles nothing on the buckets directly.

With writers confined this way, the prefix a document arrives under is a fact about
its tenant rather than a choice the writer made, which is what the ingest rule above
relies on.

⚠️ **Whoever can create access points in the account is inside this boundary.** The
delegation admits any access point the stack's account owns, because CloudFormation
cannot name in advance the access points you will generate. A principal holding
`s3:CreateAccessPoint` and `s3:PutAccessPointPolicy` could therefore create one that
grants a different principal, or another account, access to any tenant's prefix.
Grant those two actions only to the people who deploy the generated template, and
alarm on CloudTrail `CreateAccessPoint` and `PutAccessPointPolicy` events naming the
stack's buckets.

### Other controls

- **Encryption keys.** If a tenant needs its own key, encrypt that tenant's uploads
  with a customer-managed KMS key (specified by the uploader, or by an access point
  policy that requires it), and grant the solution's roles use of the key.
  Destroying the key later makes every copy of those objects unreadable, backups
  included.
- **Audit trail.** Enable CloudTrail S3 data events on the Input and Output buckets.
  That is the record of which principal read or wrote each tenant's objects.
- **Reporting data.** The evaluation and reporting tables in Athena hold every
  tenant's results. If tenant users need reporting, grant it through Lake Formation
  with a row filter on the document's configuration profile. Otherwise keep reporting
  to Admins (strict mode does this for the solution's own analytics surface).
- **Sensitive-data discovery.** Amazon Macie jobs scoped to each tenant's prefixes
  report personal data per tenant.

### When to use separate stacks instead

Shared-stack isolation suits tenants who share an operator, such as one service team
processing documents for several clients. Deploy **one stack per tenant** instead,
or one account per tenant, when any of these applies:

- a tenant requires that its documents never be processed by credentials that can
  reach another tenant's data;
- a tenant needs its own throughput guarantees or its own region;
- a tenant must be removable completely, including table backups, by deleting
  infrastructure.

Separate stacks need no tenant declarations and no strict mode, because IAM and the
stack boundary do the separating. The cost is one deployment to operate per tenant.

## Tooling

| Command | What it does |
|---|---|
| `idp-cli tenancy list` | Lists tenant declarations |
| `idp-cli tenancy put` / `delete` | Creates, edits or removes one declaration, with the same checks the web UI applies |
| `idp-cli tenancy apply --file tenants.yaml` | Declarative: shows the difference, then writes it with `--yes` (`--prune` removes undeclared tenants) |
| `idp-cli tenancy audit` | Read-only. Reports boundary-crossing mappings, unscoped or multi-tenant users, mismatched documents and the stack's mode. Exits 1 when it finds anything |
| `idp-cli tenancy generate-policies` | Writes the per-tenant access point template described above |
| `idp-cli tenancy verify` | Live cross-tenant test against a deployed stack, cleaning up after itself |

See [IDP CLI](idp-cli.md#tenancy) for options. The checks behind every command live
in `idp_common.config.tenants`; see its [module
documentation](../lib/idp_common_pkg/idp_common/config/README.md#tenant-declarations--tenantspy).
