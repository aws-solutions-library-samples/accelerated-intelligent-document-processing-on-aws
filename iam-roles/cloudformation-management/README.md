# CloudFormation Service Role for GenAI IDP Accelerator

This directory contains the `IDP-Cloudformation-Service-Role.yaml` CloudFormation template that creates a dedicated IAM Cloudformation service role for CloudFormation to deploy, manage and modify all GenAI IDP Accelerator patterns deployments.

## <span style="color: blue;">Administrator Access and Deployment Options</span>

**Note**: As detailed in [docs/deployment.md](../../docs/deployment.md), administrator access is required to deploy the GenAI IDP Accelerator solution. However, this directory provides an example CloudFormation service role that administrators can provision to allow other users to pass this role to CloudFormation for deploying and maintaining the solution stack without themselves needing administrator permissions.

This approach enables a security model where:
- **Administrators** deploy this service role once with their elevated privileges
- **Developer/DevOps users** can then deploy and manage IDP stacks using this pre-provisioned service role
- **Operational teams** can maintain the solution without requiring ongoing administrator access

## <span style="color: blue;">What This Role Does</span>

The **IDPAcceleratorCloudFormationServiceRole** is a CloudFormation service role that provides the necessary permissions for AWS CloudFormation to deploy, update, and manage GenAI IDP Accelerator stacks. The solution now uses a single **unified pattern stack** controlled by the `use_bda` configuration flag — **BDA mode** (Bedrock Data Automation) or **Pipeline mode** (Amazon Textract OCR + Bedrock classification/extraction). This role can only be assumed by the CloudFormation service, not by users directly.

Demo (5 minutes)

### Key Capabilities
- **Full CloudFormation Management**: Create, update, delete IDP stacks - This IAM service role (which CloudFormation assumes) gives necessary privileges to create/update/delete the stack which is helpful in development and sandbox environments. In production environments, admins can further limit these permissions to their discretion (e.g. disabling stack deletion).

- **All Mode Support**: Works with both processing modes of the unified pattern stack — BDA mode (Bedrock Data Automation) and Pipeline mode (Textract + Bedrock)

- **Comprehensive AWS Service Access**: Supports all services required by IDP Accelerator

## <span style="color: blue;">Read This Before Granting the Role</span>

