# Testing Guide for IDP CloudFormation Service Role

This guide provides testing procedures to validate the `IDP-Cloudformation-Service-Role.yaml` CloudFormation service role for GenAI IDP Accelerator management.

## Table of Contents

- [Offline Validation (no AWS calls)](#offline-validation-no-aws-calls)
- [Prerequisites](#prerequisites)
- [Console Deployment Steps](#console-deployment-steps)
- [Test Scenario: Processing-Mode Change](#test-scenario-processing-mode-change)
- [Security Validation](#security-validation)

## Offline Validation (no AWS calls)

Run these before deploying anything. They are the same checks CI runs, and they
catch the two failure modes that matter most: a role too weak to deploy, and a
role broader than it needs to be.

```bash
# Does the role still cover everything the templates need, and are its IAM
# writes bounded and its PassRole scoped? Fails on either.
python scripts/sdlc/validate_service_role_permissions.py

# Template syntax, and the 1024-character cap on Description.
make cfn-lint

# GovCloud portability: no hardcoded arn:aws: or amazonaws.com.
make check-arn-partitions

# Unit tests for the gate itself, including a mutation guard that proves it
# fails on the pre-fix shape of this role.
python -m pytest scripts/sdlc/tests/test_validate_service_role_permissions.py
```

If you add a new AWS resource type to any IDP template, the first command is what
tells you the service role needs a new grant — before a deployment finds out the
hard way, mid-update, where the rollback needs the same missing permission.

## Prerequisites

**IMPORTANT**: The following steps require a user or role with permissions to deploy IAM roles.

You also need an existing IAM **permissions boundary policy**.
`CreatedRolePermissionsBoundaryArn` is a required parameter with no default, and
the same ARN must be passed to the IDP stack you deploy with this role, as its own
`PermissionsBoundaryArn` parameter. See the README's "Read This Before Granting the
Role" section.

The service-role template has a **second** boundary parameter,
`ServiceRolePermissionsBoundaryArn`, which is optional and defaults to empty. It
sets the ceiling on the deployment role *itself*, not on the roles it creates.
Leave it blank for these tests. Passing the tight boundary ARN there makes the role
unable to deploy anything, because a runtime boundary contains neither
`cloudformation:` nor `iam:` — see "Two Boundaries, Two Jobs" in the README.

## Console Deployment Steps

### Step-by-Step Deployment

1. **Navigate to CloudFormation Console**
   - Open the AWS Management Console
   - Go to **CloudFormation** service
   - Select your preferred region

2. **Create New Stack**
   - Click **"Create stack"** → **"With new resources (standard)"**

3. **Specify Template**
   - Select **"Upload a template file"**
   - Click **"Choose file"** and select `IDP-Cloudformation-Service-Role.yaml`
   - Click **"Next"**

4. **Stack Details**
   - **Stack name**: Enter a name for this service-role stack
   - **`CreatedRolePermissionsBoundaryArn`** (required): ARN of the tight
     permissions boundary policy that every role this service role creates must
     carry
   - **`ServiceRolePermissionsBoundaryArn`** (optional): leave **blank** for these
     tests
   - **`ManagedStackNamePrefix`** (default `idp`): the name prefix your IDP stacks
     share. The IDP stack you test with must start with this prefix, and the
     comparison is case-sensitive.
   - Click **"Next"**

5. **Configure Stack Options**
   - **Tags** (optional): Add any desired tags
   - **Permissions**: Leave as default
   - **Stack failure options**: Leave as default
   - Click **"Next"**

6. **Review and Create**
   - Review all settings
   - **Capabilities**: Check **"I acknowledge that AWS CloudFormation might create IAM resources with custom names"**
   - Click **"Submit"**

7. **Monitor Deployment**
   - Wait for stack status to show **"CREATE_COMPLETE"**
   - Check the **Events** tab for any issues

8. **Retrieve the Outputs**
   - Go to the **Outputs** tab
   - Copy `ServiceRoleArn` (the role to pass to CloudFormation),
     `PassRolePolicyArn` (attach to the deploying user), and
     `RequiredPermissionsBoundaryArn` / `RequiredStackNamePrefix` (the two
     constraints the IDP stack must satisfy). `ServiceRoleOwnPermissionsBoundaryArn`
     shows `(none)` when you left the optional second parameter blank.

### Post-Deployment
- The role is now ready to be used with `--role-arn` parameter in CloudFormation deployments via CLI or as a "an existing AWS Identity and Access Management (IAM) service role that CloudFormation can assume" from the Permissions-Optional section in the Cloudformation Console. 
- Users will need `iam:PassRole` permission to use this role — attach the
  `PassRolePolicyArn` managed policy
- The IDP stack must be named with the `ManagedStackNamePrefix` prefix and
  deployed with its `PermissionsBoundaryArn` set to the same ARN you passed as
  `CreatedRolePermissionsBoundaryArn`
- If you are pointing this role at an IDP stack that was deployed **before** this
  hardening landed, read "Updating an Existing Deployment" in the README first —
  three configurations wedge the update in `UPDATE_ROLLBACK_FAILED`, and all three
  are detectable beforehand

## Test Scenario: Processing-Mode Change

**Objective**: Test the CloudFormation service role's ability to deploy and update IDP stacks, including an update that touches IAM roles.

**Prerequisites**: The `PassRolePolicyArn` managed policy created by the role template must be attached to the user or role performing IDP stack changes.

> **Note**: this scenario previously described switching the `IDPPattern`
> parameter between Pattern 1 and Pattern 2. Those separate pattern stacks no
> longer exist — the solution uses a single unified pattern stack whose behaviour
> is selected by the `use_bda` configuration flag. Update a configuration or an
> `Optional` feature parameter instead; what matters for this test is that the
> update adds or modifies at least one IAM role, since that is the permission
> class this role scopes most tightly.

## Console Stack Update with Service Role

### Step 1: Navigate to CloudFormation Console
1. **Open AWS Management Console**
2. **Go to CloudFormation service**
3. **Select your region** (where IDP stack is deployed)

### Step 2: Make a Direct Update
1. **Click "Update stack" button**
2. **Select "Make a direct update"**
3. **Select "Use existing template"**
4. **Click "Next"**

### Step 3: Modify Parameters
1. **Locate a parameter whose change adds or modifies an IAM role** (for example
   enabling an optional feature stack)
2. **Change the value**
3. **Leave other parameters unchanged** — in particular, do **not** clear
   `PermissionsBoundaryArn`; the service role is denied `iam:CreateRole` without it
4. **Click "Next"**

### Step 4: Configure Stack Options with Service Role
1. **Scroll down to "Permissions-optional" section**
2. **For IAM role, choose IDPAcceleratorCloudFormationServiceRole** from dropdown
3. **Leave other options as default**
4. **In the Capabilities section, check both acknowledgements**
5. **Click "Next"**

### Step 5: Review and Execute
1. **Review parameter changes**
2. **Verify service role is selected** in Permissions section
3. **Check "I acknowledge that AWS CloudFormation might create IAM resources"**
4. **Click "Submit"**

### Step 6: Monitor Update Progress
1. **Watch "Events" tab** for real-time progress
2. **Monitor "Resources" tab** for resource changes
3. **Wait for status** to show `UPDATE_COMPLETE`

## Expected Results

### Successful Update
- **Stack Update**: CloudFormation update completes without errors
- **Resource Changes**: The nested stacks and IAM roles the parameter change
  implies are created, modified or removed
- **Functional Resources**: New resources are created and operational

### Role Permission Validation
- **Delegation Success**: A non-administrator user holding only the
  `PassRolePolicyArn` managed policy can start the update
- **No Direct Assumption**: That user **cannot** assume the role itself — the
  trust policy allows only the CloudFormation service principal. An
  `sts:assume-role` attempt should fail with `AccessDenied`; that is the correct
  result, not a misconfiguration.
- **CloudFormation Access**: Full stack management capabilities
- **Service Access**: All required AWS services accessible

## Security Validation

These are the checks that would have caught
[issue #927](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/927).
The first three are offline; the last two need a deployment.

1. **Account isolation**: the role cannot be assumed cross-account, and cannot be
   assumed by a user or role at all.
2. **No unbounded IAM writes**: `python scripts/sdlc/validate_service_role_permissions.py`
   fails if any IAM write action is granted on `Resource: "*"` without an
   `iam:PermissionsBoundary` condition, or if `iam:PassRole` is granted on `"*"`
   or without an `iam:PassedToService` condition.
3. **No boundary removal**: the same gate fails if
   `iam:DeleteRolePermissionsBoundary` is granted anywhere.
4. **Boundary is enforced, not advisory** (live): deploy an IDP stack through the
   service role with `PermissionsBoundaryArn` left empty. It must fail with
   `AccessDenied` on `iam:CreateRole`. If it succeeds, the condition is not
   binding and the containment is illusory.
5. **Name prefix is enforced** (live): attempt an IDP stack whose name does not
   start with `ManagedStackNamePrefix`. It must fail on `iam:CreateRole`. Try a
   case variant too (`IDP-...` against a prefix of `idp`): the `Resource` element
   is matched case-sensitively, so it must also fail.
6. **The pre-update checks for an existing deployment** (live, and the one to run
   *before* you tighten anything): the three configurations that wedge a tightened
   update — roles carrying no boundary, a stack name that does not match the
   prefix, and clearing the IDP stack's `PermissionsBoundaryArn` — each have a
   detection command in ["Updating an Existing
   Deployment"](README.md#updating-an-existing-deployment). Run all three against
   the target stack. A wedged stack recovers only through
   `continue-update-rollback --resources-to-skip`, which skips resources rather than
   fixing them, so detecting beforehand is materially cheaper than recovering.

