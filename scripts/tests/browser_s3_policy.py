# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the browser may read from S3, derived from ``template.yaml``.

``CognitoIdentityPoolSetRole`` attaches a **single** ``authenticated`` role with no
``RoleMappings``, so Cognito group membership plays no part in which role a signed-in
user assumes. Every bucket that role names is therefore readable by every
authenticated user irrespective of group, with **no resolver in the path** — so no
group check, no per-document check and no per-user scope check is available to apply
to it. That makes this one IAM policy, not the API surface, the boundary for anything
it names.

Two gates read that set and they must read the *same* set:

* ``test_browser_s3_grants.py`` — the policy itself: which buckets it may name.
* ``test_iam_prose_consistency.py`` — whether the documents that enumerate the set
  agree with it.

Hence one parser here rather than one in each. Two copies of a policy reader is the
same defect class the rest of this tree worries about: they diverge, and the one that
diverges is the one nobody re-reads.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "template.yaml"

#: The Identity Pool's authenticated role — the one the browser assumes.
ROLE = "CognitoAuthorizedRole"


class UnreadablePolicyShape(Exception):
    """A policy this reader cannot evaluate, raised rather than skipped.

    ⚠️ **This is the one structural defect every blind spot in this gate has had.**
    Ten routes past it were found across three review rounds, and every one was the
    same thing: an intrinsic the reader does not model is silently dropped, and
    dropping fails **open**. ``flatten_resource`` says so at the ``Resource`` level
    ("a dropped ``Resource`` reads as 'no bucket named', which is the direction that
    fails open") and the same policy applied unremarked one and two levels up — at the
    ``Policies`` entry (``- !If [Cond, {PolicyName: S3, ...}, !Ref AWS::NoValue]``,
    which ``cfn-lint`` accepts and which this template already uses the ``!If`` form of
    on the very same role) and inside an ``Action`` list (``Action: !If [Cond,
    ["ssm:GetParameter", "s3:GetObject"], ["ssm:GetParameter"]]``, which also hides
    inside a policy whose *name* is already expected, so the policy-set pin does not
    fire).

    Adding a recognizer per route does not converge: each one closes the shape that
    was tried and leaves the next. What converges is refusing to read a shape at all.
    So the readers below raise this for anything they cannot evaluate as a literal,
    and the gate turns it into a failure that names the resource and the shape.

    A deliberate ``!If`` on a policy of this role is then a change that has to be
    made deliberately: model it here, or restructure the template. Both are better
    than a green gate.
    """


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _literal_actions(statement: dict, key: str, where: str) -> list:
    """The ``Action``/``NotAction`` entries of one statement, as literal strings.

    Raises :class:`UnreadablePolicyShape` for an entry that is not a string — an
    intrinsic, a nested list, anything. ``str(entry).startswith("s3:")`` on a
    ``{"Fn::If": [...]}`` is ``False``, which reads as "no S3 action here" for a
    statement that grants one in at least one branch.
    """
    entries = _as_list(statement.get(key))
    for entry in entries:
        if not isinstance(entry, str):
            raise UnreadablePolicyShape(
                f"{where}: a {key} entry is {entry!r}, which this reader cannot "
                "evaluate as a literal action. An intrinsic here reads as 'no S3 "
                "action' however its branches are written."
            )
    return [entry.lower() for entry in entries]


@lru_cache(maxsize=1)
def _load_template() -> dict:
    """``template.yaml`` parsed with CloudFormation short-form tags left opaque.

    Resolving ``!Sub``/``!Ref``/``!GetAtt`` is not the point — only which logical ids a
    ``Resource`` names — so each tag becomes a single-key dict the flattener below
    understands.
    """
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def _tag(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
        return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}

    Loader.add_multi_constructor("!", _tag)
    return yaml.load(TEMPLATE.read_text(), Loader=Loader)  # nosec B506 - SafeLoader subclass


