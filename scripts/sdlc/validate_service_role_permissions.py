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
# Adding/removing a boundary on an EXISTING role (e.g. the operator changes the
# PermissionsBoundaryArn parameter on a stack update). Also update-only.
BOUNDARY_ACTIONS = {'iam:PutRolePermissionsBoundary', 'iam:DeleteRolePermissionsBoundary'}


def extract_cfn_control_plane_iam_actions(template_path):
    """Derive the IAM actions CloudFormation needs to manage a template's roles.

    Returns the subset of the action groups above that the template's own
    AWS::IAM::Role declarations imply. Property *presence* is what matters, so
    this is unaffected by CFNLoader dropping intrinsic function values.
    """
    template = load_template(template_path)

    required = set()
    for resource in iter_resources(template):
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
    role_permissions = extract_permissions_from_role('iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml')
    role_iam_permissions = extract_iam_permissions_from_role('iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml')
    
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
    
    if exit_code == 0:
        print('✅ Service role has sufficient permissions for deployment')
    
    return exit_code

if __name__ == '__main__':
    sys.exit(main())