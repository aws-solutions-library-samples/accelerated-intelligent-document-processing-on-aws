---
title: "Role-Based Access Control (RBAC)"
---

# Role-Based Access Control (RBAC)

## Overview

The GenAI IDP Accelerator implements a comprehensive Role-Based Access Control system with **server-side enforcement** at the API layer, supplemented by UI-level navigation and action controls for a clean user experience. It also supports **configuration-profile scoping** to restrict non-admin users to specific [Configuration Profiles](configuration-profiles.md) (use cases).

> **Terminology.** A **Configuration Profile** is what earlier releases called a
> "config version"; a **revision** is an immutable snapshot of one profile's
> configuration. The stored field is still named
> `allowedConfigVersions` for compatibility, and it scopes **profiles**.


https://github.com/user-attachments/assets/a1e9ce1a-1b2e-4e98-a387-d2e48d7e557d



## Roles

Four roles are defined as Cognito User Pool groups:

| Role | Cognito Group | Description |
|------|--------------|-------------|
| **Admin** | `Admin` | Full access to all operations including user management and pricing |
| **Author** | `Author` | Read + write access to documents, configuration, tests, discovery |
| **Reviewer** | `Reviewer` | HITL review operations + limited document visibility |
| **Viewer** | `Viewer` | Read-only access to documents, configuration, agent chat |
| **Annotator** | `Annotator` | Annotates ground truth for assigned test sets only. No access to the document list, configuration, or test sets outside its assignment. |

### Multi-Group Support

Users can belong to multiple groups. Permissions are the **union** of all group permissions. For example, a user in both `Author` and `Reviewer` groups can both write documents and perform HITL reviews.

## The Annotator Role

`Annotator` exists so ground-truth labeling can be delegated to people who should
not see the rest of the deployment. It is the only role whose access is granted
per-object rather than per-feature: an Annotator with no `allowedTestSets` can do
nothing at all.

**What an Annotator can reach**

| Operation | Purpose |
|---|---|
| `getAnnotationQueue` | Their worst-first review queue for one assigned set |
| `getTestSetDocuments` | The documents and label state of an assigned set |
| `estimateReviewEffort` | The "what your review is buying" panel in the workspace |
| `reextractTestSetDocument` | Re-run extraction after correcting a document's class |
| `claimReview` / `releaseReview` / `completeSectionReview` | Claim a document and save corrected labels |

Everything else in Test Studio — creating sets, publishing versions, generating
draft labels, resetting labels, deleting — stays Admin/Author. `skipAllSectionsReview`
is deliberately excluded: marking a document reviewed without opening it is the set
owner's decision, not an annotator's.

**Two-layer enforcement.** Group membership only reaches the operation; every one
of the above additionally asserts the target test set is in the caller's
`allowedTestSets`. The check is centralized in
`idp_common/testset_scope.py::assert_can_access_test_set` and **fails closed** — an
unreadable users record, a missing attribute, or an empty list all deny. Reaching a
test-set operation is never the same as being allowed to see a given set.

For the HITL operations the scope is resolved from the document rather than an
argument: a review document carries the `TestSetId` it came from, and an Annotator
attempting a document with no `TestSetId` (i.e. ordinary production review work) is
refused outright.

**Scope caching.** Lookups are cached briefly per Lambda container. The TTL is
asymmetric on purpose: a populated scope is held for 5 minutes (bounding how long a
revoked annotator keeps access), while an empty scope is held for only 10 seconds,
so granting a new assignment takes effect almost immediately rather than leaving the
user locked out for the full TTL.

## Permission Matrix