def collect_s3_statements(policies) -> list:
    """``(policy_name, statement)`` for every S3 statement in ``policies``.

    Split out from :func:`role_s3_statements` so the collector can be driven over a
    **synthetic** policy list. That matters now that the live role grants no S3: every
    assertion in ``test_browser_s3_grants.py`` is an absence, and an absence is also
    what a collector that has stopped recognising a grant reports. Exercising it on a
    policy list that does grant S3 is the only thing that distinguishes "there is no
    grant" from "this reader can no longer see one".

    Collected across **all** of the role's inline policies rather than the one named
    ``S3``: a grant added under another policy name would be just as effective, and
    pinning only the policy that happened to hold them is how a gate stops seeing its
    subject.
    """
    statements = []
    for index, policy in enumerate(_as_list(policies)):
        where = f"{ROLE}.Policies[{index}]"
        if not isinstance(policy, dict):
            raise UnreadablePolicyShape(
                f"{where} is {policy!r}, not a policy object. A `!If` wrapping a "
                "policy entry looks like this, and the grant inside it is live in "
                "whichever branch the condition selects."
            )
        name = policy.get("PolicyName")
        document = policy.get("PolicyDocument")
        if not isinstance(name, str) or not isinstance(document, dict):
            raise UnreadablePolicyShape(
                f"{where} has PolicyName={name!r} and a "
                f"{type(document).__name__} PolicyDocument. Both must be literal for "
                "this reader to evaluate the policy at all; an entry it skips reads "
                "as a policy that grants nothing."
            )
        for position, statement in enumerate(_as_list(document.get("Statement"))):
            if not isinstance(statement, dict):
                raise UnreadablePolicyShape(
                    f"{where}.{name}.Statement[{position}] is {statement!r}, not a "
                    "statement object."
                )
            if statement_reaches_s3(statement, f"{where}.{name}[{position}]"):
                statements.append((name, statement))
    return statements


def statement_reaches_s3(statement: dict, where: str = "a statement") -> bool:
    """Whether one ``Allow`` statement can authorize an S3 action.

    Two things this answers that a ``"s3:GetObject" in statement["Action"]`` test does
    not, and each was a live false negative on a gate whose claim is "no S3 at all":

    * **Case.** IAM action names are case-INSENSITIVE, so ``S3:GetObject`` is a real
      grant. The comparison is lower-cased.
    * **``NotAction``.** ``{"Effect": "Allow", "NotAction": ["iam:*"], "Resource":
      "*"}`` grants every action except IAM — which is strictly broader than the grant
      #1033 removed — and a reader that looks only at ``Action`` sees an empty list and
      reports nothing. An ``Allow`` with ``NotAction`` reaches S3 unless ``s3:``
      appears in the exclusion, and even an ``s3:`` entry there excludes only what it
      names, so anything short of a bare ``s3:*`` or ``*`` is still a reach. A
      ``NotAction`` under ``Deny`` is not a grant and is ignored.

    ⚠️ A ``Condition`` is deliberately **not** read. A condition can narrow a grant to
    nothing in principle, but deciding that requires evaluating IAM's semantics, and
    reading a statement as "conditioned, therefore harmless" is the direction that
    fails open. A conditioned S3 grant on this role is reported, and the decision to
    keep it belongs in a registry entry rather than in this predicate.
    """
    effect = statement.get("Effect", "Allow")
    if not isinstance(effect, str):
        raise UnreadablePolicyShape(
            f"{where}: Effect is {effect!r}, which this reader cannot evaluate as a "
            "literal. An intrinsic here decides whether the statement grants anything."
        )
    if effect.lower() != "allow":
        return False

    actions = _literal_actions(statement, "Action", where)
    if any(a.startswith("s3:") or a == "*" for a in actions):
        return True

    # Named `all_but` rather than `not_actions`: `NOT_A` is a fragment in
    # `exemption_discovery.NAME_VOCABULARY`, so the latter is discovered as an
    # exemption surface and demands a registry entry for a local variable. The
    # registry's own doctrine says a false positive is the costly direction there,
    # because asking for judgements on noise is how a reviewer learns to
    # rubber-stamp them.
    all_but = _literal_actions(statement, "NotAction", where)
    if all_but:
        # Everything EXCEPT the listed actions. Only a blanket exclusion of S3 keeps
        # this statement away from S3.
        return not any(a in ("s3:*", "*") for a in all_but)
    return False


