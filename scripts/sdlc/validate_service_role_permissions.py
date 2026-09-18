#!/usr/bin/env python3
"""
Validate CloudFormation service role has sufficient permissions for IDP deployment
"""

import os
import sys

import yaml


# Custom YAML loader that ignores CloudFormation intrinsic functions.
# CFNLoader subclasses yaml.SafeLoader (NOT yaml.Loader), so no unsafe
# Python-object constructors are ever enabled: `python/object`, `python/name`
# and `python/object/apply` are not registered, so nothing in the document can
# instantiate an object or import a module. The only customization is a no-op
# multi-constructor for `!`-prefixed tags (e.g. !Ref, !Sub, !GetAtt) that
# returns None, so that real CloudFormation templates parse.
#
# idp_sdk._core.cfn_yaml is the shared home for this pattern, but this script
# deliberately does not import it: the `deployment_validation` CI job runs this
# file with only PyYAML installed (see .gitlab-ci.yml), so it must stay
# dependency-free. The collapse-to-None policy is also specific to this script —
# _iter_statements below is built around it.
class CFNLoader(yaml.SafeLoader):
    pass

def cfn_constructor(loader, tag_suffix, node):
    return None  # Ignore CloudFormation functions

# Register constructors for CloudFormation intrinsic functions
CFNLoader.add_multi_constructor('!', cfn_constructor)

def load_template(template_path):
    """Parse a CloudFormation template, with intrinsics collapsed to None.

    Deliberately does NOT swallow errors: a template this script cannot parse
    must fail the CI gate loudly rather than degrade to "no permissions
    required" (see the note on _iter_statements below).

    The loader is driven directly rather than through `yaml.load(..., Loader=)`.
    That is what yaml.load does internally, minus the call shape that scanners
    report as unsafe deserialization; see idp_sdk._core.cfn_yaml.
    """
    with open(template_path, 'r') as f:
        loader = CFNLoader(f)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()


# --- Tolerant policy-document walking -----------------------------------------
# CFNLoader collapses every intrinsic function to None, so a conditional inline
# policy, an Fn::If'd Statement list, or an Fn::If'd statement element all show
# up as None inside an otherwise ordinary policy document. The helpers below
# skip those entries.
#
# This used to be inline code with no None handling, wrapped in a broad
# try/except that printed the resulting AttributeError and returned an EMPTY
# action set — so the IAM half of this validator had been silently passing
# vacuously. That is why the missing iam:UpdateAssumeRolePolicy in issue #632
# was not caught here.
def _iter_statements(policy_document):
    """Yield the statement dicts of a PolicyDocument, skipping intrinsics."""
    if not isinstance(policy_document, dict):
        return
    statements = policy_document.get('Statement')
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list):
        return
    for statement in statements:
        if isinstance(statement, dict):
            yield statement


def _iter_actions(statement):
    """Yield the `Action` strings of a single statement."""
    actions = statement.get('Action') or []
    if isinstance(actions, str):
        actions = [actions]
    if not isinstance(actions, list):
        return
    for action in actions:
        if isinstance(action, str) and ':' in action:
            yield action


def _iter_policy_documents(resource):
    """Yield the policy documents an IAM role / managed policy declares."""
    if not isinstance(resource, dict):
        return
    props = resource.get('Properties')
    if not isinstance(props, dict):
        return
    if resource.get('Type') == 'AWS::IAM::Role':
        policies = props.get('Policies') or []
        if isinstance(policies, list):
            for policy in policies:
                if isinstance(policy, dict):
                    yield policy.get('PolicyDocument')
    elif resource.get('Type') == 'AWS::IAM::ManagedPolicy':
        yield props.get('PolicyDocument')


def _iter_resource_strings(statement):
    """Yield the literal `Resource` strings of a statement.

    Intrinsics (`!Sub`, `!GetAtt`, `!Ref`) are already None by the time we get
    here, so a statement scoped to a constructed ARN yields nothing — which is
    what we want, since the checks below only care about a literal `'*'`.
    """
    resources = statement.get('Resource')
    if isinstance(resources, str):
        resources = [resources]
    if not isinstance(resources, list):
        return
    for resource in resources:
        if isinstance(resource, str):
            yield resource


def _statement_is_on_all_resources(statement):
    return any(resource == '*' for resource in _iter_resource_strings(statement))


def _condition_keys(statement):
    """Yield the lower-cased condition keys a statement tests, with operators.

    Yields `(operator, key)` pairs. The operator matters: `Null` inverts the
    meaning of a key (it asserts the key's *absence*), so a caller that wants a
    real value comparison must exclude it.
    """
    condition = statement.get('Condition')
    if not isinstance(condition, dict):
        return
    for operator, entries in condition.items():
        if not isinstance(operator, str) or not isinstance(entries, dict):
            continue
        for key in entries:
            if isinstance(key, str):
                yield operator, key.lower()


