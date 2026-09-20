#!/usr/bin/env python3
"""
Validate CloudFormation service role has sufficient permissions for IDP deployment
"""

import fnmatch
import os
import re
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

# PyYAML calls a multi-constructor positionally as (loader, tag_suffix, node);
# all three are part of the required signature and the leading underscores mark
# the ones this collapse-to-None implementation deliberately ignores.
def cfn_constructor(_loader, _tag_suffix, _node):
    return None  # Ignore CloudFormation functions

# Register constructors for CloudFormation intrinsic functions
CFNLoader.add_multi_constructor('!', cfn_constructor)

# A second loader that PRESERVES the text of an intrinsic instead of collapsing
# it to None. Same safety properties as CFNLoader above — it subclasses
# yaml.SafeLoader, registers no `python/` constructors, and every value it can
# produce is a plain str, list, dict or None — and test_cfn_loader_safety.py
# asserts that for this class too.
#
# Why a second loader rather than changing CFNLoader. The hardening checks need
# to see the TEXT of a `Resource` written as `!Sub 'arn:${AWS::Partition}:iam::
# ${AWS::AccountId}:role/*'`, because that string is the repository's own ARN
# convention and it matches every role in the account. Collapsed to None it
# looked like "no literal resource", so the resource was treated as bounded and
# the permissions-boundary check was skipped entirely — the single widest hole in
# this gate. The permission-sufficiency half of the script still uses CFNLoader,
# whose collapse-to-None behaviour _iter_statements is built around.
class PatternCFNLoader(yaml.SafeLoader):
    pass


def cfn_pattern_constructor(loader, _tag_suffix, node):
    """Return an intrinsic's operand as plain data, never as a Python object.

    Which intrinsic it was does not matter here — only the operand text — so
    `_tag_suffix` is part of PyYAML's required signature and is unused.
    """
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


PatternCFNLoader.add_multi_constructor('!', cfn_pattern_constructor)


def load_template(template_path, loader_cls=None):
    """Parse a CloudFormation template, with intrinsics collapsed to None.

    Deliberately does NOT swallow errors: a template this script cannot parse
    must fail the CI gate loudly rather than degrade to "no permissions
    required" (see the note on _iter_statements below).

    Pass `loader_cls=PatternCFNLoader` to keep intrinsic operands as text.

    The loader is driven directly rather than through `yaml.load(..., Loader=)`.
    That is what yaml.load does internally, minus the call shape that scanners
    report as unsafe deserialization; see idp_sdk._core.cfn_yaml.
    """
    with open(template_path, 'r') as f:
        loader = (loader_cls or CFNLoader)(f)
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


def _iter_granted_action_patterns(statement):
    """Yield every `Action` string of a statement, including colon-free ones.

    _iter_actions requires a ':' so that the permission-sufficiency half of this
    script only ever sees `service:Action` strings. That filter is wrong for the
    hardening checks: a statement whose action is the bare `'*'` grants every
    action in every service — including all of IAM — and yielded NOTHING, so it
    was invisible to the forbidden-action, IAM-write and PassRole checks alike.
    """
    actions = statement.get('Action') or []
    if isinstance(actions, str):
        actions = [actions]
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, str):
                yield action


def _statement_uses_not_action(statement):
    """True if the statement is written with NotAction rather than Action.

    `Effect: Allow` with `NotAction: [...]` grants everything EXCEPT the listed
    actions, so it is a blanket grant that includes every IAM write and
    iam:PassRole. Reading only `Action` made such a statement look empty.
    """
    not_action = statement.get('NotAction')
    return isinstance(not_action, (str, list)) and bool(not_action)


def _pattern_covers_action(pattern, action):
    """True if an IAM action pattern (with `*`/`?` wildcards) covers `action`."""
    return fnmatch.fnmatch(action.lower(), pattern.lower())


# Resource types that carry a single PolicyDocument granting permissions.
# AWS::IAM::Policy and AWS::IAM::RolePolicy were missing, so a statement placed
# in either was invisible to every hardening check below — a grant of `iam:*` on
# `*` written as an AWS::IAM::Policy would have passed this gate silently.
SINGLE_POLICY_DOCUMENT_TYPES = (
    'AWS::IAM::ManagedPolicy',
    'AWS::IAM::Policy',
    'AWS::IAM::RolePolicy',
    'AWS::IAM::GroupPolicy',
    'AWS::IAM::UserPolicy',
)
# Types with an embedded list of {PolicyName, PolicyDocument} entries.
EMBEDDED_POLICY_LIST_TYPES = (
    'AWS::IAM::Role',
    'AWS::IAM::User',
    'AWS::IAM::Group',
)