def _resources_of_type(*types) -> dict:
    """``{logical_id: properties}`` for every resource of the given types.

    Raises :class:`UnreadablePolicyShape` for a ``Resources`` entry with no literal
    ``Type``. A bare ``Fn::Transform`` is the shape that matters: ``Resources:
    {MyTransform: {Fn::Transform: {Name: AWS::Include, Parameters: {Location: s3://…}}}}``
    is valid CloudFormation that splices in arbitrary resources from S3, so **no gate
    in this repository could read what it adds**. The honest control is therefore to
    refuse the transform rather than to try to read through it — the same reasoning as
    everywhere else here: a shape that is skipped reads as "no such resource", which
    fails open.
    """
    found = {}
    for logical, resource in (_load_template().get("Resources") or {}).items():
        if not isinstance(resource, dict):
            raise UnreadablePolicyShape(
                f"Resources.{logical} is {resource!r}, not a resource object."
            )
        resource_type = resource.get("Type")
        if not isinstance(resource_type, str):
            raise UnreadablePolicyShape(
                f"Resources.{logical} has Type={resource_type!r}. A resource with no "
                "literal Type — an `Fn::Transform` splicing content in from S3, say — "
                "can add anything, and nothing in this repository can read what. "
                "Declare the resources instead of transforming them in."
            )
        if resource_type in types:
            found[logical] = resource.get("Properties") or {}
    return found


#: Resource types that attach a policy document to an existing role, and the property
#: each uses to name the role. ``AWS::IAM::RolePolicy`` is the one most easily missed:
#: it is the closest sibling of ``AWS::IAM::Policy`` and differs only in using the
#: singular ``RoleName``, so a reader matching on ``Roles`` sees nothing.
ROLE_ATTACHING_TYPES = {
    "AWS::IAM::Policy": ("Roles",),
    "AWS::IAM::ManagedPolicy": ("Roles",),
    "AWS::IAM::RolePolicy": ("RoleName",),
}


def policy_names_role(properties: dict) -> bool:
    """Whether a standalone policy names :data:`ROLE` as one of its roles.

    Reads **every** role-naming property in :data:`ROLE_ATTACHING_TYPES`, not only
    ``Roles``: ``AWS::IAM::RolePolicy`` uses the singular ``RoleName`` and is otherwise
    the same idiom, so a check on ``Roles`` alone misses an inline policy attached to
    this role by the resource type closest to the one it does read.

    Public so a test can drive it over the spellings a template might use. The real
    template has no such policy on this role, so without a synthetic probe a
    recognizer that matched nothing would read identically to a tree with none.
    """
    for names in ROLE_ATTACHING_TYPES.values():
        for name in names:
            for entry in _as_list(properties.get(name)):
                for text in flatten_resource(entry):
                    if ROLE in str(text):
                        return True
    return False


def role_attached_policies() -> dict:
    """``{logical_id: policy_document}`` for standalone policies naming :data:`ROLE`.

    ``AWS::IAM::Policy`` and ``AWS::IAM::ManagedPolicy`` resources that list a role in
    their ``Roles`` attach to it just as effectively as an inline entry under the
    role's own ``Policies``, and nothing in the role resource mentions them. This
    template already uses that idiom twice — to break a circular dependency — so it
    is an available route rather than a hypothetical one, and a reader of the role
    resource would not see a grant added that way at all.
    """
    found = {}
    for logical, properties in _resources_of_type(*ROLE_ATTACHING_TYPES).items():
        if policy_names_role(properties):
            found[logical] = properties.get("PolicyDocument") or {}
    return found


