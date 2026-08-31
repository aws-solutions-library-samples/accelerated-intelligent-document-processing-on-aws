---
title: "Role-Based Access Control (RBAC)"
---

# Role-Based Access Control (RBAC)

## Overview

The GenAI IDP Accelerator implements a comprehensive Role-Based Access Control system with **server-side enforcement** at the API layer, supplemented by UI-level navigation and action controls for a clean user experience. It also supports **configuration-profile scoping** to restrict non-admin users to specific [Configuration Profiles](configuration-profiles.md) (use cases).

> **Terminology.** What this document used to call a "config version" is now a
> **Configuration Profile**; a **revision** is an immutable snapshot of one
> profile's configuration. The stored field is still named
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

### Layer 1: AppSync Schema Auth Directives (Server-Side)

Every GraphQL **mutation** and many **queries** have `@aws_cognito_user_pools(cognito_groups: [...])` directives that enforce access at the AppSync level. If a user's Cognito group is not in the allowed list, AppSync returns an **Unauthorized** error before any resolver code runs.

> **⚠️ Do NOT use `@aws_auth(cognito_groups: [...])` on this API.** The API has an additional authorization provider (`AWS_IAM`) configured, and on a multi-auth API AppSync **silently ignores** `@aws_auth` — every field decorated with it becomes reachable by *any* authenticated user regardless of group (a Viewer→Admin privilege escalation). Use `@aws_cognito_user_pools(cognito_groups: [...])`, which AppSync *does* evaluate on multi-auth APIs. As defense-in-depth, the required group is **also** enforced server-side in each privileged resolver Lambda (see Layer 2), so an operation is never reachable by an unauthorized caller even if a schema directive regresses.

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
| `startTestRun`, `addTestSet`, `addTestSetFromUpload`, `deleteTests`, `deleteTestSets` | Admin, Author |
| `syncBdaIdp`, `uploadDiscoveryDocument`, `deleteDiscoveryJob`, `autoDetectSections` | Admin, Author |
| `copyToBaseline` | Admin, Author |
| `createFinetuningJob`, `deleteFinetuningJob` | Admin, Author |
| `processChanges`, `completeSectionReview`, `claimReview`, `releaseReview`, `skipAllSectionsReview` | Admin, Reviewer |
| `sendAgentChatMessage` | Admin, Author, Viewer (Reviewer excluded; also IAM for backend) |
| `deleteChatSession`, `updateChatSessionTitle`, `deleteAgentJob` | All authenticated users (session-scoped; see note below) |
| `updateAgentChatMessage` | All authenticated users (also IAM for backend) |

> **Agent Chat authorization**: `sendAgentChatMessage` and `listAvailableAgents` restrict Agent Chat to **Admin, Author, Viewer** (Reviewer excluded). The restriction is declared in `schema.graphql` **and** enforced server-side in each resolver via a `_caller_in_groups` check — the single REST route's Cognito authorizer only authenticates, so the group gate lives in the resolver. The IAM backend publish path has no Cognito identity and bypasses the check. The session-scoped operations (`deleteChatSession`, `getChatMessages`, `listChatSessions`, etc.) remain open to any authenticated user, bounded by **session scoping** (each user only sees their own sessions).
>
> *(Previously the Reviewer exclusion was UI-only — tracked as accepted-risk gap GAP-03 — because AppSync could not combine a `cognito_groups` restriction with `@aws_iam` on one field. AppSync has since been removed, so the real groups are now enforced.)*

**Key queries and their allowed roles:**