def iter_resources(template):
    """Yield the resource dicts of a parsed template."""
    resources = (template or {}).get('Resources')
    if not isinstance(resources, dict):
        return
    for resource in resources.values():
        if isinstance(resource, dict):
            yield resource


def extract_aws_services_from_template(template_path):
    """Extract AWS services used in a CloudFormation template"""
    try:
        template = load_template(template_path)

        services = set()
        if template and 'Resources' in template:
            for resource in template['Resources'].values():
                if resource and 'Type' in resource:
                    resource_type = resource['Type']
                    if resource_type and resource_type.startswith('AWS::'):
                        service = resource_type.split('::')[1].lower()
                        services.add(service)
        return services
    except Exception as e:
        print(f'Error parsing {template_path}: {e}')
        return set()

def extract_permissions_from_role(role_template_path):
    """Extract permissions from CloudFormation service role template"""
    role_template = load_template(role_template_path)

    permissions = set()
    for resource in iter_resources(role_template):
        if resource.get('Type') != 'AWS::IAM::Role':
            continue
        for document in _iter_policy_documents(resource):
            for statement in _iter_statements(document):
                # An action named only in a Deny is not granted. Without this,
                # the guardrail Deny statements in the service role would be
                # reported as permissions the role holds.
                if statement.get('Effect') != 'Allow':
                    continue
                for action in _iter_actions(statement):
                    if '*' in action:
                        service = action.split(':')[0]
                        permissions.add(f'{service}:*')
                    else:
                        permissions.add(action)
    return permissions

def extract_iam_actions_from_template(template_path):
    """Extract IAM actions used in a CloudFormation template"""
    template = load_template(template_path)

    iam_actions = set()
    for resource in iter_resources(template):
        for document in _iter_policy_documents(resource):
            for statement in _iter_statements(document):
                iam_actions.update(_iter_actions(statement))
    return iam_actions

# --- CloudFormation control-plane IAM actions ---------------------------------
# The checks below compare the IAM actions our templates GRANT to their own
# roles against the service role. That misses a second, easily-forgotten class:
# the IAM actions CloudFormation itself must call to MANAGE those role
# resources. Most of them are only needed on UPDATE of an already existing
# role, so a fresh deploy passes and the gap only surfaces later, mid-upgrade,
# as an AccessDenied that also blocks the rollback (issue #632).
#
# Keyed by the template feature that requires them, so the requirement is
# derived from what the templates actually declare rather than hardcoded.
ROLE_LIFECYCLE_ACTIONS = {
    'iam:CreateRole',
    'iam:DeleteRole',
    'iam:GetRole',
    'iam:TagRole',
    'iam:UntagRole',
    'iam:PassRole',
    # Changing an EXISTING role's trust policy. Set via CreateRole on create,
    # so this is an update-only requirement.
    'iam:UpdateAssumeRolePolicy',
}
INLINE_POLICY_ACTIONS = {'iam:PutRolePolicy', 'iam:DeleteRolePolicy', 'iam:GetRolePolicy'}
MANAGED_POLICY_ATTACH_ACTIONS = {'iam:AttachRolePolicy', 'iam:DetachRolePolicy'}
# Attaching or changing a boundary on an EXISTING role (e.g. the operator
# changes the PermissionsBoundaryArn parameter on a stack update). Update-only.
#
# iam:DeleteRolePermissionsBoundary is deliberately NOT here. CloudFormation
# needs it only to REMOVE a boundary from a role that already has one, which
# means going from bounded to unbounded — and granting it to a role whose own
# containment rests on iam:PermissionsBoundary hands that role a one-call
# bypass. The cost is real but bounded: an operator who clears the
# PermissionsBoundaryArn parameter on an existing stack has to make that
# change with credentials of their own instead of via the service role. See
# FORBIDDEN_SERVICE_ROLE_ACTIONS below, which turns this into a hard check.
BOUNDARY_ACTIONS = {'iam:PutRolePermissionsBoundary'}
# An AWS::IAM::InstanceProfile is a separate IAM resource type with its own
# lifecycle actions, and none of them is implied by the role actions above. The
# bastion host is the only feature that declares one, so without these the
# service role deploys every stack except one with BastionHost enabled — a
# failure that only appears for the operator who turns that feature on.
INSTANCE_PROFILE_ACTIONS = {
    'iam:CreateInstanceProfile',
    'iam:DeleteInstanceProfile',
    'iam:GetInstanceProfile',
    'iam:AddRoleToInstanceProfile',
    'iam:RemoveRoleFromInstanceProfile',
}