This is a **deployment role, not a least-privilege role** — it does **not** follow
the principle of least privilege, and neither this document nor the template
claims it does
([issue #927](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/927)).
Deciding whether to create this role means understanding both halves of what it
does.

**What it can do.** It holds `cloudformation:*` and a `<service>:*` wildcard on
25 other services, all on `Resource: "*"`. Whoever can pass this role to
CloudFormation can create, modify or delete any resource in those services in
this account — not only the ones belonging to an IDP stack. It can also create
IAM roles and customer managed policies.

**What contains it.** Three mechanisms, in order of how much they actually
constrain:

1. **A required permissions boundary on every role it creates.** The
   `CreatedRolePermissionsBoundaryArn` parameter is mandatory. Every
   `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy` and related grant
   carries an `iam:PermissionsBoundary` condition requiring exactly that policy,
   so **no role created through this identity can exceed the boundary you
   supply.** This is what prevents the role from being a transitive account
   administrator, and it is only as strong as the boundary you choose. Pass the
   same ARN as the IDP stack's own `PermissionsBoundaryArn` parameter.

   This is **not** the boundary attached to the deployment role itself. That is a
   second, separate and optional parameter,
   `ServiceRolePermissionsBoundaryArn` — see
   ["Two Boundaries, Two Jobs"](#two-boundaries-two-jobs). Passing the tight ARN
   as the role's own boundary makes the role unable to deploy anything.
2. **Explicit denies.** Stripping a permissions boundary, editing the boundary
   policy, modifying the service role itself, and creating IAM users, access
   keys, or SAML/OIDC providers are denied outright. An explicit `Deny` cannot be
   overridden by any `Allow`, including a future edit to this template.
3. **Name and destination scoping.** IAM writes and `iam:PassRole` are limited to
   principals whose names begin with `ManagedStackNamePrefix`, and `PassRole`
   additionally requires an `iam:PassedToService` in a fixed list. **Your IDP
   stack name must start with that prefix** — CloudFormation derives generated
   role and policy names from the stack name.

**What is still broad.** `cloudformation:*` and the 25 service wildcards. See
["What Remains Broad, and Why"](#what-remains-broad-and-why) for the reasoning
and for what it would take to narrow them.

**Instance profiles.** The optional bastion host
(`ShouldDeployBastionHost` in `template.yaml`) declares an
`AWS::IAM::InstanceProfile`, which is a distinct IAM resource type with its own
lifecycle actions. Those are granted, scoped to the same name prefix. They do not
support `iam:PermissionsBoundary`, so the prefix is the only scope available —
but the escalation that matters is still contained, because AWS requires the
caller of
[`AddRoleToInstanceProfile`](https://docs.aws.amazon.com/IAM/latest/APIReference/API_AddRoleToInstanceProfile.html)
to hold `iam:PassRole` on the role being added, and this role holds `PassRole`
only for prefix-named roles. `scripts/sdlc/validate_service_role_permissions.py`
now derives this requirement from any `AWS::IAM::InstanceProfile` in the
templates, so a future feature that adds one cannot silently go ungranted.

## <span style="color: blue;">Two Boundaries, Two Jobs</span>

This template takes **two** permissions-boundary ARNs, and they must not be the
same value. They were a single `PermissionsBoundaryArn` parameter until review
found that the two ceilings are not merely different but mutually exclusive, so
no single value could be correct for both.

| Parameter | Required? | What it bounds | How big it must be |
|---|---|---|---|
| `CreatedRolePermissionsBoundaryArn` | **Yes**, no default | Every IAM role this service role creates or re-permissions (the IDP stack's Lambda execution roles, state-machine roles and so on) | **Tight.** No more than the services those roles need at *runtime*. The whole containment argument rests on this being small |
| `ServiceRolePermissionsBoundaryArn` | No, defaults to empty | The deployment role itself | **Wide.** It must admit everything in the role's own policy: `cloudformation:*`, the IAM actions, and all 25 service wildcards |

The concrete failure that forces the split — and the reason one ARN cannot serve
both roles: a boundary sized to "no more than the services an IDP stack's Lambda
functions actually need" contains neither `cloudformation:` nor `iam:`, because no
IDP Lambda function calls either. Effective permissions are the **intersection**
of a principal's policy and its boundary, so attaching that boundary to the
deployment role leaves the role unable to call `cloudformation:CreateStack` or
`iam:CreateRole` — it cannot deploy an IDP stack at all. That is a deterministic
day-one failure, not an edge case.

**The shipped default leaves the deployment role unbounded.**
`ServiceRolePermissionsBoundaryArn` is empty by default, and when it is empty the
template omits the `PermissionsBoundary` property entirely (via
`!Ref AWS::NoValue`) rather than setting it to an empty string. Set it only if a
Service Control Policy in your organization requires every role to carry a
boundary. With it empty, the role's containment comes from four other things: the
`iam:PermissionsBoundary` condition on the roles it creates, the
`ManagedStackNamePrefix` scope on its IAM writes and `PassRole`, the explicit
`Deny` guardrails, and the fact that only the CloudFormation service principal
can assume it.

> **Unverified.** If you *do* set `ServiceRolePermissionsBoundaryArn`, whether the
> deployment role can still deploy an IDP stack depends entirely on the policy
> document you write, and that document does not exist in this repository — there
> is no example boundary policy here to check the intersection against. We can
> state that the shipped default (empty) leaves the role's effective permissions
> equal to its policy, because no boundary is attached at all. We cannot state
> that any particular non-empty boundary is sufficient. Test it on a throwaway
> stack before using it for anything you care about.

Both boundary policies are protected from edits by the
`DenyEditingTheBoundaryPolicy` statement, which lists both ARNs (the second entry
is dropped when no service-role boundary is configured). Without that, the role
could widen its own ceiling by publishing a new default version of the policy.

## <span style="color: blue;">Updating an Existing Deployment</span>

**Read this before updating a service-role stack that was created from an earlier
version of this template.** The hardening in
[issue #927](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/927)
tightened the policy on a role whose **name did not change**. `RoleName` is still
`${AWS::StackName}-CFServiceRole` in both the old and the new template, so
updating the service-role stack in place replaces the policy on the *same role
ARN*. There is no new role, and no per-stack opt-in: every IDP stack that was
deployed with `--role-arn <that ARN>` is governed by the tightened policy from
the moment the update completes, including stacks owned by other teams.

Two things limit the damage, and it is worth being precise about which.
`CreatedRolePermissionsBoundaryArn` is a required parameter with no default, so
the update cannot happen by accident — `update-stack` fails with
`Parameters: [CreatedRolePermissionsBoundaryArn] must have values` unless you
supply it, and the console blocks the wizard. But that gate is on the
*administrator who owns the service-role stack*, not on the IDP stacks
downstream. Nothing warns those stacks' owners.

### Why a new role name is not the fix

The obvious remedy — change `RoleName` so the hardened template creates a new
role and leaves the old one alone — does not work in the same stack. `RoleName`
is a replacement-triggering property on `AWS::IAM::Role`, so updating the stack
with a different name **deletes the old role** once the new one exists. Every IDP
stack still pointing at the old ARN then fails its next operation with
`Role arn:...:role/<stack>-CFServiceRole is invalid or cannot be assumed`, which
is a worse outcome than a tightened policy: an `AccessDenied` on one IAM call can
be diagnosed and recovered, a deleted service role cannot be recovered by
re-running anything.

**The opt-in you want is a new stack name, not a new role name.** Because the
role name already derives from the stack name, deploying the hardened template as
a *second* service-role stack gives you a second role
(`<new-stack-name>-CFServiceRole`) while the original stack and its original role
keep working untouched. You then migrate one IDP stack at a time with
`aws cloudformation update-stack --stack-name <idp> --role-arn <new-role-arn>
--use-previous-template`, and if a stack fails you re-point it at the old role.
This needs no template change, which is why the remedy here is documentation
rather than code — but it does need to be documented, because nothing in the
template forces it.

The cost to you: create the tight boundary policy; deploy a second service-role
stack; attach its `PassRolePolicyArn` managed policy to your deployers (the old
one keeps working for the old role); resolve the two preconditions below for each
IDP stack; run one `update-stack --role-arn` per IDP stack; and, once every stack
has moved, delete the old service-role stack. The expensive case is an IDP stack
whose name cannot be made to match a prefix — see condition B.

### The three ways a tightened update wedges a stack

Each condition below gives the detection command to run **before** you update,
the failure signature if you do not, and the recovery.

#### A. Existing roles carry no permissions boundary

The IDP stack's own `PermissionsBoundaryArn` parameter is **optional** and
defaults to the empty string (`template.yaml`, `PermissionsBoundaryArn`, `Default:
""`), and the previous version of this service role required no boundary and
imposed no condition — it granted `iam:CreateRole`, `iam:PutRolePolicy`,
`iam:AttachRolePolicy` and 18 other IAM actions on `Resource: '*'` with no
`Condition` block at all. So the normal case for an existing deployment is that
**every role the IDP stack created has no permissions boundary.**

The hardened `CreateOrChangeRolesOnlyWithBoundary` statement grants
`iam:PutRolePolicy`, `iam:AttachRolePolicy`, `iam:DetachRolePolicy`,
`iam:DeleteRolePolicy`, `iam:CreateRole` and `iam:PutRolePermissionsBoundary`
only under `StringEquals { iam:PermissionsBoundary: <the tight ARN> }`. The
`iam:PermissionsBoundary` key
[checks that the specified policy is attached as permissions boundary on the IAM
principal resource](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_iam-condition-keys.html),
and AWS's own description of this delegation pattern says the statement "allows
[the delegate] to manage permissions policies for users **with this permissions
boundary set**". A role with no boundary does not populate the key, a plain
`StringEquals` on an absent key does not match, and no other statement allows the
action — so the call is denied.

- **Detect.** For each IDP stack, list the roles it owns and check each one's
  boundary. Roles created by nested stacks are named after the parent stack, so a
  single prefix filter finds them:

  ```bash
  aws iam list-roles \
    --query "Roles[?starts_with(RoleName, '<idp-stack-name>')].RoleName" \
    --output text \
  | tr '\t' '\n' \
  | while read -r r; do
      printf '%s\t%s\n' "$r" \
        "$(aws iam get-role --role-name "$r" \
             --query 'Role.PermissionsBoundary.PermissionsBoundaryArn' \
             --output text)"
    done
  ```

  Any line ending in `None` is a role that will fail. If **every** line ends in
  `None`, the stack was deployed with no boundary and every IAM write will fail.
- **Failure signature.** `AccessDenied` on `iam:PutRolePolicy` (or
  `iam:AttachRolePolicy`) naming the assumed-role session
  `arn:<partition>:sts::<account>:assumed-role/<stack>-CFServiceRole/AWSCloudFormation`,
  reported against a resource ARN that *does* match the name prefix — which is
  what distinguishes this from condition B. The rollback needs the same action, so
  the stack lands in `UPDATE_ROLLBACK_FAILED` and comes out only with
  `continue-update-rollback --resources-to-skip`. This is the failure mode of
  [issue #632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632).
- **Recover / prevent.** Attach the boundary to every existing role **with your
  own administrator credentials, before switching the stack to the new role**:
  `aws iam put-role-permissions-boundary --role-name <r> --permissions-boundary
  <tight-arn>` for each role from the detection loop, then re-run the loop to
  confirm no `None` remains, then set the IDP stack's own
  `PermissionsBoundaryArn` to the same ARN so future roles carry it. Do not rely
  on the stack update to do this for itself: `iam:PutRolePermissionsBoundary` on a
  boundary-less role *is* allowed (for that action the condition key reflects the
  boundary in the request), but CloudFormation does not guarantee it will set the
  boundary on a role before it edits that role's policies, so a single update can
  still fail on the role it has not reached yet.

#### B. The IDP stack name does not match `ManagedStackNamePrefix`

`ManagedStackNamePrefix` did not exist in the previous template, so existing IDP
stacks were named without reference to it. Its default is `idp`, and every IAM
write and every `iam:PassRole` is scoped to
`arn:<partition>:iam::<account>:role/<prefix>*`. A stack named `GenAIIDP`,
`docproc-prod` or anything else not beginning with the prefix produces role names
that no `Resource` entry matches.

Matching is **literal and case-sensitive**: in the `Resource` element
[the IAM entity name is case sensitive](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_elements_resource.html),
so a stack named `IDP-prod` does **not** match a prefix of `idp`. Two facts sit
next to each other here and are easy to confuse. IAM treats role names as
case-insensitive for *uniqueness* — you cannot create both `Role1` and `role1` —
but a policy's `Resource` element compares the name *case-sensitively*. So `IDP`
and `idp` are the same name for the purpose of creating a role and different
strings for the purpose of authorizing one.

- **Detect.** Compare literally, without lowercasing either side:

  ```bash
  aws cloudformation describe-stacks --stack-name <idp-stack-name> \
    --query 'Stacks[0].StackName' --output text
  ```

  and check that the result starts with the exact `ManagedStackNamePrefix` you
  intend to pass. The value in force on an existing service-role stack is echoed
  in its `RequiredStackNamePrefix` output.
- **Failure signature.** `AccessDenied` on `iam:CreateRole` or `iam:PutRolePolicy`
  where the resource ARN in the message does **not** begin with
  `role/<prefix>`. Same `UPDATE_ROLLBACK_FAILED` risk as condition A.
- **Recover.** Redeploy the service-role stack with `ManagedStackNamePrefix` set
  to a prefix that actually matches your existing stack names — the parameter
  accepts 1 to 32 characters, so an existing fleet can usually be accommodated by
  choosing the longest common prefix of the names you already have. If your stacks
  share no usable prefix, deploy one service-role stack per prefix; each gets its
  own role. Renaming an IDP stack is not an update, it is a delete and recreate,
  which means new buckets, new tables and a data migration — avoid needing it.

#### C. Clearing the IDP stack's `PermissionsBoundaryArn`

The previous version of this service role **granted**
`iam:DeleteRolePermissionsBoundary`. The hardened version does not grant it and
additionally denies it in `DenyStrippingAnyPermissionsBoundary`, on `Resource:
'*'`, and an explicit `Deny` cannot be overridden by any `Allow`. So an operation
that used to succeed now cannot succeed at all.

The trigger is specific: the IDP stack sets `PermissionsBoundary` with
`!If [HasPermissionsBoundary, !Ref PermissionsBoundaryArn, !Ref AWS::NoValue]`.
Changing `PermissionsBoundaryArn` from an ARN to the empty string makes
`AWS::NoValue` remove the property, and CloudFormation implements removing that
property by calling `iam:DeleteRolePermissionsBoundary` on every affected role.

- **Detect.** Before the update, check what the stack currently has and what you
  are about to set:

  ```bash
  aws cloudformation describe-stacks --stack-name <idp-stack-name> \
    --query "Stacks[0].Parameters[?ParameterKey=='PermissionsBoundaryArn']" \
    --output text
  ```

  If that is non-empty and your change would blank it, this condition applies.
- **Failure signature.** `AccessDenied` on `iam:DeleteRolePermissionsBoundary`
  with an explicit-deny message. Again the rollback needs the same call.
- **Recover.** Do not make this change through the service role. Going from
  bounded to unbounded is a deliberate reduction in containment, so make it with
  your own credentials (`update-stack` without `--role-arn`, or
  `aws iam delete-role-permissions-boundary` per role). The role will never do it
  for you, by design — that is the point of the `Deny`.

### The generic recovery, for all three

If an update has already wedged a stack: read the failing resource's status
reason in the **Events** tab, then
`aws cloudformation continue-update-rollback --stack-name <idp> --resources-to-skip
<LogicalIds>` to get out of `UPDATE_ROLLBACK_FAILED`, then re-point the stack at
the old role ARN (or run the update with your own credentials and no
`--role-arn`) to restore a working baseline before trying again. Skipping
resources leaves the stack's recorded state out of step with reality for those
logical IDs; the next successful update reconciles them, so verify the resource
afterwards rather than assuming.

## <span style="color: blue;">Security Features</span>

### Session Management
- **Administrator Note**: This role also creates an IAM Managed Policy to allow passing the Cloudformation service role.  Administrators must attach this managed policy to users wanting to deploy or modify CloudFormation IDP stacks with this service role, allowing them to pass the service role to the CloudFormation principal:

  ```yaml
  PassRolePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      ManagedPolicyName: IDP-PassRolePolicy
      Description: Policy to allow passing the IDP CloudFormation service role
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Action:
              - iam:PassRole
            Resource: !GetAtt CloudFormationServiceRole.Arn
            # Without this condition, the grant lets the holder hand a
            # role-creating identity to ANY service that accepts one, not
            # just to CloudFormation.
            Condition:
              StringEquals:
                iam:PassedToService: !Sub 'cloudformation.${AWS::URLSuffix}'
  ```

  You can tighten this further on the deploying principal's own policy with a
  `cloudformation:RoleArn` condition, which restricts *which* service role that
  user may pass and *which* stacks they may pass it to.

### Access Control
- **Account-Scoped**: The trust policy allows only the CloudFormation service
  principal, and only for stack operations in this account.
- **Confused-deputy conditions**: The trust policy applies
  `aws:SourceAccount` and `aws:SourceArn` conditions when CloudFormation supplies
  them, using `StringEqualsIfExists` and `ArnLikeIfExists` in a **single**
  statement so that the absent case is allowed without a second statement. See
  ["Why the trust policy is one statement, not two"](#why-the-trust-policy-is-one-statement-not-two).


## <span style="color: blue;">Files in this Directory</span>

- `IDP-Cloudformation-Service-Role.yaml` - CloudFormation service role template 
- `README.md` - This documentation file
- `testing-guide.md` - Testing procedures and validation steps

## <span style="color: blue;">Console Deployment Steps</span>

### Prerequisites
- AWS Administrator access or IAM permissions to create roles and policies
- **An existing IAM permissions boundary policy for the roles this role creates.**
  `CreatedRolePermissionsBoundaryArn` is a required parameter with no default;
  create the boundary policy first. It should allow no more than the services an
  IDP stack's Lambda functions actually need at runtime, because it is the ceiling
  on every role this service role creates. If you have no boundary policy yet,
  start from the runtime permissions documented in
  [../docs/aws-services-and-roles.md](../../docs/aws-services-and-roles.md).
  **Do not also pass this ARN as `ServiceRolePermissionsBoundaryArn`** — that is a
  different, optional parameter with an incompatible sizing requirement, and
  passing the tight ARN there stops the role deploying anything. See
  ["Two Boundaries, Two Jobs"](#two-boundaries-two-jobs).
- **A stack naming convention.** Decide the prefix your IDP stack names will
  share and pass it as `ManagedStackNamePrefix` (default `idp`). Keep it short —
  12 characters or fewer is a good rule — because CloudFormation truncates
  generated role names at the 64-character IAM limit and only the leading
  characters are guaranteed to survive.

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
   - **Stack name**: Enter a name for this service-role stack (this is *not* the
     IDP stack name; it only determines the role name,
     `<StackName>-CFServiceRole`)
   - **`CreatedRolePermissionsBoundaryArn`** (required): the ARN of the **tight**
     boundary policy that will bound every role this service role creates, e.g.
     `arn:aws:iam::123456789012:policy/IDPRuntimeBoundary`. This is the same ARN
     you will pass to the IDP stack's own `PermissionsBoundaryArn`
   - **`ServiceRolePermissionsBoundaryArn`** (optional, leave **blank**): a
     separate, **wide** boundary attached to the deployment role itself. Set it
     only if an SCP requires every role to carry a boundary, and never to the same
     value as the previous parameter
   - **`ManagedStackNamePrefix`** (default `idp`): the shared name prefix of the
     IDP stacks this role may deploy
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
   - `ServiceRoleArn` — pass this to CloudFormation as the stack's service role
   - `PassRolePolicyArn` — attach this managed policy to whoever will deploy
   - `RequiredStackNamePrefix` and `RequiredPermissionsBoundaryArn` — the two
     constraints every IDP stack deployed with this role must satisfy
   - `ServiceRoleOwnPermissionsBoundaryArn` — the boundary on the deployment role
     itself, or `(none)` when you left `ServiceRolePermissionsBoundaryArn` blank.
     This exists so the two ceilings can be told apart from the outputs alone

### Post-Deployment
- The role is now ready to be used with `--role-arn` parameter in CloudFormation deployments via CLI or as a "an existing AWS Identity and Access Management (IAM) service role that CloudFormation can assume" from the Permissions-Optional section in the Cloudformation Console. 
- Users will need `iam:PassRole` permission to use this role — attach the
  `PassRolePolicyArn` managed policy this stack creates
- **The IDP stack you deploy with this role must**: (a) have a name starting with
  `ManagedStackNamePrefix`, and (b) set its own `PermissionsBoundaryArn` parameter
  to the same ARN you passed as `CreatedRolePermissionsBoundaryArn`. Both are
  enforced by the role's policy, so a mismatch surfaces as `AccessDenied` on
  `iam:CreateRole` — see [Troubleshooting](#troubleshooting).
- **If you are pointing this role at an IDP stack that already exists**, read
  ["Updating an Existing Deployment"](#updating-an-existing-deployment) first. Both
  constraints above are new, and an existing stack satisfies neither by default.

```bash
aws cloudformation deploy \
  --stack-name idp-prod \
  --template-file <idp-template> \
  --role-arn "$(aws cloudformation describe-stacks --stack-name <this-stack> \
      --query 'Stacks[0].Outputs[?OutputKey==`ServiceRoleArn`].OutputValue' \
      --output text)" \
  --parameter-overrides PermissionsBoundaryArn=<the-CreatedRolePermissionsBoundaryArn-value> \
  --capabilities CAPABILITY_NAMED_IAM
```

The IDP stack's parameter is still called `PermissionsBoundaryArn`; only this
role's template renamed its copy to `CreatedRolePermissionsBoundaryArn`, to
distinguish it from the role's own boundary. The two must hold the same value.

## <span style="color: blue;">AWS Service Permissions</span>

The role grants `cloudformation:*`, a **scoped and conditioned** set of IAM actions, and a wildcard (`<service>:*`) on **25 other AWS services**. 24 of those back at least one CloudFormation resource type the solution declares today. The 25th, `appsync:*`, is **vestigial — retained for upgrades only**, because deleting what a pre-0.6.0 template created is also this role's job. Below is a detailed breakdown organized by category.

Three wildcards that earlier versions of this role carried have been removed
because nothing in the solution declares a resource in them: **Textract** and
**SageMaker** (runtime-only, called by Lambda execution roles rather than
CloudFormation; MLflow is referenced by ARN and uses the distinct
`sagemaker-mlflow:` prefix), and **Application Auto Scaling** (no scalable
target or scaling policy is declared anywhere, and all 19 DynamoDB tables are
`PAY_PER_REQUEST`). See the collapsed sections below for the evidence in each
case.

> **A wildcard that backs no current resource type is not automatically dead** —
> which is why `appsync:*` is **retained for upgrades only**, with nothing in the
> current templates declaring an `AWS::AppSync::*` resource. A CloudFormation
> service role performs
> **deletions** as well as creations, so it must be able to delete resource
> types that only an **older** template declared. Deriving the required action
> set from the current templates alone — which is what
> `scripts/sdlc/validate_service_role_permissions.py` does — cannot see that
> obligation. Applied consistently the same reasoning would justify dropping
> `apigateway:*` the day the UI transport changes again, while stacks still
> exist that need their API Gateway resources deleted.

### Services Summary

| Category | Services Count | Services |
|----------|---------------|----------|
| Core Infrastructure | 2 | CloudFormation, IAM (scoped — see below) |
| Compute & Serverless | 3 | Lambda, Step Functions, CodeBuild |
| AI/ML Services | 1 | Bedrock |
| Storage Services | 3 | S3, DynamoDB, ECR |
| API & Application | 2 | API Gateway, AppSync (vestigial — upgrade-only, see below) |
| Security & Identity | 5 | Cognito User Pools, Cognito Identity, KMS, Secrets Manager, WAF v2 |
| Messaging & Events | 4 | SNS, SQS, EventBridge, EventBridge Scheduler |
| Monitoring & Management | 3 | CloudWatch, CloudWatch Logs, Systems Manager |
| Analytics & Data | 2 | Glue, OpenSearch Serverless |
| Networking & CDN | 2 | CloudFront, EC2 (VPC) |

### Complete Service List

`Full Access` below means literally `<service>:*` on `Resource: "*"`. Read
["What Remains Broad, and Why"](#what-remains-broad-and-why) before treating
this role as least-privilege — it is not.

| Service | Access | Utility |
|---------|--------|---------|
| CloudFormation | Full Access | Full stack management, including nested stacks and change sets |
| IAM | **Scoped** — specific actions, name-prefixed resources, permissions-boundary and PassedToService conditions | Role and policy management for IDP components |
| Lambda | Full Access | Function creation and management |
| Step Functions | Full Access | State machine orchestration |
| CodeBuild | Full Access | Build automation for custom container images |
| Bedrock | Full Access | Foundation models, Data Automation projects, Knowledge Bases |
| S3 | Full Access | Bucket and object management |
| DynamoDB | Full Access | Table and data management |
| ECR | Full Access | Container image registry |
| API Gateway | Full Access | REST and HTTP API management — including the UI ⇄ backend REST API |
| AppSync | Full Access | Retained for backward compatibility only: deleting the GraphQL API a pre-migration (v0.5.x) stack created, when such a stack is upgraded in place. No current template creates an AppSync resource |
| Cognito User Pools | Full Access | User authentication and management |
| Cognito Identity | Full Access | Federated identity and temporary credentials |
| KMS | Full Access | Encryption key management |
| Secrets Manager | Full Access | Secure credential storage |
| WAF v2 | Full Access | Web application firewall |
| SNS | Full Access | Notification services |
| SQS | Full Access | Message queue management |
| EventBridge | Full Access | Event-driven workflow triggers |
| EventBridge Scheduler | Full Access | Scheduled task management |
| CloudWatch | Full Access | Metrics, alarms, and dashboards |
| CloudWatch Logs | Full Access | Centralized logging |
| Systems Manager (SSM) | Full Access | Parameter Store configuration |
| Glue | Full Access | Data catalog and ETL jobs |
| OpenSearch Serverless | Full Access | Vector search for embeddings |
| CloudFront | Full Access | CDN for web hosting and API acceleration |
| EC2 (VPC) | Full Access (`ec2:*`) | VPC, subnet, security group and interface-endpoint management for the private hosting variants, plus the optional bastion host. |
| AppSync | Full Access — **vestigial, retained for upgrades only** | Nothing declares an `AWS::AppSync::*` resource today. The grant exists so an in-place update of a pre-0.6.0 stack can **delete** the GraphQL API, schema, data sources and resolvers that older template created. Droppable once no pre-0.6.0 stack remains |
| ~~Textract~~ | Removed | Runtime-only; no CloudFormation resource type |
| ~~SageMaker~~ | Removed | Runtime-only; MLflow server referenced by ARN, not created |
| ~~Application Auto Scaling~~ | Removed | No scalable target or scaling policy declared |

---

### Detailed Service Breakdown

#### Core Infrastructure Services

<details>
<summary><strong>AWS CloudFormation</strong> (<code>cloudformation</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Stack management for IDP infrastructure deployment

**Actions Granted**:
```
cloudformation:*
```
- `CreateStack`, `UpdateStack`, `DeleteStack`
- `DescribeStacks`, `DescribeStackEvents`, `DescribeStackResources`
- `GetTemplate`, `ValidateTemplate`
- `CreateChangeSet`, `ExecuteChangeSet`, `DeleteChangeSet`
- `ListStacks`, `ListStackResources`
- All other CloudFormation operations

</details>

<details>
<summary><strong>AWS IAM</strong> (<code>iam</code>) — scoped, not <code>iam:*</code></summary>

**Permission Level**: Specific actions, split across twelve statements — seven `Allow` and five `Deny` — each scoped by resource and/or condition, grouped into the eight headings below. This is the only service in the template where the grant is genuinely narrowed rather than wildcarded, because IAM is the only one where a wide grant makes the role an account administrator.

**Purpose**: Create and manage the IAM roles and customer managed policies that the IDP stack's Lambda functions, state machines and service integrations need.

**1. Create or re-permission a role — only with the permissions boundary**

```
iam:CreateRole
iam:PutRolePermissionsBoundary
iam:PutRolePolicy
iam:DeleteRolePolicy
iam:AttachRolePolicy
iam:DetachRolePolicy
```
- **Resource**: `arn:<partition>:iam::<account>:role/<ManagedStackNamePrefix>*` (and the same under an IAM path)
- **Condition**: `StringEquals { iam:PermissionsBoundary: <CreatedRolePermissionsBoundaryArn> }`

Every one of these actions supports the `iam:PermissionsBoundary` condition key, which is what makes this containment real: a role created or re-permissioned through this identity cannot exceed the boundary you supply, no matter what policy the template attaches to it. This is the delegation pattern from [Delegating responsibility to others using permissions boundaries](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html#access_policies_boundaries_delegate) in the IAM User Guide, and this statement is the direct analogue of the `CreateOrChangeOnlyWithBoundary` statement in that example.

The IDP templates set `PermissionsBoundary` on every role they create — the explicit `AWS::IAM::Role` resources and the roles SAM generates for each `AWS::Serverless::Function` alike — so this condition holds for a real deployment **provided you deploy the IDP stack with the same `PermissionsBoundaryArn`**. Deploying the IDP stack with an empty `PermissionsBoundaryArn` while using this service role fails on `iam:CreateRole`, by design.

⚠️ The same condition also governs the four actions that *modify* an existing role, and `iam:PermissionsBoundary` reflects the boundary already attached to the role being modified. A role that carries no boundary therefore cannot be modified through this identity at all. Every role created before this hardening is in that state, which is why adopting this role on an existing deployment needs the procedure in ["Updating an Existing Deployment"](#updating-an-existing-deployment) rather than a plain stack update.

**2. Role lifecycle — scoped by name only**

```
iam:DeleteRole
iam:UpdateRole
iam:UpdateAssumeRolePolicy
iam:TagRole
iam:UntagRole
```
- **Resource**: same `<ManagedStackNamePrefix>*` role ARNs
- **Condition**: none

**Why these five are not conditioned on `iam:PermissionsBoundary`, and it is not because the key is unsupported.** Checked against the Service Authorization Reference, `iam:DeleteRole`, `iam:UpdateRole` and `iam:UpdateAssumeRolePolicy` **do** list `iam:PermissionsBoundary` among their action-level condition keys; only `iam:TagRole` and `iam:UntagRole` do not. Do not conclude otherwise from AWS's canonical delegation example, which is written for IAM **users**, where `iam:DeleteUser` genuinely has no action-level condition keys.

The real reason is rollback safety, and it is an admission of something unverified rather than a design argument. What is not documented is whether the key is *populated* when the target role carries **no** boundary. If it fails closed, then conditioning `iam:DeleteRole` would deny deleting any boundary-less role — and every role predating this template is boundary-less. That would fail the delete **and** the rollback of the delete, wedging the stack in `UPDATE_ROLLBACK_FAILED` (the failure mode of [issue #632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632)). We could not establish the populated-versus-absent behaviour without a live account, so these five stay bounded by the role-name prefix alone. Revisit it with a real experiment, not by reasoning about the reference tables.

`iam:DeleteRolePermissionsBoundary` is **not** granted and is additionally denied (see statement 8): the ability to strip a boundary would defeat the boundary.

**3. Pass a role to the service that will use it**

```
iam:PassRole
```
- **Resource**: same `<ManagedStackNamePrefix>*` role ARNs — never `*`
- **Condition**: `StringEqualsIfExists { iam:PassedToService: [ apigateway, bedrock, bedrock-agentcore, cloudfront, cloudwatch, codebuild, cognito-identity, ec2, events, glue, lambda, logs, scheduler, states ] }`

**One statement with `StringEqualsIfExists`, not two.** Not every service populates `iam:PassedToService`, and a plain `StringEquals` would give `AccessDenied` on the pass for any service that omits it — and would wedge the rollback too. This used to be two statements: a plain `StringEquals` on the list, plus a second statement whose condition was `Null { iam:PassedToService: true }` to allow the absent case. The collapse is exactly behaviour-preserving. The pair allowed "key absent" **or** "key present and in the list", and denied "key present and not in the list"; that is precisely what `StringEqualsIfExists` evaluates for a single key.

**Three entries were removed because they could never match.** `dynamodb`, `indexing.s3vectors` and `logging.s3` were never `PassRole` targets at all — they are *resource-policy* principals from elsewhere in `template.yaml`, transcribed into this list by mistake. `dynamodb` and `indexing.s3vectors` appear in the `Principal` element of the `CustomerManagedEncryptionKey` KMS key policy, and `logging.s3` in the `Principal` element of `LoggingBucketPolicy`. A key-policy or bucket-policy principal never appears in `iam:PassedToService`, so removing them narrows nothing real and stops the list misleading the reader about what this role hands roles to.

**`cognito-identity` was kept**, although review suggested it was inert for the same reason. It is not: `AWS::Cognito::IdentityPoolRoleAttachment` in `template.yaml` genuinely hands `CognitoAuthorizedRole`'s ARN to Cognito Identity, so a role really is passed there. What could not be established is whether that API populates the condition key. The risk is one-sided — if it does populate the key, removing the entry breaks the identity-pool attachment; if it does not, `StringEqualsIfExists` covers the pass anyway and the entry costs nothing — so the entry stays.

The statement stays bounded by the role-name prefix regardless of the condition, so a service that omits the key still cannot be handed a role outside `<ManagedStackNamePrefix>*`.

**4. Service-linked roles**

```
iam:CreateServiceLinkedRole
iam:DeleteServiceLinkedRole
```
- **Resource**: `arn:<partition>:iam::<account>:role/aws-service-role/*`

Service-linked roles live at a fixed IAM path and **cannot carry a permissions boundary** (`PutRolePermissionsBoundary` rejects them), so they get their own path-scoped statement. Their permissions are defined by AWS, not by this role.

**5. Instance profiles — scoped by name only**

```
iam:CreateInstanceProfile
iam:DeleteInstanceProfile
iam:AddRoleToInstanceProfile
iam:RemoveRoleFromInstanceProfile
iam:TagInstanceProfile
iam:UntagInstanceProfile
```
- **Resource**: `arn:<partition>:iam::<account>:instance-profile/<ManagedStackNamePrefix>*`

Only the optional bastion host declares an `AWS::IAM::InstanceProfile`, and CloudFormation names it after the stack, so the same prefix applies. None of these actions supports `iam:PermissionsBoundary`. The escalation that matters is nonetheless contained: AWS requires the caller of [`AddRoleToInstanceProfile`](https://docs.aws.amazon.com/IAM/latest/APIReference/API_AddRoleToInstanceProfile.html) to hold `iam:PassRole` on the role being added, and statement 3 grants `PassRole` only for prefix-named roles.

**6. Customer managed policies**

```
iam:CreatePolicy
iam:DeletePolicy
iam:CreatePolicyVersion
iam:DeletePolicyVersion
iam:SetDefaultPolicyVersion
iam:TagPolicy
iam:UntagPolicy
```
- **Resource**: `arn:<partition>:iam::<account>:policy/<ManagedStackNamePrefix>*`

**7. Read-only IAM** — on `Resource: "*"`, because these grant no ability to change anything

```
iam:GetRole              iam:GetPolicy
iam:GetRolePolicy        iam:GetPolicyVersion
iam:ListRoles            iam:ListPolicies
iam:ListRolePolicies     iam:ListPolicyVersions
iam:ListAttachedRolePolicies
iam:ListRoleTags
iam:GetInstanceProfile   iam:ListInstanceProfiles
iam:ListInstanceProfilesForRole
```

**8. Explicit denies** — a `Deny` cannot be overridden by any `Allow`, including a future edit to this template

| Denied | Why |
|---|---|
| `iam:DeleteRolePermissionsBoundary`, `iam:DeleteUserPermissionsBoundary` | Stripping a boundary defeats the mechanism that contains every role this identity creates. |
| `iam:CreatePolicyVersion`, `iam:DeletePolicy`, `iam:DeletePolicyVersion`, `iam:SetDefaultPolicyVersion` **on both boundary policy ARNs** | Editing a boundary is equivalent to removing it. The `Resource` list names `CreatedRolePermissionsBoundaryArn` and, when one is configured, `ServiceRolePermissionsBoundaryArn` — the second entry is dropped via `!If`/`AWS::NoValue` when the parameter is blank. Protecting only the first would let the role widen its own ceiling. |
| The role-write actions **on `<StackName>-CFServiceRole`** | The role must not be able to widen itself. |
| `iam:CreatePolicyVersion`, `iam:DeletePolicy`, `iam:DeletePolicyVersion`, `iam:SetDefaultPolicyVersion` **on `<StackName>-PassRolePolicy`** | That managed policy is what limits `iam:PassRole` on *this* role to CloudFormation only. Without this `Deny` it was reachable through the customer-managed-policy statement whenever `ManagedStackNamePrefix` happens to be a prefix of the service-role stack's own name — the default prefix `idp` and a stack named `idp-...` is exactly that case — so the role could publish a new default version of its own `PassRole` policy and hand itself to any service. The ARN is built with `!Sub` rather than `!Ref`, because referencing the policy resource from inside the role would create a circular dependency; it must stay in step with `ManagedPolicyName`. |
| `iam:CreateUser`, `iam:CreateLoginProfile`, `iam:CreateAccessKey`, `iam:UpdateAccessKey`, `iam:CreateSAMLProvider`, `iam:UpdateSAMLProvider`, `iam:CreateOpenIDConnectProvider`, `iam:UpdateOpenIDConnectProviderThumbprint` | Long-lived credentials and federation trust are never part of deploying this solution, and both are standard persistence mechanisms. |

> **Why the update-only actions are here at all.** CloudFormation sets a role's
> trust policy and permissions boundary during `iam:CreateRole`, so a fresh
> deploy succeeds without `iam:UpdateAssumeRolePolicy` or
> `iam:PutRolePermissionsBoundary` — but modifying either on an existing role is
> a separate API call. Without them, a release that changes a role's trust policy
> fails mid-update and wedges the stack in `UPDATE_ROLLBACK_FAILED`, because the
> rollback needs the same permission. See
> [issue #632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632).
> This is also why the lifecycle statement is unconditioned: a condition that
> blocks a delete blocks every rollback too.

> **The cost of dropping `iam:DeleteRolePermissionsBoundary`.** An operator who
> wants to *remove* the boundary from an existing IDP stack's roles — i.e. go
> from bounded to unbounded — has to make that change with their own credentials
> rather than through this service role. That is the intended trade.

</details>

---

#### Compute & Serverless Services

<details>
<summary><strong>AWS Lambda</strong> (<code>lambda</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Deploy and manage Lambda functions for document processing, API backends, and workflow steps

**Actions Granted**:
```
lambda:*
```
- `CreateFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `DeleteFunction`
- `GetFunction`, `GetFunctionConfiguration`, `ListFunctions`
- `CreateEventSourceMapping`, `UpdateEventSourceMapping`, `DeleteEventSourceMapping`
- `AddPermission`, `RemovePermission`
- `PublishVersion`, `CreateAlias`, `UpdateAlias`, `DeleteAlias`
- `TagResource`, `UntagResource`, `ListTags`
- `InvokeFunction`, `InvokeAsync`
- `PutFunctionConcurrency`, `DeleteFunctionConcurrency`

</details>

<details>
<summary><strong>AWS Step Functions</strong> (<code>states</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Orchestrate document processing workflows and multi-step AI pipelines

**Actions Granted**:
```
states:*
```
- `CreateStateMachine`, `UpdateStateMachine`, `DeleteStateMachine`
- `DescribeStateMachine`, `ListStateMachines`
- `StartExecution`, `StopExecution`, `DescribeExecution`, `ListExecutions`
- `GetExecutionHistory`
- `CreateActivity`, `DeleteActivity`, `DescribeActivity`, `ListActivities`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>AWS CodeBuild</strong> (<code>codebuild</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Build automation for custom container images and deployment artifacts

**Actions Granted**:
```
codebuild:*
```
- `CreateProject`, `UpdateProject`, `DeleteProject`
- `BatchGetProjects`, `ListProjects`
- `StartBuild`, `StopBuild`, `BatchGetBuilds`, `ListBuilds`
- `CreateReportGroup`, `DeleteReportGroup`
- `BatchGetReportGroups`, `ListReportGroups`

</details>

---

#### AI/ML Services

<details>
<summary><strong>Amazon Bedrock</strong> (<code>bedrock</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Foundation models for document understanding, extraction, classification, and generation

**Actions Granted**:
```
bedrock:*
```
- `InvokeModel`, `InvokeModelWithResponseStream`
- `GetFoundationModel`, `ListFoundationModels`
- `CreateModelCustomizationJob`, `GetModelCustomizationJob`
- `CreateProvisionedModelThroughput`, `UpdateProvisionedModelThroughput`, `DeleteProvisionedModelThroughput`
- `GetModelInvocationLoggingConfiguration`, `PutModelInvocationLoggingConfiguration`
- `CreateGuardrail`, `UpdateGuardrail`, `DeleteGuardrail`, `GetGuardrail`
- `CreateAgent`, `UpdateAgent`, `DeleteAgent` (for Bedrock Agents)
- `CreateKnowledgeBase`, `UpdateKnowledgeBase`, `DeleteKnowledgeBase`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon Textract</strong> and <strong>Amazon SageMaker</strong> — no longer granted</summary>

`textract:*` and `sagemaker:*` were **removed** from this role.

Both services are used by the solution, but only at **runtime**, by the Lambda
execution roles in `patterns/unified/template.yaml` — which carry their own
narrowly scoped Textract and SageMaker grants. Neither service has a
CloudFormation resource type anywhere in the solution: Textract has no
`AWS::Textract::*` types at all, and the optional MLflow tracking server is
referenced by ARN through the `MlflowTrackingServerArn` parameter rather than
created by the stack. CloudFormation therefore never calls either service, so
granting them to the deployment role added reachable permissions with no
deployment benefit.

An earlier release used a SageMaker-hosted UDOP classification endpoint, the
former "Pattern 3". The unified architecture no longer deploys a SageMaker
inference endpoint — classification is performed by Bedrock foundation models.

</details>

---

#### Storage Services

<details>
<summary><strong>Amazon S3</strong> (<code>s3</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Document storage, processing artifacts, model artifacts, and static website hosting

**Actions Granted**:
```
s3:*
```
- `CreateBucket`, `DeleteBucket`, `ListBuckets`, `GetBucketLocation`
- `PutBucketPolicy`, `GetBucketPolicy`, `DeleteBucketPolicy`
- `PutBucketEncryption`, `GetBucketEncryption`
- `PutBucketVersioning`, `GetBucketVersioning`
- `PutBucketNotification`, `GetBucketNotification`
- `PutBucketCors`, `GetBucketCors`, `DeleteBucketCors`
- `PutObject`, `GetObject`, `DeleteObject`, `ListObjects`
- `PutObjectTagging`, `GetObjectTagging`, `DeleteObjectTagging`
- `PutBucketLifecycleConfiguration`, `GetBucketLifecycleConfiguration`

</details>

<details>
<summary><strong>Amazon DynamoDB</strong> (<code>dynamodb</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Metadata storage, document tracking, extraction results, and configuration data

**Actions Granted**:
```
dynamodb:*
```
- `CreateTable`, `DeleteTable`, `UpdateTable`, `DescribeTable`, `ListTables`
- `CreateGlobalTable`, `UpdateGlobalTable`, `DescribeGlobalTable`
- `PutItem`, `GetItem`, `UpdateItem`, `DeleteItem`
- `Query`, `Scan`, `BatchGetItem`, `BatchWriteItem`
- `CreateBackup`, `DeleteBackup`, `DescribeBackup`, `ListBackups`
- `RestoreTableFromBackup`, `RestoreTableToPointInTime`
- `EnableKinesisStreamingDestination`, `DisableKinesisStreamingDestination`
- `TagResource`, `UntagResource`, `ListTagsOfResource`

</details>

<details>
<summary><strong>Amazon ECR</strong> (<code>ecr</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Container image registry for custom Lambda images and SageMaker containers

**Actions Granted**:
```
ecr:*
```
- `CreateRepository`, `DeleteRepository`, `DescribeRepositories`, `ListImages`
- `GetRepositoryPolicy`, `SetRepositoryPolicy`, `DeleteRepositoryPolicy`
- `GetAuthorizationToken`, `GetDownloadUrlForLayer`
- `BatchGetImage`, `BatchCheckLayerAvailability`
- `InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`
- `PutImage`, `BatchDeleteImage`
- `PutImageScanningConfiguration`, `StartImageScan`, `DescribeImageScanFindings`
- `PutLifecyclePolicy`, `GetLifecyclePolicy`, `DeleteLifecyclePolicy`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### API & Application Services

<details>
<summary><strong>Amazon API Gateway</strong> (<code>apigateway</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: REST and HTTP APIs for document upload, status queries, and result retrieval

**Actions Granted**:
```
apigateway:*
```
- `CreateRestApi`, `DeleteRestApi`, `UpdateRestApi`, `GetRestApi`, `GetRestApis`
- `CreateResource`, `DeleteResource`, `GetResource`, `GetResources`
- `CreateMethod`, `DeleteMethod`, `PutMethod`, `GetMethod`
- `CreateIntegration`, `DeleteIntegration`, `PutIntegration`, `GetIntegration`
- `CreateDeployment`, `DeleteDeployment`, `GetDeployment`, `GetDeployments`
- `CreateStage`, `DeleteStage`, `UpdateStage`, `GetStage`, `GetStages`
- `CreateAuthorizer`, `DeleteAuthorizer`, `UpdateAuthorizer`, `GetAuthorizer`
- `CreateUsagePlan`, `DeleteUsagePlan`, `UpdateUsagePlan`, `GetUsagePlan`
- `CreateApiKey`, `DeleteApiKey`, `UpdateApiKey`, `GetApiKey`
- `TagResource`, `UntagResource`, `GetTags`

</details>

<details>
<summary><strong>AWS AppSync</strong> (<code>appsync</code>) — retained for backward compatibility</summary>

**Permission Level**: Full (`*`) — the only grant here that is not backed by a
currently-declared resource type.

**Purpose**: in-place upgrades of a pre-0.6.0 stack, and nothing else. Nothing
declares an `AWS::AppSync::*` resource any more, and the UI no longer speaks
GraphQL: its transport is an API Gateway REST API with a Lambda dispatcher, in
`nested/api-resolvers/template.yaml` (logical id `APIRESOLVERSTACK`,
historically `nested/appsync` / `APPSYNCSTACK`). See
[the AppSync → REST migration guide](../../docs/migration-appsync-to-rest.md).
A first-time deployment of 0.6.0 or later never exercises this statement.

**Why it is nonetheless retained for upgrades.** A CloudFormation service role
performs **deletions**, not only creations, and it is the role — not the caller —
whose permissions are used. Measured at tag `v0.5.16`: the solution declared
**151** pre-migration `AWS::AppSync::*` resources, namely 24 in `template.yaml`
(including the `AWS::AppSync::GraphQLApi` itself, so this is **not** only a
nested-stack concern), 106 in the pre-migration `nested/appsync/template.yaml`,
and 21 in `feature-platform/main-stack-extensions/template.yaml`. Commit
`0b61040c0` removed all 151 and renamed the nested stack's logical id —
historically `APPSYNCSTACK`, now `APIRESOLVERSTACK` — and CloudFormation treats a
logical-id rename as a delete plus a create. Nested stacks declare no `RoleARN`
of their own, so they inherit this role.

Upgrading in place across that boundary is a **documented, supported** path, and
the repository sets no minimum upgrade source:
[`docs/migration-appsync-to-rest.md`](../../docs/migration-appsync-to-rest.md)
publishes the one-time sequence "delete the feature stacks → **update the host
stack** → reinstall the features", and
[`docs/migration-v05-to-v06.md`](../../docs/migration-v05-to-v06.md) describes
"a v0.5.x stack that you **update in place** to v0.6". Without this grant that
update fails partway through on `AccessDenied`, which also blocks the automatic
rollback and leaves the stack in `UPDATE_ROLLBACK_FAILED` (the same failure shape
as issue #632, and as the `iam:UpdateAssumeRolePolicy` gap that broke every
pre-0.6.2 → 0.6.2+ upgrade under this role).

**Actions Granted**:
```
appsync:*
```
The wildcard covers the actions below. Only the `Delete*`/`Get*`/`List*` ones are
reached today, and only while an upgrade removes a pre-migration stack's API:
- `CreateGraphqlApi`, `DeleteGraphqlApi`, `UpdateGraphqlApi`, `GetGraphqlApi`, `ListGraphqlApis`
- `CreateDataSource`, `DeleteDataSource`, `UpdateDataSource`, `GetDataSource`
- `CreateResolver`, `DeleteResolver`, `UpdateResolver`, `GetResolver`, `ListResolvers`
- `CreateType`, `DeleteType`, `UpdateType`, `GetType`, `ListTypes`
- `CreateFunction`, `DeleteFunction`, `UpdateFunction`, `GetFunction`
- `CreateApiKey`, `DeleteApiKey`, `UpdateApiKey`, `ListApiKeys`
- `StartSchemaCreation`, `GetSchemaCreationStatus`, `GetIntrospectionSchema`
- `TagResource`, `UntagResource`, `ListTagsForResource`

**Why a wildcard rather than a deletion-only action list.** AWS's own
CloudFormation resource-provider schemas declare the pre-migration delete path as
`appsync:DeleteGraphqlApi`, `appsync:DeleteResolver`,
`appsync:DeleteDataSource` and `appsync:GetDataSource` — vestigial actions, in
the sense that only an upgrade ever reaches them. Note the irregular casing
(`DeleteGraphqlApi`, not `DeleteGraphQLApi`): the resource type is `GraphQLApi`
but the IAM action is `Graphql`. That list is deliberately **not** what is
granted, for three reasons. The vestigial `AWS::AppSync::GraphQLSchema` type is a
legacy type with no `handlers` block, so AWS publishes no delete permissions for
it at all and the requirement is genuinely undocumented. A rollback landing
after a partial delete needs the
create-side actions too (`CreateGraphqlApi`, `CreateDataSource`,
`CreateResolver`, `TagResource`, plus `iam:PassRole` and `s3:GetObject`). And
this is the one path that cannot be rehearsed without a real pre-0.6.0 stack, so
an action list one call short would be discovered only by wedging a customer —
the same trade recorded for the other 24 services.

**When this can be dropped**: as soon as no stack predating 0.6.0 remains to be
upgraded. An operator who will only ever deploy 0.6.0 or later can delete this
vestigial statement — `Sid: IDPLegacyAppSyncUpgradeCleanup` in
`IDP-Cloudformation-Service-Role.yaml` — today.

</details>

---

#### Security & Identity Services

<details>
<summary><strong>Amazon Cognito User Pools</strong> (<code>cognito-idp</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: User authentication, user management, and access token issuance

**Actions Granted**:
```
cognito-idp:*
```
- `CreateUserPool`, `DeleteUserPool`, `UpdateUserPool`, `DescribeUserPool`, `ListUserPools`
- `CreateUserPoolClient`, `DeleteUserPoolClient`, `UpdateUserPoolClient`, `DescribeUserPoolClient`
- `CreateUserPoolDomain`, `DeleteUserPoolDomain`, `DescribeUserPoolDomain`
- `CreateGroup`, `DeleteGroup`, `UpdateGroup`, `GetGroup`, `ListGroups`
- `AdminCreateUser`, `AdminDeleteUser`, `AdminUpdateUserAttributes`
- `AdminAddUserToGroup`, `AdminRemoveUserFromGroup`
- `AdminSetUserPassword`, `AdminResetUserPassword`
- `AdminInitiateAuth`, `AdminRespondToAuthChallenge`
- `SetUserPoolMfaConfig`, `GetUserPoolMfaConfig`

</details>

<details>
<summary><strong>Amazon Cognito Identity Pools</strong> (<code>cognito-identity</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Federated identity management and temporary AWS credentials for authenticated users

**Actions Granted**:
```
cognito-identity:*
```
- `CreateIdentityPool`, `DeleteIdentityPool`, `UpdateIdentityPool`, `DescribeIdentityPool`
- `ListIdentityPools`, `ListIdentities`
- `GetId`, `GetOpenIdToken`, `GetCredentialsForIdentity`
- `SetIdentityPoolRoles`, `GetIdentityPoolRoles`
- `LookupDeveloperIdentity`, `MergeDeveloperIdentities`
- `UnlinkDeveloperIdentity`, `UnlinkIdentity`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>AWS KMS</strong> (<code>kms</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Encryption key management for S3, DynamoDB, Secrets Manager, and other encrypted resources

**Actions Granted**:
```
kms:*
```
- `CreateKey`, `ScheduleKeyDeletion`, `CancelKeyDeletion`, `DescribeKey`, `ListKeys`
- `EnableKey`, `DisableKey`, `EnableKeyRotation`, `DisableKeyRotation`
- `CreateAlias`, `DeleteAlias`, `UpdateAlias`, `ListAliases`
- `CreateGrant`, `RetireGrant`, `RevokeGrant`, `ListGrants`
- `Encrypt`, `Decrypt`, `ReEncrypt`, `GenerateDataKey`, `GenerateDataKeyWithoutPlaintext`
- `PutKeyPolicy`, `GetKeyPolicy`, `ListKeyPolicies`
- `TagResource`, `UntagResource`, `ListResourceTags`

</details>

<details>
<summary><strong>AWS Secrets Manager</strong> (<code>secretsmanager</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Secure storage for API keys, database credentials, and service integration secrets

**Actions Granted**:
```
secretsmanager:*
```
- `CreateSecret`, `DeleteSecret`, `UpdateSecret`, `DescribeSecret`, `ListSecrets`
- `GetSecretValue`, `PutSecretValue`
- `RotateSecret`, `CancelRotateSecret`
- `UpdateSecretVersionStage`, `ListSecretVersionIds`
- `RestoreSecret`, `ReplicateSecretToRegions`, `RemoveRegionsFromReplication`
- `GetResourcePolicy`, `PutResourcePolicy`, `DeleteResourcePolicy`, `ValidateResourcePolicy`
- `TagResource`, `UntagResource`

</details>

<details>
<summary><strong>AWS WAF v2</strong> (<code>wafv2</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Web application firewall for API Gateway and CloudFront protection

**Actions Granted**:
```
wafv2:*
```
- `CreateWebACL`, `DeleteWebACL`, `UpdateWebACL`, `GetWebACL`, `ListWebACLs`
- `CreateRuleGroup`, `DeleteRuleGroup`, `UpdateRuleGroup`, `GetRuleGroup`, `ListRuleGroups`
- `CreateIPSet`, `DeleteIPSet`, `UpdateIPSet`, `GetIPSet`, `ListIPSets`
- `CreateRegexPatternSet`, `DeleteRegexPatternSet`, `UpdateRegexPatternSet`
- `AssociateWebACL`, `DisassociateWebACL`, `GetWebACLForResource`, `ListResourcesForWebACL`
- `PutLoggingConfiguration`, `GetLoggingConfiguration`, `DeleteLoggingConfiguration`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### Messaging & Event Services

<details>
<summary><strong>Amazon SNS</strong> (<code>sns</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Notifications for processing completion, errors, and system alerts

**Actions Granted**:
```
sns:*
```
- `CreateTopic`, `DeleteTopic`, `GetTopicAttributes`, `SetTopicAttributes`, `ListTopics`
- `Subscribe`, `Unsubscribe`, `ConfirmSubscription`, `ListSubscriptions`, `ListSubscriptionsByTopic`
- `Publish`, `PublishBatch`
- `GetSubscriptionAttributes`, `SetSubscriptionAttributes`
- `AddPermission`, `RemovePermission`
- `GetDataProtectionPolicy`, `PutDataProtectionPolicy`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon SQS</strong> (<code>sqs</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Message queues for asynchronous document processing and workflow decoupling

**Actions Granted**:
```
sqs:*
```
- `CreateQueue`, `DeleteQueue`, `GetQueueAttributes`, `SetQueueAttributes`, `ListQueues`
- `GetQueueUrl`, `ListQueueTags`
- `SendMessage`, `SendMessageBatch`
- `ReceiveMessage`, `DeleteMessage`, `DeleteMessageBatch`
- `ChangeMessageVisibility`, `ChangeMessageVisibilityBatch`
- `PurgeQueue`
- `AddPermission`, `RemovePermission`
- `TagQueue`, `UntagQueue`

</details>

<details>
<summary><strong>Amazon EventBridge</strong> (<code>events</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Event-driven triggers for document processing workflows and S3 event routing

**Actions Granted**:
```
events:*
```
- `CreateEventBus`, `DeleteEventBus`, `DescribeEventBus`, `ListEventBuses`
- `PutRule`, `DeleteRule`, `DescribeRule`, `EnableRule`, `DisableRule`, `ListRules`
- `PutTargets`, `RemoveTargets`, `ListTargetsByRule`
- `PutEvents`, `PutPartnerEvents`
- `CreateArchive`, `DeleteArchive`, `DescribeArchive`, `ListArchives`
- `CreateConnection`, `DeleteConnection`, `DescribeConnection`, `UpdateConnection`
- `CreateApiDestination`, `DeleteApiDestination`, `DescribeApiDestination`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon EventBridge Scheduler</strong> (<code>scheduler</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Scheduled tasks for batch processing, cleanup jobs, and periodic workflows

**Actions Granted**:
```
scheduler:*
```
- `CreateSchedule`, `DeleteSchedule`, `UpdateSchedule`, `GetSchedule`, `ListSchedules`
- `CreateScheduleGroup`, `DeleteScheduleGroup`, `GetScheduleGroup`, `ListScheduleGroups`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### Monitoring & Management Services

<details>
<summary><strong>Amazon CloudWatch</strong> (<code>cloudwatch</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Metrics, alarms, and dashboards for monitoring IDP processing performance

**Actions Granted**:
```
cloudwatch:*
```
- `PutMetricData`, `GetMetricData`, `GetMetricStatistics`, `ListMetrics`
- `PutMetricAlarm`, `DeleteAlarms`, `DescribeAlarms`, `DescribeAlarmsForMetric`
- `EnableAlarmActions`, `DisableAlarmActions`, `SetAlarmState`
- `PutDashboard`, `DeleteDashboards`, `GetDashboard`, `ListDashboards`
- `PutCompositeAlarm`, `DescribeAnomalyDetectors`, `PutAnomalyDetector`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon CloudWatch Logs</strong> (<code>logs</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Centralized logging for Lambda functions, API Gateway, and all IDP components

**Actions Granted**:
```
logs:*
```
- `CreateLogGroup`, `DeleteLogGroup`, `DescribeLogGroups`, `ListTagsLogGroup`
- `CreateLogStream`, `DeleteLogStream`, `DescribeLogStreams`
- `PutLogEvents`, `GetLogEvents`, `FilterLogEvents`
- `PutRetentionPolicy`, `DeleteRetentionPolicy`
- `PutSubscriptionFilter`, `DeleteSubscriptionFilter`, `DescribeSubscriptionFilters`
- `CreateExportTask`, `DescribeExportTasks`
- `PutMetricFilter`, `DeleteMetricFilter`, `DescribeMetricFilters`
- `PutResourcePolicy`, `DeleteResourcePolicy`, `DescribeResourcePolicies`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>AWS Systems Manager (SSM)</strong> (<code>ssm</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Parameter Store for configuration management and secure parameter storage

**Actions Granted**:
```
ssm:*
```
- `PutParameter`, `GetParameter`, `GetParameters`, `GetParametersByPath`, `DeleteParameter`
- `DescribeParameters`, `GetParameterHistory`
- `AddTagsToResource`, `RemoveTagsFromResource`, `ListTagsForResource`
- `CreateDocument`, `DeleteDocument`, `UpdateDocument`, `DescribeDocument`
- `CreateAssociation`, `DeleteAssociation`, `UpdateAssociation`, `DescribeAssociation`
- `SendCommand`, `CancelCommand`, `ListCommands`, `ListCommandInvocations`
- `StartAutomationExecution`, `StopAutomationExecution`, `GetAutomationExecution`

</details>

---

#### Analytics & Data Services

<details>
<summary><strong>AWS Glue</strong> (<code>glue</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Data catalog, ETL jobs, and schema management for structured extraction data

**Actions Granted**:
```
glue:*
```
- `CreateDatabase`, `DeleteDatabase`, `UpdateDatabase`, `GetDatabase`, `GetDatabases`
- `CreateTable`, `DeleteTable`, `UpdateTable`, `GetTable`, `GetTables`
- `CreatePartition`, `DeletePartition`, `UpdatePartition`, `GetPartition`, `GetPartitions`
- `CreateCrawler`, `DeleteCrawler`, `UpdateCrawler`, `StartCrawler`, `StopCrawler`, `GetCrawler`
- `CreateJob`, `DeleteJob`, `UpdateJob`, `StartJobRun`, `BatchStopJobRun`, `GetJob`, `GetJobRun`
- `CreateTrigger`, `DeleteTrigger`, `UpdateTrigger`, `StartTrigger`, `StopTrigger`, `GetTrigger`
- `CreateConnection`, `DeleteConnection`, `UpdateConnection`, `GetConnection`
- `TagResource`, `UntagResource`, `GetTags`

</details>

<details>
<summary><strong>Amazon OpenSearch Serverless</strong> (<code>aoss</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Vector search for document embeddings, semantic search, and RAG implementations

**Actions Granted**:
```
aoss:*
```
- `CreateCollection`, `DeleteCollection`, `UpdateCollection`, `GetCollection`, `ListCollections`, `BatchGetCollection`
- `CreateSecurityPolicy`, `DeleteSecurityPolicy`, `UpdateSecurityPolicy`, `GetSecurityPolicy`, `ListSecurityPolicies`
- `CreateAccessPolicy`, `DeleteAccessPolicy`, `UpdateAccessPolicy`, `GetAccessPolicy`, `ListAccessPolicies`
- `CreateVpcEndpoint`, `DeleteVpcEndpoint`, `UpdateVpcEndpoint`, `GetVpcEndpoint`, `ListVpcEndpoints`, `BatchGetVpcEndpoint`
- `CreateSecurityConfig`, `DeleteSecurityConfig`, `UpdateSecurityConfig`, `GetSecurityConfig`
- `GetAccountSettings`, `UpdateAccountSettings`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### Networking & Content Delivery Services

<details>
<summary><strong>Amazon CloudFront</strong> (<code>cloudfront</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: CDN for web application hosting and API acceleration

**Actions Granted**:
```
cloudfront:*
```
- `CreateDistribution`, `DeleteDistribution`, `UpdateDistribution`, `GetDistribution`, `ListDistributions`
- `CreateOriginAccessControl`, `DeleteOriginAccessControl`, `UpdateOriginAccessControl`, `GetOriginAccessControl`
- `CreateCachePolicy`, `DeleteCachePolicy`, `UpdateCachePolicy`, `GetCachePolicy`, `ListCachePolicies`
- `CreateOriginRequestPolicy`, `DeleteOriginRequestPolicy`, `UpdateOriginRequestPolicy`, `GetOriginRequestPolicy`
- `CreateResponseHeadersPolicy`, `DeleteResponseHeadersPolicy`, `UpdateResponseHeadersPolicy`
- `CreateFunction`, `DeleteFunction`, `UpdateFunction`, `PublishFunction`, `GetFunction`
- `CreateInvalidation`, `GetInvalidation`, `ListInvalidations`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon EC2</strong> (<code>ec2</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Security groups and VPC interface endpoints for the private hosting
variants, plus the optional bastion host (`AWS::EC2::Instance` and
`AWS::EC2::LaunchTemplate` behind the `ShouldDeployBastionHost` condition).

**Actions Granted**:
```
ec2:*
```

> **`ec2:*` is the whole grant, and it is not narrowed to a VPC-only action list.**
> The solution declares `AWS::EC2::Instance` and `AWS::EC2::LaunchTemplate` as well
> as VPC resources, and narrowing `ec2:*` to a fixed action list is the same
> untested-narrowing risk described in
> ["What Remains Broad, and Why"](#what-remains-broad-and-why) — an
> `ec2:*Tags` or `ec2:*NetworkInterface*` call missing from the list would fail
> a stack update and its rollback together. If you need EC2 narrowed, derive the
> action list from CloudTrail across a create, update and delete of the specific
> hosting variant you deploy.

</details>

---

#### Removed Wildcards

<details>
<summary><strong>AWS Application Auto Scaling</strong> (<code>application-autoscaling</code>) — no longer granted</summary>

`application-autoscaling:*` was **removed** from this role.

It was documented as covering auto-scaling for DynamoDB tables and Lambda
provisioned concurrency, but the solution declares neither. There is no
`AWS::ApplicationAutoScaling::ScalableTarget` or `ScalingPolicy` anywhere, no
`ProvisionedConcurrencyConfig` on any function, and every DynamoDB table uses
on-demand billing rather than provisioned throughput — so CloudFormation never
registers a scalable target and never calls this service.

</details>

---

## <span style="color: blue;">What Remains Broad, and Why</span>

Two grants in this role are not narrowed, and this section explains the reasoning
so you can decide whether the trade is acceptable in your account rather than
discovering it later.

### `cloudformation:*` on `Resource: "*"`

The stack creates nested stacks and change sets, and several CloudFormation read
APIs (`DescribeStacks`, `ListStacks`, `ValidateTemplate`) are not
resource-scopable at all. Narrowing this to an action list would need a
CloudTrail-derived inventory from a real create, update and delete.

### `<service>:*` on `Resource: "*"` for 25 services

Two separate reasons, and both need to be addressed to narrow either half:

**The resource half.** CloudFormation assigns physical resource names at deploy
time, long after this role is created, so there is no ARN pattern to match on
beyond the stack name. The IAM statements can use a name prefix because IAM role
names are predictable from the stack name; a KMS key ID or a CloudFront
distribution ID is not. And unlike IAM, none of these services has an equivalent
of `iam:PermissionsBoundary` to bound the result of a create call.

**The action half.** An action list that turns out to be one call short does not
merely fail the deployment — it fails the deployment **and its rollback**, which
leaves the stack in `UPDATE_ROLLBACK_FAILED` and recoverable only with
`continue-update-rollback --resources-to-skip`. That has happened in this project
before ([issue #632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632)),
from a *missing* IAM action rather than a superfluous one. Given that only
CloudFormation can assume this role and every call is recorded in CloudTrail, a
wedged stack is a more likely and more damaging outcome than the escalation a
speculative action list would prevent.

**What it would take to narrow them safely.** Deploy each variant with the role
as-is, collect the `eventName` set that CloudFormation's assumed-role session
produced from CloudTrail across a full create, update and delete, take the union
across variants (BDA and Pipeline mode, each hosting variant, with and without
the optional Knowledge Base and multi-doc discovery stacks), then add a margin
for the AWS-side calls that only appear on failure paths. Until that data exists,
a role documented as broad is safer than one claimed narrow and not.

### Why the trust policy is one statement, not two

For a CloudFormation **stack service role** (`create-stack --role-arn`), AWS
documents the trust policy with the service principal and *no* conditions, and
does not state that CloudFormation populates `aws:SourceAccount` or
`aws:SourceArn` on that `sts:AssumeRole` call. Those keys are documented for
StackSets administration roles and for registry-extension execution roles, which
are different mechanisms.

A single plain `StringEquals` on a key that is never populated evaluates false and
would deny **every** stack operation, including rollbacks — an untestable-here
change that could brick all deployments. So the conditions have to be applied
"when present" rather than unconditionally. The trust policy now does that in one
statement:

```yaml
- Sid: AllowCloudFormationFromThisAccount
  Effect: Allow
  Principal:
    Service: !Sub 'cloudformation.${AWS::URLSuffix}'
  Action: sts:AssumeRole
  Condition:
    StringEqualsIfExists:
      aws:SourceAccount: !Ref AWS::AccountId
    ArnLikeIfExists:
      aws:SourceArn: !Sub 'arn:${AWS::Partition}:cloudformation:*:${AWS::AccountId}:stack/*'
```

**An earlier revision used two statements, and that shape had a gap that made the
role unassumable in one configuration.** Statement one applied plain
`StringEquals`/`ArnLike` to *both* keys; statement two allowed the assumption when
*both* keys were absent, using `Null: { aws:SourceAccount: 'true',
aws:SourceArn: 'true' }`. Enumerate the three possible cases and the third one has
no `Allow`:

| What CloudFormation populates | Statement 1 (plain operators on both keys) | Statement 2 (`Null` requires both absent) | Result |
|---|---|---|---|
| Both keys | matches | no — both keys are present | assumption allowed |
| Neither key | no — a plain operator on an absent key is false | matches | assumption allowed |
| **Exactly one key** | **no** — the plain operator on the *absent* key is false | **no** — the *present* key violates the both-absent requirement | **no `Allow` matches** |

In that third case the role becomes unassumable, and because a service role is
assumed for the rollback as well as for the operation, every stack operation *and*
its rollback fail with `AccessDenied` on `sts:AssumeRole`. Nothing in AWS's
documentation rules that case out; it says nothing about these keys for stack
service roles at all, which is precisely why the two-statement shape was a bet
rather than a design.

The `...IfExists` form removes the case analysis. Each key is evaluated
independently, and each comparison is skipped when its own key is absent, so all
four combinations of present and absent are allowed while any *populated* key that
disagrees is still denied. That is strictly tighter than an unconditioned trust
policy wherever CloudFormation supplies either key, with no configuration in which
the role stops being assumable.

Note the operator pairing: `aws:SourceAccount` is a plain account ID, so it takes
`StringEqualsIfExists`, while `aws:SourceArn` is an ARN matched with a wildcard,
so it takes `ArnLikeIfExists` — `StringEqualsIfExists` would require a literal ARN
and reject every real stack ARN.

`sts:ExternalId` is deliberately **not** used: it addresses cross-account role
assumption by a third party, and CloudFormation does not send one. For this role
the load-bearing controls are who holds `iam:PassRole` on it and, optionally, a
`cloudformation:RoleArn` condition on that principal's own policy.

## <span style="color: blue;">Security Considerations</span>

### Regional Restrictions
- **No region condition is applied.** The trust policy's `aws:SourceArn`
  condition uses `stack/*` across all regions, because a stack service role is
  commonly used in more than one. If you need a single-region role, add
  `aws:RequestedRegion` to the trust policy or to a `Deny` statement — this
  template does not do it for you, and role assumption is **not** restricted to
  the deployment region.

### Session Security
- **Account Isolation**: Only the CloudFormation service principal can assume the
  role, and only for stack operations in this account. No user or role can assume
  it directly, so there are no sessions to time out or credentials to rotate.

### Permission Scope
- **Broad service access**: `cloudformation:*` plus `<service>:*` on 25 services,
  all on `Resource: "*"`. See ["What Remains Broad, and Why"](#what-remains-broad-and-why).
- **IAM is the exception**: scoped by action, by resource name prefix, and by
  `iam:PermissionsBoundary` / `iam:PassedToService` conditions, with explicit
  denies on boundary tampering, self-modification and credential creation.
- **Boundary is mandatory**: `CreatedRolePermissionsBoundaryArn` has no default.
  The strength of the containment is the strength of the boundary policy you
  write. The role's *own* ceiling is a second, optional parameter,
  `ServiceRolePermissionsBoundaryArn`, and the two must not be given the same
  value — see ["Two Boundaries, Two Jobs"](#two-boundaries-two-jobs).
- **Compliance note**: Organizations should refine the 25 service wildcards to
  their own least-privilege requirements. The method for doing that safely is in
  ["What Remains Broad, and Why"](#what-remains-broad-and-why).

## <span style="color: blue;">Troubleshooting</span>

### Common Issues

1. **`AccessDenied` on `iam:CreateRole` during IDP stack deployment**:
   - Almost always one of the two constraints this role enforces. Check the error
     message for the role ARN it was trying to create.
   - **Missing or mismatched boundary**: the IDP stack must be deployed with its
     `PermissionsBoundaryArn` parameter set to the *same* ARN you passed as this
     role's `CreatedRolePermissionsBoundaryArn`. An empty value fails by design.
   - **Stack name prefix mismatch**: the IDP stack name must start with
     `ManagedStackNamePrefix`, and the comparison is case-sensitive. CloudFormation
     derives generated role names from the stack name, so `my-idp-prod` does not
     match a prefix of `idp`, and neither does `IDP-prod`.
   - Both values are echoed in this stack's `RequiredPermissionsBoundaryArn` and
     `RequiredStackNamePrefix` outputs.
   - **If the IDP stack was deployed before this hardening landed**, its roles
     probably carry no boundary at all, and the failure is on *update* rather than
     on create. That case, its detection and its recovery are in
     ["Updating an Existing Deployment"](#updating-an-existing-deployment) — read
     it before running the update, not after.

2. **`AccessDenied` on `iam:PassRole` during IDP stack deployment**:
   - The role being passed does not match the name prefix, or the destination
     service is not in the `iam:PassedToService` list in the template. Add the
     service principal to that list.

3. **`AccessDenied` on `iam:DeleteRolePermissionsBoundary`**:
   - Expected. This role cannot remove a boundary from a role, by design; the
     `DenyStrippingAnyPermissionsBoundary` statement forbids it outright and a
     `Deny` cannot be overridden.
   - The usual trigger is not a deliberate call. Setting the IDP stack's
     `PermissionsBoundaryArn` parameter back to the empty string makes
     CloudFormation drop the `PermissionsBoundary` property (it is wired through
     `!If [..., !Ref PermissionsBoundaryArn, !Ref AWS::NoValue]`), and dropping the
     property *is* a `DeleteRolePermissionsBoundary` call. Both the update and its
     rollback need that action, so the stack lands in `UPDATE_ROLLBACK_FAILED`.
   - Recovery and the `continue-update-rollback --resources-to-skip` procedure are
     in ["Updating an Existing Deployment"](#updating-an-existing-deployment). If
     you are intentionally moving an existing IDP stack from bounded to unbounded,
     make that update with your own credentials rather than through this role.

4. **Access Denied when Using Role**:
   - Verify your user/role has `iam:PassRole` permission for this specific role
     ARN, with `iam:PassedToService` allowing `cloudformation.amazonaws.com`
   - Ensure the role exists and is in the same account
   - Remember: Users cannot assume this role directly - only CloudFormation service can

5. **CloudFormation Deployment Failures**:
   - If using the CLI, ensure you're using `CAPABILITY_IAM` and `CAPABILITY_NAMED_IAM`
   - Check CloudWatch logs for specific service errors
   - For a wedged `UPDATE_ROLLBACK_FAILED` stack, see
     [../docs/troubleshooting.md](../../docs/troubleshooting.md)

## <span style="color: blue;">Best Practices</span>

1. **Write the boundary policy first, and take it seriously.** The policy you pass
   as `CreatedRolePermissionsBoundaryArn` is the only thing standing between this
   role and account administrator. Grant it no more than the runtime permissions in
   [../docs/aws-services-and-roles.md](../../docs/aws-services-and-roles.md). Do
   **not** pass that same ARN as `ServiceRolePermissionsBoundaryArn`: the two
   parameters have opposite requirements, and giving the deployment role a runtime
   ceiling stops it deploying anything. See
   ["Two Boundaries, Two Jobs"](#two-boundaries-two-jobs).
2. **Regular Auditing**: Periodically review who holds `iam:PassRole` on this
   role — that list is the real blast radius, not the role itself.
3. **Constrain the caller too**: add a `cloudformation:RoleArn` condition to the
   deploying principal's policy so they can pass this role only to the stacks you
   intend.
4. **Monitoring**: Enable CloudTrail and alert on `iam:*` calls made by this
   role's assumed-role session. Every one of them is a deployment event, so
   anything outside a deployment window is worth investigating.
5. **Narrow the wildcards from data, not from guesswork.** See
   ["What Remains Broad, and Why"](#what-remains-broad-and-why).