def authenticated_role_logical_id() -> Optional[str]:
    """The role the Identity Pool actually attaches as ``authenticated``.

    :data:`ROLE` is a literal, and every docstring in the two gates that read it rests
    on the premise that this is the role the browser assumes. Nothing asserted that
    premise: adding a second role carrying the old S3 policy and repointing
    ``CognitoIdentityPoolSetRole`` at it left both gates reading a role nobody
    assumes, with every assertion green. Derived here so the premise can be checked —
    see ``test_the_identity_pool_still_attaches_the_role_this_gate_reads``.

    ``None`` when the attachment cannot be read at all, which that test treats as a
    failure rather than as agreement.
    """
    attachments = _resources_of_type("AWS::Cognito::IdentityPoolRoleAttachment")
    for logical, properties in sorted(attachments.items()):
        roles = properties.get("Roles")
        if not isinstance(roles, dict):
            raise UnreadablePolicyShape(
                f"{logical}.Roles is {roles!r}, so which role the browser assumes "
                "cannot be read — and that is the premise of every assertion in "
                "test_browser_s3_grants.py."
            )
        authenticated = roles.get("authenticated")
        if authenticated is None:
            continue
        # ⚠️ Exactly ONE literal reference, or refuse. Taking the FIRST regex match out
        # of a flattened intrinsic is how this function read
        # `!If [Cond, !GetAtt RoleA.Arn, !GetAtt RoleB.Arn]` as "RoleA": the comparison
        # against ROLE then passed while the browser assumed RoleB in the other
        # condition branch. This is the same dropped-intrinsic defect as
        # `UnreadablePolicyShape` covers one level down, and it matters more here,
        # because this value is what makes the rest of the file about the right role at
        # all.
        names = set()
        for text in flatten_resource(authenticated):
            for match in re.finditer(
                r"([A-Za-z0-9]+)\.Arn|\$\{([A-Za-z0-9]+)\}", str(text)
            ):
                names.add(match.group(1) or match.group(2))
        if len(names) != 1:
            raise UnreadablePolicyShape(
                f"{logical}.Roles.authenticated is {authenticated!r}, which names "
                f"{sorted(names) or 'no role'} rather than exactly one. An intrinsic "
                "here means the browser assumes a different role in a different "
                "condition branch, and this reader cannot say which."
            )
        return names.pop()
    return None


#: CloudFormation resource types that deploy another template, and the property each
#: uses to pass values into it.
NESTED_STACK_TYPES = {
    "AWS::CloudFormation::Stack": "Parameters",
    "AWS::Serverless::Application": "Parameters",
}


def nested_stacks_given_the_role() -> dict:
    """``{logical_id: [parameter names]}`` for nested stacks passed :data:`ROLE`.

    The twelfth route past this gate, and the reason it needs its own assertion: the
    readers above open ``template.yaml`` and nothing else, so an ``AWS::IAM::Policy``
    inside ``nested/api-resolvers/template.yaml`` with ``Roles: [!Ref
    BrowserRoleName]`` — the parent passing ``BrowserRoleName: !Ref
    CognitoAuthorizedRole`` — attaches to this role and is invisible here. This
    repository is built on nested stacks, so the route is ordinary rather than exotic.

    Asserted this way round deliberately. Reading every discovered template for
    role-attaching resources would also work and would be more faithful, but it is a
    bigger mechanism with its own blind spots (an `Fn::ImportValue`, a template the
    discovery misses). **A nested stack cannot attach a policy to a role it was never
    told about**, so refusing to pass the role down closes the class with one check —
    at the cost of refusing a legitimate future reason to pass it, which is then a
    decision someone makes rather than a gap nobody sees.
    """
    found = {}
    for resource_type, parameters_key in NESTED_STACK_TYPES.items():
        for logical, properties in _resources_of_type(resource_type).items():
            parameters = properties.get(parameters_key) or {}
            if not isinstance(parameters, dict):
                raise UnreadablePolicyShape(
                    f"{logical}.{parameters_key} is {parameters!r}, so what this "
                    "nested stack is passed cannot be read."
                )
            named = [
                name
                for name, value in parameters.items()
                if any(ROLE in str(text) for text in flatten_resource(value))
            ]
            if named:
                found[logical] = sorted(named)
    return found


