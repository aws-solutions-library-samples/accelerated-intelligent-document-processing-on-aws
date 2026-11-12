---
title: "Multi-Tenant Isolation"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Multi-Tenant Isolation

One deployment can process documents for several tenants (business units, customers,
or the clients of a service team) and keep each tenant's documents visible only to
that tenant's users. This page explains how isolation works, how to turn on the
strict mode that makes it fail closed, who grants a user their tenant, which controls belong in your own AWS
environment rather than in the solution, and when separate stacks are the better
answer.

This walkthrough sets up a tenant, its prefix mapping and a user, shows the
safeguards and the strict-mode upload checks, and runs the `idp-cli tenancy`
commands (about 4.5 minutes):

https://github.com/user-attachments/assets/27e12b5d-0595-4c24-a4f3-ab5eaff6d6f7

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
| Sets a user's configuration-profile scope (`allowedConfigVersions`) | the scope reaches more than one tenant, mixes a tenant with untenanted profiles, or contains a pattern such as `a*` that could match a tenant's profiles without naming its stem |
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
| Document chat | Streams through the chat Function URL | The web UI is not given the Function URL and uses the REST path. Both paths scope the turn from the caller's verified identity: the Function URL verifies the Cognito ID token the browser presents and refuses a request without one |
| Agent Chat, agent queries and Knowledge Base queries, which read across documents | Available to their usual groups | **Admin only** |
| MCP tools | Available to the caller's role, within their configuration scope | Scoped like the UI: a signed-in user sees only their tenant's documents; `search` is **Admin only**; the stack-created client-credentials client is created **disabled** until an Admin gives it one tenant's profiles |

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

The ingest check costs one strongly consistent DynamoDB read of the tenant index per
document (two when malware scanning holds the document and releases it later). It is
deliberately not cached, so a tenant change applies to the very next upload.

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

`idp-cli tenancy` reports the mode as `Unknown` for a stack deployed from a release
that predates the parameter. Such a stack behaves as `Off`, and updating it to this
release adds the parameter with its default, `Off`.

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
      "aws:PrincipalArn": "arn:*:iam::<account-id>:role/<access-point-deployer>"
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

## Who grants a tenant

A user's tenant is their configuration-profile scope (`allowedConfigVersions`). The
stack parameter **`TenantGrantSource`** decides who writes it:

| `TenantGrantSource` | Who sets a user's scope | When it changes |
|---|---|---|
| `Manual` (default) | An Admin, in User Management | When the Admin saves it |
| `IdpClaim` | The external identity provider's group or role claim | At the user's next fresh sign-in |
| `Api` | An entitlement system, through the `setUserTenant` API operation, `idp-cli tenancy set-user-tenant` or the SDK's `client.tenancy.set_user_tenant` | At the next request after the push (within about a minute, the scope cache's lifetime) |

Under `IdpClaim` and `Api` four things change, whatever `TenantIsolationMode` says:

- **A user's scope is exactly one tenant's profile pattern, or nothing.** The grant
  writes the tenant's `profilePattern` as the whole scope, replacing whatever was there.
  A user granted no tenant gets the scope **`!no-tenant-granted`**, which matches no
  profile, and User Management shows it as *No tenant granted*.
- **A non-Admin user with no scope sees nothing,** exactly as under strict mode. A user
  who has never been granted a tenant, for example one who signed in before any
  entitlement push, is denied rather than served every tenant's documents.
- **The surfaces that read across tenants are closed to non-Admins,** as in strict
  mode: Agent Chat, agent queries, Knowledge Base queries and the MCP `search` tool are
  Admin only, the stack-created MCP client-credentials client starts disabled, and the web
  UI sends document chat over the REST path. The chat Function URL applies the same
  rules from the caller's verified ID token. Once users are confined to a tenant, any
  of these would otherwise be a way around the confinement.
- **Admins cannot edit configuration-profile scopes** in User Management or through
  `createUser`/`updateUser`. The field is shown read-only with the tenant and where it
  came from, and the API refuses an edit, naming the grant source. Test-set scope
  (`allowedTestSets`) is a separate axis and stays editable.