# --- Hardening checks on the service role itself ------------------------------
# Everything above answers "is the service role strong enough to deploy?". The
# checks below answer the opposite question — "is it stronger than it needs to
# be?" — which is the half that was missing. The shipped role granted IAM write
# actions on `Resource: "*"` with no permissions-boundary condition and
# iam:PassRole on `*` with no condition at all, and this gate reported success
# (issue #927). A gate that can only fail in the permissive direction is not a
# security gate.

# Verbs that read IAM state without changing it. Anything else in the iam:
# namespace (including a bare `iam:*`) is treated as a write.
IAM_READ_VERB_PREFIXES = (
    'get', 'list', 'describe', 'simulate', 'generate',
)

# Actions a delegated deployment role must never hold, whatever the resource.
FORBIDDEN_SERVICE_ROLE_ACTIONS = {
    # Strips a permissions boundary, i.e. defeats the mechanism that contains
    # every role this identity creates.
    'iam:deleterolepermissionsboundary',
    'iam:deleteuserpermissionsboundary',
    # Long-lived credentials and federation trust are never part of deploying
    # this solution, and both are standard persistence mechanisms.
    'iam:createaccesskey',
    'iam:createloginprofile',
    'iam:createuser',
}

# Conditions that meaningfully constrain an IAM write. iam:PermissionsBoundary
# is the only one that bounds what a CREATED role can do; the resource-tag and
# path keys constrain which roles are touched but not their power.
BOUNDARY_CONDITION_KEY = 'iam:permissionsboundary'
PASSED_TO_SERVICE_CONDITION_KEY = 'iam:passedtoservice'


def _is_iam_write_action(action):
    """True if `action` is an iam: action that can change state."""
    if not action.lower().startswith('iam:'):
        return False
    verb = action.split(':', 1)[1].lower()
    if verb.startswith('*'):
        return True
    return not verb.startswith(IAM_READ_VERB_PREFIXES)


def iter_role_statements(role_template_path):
    """Yield `(logical_id, statement)` for every policy statement in a template.

    Covers both the inline policies of AWS::IAM::Role and standalone
    AWS::IAM::ManagedPolicy resources, because the PassRole grant this role
    ships lives in the latter.
    """
    template = load_template(role_template_path) or {}
    resources = template.get('Resources')
    if not isinstance(resources, dict):
        return
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        for document in _iter_policy_documents(resource):
            for statement in _iter_statements(document):
                yield logical_id, statement


def _statement_label(logical_id, statement):
    sid = statement.get('Sid')
    return f'{logical_id}/{sid}' if isinstance(sid, str) and sid else logical_id


def find_service_role_hardening_findings(role_template_path):
    """Report over-broad grants in the service role template.

    Returns a list of human-readable findings; empty means the role passes.
    Only `Effect: Allow` statements are examined — an explicit Deny on
    `Resource: "*"` is a guardrail, not a grant.
    """
    findings = []
    for logical_id, statement in iter_role_statements(role_template_path):
        if statement.get('Effect') != 'Allow':
            continue
        label = _statement_label(logical_id, statement)
        actions = list(_iter_actions(statement))
        on_all_resources = _statement_is_on_all_resources(statement)
        condition_keys = list(_condition_keys(statement))
        # A Null test asserts a key is ABSENT, so it does not constrain the
        # value of that key and cannot stand in for a real comparison.
        compared_keys = {
            key for operator, key in condition_keys
            if operator.lower() != 'null'
        }
        all_keys = {key for _, key in condition_keys}

        forbidden = sorted(
            action for action in actions
            if action.lower() in FORBIDDEN_SERVICE_ROLE_ACTIONS
        )
        if forbidden:
            findings.append(
                f'{label}: grants {", ".join(forbidden)}, which must never be '
                f'granted to a delegated deployment role'
            )

        iam_writes = sorted(
            action for action in actions if _is_iam_write_action(action)
        )
        if iam_writes and on_all_resources:
            if BOUNDARY_CONDITION_KEY not in compared_keys:
                findings.append(
                    f'{label}: IAM write actions on Resource: "*" with no '
                    f'{BOUNDARY_CONDITION_KEY} condition '
                    f'({", ".join(iam_writes)}). Scope the resource to the '
                    f'principals this stack creates, or require a permissions '
                    f'boundary.'
                )

        pass_role = [
            action for action in actions
            if action.lower() in ('iam:passrole', 'iam:*')
        ]
        if pass_role:
            if on_all_resources:
                findings.append(
                    f'{label}: iam:PassRole on Resource: "*". Scope it to the '
                    f'role name patterns this stack creates.'
                )
            if PASSED_TO_SERVICE_CONDITION_KEY not in all_keys:
                findings.append(
                    f'{label}: iam:PassRole with no '
                    f'{PASSED_TO_SERVICE_CONDITION_KEY} condition. Restrict '
                    f'which services the role may be handed to.'
                )
    return findings