def identity_pool_allows_unauthenticated() -> bool:
    """Whether the Identity Pool hands credentials to a caller with no login.

    ⚠️ **This is the premise the rest of this module argues from, and nothing read
    it.** Every message here says "every **authenticated** user", which assumes only
    an authenticated user obtains credentials at all — and that assumption is one line
    of the template, ``AllowUnauthenticatedIdentities: false``.

    Flipped to ``true``, with an ``unauthenticated`` role beside the authenticated one,
    the outcome is **strictly worse than the hole #1033 closed**: that chain needed a
    self-registered account and its ID token, and this needs neither — ``get-id``
    followed by ``get-credentials-for-identity`` with no logins at all yields the
    unauthenticated role. It is also an ordinary change to make for an ordinary
    reason (a guest preview, a public demo), by somebody with no cause to think they
    were touching this.

    Raises :class:`UnreadablePolicyShape` for anything but a literal boolean, because
    an intrinsic here decides the premise per condition branch.
    """
    for logical, properties in sorted(
        _resources_of_type("AWS::Cognito::IdentityPool").items()
    ):
        allowed = properties.get("AllowUnauthenticatedIdentities")
        if not isinstance(allowed, bool):
            raise UnreadablePolicyShape(
                f"{logical}.AllowUnauthenticatedIdentities is {allowed!r}, not a "
                "literal boolean, so whether an unauthenticated caller can obtain "
                "credentials at all cannot be read — and that is the premise of every "
                "assertion in test_browser_s3_grants.py."
            )
        if allowed:
            return True
    return False


def identity_pool_unauthenticated_roles() -> dict:
    """``{attachment_logical_id: role_reference}`` for any ``unauthenticated`` role.

    Expected empty. The attachment's ``Roles`` map may carry an ``unauthenticated``
    key as well as an ``authenticated`` one, and nothing read it: that role is assumed
    with no login, so whatever it grants is reachable by anyone who can reach the
    Identity Pool.
    """
    found = {}
    for logical, properties in sorted(
        _resources_of_type("AWS::Cognito::IdentityPoolRoleAttachment").items()
    ):
        roles = properties.get("Roles")
        if isinstance(roles, dict) and "unauthenticated" in roles:
            found[logical] = roles["unauthenticated"]
    return found


def identity_pool_role_mappings() -> dict:
    """Any ``RoleMappings`` on the Identity Pool attachment.

    Expected empty. The *reason* every bucket this role names is readable by every
    signed-in user irrespective of group is that there are none — so a gate asserting
    that consequence should also read the premise. A deployment that grew role
    mappings would make this file's whole argument wrong rather than merely incomplete.
    """
    mappings = {}
    for properties in _resources_of_type(
        "AWS::Cognito::IdentityPoolRoleAttachment"
    ).values():
        found = properties.get("RoleMappings")
        if isinstance(found, dict):
            mappings.update(found)
    return mappings


#: Bucket logical ids whose objects are documents. A resource-based policy on one of
#: these authorizes a read on its own, so what it grants has to be read even though
#: this module is otherwise about an identity policy.
DOCUMENT_BUCKETS = ("InputBucket", "OutputBucket")

#: Resource types that carry a resource-based policy over a bucket, and the properties
#: each uses to name the bucket and hold the document. An **access point** is a
#: separate policy namespace over the same objects — it authorizes a read on its own,
#: exactly as a bucket policy does — so reading only ``AWS::S3::BucketPolicy`` would
#: leave it uncovered. None of these is used in this template today; they are here
#: because the gate's claim is about what can reach the objects, not about what the
#: template currently happens to declare.
BUCKET_POLICY_TYPES = {
    "AWS::S3::BucketPolicy": ("Bucket", "PolicyDocument"),
    "AWS::S3::AccessPoint": ("Bucket", "Policy"),
    "AWS::S3::MultiRegionAccessPointPolicy": ("MrapName", "Policy"),
}


def _principal_texts(statement: dict, where: str) -> list:
    """Every principal of one statement, flattened to strings."""
    principal = statement.get("Principal")
    texts = []
    if isinstance(principal, dict):
        for value in principal.values():
            for entry in _as_list(value):
                texts.extend(str(t) for t in flatten_resource(entry))
    elif principal is not None:
        for entry in _as_list(principal):
            texts.extend(str(t) for t in flatten_resource(entry))
    if principal is not None and not texts:
        raise UnreadablePolicyShape(
            f"{where}: Principal is {principal!r} and this reader extracted no text "
            "from it, so it cannot say whether the browser's role is included."
        )
    return texts