```
Feature / API                    Admin   Author   Reviewer   Viewer
──────────────────────────────────────────────────────────────────────
DOCUMENTS
  List documents                  ✅      ✅†      ✅*†      ✅†
  View document details           ✅      ✅†      ✅*†      ✅†
  Upload documents                ✅      ✅       ❌        ❌
  Delete documents                ✅      ✅       ❌        ❌
  Reprocess documents             ✅      ✅       ❌        ❌
  Abort workflows                 ✅      ✅       ❌        ❌

HITL REVIEW
  Claim/Release review            ✅      ❌       ✅        ❌
  Complete section review         ✅      ❌       ✅        ❌
  Skip all section reviews        ✅      ❌       ✅        ❌
  Process changes (edit mode)     ✅      ❌       ✅        ❌

CONFIGURATION
  View config profiles            ✅      ✅†      ❌        ✅†
  View/Edit configuration         ✅      ✅†      ❌        ❌
  Save as Profile (new profile)   ✅      ❌       ❌        ❌
  Save as Default                 ✅      ❌       ❌        ❌
  Delete config profile           ✅      ❌       ❌        ❌
  Set active profile              ✅      ✅†      ❌        ❌
  Sync BDA                        ✅      ✅†      ❌        ❌

CONFIGURATION PROFILE REVISIONS
  View/compare revisions          ✅      ✅†      ❌        ✅†
  Restore a revision              ✅      ✅†      ❌        ❌
  Label a revision                ✅      ✅†      ❌        ❌
  Delete a revision               ✅      ❌       ❌        ❌

DISCOVERY
  List/run discovery jobs         ✅      ✅       ❌        ❌

AGENT CHAT & CODE EXPLORER
  Chat with agent                 ✅      ✅       ❌        ✅
  Code intelligence               ✅      ✅       ❌        ✅

TEST STUDIO
  View/run test sets              ✅      ✅       ❌        ❌
  Create/delete test sets         ✅      ✅       ❌        ❌

CUSTOM MODEL FINE-TUNING
  List/view fine-tuning jobs      ✅      ✅       ❌        ❌
  Create fine-tuning jobs         ✅      ✅       ❌        ❌
  Delete fine-tuning jobs         ✅      ✅       ❌        ❌
  List available models           ✅      ✅       ❌        ❌

CAPACITY PLANNING
  Calculate capacity              ✅      ✅       ❌        ✅

USER MANAGEMENT
  List all users                  ✅      ❌       ❌        ❌
  Create/delete users             ✅      ❌       ❌        ❌
  Edit user scope                 ✅      ❌       ❌        ❌
  View own profile                ✅      ✅       ✅        ✅

PRICING
  View pricing                    ✅      ✅       ❌        ✅
  Edit pricing                    ✅      ❌       ❌        ❌

MODEL LIMITS
  View model limits               ✅      ✅       ❌        ✅
  Edit model limits               ✅      ❌       ❌        ❌

✅* = Reviewer sees only HITL-pending docs + their own completed reviews (server-side filtered)
✅† = Scoped by allowedConfigVersions if set (see Configuration-Profile Scoping below)
```

## Configuration-Profile Scoping (Use Case Isolation)

### Overview

Non-admin users can optionally be assigned **allowedConfigVersions** — a list of Configuration Profile names that restricts their view and access to only those use cases. This enables multi-tenant or multi-use-case deployments where different teams see only their relevant documents and configurations.

### How It Works

- **Admin users**: Always unrestricted — `allowedConfigVersions` is ignored even if set
- **All other roles** (Author, Reviewer, Viewer): If `allowedConfigVersions` is set and non-empty, the user can only:
  - See documents processed with those profiles (server-side filtering)
  - See and select those profiles in all profile dropdowns
  - View/edit configuration — and read, compare, restore, and label revisions — for those profiles only
- **No scope set** (empty/null): User sees all profiles and documents (unrestricted)

### Scope Is Enforced at the Profile, Never at the Revision

A revision is *content inside* a profile, not an access-control object of its own.
Every revision operation resolves its profile first and applies the same scope check
used by `updateConfiguration`, so there is exactly one rule to get right.

This is also why an Author scoped to a profile may **restore** and **label** its
revisions but still may not create a new profile: moving content inside a profile
they already own is ordinary authoring, while minting a profile creates a new
access-control object and stays Admin-only. Before revisions existed, keeping a
previous configuration *required* creating a new profile (`saveAsVersion`), which is
why a scoped Author could not iterate without an admin.

### Matching Rules

Scope entries are matched against the profile name with two deliberate rules:

- **An empty or unset scope means unrestricted.** Scoping is opt-in per user.
- **A set scope fails closed.** A profile or document with no name to match against
  is denied, not admitted. In particular, a scoped user does **not** see documents
  that carry no `ConfigVersion` (documents processed before config-version stamping,
  or whose stamp failed) — an unnamed object cannot be proven in scope.

Entries may be exact names (`lending`) or **glob patterns** (`lending-*`,
`uc?-prod`). Patterns exist for deployments that predate revision history and encode
iterations in the name (`usecaseA_v1`, `usecaseA_v2`, …), where scoping a user to a
use case would otherwise mean re-granting on every iteration. Only an Admin can set
a scope entry and only an Admin can create a profile, so a pattern cannot be used to
widen one's own access. New deployments should prefer one profile per use case with
revisions for its history, and exact-name scope entries.