| Query | Allowed Roles |
|-------|---------------|
| `getDocument`, `listDocuments`, `listDocumentsByDateRange`, etc. | All authenticated (server-side filtering in resolvers) |
| `getFileContents`, `getStepFunctionExecution` | All authenticated |
| `getConfigVersions`, `getConfigVersion`, `getPricing`, `getModelConfigLimits`, `calculateCapacity` | Admin, Author, Viewer |
| `listConfigProfileRevisions`, `getConfigProfileRevision` | Admin, Author, Viewer |
| `listAvailableAgents` | Admin, Author, Viewer (Reviewer excluded; enforced server-side — see Agent Chat note above) |
| `listChatSessions`, `getChatMessages`, `getAgentChatMessages` | All authenticated (session-scoped) |
| `submitAgentQuery`, `getAgentJobStatus`, `listAgentJobs` | Admin, Author, Viewer |
| `listConfigurationLibrary`, `getConfigurationLibraryFile` | Admin, Author, Viewer |
| `listDiscoveryJobs` | Admin, Author |
| `getTestRun`, `getTestRuns`, `getTestRunStatus`, `compareTestRuns`, `getTestSets`, `listBucketFiles`, `validateTestFileName` | Admin, Author |
| `listFinetuningJobs`, `getFinetuningJob`, `validateTestSetForFinetuning`, `listAvailableModels` | All authenticated (UI limited to Admin, Author) |
| `queryKnowledgeBase` | All authenticated |
| `sendChatDocumentMessage` (mutation), `onChatDocumentMessageUpdate` (subscription) | All authenticated; resolver enforces per-session ownership and processor enforces `allowedConfigVersions` scope on the target document |
| `listUsers` | All authenticated (non-admin sees only self in resolver) |
| `getMyProfile` | All authenticated |

**Note**: The `updateConfiguration` mutation is schema-level restricted to Admin+Author, but the resolver additionally enforces that `saveAsVersion` and `saveAsDefault` operations within that mutation are **Admin-only**.

### Layer 2: Server-Side Resolver Group Checks & Filtering

**Defense-in-depth group enforcement:** In addition to the Layer 1 schema
directives, each privileged resolver Lambda re-checks the caller's
`cognito:groups` claim at its entrypoint and rejects the request if the caller
is not in an allowed group. This ensures a privileged operation is never
reachable by an unauthorized caller even if a schema directive is missing or
misconfigured (for example, a regression back to the silently-ignored
`@aws_auth` directive). The required groups mirror the Layer 1 tables above
(Admin, Admin+Author, or Admin+Reviewer per operation).

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

**This layer is NOT a security boundary** — it's purely for user experience. Security is enforced at Layers 1 & 2.

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
             │ GraphQL
┌────────────▼────────────────────┐
│  REST API + schema directives   │  Layer 1: @aws_cognito_user_pools(cognito_groups) directives (DENY if wrong group)
│  Schema Directives              │
└────────────┬────────────────────┘
             │
┌────────────▼────────────────────┐
│  Lambda Resolvers               │  Layer 2: Server-side group checks (defense-in-depth) + filtering
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
3. Update the `VALID_PERSONAS` dict in `src/lambda/user_management/index.py`
4. Add role detection in `src/ui/src/hooks/use-user-role.ts`
5. Add navigation items in `src/ui/src/components/genaiidp-layout/navigation.tsx`
6. Pass the new group as an environment variable to the UserManagement Lambda

## Known Limitations

- **Knowledge Base queries** do not currently enforce config-version scope. KB results may include documents from out-of-scope config versions.
- **Agent Companion Chat** analytics queries (Athena) do not filter by config-version scope.
- **GetDocument API** (direct document access by URL) does not enforce config-version scope at the resolver level. UI navigation hides out-of-scope documents, but direct API access is not blocked.
- **Documents with no `ConfigVersion`** are now hidden from scoped users rather than shown (the filters fail closed). If a scoped user reports documents disappearing after an upgrade, those documents were processed before config-version stamping; reprocessing them under a profile in that user's scope restores visibility.
- **Custom Model Fine-tuning** jobs are global — not scoped by `allowedConfigVersions`. A scoped Author can see all fine-tuning jobs and create jobs from any test set. However, when applying a custom model to a configuration version (via the "Create Config Version" modal), the config-version scope IS enforced — the Author can only target versions within their scope.
- These limitations are tracked for Phase 3 implementation.