def principal_may_include_role(texts) -> bool:
    """Whether a flattened principal could include :data:`ROLE`.

    ⚠️ **Asked this way round on purpose.** The first version of this check looked for
    the literal ``CognitoAuthorizedRole`` in the principal, which meant an
    **account-wide** principal — ``arn:…:iam::${AWS::AccountId}:root``, the idiomatic
    way a bucket policy over-grants, and *broader* than the grant #1033 removed — was
    never tested against :func:`statement_reaches_s3` at all. The same held for
    ``Principal: "*"`` with a ``Condition``, and for ``{"AWS": {"Ref":
    "AWS::AccountId"}}``.

    So the question is not "does this name the role" but "can this reader rule the
    role out", and only a named service principal can. Anything else — the account,
    a wildcard, a reference this module does not model — counts as possibly
    including it, which is the direction that fails closed.
    """
    for text in texts:
        lowered = text.strip().lower()
        # The ONE shape that rules the role out, and it is computed rather than
        # listed. An AWS **service** principal (`cloudfront.amazonaws.com`,
        # `logging.s3.amazonaws.com`, …) is not an IAM role in this account, so a
        # grant to one cannot be a grant to the browser. A list of the service
        # principals this template happens to use today would be dead config on top
        # of this rule — it would shield nothing the rule does not already shield —
        # so there is deliberately no allowlist here to keep in step.
        #
        # ⚠️ `iam::` is excluded because an IAM ARN also ends in nothing useful:
        # `arn:aws:iam::123456789012:role/X` does not end in `.amazonaws.com`, but a
        # `Sub` that renders one could contain both, and reading such a string as a
        # service principal would be the fail-open direction.
        if lowered.endswith(".amazonaws.com") and "iam::" not in lowered:
            continue
        return True
    return False


def bucket_policies_reaching_documents() -> dict:
    """``{logical_id: [(statement_index, principal_texts)]}`` for the risky statements.

    The other side of the authorization. For a same-account caller, S3 needs an
    explicit ``Allow`` in **either** the identity policy or the bucket policy — so an
    identity policy granting nothing does not bound what the browser's role can read
    if a bucket policy on a document bucket allows a principal that includes it. This
    template already declares bucket policies, so the route is available.

    Only the document buckets are read, because those are the ones whose objects this
    gate is about; a Logging-bucket policy granting a log-delivery service is not a
    statement about the browser.
    """
    found = {}
    for resource_type, keys in BUCKET_POLICY_TYPES.items():
        bucket_key, document_key = keys
        for logical, properties in _resources_of_type(resource_type).items():
            found.update(
                _risky_statements(logical, properties, bucket_key, document_key)
            )
    return found


def _risky_statements(logical, properties, bucket_key, document_key) -> dict:
    """``{logical_id: [(index, principals)]}`` for one resource-based policy."""
    bucket_refs = set()
    for text in flatten_resource(properties.get(bucket_key)):
        bucket_refs.update(
            m.group(1) or m.group(2) for m in _BUCKET_REF_RE.finditer(str(text))
        )
    if not bucket_refs & set(DOCUMENT_BUCKETS):
        return {}
    document = properties.get(document_key) or {}
    risky = []
    for index, statement in enumerate(_as_list(document.get("Statement"))):
        if not isinstance(statement, dict):
            raise UnreadablePolicyShape(
                f"{logical}.{document_key}.Statement[{index}] is {statement!r}, "
                "not a statement object."
            )
        where = f"{logical}.Statement[{index}]"
        texts = _principal_texts(statement, where)
        if not texts:
            continue
        if principal_may_include_role(texts) and statement_reaches_s3(statement, where):
            risky.append((index, sorted(texts)))
    return {logical: risky} if risky else {}


def role_managed_policy_arns() -> list:
    """The ``ManagedPolicyArns`` on :data:`ROLE`, flattened to strings.

    ⚠️ **Their contents are unreadable from here**, which is the point: an AWS- or
    customer-managed policy lives outside this template, so a gate cannot tell what
    it grants. The only sound assertion is therefore about the *list*, not about what
    is in it — see ``test_the_role_attaches_no_managed_policy``.
    """
    role = _load_template()["Resources"][ROLE]
    arns = []
    for entry in _as_list(role["Properties"].get("ManagedPolicyArns")):
        arns.extend(str(t) for t in flatten_resource(entry))
    return arns


