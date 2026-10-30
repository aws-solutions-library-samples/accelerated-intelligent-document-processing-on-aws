---
title: "Human-in-the-Loop (HITL) Review"
---

# Human-in-the-Loop (HITL) Review

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [User Management](#user-management)
  - [User Personas](#user-personas)
  - [Managing Users](#managing-users)
- [Workflow](#workflow)
  - [Review Status Values](#review-status-values)
- [Configuration](#configuration)
- [Review Portal](#review-portal)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

## Overview

The GenAI-IDP solution supports Human-in-the-Loop (HITL) review capabilities through a built-in review system integrated directly into the Web UI. This feature enables human reviewers to validate and correct extracted information when the system's confidence falls below a specified threshold, ensuring accuracy for critical document processing workflows.



https://github.com/user-attachments/assets/0bf14b62-99dc-4538-a015-d8b89fa2f1f0



**Supported processing modes:** both.
- BDA mode: Amazon Bedrock Data Automation processing with HITL review
- Pipeline mode: Textract or LLM-based OCR + Bedrock processing with HITL review

**Key Features:**
- Built-in review portal within the GenAI-IDP Web UI
- Role-based access control with Admin and Reviewer personas
- `Annotator` can also reach `claimReview`, `releaseReview` and
  `completeSectionReview`, but only for documents belonging to a test set in its
  `allowedTestSets` — production review documents (those with no `TestSetId`) are
  refused. `skipAllSectionsReview` stays Admin/Reviewer. See [rbac.md](rbac.md).
- Review ownership model - reviewers claim documents before editing
- Section-by-section review workflow
- Visual editor for viewing and correcting extracted data
- Decoupled from document processing workflow for improved reliability

## Architecture

The HITL system operates independently from the document processing workflow:

- **Built-in Review Portal**: Web interface integrated into the GenAI-IDP UI for validation and correction
- **User Management**: Cognito-based authentication with role-based access control
- **Section Review Tracking**: DynamoDB-based tracking of review progress per document section
- **Decoupled Design**: Per-section review actions update tracking status directly; when the final pending section is resolved (completed or skipped), the document is finalized and downstream reprocessing (Summarization/Evaluation) is triggered automatically

### Review Flow

![Review Process Flow](../images/hitl_workflow.png)

## User Management

### User Personas

The system supports two user personas with different permission levels:

#### Admin Persona

Admins have full access to all system features:

| Feature | Access |
|---------|--------|
| View Documents | ✅ Full access |
| Upload Documents | ✅ Allowed |
| View/Edit Extraction Results | ✅ Full access |
| Complete Section Reviews | ✅ Allowed |
| Skip All Reviews | ✅ Allowed on any document outside a review queue (document page **More Actions**) |
| Release Any Review | ✅ Allowed (can release reviews owned by others) |
| View/Edit Configuration | ✅ Full access |
| User Management | ✅ Full access (create/delete users) |
| Discovery | ✅ Full access |
| Analytics & Agents | ✅ Full access |

#### Reviewer Persona

Reviewers have limited access focused on document review tasks:

| Feature | Access |
|---------|--------|
| View Documents | ✅ Unclaimed reviews (pending, or released back to the queue) and reviews they have claimed |
| Upload Documents | ❌ Not allowed |
| Start Review | ✅ Claim ownership of pending documents |
| View/Edit Extraction Results | ✅ Can view and edit (only for owned documents) |
| Complete Section Reviews | ✅ Allowed (only for owned documents) |
| Release Review | ✅ Can release own reviews only |
| Skip All Reviews | ✅ Only on a review that is still waiting (raised, and not completed or skipped), outside a review queue (document page **More Actions**) |
| View/Edit Configuration | ❌ Not allowed |
| User Management | ❌ Not allowed |
| Discovery | ❌ Not allowed |
| Analytics & Agents | ❌ Limited access |

**Note:** Reviewers see only documents that are waiting for review and unclaimed, including a review another reviewer has released back to the queue, or documents they have claimed for review. A review leaves the Reviewer's list once it is completed or skipped. It stays listed for the reviewer who owns it only on documents processed by earlier versions, which carry the legacy `HITLTriggered` flag.

### Managing Users

User management is available to Admin users through the Web UI.

#### Accessing User Management

1. Log in to the GenAI-IDP Web UI with Admin credentials
2. Navigate to **User Management** in the left navigation menu
3. View the list of all users with their email, persona, and status

#### Creating a New User

1. Click the **Create User** button
2. Enter the user's email address
3. Select the persona:
   - **Admin**: Full system access
   - **Reviewer**: Limited access for document review
4. Click **Create**

The system will:
- Create a user record in DynamoDB
- Create a corresponding user in Cognito
- Add the user to the appropriate Cognito group (Admin or Reviewer)
- Send a temporary password to the user's email

#### Deleting a User

1. Find the user in the user list
2. Click the **Delete** button in the Actions column
3. Confirm the deletion

The system will remove the user from both DynamoDB and Cognito.

#### User Synchronization

The system automatically synchronizes users between Cognito and DynamoDB:
- Existing Cognito users are synced to DynamoDB when the user list is loaded
- User persona is determined by Cognito group membership
- Users in the "Admin" group are assigned Admin persona
- All other users are assigned Reviewer persona

## Workflow

### Review Status Values

Documents progress through the following Review Status values:

| Status | Description |
|--------|-------------|
| **Review Pending** | Document is awaiting review, not yet claimed by a reviewer |
| **Review In Progress** | Document has been claimed by a reviewer and is being reviewed |
| **Review Completed** | All sections have been reviewed and approved |
| **Review Skipped** | Admin has skipped all remaining reviews |

### 1. Automatic Triggering

HITL review is automatically triggered when:
- HITL is enabled in your configuration (`hitl.enabled: true`)
- Extraction confidence score falls below the configured threshold
- Document status is set to **Review Pending**

### 2. Review Process

**For Reviewers:**

1. **View Available Documents**: Log in and navigate to the Documents page. You will see only:
   - Documents with status **Review Pending** (available to claim)
   - Documents you have claimed with status **Review In Progress**

2. **Start Review (Claim Document)**:
   - Select a document with **Review Pending** status
   - Click **Start Review** in the document page's action bar (it is shown while the
     review is pending and unclaimed)
   - Document status changes to **Review In Progress**
   - You become the review owner
   - Only a document a review was raised on can be claimed, by any user, Admins
     included: claiming a document with no review waiting is refused with
     `NO_REVIEW_PENDING`, and the document list and document page offer **Start
     Review** only where a review was raised. When a claim is refused, the page shows
     the server's message

3. **Review Sections**:
   - In the document detail view, locate the **Sections** panel
   - Choose **Edit Data** in a section row's **⋮** menu (or **Edit** in the document
     page's action bar) to open the edit page at `#/documents/<document>/edit`
   - Review and correct extracted key-value pairs
   - Click **Save** to save your corrections. Saving does not finish the
     review: the document stays **Review In Progress** and you stay its review owner,
     so you can come back and keep editing

4. **Complete the Review**:
   - Click **Process Changes** on the edit page to finish the review and process the
     changes: evaluation and summarization re-run, and OCR, classification and
     extraction are not repeated. The edit page saves your latest edits first and
     asks you to confirm. (On the document page, **More Actions › Edit Page Grouping ›
     Save and Re-extract** and **More Actions › Re-extract Sections** also complete a
     pending review, after re-extracting the changed sections.)
   - Document status changes to **Review Completed**, and **Review Completed By**
     records you
   - Unless you are an Admin, **Process Changes** applies the same checks as saving:
     you must be the document's review owner (click **Start Review** first), the
     review must not already be finished, the document must not be processing, and
     the document must be inside your configuration-profile scope

5. **Release Review** (optional):
   - If you need to stop reviewing, click **Release Review**
   - Document returns to **Review Pending** status
   - Another reviewer can claim it
   - Only a claimed review can be released: releasing a document nobody has claimed is refused
   - A finished review (completed or skipped) can be neither claimed nor released, by any user, Admins included

**Skipping a review (Admins and Reviewers):**

- **Skip All Reviews**: Choose **More Actions › Skip All Reviews** on the document page
  to mark all pending sections as skipped. It is offered for a pending review outside
  a review queue
  - Choose a reason (for example **Extracted values look correct** or **Duplicate document**) and optionally add a note; both are recorded with the skip in the review history, and the document page shows them as **Skip Reason**
  - Document status changes to **Review Skipped**
  - The **Review Completed By** field records the user who skipped
  - Reviewers can skip too, but only a review that is still waiting: the server refuses a Reviewer's skip on a document whose review has finished or was never raised
  - Downstream reprocessing (Summarization/Evaluation) is triggered automatically — consistent with completing all sections — which also fires the optional post-processing Lambda hook (`PostProcessingLambdaHookFunctionArn`) on workflow completion

**For Admins:**

- **Release Any Review**: Admins can release reviews owned by other users

### 3. Result Integration

When the review is completed with **Process Changes** on the edit page, with a re-extract from the document page, or every section is completed or skipped:
- Document status changes to **Review Completed** or **Review Skipped**
- Corrected data is saved to S3
- **Review Completed By** field records who completed/skipped the review
- Review history is recorded with reviewer information and timestamps

**Note:** Finalizing reviews — **Process Changes** on the edit page, completing the last pending section, or **Skip All Reviews** — automatically triggers document reprocessing to re-run Summarization/Evaluation and fire the optional post-processing Lambda hook on workflow completion. Use **More Actions › Reprocess** on the document page if you later edit data and need to re-run those steps again.

## Configuration

### Enabling HITL

HITL is a **configuration setting**, not a stack parameter. Enable it either in the
Web UI or in the configuration YAML — there is no deploy-time flag:

#### Method 1: Configuration UI (Runtime Toggle)

1. Log in as an Admin user
2. Navigate to **Configuration** in the Web UI
3. Find the **Human-in-the-Loop (HITL) Review (Optional)** section
4. Turn **Enabled** on or off
5. Click **Save** to apply changes

#### Method 2: Configuration YAML (Initial Setup)

Set the HITL fields under the top-level `hitl` key in the configuration passed as
`CustomConfigPath` (or `idp-cli deploy --custom-config`), so a new stack comes up
with HITL already on:

```yaml
hitl:
  enabled: true
  confidence_threshold: 0.8
```

> The `EnableHITL` CloudFormation parameter was removed in v0.4.11. If you are
> upgrading from before that release and had `EnableHITL=true`, re-enable HITL
> through one of the two methods above; the parameter no longer exists and passing
> it is rejected.

### Confidence Threshold Configuration

The confidence threshold determines when human review is triggered:

1. Navigate to **Configuration** in the Web UI
2. Find the **Human-in-the-Loop (HITL) Review (Optional)** section
3. Set **Confidence threshold** (0.0-1.0, the `hitl.confidence_threshold` key):
   - `0.8` = 80% confidence threshold (recommended starting point)
   - Fields with confidence below this threshold trigger HITL review. A confidence that is NaN or infinite is not a usable score and is read as 0.0, so it triggers review too
4. Click **Save** to apply changes

## Review Portal

### Document List View

**For Admins:** All documents are visible with their current status.

**For Reviewers:** Only documents available for review are shown:
- Documents with **Review Pending** status (available to claim)
- Documents with **Review In Progress** status owned by the current reviewer

Document statuses displayed:
- **Review Pending**: Document is awaiting review
- **Review In Progress**: Document is being reviewed
- **Review Completed**: All sections reviewed
- **Review Skipped**: Admin skipped remaining reviews

### Section Review Interface

The section review interface provides:

- **Section List**: All document sections with review status
- A **⋮** menu on each section row:
  - **Edit Data** (or **View Data** for a role that can't edit): opens the section in the edit page
  - **Download Data**: Export extraction results as JSON
  - **Download Baseline**: Export the evaluation baseline, when one exists

### Review Actions

| Action | Available To | Description |
|--------|--------------|-------------|
| **Start Review** | Reviewers, Admins | Claim a pending, unclaimed document for review (document page action bar) |
| **Release Review** | Owner, Admins | Release document back to pending status |
| **Skip All Reviews** | Reviewers (on a review still waiting), Admins | Skip all remaining sections, with a reason (document page **More Actions**) |
| **Process Changes** (edit page) | Owner, Admins | Complete the review and process the changes |

### Document Editor

**Edit Data** (a section row's **⋮** menu) and the document page's **Edit** open the document's edit page, the
same page that review queue items use, at `#/documents/<document>/edit`
(`?section=<id>` opens one section, `?field=<path>` one field). It opens for any
processed document, whether or not HITL is enabled. A document in a review queue
opens its queue item for Admins and Annotators, and read-only for every other role,
saying it is edited in its queue. A document that is still processing opens
read-only. Its breadcrumb is **Home › Documents › {document}** for a direct edit.

The title is the document's name with its status in parentheses, with the full
object key under it. The actions are in one bar at the top: **Edit | View**, the
**Reports** and **More Actions** menus, then the buttons, with **Process Changes** last.
A menu item that doesn't apply yet stays in its menu, disabled, with the reason on
hover.

| Where | Action | What it does | Who |
|---|---|---|---|
| Reports | **History** | The open section's field edits (prediction and baseline) and the document's review activity | everyone |
| Reports | **Summary** | The document's summary, on a processed document that has one | everyone |
| Reports | **Processing Report** | How the open section was extracted | everyone |
| Document Editor panel | **Edit Page Grouping** | Opens the page-grouping board in a dialog, where each section's pages and class are changed | Admin, Reviewer (disabled for other roles, with the reason) |
| More Actions | **Use As Evaluation Baseline** | Copies the processed document's results into the evaluation baseline, as the document page's **More Actions › Use As Evaluation Baseline** does; disabled while a copy is in progress | Admin, Author |
| Button | **Cancel** | Leaves for the document page; asks first when there are unsaved edits, and discards them | editors |
| Button | **Save** | Saves the edits (prediction and baseline) without reprocessing | Admin, Author, and a Reviewer who holds the document's review |
| Button | **Process Changes** | Saves the edits, confirms, then processes them: evaluation and summarization re-run, and OCR, classification and extraction are not repeated | Admin, Reviewer |

A direct edit has no **Skip**: that belongs to a queue item. **Edit | View**, first in
the bar, shows the original file in the editor's place. Summary and Use As Evaluation
Baseline are not offered while the document is still processing or is in a review
queue. The info icon opens a panel describing the screen. The
editor itself is the **Document Editor** panel, with a **Section** dropdown at its
top left. A disabled action
says why on hover. A Reviewer's edits are saved through the review
API and a Reviewer doesn't save baseline edits.

#### Navigation Controls
- **Mouse Wheel Zoom**: Zoom in/out using the mouse wheel without requiring modifier keys
- **Click-and-Drag Panning**: Pan around zoomed images by clicking and dragging (cursor shows grab/grabbing state)
- **Rotate**: Turn a sideways or upside-down page in 90° steps, either direction. The rotated page is re-fitted to the panel rather than cropped, bounding-box overlays rotate with it, the rotation stays applied as you move between pages, and **Reset View** clears it along with zoom and pan
- **Section Navigation**: Choose a section from the **Section** dropdown at the top left of the Document Editor panel; unsaved edits ask before switching

#### Tabbed Interface

**Visual Editor Tab**
- Split-pane layout with document image (left) and form-based field editing (right)
- Bounding box overlay highlighting field locations on the document
- Color-coded confidence indicators (green=meets threshold, red=below threshold, black=no threshold)
- Inline editing with visual change tracking (✏️ Edited badges)

**JSON Editor Tab**
- Raw JSON editing for advanced users
- Section filtering with multiselect dropdown to focus on specific sections
- Full JSON validation before saving

**History › Field Edits** (the editor has no separate Edit history tab)
- A table with a row for every field a save changed: **When**, **By**, **Field**, **From** and **To**, newest first, in the same shape as **Review Activity**
- **Applies to** says whether the prediction or the evaluation baseline changed, when a baseline was edited
- Every save that changes a field adds its rows; a section nobody has edited shows an empty table

#### Editing Features

**Prediction Editing**
- Edit extracted field values directly in the visual editor
- Change tracking with visual indicators (blue left border on modified fields)
- **Save** writes the changes to S3
- **Cancel** discards all unsaved edits, after asking, and leaves the edit page

**Evaluation Baseline Editing** (when evaluation is enabled)
- Edit baseline (expected) values alongside predictions
- Independent change tracking from predictions (orange left border on modified baseline fields)
- Saved and discarded with the prediction edits, by **Save** and **Cancel**
- Side-by-side comparison of predicted vs expected values

**Save & Reprocess Workflow**

After making edits to predictions or baselines, you can trigger reprocessing to re-run downstream steps with the updated data:

1. **Save and process the changes**: Click **Process Changes** on the edit page. It saves your prediction and baseline edits, asks you to confirm, then processes the changes. (**Save** saves them without reprocessing; the document page's **More Actions › Reprocess** re-runs the steps later.)
2. **Confirm**: The confirmation says which steps re-run
3. **Automatic Pipeline Execution**: The document automatically transitions through processing stages:
   - `SUMMARIZING` → Re-generates document summary using updated extraction data
   - `EVALUATING` → Re-runs evaluation comparing updated predictions against baselines
   - `COMPLETE` → Processing finished with updated results
4. **View Updated Results**: Once complete, the evaluation scores and comparison results reflect your edits

**Key Benefits of Save & Reprocess:**
- **Iterative Refinement**: Make corrections and immediately see how they affect evaluation scores
- **Baseline Correction**: Fix ground truth errors and re-evaluate without re-uploading documents
- **Prompt Tuning Workflow**: Edit predictions to match desired output, save as baseline, then use for prompt improvement
- **Quality Assurance**: Verify that corrections properly resolve evaluation mismatches

#### Smart Filtering

- **Low Confidence Filter**: Toggle to show only fields with confidence scores below threshold
- **Evaluation Mismatches Filter**: Toggle to show only fields that don't match baseline (when evaluation enabled)
- **Collapsible Tree Navigation**: Expand/Collapse All buttons for nested data structures
- **Individual Node Toggle**: Click ▶/▼ to expand or collapse specific objects/arrays

#### Evaluation Comparison Mode

When evaluation data is available, turn on **Show Evaluation**:
- Side-by-side display of prediction and baseline values for each field
- Match indicators showing ✓ Match or ⚠ Mismatch status
- Evaluation scores and LLM-generated comparison reasons
- Aggregate scores for nested groups and arrays

## Best Practices

### Review Management

- **Claim Before Editing**: Always use "Start Review" to claim a document before making edits
- **Process Promptly**: Review documents promptly to avoid backlogs
- **Release If Unavailable**: Release reviews if you cannot complete them so others can take over
- **Consistent Standards**: Establish consistent correction guidelines across reviewers
- **Quality Checks**: Implement spot-checks on completed reviews for quality assurance

### Threshold Optimization

- **Start Conservative**: Begin with higher thresholds (0.8-0.9) and adjust based on accuracy needs
- **Monitor Patterns**: Track which document types trigger reviews most frequently
- **Iterative Refinement**: Use review corrections to improve extraction prompts

### User Management

- **Least Privilege**: Assign Reviewer persona to users who only need review access
- **Admin Oversight**: Limit Admin access to users who need full system control
- **Regular Audits**: Periodically review user list and remove inactive users

## Troubleshooting

### Common Issues

**Documents Not Triggering HITL:**
- Verify HITL is enabled in Configuration
- Check confidence threshold settings
- Ensure extraction confidence scores are being calculated

**Cannot Start Review:**
- Verify document is in **Review Pending** status. A document no review was raised on
  cannot be claimed (`NO_REVIEW_PENDING`)
- Check if document is already claimed by another reviewer
- Ensure you have Reviewer or Admin persona

**Cannot Complete Section Review:**
- Verify you are the review owner or have Admin persona
- Check if section is already completed
- Ensure all required fields are filled

**User Cannot See Documents:**
- Reviewers only see unclaimed pending documents (including reviews released back to the queue) and their own in-progress reviews
- Verify user is in the correct Cognito group
- Check user persona in User Management
- Clear browser cache and re-login

**Estimated Cost Shows Zero After Start Review:**
- HITL operations do not re-serialize document data, so **Reports › Estimated Cost** keeps the document's cost; if it shows zero, ensure you have the latest deployment

### Monitoring

Monitor HITL performance through:
- **CloudWatch Metrics**: Track review completion rates
- **Web UI Dashboard**: View document processing status
- **DynamoDB**: Query HITLReviewHistory for audit trails

### Review History

Each completed review is recorded with:
- Section ID
- Reviewer username and email  
- Review completion timestamp
- Action taken (complete, skip_all)

Query the tracking table for `HITLReviewHistory` to audit review activities.

The **Review Completed By** field in the document list shows who completed or skipped the review.