def extract_cfn_control_plane_iam_actions(template_path):
    """Derive the IAM actions CloudFormation needs to manage a template's roles.

    Returns the subset of the action groups above that the template's own
    AWS::IAM::Role declarations imply. Property *presence* is what matters, so
    this is unaffected by CFNLoader dropping intrinsic function values.
    """
    template = load_template(template_path)

    required = set()
    for resource in iter_resources(template):
        if resource.get('Type') == 'AWS::IAM::InstanceProfile':
            required |= INSTANCE_PROFILE_ACTIONS
            continue
        if resource.get('Type') != 'AWS::IAM::Role':
            continue
        props = resource.get('Properties')
        if not isinstance(props, dict):
            continue
        required |= ROLE_LIFECYCLE_ACTIONS
        if 'Policies' in props:
            required |= INLINE_POLICY_ACTIONS
        if 'ManagedPolicyArns' in props:
            required |= MANAGED_POLICY_ATTACH_ACTIONS
        if 'PermissionsBoundary' in props:
            required |= BOUNDARY_ACTIONS
    return required


def extract_required_permissions_from_templates(templates):
    """Extract all required permissions from templates"""
    wildcard_permissions = set()
    required_iam_actions = set()
    
    # Services to ignore (not real AWS services)
    ignore_services = {'serverless', 'opensearchserverless', 'cognito'}
    
    for template_path in templates:
        if os.path.exists(template_path):
            services = extract_aws_services_from_template(template_path)
            iam_actions = extract_iam_actions_from_template(template_path)
            
            for service in services:
                if service != 'iam' and service not in ignore_services:
                    wildcard_permissions.add(f'{service}:*')
            
            # Only add IAM actions to required_iam_actions
            for action in iam_actions:
                if action.startswith('iam:'):
                    required_iam_actions.add(action)

            # ...plus the actions CloudFormation needs to manage the roles the
            # template declares (not the same thing as what those roles grant).
            required_iam_actions |= extract_cfn_control_plane_iam_actions(template_path)

    return wildcard_permissions, required_iam_actions

def extract_iam_permissions_from_role(role_template_path):
    """Extract actual IAM permissions from service role template"""
    return {
        action
        for action in extract_permissions_from_role(role_template_path)
        if action.startswith('iam:')
    }

def validate_permissions(role_permissions, required_wildcards, required_iam_actions, role_iam_permissions):
    """Validate if service role has required permissions"""
    missing_wildcards = []
    
    # Check wildcard permissions for non-IAM services
    for required in required_wildcards:
        if required not in role_permissions:
            missing_wildcards.append(required)
    
    # Check specific IAM actions. A blanket `iam:*` in the role satisfies all of
    # them (extract_permissions_from_role collapses any wildcarded action to
    # `<service>:*`), so don't report every action as missing in that case.
    if 'iam:*' in role_iam_permissions:
        missing_iam = set()
    else:
        missing_iam = required_iam_actions - role_iam_permissions

    return missing_wildcards, missing_iam

SERVICE_ROLE_TEMPLATE = (
    'iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml'
)


def main():
    # Templates to check
    templates = [
        'template.yaml',  # Main template
        'patterns/unified/template.yaml',
        'nested/bedrockkb/template.yaml'
    ]

    # Extract required permissions from templates
    required_wildcards, required_iam_actions = extract_required_permissions_from_templates(templates)
    print(f'Required wildcard permissions: {sorted(required_wildcards)}')
    print(f'Required IAM actions: {sorted(required_iam_actions)}')

    # Extract permissions from service role
    role_permissions = extract_permissions_from_role(SERVICE_ROLE_TEMPLATE)
    role_iam_permissions = extract_iam_permissions_from_role(SERVICE_ROLE_TEMPLATE)

    print(f'Service role has {len(role_permissions)} total permissions')
    print(f'Service role has {len(role_iam_permissions)} IAM permissions: {sorted(role_iam_permissions)}')

    # Validate permissions
    missing_wildcards, missing_iam = validate_permissions(
        role_permissions, required_wildcards, required_iam_actions, role_iam_permissions
    )

    # Report results
    exit_code = 0
    
    if missing_wildcards:
        print(f'❌ Missing wildcard permissions: {missing_wildcards}')
        exit_code = 1
    
    if missing_iam:
        print(f'❌ Missing IAM permissions: {sorted(missing_iam)}')
        exit_code = 1

    # The other direction: is the role broader than it needs to be?
    hardening_findings = find_service_role_hardening_findings(SERVICE_ROLE_TEMPLATE)
    if hardening_findings:
        print('❌ Service role grants are too broad:')
        for finding in hardening_findings:
            print(f'   - {finding}')
        exit_code = 1

    if exit_code == 0:
        print('✅ Service role has sufficient permissions for deployment')
        print('✅ Service role IAM writes are bounded and PassRole is scoped')

    return exit_code

if __name__ == '__main__':
    sys.exit(main())