def role_policies() -> dict:
    """``{policy_name: policy_document}`` for every inline policy on :data:`ROLE`.

    The whole set, not only the S3-bearing ones. ``test_browser_s3_grants.py`` pins it
    so that a grant of **any** service has to be chosen rather than noticed: the KMS
    statement that sat beside the removed S3 one was not an S3 action, so no
    S3-shaped assertion would ever have objected to it.
    """
    role = _load_template()["Resources"][ROLE]
    found = {}
    for index, policy in enumerate(_as_list(role["Properties"].get("Policies"))):
        if not isinstance(policy, dict) or not isinstance(
            policy.get("PolicyName"), str
        ):
            # Refused rather than skipped: an entry with no literal `PolicyName` —
            # an `!If`, say — is absent from this mapping, and the policy-set
            # assertion then compares equal while the grant inside it is live.
            raise UnreadablePolicyShape(
                f"{ROLE}.Policies[{index}] has no literal PolicyName "
                f"({policy!r}), so it is invisible to the policy-set assertion."
            )
        found[str(policy["PolicyName"])] = policy.get("PolicyDocument") or {}
    return found


@lru_cache(maxsize=1)
def _role_s3_statements_cached() -> tuple:
    """``(policy_name, statement)`` for every S3 statement reaching :data:`ROLE`.

    Both routes: the role's own inline ``Policies``, and any standalone
    ``AWS::IAM::Policy`` / ``AWS::IAM::ManagedPolicy`` resource that names it. The
    second was invisible until the grant went to zero made it worth closing — while
    the gate pinned *which* buckets the inline policy named, a grant arriving by the
    other route would have read as an unpinned extra; now that the claim is "no S3 at
    all", it would have read as nothing.
    """
    role = _load_template()["Resources"][ROLE]
    statements = list(collect_s3_statements(role["Properties"].get("Policies")))
    for logical, document in sorted(role_attached_policies().items()):
        statements.extend(
            (f"{logical}(attached)", statement)
            for _name, statement in collect_s3_statements(
                [{"PolicyName": logical, "PolicyDocument": document}]
            )
        )
    return tuple(statements)


def role_s3_statements() -> list:
    """Every S3 statement on :data:`ROLE`, as a fresh list.

    Thin wrapper over the cached derivation. Parsing a 14,000-line template is not
    free, and the prose gate derives this once per markdown file in the repository --
    which took the suite from seconds to minutes before the cache existed. A new list
    is returned each call so a caller cannot mutate the cached tuple.
    """
    return list(_role_s3_statements_cached())


def flatten_resource(node) -> Iterator[str]:
    """One ``Resource`` entry, as zero or more matchable strings.

    CloudFormation can name the same bucket six ways, and a matcher that reads two of
    them supports a much weaker claim than "the set cannot grow back unnoticed":

    ============================================  ==========================
    Idiom                                         Flattened to
    ============================================  ==========================
    ``!Sub "arn:...:::${Bucket}/*"``              the string, as written
    ``!GetAtt Bucket.Arn`` (scalar short form)    ``Bucket.Arn``
    ``Fn::GetAtt: [Bucket, Arn]`` (list form)     ``Bucket.Arn``
    ``!Ref Bucket``                               ``${Bucket}``
    ``!Join`` over any of the above               each part, recursively
    ``"*"``                                       the string, as written
    ============================================  ==========================

    The list form of ``Fn::GetAtt`` is the one that matters most: joining its parts
    with a space rather than the ``.`` that ``GetAtt`` means left it matching neither
    the ``${...}`` alternative nor the ``X.Arn`` one, so a bucket named that way was
    invisible to every assertion downstream.

    An intrinsic this function does not model has its string parts yielded anyway: a
    dropped ``Resource`` reads as "no bucket named", which is the direction that fails
    open.
    """
    if isinstance(node, str):
        yield node
        return
    if not isinstance(node, dict):
        return
    for fn, value in node.items():
        if fn == "Fn::GetAtt":
            if isinstance(value, list):
                yield ".".join(str(v) for v in value)
            else:
                yield str(value)
        elif fn in ("Fn::Ref", "Ref"):
            # A `Ref` to a bucket yields its NAME rather than its ARN — still a
            # reference to that bucket. Rendered in the `${...}` shape so one pattern
            # covers it and `!Sub`.
            yield "${" + str(value) + "}"
        elif fn == "Fn::Sub":
            # Either a string or `[template, {vars}]`; the `${...}` references live in
            # the template half either way.
            if isinstance(value, list):
                if value and isinstance(value[0], str):
                    yield value[0]
            else:
                yield str(value)
        elif fn == "Fn::Join":
            parts = value[1] if isinstance(value, list) and len(value) > 1 else []
            for part in _as_list(parts):
                yield from flatten_resource(part)
        elif isinstance(value, (str, dict, list)):
            for part in _as_list(value):
                yield from flatten_resource(part)