The matcher lives in `idp_common/config_scope.py`. The two document-list resolvers
carry no `idp_common` layer (they are on the hottest UI query and are kept
dependency-free), so they vendor that file verbatim; a unit test fails if the copies
drift, because a scope matcher that differs between call sites is a
privilege-escalation bug.

### Scope Enforcement Points

| Layer | Enforcement |
|-------|-------------|
| **Document List** (server-side) | Both `listDocuments` resolvers filter by the `ConfigVersion` field using `allowedConfigVersions` from UsersTable (fails closed on an unstamped document) |
| **Document Chat** (server-side) | The chat processor resolves the target document's `ConfigVersion` and refuses out-of-scope (and unstamped) documents |
| **Config Profile List** (server-side) | `getConfigVersions` Lambda resolver filters returned profiles |
| **Config Profile Access** (server-side) | `getConfigVersion` Lambda resolver rejects requests for out-of-scope profiles |
| **Revision Operations** (server-side) | All five `*ConfigProfileRevision*` operations reject out-of-scope profiles before doing any work |
| **Version Dropdowns** (UI) | `useConfigurationVersions` hook filters versions client-side for immediate UX |
| **Default Version Selection** (UI) | All version pickers auto-select the first available scoped version |

### Affected UI Components

All pages with config version selectors automatically respect scope:

| Page | Behavior |
|------|----------|
| **View/Edit Configuration** | Shows only scoped versions in Versions panel; loads first scoped version |
| **Upload Documents** | Version picker shows only scoped versions |
| **Discovery** | Version picker shows only scoped versions |
| **Test Studio** | Test runner version picker shows only scoped versions |
| **Capacity Planning** | Version picker shows only scoped versions |
| **Reprocess Document** | Defaults to document's current ConfigVersion (if in scope) |
| **Document List** | Server-side filtered — only shows documents matching scoped versions |

### Managing User Scope

Admins can manage user scope via the **User Management** page:

1. **Create user with scope**: When creating a new user, optionally select config versions from the multiselect
2. **Edit user scope**: Click "Edit scope" on any non-Admin user row to add/remove config versions
3. **Remove scope**: Clear all selections to make a user unrestricted

Admin users' scope cannot be edited (they are always unrestricted).

### API: `getMyProfile`

All authenticated users can call `getMyProfile` to retrieve their own profile including `allowedConfigVersions`. This is used by the UI to apply client-side scope filtering immediately on page load.

```graphql
query GetMyProfile {
  getMyProfile {
    userId
    email
    persona
    allowedConfigVersions
  }
}
```

### API: `updateUser` (Admin-only)

```graphql
mutation UpdateUser($userId: ID!, $allowedConfigVersions: [String]) {
  updateUser(userId: $userId, allowedConfigVersions: $allowedConfigVersions) {
    userId
    email
    allowedConfigVersions
  }
}
```

## Enforcement Layers

### Layer 0: The API dispatcher denies by default (Server-Side)

The Web UI calls a single REST route, `POST /op/{field}`, whose Cognito
authorizer only **authenticates** — it performs no group checks. Before routing a
request to a resolver, the dispatcher
(`nested/api-resolvers/src/lambda/http_api_dispatcher/authz.py`) compares the
caller's Cognito groups against a per-operation required-groups manifest and
refuses the request with **HTTP 403** (`errorType: "Unauthorized"`) when they do
not intersect. The groups come from the verified JWT claim
(`identity.claims['cognito:groups']`), never from the request body.

**A field with no manifest entry is denied.** Unmapped means denied, so an
operation whose required groups were never declared is closed rather than open —
this is what makes a forgotten resolver check on a **group-scoped** operation a
visible 403 instead of an unprotected endpoint.

**Four policies, and the difference between two of them is easy to miss.** An
operation declares one of:

| Policy | The dispatcher requires | Count |
|---|---|---|
| a group list, e.g. `[Admin, Author]` | one of those groups | 90 |
| `ANY_GROUP` | **any** group the stack creates — so a caller in *no* group is refused | 11 |
| `ANY` | authentication only; group membership is not consulted | 15 |
| `IAM_ONLY` | rejects every Cognito caller (backend/IAM principals only) | 2 |