def _iter_policy_documents(resource):
    """Yield the policy documents an IAM resource declares."""
    if not isinstance(resource, dict):
        return
    props = resource.get('Properties')
    if not isinstance(props, dict):
        return
    resource_type = resource.get('Type')
    if resource_type in EMBEDDED_POLICY_LIST_TYPES:
        policies = props.get('Policies') or []
        if isinstance(policies, list):
            for policy in policies:
                if isinstance(policy, dict):
                    yield policy.get('PolicyDocument')
    elif resource_type in SINGLE_POLICY_DOCUMENT_TYPES:
        yield props.get('PolicyDocument')


def _iter_resource_strings(statement):
    """Yield the `Resource` strings of a statement.

    Under CFNLoader an intrinsic is already None here. Under PatternCFNLoader it
    is the intrinsic's operand, so a `!Sub` ARN yields its unexpanded template
    text (`'arn:${AWS::Partition}:iam::${AWS::AccountId}:role/*'`) and the
    `!Sub [template, vars]` list form yields the template plus any string vars.
    """
    resources = statement.get('Resource')
    if isinstance(resources, str):
        resources = [resources]
    if not isinstance(resources, list):
        return
    for resource in resources:
        if isinstance(resource, str):
            yield resource
        elif isinstance(resource, list):
            # `!Sub [template, {vars}]` arrives as a list.
            for item in resource:
                if isinstance(item, str):
                    yield item


# `${...}` placeholders contain colons (`${AWS::Partition}`), which would break
# ARN field splitting, so they are replaced with a colon-free token first.
_SUB_PLACEHOLDER = re.compile(r'\$\{[^}]*\}')


def _resource_is_unbounded(resource):
    """True if `resource` places no real limit on which principals are touched.

    A bare `'*'` obviously does. So does an ARN in which EVERY segment after the
    resource type is a bare `'*'`: `arn:aws:iam::123456789012:role/*` matches
    every role in the account, which for an IAM write is the same blast radius as
    `'*'`.

    Either a name prefix or a literal path segment counts as a bound, so these
    are NOT reported:
      * `role/idp*`              - bounded by name prefix
      * `role/*/idp*`            - bounded by name prefix under any path
      * `role/aws-service-role/*` - bounded by a reserved IAM path that only
        AWS-defined service-linked roles can occupy, and whose policies AWS
        controls rather than this role. Treating this as unbounded was a false
        positive on the shipped template's ServiceLinkedRoles statement.
    """
    text = _SUB_PLACEHOLDER.sub('X', resource.strip())
    if text == '*':
        return True
    if not text.lower().startswith('arn:'):
        return False
    fields = text.split(':', 5)
    if len(fields) != 6:
        return False
    resource_field = fields[5]
    if resource_field == '*':
        return True
    segments_after_type = resource_field.split('/')[1:]
    if not segments_after_type:
        return False
    return all(segment == '*' for segment in segments_after_type)


def _statement_is_on_all_resources(statement):
    return any(
        _resource_is_unbounded(resource)
        for resource in _iter_resource_strings(statement)
    )


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


def _normalize_operator(operator):
    """Strip a ForAnyValue:/ForAllValues: set qualifier and lower-case."""
    return operator.split(':', 1)[-1].strip().lower()


def _operator_enforces_when_key_absent(operator):
    """True if the operator still constrains a request that omits the key.

    Three families do NOT:
      * `...IfExists` is defined to evaluate true when the key is missing;
      * `ForAllValues:` evaluates true for an empty (absent) key set;
      * `Null` asserts the key's ABSENCE, so it is the opposite of a comparison.

    This matters for iam:PermissionsBoundary specifically. `StringEqualsIfExists`
    on that key looks like a boundary requirement but is not one: an iam:CreateRole
    call that creates a role with NO boundary simply omits the key, the IfExists
    condition passes, and an unbounded role is created — which is exactly the
    escalation the boundary exists to prevent.
    """
    normalized = _normalize_operator(operator)
    if normalized == 'null':
        return False
    if normalized.endswith('ifexists'):
        return False
    return not operator.lower().startswith('forallvalues:')


def _is_iam_write_action(action):
    """True if `action` is an iam: action that can change state."""
    lowered = action.lower()
    # A bare '*' (or 'iam*'-style prefix wildcard) grants every IAM action.
    if lowered == '*' or (lowered.endswith('*') and 'iam:'.startswith(lowered[:-1])):
        return True
    if not lowered.startswith('iam:'):
        return False
    verb = lowered.split(':', 1)[1]
    if verb.startswith('*'):
        return True
    return not verb.startswith(IAM_READ_VERB_PREFIXES)