def resource_strings(statement: dict) -> Iterator[str]:
    """Every ``Resource`` entry of one statement, flattened."""
    for resource in _as_list(statement.get("Resource")):
        yield from flatten_resource(resource)


_BUCKET_REF_RE = re.compile(r"\$\{([A-Za-z0-9]+)\}|\b([A-Za-z0-9]+)\.Arn\b")


def buckets_named(statements: Iterable) -> set:
    """Bucket logical ids appearing in any S3 ``Resource`` of ``statements``.

    The ``X.Arn`` alternative is deliberately not anchored to the whole string: a
    ``Fn::Join`` part, or a ``GetAtt`` inside a longer rendering, is still a reference
    to that bucket, and ``^...$`` matched only the case where the ARN was the entire
    ``Resource``.
    """
    named = set()
    for _policy, statement in statements:
        for text in resource_strings(statement):
            for match in _BUCKET_REF_RE.finditer(text):
                candidate = match.group(1) or match.group(2)
                if candidate and candidate.endswith("Bucket"):
                    named.add(candidate)
    return named


_WILDCARD_RESOURCES = frozenset({"*", "arn:aws:s3:::*", "arn:aws:s3:::*/*"})


def is_wildcard_resource(text: str) -> bool:
    """Whether a ``Resource`` reaches every bucket, naming no logical id.

    Its own predicate because a wildcard is the one shape that turns the other
    assertions into false negatives: it contributes no logical id, so the derived set
    still compares equal to the pinned one while the grant reaches the whole account.

    The partition segment is matched with ``.*`` rather than ``[^:]*`` because
    ``${AWS::Partition}`` contains colons of its own — the spelling this repo actually
    writes, and the one a ``[^:]*`` matcher does not recognise as a wildcard.
    """
    stripped = text.strip()
    if stripped in _WILDCARD_RESOURCES:
        return True
    return bool(re.fullmatch(r"arn:.*:s3:::\*(?:/\*)?", stripped))


def browser_readable_buckets() -> set:
    """The set of bucket logical ids the browser's own credentials can read."""
    return buckets_named(role_s3_statements())


def granted_s3_actions() -> set:
    """The S3 actions :data:`ROLE` is granted, e.g. ``{"s3:GetObject", ...}``.

    Derived because the prose gate needs it to tell a statement *about this grant*
    from a statement that merely mentions a bucket and an S3 action in the same
    breath. This role's grant is read-only, so a sentence citing ``s3:PutObject`` is
    not describing it — and a recognizer that accepted any ``s3:`` token read
    "``s3:PutObject`` writes land on the Logging bucket" as a claim that the role
    grants Logging-bucket access, then failed on it.

    Deriving rather than listing matters for the same reason everything else here is
    derived: if the role gains a write action, prose describing that write becomes a
    real claim about the grant and should be checked, without anyone remembering to
    extend a literal list.
    """
    actions = set()
    for _policy, statement in role_s3_statements():
        for action in _as_list(statement.get("Action")):
            text = str(action)
            if text.startswith("s3:"):
                actions.add(text)
    return actions