`ANY` means authenticated, not vetted, and that is weaker than it reads. When you
set `AllowedSignUpEmailDomain`, the user pool permits self-service sign-up
(`AllowAdminCreateUserOnly: false`), so anyone with an address at that domain can
register themselves and hold a valid token whose `cognito:groups` claim is
**empty**. Such a caller satisfies every `ANY` operation and no group-scoped one.
`ANY_GROUP` is the declaration for "an administrator has onboarded this person,
whichever role they were given"; the document-content reads (`getDocument`,
`listDocuments`, `listDocumentsByDateRange`, `getDocumentVersion`,
`compareDocumentVersions`, `getFileContents`, `getFilePresignedUrl`,
`queryKnowledgeBase`) and three mutations (`deleteAgentJob`, `deleteChatSession`,
`sendChatDocumentMessage`) carry it.

`ANY_GROUP` is written as a sentinel rather than as the five group names because
the policy is about the *vocabulary*, not about five particular names:
`scripts/sdlc/generate_api_rbac_manifest.py` resolves it against the
`AWS::Cognito::UserPoolGroup` resources in `template.yaml` on every build, so a
sixth group added there is covered without editing any operation. The Lambda never
sees the sentinel — it is expanded before the manifest is written, so the runtime
keeps one comparison, and an unexpanded `ANY_GROUP` in the manifest means a broken
build and is rejected as one (deny-all) rather than guessed at.

⚠️ **The `ANY` operations are still only authenticated.** The dispatcher enforces
authentication but *not* group membership for those 15, so a forgotten resolver
check on one of them is reachable by any authenticated caller, including a caller
in no group. They are enumeration, platform, profile and feature-catalog reads —
counts, index partitions, run-id lists, the caller's own profile, the published
release number, breaker status, fine-tuning job status, the feature catalog — and
each entry in `scripts/api_rbac_expectations.yaml` carries a note saying why `ANY`
is the intended answer for it. Four of the 15 are narrowed further by record
ownership or by the caller's allowed configuration versions.

⚠️ **A group check is not a per-document check.** `ANY_GROUP` establishes that the
caller was onboarded; it does not establish that this document is theirs. A Viewer
may read any document a Viewer can see, and `getFileContents` bounds itself with a
bucket allowlist rather than a per-document scope. If your documents must be
private to their submitter or to a tenant, group membership is the wrong axis.

⚠️ **The group floor gates the API, not the S3 buckets, and the UI reads S3
directly.** `CognitoIdentityPoolSetRole` in `template.yaml` attaches a **single**
`authenticated` role with **no `RoleMappings`**, so group membership plays no part
in which role a signed-in user assumes. That role, `CognitoAuthorizedRole`, grants
`s3:GetObject`, `s3:GetObjectVersion` and `s3:ListBucket` on the Input, Output and
Configuration buckets plus `kms:Decrypt` on the customer-managed key — to **every**
authenticated user, including one in no group. This is the production read path, not
a theoretical one: `FileViewer` defaults to `presignVia = 'client'`, and the page
thumbnails, the page-image viewer and the document export all sign S3 GETs in the
browser with those credentials. And two operations that remain `ANY`,
`listDocumentsDateHour` and `listDocumentsDateShard`, return raw tracking-index rows
that carry `ObjectKey` — so a caller can enumerate keys through an `ANY` operation
and fetch the bytes without calling the API at all.

So the accurate statement of what `ANY_GROUP` buys is: **those eleven API
operations** now refuse a caller in no group. The document bytes are not yet behind
a group check, and putting them there means either group-scoped Identity Pool
`RoleMappings` or narrowing that role and routing every read through a resolver —
a change to the document-viewing data path. `UI.T06` in the threat model covers the
key-scoping half of this; the part that needs no resolver at all, and that "any
authenticated user" includes a user in **none**, is recorded here.

⚠️ **This layer gates the REST route only.** Chat streaming is served by a Lambda
Function URL that reaches the chat processors directly, without the dispatcher, and
that transport forwards no `cognito:groups` claim at all. So
`sendChatDocumentMessage` refuses a caller in no group on the REST route, and
`POST /chat/document` on the Function URL does not — recorded as `GAP-07` in
`scripts/api_rbac_expectations.yaml`. The Function URL exists in the commercial
partition only; on GovCloud the UI falls back to the dispatcher plus polling, where
the floor applies.