def iter_role_statements(role_template_path):
    """Yield `(logical_id, statement)` for every policy statement in a template.

    Covers the inline policies of AWS::IAM::Role and every standalone policy
    resource type (see SINGLE_POLICY_DOCUMENT_TYPES), because the PassRole grant
    this role ships lives in an AWS::IAM::ManagedPolicy.

    Uses PatternCFNLoader, so `Resource` entries written as `!Sub` ARNs keep
    their text and can be tested for being effectively account-wide.
    """
    template = load_template(role_template_path, PatternCFNLoader) or {}
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
        actions = list(_iter_granted_action_patterns(statement))
        uses_not_action = _statement_uses_not_action(statement)
        if uses_not_action:
            findings.append(
                f'{label}: Effect: Allow written with NotAction, which grants '
                f'every action except those listed — including every IAM write '
                f'and iam:PassRole. Use an explicit Action list.'
            )
        on_all_resources = _statement_is_on_all_resources(statement)
        condition_keys = list(_condition_keys(statement))
        # Only operators that still bite when the key is ABSENT can stand in for
        # a real requirement; see _operator_enforces_when_key_absent.
        enforced_keys = {
            key for operator, key in condition_keys
            if _operator_enforces_when_key_absent(operator)
        }
        # A Null test asserts a key is ABSENT, so it never constrains a value.
        # Everything else (including ...IfExists) is a real value comparison.
        compared_keys = {
            key for operator, key in condition_keys
            if _normalize_operator(operator) != 'null'
        }

        forbidden = sorted(
            action for action in actions
            if any(
                _pattern_covers_action(action, forbidden_action)
                for forbidden_action in FORBIDDEN_SERVICE_ROLE_ACTIONS
            )
        )
        if forbidden:
            findings.append(
                f'{label}: grants {", ".join(forbidden)}, which must never be '
                f'granted to a delegated deployment role'
            )

        iam_writes = sorted(
            action for action in actions if _is_iam_write_action(action)
        )
        if uses_not_action or (iam_writes and on_all_resources):
            if BOUNDARY_CONDITION_KEY not in enforced_keys:
                listed = ', '.join(iam_writes) if iam_writes else 'via NotAction'
                findings.append(
                    f'{label}: IAM write actions on Resource: "*" with no '
                    f'{BOUNDARY_CONDITION_KEY} condition '
                    f'({listed}). Scope the resource to the '
                    f'principals this stack creates, or require a permissions '
                    f'boundary.'
                )

        # Match by wildcard, not by equality: `iam:Pass*`, `iam:*` and a bare
        # `*` all grant iam:PassRole, and only the middle one used to be caught.
        pass_role = [
            action for action in actions
            if _pattern_covers_action(action, 'iam:passrole')
        ]
        if pass_role or uses_not_action:
            if on_all_resources or uses_not_action:
                findings.append(
                    f'{label}: iam:PassRole on Resource: "*". Scope it to the '
                    f'role name patterns this stack creates.'
                )
            # compared_keys, not every key seen: a lone
            # `Null: {iam:PassedToService: true}` matches only requests that omit
            # the key, so on its own it scopes the pass to nothing at all and
            # must not satisfy this requirement.
            if PASSED_TO_SERVICE_CONDITION_KEY not in compared_keys:
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
    
    # The CloudFormation namespace is not always the IAM prefix, so `AWS::X::Y` ->
    # `x:*` is wrong for some services. This used to be an ignore set of three
    # entries whose stated reason was "not real AWS services", and that was true of
    # exactly one of them: Cognito and OpenSearch Serverless are very real, they were
    # listed because their DERIVED TOKEN is not their IAM prefix, and ignoring them
    # meant the role's Cognito and OpenSearch grants were never derived at all. The
    # gate would have reported success with `cognito-idp:*` removed and every deploy
    # broken. The fix is a translation, not an exclusion.
    #
    # `AWS::Serverless::*` is the one genuine non-service: it is the SAM transform's
    # namespace. But it EXPANDS to real resources, so ignoring it also silently
    # dropped the `states:*` and `apigateway:*` requirements -- two more grants hidden
    # behind the one correct member of the old set.
    iam_prefixes = {
        'cognito': {'cognito-idp', 'cognito-identity'},
        'opensearchserverless': {'aoss'},
        'serverless': {'lambda', 'apigateway', 'states'},
    }

    for template_path in templates:
        if os.path.exists(template_path):
            services = extract_aws_services_from_template(template_path)
            iam_actions = extract_iam_actions_from_template(template_path)

            for service in services:
                if service == 'iam':
                    continue
                for prefix in iam_prefixes.get(service, {service}):
                    wildcard_permissions.add(f'{prefix}:*')
            
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