Two controls still need **`TenantIsolationMode=Strict`** as well: the ingest-time refusal
of a document whose key and resolved profile belong to different tenants, and the
bucket-policy delegation to per-tenant S3 Access Points. An external grant source
decides who may read what; it does not change what ingest accepts. Set both for full
isolation.

Admins are not granted a tenant by either route; they remain cross-tenant (see
[Strict mode](#strict-mode)). Under `IdpClaim`, a user granted a tenant and later given
the Admin role loses that tenant scope at their next fresh sign-in. Under `Api` the
user keeps the last tenant pushed, and "no tenant" writes the deny marker rather than
removing the scope, so to make such a user a cross-tenant Admin, delete them in User
Management and create them again as an Admin.

The reverse change is not picked up on its own. The user's stored role is set when
their record is created and is not resynced, and an Admin record is never granted a
tenant. So a user stored as Admin whose claim now maps to an ordinary role and a
tenant is granted nothing and, under `IdpClaim`, sees nothing. The sign-in logs a
warning naming the user. Delete them in User Management, and their next sign-in
recreates the record from the claim with the right role and tenant.

### A user is confined to one tenant

Each tenant declaration has an optional list of **Granted by** values (`grantedBy`):
the opaque strings that grant that tenant, such as an Entra app-role value or an Okta
group name. A value may grant only one tenant, and a tenant takes at most three. When
the values a user holds grant **two or more** tenants, the user is granted **none**:
their scope becomes `!no-tenant-granted` and the sign-in logs a warning naming the
tenants. There is no active-tenant selector, so a person who works for two tenants
needs two accounts. Values that grant no tenant are ignored, because the same claim
also carries the user's functional roles.

### Granting from the identity provider's claim

Set `TenantGrantSource=IdpClaim` on a stack with [federation](external-idp.md)
configured. It requires `ExternalIdPGroupAttributeName`, the claim the role mapping
already reads; a deployment without it is refused. At each fresh federated sign-in the
pre-token trigger reads the tenant declarations, matches the claim's values against
every tenant's **Granted by** values and writes the result to the user's record before
the token is issued. A user with no record yet gets one, with the role the claim maps
to.

The grant is bound to the signing-in account's Cognito `sub`. The trigger writes to
the record that account's `sub` already names or, failing that, to a record with the
account's email address that names no `sub` yet, such as one an Admin created, and it
records the `sub` in the same write. It never writes to a record that names a
different `sub`, so each account gets a record of its own; see
[Accounts that share an email address](#accounts-that-share-an-email-address).

The trigger applies the same two checks it uses for roles: it reads the claim only for
a user federated through the configured provider, and only on a fresh sign-in, never
on a token refresh. A user who signs in with a Cognito password rather than through
the provider is not granted a tenant, and so sees nothing.

⚠️ **The sign-in fails if the grant cannot be written.** If the tenant declarations or
the user's record cannot be read or written, the trigger raises and Cognito refuses the
token. Signing in with yesterday's tenant would be the worse failure. An absent or empty
claim is not an error: it grants no tenant, and the user's previous tenant is removed.
That is the opposite of how roles behave, where a claim mapping to nothing leaves
groups unchanged, and it is deliberate: losing a tenant reduces access, losing a role
mapping could remove the last way to sign in.

**Worked example: Microsoft Entra ID app roles.** App roles put roles and tenants in
one claim, which is what `ExternalIdPGroupAttributeName` reads.

1. In the app registration, under **App roles**, create one role per function and one
   per tenant, for example `IDP.Viewer`, `IDP.Author`, `tenant.acme` and
   `tenant.globex`. Assign users or groups to them under **Enterprise applications →
   Users and groups**.
2. Deploy with the role claim as the group attribute (`roles` for OIDC, or
   `http://schemas.microsoft.com/ws/2008/06/identity/claims/role` for SAML), the role
   mapping parameters (`ExternalIdPViewerGroupName=IDP.Viewer`, and so on) and
   `TenantGrantSource=IdpClaim`.
3. Declare the tenants with their values: `acme` granted by `tenant.acme`, `globex`
   granted by `tenant.globex` (Configuration → Prefix Mappings → **Tenants**, or the
   `grantedBy` key in `idp-cli tenancy apply`).

A user assigned `IDP.Viewer` and `tenant.acme` signs in as a Viewer scoped to
`acme-*`. Assigning them `tenant.globex` as well grants them no tenant until one of the
two is removed.

⚠️ **Entra must send an `email` claim.** The user pool requires email, so Cognito
itself refuses a federated sign-in whose token carries none, with the error
`attributes required: [email]`; the trigger also refuses to record a tenant without
one. Entra omits the claim for an account with no mailbox
unless the app registration adds the `email` optional claim (**Token configuration →
Add optional claim → ID → email**); add it before switching.

**Okta.** Create groups such as `idp-viewers`, `tenant-acme` and `tenant-globex`, send
them in the groups claim (a filter such as *Starts with* `idp-` or `tenant-` keeps the
claim small), and declare `tenant-acme` and `tenant-globex` as the tenants' **Granted
by** values. The rest is as above.

### Granting through the API

Set `TenantGrantSource=Api` when an entitlement system such as SailPoint, or your own
provisioning script, owns tenant membership. It calls one of:

- the Admin-only API operation `setUserTenant(email, tenantId)`;
- `idp-cli tenancy set-user-tenant --email <address> --tenant <tenantId>`, with
  IAM credentials (omit `--tenant` to grant none);
- `client.tenancy.set_user_tenant(email, tenant_id)` in the SDK.

The user must already exist: the call refuses an unknown address rather than create an
account. It also refuses an address that more than one user record holds, one for
each account with that address, because it cannot tell which account the grant is
for; see [Accounts that share an email address](#accounts-that-share-an-email-address).
A `tenantId` that is not declared is refused, and so is an Admin. Granting a
tenant replaces the previous one. The tenant's **Granted by** values are not used by
this route; the tenant ID is what the entitlement system sends. All three refuse when
the stack's `TenantGrantSource` is not `Api`.

### Accounts that share an email address

Cognito does not require email addresses to be unique, so two accounts in the user
pool can have the same one: a native and a federated account, for example, or an
account recreated in the identity provider. A user record belongs to the Cognito
account whose `sub` it names.

When the scope is read for a request, a record that names a different `sub` than the
caller's is not applied as the caller's under `IdpClaim`, `Api` or
[Strict mode](#strict-mode): the caller is denied, as a user with no tenant is. Under
`Manual` with isolation off it is still applied, because there a user with no record
is unrestricted and the record's scope can only narrow access. Where an address holds
several records, the one naming the caller's `sub` is used.

What gives the second account access depends on the grant source:

- **`IdpClaim`:** nothing to do. Each account gets its own record at its fresh
  sign-in. A record that two accounts already share is sorted out at each account's
  next fresh sign-in: the account the record names keeps it, and the other gets a
  record of its own.
- **`Api`, and `Manual` with Strict mode:** an address has one record, because User
  Management creates one per address and the entitlement push addresses users by
  email. The record stays with the account it names. When User Management lists
  users it re-binds a record only if the account the record names is no longer in
  the user pool, and never moves it from one account in the pool to another. To give
  the address to the other account, delete the account that should not hold it in
  the Cognito user pool, or give that account a different address, and then open
  User Management, which binds the record to the remaining account. Under `Api` the
  entitlement system's next push for the address then applies to that account; under
  `Manual` an Admin can review its scope there. An address that holds a record per
  account, which a period under `IdpClaim` can leave behind, is refused by
  `setUserTenant` until only one record for it remains.

### How quickly a removal takes effect

- **`Api`:** at the user's next request after the push, within the minute a resolver
  may cache a user's scope.
- **`IdpClaim`:** at the user's next fresh sign-in. A web session renews itself with its
  refresh token without signing in again, for up to **`RefreshTokenValidityHours`**
  (default 720 hours, 30 days); until then the user keeps the tenant the claim last
  granted. Lower that parameter to shorten the window, or disable the user in Cognito
  to remove access at once. Changing it from the default sets the web UI client's
  refresh-token lifetime in hours; left at 720 the client is not modified.

`idp-cli tenancy audit` reports users granted no tenant and, under `IdpClaim` or `Api`,
any user whose scope is not exactly one tenant's pattern (for example a scope set by
hand before the grant source changed).

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
  and `make verify-tenant-isolation` exercise the boundary on a live stack, and the
  same harness runs on a throwaway Strict stack on the CI's weekly variant-probe
  schedule (`make stacktest-tenancy` runs that probe by hand).
- **Test sets have no tenant.** An Annotator's test-set scope (`allowedTestSets`) is
  not checked against tenant declarations, because a test set records no tenant. In a
  shared stack, keep test sets built from one tenant's documents assigned only to that
  tenant's Annotators.
- **Throughput is shared.** One tenant's bulk upload uses the same workflow
  concurrency and Bedrock quotas as everyone else's.
- **Logs and metrics are shared.** Do not log document content. CloudWatch Logs
  authorises reads per log group, so a per-tenant log stream would not separate
  tenants.
- **An S3 writer chooses its own prefix.** Strict mode keeps a document inside the
  tenant of the prefix it lands under, but it cannot stop a principal with
  `s3:PutObject` on the whole bucket from writing under another tenant's prefix.
  That is an IAM control in your account, covered next.

### If MCP tools return 403

MCP callers are scoped like web UI users. The gateway's interceptor resolves each
token to a role and a configuration scope, and the MCP handler refuses a call it
cannot verify. Check, in order:

- **The caller's role.** `process` and `reprocess` need Admin or Author; `search`
  needs Admin, Author or Viewer and no configuration scope; `status` and
  `get_results` need Admin, Author, Reviewer or Viewer. A user in no group is
  refused.
- **The isolation mode and grant source.** Under `Strict`, `IdpClaim` or `Api` a
  non-Admin always has a scope, so `search` is Admin only. The stack-created
  client-credentials client ("Default (stack-created)" on the MCP Configuration
  page) is registered disabled there, because its access is unscoped; give it one
  tenant's profiles and enable it, or create a credential per tenant. Whether it starts enabled is decided once, when the stack first registers it, and
  a later stack update never changes it. Switching `TenantIsolationMode` to `Strict`
  afterwards leaves it enabled but, having no profiles, it then sees no documents;
  switching back to `Off` leaves a disabled one disabled until an Admin enables it.
- **A service credential.** It must be active and unexpired, its role must admit
  the tool, and it sees only its own configuration profiles. Under `Strict`, an
  Admin credential is refused and an unscoped one sees nothing.
- **The function's `idp_common` layer.** Without it the handler cannot verify the
  caller and refuses every tool, saying the caller could not be verified.

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
take effect once deployed (the procedure is [below](#generate-and-deploy-the-access-points-step-by-step)). Review the generated template before deploying it. Each
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

### Generate and deploy the access points, step by step

This takes a stack from declared tenants to uploaders and consumers that can reach only
their own tenant's prefixes. Before starting:

- the stack runs with `TenantIsolationMode=Strict`, because only then do its bucket
  policies delegate to access points, and the generated ones grant nothing otherwise;
- every tenant is declared and mapped (`idp-cli tenancy list` shows them);
- each tenant's uploader and consumer is an IAM role of its own, with **no** grant on
  the Input or Output bucket itself;
- creating access points is restricted as described in the warning above.

**1. Name each tenant's roles** in a `principals.yaml`. A role may be both a tenant's
uploader and its consumer; an account-root ARN is refused.

```yaml
acme:
  uploaders: [arn:aws:iam::<account-id>:role/acme-uploader]
  consumers: [arn:aws:iam::<account-id>:role/acme-results-reader]
globex:
  uploaders: [arn:aws:iam::<account-id>:role/globex-uploader]
  consumers: [arn:aws:iam::<account-id>:role/globex-results-reader]
```

A declared tenant with no entry gets no access points, and the command says so.

**2. Let the roles use the stack's encryption key.** The buckets are encrypted with the
stack's `CustomerManagedEncryptionKey`. Uploaders need `kms:GenerateDataKey` on it and
consumers `kms:Decrypt`. Find its ARN, then attach a statement like this to each role:

```bash
KEY_ID=$(aws cloudformation describe-stack-resource --stack-name <stack> \
  --logical-resource-id CustomerManagedEncryptionKey \
  --query StackResourceDetail.PhysicalResourceId --output text)
aws kms describe-key --key-id "$KEY_ID" --query KeyMetadata.Arn --output text
```

```json
{
  "Effect": "Allow",
  "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
  "Resource": "<key-arn>"
}
```

A role in the stack's account needs **no S3 permission of its own**: the access point
policy is the grant. A role in another account also needs an identity policy allowing
`s3:PutObject` (uploaders) or `s3:GetObject` and `s3:ListBucket` (consumers) on its own
access point's ARN, and the key policy must admit it.

**3. Review the template, then deploy it.** The first command only writes the template;
the second writes it again and deploys it as its own CloudFormation stack, separate
from the IDP stack, named `<stack>-tenant-access-points` unless `--access-point-stack`
says otherwise. Running it again updates that stack in place.

```bash
idp-cli tenancy generate-policies --stack-name <stack> --principals principals.yaml \
  --output tenant-access-points.yaml
idp-cli tenancy generate-policies --stack-name <stack> --principals principals.yaml \
  --output tenant-access-points.yaml --deploy
```

The deploy prints, per tenant, an **upload** access point on the Input bucket and a
**read** access point on the Output bucket, each with its ARN and alias. `--json`
prints the same as JSON. The stack's outputs hold them too.

**4. Address the access point, not the bucket.** An alias works wherever a bucket name
does, so a tenant's tooling changes only the bucket part of each S3 URI. An uploader
writes under its tenant's prefix through the upload alias:

```bash
aws s3 cp invoice.pdf s3://<acme-upload-alias>/clients/acme/2026/invoice.pdf
```

A consumer reads results through the read alias. A document's results sit under its
input key, so the results of the upload above are under
`s3://<acme-read-alias>/clients/acme/2026/invoice.pdf/`:

```bash
aws s3 ls s3://<acme-read-alias>/clients/acme/2026/invoice.pdf/ --recursive
```

Anything else is refused with `AccessDenied`: the same role writing under another
tenant's prefix through its own access point, using another tenant's access point, or
writing to the bucket directly.

**5. Check what is deployed.** `idp-cli tenancy audit --stack-name <stack> --principals
principals.yaml` compares each deployed access point policy with what the generator
would produce now, and reports any difference. Re-run step 3 with `--deploy` after
changing a tenant's prefixes or the principals file, and the audit again afterwards.

To remove the access points, delete the access point stack
(`aws cloudformation delete-stack --stack-name <stack>-tenant-access-points`). The IDP
stack and its data are untouched.

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
| `idp-cli tenancy set-user-tenant` | Grants one user one tenant, or none, when `TenantGrantSource=Api` |
| `idp-cli tenancy apply --file tenants.yaml` | Declarative: shows the difference, then writes it with `--yes` (`--prune` removes undeclared tenants) |
| `idp-cli tenancy audit` | Read-only. Reports boundary-crossing mappings, unscoped or multi-tenant users, users granted no tenant, mismatched documents and the stack's mode. Exits 1 when it finds anything |
| `idp-cli tenancy generate-policies` | Writes the per-tenant access point template described above, and with `--deploy` deploys it as its own stack (see [Generate and deploy the access points, step by step](#generate-and-deploy-the-access-points-step-by-step)) |
| `idp-cli tenancy verify` | Live cross-tenant test against a deployed stack, cleaning up after itself |

See [IDP CLI](idp-cli.md#tenancy) for options. The checks behind every command live
in `idp_common.config.tenants`; see its [module
documentation](../lib/idp_common_pkg/idp_common/config/README.md#tenant-declarations--tenantspy).