**An `identity` carried on the event is no longer authoritative for this check.**
`idp_common.api_adapter` used to pass an event carrying its own `arguments` +
`identity` through untouched, so an invocation of that shape chose the groups this
check was made against
([#978](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/978)).
The adapter now builds `identity` from the API Gateway authorizer's verified claims,
refuses an asserted identity that contradicts them, and refuses one presented with
no verified claims at all — a `403`, like any other authorization denial. The one
shape still passed through is an explicitly null `identity`, which is how a
service-to-service invocation gated by IAM on the function ARN identifies itself.
The group-scoped operations deny it, since it carries no groups; the operations
declared `ANY` and several resolver-level checks deliberately skip the Cognito
check for it rather than failing it, IAM being the control on that path.

The manifest
(`http_api_dispatcher/api_rbac_manifest.json`) is **generated** from
`scripts/api_rbac_expectations.yaml` by
`scripts/sdlc/generate_api_rbac_manifest.py`, the same file the static scan and
the live RBAC harness assert against, so there is no separate list to keep in
step; `make api-test-static` fails if the two drift, and the dispatcher treats an
unreadable or unsupported manifest as deny-all.

This layer is a **floor, not a replacement** for the resolver checks in Layer 2:
only the resolver can enforce per-object scope (config version, test set,
ownership), so both run. When triaging a 403, the dispatcher logs `Denied
<field>: ...`; a resolver denial carries the resolver's own message.

**Triaging a deploy where every operation returns 403.** Deny-all is correct when
the manifest is missing, unparseable, of an unsupported version, or carries an
entry in a shape the check cannot evaluate — but per-request it is
indistinguishable from a legitimate denial, so the dispatcher announces the
condition once per cold start at ERROR under the fixed marker
`API_RBAC_MANIFEST_UNAVAILABLE`, followed by the specific cause. Alarm on that
string in the dispatcher's log group: it means the bundled policy file is broken
(a build or packaging fault), not that the callers lack the groups they need. The
status stays 403 rather than becoming a 5xx, because the request genuinely is
unauthorized and a 5xx would invite a retry.

> **⚠️ Adding an API operation now includes declaring its required groups** in
> `scripts/api_rbac_expectations.yaml` and regenerating the manifest. Skip that
> and the new operation returns 403 for every caller, including Admin. Fix it by
> declaring the groups — never by widening an existing entry or letting an
> unmapped field through. See `.claude/skills/api-rbac-test.md`.

### Layer 1: Authentication at the API, and the declared group policy

The UI reaches the backend over an **API Gateway REST API** with a single route,
`POST /op/{field}`, fronted by a **Cognito User Pools authorizer**. That
authorizer **only authenticates**: an unauthenticated or invalid-token request is
rejected with **401** before any code runs, but the authorizer does not know
which Cognito group a field requires. AWS AppSync used to evaluate the schema's
group directives itself; it has been removed (see
[AppSync → REST API Migration](./migration-appsync-to-rest.md) §4), so the group policy is enforced by the dispatcher's
default-deny floor (Layer 0) and, per object, by the resolvers (Layer 2) — never
by the authorizer.

What Layer 1 still contributes is the *declared* policy and the input boundary:

- **The declared policy.** `nested/api-resolvers/src/api/schema.graphql` keeps a
  `@aws_cognito_user_pools(cognito_groups: [...])` directive on every mutation
  and most queries. No AWS service evaluates these directives any more — they are
  the machine-readable **source of truth** for which groups an operation
  requires, and `make api-test-static`
  ([`scripts/sdlc/scan_api_rbac.py`](../scripts/sdlc/scan_api_rbac.py)) fails the
  build when a directive, `scripts/api_rbac_expectations.yaml`, and the resolver
  that actually enforces the check disagree. Keep using
  `@aws_cognito_user_pools(cognito_groups: [...])` when adding an operation, and
  never `@aws_auth(...)`, which the scan does not recognize.
- **Input-shape validation.** The dispatcher validates each request's arguments
  against a build-time spec generated from the same schema
  (`api_validation_spec.json`), rejecting unknown arguments, missing non-null
  arguments, wrong JSON types and out-of-set enum values with **400
  BadRequest** — the boundary check the GraphQL schema used to perform for free.

The tables below therefore list the groups each operation **requires**; the
enforcement itself is Layer 2.

**Key mutations and their allowed roles:**

| Mutation | Allowed Roles |
|----------|---------------|
| `deleteConfigVersion` | Admin |
| `deleteConfigProfileRevision` | Admin |
| `restoreConfigProfileRevision`, `labelConfigProfileRevision` | Admin, Author |
| `createUser`, `updateUser`, `deleteUser` | Admin |
| `updatePricing`, `restoreDefaultPricing` | Admin |
| `updateModelConfigLimits`, `restoreDefaultModelConfigLimits` | Admin |
| `deleteDocument`, `updateConfiguration`, `setActiveVersion` | Admin, Author |
| `uploadDocument`, `reprocessDocument`, `abortWorkflow` | Admin, Author |
| `addTestSet`, `addDocumentsToTestSet`, `listBucketFiles` (import by file pattern searches a whole bucket, so it is not offered to Authors) | Admin |
| `startTestRun`, `addTestSetFromUpload`, `createEmptyTestSet`, `deleteTests`, `deleteTestSets` | Admin, Author |
| `addDocumentsToTestSetFromUpload`, `removeDocumentsFromTestSet`, `updateTestSet`, `publishTestSetVersion` | Admin, Author |
| `syncBdaIdp`, `uploadDiscoveryDocument`, `deleteDiscoveryJob`, `autoDetectSections` | Admin, Author |
| `copyToBaseline` | Admin, Author |
| `createFinetuningJob`, `deleteFinetuningJob` | Admin, Author |
| `processChanges`, `completeSectionReview`, `claimReview`, `releaseReview`, `skipAllSectionsReview` | Admin, Reviewer |
| `sendAgentChatMessage` | Admin, Author, Viewer (Reviewer excluded; also IAM for backend) |
| `deleteChatSession`, `deleteAgentJob` | Any assigned group (`ANY_GROUP`), further session-scoped; see note below |
| `updateChatSessionTitle` | All authenticated users (session-scoped) |
| `updateAgentChatMessage` | All authenticated users (also IAM for backend) |

> **Agent Chat authorization**: `sendAgentChatMessage` and `listAvailableAgents` restrict Agent Chat to **Admin, Author, Viewer** (Reviewer excluded). The restriction is declared in `schema.graphql` **and** enforced server-side in each resolver via a `_caller_in_groups` check — the single REST route's Cognito authorizer only authenticates, so the group gate lives in the resolver. The IAM backend publish path has no Cognito identity and bypasses the check. The session-scoped **reads** (`getChatMessages`, `listChatSessions`) remain open to any authenticated user, bounded by **session scoping** (each user only sees their own sessions); the session-scoped **mutations** (`deleteChatSession`, `deleteAgentJob`) additionally require an assigned group.
>
> *(Previously the Reviewer exclusion was UI-only — tracked as accepted-risk gap GAP-03 — because AppSync could not combine a `cognito_groups` restriction with `@aws_iam` on one field. AppSync has since been removed, so the real groups are now enforced.)*

**Key queries and their allowed roles:**

| Query | Allowed Roles |
|-------|---------------|
| `getDocument`, `listDocuments`, `listDocumentsByDateRange` | Any assigned group (`ANY_GROUP`); server-side row filtering in resolvers on top |
| `getDocumentVersion`, `compareDocumentVersions` | Any assigned group (`ANY_GROUP`) |
| `getFileContents`, `getFilePresignedUrl` | Any assigned group (`ANY_GROUP`); bucket allow-list, **not** key-level scoping |
| `getDocumentCount`, `listDocumentVersions`, `listDocumentsDateHour`, `listDocumentsDateShard`, `getStepFunctionExecution` | All authenticated — counts, run ids and index partitions, not content |
| `getConfigVersions`, `getConfigVersion`, `getPricing`, `getModelConfigLimits`, `calculateCapacity` | Admin, Author, Viewer |
| `listConfigProfileRevisions`, `getConfigProfileRevision` | Admin, Author, Viewer |
| `listAvailableAgents` | Admin, Author, Viewer (Reviewer excluded; enforced server-side — see Agent Chat note above) |
| `listChatSessions`, `getChatMessages`, `getAgentChatMessages` | All authenticated (session-scoped) |
| `submitAgentQuery`, `getAgentJobStatus`, `listAgentJobs` | Admin, Author, Viewer |
| `listConfigurationLibrary`, `getConfigurationLibraryFile` | Admin, Author, Viewer |
| `listDiscoveryJobs` | Admin, Author |
| `getTestRun`, `getTestRuns`, `getTestRunStatus`, `compareTestRuns`, `getTestSets`, `validateTestFileName` | Admin, Author |
| `listFinetuningJobs`, `getFinetuningJob`, `validateTestSetForFinetuning`, `listAvailableModels` | All authenticated (UI limited to Admin, Author) |
| `queryKnowledgeBase` | Any assigned group (`ANY_GROUP`); the resolver itself has no group check (GAP-02), so the dispatcher's floor is the only one |
| `sendChatDocumentMessage` (mutation), `onChatDocumentMessageUpdate` (subscription) | Any assigned group (`ANY_GROUP`) **on the REST route only** — the chat Function URL reaches the same processor with no group claim (GAP-07); the processor enforces per-session ownership and `allowedConfigVersions` scope on the target document |
| `listUsers` | All authenticated (non-admin sees only self in resolver) |
| `getMyProfile` | All authenticated |

**Note**: The `updateConfiguration` mutation is schema-level restricted to Admin+Author, but the resolver additionally enforces that `saveAsVersion` and `saveAsDefault` operations within that mutation are **Admin-only**.

### Layer 2: Server-Side Resolver Group Checks & Filtering

**This is where authorization is enforced.** Each privileged resolver Lambda —
and each RBAC-gated operation the dispatcher serves in process from
`ddb_direct` — reads the caller's `cognito:groups` claim at its entrypoint and
rejects the request with `PermissionError` (returned as **403**, `errorType:
"Unauthorized"`) if the caller is not in an allowed group. The required groups
mirror the Layer 1 tables above (Admin, Admin+Author, or Admin+Reviewer per
operation), and `make api-test-static` fails if any routable operation lacks a
recognized enforcement pattern.

**Identity-based filtering:** Lambda resolvers also apply finer-grained
filtering based on the caller's identity:

**Document Filtering:**
- **Admin**: See all documents
- **Author/Viewer**: See all documents, filtered by `allowedConfigVersions` if scope is set
- **Reviewer-only**: See only HITL-pending documents + their own completed reviews, plus config-version scope

**Configuration Filtering:**
- `getConfigVersions`: Returns only profiles in user's scope (or all if unrestricted)
- `getConfigVersion`: Rejects request if the profile is not in user's scope
- `listConfigProfileRevisions` / `getConfigProfileRevision` / `restoreConfigProfileRevision` / `labelConfigProfileRevision` / `deleteConfigProfileRevision`: Reject the request if the *profile* is not in user's scope

**User Management Filtering:**
- `listUsers`: Admin sees all users; non-admin sees only their own profile
- `getMyProfile`: Returns the calling user's own profile (including `allowedConfigVersions`)

### Layer 3: UI Adaptation (UX Convenience)

The UI adapts based on the user's role and scope:
- Navigation sidebar shows only relevant features per role
- Action buttons (delete, reprocess, upload, save, import) are hidden for roles that can't perform those actions
- Version dropdowns are automatically filtered to show only scoped versions
- The top navigation badge shows the user's role with color coding (blue=Admin, green=Author, grey=Reviewer/Viewer)
- **Admin-only buttons**: "Save as Profile", "Save as Default" in Configuration; Import/Restore/Save in Pricing and Model Limits
- **Pricing page**: Shows "View Pricing" (read-only) for non-admin; "Pricing Configuration" (editable) for admin
- **Model Limits page**: Shows "View Model Limits" (read-only) for non-admin; "Model Limits Configuration" (editable) for admin

**This layer is NOT a security boundary** — it's purely for user experience. Authentication is enforced at Layer 1 and authorization at Layer 2.

## User Management

Admins can create users with any of the four roles via the User Management page. Each user is:
1. Created in DynamoDB (source of truth)
2. Synced to Cognito (for authentication)
3. Added to the appropriate Cognito group (for authorization)
4. Optionally assigned `allowedConfigVersions` for config-version scoping

### User Table Fields

| Field | Description |
|-------|-------------|
| `userId` | Unique identifier (UUID) |
| `email` | User's email address (used as Cognito username) |
| `persona` | Role: Admin, Author, Reviewer, Viewer, or Annotator |
| `status` | User status (active) |
| `allowedConfigVersions` | Optional list of Configuration Profile names (or glob patterns) for scoping |
| `allowedTestSets` | Optional list of test set ids an Annotator may read and annotate. A separate scope axis from `allowedConfigVersions`, not a replacement — a user may carry both. |
| `createdAt` | Creation timestamp |

## Architecture

```
┌─────────────────────────────────┐
│  Browser (UI)                   │  Layer 3: Navigation/button hiding + scope filtering (UX only)
│  useUserRole + getMyProfile     │
│  useConfigurationVersions       │  ← Filters versions by allowedConfigVersions
└────────────┬────────────────────┘
             │ POST /op/{field} (Cognito ID token)
┌────────────▼────────────────────┐
│  API Gateway REST API           │  Layer 1: Cognito User Pools authorizer — authN only (401 if not signed in)
│                                 │           Group directives are DECLARED in schema.graphql, not evaluated by AWS
└────────────┬────────────────────┘
             │
┌────────────▼────────────────────┐
│  HTTP API dispatcher            │  Layer 0: DEFAULT-DENY group check from the generated manifest
│  authz.py + validation.py       │  ← no entry for the field ⇒ 403, before routing
│  api_rbac_manifest.json         │           + input-shape validation from schema.graphql (400 BadRequest)
└────────────┬────────────────────┘
             │
┌────────────▼────────────────────┐
│  Lambda Resolvers / ddb_direct  │  Layer 2: Server-side group checks (403 Unauthorized) + filtering
│  • listDocuments: ConfigVersion │  ← Filters by allowedConfigVersions from UsersTable
│  • getConfigVersions: scope     │  ← Filters profile list
│  • getConfigVersion: scope      │  ← Rejects out-of-scope access
│  • *ConfigProfileRevision*      │  ← Scope checked at the profile
│  • listUsers: self-only         │  ← Non-admin sees only own profile
└────────────┬────────────────────┘
             │
┌────────────▼────────────────────┐
│  DynamoDB                       │
│  TrackingTable (documents)      │
│  ConfigurationTable (versions)  │
│  UsersTable (scope data)        │
└─────────────────────────────────┘
```

## Adding New Roles

To add a new role:
1. Add a `AWS::Cognito::UserPoolGroup` in `template.yaml`
2. Add the group name to relevant `@aws_cognito_user_pools(cognito_groups: [...])` directives in `schema.graphql` (do **not** use `@aws_auth` — see Layer 1 warning), and update the corresponding server-side group check in the resolver Lambda
3. Add the group to the affected operations in `scripts/api_rbac_expectations.yaml` and regenerate the dispatcher manifest (`python3 scripts/sdlc/generate_api_rbac_manifest.py`) — otherwise Layer 0 denies the new role even where the resolver allows it. The operations declared `ANY_GROUP` need **no** edit: the generator resolves that sentinel against the `AWS::Cognito::UserPoolGroup` resources, so the new group is granted them by step 1 alone
4. Update the `VALID_PERSONAS` dict in `src/lambda/user_management/index.py`
5. **Add the group name to `APP_GROUPS`** in `src/ui/src/hooks/use-user-role.ts`, then add its role detection there. ⚠️ `APP_GROUPS` is not cosmetic: `hasNoRole` is computed from it and gates the **whole application**, so a group the server has just granted the `ANY_GROUP` operations (step 3) but that is missing here would be shown "your account has not been granted access yet" and reach nothing. `src/ui/src/hooks/__tests__/use-user-role.appGroups.test.ts` fails when the list and `template.yaml` disagree, so this cannot be missed silently
6. Add navigation items in `src/ui/src/components/genaiidp-layout/navigation.tsx`
7. Pass the new group as an environment variable to the UserManagement Lambda

## Known Limitations

- **Knowledge Base queries** do not currently enforce config-version scope. KB results may include documents from out-of-scope config versions.
- **Agent Companion Chat** analytics queries (Athena) do not filter by config-version scope.
- **GetDocument API** (direct document access by URL) does not enforce config-version scope at the resolver level. UI navigation hides out-of-scope documents, but direct API access is not blocked.
- **Documents with no `ConfigVersion`** are now hidden from scoped users rather than shown (the filters fail closed). If a scoped user reports documents disappearing after an upgrade, those documents were processed before config-version stamping; reprocessing them under a profile in that user's scope restores visibility.
- **Custom Model Fine-tuning** jobs are global — not scoped by `allowedConfigVersions`. A scoped Author can see all fine-tuning jobs and create jobs from any test set. However, when applying a custom model to a configuration version (via the "Create Config Version" modal), the config-version scope IS enforced — the Author can only target versions within their scope.
- These limitations are tracked for Phase 3 implementation.
